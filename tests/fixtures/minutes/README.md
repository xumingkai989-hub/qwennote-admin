# 脱敏演示 fixture

结构参考 spec 指定的本机 `~/Documents/qwennote-test/2026-10-29/` 导出文件，已读取 `_daily.md` 与 11:34 的单条 Markdown 样例。源文件只有 Markdown，没有可直接导入的 JSON。

`2026-10-29.json` 手工整理为协议所需字段：第一条参考真实样例的 11:34 创建时间、18 分钟时长及发言人/毫秒相对偏移格式；ID、标题和正文均替换为演示内容，未包含真实姓名、业务细节、链接或凭据。秒数未提供，因此创建时间仅保留样例可确认的分钟精度。第二条为补充的无转写/无总结/时长未知示例，不声称是真实录音。

每个 JSON 文件含 `recordings` 数组，每条字段为：`recording_id`、`title`、带时区的 `created_at`、整数或 null 的 `duration_min`、字符串或 null 的 `summary_md/transcript_md`、布尔值 `has_transcript/has_summary`。没有录音绝对开始时间字段。
