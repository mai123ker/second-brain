"""对话记录落盘：records/_chat/<doc_id>.json。

- 属于用户内容，但不进文档树、不进搜索索引（目录以 _ 开头，两处都会跳过）。
- 换浏览器、清缓存后仍可找回；同时提供 Markdown / JSON 导出。
- 只写这一个目录，绝不改动任何 .md 原文。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from src.storage.paths import ROOT, VAULT

CHAT_DIR = VAULT / "_chat"
MAX_KEEP = 100


def _filename(doc_id: str) -> str:
    key = str(doc_id or "_none").strip() or "_none"
    if re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", key):
        return key + ".json"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json"


def path_for(doc_id: str):
    return CHAT_DIR / _filename(doc_id)


def load(doc_id: str) -> dict:
    path = path_for(doc_id)
    if not path.exists():
        return {"messages": [], "updated": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"messages": [], "updated": ""}
    if not isinstance(data, dict):
        return {"messages": [], "updated": ""}
    messages = data.get("messages")
    return {"messages": messages if isinstance(messages, list) else [],
            "updated": str(data.get("updated") or "")}


def save(doc_id: str, messages: list) -> dict:
    """全量覆盖写。用临时文件 + replace，避免写一半断电留下半截 JSON。"""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    path = path_for(doc_id)
    now = datetime.now().isoformat(timespec="seconds")
    kept = list(messages or [])[-MAX_KEEP:]
    payload = {"doc_id": str(doc_id or ""), "updated": now, "messages": kept}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"updated": now, "count": len(kept), "path": path.relative_to(ROOT).as_posix()}


def append(doc_id: str, messages: list) -> dict:
    return save(doc_id, load(doc_id)["messages"] + list(messages or []))


def export_markdown(doc_id: str, title: str = "") -> str:
    data = load(doc_id)
    lines = [f"# 对话记录 · {title or doc_id or '未打开文档'}", "",
             f"导出时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    if not data["messages"]:
        lines += ["（这篇文档还没有对话记录）", ""]
    for item in data["messages"]:
        if not isinstance(item, dict):
            continue
        who = "我" if item.get("role") == "user" else "模型"
        lines += [f"## {who}", "", str(item.get("content") or "")]
        chips = item.get("attach") or []
        if chips:
            lines += ["", "引用文档：" + "、".join(str(c) for c in chips)]
        if item.get("patch"):
            lines += ["", "**可插入正文**", "", "```markdown",
                      str(item["patch"]), "```"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_json(doc_id: str) -> str:
    data = load(doc_id)
    return json.dumps({"doc_id": doc_id, **data}, ensure_ascii=False, indent=2)
