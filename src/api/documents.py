"""文档树、文档本体、历史版本、重命名/移动/归档、待办聚合与搜索。"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import ApiError, ok
from src.storage import index, repo

router = APIRouter(tags=["documents"])


# 注意：下面两条必须声明在 /docs/{doc_id} 之前，否则会被通配路由抢先匹配。
@router.get("/docs/orphans")
def get_orphans():
    """列出还没初始化的文件（用户在 Obsidian 里直接新建的）。只读。"""
    return ok({"orphans": repo.list_orphans()})


@router.post("/docs/adopt")
def post_adopt(payload: dict):
    """给「还没初始化」的文件补齐 frontmatter。

    只在用户自己点了「补齐」之后才调用；正文一个字节都不动。
    """
    return ok(repo.adopt_doc(payload.get("path") or "", payload))


@router.get("/docs/tree")
def get_tree(force: bool = False):
    return ok(repo.tree(force=force))


@router.get("/docs/{doc_id}")
def get_doc(doc_id: str):
    doc = repo.read_doc(doc_id)
    if not doc:
        raise ApiError("INVALID_PARAM", "文档不存在")
    return ok({"doc": doc})


@router.put("/docs/{doc_id}")
def put_doc(doc_id: str, payload: dict):
    return ok(repo.save_body(doc_id, payload.get("content"),
                             payload.get("base_updated")))


@router.get("/docs/{doc_id}/revisions")
def get_revisions(doc_id: str):
    return ok({"revisions": repo.list_revisions(doc_id)})


@router.get("/docs/{doc_id}/revisions/{name}")
def get_revision(doc_id: str, name: str):
    return ok(repo.read_revision(doc_id, name))


@router.post("/docs/{doc_id}/revisions/{name}/restore")
def post_restore(doc_id: str, name: str):
    return ok(repo.restore_revision(doc_id, name))


@router.post("/docs/{doc_id}/rename")
def post_rename(doc_id: str, payload: dict):
    return ok(repo.rename_doc(doc_id, payload.get("title"), payload.get("dir")))


@router.post("/docs/{doc_id}/archive")
def post_archive(doc_id: str):
    return ok(repo.archive_doc(doc_id))


@router.post("/docs/{doc_id}/todo")
def post_todo(doc_id: str, payload: dict):
    action = payload.get("action") or "add"
    if action in ("toggle", "flag", "place"):
        try:
            line = int(payload.get("line"))
        except (TypeError, ValueError) as exc:
            raise ApiError("INVALID_PARAM", "缺少合法的待办行号") from exc
        if action == "flag":
            return ok(repo.flag_todo(doc_id, line,
                                     str(payload.get("field") or ""),
                                     bool(payload.get("value"))))
        if action == "place":
            return ok(repo.place_todo(doc_id, line,
                                      bool(payload.get("important")),
                                      bool(payload.get("urgent"))))
        return ok(repo.set_todo(doc_id, line, bool(payload.get("checked"))))
    if action == "add":
        return ok(repo.add_todo(doc_id, payload.get("text")))
    raise ApiError("INVALID_PARAM", "action 只能是 toggle / flag / place / add")


@router.post("/folders")
def post_folder(payload: dict):
    return ok(repo.create_folder(payload.get("parent") or "", payload.get("name")))


@router.post("/folders/move")
def post_folder_move(payload: dict):
    return ok(repo.move_folder(payload.get("from") or "", payload.get("to") or ""))


@router.post("/folders/rename")
def post_folder_rename(payload: dict):
    return ok(repo.rename_folder(payload.get("path") or "", payload.get("name")))


@router.post("/folders/copy")
def post_folder_copy(payload: dict):
    return ok(repo.copy_folder(payload.get("from") or "", payload.get("to") or ""))


@router.post("/folders/delete")
def post_folder_delete(payload: dict):
    return ok(repo.delete_folder(payload.get("path") or ""))


@router.post("/tree/order")
def post_order(payload: dict):
    return ok(repo.set_order(payload.get("dir") or "", payload.get("order") or []))


@router.post("/docs/{doc_id}/move")
def post_doc_move(doc_id: str, payload: dict):
    doc = repo.read_doc(doc_id)
    if not doc:
        raise ApiError("INVALID_PARAM", "文档不存在")
    return ok(repo.rename_doc(doc_id, doc.get("title") or "未命名",
                              payload.get("dir")))


@router.post("/docs/{doc_id}/copy")
def post_doc_copy(doc_id: str, payload: dict):
    return ok(repo.copy_doc(doc_id, payload.get("dir")))


@router.post("/docs/{doc_id}/delete")
def post_doc_delete(doc_id: str):
    return ok(repo.delete_doc(doc_id))


@router.post("/trash/restore")
def post_trash_restore(payload: dict):
    return ok(repo.restore_from_trash(payload.get("name"), payload.get("to")))


@router.get("/todos")
def get_todos():
    items = repo.all_todos()
    return ok({"todos": items,
               "open": [t for t in items if not t["checked"]],
               "done": [t for t in items if t["checked"]]})


@router.get("/search")
def get_search(q: str = ""):
    return ok({"query": q, "results": index.search(q), "engine": index.engine()})
