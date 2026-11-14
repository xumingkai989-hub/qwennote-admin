# T1：会议与标签扫描、索引生成

`app.kb.scan(store)` 在单次只读 SQLite 快照中读取会议、标签关联、词表和分类，返回规格第二节的索引结构。`build_index(store, root)` 创建根目录、`tags/`、`categories/`，写入 `index.json` 和 `index.md`，并返回索引。`build(store, root, tags=None)` 本票始终调用全量索引构建，预留标签增量参数。省略 root 时使用仓库根目录下的 `knowledge/`。

```python
from pathlib import Path
from app.kb import build
from app.store import SQLiteStore

index = build(SQLiteStore(Path('/path/to/database-copy.sqlite3')),
              Path('/path/to/output/knowledge'))
```

统计口径：

- 全部会议计入 `source.recordings`。每个标签按实际关联会议计数，同一会议可以属于多个标签和分类；同一分类内按会议 ID 去重，避免重复累计时长。NULL 时长计入场次、不计分钟。
- 保留未合并词表标签，包括未使用或已停用标签；合并历史名及别名归到当前标签。缺少词表的历史关联仍保留，`id=null`，分类为「其他」。分类沿用数据库顺序及原名称，包括空分类。
- 无标签会议生成 `id=null` 的虚拟「未分类」标签及分类。虚拟标签不计入 `source.tags`；如果已有同名真实标签，二者分别统计并分配不同路径。
- `last_session_at` 为关联会议最新的 `created_at`，按时间点比较并保留原字符串；无会议时为 null。`updated_at` 取词表更新、会议同步、会议创建和关联创建时间中的最新值，不使用构建时间。`content_hash` 对场次、分钟、最近会议时间求 SHA-256。所有 notes 均为 0。
- `slug(name)` 为纯清洗函数，删除指定非法字符、压缩空白并保留中文及大小写；空名、点号名称兜底为「未分类」。路径分配在每次扫描内独立处理大小写不敏感冲突，依序添加 `-2`、`-3`，包括与已有数字后缀的冲突。词表按 ID 排序，确保重复构建路径不变。

本票只生成索引，`tags/` 和 `categories/` 下的正文路径是后续票预留，验收检查父目录存在。没有新增数据库迁移、依赖、CLI、定时任务或 AI 调用，不修改会议、标签、看板逻辑和 `.gitignore`。

`tests/test_kb.py` 使用独立 fixture 验证 52 场/22 标签、无标签、NULL 时长、分类去重、时区比较、未使用/停用/合并/历史标签、安全文件名、目录、索引结构及隔日重建幂等。生产验收在 SQLite backup API 建立的独立副本上运行，比较构建前后的数据库逻辑摘要，保存两个索引与统计证据到仓库外交付目录。发布沿用当前 release 的运行环境，只覆盖本票提交文件，使用 app-only activate。
