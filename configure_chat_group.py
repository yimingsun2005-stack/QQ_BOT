"""向服务器现有配置追加聊天群，不修改画图白名单。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import tempfile
from pathlib import Path


def add_chat_group(config_path: Path, group_id: str) -> Path | None:
    group_id = group_id.strip()
    if not group_id or not group_id.isascii() or not group_id.isdigit():
        raise ValueError("群号必须只包含数字")
    config_path = config_path.resolve(strict=True)
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    groups = data.get("allowed_groups", [])
    if not isinstance(groups, list) or any(
        type(group) not in (str, int)
        or not str(group).isascii()
        or not str(group).isdigit()
        for group in groups
    ):
        raise ValueError("聊天白名单必须是群号数组")
    if group_id in {str(group) for group in groups}:
        return None
    data["allowed_groups"] = [*groups, group_id]
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    backup_fd, backup_name = tempfile.mkstemp(
        prefix=config_path.name + ".bak.", dir=config_path.parent,
    )
    os.close(backup_fd)
    backup_path = Path(backup_name)
    shutil.copy2(config_path, backup_path)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=config_path.name + ".tmp.", dir=config_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        shutil.copymode(config_path, temporary_path)
        temporary_path.replace(config_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.json"),
        help="服务器现有 config.json 的路径（默认与脚本同目录）",
    )
    args = parser.parse_args()
    try:
        group_id = getpass.getpass("请输入要加入聊天白名单的群号（输入隐藏）：")
        backup = add_chat_group(args.config, group_id)
    except (OSError, ValueError, EOFError):
        print("配置未更新：请检查配置路径、JSON 格式、白名单格式和群号输入。")
        return 1
    except KeyboardInterrupt:
        print("已取消。")
        return 1
    if backup is None:
        print("该群已允许聊天，无需修改配置；画图白名单保持原样。")
    else:
        print("已追加聊天白名单并备份原配置；画图白名单保持原样。请重启 Bot。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
