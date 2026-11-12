"""SQLite 索引：仅作索引，删库可重建，文档一条不丢。

搜索优先走 FTS5（trigram 分词，中文子串可命中，bm25 相关性排序），
SQLite 不支持或查询词过短时自动回退 LIKE（标题命中优先）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from src.storage import frontmatter
from src.storage.paths import INDEX_DB, rel, scan_documents

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    title TEXT, stream TEXT, kind TEXT, target TEXT, topic TEXT, version INTEGER,
    confidence INTEGER, status TEXT, created TEXT, updated TEXT,
    source TEXT, stance TEXT, scope TEXT, claim_relation TEXT,
    path TEXT, content TEXT
)
"""
COLS = ("id", "title", "stream", "kind", "target", "topic", "version", "confidence",
        "status", "created", "updated", "source", "stance", "scope",
        "claim_relation", "path", "content")
INSERT = ("INSERT OR REPLACE INTO records (" + ", ".join(COLS) + ") VALUES (" +
          ", ".join(["?"] * len(COLS)) + ")")
FTS_INSERT = "INSERT INTO records_fts(id, title, content) VALUES (?, ?, ?)"
FTS_TRIGRAM = ("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING "
               "fts5(id UNINDEXED, title, content, tokenize='trigram')")
FTS_PLAIN = ("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING "
             "fts5(id UNINDEXED, title, content)")

_FTS_MODE: str | None = None


def _connect() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _row(record: dict) -> tuple:
    topic = record.get("topic") or []
    if isinstance(topic, str):
        try:
            topic = json.loads(topic)
        except (ValueError, TypeError):
            topic = [t.strip() for t in topic.split(",") if t.strip()]
    data = dict(record)
    data["topic"] = json.dumps(topic, ensure_ascii=False)
    for key in ("version", "confidence"):
        try:
            data[key] = int(data.get(key) or 0)
        except (TypeError, ValueError):
            data[key] = 0
    return tuple(str(data.get(k) if data.get(k) is not None else "") for k in COLS)


def _fts_values(row: tuple) -> tuple:
    # COLS: id=0, title=1, content=16
    return row[0], row[1], row[16]


def _probe_mode(conn: sqlite3.Connection) -> str:
    """探测并创建 FTS5 表；trigram 不支持时退到默认分词，再不行就纯 LIKE。"""
    global _FTS_MODE
    if _FTS_MODE is not None:
        return _FTS_MODE
    for ddl, mode in ((FTS_TRIGRAM, "trigram"), (FTS_PLAIN, "unicode61")):
        try:
            conn.execute(ddl)
            _FTS_MODE = mode
            return mode
        except sqlite3.OperationalError:
            continue
    _FTS_MODE = "off"
    return "off"


def engine() -> str:
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        mode = _probe_mode(conn)
        return "fts5-" + mode if mode != "off" else "like"
    finally:
        conn.close()


def _from_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["topic"] = json.loads(data.get("topic") or "[]")
    except (ValueError, TypeError):
        data["topic"] = []
    return data


def read_file(path) -> dict:
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    meta["path"] = rel(path)
    meta["content"] = body
    return meta


def upsert(record: dict) -> None:
    # 没有 id 的文件是「还没初始化」的孤儿（用户直接在 Obsidian 里新建的），
    # 它们不是文档，不进索引——否则多条孤儿会挤在同一个空主键上互相覆盖。
    if not str(record.get("id") or "").strip():
        return
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        row = _row(record)
        conn.execute(INSERT, row)
        if _probe_mode(conn) != "off":
            conn.execute("DELETE FROM records_fts WHERE id = ?", (row[0],))
            conn.execute(FTS_INSERT, _fts_values(row))
        conn.commit()
    finally:
        conn.close()


def rebuild() -> int:
    global _FTS_MODE
    conn = _connect()
    try:
        conn.execute("DROP TABLE IF EXISTS records_fts")
        conn.execute("DROP TABLE IF EXISTS records")
        conn.execute(SCHEMA)
        _FTS_MODE = None
        mode = _probe_mode(conn)
        for path in scan_documents():
            record = read_file(path)
            if not str(record.get("id") or "").strip():
                continue  # 孤儿文件不进索引
            row = _row(record)
            conn.execute(INSERT, row)
            if mode != "off":
                conn.execute(FTS_INSERT, _fts_values(row))
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()


def count() -> int:
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()


def by_id(record_id: str) -> dict | None:
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return _from_row(row) if row else None
    finally:
        conn.close()


def by_target(target: str, stream: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        sql = "SELECT * FROM records WHERE target = ?"
        args: list = [target]
        if stream:
            sql += " AND stream = ?"
            args.append(stream)
        return [_from_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def all_records() -> list[dict]:
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        return [_from_row(r) for r in conn.execute("SELECT * FROM records").fetchall()]
    finally:
        conn.close()


def _like_search(conn: sqlite3.Connection, like: str, limit: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT * FROM records WHERE title LIKE ? OR content LIKE ? "
        "ORDER BY CASE WHEN title LIKE ? THEN 0 ELSE 1 END, updated DESC LIMIT ?",
        (like, like, like, limit)).fetchall()
    return [(row, 0.0) for row in rows]


def search(keyword: str, limit: int = 30) -> list[dict]:
    """返回按相关度排序的结果；每条带 snippet / score / engine。"""
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    like = f"%{keyword}%"
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        mode = _probe_mode(conn)
        pairs: list[tuple] = []
        used = "like"
        # trigram 至少 3 个字符才能切出窗口；短词直接走 LIKE，别硬凑
        if mode == "trigram" and len(keyword) >= 3:
            phrase = '"' + keyword.replace('"', '""') + '"'
            try:
                rows = conn.execute(
                    "SELECT r.*, bm25(records_fts) AS score FROM records_fts "
                    "JOIN records r ON r.id = records_fts.id "
                    "WHERE records_fts MATCH ? ORDER BY score LIMIT ?",
                    (phrase, limit)).fetchall()
                pairs = [(row, float(row["score"] or 0.0)) for row in rows]
                if pairs:
                    used = "fts5-trigram"
            except sqlite3.OperationalError:
                pairs = []
        elif mode == "unicode61":
            phrase = '"' + keyword.replace('"', '""') + '"'
            try:
                rows = conn.execute(
                    "SELECT r.*, bm25(records_fts) AS score FROM records_fts "
                    "JOIN records r ON r.id = records_fts.id "
                    "WHERE records_fts MATCH ? ORDER BY score LIMIT ?",
                    (phrase, limit)).fetchall()
                pairs = [(row, float(row["score"] or 0.0)) for row in rows]
                if pairs:
                    used = "fts5-unicode61"
            except sqlite3.OperationalError:
                pairs = []
        if not pairs:
            pairs = _like_search(conn, like, limit)
            used = "like"
    finally:
        conn.close()
    out = []
    for row, score in pairs:
        data = _from_row(row)
        content = str(data.get("content") or "")
        position = content.find(keyword)
        if position >= 0:
            snippet = content[max(0, position - 40): position + 60]
        else:
            snippet = content[:100]
        out.append({"id": data.get("id"), "title": data.get("title"),
                    "path": data.get("path"), "kind": data.get("kind"),
                    "target": data.get("target"), "snippet": snippet.strip(),
                    "score": round(score, 4), "engine": used})
    return out


# ---------- §20 对话内实时对照：检索用户自己的历史原文（只读、三态降级） ----------

def _keywords(text: str, limit: int = 8) -> list[str]:
    """从一句话里挑检索词：英文/数字词 + 中文 2 字滑窗单元，剔除停用词与互相包含的词。

    中文不走长滑窗——跨词碎片（"这周把精"）命不中任何原文；2 字单元贴近真实词边界，
    靠 related_passages 的多词覆盖数来排序，而不是靠单词精度。
    """
    text = str(text or "")
    freq: dict = {}
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_]+", text):
        w = word.lower()
        if len(w) >= 2 and w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        pieces = [run] if len(run) <= 4 else [run[i:i + 2] for i in range(len(run) - 1)]
        for piece in pieces:
            if piece in STOPWORDS or len(piece) < 2:
                continue
            freq[piece] = freq.get(piece, 0) + 1
    cands = sorted(freq, key=lambda w: (-freq[w], -len(w)))
    picked: list[str] = []
    for word in cands:
        if any(word in p or p in word for p in picked):
            continue
        picked.append(word)
        if len(picked) >= max(1, min(limit, 10)):
            break
    return picked


def related_passages(query: str, exclude_ids: set | None = None,
                     limit: int = 5, excerpt_chars: int = 200) -> list[dict]:
    """取与这句话最相关的「用户自己的」历史原文段，供对话内偏差对照。

    - 只返回 stream=self（含目标线），外部素材无权在对话内出现（全集 §20.2）。
    - 排序按命中的不同关键词数（覆盖数），不是单词相关度。
    - 检索三态：命中返回列表 / 无命中返回 [] / 索引异常抛给调用方静默降级。
    """
    exclude = {str(i) for i in (exclude_ids or set()) if i}
    kws = _keywords(query)
    if not kws:
        return []
    conn = _connect()
    merged: dict[str, dict] = {}
    try:
        conn.execute(SCHEMA)
        for kw in kws:
            for hit in search(kw, limit=6):
                rid = str(hit.get("id") or "")
                if not rid or rid in exclude:
                    continue
                row = conn.execute(
                    "SELECT stream, kind, title, updated, content FROM records WHERE id = ?",
                    (rid,)).fetchone()
                if row is None or str(row["stream"] or "") != "self":
                    continue
                entry = merged.get(rid)
                if entry is None:
                    content = str(row["content"] or "")
                    pos = -1
                    for probe in kws:
                        pos = content.find(probe)
                        if pos >= 0:
                            break
                    start = max(0, pos - 100)
                    excerpt = content[start:start + excerpt_chars].strip()
                    if pos > 0 and start > 0:
                        excerpt = "……" + excerpt
                    entry = {"id": rid, "path": hit.get("path") or "",
                             "title": str(row["title"] or "未命名"),
                             "kind": str(row["kind"] or ""),
                             "date": str(row["updated"] or "")[:10],
                             "excerpt": excerpt, "hits": 0}
                    merged[rid] = entry
                entry["hits"] += 1
        ranked = sorted(merged.values(), key=lambda e: -e["hits"])
        out = []
        for e in ranked[:max(1, min(limit, 8))]:
            e.pop("hits", None)
            out.append(e)
        return out
    finally:
        conn.close()


# ---------- P1 统计：未探索提示 + 悬空概念（纯规则、只读、不写盘） ----------

STOPWORDS = set("""
的 了 自己 我们 你们 他们 觉得 就是 一个 什么 问题 时间 事情 现在 可能
因为 所以 但是 如果 这个 那个 这些 那些 没有 不是 还是 已经 可以 应该
怎么 其实 这样 那样 一些 一点 开始 最后 之后 然后 东西 地方 方面 过程
结果 原因 方式 方法 情况 时候 而且 虽然 不过 只是 关于 通过 对于 作为
能够 出来 起来 下来 一直 一定 有点 有些 很多 许多 直接 重要 紧急 今天
昨天 明天 上午 下午 以及 或者 比如 例如 因此 总之 其实 到底 确实 真的
""".split())


def coverage(days: int = 7) -> dict:
    """最近 N 天动过哪些线。只报事实（哪些天、哪些线），判定权在用户。"""
    conn = _connect()
    try:
        conn.execute(SCHEMA)
        rows = conn.execute(
            "SELECT updated, target, kind FROM records WHERE status != 'silent'").fetchall()
        goal_names = {r["id"]: r["title"] for r in conn.execute(
            "SELECT id, title FROM records WHERE kind = 'goal'").fetchall()}
    finally:
        conn.close()
    today = date.today()
    by_day: dict = {}
    for row in rows:
        if row["kind"] == "goal":
            continue
        day_str = str(row["updated"] or "")[:10]
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            continue
        if not 0 <= (today - day).days < max(1, min(days, 30)):
            continue
        by_day.setdefault(day, set()).add(str(row["target"] or "").strip())
    active = sorted(by_day, reverse=True)
    target_ids: set = set()
    for targets in by_day.values():
        target_ids |= {t for t in targets if t}
    lines = sorted(goal_names.get(t, t) for t in target_ids)
    on_single = len(active) >= 2 and len(lines) == 1
    return {"window": max(1, min(days, 30)), "active_days": len(active),
            "lines": lines, "on_single_line": on_single,
            "line": lines[0] if on_single else "",
            "days": [d.isoformat() for d in active]}


def dangling_concepts(limit: int = 3) -> list[dict]:
    """悬空概念：≥2 篇没归位的记录里出现、但从未匹配任何线/topic 的词。

    语料只取标题 + 标题行 + 待办行（正文太吵，误报会淹没提议）。
    纯规则统计，不写盘；「立成一条线」必须用户点。"""
    recs = all_records()
    known: set = set()
    goals: dict = {}
    pending: list[dict] = []
    for r in recs:
        title = str(r.get("title") or "").strip()
        if r.get("kind") == "goal":
            goals[str(r.get("id") or "")] = title
            if title:
                known.add(title)
            continue
        if (r.get("target") or "").strip() or r.get("status") == "silent":
            continue
        pending.append(r)
    for r in recs:  # 已归位文档的 topic 也算已知概念
        if r.get("kind") == "goal" or not (r.get("target") or "").strip():
            continue
        for t in (r.get("topic") or []):
            if str(t).strip():
                known.add(str(t).strip())
    for t in {str(r.get("target") or "").strip() for r in recs
              if (r.get("target") or "").strip()}:
        if t in goals and goals[t]:
            known.add(goals[t])

    docs_tokens: list[set] = []
    for r in pending:
        content = str(r.get("content") or "")
        parts = [str(r.get("title") or "")]
        parts += re.findall(r"^#{1,6}\s+(.+)$", content, flags=re.M)
        parts += re.findall(r"^\s*(?:[-*]|\d+\.)\s+\[[ xX]\]\s+(.+)$", content, flags=re.M)
        cands: set = set()
        for text in parts:
            for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
                for size in range(2, min(len(run), 6) + 1):
                    for start in range(len(run) - size + 1):
                        cands.add(run[start:start + size])
        docs_tokens.append(cands)

    df: dict = {}
    for cands in docs_tokens:
        for word in cands:
            df[word] = df.get(word, 0) + 1

    def contaminated(word: str) -> bool:
        return any(len(k) >= 2 and (k in word or word in k) for k in known)

    kept = [w for w, n in df.items()
            if n >= 2 and w not in STOPWORDS and not contaminated(w)]
    kept.sort(key=lambda w: (-df[w], -len(w)))
    final: list = []
    for w in kept:  # 高频长词优先，丢掉它的子串
        if any(w in f for f in final):
            continue
        final.append(w)
        if len(final) >= max(1, min(limit, 5)):
            break
    return [{"word": w, "docs": df[w]} for w in final]
