# 看板 T3：页面与导航

`GET /board` 使用现有会话和活跃白名单，未登录或白名单外均返回 403。默认取上海当天，`?day=YYYY-MM-DD` 切换日期；非标准日期返回 400。响应禁止缓存。

三张卡实时调用 `totals(store)`、`day_stats(store, day)` 和 `week_stats(store, monday, day)`，不使用存档的数字快照。历史页统计截止到所选日期；周范围为真实周一至该日，例如 2026-11-02 至 2026-11-06。

## 接口适配与展示

T1/T2 实际未提供 `load_report` 或 `monday_of`。本票在 `app.board` 补充 `monday_of(day)`、`load_report(store, day, scope='day')` 和 `report_days(store)`；只读存档使用现有复合主键。原统计函数与 AI 逻辑未改。

日报读取当天、周报读取所在周周一的 `analysis_md`。周报键每周只有一个，历史日的周分析可能来自该周较晚的生成；页面明确标示「本周最近一次存档」及生成时间。数字始终截止所选日，不宣称分析是历史当天的版本。存档分析按纯文本自动转义显示，缺失或失败均显示「分析未生成」。

分钟低于 60 显示分钟，其余显示一位小数小时；NULL 明细显示「时长未知」及排除占比的脚注。未知时长仍计入场次。占比条采用真实分钟比例，已知正时长最小 2px；显示整数使用最大余数法，确保非零总时长下合计 100%，避免大量会议累积舍入误差。没有已知时长时全部条宽为零，未知项占比显示破折号。

历史只查询 `scope='day'` 的最新 30 条存档日期，倒序显示，不补造没有存档的日期。有失败存档的日期仍可查看实时数字与分析占位。

## 操作与导航

管理员顶部「重新生成」调用 `POST /board/generate`，顺序生成所选日的日报与截至该日的周报；占位旁的「生成」只请求对应 scope。直接复用 `board_analysis.generate(store, scope=..., day=...)`。生成在线程池执行，表单提交期间禁用重复点击，完成后 303 回到所选日期；模型失败沿用原有脱敏存档逻辑。

POST 使用现有管理员身份规则与签名 CSRF 校验，cookie 限于 `/board`、HttpOnly、SameSite=Lax，并按部署启用 Secure。表单 token 与会话绑定，拒绝缺失、伪造、重复字段及跨会话重放。普通白名单用户可访问看板和导航，HTML 不包含生成表单或「重新生成」。

共享导航在「会议」后、「热词中心」前加入看板，沿用 `aria-current`。柱状 SVG stroke 1.7。共享导航同时用于移动端抽屉。页面使用现有主题变量、12px 圆角和 CSS 占比条，不引入图表库。390px 三卡纵向，明细第一组为标题和时长，第二组为标签和占比；长标题、长标签可换行。

## 验证与交付

- Python：1451 项（含新增 31 项页面测试）；原有统计与 AI 的 34 项测试保留。
- JavaScript 交互回归：42 项。
- 看板真实 Chrome 浏览器：5 项，覆盖 1440/390、深浅色、空状态、最小条宽、长字符串、导航抽屉和生成中状态。
- 既有浏览器回归：45 项（导航/移动端 23 项，首页/详情/标签/分类树 22 项）；含 68 个状态、856 次触摸目标观测。

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tests/render_board.py /tmp/qwennote-board-pages
BOARD_PAGES=/tmp/qwennote-board-pages node --test tests/board_browser.cjs
```

既有截图夹具增加登录同步线程隔离，避免自动同步插入额外录音并干扰标签断言；仅影响测试数据构造。

浏览器使用已有 Playwright；必要时设置 `PLAYWRIGHT_MODULE` 和 `CHROME_PATH`。截图全部采用合成会议数据，保存在仓库外 `../qwennote-admin-artifacts/board-t3-20261106/`：`1440-data-light.png`、`1440-empty.png`、`390-data-light.png`，另附深色截图、HTML 报告、原始测试日志和 release 证据。

发布复用既有依赖，复制当前 release、覆盖本次及 T1/T2 的已提交文件，只运行 app-only activate。数据库迁移沿用 T1/T2 幂等建表；不执行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`。调度接入属于后续票。
