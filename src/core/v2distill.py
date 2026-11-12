"""知识提炼与增量更新：LLM 优先，确定性规则兜底。

增量规则（这是「结构化框架优先于尚未吸收的认知」的落点）：
- 命中已有条目 → 合并：只追加来源与补充结构，绝不改写已有陈述。
- 相似但不一致 → 并列新建，标 conflicts，状态 unabsorbed，等用户对照。
- 全新 → 新建。
- 任何情况下知识库里都不出现对话原文，原文只在 vault/sources/。
"""
from __future__ import annotations

import json
import os
import re

from src.api.errors import ApiError
from src.core import v2store as st, v2text as tx
from src.llm import client
from src.storage.paths import PROMPTS, ROOT

MERGE_AT = 0.62
CONFLICT_AT = 0.50


def _prompt(name: str) -> str:
    path = PROMPTS / name
    if not path.exists():
        raise ApiError("STORAGE_ERROR", f"缺少 prompts/{name}")
    return path.read_text(encoding="utf-8")


def _json_from(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?", "", (raw or "").strip(), flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("not a dict")
    return obj


def statement_of(body: str) -> str:
    """从条目正文里取「## 陈述」下的第一段。"""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## 陈述":
            for follow in lines[i + 1:]:
                if follow.strip().startswith("## "):
                    break
                if follow.strip():
                    return follow.strip()
            break
    return ""


def _llm_items(sentences: list[str], source: dict, existing: list[dict]) -> dict | None:
    mode = os.environ.get("MIRROR_LLM_MODE", "").strip().lower()
    if mode in ("off", "none", "mock") or not client.configured():
        return None
    brief = [{"id": k.get("id"), "type": k.get("type"), "title": k.get("title"),
              "statement": statement_of(k.get("body", ""))[:120]}
             for k in existing[:40]]
    payload = {
        "来源": {"id": source.get("id"), "kind": source.get("kind"),
                 "title": source.get("title")},
        "待整理文本": "\n".join(sentences),
        "已有条目": brief,
    }
    try:
        raw = client.chat_text(_prompt("distill_system.md"),
                               [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        data = _json_from(raw)
        items = data.get("items")
        if not isinstance(items, list):
            return None
        return {"items": items[:6], "summary": str(data.get("summary") or "")[:200]}
    except Exception:
        return None


def _merge(target: dict, source: dict, structure: list[str], score: float) -> dict:
    sources = list(target.get("sources") or [])
    if source.get("id") and source["id"] not in sources:
        sources.append(source["id"])
    body = (target.get("body") or "").rstrip()
    line = tx.source_line(source)
    if line not in body:
        body += "\n" + line
    if structure:
        body += ("\n\n## 补充结构（" + st.now() + "）\n\n"
                 + "\n".join(f"- {s}" for s in structure))
    meta = dict(target, sources=sources, updated=st.now())
    st.write_knowledge(meta, body)
    return {"action": "merged", "id": target.get("id"), "title": target.get("title"),
            "type": target.get("type"), "domain": target.get("domain"), "score": score}


def _apply(raw: dict, source: dict, mode: str, existing: list[dict] | None = None) -> dict:
    statement = re.sub(r"\s+", " ", str(raw.get("statement") or "")).strip()[:300]
    if not statement:
        return {"action": "skipped", "reason": "空陈述"}
    title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:40] or statement[:18]
    typ = raw.get("type") if raw.get("type") in ("frame", "point") else "point"
    domain = raw.get("domain") if raw.get("domain") in st.DOMAINS else tx.guess_domain(statement)
    structure = [str(x).strip()[:160] for x in (raw.get("structure") or []) if str(x).strip()][:5]
    absorbed = bool(raw.get("absorbed", False))
    blob = f"{title} {statement}"

    explicit = str(raw.get("merge_with") or "").strip()
    pool = existing if existing is not None else st.all_knowledge()
    best, best_score = None, 0.0
    for k in pool:
        candidate = f"{k.get('title', '')} {statement_of(k.get('body', ''))}"
        score = tx.similarity(blob, candidate)
        if tx.containment(blob, candidate) >= 0.82:
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = k, score
    if explicit:
        target = st.get_knowledge(explicit)
        if target:
            return _merge(target, source, structure, 1.0)
    elif best_score >= MERGE_AT and best:
        return _merge(best, source, structure, round(best_score, 2))

    conflicts = [best["id"]] if best and best_score >= CONFLICT_AT and not explicit else []
    kid = st.uid("k")
    item = {
        "id": kid, "type": typ, "domain": domain, "title": title,
        "status": "absorbed" if absorbed else "unabsorbed",
        "confidence": 4 if mode == "llm" else 3,
        "created": st.now(), "updated": st.now(),
        "sources": [source.get("id", "")], "tags": [domain], "conflicts": conflicts,
    }
    body_item = dict(item, statement=statement, structure=structure)
    body_text = tx.build_body(body_item, source, conflicts)
    st.write_knowledge(item, body_text)
    # 同一批里后面的条目也要跟刚建的对上，否则一次投喂会自己跟自己重复
    if existing is not None:
        existing.append(dict(item, body=body_text))
    return {"action": "conflict" if conflicts else "created", "id": kid, "title": title,
            "type": typ, "domain": domain, "score": round(best_score, 2),
            "conflict_with": conflicts}


def distill(text: str, source: dict, force_rule: bool = False, limit: int = 6) -> dict:
    norm = tx.normalize(text or "")
    if not norm["sentences"]:
        return {"summary": "没有可沉淀的内容", "changes": [], "normalized": norm,
                "mode": "rule", "source": source}
    existing = st.all_knowledge()
    llm = None if force_rule else _llm_items(norm["sentences"], source, existing)
    mode = "llm" if llm else "rule"
    raw_items = llm["items"] if llm else tx.rule_items(norm["sentences"], source.get("kind", "chat"))
    summary = (llm or {}).get("summary") or tx.rule_summary(norm["sentences"])
    changes = [_apply(raw, source, mode, existing) for raw in raw_items[:limit]
               if isinstance(raw, dict)]
    return {"summary": summary, "changes": changes, "normalized": norm,
            "mode": mode, "source": source}


def _lead(text: str, limit: int = 42) -> str:
    """取第一个自然句；超长在标点处收住，不留半句。"""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return ""
    head = re.split(r"(?<=[。！？；])|[|｜]", clean)[0].strip(" 。；")
    if len(head) > limit:
        cut = max(head.rfind("，", 0, limit), head.rfind("、", 0, limit), head.rfind("；", 0, limit))
        head = head[:cut] if cut >= 12 else head[:limit]
    return head.rstrip("，、；。") + ("…" if len(head) >= limit else "")


def _short(title: str, limit: int = 16) -> str:
    text = re.sub(r"\s+", " ", str(title or "").strip())
    return text if len(text) <= limit else text[:limit - 1].rstrip("的和与就才而") + "…"


def manual() -> dict:
    """个人使用说明：按领域把框架、知识点、待对照内容组织成可翻阅的说明。"""
    items = st.all_knowledge()
    sections = []
    for domain in st.DOMAINS:
        group = [k for k in items if k.get("domain") == domain]
        frames = [k for k in group if k.get("type") == "frame"]
        points = [k for k in group if k.get("type") == "point"]
        pending = [k for k in group if k.get("status") == "unabsorbed"]
        parts = []
        if frames:
            names = "、".join(f"《{_short(k.get('title'))}》" for k in frames[:3])
            parts.append(f"关于{domain}，我有 {len(frames)} 条成型框架：{names}。")
            lead = _lead(statement_of(frames[0].get("body", "")))
            # 标题本身就是陈述前半句时不再重复引用，避免「《X》…按「X…」判断」
            if lead and lead[:8] not in _short(frames[0].get("title"), 16):
                parts.append(f"遇到这类问题，我通常先按「{lead}」判断。")
        elif group:
            parts.append(f"关于{domain}，还没有成型框架，先拿零散知识点参照。")
        if points:
            parts.append(f"另有 {len(points)} 条知识点补细节。")
        if pending:
            if frames:
                parts.append(f"其中 {len(pending)} 条还没标记为吸收，暂时只作参照。")
            else:
                names = "、".join(f"《{_short(k.get('title'))}》" for k in pending[:3])
                parts.append(f"还有 {len(pending)} 条没吸收，等我和已有框架对照：{names}。")
        sections.append({
            "domain": domain, "narrative": "".join(parts), "count": len(group),
            "frames": [_brief(k) for k in frames], "points": [_brief(k) for k in points],
            "unabsorbed": [_brief(k) for k in pending],
        })
    return {"sections": sections, "total": len(items), "generated": st.now()}


def _brief(k: dict) -> dict:
    return {"id": k.get("id"), "title": k.get("title"), "type": k.get("type"),
            "domain": k.get("domain", ""), "status": k.get("status"),
            "sources": k.get("sources") or [],
            "statement": statement_of(k.get("body", ""))[:90],
            "conflicts": k.get("conflicts") or []}


FRONT = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
META_LINE = re.compile(
    r"^(id|title|stream|kind|target|topic|version|confidence|status|created|updated|"
    r"source|stance|scope|claim_relation)\s*[:：]", re.I)
HEADING = re.compile(r"^#{1,3}\s")
TODO_LINE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*")


def strip_frontmatter(text: str) -> str:
    """剥掉 YAML 头：旧文档每篇都带 14 个字段，留着会被当成正文句子提炼。"""
    return FRONT.sub("", text or "", count=1)


PLACEHOLDER = re.compile(r"^[（(][^）)]{0,60}[）)]$")


def clean_block(text: str) -> str:
    """去掉标题行、元数据行、待办勾选与括号占位句，只留可提炼的陈述。"""
    out = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            out.append("")
            continue
        if HEADING.match(raw) or META_LINE.match(raw) or TODO_LINE.match(raw):
            continue
        if raw.startswith(("- ", "* ")) and META_LINE.match(raw[2:].strip()):
            continue
        if PLACEHOLDER.match(raw):
            continue
        out.append(line)
    return "\n".join(out)


def sections(text: str, limit: int = 1400, max_parts: int = 4) -> list[str]:
    """按一二级标题切块；一块超过 limit 字就硬切，保证长文后半段也被提炼。"""
    blocks, buf, size = [], [], 0
    for line in (text or "").splitlines():
        if re.match(r"^#{1,2}\s", line) and size >= 240:
            blocks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
        if size >= limit:
            blocks.append("\n".join(buf))
            buf, size = [], 0
    if buf:
        blocks.append("\n".join(buf))
    return [b.strip() for b in blocks if len(b.strip()) >= 40][:max_parts]


def import_records(limit: int = 12, max_sections: int = 4, per_section: int = 3) -> dict:
    """把 records/ 里已有的长文档导入知识库（用户主动触发，规则版，不批量调模型）。

    - 先剥 frontmatter 再切块，元数据不会变成知识条目；
    - 同一 origin 已导入过就整篇跳过，重复点按钮不会翻倍；
    - 每篇最多 max_sections 块、每块最多 per_section 条，控制条数质量。
    """
    from src.storage.paths import scan_documents

    summary = {"docs": 0, "created": 0, "merged": 0, "conflict": 0,
               "skipped": 0, "skipped_docs": 0, "sections": 0}
    seen = {str(s.get("origin") or "") for s in st.all_sources() if s.get("kind") == "import"}
    for path in scan_documents()[:limit]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            origin = str(path.relative_to(ROOT))
        except ValueError:
            origin = path.name
        if origin in seen:
            summary["skipped_docs"] += 1
            continue
        chunks = [clean_block(c) for c in sections(strip_frontmatter(text), max_parts=max_sections)]
        chunks = [c for c in chunks if len(c.strip()) >= 40]
        if not chunks:
            summary["skipped_docs"] += 1
            continue
        seen.add(origin)
        summary["docs"] += 1
        for index, chunk in enumerate(chunks, 1):
            source = st.write_source("import", f"{path.stem} · {index}", chunk, origin=origin)
            result = distill(chunk, source, force_rule=True, limit=per_section)
            summary["sections"] += 1
            for change in result["changes"]:
                key = change.get("action")
                if key in ("created", "conflict"):
                    summary["created"] += 1
                elif key == "merged":
                    summary["merged"] += 1
                else:
                    summary["skipped"] += 1
    return summary
