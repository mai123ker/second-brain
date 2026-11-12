"""文档主动提问：让库里的一篇文档开口向作者提问。

- 诚实前提：纯本地应用收不到推送，「主动」= 打开应用时问题已在收件箱 + 手动触发现场问。
- 素材不足不调模型：文档太短（<80 字）直接 NEEDS_MATERIAL，不烧 token。
- 没配模型 / 模型挂了 → LLM_UNAVAILABLE，前端如实显示。
- 问题由用户点「去回答」才进对话，AI 永远不自动写任何东西。
"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.llm import client
from src.storage import index, questions, repo

router = APIRouter(tags=["proactive"])

MIN_CHARS = 80
MAX_QUESTIONS = 3
PROMPT_NAME = "proactive_system.md"


def _load_prompt() -> str:
    from src.storage.paths import PROMPTS
    path = PROMPTS / PROMPT_NAME
    if not path.exists():
        raise ApiError("LLM_UNAVAILABLE", f"缺少 prompts/{PROMPT_NAME}")
    return path.read_text(encoding="utf-8")


def _doc_block(doc: dict) -> str:
    title = str(doc.get("title") or "未命名")
    body = str(doc.get("body") or "").strip()
    if len(body) > 4000:
        body = body[:4000] + "\n……（正文过长，已截断）"
    return f"《{title}》\n\n{body}"


def _parse_questions(raw: str) -> list[dict]:
    """从模型回复里抠出 JSON 数组；解析不了就当问不出（诚实降级为空）。"""
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        items = json.loads(text)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:MAX_QUESTIONS]:
        if isinstance(item, dict) and str(item.get("question") or "").strip():
            out.append({"question": str(item.get("question")).strip(),
                        "reason": str(item.get("reason") or "").strip()})
    return out


def _pick_doc(doc_id: str = "") -> dict:
    """指定了 doc_id 就用它；否则挑最近更新的、够长的一篇文档。

    字段口径统一成 body（index 记录叫 content，这里归一），标题取 title。
    """
    if doc_id:
        doc = index.by_id(doc_id) or repo.read_doc(doc_id)
        if not doc:
            raise ApiError("INVALID_PARAM", "库里没有这篇文档")
    else:
        recs = [r for r in index.all_records()
                if not any(part.startswith("_") or part.startswith(".")
                           for part in str(r.get("path") or "").split("/"))
                and len(str(r.get("content") or "")) >= MIN_CHARS]
        if not recs:
            raise ApiError("NEEDS_MATERIAL",
                           "库里还没有够长（80 字以上）的文档，先写点什么再让它问。")
        doc = max(recs, key=lambda r: str(r.get("updated") or ""))
    if not doc.get("body"):
        doc = dict(doc, body=doc.get("content") or "")
    return doc


def _mock_questions(doc: dict) -> list[dict]:
    title = str(doc.get("title") or "这篇文档")
    return [{"question": f"（演示模式）《{title}》里哪个结论你最想先补上证据？",
             "reason": "演示模式下没有真模型，这是占位问题。"}]


class GenerateIn(BaseModel):
    doc_id: str = ""


@router.post("/questions/generate")
def post_generate(payload: GenerateIn):
    """让一篇文档（默认挑最近更新的）向作者提问，落进收件箱。"""
    doc = _pick_doc(payload.doc_id)
    body_chars = len(str(doc.get("body") or "").strip())
    if body_chars < MIN_CHARS:
        raise ApiError("NEEDS_MATERIAL", f"《{doc.get('title')}》只有 {body_chars} 字，素材不够，问不出值得问的。")
    try:
        if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
            found = _mock_questions(doc)
        else:
            raw = client.chat_text(_load_prompt(),
                                   [{"role": "user", "content": _doc_block(doc)}])
            found = _parse_questions(raw)
    except client.LLMUnavailable as exc:
        raise ApiError("LLM_UNAVAILABLE", f"模型不可用：{exc}") from exc
    if not found:
        return ok({"questions": [], "doc_id": doc.get("id"),
                   "doc_title": doc.get("title"),
                   "message": "它觉得没什么拿不准的——这本身也是一种诚实。"})
    made = questions.add(doc.get("id"), str(doc.get("title") or ""), found)
    return ok({"questions": made, "doc_id": doc.get("id"),
               "doc_title": doc.get("title")})


@router.get("/questions/pending")
def get_pending():
    return ok({"items": questions.pending(),
               "last_generated": questions.last_generated()})


class ConsumeIn(BaseModel):
    id: str = ""


@router.post("/questions/consume")
def post_consume(payload: ConsumeIn):
    hit = questions.consume(payload.id)
    if not hit:
        raise ApiError("INVALID_PARAM", "收件箱里没有这条问题（可能已被处理）")
    return ok({"consumed": payload.id})
