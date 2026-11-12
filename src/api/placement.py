"""待归位：只改 frontmatter 的 target / status，不碰正文与不可变字段。"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import ok
from src.storage import repo

router = APIRouter(tags=["placement"])


@router.get("/records")
def get_records(status: str | None = None, stream: str | None = None):
    return ok({"records": repo.list_docs(status=status, stream=stream)})


@router.post("/records/{record_id}/place")
def place(record_id: str, payload: dict):
    result = repo.place(record_id, str(payload.get("action") or ""), payload)
    record = result.pop("record", {})
    result["file"] = record.get("path", "")
    return ok(result)
