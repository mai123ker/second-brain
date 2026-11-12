"""语音转文字路由：录音（data URL）进、纯文本出，只转写、不落盘、不入对话记录。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.llm import stt

router = APIRouter(tags=["speech"])


class STTIn(BaseModel):
    data: str = ""


@router.post("/stt")
def post_stt(payload: STTIn):
    try:
        text = stt.transcribe(payload.data)
    except stt.STTUnavailable as exc:
        raise ApiError("STT_UNAVAILABLE", str(exc)) from exc
    return ok({"text": text})
