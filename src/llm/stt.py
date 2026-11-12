"""语音转文字（BYOK）：录音发给用户自己配置的 OpenAI 兼容转写接口。

- 配置只存本机 config.json 的 "stt" 节：{"base_url": ".../v1", "api_key": "...", "model": "..."}，
  环境变量 MIRROR_STT_BASE_URL / MIRROR_STT_API_KEY / MIRROR_STT_MODEL 可覆盖。
- 音频只发往用户自己填的接口，不发任何第三家；不落盘、不存档。
- 三态诚实降级：没配/失败都如实返回，绝不假装转出来。
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
import uuid

from src.llm import settings

TIMEOUT = 45


class STTUnavailable(Exception):
    pass


def stt_config() -> dict:
    cfg = settings.load()
    section = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
    return {
        "base_url": (os.environ.get("MIRROR_STT_BASE_URL", "").strip()
                     or str(section.get("base_url") or "").strip()).rstrip("/"),
        "api_key": (os.environ.get("MIRROR_STT_API_KEY", "").strip()
                    or str(section.get("api_key") or "").strip()),
        "model": (os.environ.get("MIRROR_STT_MODEL", "").strip()
                  or str(section.get("model") or "whisper-1").strip()),
    }


def availability() -> dict:
    mock = os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock"
    cfg = stt_config()
    ready = bool(cfg["base_url"]) and bool(cfg["api_key"]) and bool(cfg["model"])
    return {"available": bool(ready) or mock, "model": cfg["model"]}


def _multipart(field_file: str, filename: str, mime: str, blob: bytes,
               fields: dict) -> tuple[bytes, str]:
    boundary = "----mirrorform" + uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_file}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n")
    body = "".join(lines).encode("utf-8") + blob + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, boundary


def transcribe(data_url: str) -> str:
    """data URL 进，纯文本出。任何失败都收敛成 STTUnavailable，由路由如实报告。"""
    match = re.match(r"^data:([^;,]+)(?:;[^,]*)?,(.*)$", str(data_url or ""), re.S)
    if not match:
        raise STTUnavailable("音频数据格式不对（期待 data URL）")
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
        return "（演示模式占位）这是 mock 转写出来的文字，检查格式后直接发送。"
    cfg = stt_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise STTUnavailable(
            "还没配转写接口：config.json 加 \"stt\" 节（base_url / api_key / model，"
            "OpenAI 兼容的 /audio/transcriptions），改完重启生效")
    mime, b64 = match.group(1), match.group(2)
    try:
        blob = base64.b64decode(b64)
    except Exception as exc:
        raise STTUnavailable(f"音频解码失败：{exc}") from exc
    if len(blob) < 800:
        raise STTUnavailable("录音太短，没听到有效内容")
    if len(blob) > 25 * 1024 * 1024:
        raise STTUnavailable("录音超过 25MB，说短一点再试")
    ext = ".webm" if "webm" in mime else (".mp4" if "mp4" in mime else ".wav")
    body, boundary = _multipart("file", f"speech{ext}", mime or "audio/webm", blob,
                                {"model": cfg["model"]})
    request = urllib.request.Request(
        cfg["base_url"] + "/audio/transcriptions", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        detail = ""
        status = getattr(exc, "code", None)
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
        raise STTUnavailable(_friendly(status, detail, exc)) from exc
    text = str((data or {}).get("text") or "").strip()
    if not text:
        raise STTUnavailable("转写接口返回了空文本（可能没听清，靠近点麦克风再试）")
    return text


def _friendly(status, detail: str, exc: Exception) -> str:
    """把接口的原始错误翻成人能直接照做的下一步，不把 JSON 甩给用户。"""
    low = (detail or "").lower()
    if status == 401 or "unauthorized" in low or "invalid api key" in low:
        return ("转写接口鉴权没通过：config.json 里的 stt.api_key 不对或已失效。"
                "去服务商控制台重新复制 Key 填上，重启后端再试。")
    if status == 402 or "payment required" in low or "balance" in low or "余额" in low:
        return ("转写接口账户余额不足（HTTP 402）。Key 是好的，是账户没额度了——"
                "去服务商控制台充值或领取免费赠金后重试；"
                "或者换 Edge 浏览器，它自带语音识别、不走这个接口。")
    if status == 404:
        return ("转写接口地址不对：config.json 的 stt.base_url 后面接 /audio/transcriptions，"
                "确认是不是少写了 /v1 或写错了服务商地址。")
    if status == 429:
        return "转写接口被限流了，等一会儿再试。"
    suffix = f" · {detail}" if detail else ""
    return f"转写接口没成上：{exc}{suffix}"
