"""备份状态与手动触发：GET /backups、POST /backups/run。"""
from __future__ import annotations

from fastapi import APIRouter

from scripts import backup as backup_mod
from src.api.errors import ok

router = APIRouter(tags=["backups"])


@router.get("/backups")
def get_backups():
    return ok({"backups": backup_mod.list_backups(),
               "last": backup_mod.last_state(),
               "auto": True, "interval_hours": 24, "keep": backup_mod.KEEP})


@router.post("/backups/run")
def post_backup():
    return ok(backup_mod.backup_now("manual"))
