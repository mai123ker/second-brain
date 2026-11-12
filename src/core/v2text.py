"""确定性整理规则：去口语化 / 去重 / 去情绪化 / 领域归类 / 相似度。

不依赖模型，保证没配 Key、模型报错或离线时，整条闭环依然跑得通。
模型可用时用它出更准的条目；模型不可用时这一层就是完整实现，不是占位。
"""
from __future__ import annotations

import re

DOMAIN_KEYWORDS = {
    "制度": ["制度", "规则", "规章", "机制设计", "激励", "约束", "流程", "考核", "治理", "科层"],
    "法律": ["法律", "法规", "条款", "合同", "权利", "义务", "合规", "诉讼", "判例", "立法"],
    "经济运行": ["经济", "通胀", "利率", "货币", "供需", "市场", "价格", "周期", "财政", "税收", "资本"],
    "教育": ["教育", "学习", "课程", "教学", "考试", "学生", "老师", "认知负荷", "培养", "招生"],
    "组织": ["组织", "团队", "管理", "协作", "分工", "领导", "决策链", "招聘", "绩效", "文化"],
    "用户增长": ["增长", "用户", "留存", "转化", "获客", "漏斗", "活跃", "渠道", "运营", "复购"],
}

FILLER_HEAD = re.compile(
    r"^(嗯+|啊+|额+|呃+|唉+|诶+|em+m+|那个|就是说|其实吧|怎么说呢|老实说|说真的|反正|总之吧|然后吧|我跟你讲|你知道吧)[，,、:：\s]*",
    re.I)
FILLER_MID = re.compile(
    r"(?:[，,、\s]|^)(对吧|是吧|懂吧|你懂我意思吧|怎么说呢|我觉得吧|可能吧|大概吧|说实话|讲真|反正吧)(?:[，,、\s]|$)")
FILLER_WORDS = ["那个", "就是说", "其实吧", "我觉得吧", "我觉得", "我感觉",
                "怎么说呢", "反正吧", "讲真", "说实话", "你知道吧", "我跟你讲", "然后吧"]
EMOTIONS = [
    "太气了", "气死了", "烦死了", "好烦", "烦人", "心态崩了", "崩溃了", "崩溃",
    "无语了", "真的无语", "无语", "服了", "真的服", "太爽了", "爽死了", "绝了",
    "笑死了", "笑死", "牛逼", "太牛了", "好累", "累死了", "太难了", "受不了",
    "妈的", "他妈的", "离谱", "太离谱了", "emo了", "破防了", "气人",
]
STRUCT_WORDS = ["因为", "所以", "如果", "那么", "前提", "取决于", "导致", "机制", "分为",
                "第一", "第二", "第三", "一方面", "另一方面", "约束条件", "路径", "条件",
                "结果", "本质", "换句话说", "意味着", "才能", "就会", "结构"]
STRUCT_SPLIT = re.compile(r"(因为|所以|如果|那么|但是|而|取决于|导致|前提|一方面|另一方面|意味着|才能)")
CONNECTORS = {"因为", "所以", "如果", "那么", "但是", "而", "取决于", "导致", "前提",
              "一方面", "另一方面", "意味着", "才能"}
GREETING = {"你好", "您好", "在吗", "谢谢", "多谢", "好的", "嗯", "啊", "哦", "哈喽",
            "hi", "hello", "收到", "ok", "好的吧", "拜拜", "试试", "看看"}

CODE_BLOCK = re.compile(r"```[\s\S]*?```")
MD_PREFIX = re.compile(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s*")


def _grams(text: str, n: int = 2) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fa5]", "", text or "")
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def similarity(a: str, b: str) -> float:
    """字符 2-gram 的 Sørensen–Dice；中文短句上比 Jaccard 更贴近直觉。"""
    left, right = _grams(a), _grams(b)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def containment(a: str, b: str) -> float:
    """短句被长句包含的程度，用来抓「同一句换了个说法」。"""
    left, right = _grams(a), _grams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def split_sentences(text: str):
    raw = CODE_BLOCK.sub(" ", text or "")
    for line in raw.splitlines():
        line = MD_PREFIX.sub("", line.strip()).strip()
        if not line:
            continue
        for part in re.split(r"[。！？!?；;]+", line):
            part = part.strip()
            if part:
                yield part


def normalize(text: str) -> dict:
    """把口语化文本收敛成中性陈述句，并如实报告去掉了什么。"""
    kept: list[str] = []
    filler = emotion = duplicate = 0
    for sentence in split_sentences(text):
        stripped = sentence
        for _ in range(3):
            peeled = FILLER_HEAD.sub("", stripped)
            if peeled == stripped:
                break
            stripped = peeled
        stripped = FILLER_MID.sub(" ", stripped)
        for word in FILLER_WORDS:
            stripped = stripped.replace(word, "")
        if stripped != sentence:
            filler += 1
        stripped = re.sub(r"\s+", " ", stripped).strip(" ，,、")
        for word in EMOTIONS:
            if word in stripped:
                stripped = stripped.replace(word, "").strip(" ，,、")
                emotion += 1
        stripped = re.sub(r"[，,]{2,}", "，", stripped).strip("，, 、")
        if not stripped or len(stripped) < 6 or stripped.lower() in GREETING:
            continue
        if any(containment(stripped, prev) > 0.82 or similarity(stripped, prev) > 0.9
               for prev in kept):
            duplicate += 1
            continue
        kept.append(stripped)
        if len(kept) >= 80:
            break
    return {
        "sentences": kept,
        "raw_chars": len(text or ""),
        "kept_chars": sum(len(s) for s in kept),
        "removed_filler": filler,
        "removed_emotion": emotion,
        "removed_duplicate": duplicate,
        "truncated": len(kept) >= 80,
    }


def guess_domain(text: str) -> str:
    scores = {domain: sum(text.count(word) for word in words)
              for domain, words in DOMAIN_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] else "通用认知"


def looks_like_frame(sentence: str) -> bool:
    return len(sentence) >= 14 and any(word in sentence for word in STRUCT_WORDS)


TITLE_CUT = re.compile(r"[，,。、：:；;！？!?|｜]")


def title_of(sentence: str) -> str:
    """给条目起一个能读的短标题：取第一个分句，超长在 18 字处收住并加省略号。"""
    head = TITLE_CUT.split(str(sentence or "").strip())[0].strip()
    head = re.sub(r"^(如果|因为|所以|那么|但是|而|当|在|对于|要是)\s*", "", head)
    if len(head) > 18:
        head = head[:17].rstrip("的和与就才而把被让") + "…"
    return head or str(sentence or "")[:18]


def rule_items(sentences: list[str], kind: str = "chat") -> list[dict]:
    """规则版提炼：含因果/条件/分层的进框架，其余进知识点，最多 6 条。"""
    frames, points = [], []
    absorbed = kind in ("book", "pdf", "docx", "link", "import")
    for sentence in sentences:
        pieces = [p.strip() for p in STRUCT_SPLIT.split(sentence)
                  if p and p.strip() not in CONNECTORS and len(p.strip()) > 3]
        item = {
            "type": "frame" if looks_like_frame(sentence) else "point",
            "domain": guess_domain(sentence),
            "title": title_of(sentence),
            "statement": sentence[:220],
            "structure": pieces[:5],
            "absorbed": absorbed,
            "merge_with": None,
        }
        (frames if item["type"] == "frame" else points).append(item)
    return frames[:3] + points[:3]


def rule_summary(sentences: list[str]) -> str:
    text = "；".join(sentences[:3])
    return (text[:157] + "…") if len(text) > 160 else text


def source_line(src: dict) -> str:
    return f"- {src.get('id', '')} · {src.get('title', '未命名')} · {src.get('created', '')}（{src.get('kind', 'text')}）"


def build_body(item: dict, src: dict, conflicts: list[str] | None = None) -> str:
    out = ["## 陈述", "", item["statement"], ""]
    if item.get("structure"):
        out += ["## 结构", ""] + [f"- {s}" for s in item["structure"]] + [""]
    if conflicts:
        out += ["## 待对照", "", "与既有框架可能不一致，先并列保留，不覆盖原框架：", ""]
        out += [f"- {c}" for c in conflicts] + [""]
    out += ["## 来源", "", source_line(src)]
    return "\n".join(out).rstrip() + "\n"
