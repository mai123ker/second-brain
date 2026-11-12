"""粘贴 / 拖入的图片落到 records/_assets/，由 /assets 静态路由供图。

只接受 data:image/...;base64，按内容哈希命名（同一张图重复粘贴只存一份）。
"""
from __future__ import annotations

import base64
import hashlib
import re

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.storage.paths import ROOT, VAULT

ASSET_DIR = VAULT / "_assets"
MAX_BYTES = 8 * 1024 * 1024
MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
DATA_URL = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)

router = APIRouter(tags=["assets"])


class AssetIn(BaseModel):
    data: str = ""
    name: str = ""


@router.post("/assets/upload")
def post_asset(payload: AssetIn):
    match = DATA_URL.match(str(payload.data or "").strip())
    if not match:
        raise ApiError("INVALID_PARAM", "只接受粘贴或拖入的图片")
    mime = match.group(1).lower()
    ext = MIME_EXT.get(mime)
    if not ext:
        raise ApiError("INVALID_PARAM", f"不支持的图片格式：{mime}")
    try:
        blob = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ApiError("INVALID_PARAM", "图片内容不是合法 base64") from exc
    if not blob or len(blob) > MAX_BYTES:
        raise ApiError("INVALID_PARAM", "图片为空或超过 8 MB")
    digest = hashlib.sha256(blob).hexdigest()[:20]
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{digest}.{ext}"
    target = ASSET_DIR / filename
    if not target.exists():
        tmp = ASSET_DIR / (filename + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(target)
    return ok({"url": "/assets/" + filename, "name": filename,
               "size": len(blob), "path": target.relative_to(ROOT).as_posix()})
