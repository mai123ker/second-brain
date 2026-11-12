"""处境梳理 → 待办 → 完成激励 → 执行回补 → 相似问题召回。"""
from __future__ import annotations

import json
import os
import re

from src.api.errors import ApiError
from src.core import v2distill as dz
from src.core import v2store as st, v2text as tx
from src.llm import client
from src.llm import settings
from src.storage.paths import PROMPTS

ENCOURAGE = [
    "这一步落地了。把想法变成做过的事，本身就是最难的一段。",
    "记下了。你又多了一条经过验证的经验，而不只是想过。",
    "完成得干脆。下一步的处境，会比这次更清楚一点。",
    "这件事翻篇了。剩下的力气留给真正还没想清楚的部分。",
    "又往前推了一格。持续推进比一次想明白更值钱。",
]
QUOTES = [
    {"text": "不积跬步，无以至千里。", "from": "《荀子·劝学》"},
    {"text": "千里之行，始于足下。", "from": "《道德经》"},
    {"text": "知是行之始，行是知之成。", "from": "王阳明《传习录》"},
    {"text": "纸上得来终觉浅，绝知此事要躬行。", "from": "陆游《冬夜读书示子聿》"},
    {"text": "工欲善其事，必先利其器。", "from": "《论语·卫灵公》"},
]
PITFALL_WORDS = ["又", "重复", "失误", "踩坑", "不该", "下次", "同样", "再次", "还是", "老问题"]


def _prompt(name: str) -> str:
    path = PROMPTS / name
    if not path.exists():
        raise ApiError("STORAGE_ERROR", f"缺少 prompts/{name}")
    return path.read_text(encoding="utf-8")


def similar(text: str) -> list[dict]:
    """召回历史相似处境，并把当时写下的重复失误带回来。"""
    situations = st.situations_data().get("items", [])
    actions = {a.get("id"): a for a in st.actions_data().get("items", [])}
    hits = []
    for record in situations:
        score = tx.similarity(text, str(record.get("text") or ""))
        if score < 0.28:
            continue
        pitfalls = []
        for aid in record.get("todo_ids", []):
            reflection = str((actions.get(aid) or {}).get("reflection") or "")
            if reflection and any(w in reflection for w in PITFALL_WORDS):
                pitfalls.append(reflection[:120])
        for extra in record.get("reflections", []):
            line = str(extra.get("text") or "")
            if line and any(w in line for w in PITFALL_WORDS) and line[:120] not in pitfalls:
                pitfalls.append(line[:120])
        frames = [str(f) for f in record.get("frame_ids", [])]
        hits.append({"id": record.get("id"), "text": str(record.get("text") or "")[:160],
                     "created": record.get("created"), "score": round(score, 2),
                     "frames": frames, "pitfalls": pitfalls[:3],
                     "problems": [str(p) for p in record.get("problems", [])][:3]})
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits[:3]


def _frame_brief(items: list[dict]) -> list[dict]:
    return [{"id": k.get("id"), "title": k.get("title"),
             "statement": dz.statement_of(k.get("body", ""))[:180]}
            for k in items[:12]]


def _llm_plan(text: str, frames: list[dict], history: list[dict]) -> dict | None:
    mode = os.environ.get("MIRROR_LLM_MODE", "").strip().lower()
    if mode in ("off", "none", "mock") or not client.configured():
        return None
    payload = {"当前处境": text, "可用框架": _frame_brief(frames), "历史相似处境": history}
    try:
        raw = client.chat_text(_prompt("plan_system.md"),
                               [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        data = dz._json_from(raw)
        problems = [p for p in data.get("problems", []) if isinstance(p, dict) and p.get("text")]
        todos = [t for t in data.get("todos", []) if isinstance(t, dict) and t.get("title")]
        if not todos:
            return None
        valid = {f["id"] for f in frames}
        for t in todos:
            t["frame_ids"] = [i for i in (t.get("frame_ids") or []) if i in valid]
        return {"problems": problems[:4], "todos": todos[:5]}
    except Exception:
        return None


def _rule_plan(text: str, frames: list[dict], history: list[dict]) -> dict:
    known = {f["id"]: f for f in _frame_brief(frames)}
    sentences = tx.normalize(text)["sentences"] or [text.strip()]
    guard = ""
    if history and history[0].get("pitfalls"):
        guard = "上次在类似处境里你写过「" + history[0]["pitfalls"][0][:60] + "」，这次先避开。"

    problems = []
    for sentence in sentences[:2]:
        related = [f for f in _frame_brief(frames)
                   if tx.similarity(sentence, f"{f['title']} {f['statement']}") >= 0.18][:1]
        if related:
            note = "已有框架《" + related[0]["title"] + "》可作参照：" + related[0]["statement"][:70]
        elif frames:
            note = "库里有 " + str(len(frames)) + " 条框架，但没有直接命中这句话；先翻一下相近领域。"
        else:
            note = "库里还没有能直接调用的框架，先补一份资料。"
        problems.append({"text": f"「{sentence[:60]}」还没落成可验证的判断。",
                         "frame_ids": [related[0]["id"]] if related else [], "note": note})
    if frames:
        problems.append({"text": f"已有 {len(frames)} 条成型框架，需要先核对是否仍适用于当前处境。",
                         "frame_ids": [frames[0]["id"]], "note": "框架优先，先别急着按直觉改做法。"})

    lead = sentences[0][:24]
    todos = [
        {"title": f"把「{lead}」写成一句可验证的判断",
         "why": "先分清事实与推测，避免把情绪当成处境",
         "next_action": "在中间对话框补一段现状，写清依据和能观察到的结果",
         "frame_ids": [], "guard": guard},
    ]
    if frames:
        top = _frame_brief(frames)[0]
        todos.append({"title": f"用《{top['title']}》核对一次当前做法",
                      "why": top["statement"][:90] or "这条框架是你自己沉淀的判断",
                      "next_action": "打开左栏该框架，列出与现状不一致的地方",
                      "frame_ids": [top["id"]], "guard": ""})
    else:
        todos.append({"title": "投喂一份与当前处境相关的资料",
                      "why": "库里还没有成型框架，行动暂时没有可复用的依据",
                      "next_action": "拖入 PDF / Word，或贴一段文字、一段视频字幕",
                      "frame_ids": [], "guard": ""})
    todos.append({"title": "定一个本周能完成的下一步，并写下时间",
                  "why": "处境梳理只有落到具体动作，才和知识库接成闭环",
                  "next_action": "在右侧确认待办；完成后写一条执行中的新想法回补进库",
                  "frame_ids": [], "guard": ""})
    return {"problems": problems, "todos": todos}


def plan(situation: str) -> dict:
    text = str(situation or "").strip()
    if len(text) < 4:
        raise ApiError("INVALID_PARAM", "先用一句话说说现在的处境。")
    frames = [k for k in st.all_knowledge() if k.get("type") == "frame"]
    history = similar(text)
    llm = _llm_plan(text, frames, history)
    generated = llm or _rule_plan(text, frames, history)
    return _persist(text, generated, history, frames, "llm" if llm else "rule")


def _persist(text: str, generated: dict, history: list[dict], frames: list[dict], mode: str) -> dict:
    data = st.actions_data()
    data["situation"] = text
    created = []
    for raw in generated["todos"][:5]:
        item = {
            "id": st.uid("a"), "title": str(raw.get("title") or "")[:80],
            "why": str(raw.get("why") or "")[:200],
            "next_action": str(raw.get("next_action") or "")[:200],
            "frame_ids": list(raw.get("frame_ids") or []),
            "guard": str(raw.get("guard") or "")[:200],
            "status": "open", "created": st.now(), "completed_at": "",
            "reflection": "", "encouragement": "",
        }
        data.setdefault("items", []).append(item)
        created.append(item)
    st.save_actions(data)

    situations = st.situations_data()
    situations.setdefault("items", []).append({
        "id": st.uid("p"), "text": text, "created": st.now(),
        "problems": [str(p["text"]) for p in generated["problems"]],
        "todo_ids": [a["id"] for a in created],
        "frame_ids": sorted({f for p in generated["problems"] for f in p.get("frame_ids", [])}),
        "similar": [h["id"] for h in history], "reflections": [],
    })
    st.save_situations(situations)
    return {"problems": generated["problems"], "todos": created,
            "similar": history, "mode": mode, "frames_used": _frame_brief(frames)[:5]}


def complete(aid: str) -> dict:
    data = st.actions_data()
    done_count = sum(1 for x in data.get("items", []) if x.get("status") == "done")
    for item in data.get("items", []):
        if item.get("id") != aid:
            continue
        if item.get("status") == "done":
            return {"action": item, "already": True,
                    "encouragement": {"line": item.get("encouragement") or ENCOURAGE[0],
                                      "quote": QUOTES[done_count % len(QUOTES)]}}
        item["status"] = "done"
        item["completed_at"] = st.now()
        line = ENCOURAGE[done_count % len(ENCOURAGE)]
        item["encouragement"] = line
        st.save_actions(data)
        return {"action": item, "already": False,
                "encouragement": {"line": line, "quote": QUOTES[done_count % len(QUOTES)]},
                "prompt": "执行过程中有新想法吗？写下来会回补进知识库。"}
    raise ApiError("INVALID_PARAM", "找不到这条待办。")


def reflect(aid: str, text: str) -> dict:
    thought = str(text or "").strip()
    if len(thought) < 4:
        raise ApiError("INVALID_PARAM", "写一句执行中的新想法再回补。")
    data = st.actions_data()
    item = next((a for a in data.get("items", []) if a.get("id") == aid), None)
    if not item:
        raise ApiError("INVALID_PARAM", "找不到这条待办。")
    source = st.write_source("reflection", f"执行回补 · {item['title'][:24]}",
                             thought, origin=f"action:{aid}")
    result = dz.distill(thought, source)
    item["reflection"] = thought
    st.save_actions(data)

    situations = st.situations_data()
    for record in situations.get("items", []):
        if aid in record.get("todo_ids", []):
            record.setdefault("reflections", []).append({
                "action": aid, "text": thought, "created": st.now(),
                "changes": result["changes"],
            })
    st.save_situations(situations)
    return {"source": source, "distill": result}


def state() -> dict:
    knowledge = st.all_knowledge()
    actions = st.actions_data().get("items", [])
    creds = settings.credentials() or {}
    mode = os.environ.get("MIRROR_LLM_MODE", "").strip().lower()
    mock = mode == "mock"
    usable = bool(creds) and mode not in ("off", "none", "mock")
    return {
        "counts": {
            "knowledge": len(knowledge),
            "frames": sum(1 for k in knowledge if k.get("type") == "frame"),
            "unabsorbed": sum(1 for k in knowledge if k.get("status") == "unabsorbed"),
            "sources": len(st.all_sources()),
            "todos_open": sum(1 for a in actions if a.get("status") == "open"),
            "todos_done": sum(1 for a in actions if a.get("status") == "done"),
            "chat": len(st.chat_data().get("messages", [])),
            "situations": len(st.situations_data().get("items", [])),
        },
        "model": {"configured": usable, "name": creds.get("model", "") if usable else "",
                  "mode": mode or "byok", "mock": mock},
        "situation": st.actions_data().get("situation", ""),
    }
