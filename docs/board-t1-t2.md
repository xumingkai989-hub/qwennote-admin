# 看板 T1+T2：统计与分析

本票提供统计、存档和 CLI；页面与调度接入属于后续票。本次只在 `store.migrate()` 增加 `board_reports`，未改同步、打标或热词行为。

## 接口与日期

- `app.board.totals(store)` 返回 `{count, minutes}`。
- `day_stats(store, day)` 返回数字和 `items`；每条包含 `recording_id/title/minutes/created_at/tags/summary_head`。时间为上海 `HH:MM`，总结截取前 120 字，按实际创建时间正序，同时间以录音 ID 稳定排序。
- `week_stats(store, monday, today)` 使用调用方明确给定的周一，截止日必须处于同一周；返回数字和 `by_day`。无记录时为空列表；有记录时填满周一至截止日，空白日为零。
- 日期参数支持 `date` 或 ISO 日期字符串。录音的带时区时间先转 `SHANGHAI`，无时区历史值按上海本地时间解释。按 `created_at` 归属，不按同步日期归属。
- NULL 时长计入场次，不加入总分钟；明细保留 `None`，后续页面应展示「时长未知」。总结与标签直接读同步总结及现有标签，不触发打标、覆盖或热词处理。

`app.board_analysis.generate(store, *, scope, day, model=None)` 生成并写库；`scope` 为 `day/week`，`day` 始终表示统计截止日。日报存档键是当天，周报存档键是所在周的周一。`model` 可覆盖请求的模型 ID，端点与凭据仍由 `resolve_model(store)` 统一解析。

返回 `{day, scope, stats, analysis, model, generated_at, error}`；`generated_at` 是上海时区 ISO 时间。`stats` 序列化为 `stats_json`，`analysis` 保存到 `analysis_md`。复合主键 `(day, scope)` 允许周一的日报与周报并存；UPSERT 覆盖全部快照字段，失败重生成会清除旧分析。

## 模型与边界

`build_input()` 将会议标题、时长、前 300 字总结和标签组成 JSON 行。system 提示词与 user 内容合计不超过 8000 字（包括标题、标签、日期和遗漏说明）。未超限时保持时间顺序，超限后按时长降序选取完整会议，未知时长放最后；为「（另有 N 场未纳入）」保留空间。超大单条无法装入时也计入遗漏数，快照数字始终覆盖完整范围。

提示词要求 3–5 句中文，先描述主要时间去向，再解释工作关系，末句总结性质，禁止编造、列表、标题与建议。`clean_analysis()` 去除标题行、列表前缀及多余空白，保留最多五句；清洗后不足三句或不含中文的输出记为 `invalid_output`。会议材料作为数据处理，不遵循材料中的指令。

模型调用使用既有 Messages URL、`x-api-key`、`anthropic-version` 和 httpx。每次超时 30 秒，首次失败后最多重试两次，等待 1 秒、3 秒；无可用模型直接返回 `model_unavailable`。HTTP、网络、响应解析和空输出只记录固定错误码及 HTTP 状态码，不保存异常文本、URL、凭据或响应正文。空会议范围不调用模型，分析与错误均为空。

`build_report()` 与 `generate()` 使用相同的数据和模型逻辑，前者不写库，供 CLI 预览使用。

## CLI

采用 `python -m app.board`，因为统计函数与单次执行共用同一模块，无需另设转发脚本。需使用项目 Python 环境，数据库通过 `DB_PATH` 或显式 `--db` 提供；不要求飞书登录配置。

```sh
DB_PATH=/path/to/qwennote.db .venv/bin/python -m app.board --day 2026-11-06
.venv/bin/python -m app.board --db /tmp/board-copy.db --day 2026-11-06 --print
.venv/bin/python -m app.board --db /tmp/board-copy.db --day 2026-11-06 --print --no-analysis
```

省略 `--day` 时在运行时读取上海今天，同时输出 day/week 两份和 totals。普通执行先幂等迁移再写两份存档；`--print` 不迁移、不写报告，仍可生成分析。`--no-analysis` 仅配合 `--print`，完全跳过模型配置读取和网络调用，输出 `analysis: null, error: "analysis_skipped"`，用于生产数据库副本离线核对。输出为单个 JSON 对象，成功生成数字时退出码为 0，模型是否成功由报告 `error` 判断。

生产验收必须通过 SQLite backup 从只读连接复制到 `/tmp`，仅操作副本，不对生产库执行迁移。本票验收使用 `--print --no-analysis`；AI 成功、失败、重试与清洗均由 MockTransport 验证，不调用生产模型。
