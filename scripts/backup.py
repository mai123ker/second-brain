"""把 records/、cards/ 与 vault/ 打成 backup/YYYYMMDD-HHMMSS.zip。

用法：
    python scripts/backup.py            # 手动打一份
    python scripts/backup.py --list     # 列出已有备份

也可以被应用导入：backup_job 会每天自动调用 backup_now()，/backups 提供手动触发。
只读打包，不改动任何原文件。对话（_chat）与附件（_assets）一起备份；
vault/ 是 V2 的知识库与对话，必须一起进包，否则 V2 侧没有可恢复的历史。
"""
from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [ROOT / "records", ROOT / "cards", ROOT / "vault"]
OUTPUT = ROOT / "backup"
STATE = ROOT / ".backup_state.json"
SKIP_DIRS = {"_trash", "_v0_fragments"}
KEEP = 14


def _include(path: Path, folder: Path) -> bool:
    parts = path.relative_to(folder).parts
    dirs = parts[:-1]
    if "__pycache__" in dirs:
        return False
    return not any(part.startswith(".") or part in SKIP_DIRS for part in dirs)


def list_backups() -> list[dict]:
    if not OUTPUT.exists():
        return []
    out = []
    for path in sorted(OUTPUT.glob("*.zip"), reverse=True):
        stat = path.stat()
        out.append({"name": path.name, "size": stat.st_size,
                    "at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return out[:KEEP]


def last_state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def should_run(hours: int = 24) -> bool:
    state = last_state()
    stamp = str(state.get("last") or "")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now() - last).total_seconds() >= hours * 3600


def prune(keep: int = KEEP) -> int:
    files = sorted(OUTPUT.glob("*.zip"), reverse=True)
    removed = 0
    for path in files[keep:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def backup_now(reason: str = "manual") -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = OUTPUT / f"{stamp}.zip"
    if archive.exists():
        archive = OUTPUT / f"{stamp}-{datetime.now().microsecond:06d}.zip"
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in TARGETS:
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file() and _include(path, folder):
                    zf.write(path, path.relative_to(ROOT).as_posix())
                    count += 1
    now = datetime.now().isoformat(timespec="seconds")
    state = {"last": now, "reason": reason, "file": archive.name, "files": count}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    removed = prune()
    return {"file": archive.name, "files": count, "size": archive.stat().st_size,
            "at": now, "reason": reason, "pruned": removed,
            "path": archive.relative_to(ROOT).as_posix()}


def main() -> int:
    if "--list" in sys.argv:
        for item in list_backups():
            print(f"{item['at']}  {item['size']:>9} B  {item['name']}")
        return 0
    result = backup_now("manual")
    print(f"packed {result['files']} files -> {result['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
