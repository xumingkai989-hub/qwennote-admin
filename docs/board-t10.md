# T10：近 N 周垂直堆叠柱

只调整 `app/board_page.py` 的展示数据、`board.html` 的 SVG 和 `board.js` 的横坐标缩放。
每周一柱，最早在左；保留聚合返回的分类顺序，由下向上堆叠。
绘图区高 240px，Y 轴上限为窗口内最大周时长的 1.1 倍；柱宽为可用宽度 / N × 0.6。
五条淡网格线覆盖 0 至最大周时长，顶部保留余量。空窗口只显示零基线。

相邻周同名分类通过四边形连接，透明度 0.18；缺分类断开，存在但时长为零仍连接。
日期为水平 MM-DD、12px；总时长为 11px，柱高不足 30px 时省略，完整值仍保留在 SVG title。
缩放时按实际文字边界处理柱顶标注碰撞和边缘裁切，窄屏日期间隔显示。
390px 的 12 周柱宽约 13.7px；不引入图表库，不修改聚合、判定、饼图或按天图。
4/8/12 周链接、查询参数保留和无 JavaScript 切换沿用 T8。

## 验收

- Python：1774 passed（原有 1765 项保留，新增 9 项；横条几何断言更新为垂直柱）。
- Node 交互：43 passed。
- 浏览器：20 passed，覆盖 1440/390、明暗主题、开关 JavaScript、实际切换三种周数、窗口缩放。
- 校验柱数、cx 顺序、柱高统一比例、60% 槽宽、堆叠接缝、连接带全部顶点、缺周断带、配色、文字边界与无横向溢出。
- 原有看板卡片、按天图、饼图、键盘切换和 320/720 放大字号检查保留。
- 五张截图使用脱敏合成数据（含缺周、分类变化与不同窗口最大值）：
  `1440-4-weeks.png`、`1440-8-weeks.png`、`1440-12-weeks.png`、`390-4-weeks.png`、`390-12-weeks.png`。

产物位于仓库外 `../qwennote-admin-artifacts/board-t10-20261114/`。
部署复用当前 release 的依赖，仅覆盖本票提交文件，使用 app-only activate。
发布前后核验受保护应用模块与现有配置哈希；不运行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`。
实际 release 与线上验证结果分别保存在 `deployment.json`、`verification.json`。

```sh
.venv/bin/python -m pytest -q
node --test tests/*_interactions.cjs
.venv/bin/python tests/render_board.py /tmp/board-t10-pages
# 使用已有 Playwright/Chrome，必要时通过 PLAYWRIGHT_MODULE 和 CHROME_PATH 指定。
BOARD_PAGES=/tmp/board-t10-pages node --test tests/board_browser.cjs tests/board_allocation_browser.cjs tests/board_comparison_browser.cjs tests/board_weeks_browser.cjs
```
