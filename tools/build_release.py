"""Build public archives from an explicit allowlist, never from local config.json."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import zipfile

VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parents[1]
FILES = (
    '.gitignore', '.gitattributes', 'README.md', 'CHANGELOG.md', 'requirements.txt',
    'config.example.json', 'bot.py', 'runtime_config.py', 'single_instance.py',
    'ai_client.py', 'group_context.py', 'message_utils.py', 'music_service.py',
    'novelai_image_service.py', 'web_search_service.py', 'configure_chat_group.py',
    'deploy/qq-bot.service', 'tools/build_release.py', '.github/workflows/ci.yml',
    'tests/test_ai_client.py', 'tests/test_bot.py', 'tests/test_bot_config.py',
    'tests/test_configure_chat_group.py', 'tests/test_group_context.py',
    'tests/test_message_utils.py', 'tests/test_music_service.py', 'tests/test_novelai.py',
    'tests/test_web_search_service.py', 'tests/test_server_release.py',
    'tests/test_single_instance.py', 'tests/test_server_lifecycle.py',
)


def assert_blank(value):
    if isinstance(value, dict):
        if not value:
            raise ValueError('Blank configuration must preserve fields')
        for item in value.values():
            assert_blank(item)
    elif value is not None and value != '' and value != []:
        raise ValueError('Public configuration contains a non-blank value')


def release_contents(root: Path = ROOT) -> dict[str, bytes]:
    contents = {name: (root / name).read_bytes() for name in FILES}
    assert_blank(json.loads(contents['config.example.json']))
    contents['config.json'] = contents['config.example.json']
    return contents


def build(destination: Path, root: Path = ROOT) -> list[Path]:
    contents = release_contents(root)
    destination.mkdir(parents=True, exist_ok=True)
    zip_path = destination / f'QQ_BOT-v{VERSION}.zip'
    tar_path = destination / f'QQ_BOT-v{VERSION}.tar.gz'
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo('QQ_BOT/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with tarfile.open(tar_path, 'w:gz') as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo('QQ_BOT/' + name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(data))
    checksums = destination / 'SHA256SUMS.txt'
    checksums.write_text(''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in (zip_path, tar_path)), encoding='utf-8')
    return [zip_path, tar_path, checksums]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    for path in build(args.output):
        print(path.name)
