"""V2 端到端验收：投喂 → 提炼 → 增量合并 → 对话 → 处境 → 待办 → 激励 → 回补 → 相似召回。

用法：先启动服务（建议 MIRROR_LLM_MODE=mock 走确定性规则版），再 python tests/verify_v2.py
结束时自动还原 vault/ 到运行前状态，不留下测试知识、对话、待办或原文。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
_override = os.environ.get("MIRROR_V2_VAULT", "").strip()
VAULT = Path(_override) if _override else ROOT / "vault"
RECORDS = ROOT / "records"
BASE = os.environ.get("MIRROR_V2_BASE", "http://127.0.0.1:8000")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RESULTS: list[tuple[str, bool, str]] = []


def call(path: str, method: str = "GET", payload=None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + quote(path, safe="/?&=%:+,~"), data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def api(path: str, method: str = "GET", payload=None) -> dict:
    result = call(path, method, payload)
    if not result.get("ok"):
        raise AssertionError(f"{path} 返回错误：{result.get('error')}")
    return result["data"]


def check(name: str, condition, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok, str(detail)[:200]))
    print(("[ OK ] " if ok else "[FAIL] ") + name + (("  · " + str(detail)[:160]) if detail else ""))


def snapshot(base: Path) -> dict:
    if not base.exists():
        return {}
    return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}


def fingerprint(base: Path) -> dict:
    if not base.exists():
        return {}
    return {str(p.relative_to(base)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in base.rglob("*") if p.is_file()}


def cleanup(original: dict) -> None:
    for path in sorted(VAULT.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    VAULT.mkdir(parents=True, exist_ok=True)
    for name, content in original.items():
        target = VAULT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def data_url(mime: str, blob: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")


def make_docx(text: str) -> bytes:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>' + safe + '</w:t></w:r></w:p></w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def make_pdf(text: str) -> bytes:
    stream = ("BT /F1 12 Tf 72 720 Td (" + text + ") Tj ET").encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


CHATTY = (
    "嗯，那个，就是说，其实吧我觉得吧制度这个东西太气了，"
    "因为激励约束没对齐，所以流程会变形。"
    "因为激励约束没对齐，所以流程会变形。烦死了。"
    "另外用户增长如果只看拉新，不看留存，就会把预算耗在低质量渠道上。"
)


def main() -> int:
    original = snapshot(VAULT)
    cleanup({})  # 隔离目录里强制从空库起跑，重复运行不受上次残留影响
    records_before = fingerprint(RECORDS)
    chat_before = fingerprint(ROOT / "records" / "_chat")
    try:
        # A. 服务与形态
        state = api("/v2/state")
        check("A1 服务可访问且初始数据为空",
              all(state["counts"][key] == 0 for key in
                  ("knowledge", "sources", "todos_open", "chat", "situations")),
              state["counts"])
        check("A2 模型状态如实标注（演示模式不冒充真实模型）",
              state["model"]["mode"] == "mock" and not state["model"]["configured"], state["model"])
        with OPENER.open(BASE + "/", timeout=20) as response:
            home = response.read().decode("utf-8")
        check("A3 首页是旧版文档界面（用户常用形态）",
              "/app.js" in home and "/v2.js" not in home and "doc-body" in home)
        with OPENER.open(BASE + "/new", timeout=20) as response:
            newpage = response.read().decode("utf-8")
        check("A4 新版认知库挂在 /new，可打开且带返回旧版入口",
              "第二大脑 · 认知与行动" in newpage and "/v2.js" in newpage
              and "legacy-btn" in newpage)
        check("A5 旧 P0 接口未被破坏", call("/docs/tree").get("ok") is True)

        # B. 投喂与提炼
        sources_before = {p.name for p in (VAULT / "sources").glob("*.md")}
        first = api("/v2/ingest/text", "POST", {"text": CHATTY, "title": "制度随手记", "kind": "text"})
        check("B1 文本投喂返回摘要与变更", bool(first["summary"]) and len(first["changes"]) >= 1,
              first["summary"][:80])
        check("B2 整理过程如实报告去口语 / 去情绪 / 去重",
              first["normalized"]["removed_filler"] >= 1
              and first["normalized"]["removed_emotion"] >= 1
              and first["normalized"]["removed_duplicate"] >= 1,
              first["normalized"])
        bodies = "\n".join(p.read_text(encoding="utf-8")
                           for p in (VAULT / "knowledge").glob("*.md"))
        check("B3 知识库里没有口语 / 情绪原文",
              all(word not in bodies for word in ("嗯，", "那个", "其实吧", "我觉得吧", "烦死了", "太气了")))
        new_sources = [p for p in (VAULT / "sources").glob("*.md")
                       if p.name not in sources_before]
        check("B4 原文单独归档且标注不可变",
              len(new_sources) == 1 and "immutable: true" in new_sources[0].read_text(encoding="utf-8"),
              [p.name for p in new_sources])
        first_source_bytes = new_sources[0].read_bytes()
        sources_after_first = new_sources

        second = api("/v2/ingest/text", "POST", {"text": CHATTY, "title": "同一段再说一遍", "kind": "text"})
        check("B5 重复投喂走增量合并，不新建重复条目",
              any(c["action"] == "merged" for c in second["changes"]),
              [(c["action"], c.get("title")) for c in second["changes"]])
        merged_id = next(c["id"] for c in second["changes"] if c["action"] == "merged")
        detail = api("/v2/knowledge/" + merged_id)
        check("B6 合并条目保留多个来源标识", len(detail["item"]["sources"]) >= 2,
              detail["item"]["sources"])
        check("B7 合并没有改写原有陈述", "## 陈述" in detail["item"]["body"])
        check("B8 原文归档未被改写（不可变）", sources_after_first[0].read_bytes() == first_source_bytes)

        created = [c for c in first["changes"] if c["action"] == "created"]
        check("B9 框架与知识点都有归类",
              any(c["type"] == "frame" for c in created) or api("/v2/knowledge?type=frame")["items"],
              [(c["action"], c["type"], c["domain"]) for c in created])

        # C. 文件投喂
        note = "制度设计里，考核指标决定了基层会优化什么，而不是口号决定了什么。"
        txt = api("/v2/ingest/file", "POST",
                  {"name": "制度笔记.txt", "data": data_url("text/plain", note.encode("utf-8"))})
        check("C1 .txt 投喂成功", bool(txt["summary"]) and not txt["truncated"])
        docx = api("/v2/ingest/file", "POST",
                   {"name": "组织笔记.docx",
                    "data": data_url("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                     make_docx("如果组织里信息不透明，那么协作成本会上升，因为大家需要反复确认。"))})
        check("C2 .docx（标准库解析）投喂成功", bool(docx["summary"]))
        pdf = call("/v2/ingest/file", "POST",
                   {"name": "econ.pdf",
                    "data": data_url("application/pdf", make_pdf("Incentives determine whether a process deforms."))})
        check("C3 .pdf 投喂成功", pdf.get("ok") is True, pdf.get("error"))
        bad = call("/v2/ingest/file", "POST",
                   {"name": "x.exe", "data": data_url("application/octet-stream", b"MZ")})
        check("C4 不支持的格式明确拒绝", bad.get("ok") is False and bad["error"]["code"] == "UNSUPPORTED_FILE")

        # D. 视频链接
        only_link = api("/v2/ingest/link", "POST", {"url": "https://example.com/watch?v=1", "title": "某期视频"})
        check("D1 只给链接时如实说明未提炼，不编造内容",
              only_link["needs_text"] is True and not only_link["changes"])
        with_note = api("/v2/ingest/link", "POST",
                        {"url": "https://example.com/watch?v=2", "title": "增长访谈",
                         "note": "用户增长的留存如果只看次日，会漏掉长期价值，所以要分渠道看cohort。"})
        check("D2 贴字幕 / 摘要后可提炼并保留链接",
              bool(with_note["summary"]) and with_note["source"]["url"].startswith("https://"))

        # E. 对话
        chat = api("/v2/chat", "POST",
                   {"text": "我觉得如果组织里信息不透明，那么协作成本会上升，因为大家要反复确认。"})
        check("E1 对话有回复与沉淀卡片", bool(chat["reply"]) and bool(chat["assistant"]["ledger"]))
        bodies = "\n".join(p.read_text(encoding="utf-8") for p in (VAULT / "knowledge").glob("*.md"))
        check("E2 对话原文没有堆进知识库", "我觉得" not in bodies)
        chat_log = (VAULT / "chat.json").read_text(encoding="utf-8")
        check("E3 对话原文只留在对话记录里", "我觉得如果组织里信息不透明" in chat_log)
        small = api("/v2/chat", "POST", {"text": "嗯，你好呀"})
        check("E4 没有实质内容时如实说「没有可沉淀的内容」",
              small["distill"]["summary"] == "没有可沉淀的内容" and not small["distill"]["changes"])
        manual_distill = api("/v2/distill", "POST",
                             {"text": "教育里的反馈如果只给分数，学生就会优化分数而不是理解。",
                              "title": "手动整理", "kind": "note"})
        check("E5 手动整理入口可用", bool(manual_distill["changes"]))

        # F. 处境 → 待办 → 完成 → 回补
        situation = "想推进制度设计，但激励约束没对齐，流程又变形了，协作成本一直在涨"
        plan1 = api("/v2/plan", "POST", {"situation": situation})
        check("F1 梳理出问题清单", len(plan1["problems"]) >= 1, plan1["problems"])
        check("F2 生成 3 条以上明确待办且带下一步动作",
              len(plan1["todos"]) >= 3 and all(t["next_action"] for t in plan1["todos"]))
        check("F3 待办引用了知识库框架或如实说明缺框架",
              any(t["frame_ids"] for t in plan1["todos"]) or plan1["frames_used"] == [])
        situation2 = "还是在推进制度设计，激励约束没对齐，流程变形导致协作成本上涨"
        plan2 = api("/v2/plan", "POST", {"situation": situation2})
        check("F4 相似处境被召回", len(plan2["similar"]) >= 1,
              [(h["score"], h["text"][:30]) for h in plan2["similar"]])
        aid = plan2["todos"][0]["id"]
        done = api(f"/v2/actions/{aid}/complete", "POST")
        check("F5 完成待办给正向反馈与名言出处",
              bool(done["encouragement"]["line"]) and bool(done["encouragement"]["quote"]["from"]),
              done["encouragement"]["quote"])
        reflect = api(f"/v2/actions/{aid}/reflect", "POST",
                      {"text": "下次不要又先改流程，应该先对齐激励约束，这是重复失误。"})
        check("F6 执行中的新想法回补进知识库",
              bool(reflect["distill"]["changes"]) and reflect["source"]["kind"] == "reflection")
        plan3 = api("/v2/plan", "POST", {"situation": situation2})
        check("F7 相似问题调用既有框架并提示重复失误",
              any(h["pitfalls"] for h in plan3["similar"]),
              [h["pitfalls"] for h in plan3["similar"]])

        # G. 检索、说明、吸收标记
        search = api("/v2/search?q=激励约束")
        check("G1 统一检索覆盖知识 / 来源 / 待办",
              len(search["items"]) >= 1 and any(i["kind"] == "knowledge" for i in search["items"]),
              [i["kind"] for i in search["items"]])
        manual = api("/v2/manual")
        check("G2 个人使用说明按领域生成",
              manual["total"] >= 1 and any(s["narrative"] for s in manual["sections"]))
        pending = [k for k in api("/v2/knowledge")["items"] if k["status"] == "unabsorbed"]
        if pending:
            absorbed = api("/v2/knowledge/" + pending[0]["id"] + "/absorb", "POST")
            check("G3 可把未吸收认知标记为已吸收", absorbed["status"] == "absorbed")

        # H. 导入与数据完整性
        imported = api("/v2/import/records", "POST")
        check("H1 现有 records/ 可主动导入知识库", imported["docs"] >= 1, imported)
        check("H2 导入不改动 records/ 任何文件", fingerprint(RECORDS) == records_before)
        check("H3 导入不改动 _chat/", fingerprint(ROOT / "records" / "_chat") == chat_before)
        final_state = api("/v2/state")
        check("H4 统计如实反映沉淀结果",
              final_state["counts"]["knowledge"] >= 1
              and final_state["counts"]["todos_done"] >= 1
              and final_state["counts"]["situations"] >= 3, final_state["counts"])
    finally:
        cleanup(original)
        print("\n=== 测试数据已还原（vault/ 回到运行前状态）===")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n合计 {len(RESULTS)} 条，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print("  FAIL:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
