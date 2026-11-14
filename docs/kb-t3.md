# T3：补充记录回写与重建保真

补充记录以 SQLite `knowledge_notes` 表为唯一来源，`knowledge/` 始终是可丢弃、可重建的派生目录。`SQLiteStore.migrate()` 幂等创建表及 `(tag_name, created_at DESC)` 索引，无标签外键，不修改会议逻辑。

`app.kb` 提供 `add_note`、`list_notes`、`remove_note`、`notes_for`。新增返回整数 ID，删除返回是否实际删除；正文和标签去除首尾空白后校验，分别限制 2000 字和 60 字，空值拒绝，正文超限抛出 `ValueError('note too long')`。source 仅接受 codex/hermes/cli，其他值归 cli。写入时间为 Asia/Shanghai ISO 时间；查询按 created_at、id 倒序，默认 20 条，可用 limit 增大范围，0 返回空列表，负数拒绝。

```python
from app import kb

note_id = kb.add_note(store, 'AI Tutor', 'T3 验收：补充记录持久化测试。', source='codex')
kb.build(store, root, tags=['AI Tutor'])  # 单标签增量
kb.build(store, root)                    # 全量；删除整个 root 后也能恢复
notes = kb.list_notes(store, 'AI Tutor', limit=100)
assert kb.remove_note(store, note_id)
kb.build(store, root, tags=['AI Tutor'])
```

每次 `build()` 先 `scan()` 读取数据库中的会议、词表和补充记录，统计真实 `source.notes` 与 `tags[].notes`。不存在于词表的回写标签追加为 `id=null`、分类「其他」的标签，不写入会议词表，保持已有标签文件路径。已有虚拟「未分类」文件沿用 T2 的两章节结构；同名独立回写标签另按 T1 重名规则分配路径。

在逐个渲染选中标签时，`build()` 显式调用 `notes_for(store, tag['name'])` 并传给 `render_tag_file`，全量和增量走相同步骤。渲染只填充既有「补充记录」段，最多 20 条，时间格式为上海时区 `YYYY-MM-DD HH:MM`，超出提示剩余数量，无记录显示「暂无补充记录。」。沿用现有 Markdown 文本转义。索引结构不变；updated_at 纳入补充时间，content_hash 纳入全部补充内容，超过展示上限的记录也参与哈希。

增量构建更新选中标签、受影响分类和全局索引；缺失文件会补建。删除 note-only 标签的最后一条记录后，清理先前索引登记的派生文件；若发生清洗文件名碰撞重新分配，也重渲染换了归属的文件。删除范围严格限定于 knowledge 下的 tags/categories 单层 Markdown 路径。

本票交付 Python API；CLI 的 `build --full` 等价于 `build(store, root)`，`build --tag NAME` 等价于 `build(store, root, tags=[NAME])`，命令行入口仍由后续 CLI 票接入。add/remove 不隐式构建目录，由调用方显式调用 build。会议、综述及时间线逻辑保持 T2 行为。

测试覆盖删掉派生目录后的三条记录全量恢复、同时间 ID 倒序、增量读取 notes_for、其他标签文件不重写、输入边界、未知标签、来源归一化、删除与计数哈希、20 条截断、上海时区及 Markdown 转义、删除最后一条与碰撞重分配。部署沿用 T2 release 依赖，仅覆盖本票文件，执行 app-only activate，不执行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`。
