"""右侧对话面板：把当前文档当上下文，回答可以带一段可插入的正文。

- 不是"另一个 AI 助手"：它不产卡片、不直接改文档。
- 模型想往文档里写东西时，只能给出 patch，由用户点按钮才落地。
- 没配模型时返回 LLM_UNAVAILABLE，前端如实显示，不假装回答。
- 对话记录落到 records/_chat/，可导出；流式回复走 /chat/stream（SSE）。
"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.llm import client, settings
from src.storage import chatlog, index, repo

router = APIRouter(tags=["chat"])

MAX_MESSAGES = 20
DOC_CHARS = 6000
PATCH_START = "<<<PATCH>>>"
PATCH_END = "<<<END>>>"
PATCH_LIMIT = 1200
REHOME_START = "<<<REHOME>>>"
REHOME_NEW = "新建"


class Message(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    doc_id: str = ""
    attach: list[str] = []
    allow_patch: bool = True
    web: bool = False
    messages: list[Message] = []


def _context_block(doc: dict, limit: int = DOC_CHARS) -> str:
    """把一篇文档压成一段上下文：标题、主题、小节目录、正文（超长截断）。"""
    title = str(doc.get("title") or "未命名")
    body = str(doc.get("body") or "").strip()
    parts = [f"《{title}》"]
    topic = doc.get("topic") or []
    if isinstance(topic, list) and topic:
        parts.append("主题：" + "、".join(str(t) for t in topic))
    heads = [line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")]
    if heads:
        parts.append("小节：" + " / ".join(heads[:20]))
    if len(body) > limit:
        body = body[:limit] + "\n……（正文过长，已截断）"
    parts.append("正文：\n\n" + body)
    return "\n\n".join(parts)


def _split_rehome(text: str) -> tuple[str, str]:
    """拆出归位建议标记 <<<REHOME>>>《标题》| 新建 <<<END>>>；没有就原样返回。

    返回 (去掉标记后的文本, rehome)：rehome 是《标题》里的标题名、"new"（建议新建）
    或空串（不建议）。标题只做消毒，不在这里校验存在性——库里有没有那篇由前端判断。
    """
    if REHOME_START not in text:
        return text, ""
    head, _, rest = text.partition(REHOME_START)
    inner, _, tail = rest.partition(PATCH_END)
    inner = inner.strip().strip("《》").strip()
    # 万一模型把标记嵌进补丁里，去掉标记后仍要能正常走补丁解析
    remainder = (head + tail).strip()
    if not inner or len(inner) > 60:
        return remainder, ""
    if inner == REHOME_NEW:
        return remainder, "new"
    return remainder, inner


def _split_patch(text: str) -> tuple[str, str]:
    """把回答拆成「话」和「可插入的正文」；没有标记就当作纯回答。"""
    if PATCH_START not in text:
        return text.strip(), ""
    head, _, rest = text.partition(PATCH_START)
    patch, _, _tail = rest.partition(PATCH_END)
    patch = patch.strip()
    patch = re.sub(r"^```(?:markdown|md)?\s*", "", patch)
    patch = re.sub(r"\s*```$", "", patch).strip()
    if patch.startswith("---"):
        blocks = patch.split("---")
        if len(blocks) >= 3:
            patch = "---".join(blocks[2:]).strip()
    if len(patch) > PATCH_LIMIT:
        patch = patch[:PATCH_LIMIT].rstrip() + "\n\n……（补丁过长，已截断）"
    return head.strip(), patch


def _self_history_block(query: str, exclude_ids: set) -> str:
    """全集 §20 对话内实时对照：检索用户自己的历史原文注入上下文。

    只返回 stream=self（含目标线），外部素材无权在对话内出现。
    三态降级：无命中返回空串（不注入、不提），索引异常同样静默，不打断对话。
    """
    try:
        hits = index.related_passages(query, exclude_ids=exclude_ids, limit=5)
    except Exception:
        return ""
    if not hits:
        return ""
    blocks = []
    for h in hits:
        title = str(h.get("title") or "未命名")
        date_part = str(h.get("date") or "日期未知")
        excerpt = str(h.get("excerpt") or "").strip()
        if excerpt:
            blocks.append(f"- 《{title}》（{date_part}）：{excerpt}")
    if not blocks:
        return ""
    return ("\n\n# 用户过去写下的相关记录\n\n"
            "下面是用户自己在其他文档里写下的原话，两个用途：一是偏差对照"
            "（对照用户此刻的说法与这些记录是否矛盾），二是归位建议"
            "（判断这段话该不该沉淀进其中某篇）；不要当作本次提问的资料去复述：\n\n"
            + "\n".join(blocks))


def _web_block(query: str) -> tuple[str, list[dict], str]:
    """联网搜索（用户在输入框开了「联网」才走这里）。三态诚实降级：
    正常 → 注入结果块，返回来源列表；失败 → 返回诚实 note，不注入任何假结果。"""
    from src.llm import websearch
    mock = os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock"
    if mock:
        # 验收链路：mock 给占位结果，保证 web=true 的整条管道可测
        fake = [{"title": f"（演示搜索结果 {i + 1}）{query[:18]}",
                 "url": f"https://example.com/mock/{i + 1}",
                 "snippet": f"这是演示模式的第 {i + 1} 条占位摘要。"} for i in range(3)]
        return ("\n\n# 联网搜索结果\n\n" + _format_web_hits(fake), fake, "")
    try:
        results, note = websearch.search_web(query)
    except websearch.SearchUnavailable as exc:
        return "", [], f"联网搜索没成上：{exc}。以下回答只基于本地内容，没有用到网络信息。"
    except Exception as exc:  # 网络层任何异常都不打断对话
        return "", [], f"联网搜索失败：{exc}。以下回答只基于本地内容。"
    if not results:
        return "", [], "联网搜索没有返回结果。以下回答只基于本地内容。"
    return ("\n\n# 联网搜索结果\n\n" + _format_web_hits(results), results, "")


def _format_web_hits(results: list[dict]) -> str:
    lines = ["下面是刚从网络搜到的公开资料摘要，供本次回答参考。"
             "引用时注明来自哪一条（如「来源1」），不要把网络内容说成用户自己的想法；"
             "与用户文档冲突时，指明冲突并让用户自己判断："]
    for i, item in enumerate(results, start=1):
        lines.append(f"- 来源{i}《{item.get('title') or '无标题'}》{item.get('url') or ''}\n"
                     f"  摘要：{item.get('snippet') or '（无摘要）'}")
    return "\n".join(lines)


def _prepare(payload: ChatIn) -> tuple[str, list[dict], dict | None, list[dict], str]:
    """流式与非流式共用同一套上下文拼装，避免两条链路口径分叉。"""
    incoming = [m for m in payload.messages
                if m.role in ("user", "assistant") and str(m.content).strip()]
    if not incoming or incoming[-1].role != "user":
        raise ApiError("INVALID_PARAM", "最后一条必须是你自己的提问")
    system = client.load_chat_prompt()
    docs: list[dict] = []
    if payload.doc_id:
        current = repo.read_doc(payload.doc_id)
        if current:
            docs.append(current)
    for extra_id in payload.attach[:5]:
        if any(item.get("id") == extra_id for item in docs):
            continue
        extra = repo.read_doc(extra_id)
        if extra:
            docs.append(extra)
    if docs:
        system += "\n\n# 用户当前打开的文档\n\n" + _context_block(docs[0])
        if len(docs) > 1:
            blocks = "\n\n".join(_context_block(item, limit=2500) for item in docs[1:])
            system += "\n\n# 用户另外附加的参考文档\n\n" + blocks
    # §20 对话内实时对照：拿用户最新提问检索他自己写下的历史原文（跳过已在上下文里的文档）
    last_query = str(incoming[-1].content or "").strip()
    if last_query:
        system += _self_history_block(last_query, {str(d.get("id") or "") for d in docs})
    # 联网搜索：用户开了「联网」开关才发起；失败诚实降级不打断
    sources: list[dict] = []
    web_note = ""
    if payload.web and last_query:
        block, sources, web_note = _web_block(last_query)
        system += block
    if not payload.allow_patch:
        system += "\n\n本次对话为只读模式：不要输出 <<<PATCH>>> 段，只说话。"
    # 发给模型的消息只留 role/content，不带任何前端私有字段
    trimmed = [{"role": m.role, "content": m.content} for m in incoming][-MAX_MESSAGES:]
    return system, trimmed, (docs[0] if docs else None), sources, web_note


def _store(payload: ChatIn, trimmed: list[dict], reply: str, patch: str,
           rehome: str = "", sources: list[dict] | None = None,
           web_note: str = "") -> dict:
    """服务端为对话落盘的权威方：整段上下文 + 本轮回答一次写全。"""
    full = list(trimmed)
    titles = []
    for extra_id in payload.attach[:5]:
        extra = repo.read_doc(extra_id)
        if extra:
            titles.append(str(extra.get("title") or ""))
    if titles and full:
        full[-1] = dict(full[-1], attach=titles)
    full = full + [{"role": "assistant", "content": reply}]
    if patch:
        full[-1]["patch"] = patch
    if rehome:
        full[-1]["rehome"] = rehome
    if sources:
        full[-1]["sources"] = sources
    if web_note:
        full[-1]["web_note"] = web_note
    return chatlog.save(payload.doc_id, full)


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _config_payload() -> dict:
    mock = os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock"
    public = settings.public()
    env = settings.env_model()
    active = settings.credentials()
    label = "演示模式" if mock else (active.get("label") if active else "未配置模型")
    from src.llm import websearch
    search = websearch.availability()
    from src.llm import stt
    return {
        "model": label,
        "mock": mock,
        "configured": bool(active) or mock,
        "active": public["active"],
        "models": public["models"],
        "env_override": bool(env),
        "config_path": str(settings.CONFIG_PATH),
        "search": {"available": bool(search["available"]) or mock,
                   "provider": search["provider"]},
        "stt": stt.availability(),
    }


@router.get("/config")
def get_config():
    return ok(_config_payload())


class ModelIn(BaseModel):
    id: str = ""
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@router.post("/config/models")
def post_model(payload: ModelIn):
    try:
        settings.add_model(payload.model_dump())
    except ValueError as exc:
        raise ApiError("INVALID_PARAM", str(exc)) from exc
    return ok(_config_payload())


@router.post("/config/models/remove")
def post_remove_model(payload: dict):
    settings.remove_model(str(payload.get("id") or ""))
    return ok(_config_payload())


@router.post("/config/active")
def post_active(payload: dict):
    settings.set_active(str(payload.get("id") or ""))
    return ok(_config_payload())


@router.post("/config/test")
def post_test(payload: dict):
    model_id = str(payload.get("id") or "")
    if model_id:
        cfg = settings.load()
        entry = next((item for item in cfg.get("models", []) if item.get("id") == model_id), None)
        if not entry:
            raise ApiError("INVALID_PARAM", "没有这个模型")
        creds = entry
    else:
        creds = settings.credentials()
    try:
        message = client.probe(creds)
    except client.LLMUnavailable as exc:
        raise ApiError("LLM_UNAVAILABLE", f"连接失败：{exc}") from exc
    return ok({"message": message})


@router.post("/chat")
def post_chat(payload: ChatIn):
    """保留阻塞式接口（老前端 / 外部脚本仍可用），同样落盘。"""
    system, trimmed, doc, sources, web_note = _prepare(payload)
    try:
        raw = client.chat_text(system, trimmed)
    except client.LLMUnavailable as exc:
        raise ApiError("LLM_UNAVAILABLE", f"模型不可用：{exc}") from exc
    raw, rehome = _split_rehome(raw)
    reply, patch = _split_patch(raw)
    if not payload.allow_patch:
        patch = ""
    _store(payload, trimmed, reply, patch, rehome, sources, web_note)
    return ok({"reply": reply, "patch": patch, "rehome": rehome,
               "sources": sources, "web_note": web_note,
               "doc_id": doc.get("id") if doc else "", "doc_title": doc.get("title") if doc else ""})


@router.post("/chat/stream")
def post_chat_stream(payload: ChatIn):
    """SSE 流式：delta 边生成边发，done 带最终 reply/patch，error 让前端出重试按钮。"""
    system, trimmed, _doc, sources, web_note = _prepare(payload)

    def events():
        buffer = ""
        try:
            for chunk in client.stream_chat(system, trimmed):
                buffer += chunk
                yield _sse({"type": "delta", "text": chunk})
        except client.LLMUnavailable as exc:
            yield _sse({"type": "error", "code": "LLM_UNAVAILABLE", "message": str(exc)})
            return
        except Exception as exc:
            yield _sse({"type": "error", "code": "LLM_UNAVAILABLE",
                        "message": f"模型中断：{exc}"})
            return
        if not buffer.strip():
            yield _sse({"type": "error", "code": "LLM_UNAVAILABLE", "message": "模型返回为空"})
            return
        remainder, rehome = _split_rehome(buffer)
        reply, patch = _split_patch(remainder)
        if not payload.allow_patch:
            patch = ""
        saved = _store(payload, trimmed, reply, patch, rehome, sources, web_note)
        yield _sse({"type": "done", "reply": reply, "patch": patch, "rehome": rehome,
                    "sources": sources, "web_note": web_note,
                    "updated": saved["updated"], "count": saved["count"]})

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class SummaryIn(BaseModel):
    doc_id: str = ""
    messages: list[Message] = []


@router.post("/chat/summarize")
def post_chat_summarize(payload: SummaryIn):
    """把这段对话的关键内容总结成一篇可归档的 Markdown 草稿。

    只产出草稿，不写任何盘——归到哪、怎么归，全部由用户点按钮决定。
    """
    incoming = [m for m in payload.messages
                if m.role in ("user", "assistant") and str(m.content).strip()]
    if len(incoming) < 2:
        raise ApiError("INVALID_PARAM", "对话还没几句，不值得总结。")
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
        heads = [str(m.content).strip().split("\n")[0][:24]
                 for m in incoming if m.role == "user"][:4]
        draft = ("## 对话要点（演示模式占位）\n\n"
                 + "\n".join(f"- {h}" for h in heads)
                 + "\n\n- [ ] 把上面这些要点整理成你自己的结论。")
        return ok({"draft": draft, "count": len(incoming)})
    system = _load_summary_prompt()
    convo = "\n".join(f"{'用户' if m.role == 'user' else '回答'}：{str(m.content).strip()}"
                      for m in incoming[-30:])
    try:
        draft = client.chat_text(system, [{"role": "user", "content": convo}])
    except client.LLMUnavailable as exc:
        raise ApiError("LLM_UNAVAILABLE", f"模型不可用：{exc}") from exc
    draft = draft.strip()
    if PATCH_START in draft:  # 模型擅自带补丁标记就清掉
        draft, patch = _split_patch(draft)
        draft = (draft + ("\n\n" + patch if patch else "")).strip()
    if not draft:
        raise ApiError("LLM_UNAVAILABLE", "模型没有给出总结")
    return ok({"draft": draft, "count": len(incoming)})


def _load_summary_prompt() -> str:
    """总结提示词同样只从文件读，代码里不内联第二份。"""
    from src.storage.paths import PROMPTS
    path = PROMPTS / "summarize_system.md"
    if not path.exists():
        raise ApiError("LLM_UNAVAILABLE", "缺少 prompts/summarize_system.md")
    return path.read_text(encoding="utf-8")


@router.get("/related")
def get_related(q: str = ""):
    """归位建议的「理由」来源：与这句话最相关的用户历史记录摘句。只读，失败静默。"""
    try:
        return ok({"passages": index.related_passages(q, limit=3)})
    except Exception:
        return ok({"passages": []})


@router.get("/chat/history")
def get_chat_history(doc_id: str = ""):
    return ok(chatlog.load(doc_id))


class HistoryIn(BaseModel):
    doc_id: str = ""
    messages: list[dict] = []


@router.put("/chat/history")
def put_chat_history(payload: HistoryIn):
    return ok(chatlog.save(payload.doc_id, payload.messages))


@router.get("/chat/export")
def get_chat_export(doc_id: str = "", format: str = "md"):
    title = ""
    if doc_id:
        doc = repo.read_doc(doc_id)
        if doc:
            title = str(doc.get("title") or "")
    safe = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", title or doc_id or "对话").strip("_")[:40]
    stamp = os.path.basename(chatlog.path_for(doc_id)).replace(".json", "")
    if format == "json":
        return ok({"filename": f"对话-{safe}.json", "content": chatlog.export_json(doc_id)})
    return ok({"filename": f"对话-{safe}-{stamp}.md",
               "content": chatlog.export_markdown(doc_id, title)})
