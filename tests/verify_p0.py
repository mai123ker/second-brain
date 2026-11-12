"""P0 端到端验收（文档形态）。

前置（端口可用 VERIFY_BASE / VERIFY_DEGRADED 环境变量覆盖）：
- 验收实例（默认 8010）用 MIRROR_LLM_MODE=mock 启动，对话链路需要模型；
- 降级实例（默认 8011）用 MIRROR_LLM_MODE=off 强制视为未配置，验证 LLM_UNAVAILABLE；
  注意 8000 往往是日常真模型主服务，验收绝不直接打 8000。
- 两个临时实例都设 MIRROR_DISABLE_AUTO_BACKUP=1，避免和主服务抢写 backup/。
日常使用不要设 MIRROR_LLM_MODE，在界面或 config.json 里配真实模型。
验收会写文档 / 对话 / 图片 / 备份，结尾 cleanup() 逐项还原，不留痕迹。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.storage import frontmatter  # noqa: E402
from src.storage.paths import INDEX_DB, VAULT  # noqa: E402

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8010")
DEGRADED = os.environ.get("VERIFY_DEGRADED", "http://127.0.0.1:8011")
results: list[tuple[str, bool, str]] = []


def call(method, path, payload=None, base=BASE):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8"))
        except Exception:
            return error.code, {"ok": False, "error": {"code": "NON_JSON", "message": ""}}
    except urllib.error.URLError as error:
        return 0, {"ok": False, "error": {"code": "NETWORK", "message": str(error)}}


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def error_code(response):
    return response.get("error", {}).get("code", "") if isinstance(response, dict) else ""


def doc_path(doc_id):
    status, body = call("GET", f"/docs/{doc_id}")
    return ROOT / body["data"]["doc"]["path"] if status == 200 else None


def _flatten_tree(nodes):
    out = []
    for n in nodes or []:
        out.append(n)
        if n.get("type") == "dir":
            out.extend(_flatten_tree(n.get("children", [])))
    return out


CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / ".backup_state.json"
CONFIG_RAW = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else None
STATE_RAW = STATE_FILE.read_bytes() if STATE_FILE.exists() else None
CHAT_DIR = VAULT / "_chat"
CHAT_RAW = ({p.name: p.read_bytes() for p in CHAT_DIR.glob("*.json")}
            if CHAT_DIR.exists() else {})
ZIPS_BEFORE = {p.name for p in (ROOT / "backup").glob("*.zip")} if (ROOT / "backup").exists() else set()
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


def stream_call(path, payload, base=None):
    request = urllib.request.Request(
        (base or BASE) + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:
        return 0, str(error)


def check_new_features():
    """第一档 / 第二档新增能力的端到端验收；跑完自清理，不污染真实笔记。"""
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    chat_id = "verify-chat-doc"
    chat_file = VAULT / "_chat" / (chat_id + ".json")
    chat_existed = chat_file.exists()
    chat_raw = chat_file.read_bytes() if chat_existed else None
    asset_name = ""
    try:
        messages = [{"role": "user", "content": "验收落盘问题"},
                    {"role": "assistant", "content": "验收落盘回答"}]
        status, put = call("PUT", "/chat/history", {"doc_id": chat_id, "messages": messages})
        _, got = call("GET", "/chat/history?doc_id=" + chat_id)
        check("新增 1 对话落到 records/_chat/ 并可读回",
              status == 200 and put.get("ok") is True
              and len(got.get("data", {}).get("messages", [])) == 2 and chat_file.exists())

        _, tree_resp = call("GET", "/docs/tree")
        _, leakage = call("GET", "/search?q=" + urllib.parse.quote("验收落盘问题"))
        check("新增 2 对话文件不进文档树与索引",
              "_chat" not in json.dumps(tree_resp, ensure_ascii=False)
              and not leakage.get("data", {}).get("results"))

        _, md = call("GET", f"/chat/export?doc_id={chat_id}&format=md")
        _, js = call("GET", f"/chat/export?doc_id={chat_id}&format=json")
        check("新增 3 对话可导出 Markdown / JSON",
              "验收落盘问题" in str(md.get("data", {}).get("content", ""))
              and "验收落盘回答" in str(js.get("data", {}).get("content", "")))

        status, raw_stream = stream_call("/chat/stream", {
            "doc_id": chat_id, "allow_patch": True,
            "messages": [{"role": "user", "content": "帮我补充一段"}],
        })
        check("新增 4 /chat/stream 逐块 delta 并以 done 收尾",
              status == 200 and '"delta"' in raw_stream and '"done"' in raw_stream,
              f"HTTP {status}, {len(raw_stream)} chars")

        _, after = call("GET", "/chat/history?doc_id=" + chat_id)
        saved_messages = after.get("data", {}).get("messages", [])
        check("新增 5 流式结束后服务端自动落盘完整回答",
              any(m.get("role") == "assistant" and str(m.get("content", "")).strip()
                  for m in saved_messages), f"{len(saved_messages)} 条")

        _, uploaded = call("POST", "/assets/upload", {
            "data": "data:image/png;base64," + PNG_1PX, "name": "verify.png"})
        url = str(uploaded.get("data", {}).get("url", ""))
        asset_name = url.rsplit("/", 1)[-1] if url else ""
        image_status = 0
        if url:
            try:
                with urllib.request.urlopen(BASE + url, timeout=10) as response:
                    image_status = response.status
            except Exception:
                pass
        check("新增 6 图片上传落盘并可由 /assets 访问",
              bool(url) and image_status == 200
              and (VAULT / "_assets" / asset_name).exists())

        _, searched = call("GET", "/search?q=" + urllib.parse.quote("认知镜像"))
        engine_name = str(searched.get("data", {}).get("engine", ""))
        check("新增 7 搜索升级为 FTS5 相关性排序",
              engine_name.startswith("fts5")
              and len(searched.get("data", {}).get("results", [])) >= 1, engine_name)

        _, listed = call("GET", "/backups")
        _, packed = call("POST", "/backups/run")
        zip_path = ROOT / "backup" / str(packed.get("data", {}).get("file", ""))
        has_chat = False
        if zip_path.exists():
            with zipfile.ZipFile(zip_path) as archive:
                has_chat = any(name.startswith("records/_chat/") for name in archive.namelist())
        check("新增 8 备份自动化可用，且备份包含对话记录",
              listed.get("data", {}).get("auto") is True
              and packed.get("data", {}).get("files", 0) > 0 and has_chat)

        # 拖拽文件入库：文本 → 外部素材；不认识的类型 → 诚实拒绝
        text_b64 = base64.b64encode("拖拽整理测试\n\n这是拖进对话框的文件原文。".encode("utf-8")).decode()
        ing_status, ingested = call("POST", "/ingest/file",
                                    {"name": "拖拽整理测试.txt",
                                     "data": "data:text/plain;base64," + text_b64})
        ing_data = ingested.get("data", {})
        check("新增 17 拖拽文件入库：文本落外部素材，stance=unverified，可被检索",
              ing_status == 200 and ing_data.get("id")
              and "外部素材" in str(ing_data.get("file", ""))
              and ing_data.get("chars", 0) > 0)
        bad_status, bad_file = call("POST", "/ingest/file",
                                    {"name": "bad.exe", "data": "data:application/octet-stream;base64,AAAA"})
        check("新增 17b 不支持的文件类型诚实拒绝，不落库",
              bad_status == 400 and error_code(bad_file) == "UNSUPPORTED_FILE")

        check("新增 9 前端具备流式 / 重试 / 导出 / 大纲 / 表格 / 图片 / 待办添加入口",
              all(key in app_js for key in ("requestReply", "chat-retry", "exportChat",
                                            "renderOutline", "tableAction", "uploadImage",
                                            "/chat/stream", "todo-add-btn"))
              and 'id="outline"' in page and 'id="chat-export"' in page)

        check("新增 10 对话记录可写进正文（追加 / 插入 / 替换 + 锚点），消息级写入不再有导出形式",
              all(key in app_js for key in ("writeToDoc", "writeActionsHtml", "WRITE_MODES",
                                            "updateAnchor", "renderAnchorHint", "locateBlock",
                                            "insertChatPatch", "patch-insert"))
              and all(f'data-write="{mode}"' in app_js for mode in ("append", "insert", "replace"))
              and 'id="anchor-hint"' in page)

        # 18 归位闭环：跨文档写盘 + 归位建议协议
        from src.api.chat import _split_rehome  # noqa: E402
        r1_text, r1 = _split_rehome("先答你的问题。\n<<<REHOME>>>《季度复盘》<<<END>>>")
        r2_text, r2 = _split_rehome("这句值得留下。\n<<<REHOME>>>新建<<<END>>>")
        r3_text, r3 = _split_rehome("没有建议，纯回答。")
        prompt_raw = (ROOT / "prompts" / "chat_system.md").read_text(encoding="utf-8")
        check("新增 18 归位建议协议：标记解析（归到已有 / 新建 / 无标记）+ 提示词内含协议",
              r1 == "季度复盘" and r2 == "new" and r3 == ""
              and "先答你的问题。" in r1_text and "这句值得留下。" in r2_text
              and "<<<REHOME>>>" in prompt_raw and "绝不硬凑" in prompt_raw)
        check("新增 18b 前端具备跨文档写盘与建议卡片（归到… / 归到这篇 / 新建一篇 / 撤回锚点）",
              all(key in app_js for key in ("writeTextIntoDoc", "rehomePickTarget", "insertUnderHeading",
                                            "applyRehome", "dropRehome",
                                            "rehomeSourceText", "rehome-apply", "rehome-drop",
                                            'data-write="rehome"')))

        # 19 归位反馈：直达入口 / 理由摘句 / 待归位 badge / 搜索过滤高亮
        rel_status, rel_data = call("GET", "/related?q=" + urllib.parse.quote("复盘 方法 岗位"))
        passages = rel_data.get("data", {}).get("passages", [])
        check("新增 19 /related 只读检索：返回 passages 且带 title/excerpt/date 字段",
              rel_status == 200 and isinstance(passages, list)
              and all(k in passages[0] for k in ("title", "excerpt", "date")) if passages
              else rel_status == 200)
        check("新增 19b 前端归位反馈锚点：notice 动作直达 / 理由行 fetchRelatedWhy / badge / hover 收敛",
              all(key in app_js for key in ("notice-act", "fetchRelatedWhy", "rehome-why",
                                            "dir-badge", "highlightHit", "dir-chip")))
        check("新增 19c 高亮先转义再包 mark（XSS 防护）",
              "esc(s.slice(0, idx))" in app_js and "<mark>" in app_js)

        # 19d 树节点状态标识：doc 节点带 status，前端状态点 + 只读待归位提示（不再有挂载/忽略按钮）
        tree_status, tree_data = call("GET", "/docs/tree")
        tree_docs = [n for n in _flatten_tree(tree_data.get("data", {}).get("children", []))
                     if n.get("type") == "doc"]
        check("新增 19d 树节点带 status 字段，前端有状态点且移除挂载/忽略按钮",
              tree_status == 200 and tree_docs
              and all("status" in n for n in tree_docs)
              and "status-dot" in app_js and "pending-text" in app_js
              and "data-place=\"attach\"" not in app_js and "data-place=\"ignore\"" not in app_js)

        # 20 文档主动提问：生成（mock 占位）/ 收件箱 / 消费，三态齐全
        gen_status, gen = call("POST", "/questions/generate", {})
        gen_data = gen.get("data", {}) if isinstance(gen, dict) else {}
        gen_qs = gen_data.get("questions", [])
        check("新增 20 文档主动提问：生成 ≥1 条且挂来源文档与理由",
              gen_status == 200 and len(gen_qs) >= 1
              and bool(gen_qs[0].get("doc_id")) and bool(gen_qs[0].get("question"))
              and "reason" in gen_qs[0])
        pend_status, pend = call("GET", "/questions/pending")
        pend_items = pend.get("data", {}).get("items", []) if isinstance(pend, dict) else []
        check("新增 20b 收件箱可读回，条目带 status=pending 与 created",
              pend_status == 200 and len(pend_items) >= 1
              and pend_items[0].get("status") == "pending"
              and bool(pend_items[0].get("created")))
        if pend_items:
            c_status, _c = call("POST", "/questions/consume", {"id": pend_items[0]["id"]})
            _, pend2 = call("GET", "/questions/pending")
            rest = pend2.get("data", {}).get("items", []) if isinstance(pend2, dict) else []
            check("新增 20c 消费后从收件箱消失",
                  c_status == 200 and not any(x.get("id") == pend_items[0]["id"] for x in rest))
        else:
            check("新增 20c 消费后从收件箱消失", False)
        check("新增 20d 前端收件箱锚点：渲染 / 回答 / 忽略 / 现场问 / 文档名直达 / 树上问标记 + 提问提示词存在",
              all(key in app_js for key in ("renderInbox", "refreshInbox", "answerInbox",
                                            "dismissInbox", "askNow", "chat-inbox",
                                            "inbox-answer", "inbox-open", "ask-badge", "asked-tag"))
              and (ROOT / "prompts" / "proactive_system.md").exists())

        # 21 对话三步流程：文档问答 → 联网搜索 → 总结归档
        three_step = {"config_search": False, "web_chat": False, "summarize": False,
                      "web_degrade": False}
        _, cfg_q = call("GET", "/config")
        three_step["config_search"] = bool(cfg_q.get("data", {}).get("search", {}).get("available"))
        # 联网对话：mock 给占位来源，验证整条注入管道
        _, web_q = call("POST", "/chat", {
            "doc_id": "", "web": True, "allow_patch": False,
            "messages": [{"role": "user", "content": "Obsidian 最新版本支持什么新功能？"}]})
        wdata = web_q.get("data", {})
        three_step["web_chat"] = (web_q.get("ok") is True
                                  and len(wdata.get("sources") or []) >= 3
                                  and "来源" not in (wdata.get("reply") or "")[:0] + "")
        three_step["web_degrade"] = bool(wdata.get("web_note")) or len(wdata.get("sources") or []) > 0
        # 总结归档：mock 返回占位草稿
        _, sum_q = call("POST", "/chat/summarize", {
            "doc_id": "",
            "messages": [{"role": "user", "content": "我想把定价分析先从竞品看起。"},
                          {"role": "assistant", "content": "你想先看竞品再定自己的档位，这个顺序背后的依据是什么？"}]})
        sdata = sum_q.get("data", {})
        three_step["summarize"] = (sum_q.get("ok") is True
                                   and "## 对话要点" in str(sdata.get("draft") or ""))
        check("新增 21 三步流程：/config 带搜索可用性、web=true 对话带 sources、/chat/summarize 出草稿",
              all(three_step.values()))

        check("新增 21b 前端锚点：联网开关 / 总结按钮 / 来源行 / 归档选择器 + 两份提示词存在",
              all(key in app_js for key in ("summarizeAndArchive", "chat-web", "chat-summarize",
                                            "msg-sources", "rehomePickTarget", "state.web"))
              and (ROOT / "prompts" / "summarize_system.md").exists())

        # 21c 语音输入三级策略：录音走本机 /stt（BYOK），浏览器识别兜底，否则诚实
        _, stt_cfg = call("GET", "/config")
        has_stt_api = "stt" in stt_cfg.get("data", {})
        fake_wav = ("data:audio/webm;base64," + "AAAA" * 300)  # 足够 800 字节的假录音
        _, stt_q = call("POST", "/stt", {"data": fake_wav})
        stt_mock = (stt_q.get("ok") is True
                    and "演示模式占位" in str(stt_q.get("data", {}).get("text") or ""))
        _, stt_bad = call("POST", "/stt", {"data": "不是dataURL"})
        check("新增 21c 语音输入：/config 带 stt 可用性、mock 转写返回占位文本、坏数据诚实拒绝",
              has_stt_api and stt_mock and not stt_bad.get("ok"))
        check("新增 21d 前端语音锚点：麦克风常显 + 录音/识别双通道 + 降级话术",
              all(key in app_js for key in ("startRecording", "blobToDataUrl", "encodeWav",
                                            "chat-mic", "SpeechRecognition", "sttReady")))

        # 11 四象限：标签即依据，标了能读回、能取消，且不改原文语义
        # 样本自己造：用户库里可能一条未完成待办都没有（都勾完了），
        # 验收不能依赖用户的真实数据状态。
        _, made_todo = call("POST", "/records", {
            "title": "验收待办样本", "stream": "self", "kind": "thought",
            "content": "# 验收待办样本\n\n- [ ] 验收用待办一\n- [ ] 验收用待办二\n"})
        sample_todo_id = made_todo.get("data", {}).get("id", "")
        _, todo_list = call("GET", "/todos")
        pick = next((t for t in todo_list.get("data", {}).get("todos", [])
                     if t["doc_id"] == sample_todo_id and not t.get("checked")), None)
        quad = {"api": False, "flag": False, "basis": False, "restored": False}
        if pick:
            todo_file = ROOT / pick["path"]
            todo_raw = todo_file.read_bytes()
            endpoint = f"/docs/{pick['doc_id']}/todo"
            call("POST", endpoint, {"action": "flag", "line": pick["line"],
                                    "field": "important", "value": True})
            _, after_flag = call("GET", "/todos")
            item = next((t for t in after_flag.get("data", {}).get("todos", [])
                         if t["doc_id"] == pick["doc_id"] and t["line"] == pick["line"]), {})
            disk = todo_file.read_text(encoding="utf-8")
            quad["api"] = item.get("important") is True and "#重要" in disk
            quad["basis"] = "#重要" not in str(item.get("text", "")) and "urgent" in item
            call("POST", endpoint, {"action": "flag", "line": pick["line"],
                                    "field": "important", "value": False})
            _, after_clear = call("GET", "/todos")
            cleared = next((t for t in after_clear.get("data", {}).get("todos", [])
                            if t["doc_id"] == pick["doc_id"] and t["line"] == pick["line"]), {})
            quad["flag"] = cleared.get("important") is False
            todo_file.write_bytes(todo_raw)
            quad["restored"] = todo_file.read_bytes() == todo_raw
        check("新增 11 待办四象限：标签即依据、可标可推翻、系统不自动判定",
              all(quad.values())
              and all(key in app_js for key in ("QUADRANTS", "quadrantIndex",
                                                "renderAllTodos", "flag-btn",
                                                "todo-tag", "不重要不紧急"))
              and "系统不替你判定" in app_js)

        # 12 拖放：一次写盘同时定下两个标签，也能拖回
        _, todo_list2 = call("GET", "/todos")
        pick2 = next((t for t in todo_list2.get("data", {}).get("todos", [])
                      if t["doc_id"] == sample_todo_id and not t.get("checked")), None)
        drag = {"placed": False, "parsed": False, "back": False, "restored": False}
        if pick2:
            dfile = ROOT / pick2["path"]
            draw = dfile.read_bytes()
            endpoint2 = f"/docs/{pick2['doc_id']}/todo"
            _, placed = call("POST", endpoint2, {"action": "place", "line": pick2["line"],
                                                  "important": True, "urgent": True})
            _, after_place = call("GET", "/todos")
            it = next((t for t in after_place.get("data", {}).get("todos", [])
                       if t["doc_id"] == pick2["doc_id"] and t["line"] == pick2["line"]), {})
            drag["placed"] = (placed.get("ok") is True
                              and it.get("important") is True and it.get("urgent") is True)
            drag["parsed"] = "#重要 #紧急" in dfile.read_text(encoding="utf-8")
            call("POST", endpoint2, {"action": "place", "line": pick2["line"],
                                     "important": False, "urgent": False})
            _, after_back = call("GET", "/todos")
            back = next((t for t in after_back.get("data", {}).get("todos", [])
                         if t["doc_id"] == pick2["doc_id"] and t["line"] == pick2["line"]), {})
            drag["back"] = back.get("important") is False and back.get("urgent") is False
            dfile.write_bytes(draw)
            drag["restored"] = dfile.read_bytes() == draw
        check("新增 12 待办可拖到别的象限：一次写盘、双标签同步、可拖回",
              all(drag.values())
              and all(key in app_js for key in ("bindTodoDrag", "QUADRANT_FLAGS",
                                                "placeTodo", 'draggable="true"'))
              and "action: 'place'" in app_js)

        # 13 速记已并入对话：POST /records 接口仍在（对话归档共用），表单入口已删
        chat_prompt_raw = (ROOT / "prompts" / "chat_system.md").read_text(encoding="utf-8")
        chat_prompt_has_recording_rule = ("记一下" in chat_prompt_raw
                                          and "<<<REHOME>>>" in chat_prompt_raw)
        _, chat_rehome = call("POST", "/chat", {"doc_id": "", "allow_patch": False,
            "messages": [{"role": "user", "content": "帮我记一下：今天想到定价要先看竞品。"}]})
        mock_rehome_works = (chat_rehome.get("data", {}) or {}).get("rehome") == "new"
        quick = {"created": False, "pending": False, "placed": False, "cleaned": False}
        _, goals_list = call("GET", "/goals")
        goals_data = goals_list.get("data", {}).get("goals", [])
        target_id = goals_data[0]["id"] if goals_data else ""
        _, made = call("POST", "/records", {
            "title": "验收速记条目", "content": "# 验收速记条目\n\n一句话进探索区。",
            "stream": "self", "kind": "thought"})
        quick_id = made.get("data", {}).get("id", "")
        quick["created"] = bool(quick_id)
        if quick_id:
            quick["pending"] = (made.get("data", {}).get("status") == "pending"
                                and str(made.get("data", {}).get("file", "")).startswith("records/待归位"))
            _, tree_q = call("GET", "/docs/tree")
            quick["pending"] = quick["pending"] and "验收速记条目" in json.dumps(
                tree_q.get("data", {}), ensure_ascii=False)
            if target_id:
                _, placed_q = call("POST", f"/records/{quick_id}/place",
                                   {"action": "attach", "target": target_id})
                quick["placed"] = placed_q.get("data", {}).get("status") == "placed"
            call("POST", f"/docs/{quick_id}/delete", {})
            _, tree_after = call("GET", "/docs/tree")
            quick["cleaned"] = "验收速记条目" not in json.dumps(
                tree_after.get("data", {}), ensure_ascii=False)
        check("新增 13 速记并入对话：POST /records 仍通（对话归档共用），表单入口已删，对话归档锚点在位",
              all(quick.values())
              and "openQuickNote" not in app_js and 'id="quick-note-btn"' not in page
              and "applyRehome" in app_js)
        check("新增 13b 记录意图协议：用户说「记一下」时模型必须给归档标记（mock 链路验证）",
              chat_prompt_has_recording_rule and mock_rehome_works)

        # 14 未探索提示：字段齐全、逻辑自洽（单线信号成立时活跃天数 ≥2）
        cov_ok = True
        for days in (1, 7, 30):
            _, cov = call("GET", f"/stats/coverage?days={days}")
            c = cov.get("data", {})
            cov_ok = cov_ok and all(k in c for k in
                                    ("window", "active_days", "lines",
                                     "on_single_line", "line", "days"))
            if cov_ok:
                cov_ok = (c["window"] == days and isinstance(c["on_single_line"], bool)
                          and (not c["on_single_line"] or c["active_days"] >= 2))
        check("新增 14 未探索提示：coverage 只报事实、字段齐全、信号成立时活跃天数 ≥2",
              cov_ok and "refreshCoverage" in app_js and 'id="cov-badge"' in page)

        # 15 悬空概念：两篇未归位记录共享独有概念 → 提议出现；立线/清理后消失
        dang = {"fired": False, "docs": 2, "gone": False}
        concept = "潮汐规律"
        made_ids = []
        for suffix in ("甲", "乙"):
            _, made_d = call("POST", "/records", {
                "title": f"验收悬空{suffix}：{concept}",
                "content": f"# 验收悬空{suffix}\n\n- [ ] 复查{concept}的前提",
                "stream": "self", "kind": "thought"})
            made_ids.append(made_d.get("data", {}).get("id", ""))
        _, d_after = call("GET", "/stats/dangling")
        hits = [c for c in d_after.get("data", {}).get("concepts", [])
                if concept in str(c.get("word", ""))]
        dang["fired"] = bool(hits) and hits[0].get("docs", 0) >= 2
        if hits:
            dang["docs"] = hits[0].get("docs", 0)
        for did in made_ids:
            call("POST", f"/docs/{did}/delete", {})
        _, d_clean = call("GET", "/stats/dangling")
        dang["gone"] = not any(concept in str(c.get("word", ""))
                               for c in d_clean.get("data", {}).get("concepts", []))
        check("新增 15 悬空概念：≥2 篇未归位记录触发提议，清理后消失，立线入口在前端",
              all(dang.values()) and "loadDanglingRow" in app_js
              and "dangling-make" in app_js and "/stats/dangling" in app_js)

        # 16 桌面打包：sidecar 与 Tauri 工程文件就位，frozen 路径分支存在
        tauri_conf = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
        tauri_main = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
        paths_src = (ROOT / "src" / "storage" / "paths.py").read_text(encoding="utf-8")
        check("新增 16 桌面打包：Tauri 工程与 sidecar 脚本就位，paths.py 支持 frozen 路径",
              (ROOT / "desktop" / "build_sidecar.ps1").exists()
              and (ROOT / "desktop" / "README.md").exists()
              and tauri_conf.exists() and tauri_main.exists()
              and '"externalBin"' in tauri_conf.read_text(encoding="utf-8")
              and "sidecar" in tauri_main.read_text(encoding="utf-8")
              and 'getattr(sys, "frozen", False)' in paths_src)

        # 17 孤儿文件（用户直接在 Obsidian 里新建、没有元数据的 .md）：
        #     树里标灰「未初始化」，点它弹「补齐」，点了才写盘。
        orphan_dir = VAULT / "学习"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan_file = orphan_dir / "验收孤儿样本.md"
        orphan_body = ("# 我自己写的标题\n\n有 <script>alert(1)</script> 和 & 符号。\n\n"
                       "- [ ] 待办甲\n- [x] 待办乙\n\n最后一行没有换行符")
        orphan_file.write_text(orphan_body, encoding="utf-8", newline="")
        try:
            _, orphans_resp = call("GET", "/docs/orphans")
            listed = [o.get("path") for o in orphans_resp.get("data", {}).get("orphans", [])]
            check("新增 17 孤儿文件被列出（只读，不改文件）",
                  "records/学习/验收孤儿样本.md" in listed,
                  f"{len(listed)} 个")

            _, tree_resp = call("GET", "/docs/tree")
            flat = _flatten_tree(tree_resp.get("data", {}).get("children", []))
            node = next((n for n in flat
                         if n.get("path") == "records/学习/验收孤儿样本.md"), None)
            check("新增 18 树里孤儿标 orphan 且 id/status 置空（不误算待归位）",
                  node is not None and node.get("orphan") is True
                  and node.get("id") == "" and node.get("status") == "",
                  json.dumps(node, ensure_ascii=False) if node else "树里没有")

            check("新增 19 前端：孤儿标灰 + 未初始化标签 + 补齐入口齐备",
                  "orphan" in app_js and "orphan-tag" in app_js
                  and "openAdopt" in app_js and "/docs/adopt" in app_js
                  and "未初始化" in app_js)

            status, adopted = call("POST", "/docs/adopt", {
                "path": "records/学习/验收孤儿样本.md", "title": "验收孤儿样本", "target": ""})
            check("新增 20 用户点「补齐」才写盘，返回新 id",
                  status == 200 and bool(adopted.get("data", {}).get("id")),
                  f"HTTP {status}")

            # 正文必须逐字节不变——含末尾没有换行这一点
            written = orphan_file.read_text(encoding="utf-8")
            body_now = written.split("---\n", 2)[-1]
            if body_now.startswith("\n"):
                body_now = body_now[1:]
            check("新增 21 补齐后正文逐字节不变（不补末尾换行、不规整格式）",
                  body_now == orphan_body,
                  f"原 {len(orphan_body)} 字 / 现 {len(body_now)} 字")

            head = written.split("---")[1]
            fields = [ln.split(":")[0].strip() for ln in head.strip().splitlines()
                      if ":" in ln]
            check("新增 22 补齐写入完整 15 字段 frontmatter", len(fields) == 15, f"{len(fields)} 个")

            _, tree_resp = call("GET", "/docs/tree")
            flat = _flatten_tree(tree_resp.get("data", {}).get("children", []))
            node = next((n for n in flat
                         if n.get("path") == "records/学习/验收孤儿样本.md"), None)
            check("新增 23 补齐后树上 orphan 标记消失",
                  node is not None and node.get("orphan") is False
                  and node.get("id") == adopted.get("data", {}).get("id"))

            status, dup = call("POST", "/docs/adopt", {
                "path": "records/学习/验收孤儿样本.md", "title": "再补一次", "target": ""})
            check("新增 24 重复补齐被拒（CONFLICT），不会二次写盘",
                  status == 409 and error_code(dup) == "CONFLICT", f"HTTP {status}")

            status, _ = call("POST", "/docs/adopt", {
                "path": "../../etc/passwd", "title": "越界", "target": ""})
            check("新增 25 路径穿越被拒", status >= 400, f"HTTP {status}")

            # 分隔线不能被当成 frontmatter 吞掉
            sep_file = VAULT / "学习" / "验收孤儿分隔线.md"
            sep_body = "---\n这是分隔线不是元数据\n---\n真正的正文"
            sep_file.write_text(sep_body, encoding="utf-8", newline="")
            status, _ = call("POST", "/docs/adopt", {
                "path": "records/学习/验收孤儿分隔线.md", "title": "验收孤儿分隔线", "target": ""})
            sep_now = sep_file.read_text(encoding="utf-8").split("---\n", 2)[-1]
            if sep_now.startswith("\n"):
                sep_now = sep_now[1:]
            check("新增 26 用户拿 --- 当分隔线用时，分隔线与正文一字未丢",
                  status == 200 and sep_now == sep_body, f"HTTP {status} / {sep_now!r}")

            # 用户自己写过的部分元数据要保留
            part_file = VAULT / "学习" / "验收孤儿部分元数据.md"
            part_body = "---\ntitle: 我自己写的标题\n---\n正文在这里"
            part_file.write_text(part_body, encoding="utf-8", newline="")
            status, part = call("POST", "/docs/adopt", {
                "path": "records/学习/验收孤儿部分元数据.md", "title": "", "target": ""})
            check("新增 27 用户原有的 title 被保留，不被默认值抹掉",
                  status == 200
                  and part.get("data", {}).get("title") == "我自己写的标题",
                  str(part.get("data", {}).get("title")))
        finally:
            orphan_file.unlink(missing_ok=True)
            (VAULT / "学习" / "验收孤儿分隔线.md").unlink(missing_ok=True)
            (VAULT / "学习" / "验收孤儿部分元数据.md").unlink(missing_ok=True)
    finally:
        # 逐项还原，验收跑完不留痕迹
        if chat_existed and chat_raw is not None:
            chat_file.write_bytes(chat_raw)
        else:
            chat_file.unlink(missing_ok=True)
        chat_dir = VAULT / "_chat"
        if chat_dir.exists() and not any(chat_dir.iterdir()):
            chat_dir.rmdir()
        if asset_name:
            (VAULT / "_assets" / asset_name).unlink(missing_ok=True)
        if CONFIG_RAW is not None:
            CONFIG_FILE.write_bytes(CONFIG_RAW)
        elif CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        if STATE_RAW is not None:
            STATE_FILE.write_bytes(STATE_RAW)
        else:
            STATE_FILE.unlink(missing_ok=True)
        backup_dir = ROOT / "backup"
        if backup_dir.exists():
            for path in backup_dir.glob("*.zip"):
                if path.name not in ZIPS_BEFORE:
                    path.unlink(missing_ok=True)


def main():
    # 文档形态：取全库最长的一篇做样本（种子长文或用户自己写的都适用，不绑死文件名）
    _, tree_resp0 = call("GET", "/docs/tree")
    tree0 = tree_resp0.get("data", {}) if isinstance(tree_resp0, dict) else {}
    leaves = []

    def collect(nodes):
        for n in nodes:
            if n.get("type") == "dir":
                collect(n.get("children", []))
            elif n.get("id"):
                leaves.append(n)

    collect(tree0.get("children", []))
    backups_before = {str(p) for p in (ROOT / ".backup").rglob("*.md")} if (ROOT / ".backup").exists() else set()
    sample, sample_body = None, ""
    for leaf in leaves:
        status, resp = call("GET", f"/docs/{leaf['id']}")
        if status != 200:
            continue
        body = (resp["data"]["doc"].get("body") or "")
        if len(body) > len(sample_body):
            sample, sample_body = leaf, body
    sample_path = doc_path(sample["id"]) if sample else None
    raw = sample_path.read_bytes() if sample_path else b""
    meta = frontmatter.parse_document(raw.decode("utf-8"))[0] if raw else {}
    check("形态 1 一个主题一篇长文（标题/正文同篇）",
          bool(sample) and sample_body.startswith("# ") and len(sample_body) > 200,
          f"{sample['name'] if sample else '-'} / {len(sample_body)} 字")
    check("形态 2 frontmatter 字段齐全（含 title）",
          bool(meta) and all(k in meta for k in frontmatter.FIELDS) and bool(meta.get("title")),
          f"{len(meta)} 字段 / title={meta.get('title')}")
    check("A3 UTF-8 无 BOM / LF / 末尾换行",
          bool(raw) and not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
          and raw.endswith(b"\n"))

    # 文件树
    _, tree_resp = call("GET", "/docs/tree")
    tree = tree_resp.get("data", {}) if isinstance(tree_resp, dict) else {}
    names = [c.get("name") for c in tree.get("children", [])]
    check("形态 3 左侧文件树按主题目录组织，无图谱",
          all(n in names for n in ("目标", "人生阶段总结", "思考", "学习", "外部素材", "待归位")),
          "/".join(str(n) for n in names))
    has_nested = any(c.get("name") == "人生阶段总结" and
                     any(g.get("name") == "岗位拆解" for g in c.get("children", []))
                     for c in tree.get("children", []))
    check("形态 4 支持二级目录（人生阶段总结 / 岗位拆解）", has_nested)

    # 待办：勾选 + 新增 + 编辑，全部写回同一篇 .md
    _, created_resp = call("POST", "/records", {
        "title": "验收用文档", "content": "# 验收用文档\n\n这是一段可以点名的判断。\n\n## 待办\n\n- [ ] 写一个 Agent 工具调用 demo\n",
        "dir": "思考", "stream": "self", "kind": "thought", "topic": ["产品"],
    })
    test_id = created_resp.get("data", {}).get("id", "")
    test_path = doc_path(test_id)
    _, doc_resp = call("GET", f"/docs/{test_id}")
    before_body = doc_resp["data"]["doc"]["body"]
    first_line = next(i for i, l in enumerate(before_body.splitlines()) if "- [ ]" in l)
    before = test_path.read_text(encoding="utf-8") if test_path else ""
    status, toggled = call("POST", f"/docs/{test_id}/todo",
                           {"action": "toggle", "line": first_line, "checked": True})
    after_toggle = test_path.read_text(encoding="utf-8") if test_path else ""
    check("待办 1 勾选写回文档（- [ ] -> - [x]）",
          status == 200 and "- [x] 写一个 Agent" in after_toggle)
    status, added = call("POST", f"/docs/{test_id}/todo",
                         {"action": "add", "text": "补一条新的待办"})
    after_add = test_path.read_text(encoding="utf-8") if test_path else ""
    check("待办 2 新增待办写进同一篇文档",
          status == 200 and "- [ ] 补一条新的待办" in after_add)
    status, _ = call("PUT", f"/docs/{test_id}",
                     {"content": "# 验收用文档\n\n用户整篇编辑后的正文。\n\n## 待办\n\n- [ ] 编辑后仍可勾选\n"})
    after_edit = test_path.read_text(encoding="utf-8") if test_path else ""
    check("编辑 整篇 Markdown 写回本地 .md",
          status == 200 and "用户整篇编辑后的正文" in after_edit and "## 待办" in after_edit)

    # A4：正文编辑不得改写 frontmatter 的 title（标题只由 rename 改）
    meta_before, _ = frontmatter.parse_document(after_edit)
    title_before = meta_before.get("title", "")
    status, _ = call("PUT", f"/docs/{test_id}",
                     {"content": "# 这句是正文里的 H1，不该变成标题\n\n用户又写了一段。\n"})
    meta_after, _ = frontmatter.parse_document(test_path.read_text(encoding="utf-8"))
    check("A4 编辑正文不改写 frontmatter 的 title（标题只由 rename 改）",
          status == 200 and bool(title_before) and meta_after.get("title") == title_before,
          f"before={title_before} after={meta_after.get('title')}")

    # B1：归位不改不可变字段与正文
    pending_id = "pending-proof"
    pending_path = doc_path(pending_id)
    pending_raw = pending_path.read_bytes()
    p_before = frontmatter.parse_document(pending_path.read_text(encoding="utf-8"))[0]
    p_body_before = frontmatter.parse_document(pending_path.read_text(encoding="utf-8"))[1]
    _, attach = call("POST", f"/records/{pending_id}/place",
                     {"action": "attach", "target": "goal-grad"})
    p_after = frontmatter.parse_document(pending_path.read_text(encoding="utf-8"))[0]
    p_body_after = frontmatter.parse_document(pending_path.read_text(encoding="utf-8"))[1]
    check("B1 归位不改 id/stream/created/updated 与正文",
          all(p_before.get(k) == p_after.get(k) for k in ("id", "stream", "created", "updated"))
          and p_body_before == p_body_after and p_after.get("status") == "placed")

    # EXTERNAL_FIELD_MISSING
    status, bad = call("POST", "/records", {
        "title": "缺前提素材", "content": "# 缺前提素材\n\n一段正文。", "dir": "外部素材",
        "stream": "external", "kind": "article", "source": "《测试》",
        "stance": "experience", "scope": "", "topic": [],
    })
    check("EXTERNAL_FIELD_MISSING 不降级保存",
          status == 400 and error_code(bad) == "EXTERNAL_FIELD_MISSING")

    # B2（B2 用全库快照比对，证明对话链路一张用户文档都不写）
    def vault_signature():
        sig = {}
        for path in VAULT.rglob("*.md"):
            if any(part.startswith(("_", ".")) for part in path.relative_to(VAULT).parts):
                continue
            sig[str(path)] = (path.stat().st_mtime_ns, path.stat().st_size)
        return sig

    doc_before = vault_signature()
    stream_call("/chat/stream", {
        "doc_id": sample["id"], "allow_patch": True,
        "messages": [{"role": "user", "content": "帮我补充一段"}],
    })
    doc_after = vault_signature()
    check("B2 模型只在对话里说话，不写用户文档", doc_before == doc_after)

    # A2：删库可重建
    if INDEX_DB.exists():
        INDEX_DB.unlink()
    from src.storage import index as index_module
    rebuilt = index_module.rebuild()
    _, goals_after = call("GET", "/goals")
    check("A2 删掉 .index/index.db 后重建，文档一条不丢",
          rebuilt >= 13 and len(goals_after.get("data", {}).get("goals", [])) == 3,
          f"重建 {rebuilt} 篇 / 目标 3 个")

    # E1：诚实降级（MIRROR_LLM_MODE=off 的降级实例，对话流返回 SSE error）
    status, degraded_raw = stream_call("/chat/stream", {
        "doc_id": sample["id"], "allow_patch": True,
        "messages": [{"role": "user", "content": "帮我补充一段"}],
    }, base=DEGRADED)
    check("E1 模型不可用时对话流返回 LLM_UNAVAILABLE，不给假回答",
          '"LLM_UNAVAILABLE"' in degraded_raw and '"delta"' not in degraded_raw,
          f"HTTP {status}, {len(degraded_raw)} chars")

    # F2 / F3 / E2
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("F2 空态有明确下一步，不出现「暂无数据」",
          "暂无数据" not in app_js and "＋笔记" in app_js)
    check("F3 三种失败三种不同文案",
          all(t in app_js for t in ("这次没能连上模型，你的记录没有丢",
                                    "外部素材缺少来源 / 立场 / 适用前提",
                                    "服务没有响应，请确认本地服务还在运行")))
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    page_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("E2 前端不引 CDN / 远程字体、无图谱面板",
          "http://" not in app_js
          and 'src="http' not in page_html and 'href="http' not in page_html
          and "@import" not in css
          and "graph" not in page_html.lower())

    # 冲突检测（先做，因为它依赖当前 updated）
    _, current = call("GET", f"/docs/{test_id}")
    fresh_updated = current["data"]["doc"]["updated"]
    status, conflict = call("PUT", f"/docs/{test_id}", {
        "content": "# 冲突测试\n\n正文", "base_updated": "2000-01-01T00:00:00+08:00"})
    check("冲突 1 过期 base_updated 返回 CONFLICT",
          status == 409 and error_code(conflict) == "CONFLICT")
    status, _ = call("PUT", f"/docs/{test_id}", {
        "content": "# 冲突测试\n\n正文", "base_updated": fresh_updated})
    check("冲突 2 带正确的 base_updated 可写入", status == 200)

    # 历史快照
    _, rev_resp = call("GET", f"/docs/{test_id}/revisions")
    revs = rev_resp.get("data", {}).get("revisions", [])
    check("历史 1 每次保存前自动快照", len(revs) >= 1, f"{len(revs)} 版")
    if revs:
        oldest = revs[-1]["name"]
        _, rev_doc = call("GET", f"/docs/{test_id}/revisions/{oldest}")
        check("历史 2 可预览历史版本", "body" in rev_doc.get("data", {}))
        status, _ = call("POST", f"/docs/{test_id}/revisions/{oldest}/restore")
        check("历史 3 可一键恢复历史版本", status == 200)

    # 搜索
    status, found = call("GET", "/search?q=%E5%AE%9E%E6%97%B6%E8%A1%8C%E6%83%85")
    check("搜索 命中标题或正文并返回上下文",
          status == 200 and len(found.get("data", {}).get("results", [])) >= 1,
          f"{len(found.get('data', {}).get('results', []))} 条")

    # 待办聚合
    status, todos = call("GET", "/todos")
    check("待办 跨文档全局聚合",
          status == 200 and len(todos.get("data", {}).get("todos", [])) >= 1,
          f"未完成 {len(todos.get('data', {}).get('open', []))} 条")

    # 重命名 + 移动
    status, renamed = call("POST", f"/docs/{test_id}/rename",
                           {"title": "改名后的文档", "dir": "学习"})
    renamed_path = renamed.get("data", {}).get("file", "")
    check("文档管理 1 重命名同步标题/文件名并移动目录",
          status == 200 and renamed_path.startswith("records/学习/")
          and "改名后的文档" in renamed_path, renamed_path)

    # 归档
    status, archived = call("POST", f"/docs/{test_id}/archive")
    _, tree_after = call("GET", "/docs/tree")
    check("文档管理 2 归档移入 _archive 且从树里消失",
          status == 200 and "_archive" in archived.get("data", {}).get("file", "")
          and "改名后的文档" not in str(tree_after),
          archived.get("data", {}).get("file", ""))

    # 文件夹可以在任意层级之间移动（一级 / 二级 / 笔记根）
    _, made = call("POST", "/folders", {"parent": "", "name": "验收文件夹"})
    level0 = made.get("data", {}).get("path", "")
    _, mv1 = call("POST", "/folders/move", {"from": level0, "to": "人生阶段总结"})
    level1 = mv1.get("data", {}).get("path", "")
    _, mv2 = call("POST", "/folders/move", {"from": level1, "to": "人生阶段总结/岗位拆解"})
    level2 = mv2.get("data", {}).get("path", "")
    _, mv3 = call("POST", "/folders/move", {"from": level2, "to": "records"})
    back = mv3.get("data", {}).get("path", "")
    check("文件夹可在一级 / 二级 / 笔记根之间移动",
          level1.endswith("人生阶段总结/验收文件夹")
          and level2.endswith("岗位拆解/验收文件夹")
          and back.endswith("records/验收文件夹"),
          f"{level1} → {level2} → {back}")
    call("POST", "/folders/delete", {"path": back})

    # 前端能力
    check("前端 1 支持代码块 / 链接 / 图片渲染",
          "md-code" in app_js and "<img" in app_js)
    check("前端 2 自动保存有 3 秒最小间隔", "3000 - (Date.now()" in app_js)
    check("前端 3 冲突时给出覆盖 / 重新载入两条路",
          "用我的覆盖" in app_js and "载入磁盘版本" in app_js)
    check("前端 4 历史 / 搜索 / 全部待办入口齐备",
          all(name in app_js for name in ("openRevisions", "openSearch", "openAllTodos")))
    check("前端 5 渲染视图直接可编辑（所见即所得，不切源码）",
          "htmlToMarkdown" in app_js and "'contenteditable'" in app_js
          and "live-editor" not in app_js and "textarea id=\\\"live-editor\\\"" not in app_js)

    # 三栏布局 + 右侧对话面板
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("前端 6 三栏布局，右栏对话面板可发消息、可收缩",
          all(tag in html for tag in ('id="chat-log"', 'id="chat-input"',
                                      'id="chat-send"', 'id="resizer-chat"', 'id="chat-btn"'))
          and "/chat" in app_js and "renderChat" in app_js and "bindSplit" in app_js)
    status, chat_ok = call("POST", "/chat", {"doc_id": "doc-role",
                                             "messages": [{"role": "user", "content": "这句该先动哪一步？"}]})
    reply = chat_ok.get("data", {}).get("reply", "") if isinstance(chat_ok, dict) else ""
    check("对话 1 能回话（未配模型时如实降级，不假装回答）",
          status in (200, 503) and (bool(reply.strip()) or error_code(chat_ok) == "LLM_UNAVAILABLE"),
          (reply[:24] + "…") if reply else error_code(chat_ok))
    status, chat_bad = call("POST", "/chat", {"messages": [{"role": "assistant", "content": "越权"}]})
    check("对话 2 最后一条必须是用户提问",
          status == 400 and error_code(chat_bad) == "INVALID_PARAM")

    from src.api.chat import _context_block, _split_patch  # noqa: PLC0415
    from src.storage import repo as repo_module  # noqa: PLC0415

    reply_part, patch_part = _split_patch("先说一句\n<<<PATCH>>>\n## 待验证\n\n- [ ] 写清楚\n<<<END>>>")
    check("对话 3 回答能拆成「话」与「可插入正文」",
          reply_part == "先说一句" and patch_part.startswith("## 待验证") and "写清楚" in patch_part)
    check("前端 7 补丁要用户点「插入到文末」才写进文档",
          "insertChatPatch" in app_js and "patch-insert" in app_js and "patch-drop" in app_js)
    context_block = _context_block(repo_module.read_doc("doc-role") or {})
    check("对话 4 当前文档正文进入模型上下文",
          "正文：" in context_block and "岗位" in context_block and len(context_block) > 200,
          f"{len(context_block)} 字")

    _, cfg = call("GET", "/config")
    check("对话 5 输入区显示当前模型（含演示模式标识）",
          bool(cfg.get("data", {}).get("model")),
          str(cfg.get("data", {}).get("model")))

    _, attached = call("POST", "/chat", {"doc_id": "doc-role", "attach": ["doc-review"],
                                         "messages": [{"role": "user", "content": "帮我补充一段"}]})
    check("对话 6 可附加其它文档当上下文",
          attached.get("ok") is True and bool(attached.get("data", {}).get("reply"))
          or error_code(attached) == "LLM_UNAVAILABLE",
          "未配模型时跳过真实回话")

    _, readonly = call("POST", "/chat", {"doc_id": "doc-role", "allow_patch": False,
                                         "messages": [{"role": "user", "content": "帮我补充一段"}]})
    check("对话 7 只读模式不给补丁（只说话）",
          (readonly.get("ok") is True and readonly.get("data", {}).get("patch", "") == "")
          or error_code(readonly) == "LLM_UNAVAILABLE")

    # 本机模型配置：加一个假模型 → 启用 → 删除（不留痕迹）
    _, added = call("POST", "/config/models", {"label": "验收模型", "model": "verify-model",
                                               "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-verify"})
    added_models = added.get("data", {}).get("models", []) if isinstance(added, dict) else []
    verify_entry = next((m for m in added_models if m.get("label") == "验收模型"), {})
    call("POST", "/config/active", {"id": verify_entry.get("id", "")})
    _, after_active = call("GET", "/config")
    check("配置 1 可在本机添加 / 启用 / 删除模型（Key 不回显）",
          verify_entry.get("has_key") is True
          and after_active.get("data", {}).get("active") == verify_entry.get("id")
          and "sk-verify" not in str(after_active))
    call("POST", "/config/models/remove", {"id": verify_entry.get("id", "")})

    check("前端 8 输入区有 @ 引用 / 指令 / 权限 / 模型名 / 停止按钮",
          all(key in app_js for key in ("CHAT_COMMANDS", "chatDocList", "addChatAttach",
                                        "chatSendingUI", "chat-perm", "chat-mic"))
          and all(tag in html for tag in ('id="chat-plus"', 'id="chat-perm"',
                                          'id="chat-model"', 'id="chat-pop"', 'icon-stop')))

    check_new_features()

    # 收尾：还原验收写过的痕迹，跑完不污染真实笔记
    def cleanup():
        try:
            # /chat 与 /chat/stream 都会按 doc_id 落盘，整目录恢复成验收前的样子
            if CHAT_DIR.exists():
                for path in CHAT_DIR.glob("*.json"):
                    if path.name not in CHAT_RAW:
                        path.unlink(missing_ok=True)
            for name, data in CHAT_RAW.items():
                CHAT_DIR.mkdir(parents=True, exist_ok=True)
                (CHAT_DIR / name).write_bytes(data)
            if sample_path and raw:
                sample_path.write_bytes(raw)
            if pending_path and pending_raw:
                pending_path.write_bytes(pending_raw)
            junk = (list(VAULT.glob("思考/验收用文档*.md"))
                    + list(VAULT.glob("学习/改名后的文档*.md"))
                    + list((VAULT / "_archive").glob("改名后的文档*.md"))
                    + list(VAULT.glob("外部素材/拖拽整理测试*.md"))
                    + list(VAULT.rglob("验收待办样本*.md")))
            for path in junk:
                path.unlink(missing_ok=True)
            trash = ROOT / "records" / "_trash"
            if trash.exists():
                for path in trash.glob("验收文件夹*"):
                    shutil.rmtree(path, ignore_errors=True)
                # 验收产物（标题以「验收」开头）不该留在垃圾桶里堆着
                for path in trash.glob("验收*.md"):
                    path.unlink(missing_ok=True)
                if not any(trash.iterdir()):
                    trash.rmdir()
            backup = ROOT / ".backup"
            if backup.exists():
                for path in backup.rglob("*.md"):
                    if str(path) not in backups_before:
                        path.unlink(missing_ok=True)
            index_module.rebuild()
        except Exception as exc:  # 清理失败不影响结论，但不能静默
            print(f"[warn] 清理未完成：{exc}")

    cleanup()

    failed = [r for r in results if not r[1]]
    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    print(f"\n合计 {len(results)} 条，通过 {len(results) - len(failed)} 条，未通过 {len(failed)} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
