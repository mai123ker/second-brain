"""文档 ID 与语义化文件名：像 Obsidian 一样用主题名当文件名。"""
from __future__ import annotations

import re
import secrets

_FORBIDDEN = r'[\s/:?*"<>|\\\r\n\t]'
_KEEP = r"[^\u4e00-\u9fffA-Za-z0-9_\-]"


def make_id(prefix: str = "doc") -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


def safe_name(title: str) -> str:
    name = re.sub(_FORBIDDEN, "", str(title or "").strip())
    name = re.sub(_KEEP, "", name).strip(".-_")
    return name[:24] or "untitled"


def unique_path(directory, title: str) -> tuple[str, int]:
    """返回 (文件名, 重名序号)；重名追加 -2、-3，不覆盖已有文件。"""
    base = safe_name(title)
    dup = 0
    while True:
        name = f"{base}.md" if dup == 0 else f"{base}-{dup + 1}.md"
        if not (directory / name).exists():
            return name, dup
        dup += 1
