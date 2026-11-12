# 第二大脑 · 认知与行动

一个纯本地、单用户的认知对齐系统。数据全部是磁盘上的 Markdown，没有账号、没有云同步、没有埋点。

它解决的不是「把资料收进来」，而是「让想法和行动对得上」：一篇文档对应一个主题，对话只能提建议，
写不写、写到哪，永远由你点那一下决定。

## 三条硬约束

1. **原文不可变**——程序不会改写你写过的句子。要改，你自己改，改动前自动留快照。
2. **模型只产补丁，不写自我流**——模型想往文档里写东西，只能给出一段 patch，等你点按钮才落地。
3. **索引可删可重建**——`.index/` 删掉不影响任何原文，重启自动重建。

## 有什么

**文档库（`/`）**
- 三栏同屏：左侧文件树 / 中间长文 / 右侧对话，两条分隔线可拖拽，宽度记在本地
- 所见即所得编辑：`# `、`- `、`1. `、`- [ ] `、`> ` 即时成型，`Ctrl+B` / `Ctrl+I` 加粗斜体
- 跨文档待办：任意文档里的 `- [ ]` 汇总到一处，勾选直接写回
- 全库检索（`Ctrl+K`）、历史版本回滚、回收站、`Ctrl+Z` 全站撤销
- 拖文件入库：`.md` / `.txt` / `.csv` / PDF / `.docx` / 图片

**认知库（`/new`）**
- 投喂书籍、PDF、Word、图片或直接说话，自动提炼成判断与框架
- 去掉口语、重复与情绪措辞，只留判断；相似内容合并，冲突并列而不覆盖
- 每条知识标来源，原文只归档不进正文
- 按领域生成可翻阅的「个人使用说明」
- 写下当前处境，生成待办；完成给反馈，新想法再回补知识库

## 跑起来

需要 Python 3.11+。

```bash
pip install -r requirements.txt
python run.py                # http://127.0.0.1:8000
python run.py --reload       # 改 src/ 或 prompts/ 自动重启
```

桌面壳（Tauri）在 `desktop/`，可选，不影响主流程。

## 模型配置

不配模型也能用：提炼走规则版，界面会如实显示「规则版」，不会假装是模型输出。

要接真实模型，在界面右上「设置」里填 Base URL / 模型名 / API Key，或直接建一个
`config.json`（**已在 `.gitignore` 里，不会入库**）：

```json
{
  "active": "deepseek",
  "models": [
    {
      "id": "deepseek",
      "label": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "api_key": "sk-..."
    }
  ]
}
```

涉及模型能力的提示词都在 `prompts/`，可以直接改，改完重启生效。

## 目录

```
src/api/        HTTP 接口（文档、对话、投喂、待办、统计）
src/core/       认知库：提炼、归一化、待办闭环
src/storage/    文件读写、frontmatter、检索索引、命名规则
web/            前端（原生 JS，无框架、无 CDN）
prompts/        提示词，只读契约
docs/           设计文档与已作废的旧口径
tests/          验收脚本
seed/           生成演示数据
```

数据目录（首次运行自动创建）：

```
records/        你的文档，一个主题一篇
vault/          认知库数据（sources 原文 / knowledge 判断）
.index/         检索索引，可删
.backup/        写盘前的快照，保留最近 50 版
```

`records/`、`vault/`、`.index/`、`.backup/`、`config.json` 都不在版本控制里——这个仓库只有代码。

## 验收

```bash
python tests/verify_v2.py     # 认知库全链路
python tests/verify_p0.py     # 文档库全链路（需本机 8010 / 8011 两个实例）
```

`verify_v2.py` 支持用 `MIRROR_V2_VAULT` 指向临时目录，跑完不碰真实数据：

```bash
MIRROR_V2_VAULT=/tmp/probe python tests/verify_v2.py
```

## 状态

个人自用的 P0：单用户、无权限体系、无并发写保护。设计取舍优先「说得清、可回滚」，
而不是「功能多」。欢迎看思路，不建议直接拿去当多人产品用。
