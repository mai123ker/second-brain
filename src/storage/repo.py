"""文档仓库：一个主题 = 一篇长 .md，正文即完整 Markdown。

新增（2026-11-11 全量落地）：
- 每次写盘前自动快照到 .backup/{doc_id}/，每篇保留最近 50 版，可列出/预览/恢复；
- PUT 带 base_updated 做冲突检测，被别处改过返回 CONFLICT，不静默覆盖；
- 支持重命名（同步标题与文件名）、移动目录、归档（移入 _archive，不真删）；
- 待办全局聚合；tree() 加 mtime 签名缓存，避免每次都读全文。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.api.errors import ApiError
from src.storage import frontmatter, index, naming
from src.storage.paths import (ARCHIVE, BACKUP, EXTERNAL, GOALS, PENDING, ROOT,
                               TOPIC_DIRS, TRASH, VAULT, rel, scan_documents)

KINDS = {"goal", "plan", "todo", "thought", "dialog", "review",
         "book", "course", "article", "opinion"}
STANCES = {"experience", "hypothesis", "commercial", "case", "unverified"}
TZ = timezone(timedelta(hours=8))
TODO_RE = re.compile(r"^(\s*(?:[-*]|\d+\.)\s+)\[([ xX])\]\s*(.*)$")
# 四象限判定只认用户自己写在行尾的标签；系统不做推断，标签即依据、可随时取消
TODO_TAG_RE = re.compile(r"(?:\s+#(?:重要|紧急))+\s*$")
TODO_FLAGS = {"important": "重要", "urgent": "紧急"}
IMMUTABLE = {"id", "stream", "created"}
REVISION_KEEP = 50
_TREE_CACHE: dict = {"key": None, "data": None}


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_todos(body: str) -> list[dict]:
    out = []
    for number, line in enumerate(body.splitlines()):
        match = TODO_RE.match(line)
        if not match:
            continue
        raw = match.group(3).strip()
        tail = TODO_TAG_RE.search(raw)
        tags = re.findall(r"#(重要|紧急)", tail.group(0)) if tail else []
        text = (raw[:tail.start()] if tail else raw).strip()
        out.append({"line": number, "text": text,
                    "checked": match.group(2).lower() == "x",
                    "important": "重要" in tags, "urgent": "紧急" in tags,
                    "tags": tags})
    return out


def judgments(body: str, limit: int = 8) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if not text:
            continue
        first = text.splitlines()[0].strip()
        if (first.startswith("#") or first.startswith("|") or first.startswith(">")
                or first.startswith("```") or TODO_RE.match(first)):
            continue
        out.append(text[:300])
        if len(out) >= limit:
            break
    return out


def _snapshot(path: Path, doc_id: str) -> str | None:
    """写盘前把旧版本存进 .backup/{doc_id}/，保留最近 50 版；返回快照文件名。"""
    if not path or not path.exists():
        return None
    folder = BACKUP / (doc_id or path.stem)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    name = f"{stamp}.md"
    try:
        shutil.copy2(path, folder / name)
    except OSError:
        return None
    for old in sorted(folder.glob("*.md"))[:-REVISION_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return name


def _doc_path(doc_id: str) -> Path:
    record = index.by_id(doc_id)
    if not record:
        raise ApiError("INVALID_PARAM", "文档不存在")
    return ROOT / record["path"]


def read_doc(doc_id: str) -> dict | None:
    record = index.by_id(doc_id)
    if not record:
        return None
    path = ROOT / record["path"]
    if not path.exists():
        return None
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    data = dict(meta)
    data.update({"path": rel(path), "body": body,
                 "todos": parse_todos(body), "judgments": judgments(body)})
    return data


def _write_doc(path: Path, meta: dict, body: str, changes: dict | None = None,
               user_edit: bool = True, snapshot: bool = True) -> dict:
    previous = _snapshot(path, str(meta.get("id") or path.stem)) if snapshot else None
    for key, value in (changes or {}).items():
        if key in frontmatter.FIELDS and key not in IMMUTABLE:
            meta[key] = value
    if user_edit:
        meta["updated"] = _now()
    try:
        frontmatter.write_document(path, meta, body)
    except OSError as exc:
        raise ApiError("STORAGE_ERROR", f"写入失败：{exc}") from exc
    record = index.read_file(path)
    index.upsert(record)
    _TREE_CACHE["key"] = None
    if previous:
        record["_previous_revision"] = previous
    return record


def _directory_for(payload: dict, stream: str, kind: str, target: str) -> Path:
    name = str(payload.get("dir") or "").strip()
    if name:
        directory = _safe_rel_path(name)
    elif kind == "goal":
        directory = GOALS
    elif stream == "external":
        directory = EXTERNAL
    elif not target:
        directory = PENDING
    else:
        directory = TOPIC_DIRS["思考"]
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def create_doc(payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    content = payload.get("content")
    if not title:
        raise ApiError("INVALID_PARAM", "文档标题不能为空")
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        raise ApiError("INVALID_PARAM", "正文必须是 1–20000 字的非空 Markdown")
    stream = payload.get("stream") or "self"
    if stream not in ("self", "external"):
        raise ApiError("INVALID_PARAM", "stream 只能是 self 或 external")
    kind = payload.get("kind") or "thought"
    if kind not in KINDS:
        raise ApiError("INVALID_PARAM", "kind 不合法")
    topic = payload.get("topic") or []
    if not isinstance(topic, list) or len(topic) > 5:
        raise ApiError("INVALID_PARAM", "topic 最多 5 个标签")
    target = str(payload.get("target") or "").strip()
    source = str(payload.get("source") or "").strip()
    stance = str(payload.get("stance") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    if stream == "external":
        missing = [k for k, v in (("source", source), ("stance", stance), ("scope", scope)) if not v]
        if missing:
            raise ApiError("EXTERNAL_FIELD_MISSING", "外部素材缺少 " + "/".join(missing) + "，无法参与对照")
        if stance not in STANCES:
            raise ApiError("INVALID_PARAM", "stance 取值不合法")

    body = content.strip()
    if not body.startswith("#"):
        body = f"# {title}\n\n{body}"
    now = _now()
    doc_id = naming.make_id("goal" if kind == "goal" else "doc")
    meta = {
        "id": doc_id, "title": title, "stream": stream, "kind": kind,
        "target": target, "topic": [str(t).strip() for t in topic if str(t).strip()],
        "version": 1, "confidence": 70,
        "status": "placed" if (target or kind == "goal") else "pending",
        "created": now, "updated": now,
        "source": source, "stance": stance, "scope": scope, "claim_relation": "",
    }
    directory = _directory_for(payload, stream, kind, target)
    filename, _ = naming.unique_path(directory, title)
    path = directory / filename
    record = _write_doc(path, meta, body, user_edit=False)
    return {"id": doc_id, "title": title, "status": meta["status"],
            "file": rel(path), "created": now}


def save_body(doc_id: str, body: str, base_updated: str | None = None) -> dict:
    """用户主动编辑整篇正文；base_updated 不一致视为冲突，不静默覆盖。"""
    if not isinstance(body, str) or not body.strip() or len(body) > 20000:
        raise ApiError("INVALID_PARAM", "正文必须是 1–20000 字的非空 Markdown")
    path = _doc_path(doc_id)
    meta, _ = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    current = str(meta.get("updated") or "")
    if base_updated and current and base_updated != current:
        raise ApiError("CONFLICT",
                       f"这篇在别处被改过（磁盘版本 {current[:19]}），请选择覆盖或重新载入")
    # 标题只由 rename_doc 修改。用户写正文不该悄悄改 frontmatter 的 title：
    # 旧实现把正文第一行 H1 的整行写进 title，导致「新建后直接打字」污染标题与树名。
    record = _write_doc(path, meta, body.strip(), None)
    previous = record.pop("_previous_revision", None)
    return {"id": doc_id, "title": record.get("title"), "updated": record.get("updated"),
            "file": record.get("path"), "body": body.strip(),
            "previous_revision": previous, "todos": parse_todos(body)}


def list_revisions(doc_id: str) -> list[dict]:
    folder = BACKUP / doc_id
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.md"), reverse=True):
        text = path.read_text(encoding="utf-8", errors="ignore")
        out.append({"name": path.name, "size": path.stat().st_size,
                    "created": path.stem, "lines": len(text.splitlines())})
    return out


def read_revision(doc_id: str, name: str) -> dict:
    path = BACKUP / doc_id / Path(name).name
    if not path.exists():
        raise ApiError("INVALID_PARAM", "版本不存在")
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    return {"name": path.name, "title": meta.get("title", ""), "body": body,
            "created": meta.get("updated", "")}


def restore_revision(doc_id: str, name: str) -> dict:
    source = BACKUP / doc_id / Path(name).name
    if not source.exists():
        raise ApiError("INVALID_PARAM", "版本不存在")
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(source.read_text(encoding="utf-8"))
    meta["id"] = doc_id
    record = _write_doc(path, meta, body)
    return {"id": doc_id, "updated": record.get("updated"),
            "file": record.get("path"), "restored_from": Path(name).name}


def rename_doc(doc_id: str, title: str, dir_name: str | None = None) -> dict:
    title = str(title or "").strip()
    if not title:
        raise ApiError("INVALID_PARAM", "新标题不能为空")
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines[0] = f"# {title}"
    body = "\n".join(lines)
    meta["title"] = title
    if dir_name:
        target_dir = _safe_rel_path(dir_name)
    else:
        target_dir = path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    filename, _ = naming.unique_path(target_dir, title)
    new_path = target_dir / filename
    _snapshot(path, doc_id)
    if new_path != path:
        shutil.move(str(path), str(new_path))
    record = _write_doc(new_path, meta, body)
    return {"id": doc_id, "title": title, "file": rel(new_path),
            "updated": record.get("updated")}


def archive_doc(doc_id: str) -> dict:
    path = _doc_path(doc_id)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE / path.name
    dup = 0
    while target.exists():
        dup += 1
        target = ARCHIVE / f"{path.stem}-{dup + 1}{path.suffix}"
    _snapshot(path, doc_id)
    shutil.move(str(path), str(target))
    index.rebuild()
    return {"id": doc_id, "file": rel(target)}


def set_todo(doc_id: str, line: int, checked: bool) -> dict:
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    lines = body.splitlines()
    if line < 0 or line >= len(lines):
        raise ApiError("INVALID_PARAM", "待办行号不存在")
    match = TODO_RE.match(lines[line])
    if not match:
        raise ApiError("INVALID_PARAM", "这一行不是待办")
    mark = "x" if checked else " "
    lines[line] = f"{match.group(1)}[{mark}] {match.group(3)}"
    record = _write_doc(path, meta, "\n".join(lines))
    return {"id": doc_id, "line": line, "checked": checked,
            "updated": record.get("updated"), "todos": parse_todos("\n".join(lines))}


def _set_todo_flags(doc_id: str, line: int, important: bool, urgent: bool) -> dict:
    """一次写盘把行尾标签重建为目标状态；勾选状态与正文原样保留。"""
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    lines = body.splitlines()
    if line < 0 or line >= len(lines):
        raise ApiError("INVALID_PARAM", "待办行号不存在")
    match = TODO_RE.match(lines[line])
    if not match:
        raise ApiError("INVALID_PARAM", "这一行不是待办")
    raw = match.group(3).strip()
    tail = TODO_TAG_RE.search(raw)
    clean = (raw[:tail.start()] if tail else raw).strip()
    # 固定按「重要 → 紧急」顺序重建，避免重复标签或顺序漂移
    tags = [tag for tag, on in (("重要", important), ("紧急", urgent)) if on]
    suffix = "".join(f" #{tag}" for tag in tags)
    mark = "x" if match.group(2).lower() == "x" else " "
    lines[line] = f"{match.group(1)}[{mark}] {clean}{suffix}"
    body = "\n".join(lines)
    record = _write_doc(path, meta, body)
    return {"id": doc_id, "line": line, "important": important, "urgent": urgent,
            "updated": record.get("updated"), "todos": parse_todos(body)}


def flag_todo(doc_id: str, line: int, field: str, value: bool) -> dict:
    """给一条待办加/去 #重要 或 #紧急。标签写在原文里，是判定依据，也可随时推翻。"""
    if field not in TODO_FLAGS:
        raise ApiError("INVALID_PARAM", "只能标 important / urgent")
    path = _doc_path(doc_id)
    _meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    current = next((t for t in parse_todos(body) if t["line"] == line), None)
    if not current:
        raise ApiError("INVALID_PARAM", "这一行不是待办")
    important = value if field == "important" else current["important"]
    urgent = value if field == "urgent" else current["urgent"]
    return _set_todo_flags(doc_id, line, important, urgent)


def place_todo(doc_id: str, line: int, important: bool, urgent: bool) -> dict:
    """拖放到某个象限：一次写盘同时定下两个标签，不分两步、不留半截状态。"""
    return _set_todo_flags(doc_id, line, bool(important), bool(urgent))


def add_todo(doc_id: str, text: str) -> dict:
    text = str(text or "").strip().replace("\n", " ")
    if not text:
        raise ApiError("INVALID_PARAM", "待办内容不能为空")
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    lines = body.splitlines()
    heading = next((i for i, l in enumerate(lines) if l.strip().startswith("## 待办")), None)
    if heading is None:
        body = body.rstrip() + f"\n\n## 待办\n\n- [ ] {text}\n"
    else:
        insert = len(lines)
        for i in range(heading + 1, len(lines)):
            if lines[i].startswith("## "):
                insert = i
                break
        while insert > heading + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, f"- [ ] {text}")
        body = "\n".join(lines)
    record = _write_doc(path, meta, body)
    return {"id": doc_id, "text": text, "updated": record.get("updated"),
            "todos": parse_todos(body)}


def all_todos() -> list[dict]:
    out = []
    for record in index.all_records():
        doc = read_doc(record.get("id"))
        if not doc:
            continue
        for todo in doc.get("todos") or []:
            out.append({"doc_id": record.get("id"), "title": record.get("title"),
                        "target": record.get("target"), "path": record.get("path"),
                        "line": todo["line"], "text": todo["text"],
                        "checked": todo["checked"],
                        "important": todo.get("important", False),
                        "urgent": todo.get("urgent", False),
                        "tags": todo.get("tags", [])})
    out.sort(key=lambda x: (x["checked"], str(x["path"]), x["line"]))
    return out


def _desc(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if text and not text.startswith("#") and not text.startswith("|") and not text.startswith(">"):
            return text[:120]
    return ""


def read_goal(goal_id: str) -> dict | None:
    record = index.by_id(goal_id)
    if not record or record.get("kind") != "goal":
        return None
    doc = read_doc(goal_id) or {}
    return {"id": goal_id, "name": record.get("title") or goal_id,
            "desc": _desc(doc.get("body", "")), "topic": record.get("topic") or [],
            "path": record.get("path")}


def list_goals() -> list[dict]:
    out = []
    for record in index.all_records():
        if record.get("kind") != "goal":
            continue
        gid = record.get("id")
        doc = read_doc(gid) or {}
        out.append({
            "id": gid, "name": record.get("title") or gid,
            "desc": _desc(doc.get("body", "")),
            "topic": record.get("topic") or [],
            "self_count": len(index.by_target(gid, "self")),
            "external_count": len([r for r in index.by_target(gid, "external")
                                   if str(r.get("scope") or "").strip()]),
        })
    return out


def list_docs(status: str | None = None, stream: str | None = None) -> list[dict]:
    records = index.all_records()
    if status:
        records = [r for r in records if r.get("status") == status]
    if stream:
        records = [r for r in records if r.get("stream") == stream]
    records.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    return [{"id": r.get("id"), "title": r.get("title"), "stream": r.get("stream"),
             "kind": r.get("kind"), "target": r.get("target"), "topic": r.get("topic"),
             "status": r.get("status"), "date": str(r.get("updated") or "")[:10],
             "content": r.get("content"), "source": r.get("source"),
             "stance": r.get("stance"), "scope": r.get("scope"),
             "file": r.get("path")} for r in records]


ORDER_FILE = ".order.json"


def _read_order(directory: Path) -> list[str]:
    path = directory / ORDER_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_order(directory: Path, order: list[str]) -> None:
    path = directory / ORDER_FILE
    path.write_text(json.dumps(order, ensure_ascii=False, indent=2),
                    encoding="utf-8", newline="\n")


def _safe_rel_path(rel_str: str) -> Path:
    parts = [p for p in re.split(r"[/\\]", str(rel_str or "").strip())
             if p and p not in (".", "..")]
    if parts and parts[0] == "records":
        parts = parts[1:]
    path = VAULT.joinpath(*parts) if parts else VAULT
    if not path.resolve().is_relative_to(VAULT.resolve()):
        raise ApiError("INVALID_PARAM", "路径不合法")
    return path


def create_folder(parent: str, name: str) -> dict:
    clean = naming.safe_name(name)
    if not clean:
        raise ApiError("INVALID_PARAM", "文件夹名不能为空")
    base = _safe_rel_path(parent) if parent else VAULT
    if not base.exists() or not base.is_dir():
        raise ApiError("INVALID_PARAM", "上级文件夹不存在")
    target = base / clean
    if target.exists():
        raise ApiError("INVALID_PARAM", "同名文件夹已存在")
    target.mkdir(parents=True)
    _TREE_CACHE["key"] = None
    return {"name": clean, "path": rel(target)}


def move_folder(src: str, dst: str) -> dict:
    source = _safe_rel_path(src)
    target_dir = _safe_rel_path(dst)
    if not source.exists() or not source.is_dir():
        raise ApiError("INVALID_PARAM", "要移动的文件夹不存在")
    if not target_dir.exists() or not target_dir.is_dir():
        raise ApiError("INVALID_PARAM", "目标文件夹不存在")
    if target_dir == source or target_dir.is_relative_to(source):
        raise ApiError("INVALID_PARAM", "不能移到自己或自己的子目录里")
    target = target_dir / source.name
    if target.exists():
        raise ApiError("INVALID_PARAM", "目标位置已有同名文件夹")
    shutil.move(str(source), str(target))
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"path": rel(target)}


def set_order(dir_rel: str, order: list) -> dict:
    directory = _safe_rel_path(dir_rel) if dir_rel else VAULT
    if not directory.exists() or not directory.is_dir():
        raise ApiError("INVALID_PARAM", "目录不存在")
    names = [str(n) for n in (order or []) if str(n).strip()]
    _write_order(directory, names)
    _TREE_CACHE["key"] = None
    return {"path": rel(directory), "order": names}


def rename_folder(path_rel: str, name: str) -> dict:
    folder = _safe_rel_path(path_rel)
    if not folder.exists() or not folder.is_dir():
        raise ApiError("INVALID_PARAM", "文件夹不存在")
    if folder.name.startswith("_"):
        raise ApiError("INVALID_PARAM", "系统目录不能重命名")
    clean = naming.safe_name(name)
    if not clean:
        raise ApiError("INVALID_PARAM", "文件夹名不能为空")
    target = folder.parent / clean
    if target.exists():
        raise ApiError("INVALID_PARAM", "同名文件夹已存在")
    shutil.move(str(folder), str(target))
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"path": rel(target), "name": clean}


def _free_path(directory: Path, name: str, suffix: str = "") -> Path:
    target = directory / f"{name}{suffix}"
    dup = 0
    while target.exists():
        dup += 1
        target = directory / f"{name}-{dup + 1}{suffix}"
    return target


def copy_folder(src: str, dst: str) -> dict:
    source = _safe_rel_path(src)
    target_dir = _safe_rel_path(dst)
    if not source.exists() or not source.is_dir():
        raise ApiError("INVALID_PARAM", "要复制的文件夹不存在")
    if not target_dir.exists() or not target_dir.is_dir():
        raise ApiError("INVALID_PARAM", "目标文件夹不存在")
    if target_dir == source or target_dir.is_relative_to(source):
        raise ApiError("INVALID_PARAM", "不能复制到自己或自己的子目录里")
    target = _free_path(target_dir, source.name)
    shutil.copytree(source, target)
    for path in target.rglob("*.md"):
        meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
        if meta.get("id"):
            meta["id"] = naming.make_id("doc")
            frontmatter.write_document(path, meta, body)
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"path": rel(target)}


def delete_folder(path_rel: str) -> dict:
    folder = _safe_rel_path(path_rel)
    if folder == VAULT:
        raise ApiError("INVALID_PARAM", "不能删除笔记根")
    if folder.name.startswith("_"):
        raise ApiError("INVALID_PARAM", "系统目录不能删除")
    if not folder.exists() or not folder.is_dir():
        raise ApiError("INVALID_PARAM", "文件夹不存在")
    TRASH.mkdir(parents=True, exist_ok=True)
    target = _free_path(TRASH, folder.name)
    shutil.move(str(folder), str(target))
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"path": rel(target), "trash_name": target.name,
            "original": rel(folder), "restorable": True}


def copy_doc(doc_id: str, dir_name: str | None = None) -> dict:
    doc = read_doc(doc_id)
    if not doc:
        raise ApiError("INVALID_PARAM", "文档不存在")
    directory = _safe_rel_path(dir_name) if dir_name else VAULT
    directory.mkdir(parents=True, exist_ok=True)
    title = (doc.get("title") or "未命名") + " 副本"
    filename, _ = naming.unique_path(directory, title)
    meta = {key: doc.get(key, "") for key in frontmatter.FIELDS}
    meta["id"] = naming.make_id("doc")
    meta["title"] = title
    now = _now()
    meta["created"] = now
    meta["updated"] = now
    body = doc.get("body") or ""
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines[0] = f"# {title}"
        body = "\n".join(lines)
    record = _write_doc(directory / filename, meta, body, user_edit=False)
    return {"id": meta["id"], "title": title, "file": record.get("path")}


def delete_doc(doc_id: str) -> dict:
    path = _doc_path(doc_id)
    TRASH.mkdir(parents=True, exist_ok=True)
    target = _free_path(TRASH, path.stem, path.suffix)
    _snapshot(path, doc_id)
    shutil.move(str(path), str(target))
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"id": doc_id, "file": rel(target), "trash_name": target.name,
            "original": rel(path), "restorable": True}


def restore_from_trash(name: str, to: str | None = None) -> dict:
    """从 records/_trash/ 还原到指定目录；用于 Ctrl+Z 撤销删除。"""
    clean = Path(str(name or "")).name
    source = TRASH / clean
    if not clean or not source.exists():
        raise ApiError("INVALID_PARAM", "回收站里没有这一项，可能已经还原过")
    target_dir = _safe_rel_path(to) if to else VAULT
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (_free_path(target_dir, source.name) if source.is_dir()
              else _free_path(target_dir, source.stem, source.suffix))
    shutil.move(str(source), str(target))
    index.rebuild()
    _TREE_CACHE["key"] = None
    return {"path": rel(target), "restored": True}


def _vault_dirs() -> list[Path]:
    """vault 下所有用户可见目录（含嵌套），用于感知新建/删除文件。"""
    out = []
    for path in VAULT.rglob("*"):
        if not path.is_dir():
            continue
        parts = path.relative_to(VAULT).parts
        if any(p.startswith(".") or p.startswith("_") or p == "archive" for p in parts):
            continue
        out.append(path)
    return out


def tree(force: bool = False) -> dict:
    signature = []
    for path in scan_documents():
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
    # 目录本身也要进签名：用户可以在 Obsidian 里直接新建/删除文件，
    # 光看已有文件的 mtime 察觉不到「多了一个文件」，树就会停留在旧状态。
    for directory in _vault_dirs():
        try:
            stat = directory.stat()
            signature.append((str(directory), stat.st_mtime_ns, 0))
        except OSError:
            pass
    key = tuple(sorted(signature))
    if not force and _TREE_CACHE["key"] == key and _TREE_CACHE["data"]:
        return _TREE_CACHE["data"]

    def walk(directory: Path) -> list[dict]:
        nodes = []
        order = _read_order(directory)
        items = [p for p in directory.iterdir()
                 if not p.name.startswith(".") and not p.name.startswith("_")
                 and p.name != "archive"]

        def sort_key(path: Path):
            try:
                position = order.index(path.name)
            except ValueError:
                position = len(order) + 10000
            return (position, path.name)

        for path in sorted(items, key=sort_key):
            if path.is_dir():
                nodes.append({"type": "dir", "name": path.name, "path": rel(path),
                              "file": path.name, "children": walk(path)})
            elif path.suffix == ".md":
                meta, _ = frontmatter.parse_document(path.read_text(encoding="utf-8"))
                doc_id = str(meta.get("id") or "")
                # 没有 id 的文件是「孤儿」：用户直接在 Obsidian 里新建的，
                # 还没初始化。它不是待归位，别混进 pending 计数里。
                nodes.append({"type": "doc", "id": doc_id,
                              "name": meta.get("title") or path.stem,
                              "path": rel(path), "file": path.name,
                              "kind": meta.get("kind", ""),
                              "status": "" if not doc_id else meta.get("status", "pending"),
                              "target": meta.get("target", ""),
                              "orphan": not doc_id})
        return nodes

    data = {"name": "笔记", "path": "records", "children": walk(VAULT)}
    _TREE_CACHE.update(key=key, data=data)
    return data


_KEY_LINE = re.compile(r"^\s*[^\s:#][^:]*\s*:")
_CONT_LINE = re.compile(r"^\s*(?:[-*]\s|\S+\s*:)")


def _split_raw(text: str) -> tuple[dict, str]:
    """切成 (元数据, 正文原文)。

    和 frontmatter.parse_document 的区别：正文部分**原样保留**（不 rstrip、
    不规整换行），因为补齐 frontmatter 时用户原文一个字都不能动。

    判定开头那段是不是 frontmatter，两道关：
    1. 每一行都得像 YAML 的「键: 值」（或它的列表续行）——否则是用户拿 --- 当分隔线；
    2. 至少含一个**本系统认识的字段**——否则说明这不是元数据，只是长得像。
    两道都过才切开。判不出来时一律当正文：宁可多当正文，绝不吞用户内容。
    """
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", text, re.S)
    if not match:
        return {}, text
    block = match.group(1)
    for line in block.splitlines():
        if not line.strip():
            continue
        if _KEY_LINE.match(line):
            continue
        if line[:1].isspace() and _CONT_LINE.match(line):
            continue  # 列表续行：topic: 下面的 - xxx
        return {}, text
    meta, _ = frontmatter.parse_document(match.group(0))
    if not meta:
        # 一行都不认识 —— 这不是本系统的元数据，整篇当正文。
        return {}, text
    return meta, text[match.end():]


def _orphan_defaults(path: Path) -> dict:
    """孤儿文件（没有元数据的 .md）补齐时用的推断默认值。

    推断只依据「文件所在目录」这一个客观事实，不猜用户意图；
    立场一律标 unverified（未核实），来源与适用前提留空由用户自己补。
    """
    folder = path.parent.name
    base = {"stream": "self", "kind": "thought", "source": "",
            "stance": "", "scope": "", "claim_relation": ""}
    if folder == "外部素材":
        base.update({"stream": "external", "kind": "article",
                     "source": path.name, "stance": "unverified",
                     "scope": "未标注（Obsidian 新建）"})
    elif folder == "目标":
        base.update({"kind": "goal"})
    elif folder == "待归位":
        base.update({"kind": "todo"})
    return base


def list_orphans() -> list[dict]:
    """列出还没初始化的文件（用户直接在 Obsidian 里直接新建的 .md）。

    只报事实：路径、文件大小、正文头几行。系统不主动改这些文件。
    过滤规则与 tree() 一致（跳过 _ 开头的归档目录与文件），两边看到的必须是同一批。
    """
    out = []
    for path in scan_documents():
        if path.name.startswith(".") or path.name.startswith("_"):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _split_raw(raw)
        if str(meta.get("id") or "").strip():
            continue
        head = body.strip().splitlines()
        out.append({"path": rel(path), "name": path.stem,
                    "folder": path.parent.name,
                    "size": path.stat().st_size,
                    "preview": " ".join(head[:3])[:120]})
    return out


def adopt_doc(path_rel: str, payload: dict) -> dict:
    """把一个没有元数据的文件补成正式文档：只写 frontmatter，正文一个字节不动。

    这是用户手动点「补齐」才走的路径——系统不会主动改任何用户文件。
    """
    path = _safe_rel_path(path_rel)
    if path.suffix != ".md" or not path.exists() or not path.is_file():
        raise ApiError("INVALID_PARAM", "文件不存在")
    raw = path.read_text(encoding="utf-8")
    meta, body_raw = _split_raw(raw)
    if meta.get("id"):
        raise ApiError("CONFLICT", "这篇已经有元数据了，不用补齐")

    title = str(payload.get("title") or "").strip()
    target = str(payload.get("target") or "").strip()
    if target:
        goal = index.by_id(target)
        if not goal or goal.get("kind") != "goal":
            raise ApiError("INVALID_PARAM", "目标不存在")

    now = _now()
    defaults = _orphan_defaults(path)
    kind = defaults["kind"]
    stream = defaults["stream"]
    new_meta = {
        "id": naming.make_id("doc"), "title": title, "stream": stream,
        "kind": kind, "target": target, "topic": [],
        "version": 1, "confidence": 70,
        "status": "placed" if (target or kind == "goal") else "pending",
        "created": now, "updated": now,
        "source": defaults["source"], "stance": defaults["stance"],
        "scope": defaults["scope"], "claim_relation": defaults["claim_relation"],
    }
    # 用户自己已经写过的 frontmatter 字段（部分元数据）要**保留**，
    # 只补上缺的 id/时间等，不能把他写的东西抹掉。
    for key, value in meta.items():
        if key in ("id", "created", "updated", "version"):
            continue
        if isinstance(value, list):
            if value:
                new_meta[key] = value
        elif str(value).strip():
            new_meta[key] = value
    # 弹窗里填的标题/目标是他刚刚明确操作过的，优先于文件里原有的。
    if title:
        new_meta["title"] = title
    if target:
        new_meta["target"] = target
    if not new_meta["title"]:
        new_meta["title"] = path.stem
    if new_meta["status"] == "pending" and (new_meta["target"] or new_meta["kind"] == "goal"):
        new_meta["status"] = "placed"

    # 只在原 frontmatter 位置写入；正文原文（含原始换行）原样保留。
    # 注意：正文末尾**不补换行**——补齐只加元信息，正文一个字节都不动。
    separator = "" if body_raw.startswith("\n") else "\n"
    text = frontmatter.dump(new_meta) + (separator + body_raw if body_raw.strip() else "")
    _snapshot(path, new_meta["id"])
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ApiError("STORAGE_ERROR", f"写入失败：{exc}") from exc
    record = index.read_file(path)
    index.upsert(record)
    _TREE_CACHE["key"] = None
    return {"id": new_meta["id"], "title": new_meta["title"],
            "status": new_meta["status"], "stream": stream, "kind": kind,
            "target": new_meta["target"], "file": rel(path),
            "created": now, "updated": now}


def place(doc_id: str, action: str, payload: dict) -> dict:
    path = _doc_path(doc_id)
    meta, body = frontmatter.parse_document(path.read_text(encoding="utf-8"))
    if action == "ignore":
        return {"id": doc_id, "status": "silent",
                "record": _write_doc(path, meta, body, {"status": "silent"}, user_edit=False)}
    if action == "attach":
        target = str(payload.get("target") or "").strip()
        if not target:
            raise ApiError("INVALID_PARAM", "缺少目标 id")
        return {"id": doc_id, "status": "placed", "target": target,
                "record": _write_doc(path, meta, body,
                                     {"target": target, "status": "placed"}, user_edit=False)}
    if action == "create":
        created = create_doc({"title": payload.get("name"), "content": payload.get("name"),
                              "stream": "self", "kind": "goal", "topic": []})
        return {"id": doc_id, "status": "placed", "target": created["id"],
                "goal": {"id": created["id"], "name": payload.get("name")},
                "record": _write_doc(path, meta, body,
                                     {"target": created["id"], "status": "placed"}, user_edit=False)}
    raise ApiError("INVALID_PARAM", "action 只能是 attach / create / ignore")
