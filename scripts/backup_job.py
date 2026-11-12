"""每日自动备份：应用启动时起一个后台守护线程。

- 距上次备份超过 24 小时就打一份；启动时若从没备份过，立即打一份。
- 每小时检查一次；备份失败只打印，不影响主应用。
- 只写 backup/ 与 .backup_state.json，不动用户原文。
"""
from __future__ import annotations

import os
import threading
import time

from scripts.backup import backup_now, should_run

_thread: threading.Thread | None = None
CHECK_SECONDS = 3600


def _loop() -> None:
    # 启动后先给主应用一点时间完成灌种子 / 重建索引
    time.sleep(3)
    while True:
        try:
            if should_run():
                result = backup_now("auto")
                print(f"[backup] auto: {result['file']} ({result['files']} files)")
        except Exception as exc:  # 备份失败不能拖垮应用
            print(f"[backup] failed: {exc}")
        time.sleep(CHECK_SECONDS)


def start() -> bool:
    global _thread
    # 验收 / 降级实例不参与定时备份，只有主服务写 backup/
    if os.environ.get("MIRROR_DISABLE_AUTO_BACKUP", "").strip() == "1":
        return False
    if _thread and _thread.is_alive():
        return False
    _thread = threading.Thread(target=_loop, daemon=True, name="mirror-auto-backup")
    _thread.start()
    return True
