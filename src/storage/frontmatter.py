"""frontmatter：一篇长 .md 文档的元数据。

形态变更（用户 2026-11-11 拍板）：一个主题 = 一篇长文档，
正文是完整 Markdown（标题、段落、表格、- [ ] 待办、结论区块），
不再是一条记录一个碎片文件。字段顺序固定，空值留空但保留键。
"""
from __future__ import annotations

FIELDS = [
    "id", "title", "stream", "kind", "target", "topic", "version", "confidence",
    "status", "created", "updated", "source", "stance", "scope", "claim_relation",
]
_INT_FIELDS = {"version", "confidence"}


def parse_document(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    body_start = 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        line = lines[i]
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key not in FIELDS:
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [p.strip() for p in inner.split(",") if p.strip()] if inner else []
        elif key in _INT_FIELDS:
            try:
                meta[key] = int(value)
            except (TypeError, ValueError):
                meta[key] = value
        else:
            meta[key] = value
    body = "\n".join(lines[body_start:]).lstrip("\n") if body_start else ""
    return meta, body


def parse(text: str) -> dict:
    meta, _ = parse_document(text)
    return meta


def dump(meta: dict) -> str:
    out = ["---"]
    for key in FIELDS:
        value = meta.get(key, "")
        if value is None:
            value = ""
        if isinstance(value, (list, tuple)):
            value = "[" + ", ".join(str(v) for v in value) + "]"
        elif isinstance(value, bool):
            value = "true" if value else "false"
        value = str(value)
        out.append(f"{key}: {value}" if value else f"{key}:")
    out.append("---")
    return "\n".join(out) + "\n"


def write_document(path, meta: dict, body: str) -> None:
    """UTF-8 无 BOM、LF 换行、末尾保留一个换行符。"""
    from pathlib import Path
    text = dump(meta) + "\n" + body.rstrip() + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")
