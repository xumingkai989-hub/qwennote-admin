# 工单 05：按日期分组的卡片流

主页显示当前筛选总数、44×44 同步图标、轻量日期筛选、搜索和日期分组卡片。日期按 Asia/Shanghai 计算，组间及组内均倒序；2px 时间轴从首组圆点中心延伸至末组圆点中心。手机保留 20px 左侧留白。卡片支持两行标题、最多 120 字纯文本摘要、时长胶囊和创建时间，缺摘要或时长时省略对应节点，未转写显示灰色徽标。整卡可键盘聚焦并进入详情，保留筛选与分页。

同步继续调用原有 POST /sync。请求中禁用按钮，成功显示 1 秒对勾后刷新当前 URL；批次失败、HTTP 错误和网络错误显示原因并允许重试。删除历史同步时间、状态和计数整条。搜索结果保持原有命中内容。

## 数据边界

未改 store、分钟数据模型、MCP 源或同步流程。摘要通过当前页已有详情读取接口获取，每页额外读取最多 20 次；接口会读取详情正文，但只将 120 字摘录交给模板。此实现保留现有数据层约束，若后续需要提升大摘要列表性能，可另行增加批量摘要投影接口。

现有时长仅有整数分钟，无法还原秒数，显示例如“1小时30分”，不伪造“4秒”。没有时长时隐藏胶囊，0 显示“0分”。

## 验证

原基线 429 项 Python、14 项 JavaScript、1 项浏览器回归。完成后 **446 项 Python、19 项 JavaScript、2 项浏览器回归全部通过**，没有删除原用例；旧展示断言按新规格更新。新增覆盖跨三天排序、组内排序、上海跨日、四要素、Markdown 去标记与 120 字截断、缺省摘要与时长、筛选总数与详情链接、安全转义、同步成功和失败反馈。

浏览器实测 1440 / 390 / 320 的浅色与深色主页：无横向溢出；圆点 8px、时间轴 2px 且端点对齐、组间 28px、卡片间 12px、内边距 16px、按钮 44×44。原 04h 六视口主页/总结/转写同版心回归通过。

```sh
.venv/bin/pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-layout-05
LAYOUT_PAGES=/tmp/qwennote-layout-05 node --test tests/detail_layout.cjs tests/home_layout.cjs
```

浏览器测试使用外部 Playwright；必要时设置 `PLAYWRIGHT_MODULE` 和 `CHROME_PATH`。无新增应用依赖。部署沿用 `python3 scripts/deploy.py --app-only`，不修改 nginx、其他站点或 `.scratch/`。

生产真实数据截图、release 路径、文件哈希核验与 HTML 报告保存于仓库外 `../qwennote-admin-artifacts/05-20261030/`。截图来自生产 HTTP 响应快照，不使用合成数据，快照仅用于本地截图验收，不公开发布业务内容。
