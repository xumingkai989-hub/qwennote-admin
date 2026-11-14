---
name: qwennote-kb
description: Read meeting notes by tag from a QwenNote knowledge base.
---

# QwenNote Knowledge Base

QwenNote 会把会议按标签整理成 Markdown 文件，供 agent 直接读取。这个 skill 说明其数据结构与读取方式，不含任何部署信息。

适用前提：你已能访问一个运行中的 QwenNote 实例，或本地 clone 了该项目。

## 数据结构

```
knowledge/
├── index.json          全部标签的元数据
├── index.md            人读总览
├── categories/<分类>.md
└── tags/<标签>.md
```

### index.json

```json
{
  "generated_at": "2026-11-14T15:30:00+08:00",
  "source": {"recordings": 58, "tags": 21, "notes": 3},
  "categories": [
    {"name": "<分类>", "path": "categories/<分类>.md", "tags": 6, "sessions": 30, "minutes": 1420}
  ],
  "tags": [
    {
      "id": 1,
      "name": "<标签>",
      "category": "<分类>",
      "path": "tags/<标签>.md",
      "sessions": 17,
      "minutes": 860,
      "notes": 3,
      "last_session_at": "2026-11-13T10:04:00+08:00",
      "updated_at": "2026-11-14T15:30:00+08:00",
      "content_hash": "a1b2c3d4"
    }
  ]
}
```

`path` 是相对 `knowledge/` 的路径，标签名含中文与空格时需要 URL 编码。

### 标签文件

固定四段，顺序稳定：

```markdown
# <标签>

> <分类> · 17 场会议 · 860 分钟 · 3 条补充 · 更新于 2026-11-14 15:30

## 当前状态

3–5 句综述：在推进什么、进展到哪、卡在哪。

## 补充记录

- **2026-11-14 15:42** `codex` — 一条由 agent 写入的记录。

## 会议

### 2026-11-13 · <会议标题>
- 21 分钟 · 主议题：<标签> · 标签：A、B、C
- 一句话摘要。
- [查看原文](/recordings/<id>)

## 时间线

| 日期 | 类型 | 内容 |
|---|---|---|
| 11-13 | 会议 | <会议标题> |
```

解析要点：

- 「## 当前状态」之后到下一个 `##` 之间是综述正文，无子标题
- 「## 会议」下每个 `###` 是一场会议，`### ` 后是 `YYYY-MM-DD · 标题`
- 会议最多 20 场，超出时文末有 `另有 N 场更早的会议未列出。`
- 补充记录最多 20 条，按时间倒序，格式 `- **YYYY-MM-DD HH:MM** \`source\` — 正文`
- `source` 取值 `codex` / `hermes` / `cli`

## 读取方式

### 方式一：直读文件（最省事）

只需要一个主题的内容时，读 `knowledge/tags/<标签>.md` 就够了，不必走 CLI。

```bash
cat knowledge/tags/"<标签>.md"
```

先用 `knowledge/index.json` 找到 `path`，再读文件。

### 方式二：CLI

```bash
python -m app.kb list --json                    # 全部标签
python -m app.kb list --category <分类> --json    # 按分类过滤
python -m app.kb show "<标签>" --json             # 单标签全文
python -m app.kb search <关键词> --limit 5 --json  # 跨标签检索
python -m app.kb build --full                   # 重建（需数据权限）
python -m app.kb note add "<标签>" --text "…" --source codex
python -m app.kb note list "<标签>" --json
python -m app.kb note remove <id>
```

输出契约：

- `list --json` → 数组，元素同 index.json 的 `tags[]`
- `show --json` → `{"tag", "path", "sessions", "notes", "content"}`，`content` 是完整 Markdown
- `search --json` → 数组，元素 `{"tag", "category", "sessions", "notes", "path"}`
- 退出码：有结果 0，无结果 1，参数或运行错误 2

`search` 匹配范围：标签名、会议标题、补充记录正文、综述正文。

### 方式三：写入

`note add` 会立即重建该标签文件，随后 `show` 即可读到。

```bash
python -m app.kb note add "<标签>" --text "状态机卡点已确认：教师样本不足" --source codex
```

限制：单条 2000 字；只追加不可编辑；删除用 `note remove <id>`。

## 使用建议

**先读 index，再读文件。** `index.json` 告诉你有哪些标签、各自规模与最近更新时间，避免盲目遍历。

**只需要概览时读「当前状态」一段。** 它是最新状态的浓缩，够回答「现在怎么样」。要证据再往下读会议段。

**写完立刻读回。** `note add` 后跑一次 `show`，确认记录真的进了文件——派生产物与数据库的同步是这条链路上最容易出错的一环。

**注意标签名含空格与中文。** Shell 里必须加引号，URL 里需要 percent-encode。

## 常见问题

**`list` 返回空数组**：知识库尚未生成，或 `KB_ROOT` 指向了空目录。运行 `python -m app.kb build --full`。

**`show` 报标签不存在**：先用 `list --json` 拿准确名称，注意中文标点与空格。

**写入后文件没变**：`note add` 默认会重建；若加了 `--no-build`，需要手动 `build --tag <标签>`。

**CLI 报 SQLite 相关错误**：手动跑时需带上与服务一致的环境变量（`LD_LIBRARY_PATH` 指向项目自带的 sqlite 库）。生产部署通常已封装为 `scripts/kb.sh`，优先用它。
