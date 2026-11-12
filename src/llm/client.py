"""OpenAI 兼容模型调用（BYOK）。

密钥只从本机环境变量读取，绝不写进文件、绝不上报。
除这里发起的模型 API 外，程序不发起任何对外网络请求。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from src.llm import settings
from src.storage.paths import PROMPTS

ENV_BASE = "MIRROR_LLM_BASE_URL"
ENV_KEY = "MIRROR_LLM_API_KEY"
ENV_MODEL = "MIRROR_LLM_MODEL"


class LLMUnavailable(Exception):
    pass


def load_chat_prompt() -> str:
    """对话面板的提示词同样只从文件读，代码里不内联第二份。"""
    path = PROMPTS / "chat_system.md"
    if not path.exists():
        raise LLMUnavailable("缺少 prompts/chat_system.md")
    return path.read_text(encoding="utf-8")


def configured() -> bool:
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
        return True
    return bool(settings.credentials())


def _credentials() -> dict:
    creds = settings.credentials()
    if not creds:
        raise LLMUnavailable("还没配置模型：填入 Base URL / 模型名 / API Key 即可，改完立刻生效")
    return creds


def _post(payload: dict) -> dict:
    """统一发一次 /chat/completions，任何异常都收敛成 LLMUnavailable。"""
    creds = _credentials()
    payload = dict(payload, model=creds["model"])
    request = urllib.request.Request(
        creds["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {creds['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise LLMUnavailable(str(exc)) from exc


def _mock_chat(messages: list[dict]) -> str:
    """演示模式下的对话回复：把话还给你；被要求补充时才给一段补丁。"""
    last = ""
    for item in reversed(messages):
        if item.get("role") == "user":
            last = str(item.get("content") or "")
            break
    head = re.sub(r"\s+", " ", last).strip()[:24]
    if not head:
        return "（演示模式·未接模型）这篇文档里你最想确认的是哪一句？"
    reply = f"（演示模式·未接模型）你说的是「{head}」。这句背后你更想保住哪一头？"
    if re.search(r"补充|续写|改写|写进|加一段|补一段|展开", last):
        reply += ("\n<<<PATCH>>>\n## 待验证\n\n"
                  f"- [ ] 把「{head}」落成一个具体动作，写清时间和能拿出的证据。\n"
                  "<<<END>>>")
    elif re.search(r"记一下|记住|帮我记|存一下|归档|怕忘", last):
        reply += ("\n（已建议沉淀。点下面按钮就落进库。）\n<<<REHOME>>>新建<<<END>>>")
    return reply


def probe(creds: dict) -> str:
    """用指定凭据发一次极短的请求，验证 Base URL / 模型名 / Key 是否可用。"""
    if not creds or not creds.get("api_key") or not creds.get("model"):
        raise LLMUnavailable("缺少 API Key 或模型名")
    payload = {
        "model": creds["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    request = urllib.request.Request(
        str(creds.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {creds['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        _ = data["choices"][0]["message"]["content"]
        return "连接成功"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:160]
        except Exception:
            pass
        friendly = {
            401: "API Key 无效或已过期（401）",
            403: "没有调用权限（403）",
            404: "Base URL 或模型名不对（404）",
            429: "请求太频繁或额度用完（429）",
        }.get(exc.code)
        if friendly:
            raise LLMUnavailable(friendly + (f" · {detail}" if detail and exc.code not in (401,) else "")) from exc
        raise LLMUnavailable(f"HTTP {exc.code}：{detail or '请求被拒绝'}") from exc
    except Exception as exc:
        raise LLMUnavailable(str(exc)) from exc


def chat_text(system: str, messages: list[dict]) -> str:
    """多轮纯文本对话，不要求 JSON。未配置模型时抛 LLMUnavailable，让前端说实话。"""
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
        return _mock_chat(messages)
    body = _post({
        "messages": [{"role": "system", "content": system}] + messages[-20:],
        "temperature": 0.3,
    })
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable("模型返回格式不对") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMUnavailable("模型返回为空")
    return content.strip()


def stream_chat(system: str, messages: list[dict]):
    """逐块产出回答文本。真实模式请求 stream:true 并解析 SSE data: 行。

    只 yield 纯文本增量；上游的补丁切分 / 落盘由 api/chat.py 统一处理，
    这样流式与非流式两条链路的行为完全一致。
    """
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() == "mock":
        text = _mock_chat(messages)
        for start in range(0, len(text), 12):
            yield text[start:start + 12]
            time.sleep(0.03)
        return
    creds = _credentials()
    payload = {
        "model": creds["model"],
        "messages": [{"role": "system", "content": system}] + messages[-20:],
        "temperature": 0.3,
        "stream": True,
    }
    request = urllib.request.Request(
        str(creds["base_url"]).rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {creds['api_key']}",
                 "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                try:
                    delta = obj["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, TypeError, AttributeError):
                    delta = ""
                if delta:
                    yield delta
    except Exception as exc:
        raise LLMUnavailable(str(exc)) from exc
