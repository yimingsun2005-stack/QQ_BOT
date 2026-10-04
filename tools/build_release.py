"""Build deterministic, allowlisted archives from a blank template."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md", "CHANGELOG.md", "requirements.txt", "config.example.json", ".gitignore",
    "ai_client.py", "bot.py", "chat_config.py", "group_context.py", "message_utils.py",
    "music_service.py", "novelai_image_service.py", "runtime_config.py", "single_instance.py",
    "deploy/qq-bot.service", "tools/build_release.py", "tools/check_release.py",
)


def blank(value):
    if isinstance(value, dict):
        return {key: blank(item) for key, item in value.items()}
    if isinstance(value, list):
        return []
    return "" if isinstance(value, str) else None


def build(output: Path, version: str = "v1.1.0") -> list[Path]:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid version")
    output.mkdir(parents=True, exist_ok=True)
    template = (json.dumps(blank(json.loads((ROOT / "config.example.json").read_text(
        encoding="utf-8"))), ensure_ascii=False, indent=2) + "\n").encode()
    content = {name: (ROOT / name).read_bytes() for name in FILES}
    content["config.example.json"] = template
    content["config.json"] = template
    zip_path = output / f"QQ_BOT-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(content.items()):
            info = zipfile.ZipInfo("QQ_BOT/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
    tar_path = output / f"QQ_BOT-{version}.tar.gz"
    with tar_path.open("wb") as file, gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, body in sorted(content.items()):
                info = tarfile.TarInfo("QQ_BOT/" + name)
                info.size, info.mode, info.mtime = len(body), 0o644, 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(body))
    sums = output / "SHA256SUMS.txt"
    sums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                            for path in (zip_path, tar_path)), encoding="utf-8")
    return [zip_path, tar_path, sums]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", default="v1.1.0")
    args = parser.parse_args()
    for path in build(args.output, args.version):
        print(path.name)
