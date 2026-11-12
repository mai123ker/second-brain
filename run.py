"""本地启动入口：http://127.0.0.1:8000（可用 MIRROR_PORT 覆盖，桌面壳也用这个口）。

开发时加 --reload：改代码自动重启，不用手动 Ctrl+C 再起。

为什么不用 uvicorn 自带的 --reload：
- 它在 Windows 上用 CTRL_C_EVENT 杀子进程重启，重启一次后 watchfiles 的监视就失效，
  第二次改动不再触发（实测：改 .py 生效，紧接着再改 .py 就没反应了）。
- 它的通配模式会用 cwd.glob() 展开监视目录，写成 "*" 会把 records/、.index/ 全拖进去，
  那样每保存一次文档就会把后端重启一次。

这里自己盯：只监视 src/ 与 prompts/，只有 .py 与 prompts/*.md 会触发重启，
改 records/ 里的文档永远不影响服务。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import uvicorn

from src.api.app import app
from src.storage.paths import ROOT

# 只盯这些：业务代码与提示词
WATCH_DIRS = [str(ROOT / "src"), str(ROOT / "prompts")]


def _is_watched(path: Path) -> bool:
    """只有业务代码与提示词的变化才值得重启。"""
    if path.suffix == ".py":
        return True
    return path.parent.name == "prompts" and path.suffix == ".md"


def _dev_loop(port: int) -> int:
    """开发模式：盯住 src/ 与 prompts/，一有改动就重启服务。

    服务本身跑在子进程里（普通模式），父进程只管盯着——这样每次重启后监视器都是全新的，
    不会像 uvicorn 那样"只灵一次"。
    """
    try:
        import watchfiles
    except ImportError:
        print("[dev] 没装 watchfiles，装一下才能用开发模式：pip install watchfiles")
        return 1

    cmd = [sys.executable, str(ROOT / "run.py"), "--port", str(port)]
    proc: subprocess.Popen | None = None
    try:
        while True:
            proc = subprocess.Popen(cmd, cwd=str(ROOT))
            print(f"[dev] 服务已启动（PID {proc.pid}）→ http://127.0.0.1:{port}", flush=True)
            print("[dev] 改 src/ 下的 .py 或 prompts/ 下的 .md 会自动重启；改文档不会。", flush=True)

            restart = False
            for changes in watchfiles.watch(*WATCH_DIRS,
                                            watch_filter=None,
                                            yield_on_timeout=True,
                                            rust_timeout=1000,
                                            ignore_permission_denied=True):
                if proc.poll() is not None:
                    print("[dev] 服务进程自己退出了，重新拉起。")
                    restart = True
                    break
                paths = {Path(c[1]) for c in changes}
                hit = sorted(p for p in paths if _is_watched(p))
                if not hit:
                    continue
                print("[dev] 检测到改动，重启服务：" + "、".join(p.name for p in hit), flush=True)
                restart = True
                break

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not restart:
                break
    except KeyboardInterrupt:
        print("\n[dev] 退出。")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="第二大脑 · 认知与行动 本地服务")
    parser.add_argument("--reload", action="store_true",
                        help="开发模式：改代码自动重启（只监视 src/ 与 prompts/，不动 records/）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MIRROR_PORT", "8000")),
                        help="监听端口，默认 8000")
    # 打包成 sidecar 后可能带额外参数，认不出就忽略，别让服务起不来
    args, _unknown = parser.parse_known_args()

    if args.reload:
        raise SystemExit(_dev_loop(args.port))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
