"""项目内所有路径的唯一口径。项目根 = 认知镜像P0/（vault 根）。

打包成桌面应用（PyInstaller sidecar）后：
- ROOT 跟着 exe 走 → 用户数据（records/ 索引/备份）在 exe 旁边；
- ASSETS 用 PyInstaller 解包目录 → web/ prompts/ 等只读资源打进 exe。"""
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller --onefile 下 __file__ 在临时解包目录
    ROOT = Path(sys.executable).resolve().parent
    ASSETS = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parents[2]
    ASSETS = ROOT

VAULT = ROOT / "records"                 # 用户内容根：整棵文档树，Obsidian 直接打开
LEGACY = VAULT / "_v0_fragments"        # 上一版碎片记录，不参与索引与展示
ARCHIVE_DIR = VAULT / "archive"

TOPIC_DIRS = {
    "目标": VAULT / "目标",
    "人生阶段总结": VAULT / "人生阶段总结",
    "思考": VAULT / "思考",
    "学习": VAULT / "学习",
    "外部素材": VAULT / "外部素材",
    "待归位": VAULT / "待归位",
}
GOALS = TOPIC_DIRS["目标"]
EXTERNAL = TOPIC_DIRS["外部素材"]
PENDING = TOPIC_DIRS["待归位"]

RECORDS = ROOT / "records"
BACKUP = ROOT / ".backup"                # 每篇文档的历史快照，自动轮转
ARCHIVE = VAULT / "_archive"             # 归档文档，不参与树与索引
TRASH = VAULT / "_trash"                 # 删除的文档/文件夹，可手动找回
INDEX_DB = ROOT / ".index" / "index.db"
PROMPTS = ASSETS / "prompts"
WEB = ASSETS / "web"
SEED = ASSETS / "seed"

SELF_DIR = VAULT                         # 兼容别名
EXTERNAL_DIR = EXTERNAL


def scan_documents() -> list[Path]:
    """递归扫描整棵文档树；跳过上一版碎片、archive 与隐藏目录。"""
    VAULT.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(VAULT.rglob("*.md")):
        parts = path.relative_to(VAULT).parts
        if any(p.startswith(".") or p.startswith("_") or p == "archive" for p in parts[:-1]):
            continue
        out.append(path)
    return out


def rel(path: Path | str) -> str:
    p = Path(path)
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()
