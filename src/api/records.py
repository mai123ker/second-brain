"""POST /records —— 新建一篇长文 .md 文档（新形态下 /records 的语义）。"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import ok
from src.storage.repo import create_doc

router = APIRouter(tags=["records"])


@router.post("/records")
def post_records(payload: dict):
    return ok(create_doc(payload))
