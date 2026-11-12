"""V2 接口：投喂 / 对话 / 知识库 / 个人使用说明 / 处境与待办 / 回补 / 检索。

统一挂在 /v2 前缀下，与旧 P0 接口互不影响；旧界面仍可从 /_legacy/ 打开。
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.api.ingest import _decode_text, _docx_text, _pdf_text, _title_stem
from src.core import v2action as act, v2distill as dz
from src.core import v2store as st, v2text as tx
from src.llm import client
from src.storage.paths import PROMPTS

router = APIRouter(prefix="/v2", tags=["v2"])

DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.+)$", re.S)
TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}
MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 20000


class TextIn(BaseModel):
    text: str = ""
    title: str = ""
    kind: str = "text"


class FileIn(BaseModel):
    name: str = ""
    data: str = ""


class LinkIn(BaseModel):
    url: str = ""
    title: str = ""
    note: str = ""


class ChatIn(BaseModel):
    text: str = ""
    via: str = ""


class PlanIn(BaseModel):
    situation: str = ""


class ReflectIn(BaseModel):
    text: str = ""


def _prompt(name: str) -> str:
    path = PROMPTS / name
    if not path.exists():
        raise ApiError("STORAGE_ERROR", f"缺少 prompts/{name}")
    return path.read_text(encoding="utf-8")


def _cut(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS] + "\n\n[已截断：原文超过 2 万字，只整理前 2 万字]", True


def _ledger(result: dict) -> dict:
    norm = result.get("normalized") or {}
    return {"summary": result.get("summary", ""), "changes": result.get("changes", []),
            "mode": result.get("mode", "rule"),
            "removed": {"filler": norm.get("removed_filler", 0),
                        "emotion": norm.get("removed_emotion", 0),
                        "duplicate": norm.get("removed_duplicate", 0)}}


def _feed(title: str, result: dict) -> None:
    """投喂结果也留在对话流里（只记整理结果，不搬原文）。"""
    st.append_chat("feed", title, ledger=_ledger(result))


def _ingest(text: str, kind: str, title: str, origin: str = "", url: str = "") -> dict:
    body, truncated = _cut(text)
    source = st.write_source(kind, title, body, origin=origin, url=url)
    result = dz.distill(body, source)
    result["truncated"] = truncated
    _feed(f"投喂 · {title}", result)
    return result


def _rule_reply(text: str) -> str:
    sentences = tx.normalize(text)["sentences"]
    if not sentences:
        return "这句没有可沉淀的内容。说说具体发生了什么、你在意哪一点。"
    head = sentences[0][:38]
    if any(word in text for word in ("因为", "如果", "取决于", "前提", "所以", "机制")):
        return f"我把「{head}」按框架收了。它成立的条件是什么？"
    return f"留下了一条判断：「{head}」。它跟库里哪条已有框架是同一件事吗？"


def _chat_reply(text: str) -> str:
    mode = os.environ.get("MIRROR_LLM_MODE", "").strip().lower()
    if mode in ("off", "none", "mock") or not client.configured():
        return _rule_reply(text)
    try:
        return client.chat_text(_prompt("v2_chat_system.md"),
                                [{"role": "user", "content": text}])[:400]
    except Exception:
        return _rule_reply(text)


@router.get("/state")
def get_state():
    return ok(act.state())


@router.get("/knowledge")
def get_knowledge(domain: str = "", type: str = "", status: str = "", q: str = ""):
    items = st.all_knowledge()
    if domain:
        items = [k for k in items if k.get("domain") == domain]
    if type in ("frame", "point"):
        items = [k for k in items if k.get("type") == type]
    if status in ("absorbed", "unabsorbed"):
        items = [k for k in items if k.get("status") == status]
    if q.strip():
        items = [k for k in items
                 if q.strip() in f"{k.get('title', '')} {k.get('body', '')}"]
    return ok({"items": [dz._brief(k) for k in items],
               "total": len(st.all_knowledge())})


@router.get("/knowledge/{kid}")
def get_knowledge_one(kid: str):
    item = st.get_knowledge(kid)
    if not item:
        raise ApiError("INVALID_PARAM", "找不到这条知识。")
    brief = dz._brief(item)
    brief["body"] = item.get("body", "")
    brief["sources_detail"] = [s for s in st.all_sources()
                               if s.get("id") in (item.get("sources") or [])]
    return ok({"item": brief})


@router.post("/knowledge/{kid}/absorb")
def post_absorb(kid: str):
    item = st.get_knowledge(kid)
    if not item:
        raise ApiError("INVALID_PARAM", "找不到这条知识。")
    st.write_knowledge(dict(item, status="absorbed", updated=st.now()), item.get("body", ""))
    return ok({"id": kid, "status": "absorbed"})


@router.get("/manual")
def get_manual():
    return ok(dz.manual())


@router.get("/sources")
def get_sources():
    return ok({"items": st.all_sources()})


@router.get("/chat/history")
def get_chat_history():
    return ok({"messages": st.chat_data().get("messages", [])})


@router.post("/chat")
def post_chat(payload: ChatIn):
    text = str(payload.text or "").strip()
    if not text:
        raise ApiError("INVALID_PARAM", "先写点什么。")
    user_msg = st.append_chat("user", text, via=payload.via)
    reply = _chat_reply(text)
    # 原文只进归档；标题保持中性，避免把对话原句顺着「来源」带进知识库正文
    source = st.write_source("chat", f"对话片段 · {st.now()}", text, origin="chat")
    result = dz.distill(text, source)
    assistant = st.append_chat("assistant", reply, ledger=_ledger(result))
    return ok({"user": user_msg, "assistant": assistant, "reply": reply,
               "distill": result})


@router.post("/distill")
def post_distill(payload: TextIn):
    text = str(payload.text or "").strip()
    if not text:
        raise ApiError("INVALID_PARAM", "没有可整理的内容。")
    return ok(_ingest(text, payload.kind or "text", payload.title or "手动整理"))


@router.post("/ingest/text")
def post_ingest_text(payload: TextIn):
    text = str(payload.text or "").strip()
    if not text:
        raise ApiError("INVALID_PARAM", "内容是空的。")
    kind = payload.kind if payload.kind in ("text", "book", "note") else "text"
    return ok(_ingest(text, kind, payload.title or "粘贴的文本"))


@router.post("/ingest/file")
def post_ingest_file(payload: FileIn):
    name = str(payload.name or "").strip()
    match = DATA_URL.match(str(payload.data or "").strip())
    if not name or not match:
        raise ApiError("INVALID_PARAM", "文件内容格式不对（需要文件名 + base64 data URL）。")
    blob = base64.b64decode(match.group(2) + "=" * (-len(match.group(2)) % 4))
    if len(blob) > MAX_BYTES:
        raise ApiError("INVALID_PARAM", "文件超过 8 MB，先拆小一点。")
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        text, kind = _pdf_text(blob), "pdf"
    elif ext == ".docx":
        text, kind = _docx_text(blob), "docx"
    elif ext in TEXT_EXTS:
        text, kind = _decode_text(blob), ("book" if ext in (".md", ".markdown") else "text")
    else:
        raise ApiError("UNSUPPORTED_FILE", "支持 .pdf / .docx / .md / .txt / .csv；其他格式先转成文本。")
    return ok(_ingest(text, kind, _title_stem(name), origin=name))


@router.post("/ingest/link")
def post_ingest_link(payload: LinkIn):
    url = str(payload.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ApiError("INVALID_PARAM", "链接要以 http:// 或 https:// 开头。")
    host = urlparse(url).netloc or "视频链接"
    title = str(payload.title or "").strip() or f"视频链接 · {host}"
    note = str(payload.note or "").strip()
    if not note:
        source = st.write_source("link", title, f"（仅登记链接，未投喂字幕或摘要）\n{url}",
                                 origin="link", url=url)
        result = {"source": source, "summary": "只登记了链接；把字幕或摘要贴进来才能提炼，系统不会擅自抓取内容。",
                  "changes": [], "normalized": tx.normalize(""), "mode": "rule",
                  "needs_text": True, "truncated": False}
        _feed(f"链接 · {title}", result)
        return ok(result)
    return ok(_ingest(note, "link", title, origin="link", url=url))


@router.post("/import/records")
def post_import_records():
    summary = dz.import_records()
    st.append_chat("feed", "导入现有文档", ledger={
        "summary": f"已把 {summary['docs']} 篇文档整理进知识库：新增 {summary['created']} 条、并入 {summary['merged']} 条。",
        "changes": [], "mode": "rule", "removed": {"filler": 0, "emotion": 0, "duplicate": 0}})
    return ok(summary)


@router.get("/search")
def get_search(q: str = ""):
    query = q.strip()
    if not query:
        return ok({"items": [], "q": query})
    results = []
    for k in st.all_knowledge():
        hay = f"{k.get('title', '')} {k.get('body', '')}"[:600]
        score = tx.similarity(query, hay) + (0.5 if query in k.get("title", "") else 0)
        if query in hay or score >= 0.12:
            results.append({"kind": "knowledge", "id": k.get("id"),
                            "title": k.get("title"), "score": round(score, 2),
                            "snippet": dz.statement_of(k.get("body", ""))[:90],
                            "meta": f"{k.get('type')} · {k.get('domain')}"})
    for s in st.all_sources():
        if query in f"{s.get('title', '')} {s.get('origin', '')}":
            results.append({"kind": "source", "id": s.get("id"), "title": s.get("title"),
                            "score": 1.0, "snippet": "原文归档（不可变）",
                            "meta": s.get("kind", "")})
    for a in st.actions_data().get("items", []):
        if query in f"{a.get('title', '')} {a.get('why', '')}":
            results.append({"kind": "action", "id": a.get("id"), "title": a.get("title"),
                            "score": 0.8, "snippet": a.get("next_action", ""),
                            "meta": "待办"})
    results.sort(key=lambda x: x["score"], reverse=True)
    return ok({"items": results[:20], "q": query})


@router.get("/similar")
def get_similar(q: str = ""):
    if not q.strip():
        return ok({"items": []})
    return ok({"items": act.similar(q.strip())})


@router.post("/plan")
def post_plan(payload: PlanIn):
    return ok(act.plan(payload.situation))


@router.get("/actions")
def get_actions():
    data = st.actions_data()
    return ok({"items": data.get("items", []), "situation": data.get("situation", "")})


@router.post("/actions/{aid}/complete")
def post_complete(aid: str):
    return ok(act.complete(aid))


@router.post("/actions/{aid}/reflect")
def post_reflect(aid: str, payload: ReflectIn):
    return ok(act.reflect(aid, payload.text))
