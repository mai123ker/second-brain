"""统一错误码与响应包装。依据 docs/接口契约.md 错误码总表。"""
from __future__ import annotations

from fastapi.responses import JSONResponse

ERROR_STATUS = {
    "INVALID_PARAM": 400,
    "EXTERNAL_FIELD_MISSING": 400,
    "INSUFFICIENT_MATERIAL": 409,
    "CONFLICT": 409,
    "EMPTY_THOUGHT": 400,
    "LLM_UNAVAILABLE": 503,
    "STORAGE_ERROR": 500,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def error_response(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def ok(data: dict) -> dict:
    return {"ok": True, "data": data}


async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=ERROR_STATUS.get(exc.code, 400),
        content=error_response(exc.code, exc.message),
    )
