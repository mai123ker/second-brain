"""拖拽文件入库：POST /ingest/file —— 一个文件一篇 .md，落到 外部素材/。

口径（用户 2026-11-11 拍板）：
- 落库 + 自动附加进对话：原文不可变，整理结果由用户点按钮写进目标文档。
- stance 诚实标 unverified（未核实）：系统不替用户判定外部材料的立场。
- 文本 UTF-8/GBK 解码；PDF 用 pypdf 提取文字（扫描版提取不到就诚实报错）；
  .docx 是 zip + XML，标准库解段落和表格文字（.doc 老格式不支持）；
  图片存 records/_assets/（AI 看不了像素内容，只存档）；其余类型明确拒绝。
- 正文上限 2 万字（与 create_doc 一致），超长截断并显式注明，不静默丢内容。
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import ApiError, ok
from src.storage.repo import create_doc
from src.storage.paths import VAULT

ASSET_DIR = VAULT / "_assets"
MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 20000
DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.+)$", re.S)
MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp", "image/bmp": "bmp"}
TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}

router = APIRouter(tags=["ingest"])


class IngestIn(BaseModel):
    name: str = ""
    data: str = ""


def _title_stem(name: str) -> str:
    stem = Path(str(name or "未命名").strip()).stem.strip() or "未命名"
    return stem[:60]


def _decode_text(blob: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ApiError("UNSUPPORTED_FILE", "无法识别文件编码（只收 UTF-8 / GBK 文本）")


def _pdf_text(blob: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ApiError("UNSUPPORTED_FILE", "服务端缺少 pypdf，无法解析 PDF") from exc
    import io
    try:
        reader = PdfReader(io.BytesIO(blob))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        raise ApiError("UNSUPPORTED_FILE", f"这个 PDF 解析失败：{exc}") from exc
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        raise ApiError("UNSUPPORTED_FILE", "扫描版/纯图 PDF 提取不到文字，先转成文本再拖进来")
    return text


def _docx_text(blob: bytes) -> str:
    """提取 .docx 的段落与表格文字（标准库 zipfile + ElementTree，不加依赖）。"""
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def para_text(para: ET.Element) -> str:
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{NS}t" and node.text:
                parts.append(node.text)
            elif node.tag in (f"{NS}br", f"{NS}cr"):
                parts.append("\n")
            elif node.tag == f"{NS}tab":
                parts.append(" ")
        return "".join(parts)

    def cell_text(cell: ET.Element) -> str:
        lines = [para_text(p) for p in cell.findall(f"{NS}p")]
        return " ".join(line for line in lines if line)

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
    except zipfile.BadZipFile as exc:
        raise ApiError("UNSUPPORTED_FILE", "这不是有效的 .docx 文件（老 .doc 二进制格式不支持，先另存为 .docx）") from exc
    except KeyError as exc:
        raise ApiError("UNSUPPORTED_FILE", "这个 .docx 缺少正文（word/document.xml），没法提取") from exc
    except ET.ParseError as exc:
        raise ApiError("UNSUPPORTED_FILE", "这个 .docx 正文 XML 解析失败") from exc

    lines: list[str] = []
    body = root.find(f"{NS}body")
    if body is None:
        raise ApiError("UNSUPPORTED_FILE", "这个 .docx 没有正文内容")
    for child in body:
        if child.tag == f"{NS}p":
            text = para_text(child)
            lines.append(text.strip())
        elif child.tag == f"{NS}tbl":
            for row in child.findall(f"{NS}tr"):
                cells = [cell_text(c).strip() for c in row.findall(f"{NS}tc")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    text = "\n".join(lines).replace("\n\n\n", "\n\n").strip()
    if not text:
        raise ApiError("UNSUPPORTED_FILE", "这个 .docx 提取不到文字（可能是纯图片内容），先转成文本再拖进来")
    return text


def _save_image(mime: str, blob: bytes, name: str) -> str:
    ext = MIME_EXT.get(mime)
    if not ext:
        raise ApiError("UNSUPPORTED_FILE", f"不支持的图片格式：{mime}")
    digest = hashlib.sha256(blob).hexdigest()[:20]
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    target = ASSET_DIR / f"{digest}.{ext}"
    if not target.exists():
        tmp = ASSET_DIR / (target.name + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(target)
    alt = _title_stem(name)
    return f"![{alt}](/assets/{target.name})"


@router.post("/ingest/file")
def post_ingest_file(payload: IngestIn):
    name = str(payload.name or "").strip()
    if not name:
        raise ApiError("INVALID_PARAM", "缺少文件名")
    match = DATA_URL.match(str(payload.data or "").strip())
    if not match:
        raise ApiError("INVALID_PARAM", "文件内容必须是 data URL")
    blob = base64.b64decode(match.group(2), validate=True)
    if not blob:
        raise ApiError("INVALID_PARAM", "文件是空的")
    if len(blob) > MAX_BYTES:
        raise ApiError("INVALID_PARAM", f"文件超过 {MAX_BYTES // 1024 // 1024} MB，先压缩或拆分")
    mime = match.group(1).lower()
    ext = Path(name).suffix.lower()

    truncated = False
    if mime.startswith("image/"):
        body = _save_image(mime, blob, name)
        kind = "image"
        chars = len(body)
    elif ext in TEXT_EXTS or mime.startswith("text/"):
        body = _decode_text(blob).replace("\r\n", "\n").strip()
        kind = "text"
        chars = len(body)
    elif ext == ".pdf" or mime == "application/pdf":
        body = _pdf_text(blob)
        kind = "pdf"
        chars = len(body)
    elif ext == ".docx" or mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",):
        body = _docx_text(blob)
        kind = "docx"
        chars = len(body)
    else:
        raise ApiError("UNSUPPORTED_FILE",
                       "还没法解析这种文件（支持 .md/.txt/.csv 文本、PDF、.docx、图片）")

    if kind != "image" and len(body) > MAX_CHARS:
        body = body[:MAX_CHARS - 60].rstrip() + "\n\n> 原文超过 2 万字，这里只收录前 2 万字。"
        truncated = True

    result = create_doc({
        "title": _title_stem(name),
        "content": body,
        "stream": "external",
        "kind": "article",
        "source": name,
        "stance": "unverified",
        "scope": "未标注（拖拽原文）",
    })
    return ok({**result, "kind": kind, "chars": chars, "truncated": truncated})
