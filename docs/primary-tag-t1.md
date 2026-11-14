# T1 会议核心议题判定

`app.primary_tag` 仅负责判定和存储，不接入页面、聚合或调度器，不修改原有打标流程与 `recording_tags`。

- `fingerprint(tags)`：标签名去重、排序，以紧凑 UTF-8 JSON 编码后取 SHA-256 前 16 位。排序变化不失效，增删或改名会失效；分类调整不改变指纹。
- `judge(store, recording_id, *, model=None)`：读取标题、总结前 300 字和全部标签/分类，调用现有默认模型。`model` 与 `board_analysis` 一致，为可选模型名称覆盖。返回并 upsert 完整的 `recording_primary_tag` 行。
- `ensure_week(store, monday, today, *, model=None)`：接受 ISO 日期或 `date`，要求周一和同周截止日；沿用看板上海时区日期筛选，并排除当前时刻之后的会议。仅补判无记录或标签指纹变化的会议，包括缓存 fallback。单场错误不会阻断后续会议。

批量返回 `total / judged / model / fallback / skipped / failed / items / errors`。`judged` 为本次已写入判定数（含 fallback），`model` 与 `fallback` 为这批新判定的来源计数，`items` 包含新判定和缓存记录；存储等异常计入 `failed`，仅返回固定错误码，不能将未写入的结果计作 fallback。

模型请求通过 `board_analysis.request_text` 共用原有 `resolve_model`、`messages_url`、30 秒超时、`x-api-key`、`anthropic-version` 与关闭 thinking 的方式；原有分析重试和函数签名保持不变。主标签判定单次调用失败即持久化 fallback，不重试。空标签不解析配置、不调用模型。

返回清洗去首尾空白、引号、行首列表符，保留 `AI Tutor` 等名称内部空格。标签必须精确匹配当前会议集合且能解析到标签 ID；`未分类`、集合外名称、空/异常响应均保存为 fallback。提示要求仅输出一个标签名；若仍附带第二行明确的 `理由：` 或 `原因：`，可保存该理由，多标签/不明附文不接受。遗留未进入词表的标签仍完整送入提示，但无法关联 ID 时按 fallback 处理，不新增标签。

迁移幂等创建新表，保留冗余 `tag_name`；合并标签时历史记录仍可读，下一次补齐按指纹重判。新增表对旧 release 无调用影响，回滚可保留表。

```python
from datetime import datetime, timedelta
from app.minutes import SHANGHAI
from app.primary_tag import ensure_week
from app.store import SQLiteStore

store = SQLiteStore(copy_path)  # 验收使用 SQLite backup API 建立的独立生产库副本
store.migrate()
today = datetime.now(SHANGHAI).date()
result = ensure_week(store, today - timedelta(days=today.weekday()), today)
```

部署复用既有 release 依赖，仅覆盖本票已提交文件并执行 app-only activate，不运行 prepare。生产库副本验收使用真实模型，并比对原有表内容及二次运行缓存；本票不向生产会议写入主标签判定。测试、release、逐场副本结果及私有 HTML 说明保存在仓库外交付目录。
