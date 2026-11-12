"""FastAPI 应用：启动钩子灌种子 + 重建索引 + 拉起每日自动备份；前端挂在根路径。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from scripts import backup_job
from seed.generate_seed import generate
from src.api import (assets, backups, chat, documents, goals, ingest,
                     placement, proactive, records, speech, stats, v2)
from src.api.errors import ApiError, api_error_handler
from src.llm import settings
from src.storage import index
from src.storage.paths import WEB

# StaticFiles 在挂载时就要目录存在，不能等 lifespan
assets.ASSET_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_file()
    generate()
    index.rebuild()
    assets.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    backup_job.start()
    yield


app = FastAPI(title="第二大脑 · 认知与行动", version="2.0.0", lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)
app.include_router(records.router)
app.include_router(ingest.router)
app.include_router(goals.router)
app.include_router(placement.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(proactive.router)
app.include_router(speech.router)
app.include_router(assets.router)
app.include_router(backups.router)
app.include_router(stats.router)
app.include_router(v2.router)

# 根路径 `/` 是旧版文档界面（web/index.html，用户常用形态）。
# 新的「认知库」界面放在 /new（web/v2.html），数据在 vault/，与旧文档库各存各的。
# /_legacy 保留跳转，旧书签不失效。
NEW_PAGE = WEB / "v2.html"


@app.get("/_legacy", include_in_schema=False)
@app.get("/_legacy/{rest:path}", include_in_schema=False)
async def legacy_redirect(rest: str = ""):
    return RedirectResponse("/", status_code=302)


@app.get("/new", include_in_schema=False)
@app.get("/new/", include_in_schema=False)
async def v2_page():
    if not NEW_PAGE.is_file():
        raise HTTPException(status_code=404, detail="新版页面缺失")
    return FileResponse(NEW_PAGE)


# 静态资源必须在根挂载之前；GET /assets/<name> 走文件，POST /assets/upload 走接口
app.mount("/assets", StaticFiles(directory=str(assets.ASSET_DIR)), name="assets")
app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
