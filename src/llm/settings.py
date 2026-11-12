"""本机模型配置（BYOK）。

- 只存在本机 `config.json`，不上传、不外发。
- 接口永远不回传 api_key，只回 has_key。
- 环境变量（MIRROR_LLM_API_KEY / MODEL / BASE_URL）优先级高于这里的模型。
"""
from __future__ import annotations

import json
import os
import re

from src.storage.paths import ROOT

CONFIG_PATH = ROOT / "config.json"
DEFAULT_BASE = "https://api.openai.com/v1"

TEMPLATE = {
    "active": "",
    "models": [
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": "",
        },
        {
            "id": "openai",
            "label": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "",
        },
        {
            "id": "qwen",
            "label": "通义千问",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
            "api_key": "",
        },
    ],
}


def _slug(text: str) -> str:
    clean = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "-", str(text or "").strip().lower()).strip("-")
    return clean or "model"


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {"active": "", "models": []}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": "", "models": []}
    if not isinstance(data, dict):
        return {"active": "", "models": []}
    data.setdefault("active", "")
    if not isinstance(data.get("models"), list):
        data["models"] = []
    return data


def save(data: dict) -> dict:
    payload = {
        "active": str(data.get("active") or ""),
        "models": [
            {
                "id": str(item.get("id") or _slug(item.get("label") or item.get("model"))),
                "label": str(item.get("label") or item.get("model") or "未命名"),
                "base_url": str(item.get("base_url") or DEFAULT_BASE).rstrip("/"),
                "model": str(item.get("model") or ""),
                "api_key": str(item.get("api_key") or ""),
            }
            for item in (data.get("models") or [])
            if str(item.get("model") or "").strip()
        ],
    }
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    return payload


def ensure_file() -> None:
    """首次运行时写一份带空 key 的模板，方便用户直接填。"""
    if CONFIG_PATH.exists():
        return
    save({"active": "", "models": TEMPLATE["models"]})


def public(data: dict | None = None) -> dict:
    cfg = data or load()
    models = [{
        "id": item.get("id"),
        "label": item.get("label"),
        "base_url": item.get("base_url"),
        "model": item.get("model"),
        "has_key": bool(str(item.get("api_key") or "").strip()),
    } for item in cfg.get("models", [])]
    return {"active": cfg.get("active") or "", "models": models}


def active_model(data: dict | None = None) -> dict | None:
    cfg = data or load()
    for item in cfg.get("models", []):
        if item.get("id") == cfg.get("active") and str(item.get("api_key") or "").strip():
            return item
    return None


def add_model(payload: dict) -> dict:
    cfg = load()
    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("模型名不能为空")
    entry = {
        "id": _slug(payload.get("id") or payload.get("label") or model),
        "label": str(payload.get("label") or model).strip(),
        "base_url": str(payload.get("base_url") or DEFAULT_BASE).strip().rstrip("/"),
        "model": model,
        "api_key": str(payload.get("api_key") or "").strip(),
    }
    models = [item for item in cfg["models"] if item.get("id") != entry["id"]]
    models.append(entry)
    cfg["models"] = models
    if not cfg.get("active"):
        cfg["active"] = entry["id"]
    return save(cfg)


def remove_model(model_id: str) -> dict:
    cfg = load()
    cfg["models"] = [item for item in cfg["models"] if item.get("id") != model_id]
    if cfg.get("active") == model_id:
        cfg["active"] = cfg["models"][0]["id"] if cfg["models"] else ""
    return save(cfg)


def set_active(model_id: str) -> dict:
    cfg = load()
    if any(item.get("id") == model_id for item in cfg["models"]):
        cfg["active"] = model_id
    return save(cfg)


def env_model() -> dict | None:
    key = os.environ.get("MIRROR_LLM_API_KEY", "").strip()
    model = os.environ.get("MIRROR_LLM_MODEL", "").strip()
    if not key or not model:
        return None
    return {
        "id": "env",
        "label": f"{model}（环境变量）",
        "base_url": os.environ.get("MIRROR_LLM_BASE_URL", DEFAULT_BASE).strip().rstrip("/"),
        "model": model,
        "api_key": key,
    }


def credentials() -> dict | None:
    """真正用来发请求的凭据：环境变量优先，其次 config.json 里选中的模型。

    MIRROR_LLM_MODE=off 强制视为未配置（验收的降级实例用）：
    即使 config.json 里已有真 Key，也如实返回 LLM_UNAVAILABLE。
    """
    if os.environ.get("MIRROR_LLM_MODE", "").strip().lower() in ("off", "none"):
        return None
    return env_model() or active_model()
