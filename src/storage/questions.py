"""文档主动提问的收件箱：records/_chat/questions.json。

- 属于对话侧数据，不进文档树、不进搜索索引（_chat 目录两处都跳过）。
- 每条问题必须挂着来源文档（doc_id），回答时带上下文进对话。
- 只写这一个 JSON，绝不改动任何 .md 原文。
- 状态机：pending（等你答）→ done（已答或已忽略）。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime

from src.storage.paths import ROOT, VAULT

QUESTIONS_PATH = VAULT / "_chat" / "questions.json"
MAX_KEEP = 50


def _load() -> dict:
    if not QUESTIONS_PATH.exists():
        return {"items": [], "last_generated": ""}
    try:
        data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": [], "last_generated": ""}
    if not isinstance(data, dict):
        return {"items": [], "last_generated": ""}
    items = data.get("items")
    return {"items": items if isinstance(items, list) else [],
            "last_generated": str(data.get("last_generated") or "")}


def _save(data: dict) -> None:
    QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    kept = list(data.get("items") or [])[-MAX_KEEP:]
    payload = {"items": kept,
               "last_generated": str(data.get("last_generated") or "")}
    tmp = QUESTIONS_PATH.with_name(QUESTIONS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUESTIONS_PATH)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pending() -> list[dict]:
    return [item for item in _load()["items"]
            if isinstance(item, dict) and item.get("status") == "pending"]


def last_generated() -> str:
    return _load()["last_generated"]


def add(doc_id: str, doc_title: str, questions: list[dict]) -> list[dict]:
    """一批问题一次写入。每条 {question, reason}，落库补 id/created/status。"""
    data = _load()
    now = _now()
    made = []
    for q in questions:
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        item = {"id": "q-" + secrets.token_hex(4),
                "doc_id": str(doc_id or ""),
                "doc_title": str(doc_title or ""),
                "question": text[:500],
                "reason": str(q.get("reason") or "").strip()[:300],
                "status": "pending", "created": now}
        data["items"].append(item)
        made.append(item)
    data["last_generated"] = now
    _save(data)
    return made


def consume(qid: str) -> bool:
    """回答过 / 忽略过，就从收件箱消失（保留历史行，便于追溯）。"""
    data = _load()
    hit = False
    for item in data["items"]:
        if isinstance(item, dict) and item.get("id") == qid and item.get("status") == "pending":
            item["status"] = "done"
            item["closed"] = _now()
            hit = True
    if hit:
        _save(data)
    return hit
