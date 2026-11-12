"""P1 统计只读接口：未探索提示 + 悬空概念。不产生任何写操作。"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import ok
from src.storage import index

router = APIRouter(tags=["stats"])


@router.get("/stats/coverage")
def get_coverage(days: int = 7):
    return ok(index.coverage(days))


@router.get("/stats/dangling")
def get_dangling():
    return ok({"concepts": index.dangling_concepts()})
