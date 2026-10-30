# 工单 04i：详情卡片内边距验收

Chrome 实测由当前应用 TestClient 渲染的合成数据，非生产录音截图。单位均为 px；scrollWidth 为 document.documentElement.scrollWidth。首个文字为总结声明 / 转写段数，另外断言正文标题 / 发言人位置也在内边距之内。

| 视口 | 视图 | 卡片 x | 卡片宽 | 首个文字元素 x | 文字与卡片左边缘间距 | scrollWidth |
| --- | --- | --- | --- | --- | --- | --- |
| 1440 | 总结 | 176 | 1088 | 208 | 32 | 1440 |
| 1440 | 转写 | 176 | 1088 | 208 | 32 | 1440 |
| 1024 | 总结 | 16 | 992 | 48 | 32 | 1024 |
| 1024 | 转写 | 16 | 992 | 48 | 32 | 1024 |
| 390 | 总结 | 16 | 358 | 32 | 16 | 390 |
| 390 | 转写 | 16 | 358 | 32 | 16 | 390 |
| 320 | 总结 | 16 | 288 | 32 | 16 | 320 |
| 320 | 转写 | 16 | 288 | 32 | 16 | 320 |

所有视口卡片 x、宽度与主页 `.recording-list` 完全相同；1440 下 x=176、宽 1088。桌面 padding 为 28px 32px 40px，719.98px 以下为 20px 16px 28px。卡片本身未缩窄。

桌面发言人区域保持 120px（36px 头像 + 12px 间隔 + 72px 姓名），正文列继续使用 minmax(0, 1fr) 和 min-width: 0。900px 以下沿用头像、时间、姓名在第一行、正文在第二行的布局。内边距后的转写内容宽度为卡片宽减左右 padding。

表格固定布局、单元格允许长词换行；pre 保留换行并允许长行折行。浏览器附加极长 URL、英文单词、五列表格、代码块、嵌套列表和引用块，断言页面和元素均无横向溢出，且引用和列表不使用 hidden/clip 裁切。另保留 1100、900、640 视口并增加 719/720 断点检查，共 9 视口 × 3 页。

## 回归与复现

- Python：455 passed（原有 454 项保留，新增 CSS 非零左右 padding 断言 1 项）。
- JavaScript：19 passed。
- Chrome 浏览器：2 passed，详情九视口及主页明暗模式回归。
- 未改数据层、同步、.scratch、nginx 或其他站点。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-04i-layout
LAYOUT_PAGES=/tmp/qwennote-04i-layout node --test tests/detail_layout.cjs tests/home_layout.cjs
```

Chrome 测试使用已有外部 Playwright；需要时设置 PLAYWRIGHT_MODULE 和 CHROME_PATH，不新增应用依赖。截图、完整测量 JSON 和 HTML 报告位于仓库外 `../qwennote-admin-artifacts/04i-20261030/`。部署使用 `python3 scripts/deploy.py --app-only`。
