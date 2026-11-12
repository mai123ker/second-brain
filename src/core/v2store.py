"""V2 数据层：vault/ 是唯一真相。

- sources/     投喂原文归档，写入后永不改写；只用于溯源，不进知识库
- knowledge/   提炼后的知识点 / 框架，一篇 .md 一条，Obsidian 可直接翻阅
- *.json       对话、处境、待办

索引不单独存盘：每次扫描 knowledge/*.md 重建，删掉也不会丢数据。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from src.storage.paths import ROOT

# 验收 / 演示可用 MIRROR_V2_VAULT 指向临时目录，避免碰到真实知识库
_override = os.environ.get("MIRROR_V2_VAULT", "").strip()
VAULT = Path(_override) if _override else ROOT / "vault"
SOURCES = VAULT / "sources"
KNOWLEDGE = VAULT / "knowledge"
ACTIONS_F = VAULT / "actions.json"
CHAT_F = VAULT / "chat.json"
SITUATIONS_F = VAULT / "situations.json"

DOMAINS = ["制度", "法律", "经济运行", "教育", "组织", "用户增长", "通用认知"]
META_FIELDS = ["id", "type", "domain", "title", "status", "confidence",
               "created", "updated", "sources", "tags", "conflicts"]
LIST_FIELDS = {"sources", "tags", "conflicts"}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def uid(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def ensure() -> None:
    SOURCES.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    ensure()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    tmp.replace(path)


def _dump_meta(meta: dict) -> str:
    lines = ["---"]
    for key in META_FIELDS:
        value = meta.get(key, "")
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(v) for v in value)
        text = str(value).replace("\n", " ")
        lines.append(f"{key}: {text}" if text else f"{key}:")
    lines.append("---")
    return "\n".join(lines)


def _parse_meta(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    end = 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i + 1
            break
        key, _, value = lines[i].partition(":")
        key = key.strip()
        if key not in META_FIELDS:
            continue
        value = value.strip()
        meta[key] = ([p.strip() for p in value.split(",") if p.strip()]
                     if key in LIST_FIELDS else value)
    body = "\n".join(lines[end:]).lstrip("\n") if end else ""
    return meta, body


def write_knowledge(item: dict, body: str) -> dict:
    ensure()
    clean = {k: item.get(k, "") for k in META_FIELDS}
    path = KNOWLEDGE / f"{clean['id']}.md"
    path.write_text(_dump_meta(clean) + "\n" + body.strip() + "\n",
                    encoding="utf-8", newline="\n")
    return clean


def all_knowledge() -> list[dict]:
    ensure()
    out = []
    for path in sorted(KNOWLEDGE.glob("*.md")):
        try:
            meta, body = _parse_meta(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not meta.get("id"):
            continue
        meta["body"] = body
        out.append(meta)
    rank = {"frame": 0, "point": 1}
    out.sort(key=lambda x: x.get("updated", ""), reverse=True)
    out.sort(key=lambda x: rank.get(x.get("type"), 2))
    return out


def get_knowledge(kid: str) -> dict | None:
    for item in all_knowledge():
        if item.get("id") == kid:
            return item
    return None


SOURCE_FIELDS = ["id", "kind", "title", "created", "origin", "url", "chars", "immutable"]


def write_source(kind: str, title: str, text: str, origin: str = "", url: str = "") -> dict:
    """原文只写一次。之后任何路径都不得改写 sources/ 里的文件。"""
    ensure()
    meta = {"id": uid("s"), "kind": kind, "title": (title or "未命名")[:80],
            "created": now(), "origin": origin[:120], "url": url[:400],
            "chars": len(text), "immutable": "true"}
    head = "---\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n---\n\n"
    (SOURCES / f"{meta['id']}.md").write_text(head + text.rstrip() + "\n",
                                              encoding="utf-8", newline="\n")
    return meta


def all_sources() -> list[dict]:
    ensure()
    out = []
    for path in sorted(SOURCES.glob("*.md"), reverse=True):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        meta: dict = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            if key.strip() in SOURCE_FIELDS:
                meta[key.strip()] = value.strip()
        if meta.get("id"):
            out.append(meta)
    return out


def chat_data() -> dict:
    return read_json(CHAT_F, {"messages": []})


def append_chat(role: str, text: str, ledger: dict | None = None, via: str = "") -> dict:
    data = chat_data()
    msg = {"id": uid("m"), "role": role, "text": text, "created": now(),
           "ledger": ledger or None, "via": via}
    data.setdefault("messages", []).append(msg)
    write_json(CHAT_F, data)
    return msg


def actions_data() -> dict:
    return read_json(ACTIONS_F, {"items": [], "situation": ""})


def save_actions(data: dict) -> None:
    write_json(ACTIONS_F, data)


def situations_data() -> dict:
    return read_json(SITUATIONS_F, {"items": []})


def save_situations(data: dict) -> None:
    write_json(SITUATIONS_F, data)
