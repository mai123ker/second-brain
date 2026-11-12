"""目标相关只读接口。归位面板与覆盖统计用；对照已移除（全集 V1.2）。"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import ok
from src.storage import repo

router = APIRouter(tags=["goals"])


@router.get("/goals")
def get_goals():
    return ok({"goals": repo.list_goals()})
