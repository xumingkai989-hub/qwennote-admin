# 看板 T4：每日生成与手动重算

设置中心新增 `board_enabled`（默认 true）与 `board_time`（默认 22:00），沿用严格 HH:MM 校验、签名 CSRF 与 settings 表的完整快照持久化。页面显示按十分钟 tick 舍入后的「下次生成看板」，关闭时显示停用状态。

## 调度与存档

`board` 每个上海自然日领取一次 scheduler_state 时段，领取提交早于模型调用；重复 tick、进程重启及同日修改时间均不重跑。关闭开关不领取时段。任务独立捕获失败，既有同步、打标、保留与增强任务逻辑不变。

每次生成当天日报与截至当天的周报。现有 `generate(scope='week', day=...)` 的 day 是统计截止日，内部自动将存档键转换为周一；调用时仍传当天，避免周二到周日被遗漏。两份存档使用 `(day, scope)` 复合键覆盖，周一的日报和周报可共存。

如果当天已有成功日报，且实际生成日期为今天、generated_at 严格晚于最后同步完成时间，则跳过整次定时生成；无同步记录时成功的今日存档也可跳过。未完成同步、无效时间或失败日报均不跳过。跳过也消耗当日领取时段。手动重算始终覆盖，不受调度领取或开关限制。

模型常见异常沿用已有固定错误码和最多两次重试；额外捕获模型调用边界的未预期异常，写入固定 `analysis_failed`，不保存异常原文。数字照常入库，两份分析均有生成机会。调度结果将分析失败标为 board failed，但 tick 不向外抛出模型异常，其他任务继续执行。

## 手动重算

顶部按钮使用 `POST /board/regenerate`。管理员及活跃白名单身份、签名 CSRF 仍使用设置页同一套校验，cookie 独立限定于 `/board`。day 可省略，默认上海今天；显式日期须为 YYYY-MM-DD。固定生成 day 与 week，成功完成请求后 302 到 `/board?day=<day>`。

非管理员、匿名及无效 CSRF 均为 403。模型失败仍 302，数字更新，分析显示「分析未生成」，页面显示「数字已更新，部分分析未生成，可稍后重新生成。」现有 `/board/generate` 保留按 scope 生成及 303，兼容 T3 的单份生成按钮。

## 验证

- Python 全量 1479 项通过（T3 1451 项 + T4 28 项）。
- JavaScript 交互 42 项通过。
- 浏览器 6 项通过：设置页含 1440/390/320 深浅色保存与校验，另有看板 5 项布局和交互回归。
- 截图、真实生成摘要、发布证据与 HTML 验收报告保存在仓库外 `../qwennote-admin-artifacts/board-t4-20261106/`。

```sh
.venv/bin/python -m pytest -q
node --test tests/*_interactions.cjs
.venv/bin/python tests/recording_edit_preview.py --port 8768
# 使用已安装 Playwright 和 Chrome：
EDIT_PREVIEW_URL=http://127.0.0.1:8768 node --test tests/settings_browser.cjs
```

发布复用现有 release 的依赖，覆盖本票提交文件后仅执行 app-only activate；不运行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`，不新增依赖。
