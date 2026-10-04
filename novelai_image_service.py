from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import secrets
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import aiohttp


API_URL = "https://image.novelai.net/ai/generate-image"
MODEL = "nai-diffusion-5-full"
HEAVY_NEGATIVE_PROMPT = (
    "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, "
    "jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, "
    "screentone, multiple views, logo, too many watermarks, negative space, blank page"
)
SAMPLERS = {
    "Euler Ancestral": "k_euler_ancestral",
    "Euler": "k_euler",
    "DPM++ 2M": "k_dpmpp_2m",
    "DPM++ 2S Ancestral": "k_dpmpp_2s_ancestral",
    "DPM++ SDE": "k_dpmpp_sde",
    "DPM++ 2M SDE": "k_dpmpp_2m_sde",
}
MAX_PROMPT_CHARS = 4000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024


class NovelAIServiceError(RuntimeError):
    """只携带可公开显示的错误，不包含服务端响应或凭证。"""


@dataclass(frozen=True)
class NovelAIImageConfig:
    enabled: bool = False
    token: str = field(default="", repr=False)
    width: int = 832
    height: int = 1216
    steps: int = 23
    guidance: float = 7.0
    sampler: str = "k_euler_ancestral"
    timeout_seconds: float = 180.0
    allowed_groups: list[str] = field(default_factory=list, repr=False)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("NovelAI 画图开关必须是布尔值")
        if not isinstance(self.allowed_groups, list) or any(
            not isinstance(group, str) or not group.isascii() or not group.isdigit()
            for group in self.allowed_groups
        ):
            raise ValueError("画图群号必须是只包含数字字符串的数组")
        if not isinstance(self.token, str) or any(c in self.token for c in "\r\n"):
            raise ValueError("NovelAI Token 格式无效")
        for label, value in (("宽度", self.width), ("高度", self.height)):
            if type(value) is not int or not 64 <= value <= 2048 or value % 64:
                raise ValueError(f"画图{label}必须为 64–2048 之间的 64 倍数")
        if self.width * self.height > 2097152:
            raise ValueError("画图总像素不能超过 2,097,152")
        if type(self.steps) is not int or not 1 <= self.steps <= 50:
            raise ValueError("画图 Steps 必须是 1–50 之间的整数")
        for label, value, minimum, maximum in (
            ("Guidance", self.guidance, 0, 10),
            ("超时时间", self.timeout_seconds, 1, 300),
        ):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not minimum <= value <= maximum):
                raise ValueError(f"画图{label}必须在 {minimum}–{maximum} 之间")
        if not isinstance(self.sampler, str) or self.sampler not in SAMPLERS.values():
            raise ValueError("画图采样器无效")

    @classmethod
    def from_mapping(cls, data: Any) -> NovelAIImageConfig:
        if not isinstance(data, dict):
            raise ValueError("novelai_image_generation 必须是 JSON 对象")
        config = cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
        config.validate()
        return config


def parse_image_group_ids(value: str) -> list[str]:
    groups = re.split(r"[\s,，;；]+", value.strip())
    result: list[str] = []
    for group in groups:
        if not group:
            continue
        if not group.isascii() or not group.isdigit():
            raise ValueError("画图群号只能包含数字，多个群号请用逗号、空格或换行分隔")
        if group not in result:
            result.append(group)
    return result


def parse_image_command(text: str) -> str | None:
    match = re.fullmatch(r"\s*画图(?:\s+(.*))?\s*", text, flags=re.DOTALL)
    return match.group(1).strip() if match and match.group(1) else ("" if match else None)


def validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise NovelAIServiceError("请在“画图”后输入提示词，例如：@Bot 画图 1girl, sunset")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise NovelAIServiceError("画图提示词不能超过 4000 字符，请缩短后再试。")


def validate_png(body: bytes) -> None:
    """验证 PNG 结构、校验和及像素数据，并限制解压大小。"""
    invalid = NovelAIServiceError("画图服务返回的图片无效，请稍后再试。")
    if not 45 <= len(body) <= MAX_IMAGE_BYTES or body[:8] != b"\x89PNG\r\n\x1a\n":
        raise invalid
    offset, dimensions, compressed, ended = 8, None, bytearray(), False
    try:
        while offset + 12 <= len(body):
            length = struct.unpack_from(">I", body, offset)[0]
            kind = body[offset + 4:offset + 8]
            end = offset + 8 + length
            if end + 4 > len(body):
                raise invalid
            data = body[offset + 8:end]
            if zlib.crc32(kind + data) & 0xffffffff != struct.unpack_from(">I", body, end)[0]:
                raise invalid
            if offset == 8:
                if kind != b"IHDR" or length != 13:
                    raise invalid
                width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if (not 1 <= width <= 2048 or not 1 <= height <= 2048
                        or width * height > 2097152 or depth != 8 or color not in (2, 6)
                        or compression or filtering or interlace):
                    raise invalid
                dimensions = (width, height, 3 if color == 2 else 4)
            elif kind == b"IHDR":
                raise invalid
            if kind == b"IDAT":
                compressed.extend(data)
            offset = end + 4
            if kind == b"IEND":
                ended = length == 0 and offset == len(body)
                break
        if not ended or dimensions is None or not compressed:
            raise invalid
        width, height, channels = dimensions
        expected = (width * channels + 1) * height
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if not decoder.eof or decoder.unused_data or len(pixels) != expected:
            raise invalid
        if any(pixels[i] > 4 for i in range(0, expected, width * channels + 1)):
            raise invalid
    except (struct.error, zlib.error) as exc:
        raise invalid from exc


class NovelAIImageService:
    def __init__(self, config: NovelAIImageConfig) -> None:
        config.validate()
        self.config = config

    def build_payload(self, prompt: str, *, model: str='nai-diffusion-5-curated') -> dict[str, Any]:
        if model not in ('nai-diffusion-5-full', 'nai-diffusion-5-curated'):
            raise NovelAIServiceError('画图模型无效，请检查设置。')
        validate_prompt(prompt)
        return {'input': prompt, 'model': model, 'action': 'generate', 'parameters': {'params_version': 4, 'width': self.config.width, 'height': self.config.height, 'steps': self.config.steps, 'scale': self.config.guidance, 'sampler': self.config.sampler, 'n_samples': 1, 'seed': secrets.randbelow(2 ** 32), 'negative_prompt': HEAVY_NEGATIVE_PROMPT, 'v4_prompt': {'caption': {'base_caption': prompt, 'char_captions': []}, 'use_coords': False, 'use_order': True}, 'v4_negative_prompt': {'caption': {'base_caption': HEAVY_NEGATIVE_PROMPT, 'char_captions': []}, 'legacy_uc': False}, 'noise_schedule': 'native', 'cfg_rescale': 0, 'sm': False, 'sm_dyn': False, 'dynamic_thresholding': False, 'qualityToggle': False, 'deliberate_euler_ancestral_bug': False, 'prefer_brownian': True}}

    async def generate(self, prompt: str, *, model: str='nai-diffusion-5-curated') -> bytes:
        payload = self.build_payload(prompt, model=model)
        if not self.config.token.strip():
            raise NovelAIServiceError('请先在配置中填写 NovelAI Token。')
        headers = {'Authorization': f'Bearer {self.config.token.strip()}', 'Accept': 'application/json'}
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds), trust_env=True) as session:
                async with session.post(API_URL, json=payload, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        raise NovelAIServiceError(self._status_error(response.status))
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise NovelAIServiceError('画图服务返回的数据过大，请稍后再试。')
            return await asyncio.to_thread(self.parse_response, bytes(body))
        except asyncio.TimeoutError:
            raise NovelAIServiceError('画图超时，请稍后再试。') from None
        except aiohttp.ClientProxyConnectionError:
            raise NovelAIServiceError('无法连接本机代理，请检查代理是否已启动。') from None
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError):
            raise NovelAIServiceError('画图服务的安全连接校验失败，请检查系统时间和代理设置。') from None
        except aiohttp.ClientError:
            raise NovelAIServiceError('无法连接画图服务，请检查网络和系统代理后再试。') from None

    @staticmethod
    def _status_error(status: int) -> str:
        if status in (401, 403):
            return "NovelAI Token 无效或无权限，请检查设置。"
        if status == 402:
            return "NovelAI 额度不足，请检查账户。"
        if status == 429:
            return "画图请求过于频繁，请稍后再试。"
        return "画图服务暂时不可用，请稍后再试。"

    @staticmethod
    def parse_response(body: bytes) -> bytes:
        try:
            data = json.loads(body)
            images = data["images"]
            if not isinstance(images, list) or len(images) != 1:
                raise ValueError
            image = base64.b64decode(images[0]["image"], validate=True)
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error):
            raise NovelAIServiceError("画图服务返回的图片无效，请稍后再试。") from None
        validate_png(image)
        return image
