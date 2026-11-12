"""种子数据生成器（文档形态）。

> **说明**：这是纯虚构的演示数据，用来让首次启动的界面不空。
> 一个主题 = 一篇长 .md：标题、段落、小标题、表格、- [ ] 待办都在同一篇里。
> 想跳过生成，先建 `records/目标/` 放任意一篇；想清空，删掉这些文件即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.storage import frontmatter, naming  # noqa: E402
from src.storage.paths import VAULT  # noqa: E402

D = "2026-11-10T09:00:00+08:00"

GOAL_WEEKLY = """# 把每周复盘变成固定动作

每周五下午留一小时，只回答三个问题：这周实际做了什么、哪件事没推动、下周只做哪一件。

上周留了时间但被临时会议占掉，说明「留一小时」这个安排太脆——要么提前锁日历，要么改成周五早上。

## 待办

- [ ] 把复盘模板写成三个固定问题
- [ ] 在日历上把周五下午标成忙碌
- [ ] 连续四周不断，再决定要不要加内容
"""

GOAL_LIBRARY = """# 建立自己的方法论库

遇到第二次同类问题，就该有一页纸的判断，而不是重新想一遍。

判断标准很简单：这件事我三个月内做过两遍以上，且第二遍没有比第一遍快，就值得写下来。

## 待办

- [ ] 先攒满十条再谈结构
- [ ] 每条都写清「什么前提下成立」
"""

GOAL_WRITING = """# 每月写一篇长文

写长文的目的不是输出，是逼自己把模糊的判断写成能被人挑错的句子。

## 待办

- [ ] 先写三篇短的，确认能坐下来写完
- [ ] 定一个固定发布日
"""

ROLE_NOTE = """# 岗位能力拆解

同一类岗位通常分成两条路：一条往深处走，解决底层工程问题；一条往场景走，把能力换成可验收的结果。两条路对人的要求差别很大，先选一条，再谈补什么。

这个判断需要一个前提：你已经在真实项目里交付过东西。没有交付经验时，谈「往哪条路走」都是空的，因为两边都觉得能做。

## 第一步：先分清自己已经在哪条路上

多数人的问题不是选错路，而是同时按两条路的标准要求自己，结果两边都不够。

| 维度 | 纵深路线 | 场景路线 |
| --- | --- | --- |
| 核心价值 | 系统在恶劣条件下也跑得稳 | 能力被换成可验收的结果 |
| 关键能力 | 底层实现、性能、稳定性 | 需求拆解、快速原型、交付节奏 |
| 适合的人 | 对「为什么稳」有执念 | 对「省了多少」更敏感 |
| 长期方向 | 技术专家 | 解决方案负责人 |

对照这张表，我目前更像第一条路，但交付经验偏少，所以真正缺的是把东西做完并让人用起来。

## 待办

- [ ] 挑一个已经做完的小工具，走完一次完整交付
- [ ] 记录每一步卡在哪
"""

REVIEW = """# 半年复盘

上半年做了三件事：换方向、补基础、把一个想法做成了能跑的东西。第三件最有价值，因为它第一次让我知道「做完」是什么感觉。

做得不好的地方：遇到不确定就换方向，换一次要重新积累两周。

## 待办

- [ ] 把「换方向」的门槛写死：至少做满一个月
"""

THOUGHT_FOCUS = """# 先窄后宽

先做窄场景更容易验证，因为成功标准清楚。宽场景看起来机会多，实际上很难知道自己是不是做对了。

这个判断的前提是：窄场景里确实有人愿意用。如果连窄场景都没人用，说明问题本身不成立，扩宽只会更慢。

后来又觉得应该先铺开再看——这两条方向相反，值得回头看一次。
"""

THOUGHT_SAMPLE = """# 样本够不够

手上的记录只覆盖了三个月，够用来发现明显问题，不够用来下结论。

如果下个月的节奏和这三个月明显不同，这个前提就不成立了。
"""

LEARN_NOTES = """# 复盘方法笔记

有效的复盘只做两件事：把事实和解释分开，把可改的和不可改的分开。

多数复盘失败在第一步——把「我认为他不在乎」当成事实写下来，后面全是错的。

## 待办

- [ ] 下次复盘只写事实，解释另起一栏
"""

EXT_DEEPWORK = """# 深度工作

长时间不被打断的块状时间，产出的质量和碎片时间不在一个量级。

## 适用前提

任务本身可以整块安排，且不需要即时响应别人。
"""

EXT_RETRO = """# 复盘的四层结构

复盘分四层：发生了什么、为什么发生、下次怎么做、怎么知道这次做对了。第四层最容易被跳过。

## 适用前提

有可回溯的过程记录，而不是只靠回忆。
"""

EXT_TOOLS = """# 工具选型笔记

选工具时先看退出成本，再看功能。退出成本高的工具，功能再好也会在半年后变成负担。

## 适用前提

（这份素材还没写适用前提，因此不参与对照）
"""

PENDING_MINUTES = """# 会议纪要待整理

上周的会议纪要先落到纸上，再决定归到哪一篇。
"""

PENDING_READING = """# 待读清单

攒了几篇想读的，先列着，读完再拆成判断。
"""

DOCS = [
    dict(dir="目标", id="goal-weekly", title="把每周复盘变成固定动作", kind="goal",
         topic=["习惯"], body=GOAL_WEEKLY),
    dict(dir="目标", id="goal-library", title="建立自己的方法论库", kind="goal",
         topic=["方法"], body=GOAL_LIBRARY),
    dict(dir="目标", id="goal-writing", title="每月写一篇长文", kind="goal",
         topic=["写作"], body=GOAL_WRITING),
    dict(dir="人生阶段总结/岗位拆解", id="doc-role", title="岗位能力拆解", kind="thought",
         target="goal-library", topic=["方法"], body=ROLE_NOTE),
    dict(dir="人生阶段总结", id="doc-review", title="半年复盘", kind="review",
         target="goal-weekly", topic=["习惯"], body=REVIEW),
    dict(dir="思考", id="doc-focus", title="先窄后宽", kind="thought",
         target="goal-library", topic=["方法"], body=THOUGHT_FOCUS),
    dict(dir="思考", id="doc-sample", title="样本够不够", kind="thought",
         target="goal-library", topic=["方法"], body=THOUGHT_SAMPLE),
    dict(dir="学习", id="doc-notes", title="复盘方法笔记", kind="thought",
         target="goal-weekly", topic=["方法"], body=LEARN_NOTES),
    dict(dir="外部素材", id="ext-deepwork", title="深度工作", stream="external",
         kind="book", target="goal-weekly", topic=["习惯"], source="《深度工作》",
         stance="experience", scope="任务本身可以整块安排，且不需要即时响应别人",
         body=EXT_DEEPWORK),
    dict(dir="外部素材", id="ext-retro", title="复盘的四层结构", stream="external",
         kind="article", target="goal-weekly", topic=["方法"],
         source="《复盘的四层结构》", stance="experience",
         scope="有可回溯的过程记录，而不是只靠回忆", body=EXT_RETRO),
    dict(dir="外部素材", id="ext-tools", title="工具选型笔记", stream="external",
         kind="book", target="goal-library", topic=["方法"],
         source="《工具选型笔记》", stance="experience", scope="", body=EXT_TOOLS),
    dict(dir="待归位", id="pending-minutes", title="会议纪要待整理", kind="todo",
         topic=["习惯"], body=PENDING_MINUTES, status="pending"),
    dict(dir="待归位", id="pending-reading", title="待读清单", kind="todo",
         topic=["方法"], body=PENDING_READING, status="pending"),
]


def _meta(doc: dict) -> dict:
    target = doc.get("target", "")
    status = doc.get("status") or ("placed" if target or doc["kind"] == "goal" else "pending")
    return {
        "id": doc["id"], "title": doc["title"],
        "stream": doc.get("stream", "self"), "kind": doc["kind"],
        "target": target, "topic": doc.get("topic", []),
        "version": 1, "confidence": 70, "status": status,
        "created": D, "updated": D,
        "source": doc.get("source", ""), "stance": doc.get("stance", ""),
        "scope": doc.get("scope", ""), "claim_relation": "",
    }


def generate(force: bool = False) -> dict:
    goal_dir = VAULT / "目标"
    if not force and goal_dir.exists() and any(goal_dir.glob("*.md")):
        return {"created": 0, "reason": "documents already present"}
    for doc in DOCS:
        directory = VAULT / doc["dir"]
        directory.mkdir(parents=True, exist_ok=True)
        filename, _ = naming.unique_path(directory, doc["title"])
        frontmatter.write_document(directory / filename, _meta(doc), doc["body"])
    return {"created": len(DOCS), "goals": 3, "self": 5, "external": 3, "pending": 2}


if __name__ == "__main__":
    print(generate(force="--force" in sys.argv))
