# T8：对比周数可选

`GET /board?weeks=N` 接受严格的 `4`、`8`、`12`，缺省或非法值回退 8。
解析后的整数传入 `weekly_allocation(store, weeks=N, today=today)`，保持现有聚合返回结构。
历史日期仍按原有规则选择日报，周对比仍截至今天。

标题右侧使用同款分段样式的原生链接，选中项标记 `aria-pressed="true"`。
链接只替换 `weeks`，保留 `day`、重复参数、空值与编码字符；无需 JavaScript。
标题、SVG 描述、空态与错误文案均随 N 变化。

复用现有横向堆叠几何：分母为当前 N 周最大总分钟数，高度为 `N * 28 - 10`，
行距 28px、条高 14px、连接带只连接相邻周。未修改饼图、时间分配、按天柱状图或 `app/tag_time.py`。
390px 正常字号下标题与三项选择保持同一行；极窄且放大字体时标题栏可分行，避免横向溢出。

## 验收

- Python：1750 passed，含 17 个新增参数化用例；1 个现有 Starlette/anyio 弃用警告。
- Node 交互：43 passed。
- 看板浏览器：20 passed，覆盖 1440/390、明暗主题、320/720 下 200% 字号、三种周数、
  禁用 JavaScript 后实际点击切换、查询参数保留、全局比例、固定行距与相邻连接。
- 构造 12 周数据，4/8/12 窗口分别具有不同最大值，验证各窗口最长条均为 100%。
- 截图来自合成会议：`1440-4-weeks.png`、`1440-12-weeks.png`、`390-12-weeks.png`。

```sh
.venv/bin/python -m pytest -q
node --test tests/*_interactions.cjs
.venv/bin/python tests/render_board.py /tmp/board-t8-pages
# 使用环境已有 Playwright/Chrome，不新增应用依赖。
BOARD_PAGES=/tmp/board-t8-pages node --test tests/board_browser.cjs tests/board_allocation_browser.cjs tests/board_comparison_browser.cjs tests/board_weeks_browser.cjs
```

发布复用当前 release 的运行依赖，仅安装本票提交文件并核验 SHA，执行 app-only activate；
不运行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`。
截图、测试日志、HTML 验收说明与实际 release 验证记录保存在仓库外
`../qwennote-admin-artifacts/board-t8-20261114/`。
