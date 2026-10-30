# 工单 04h：详情与主页共用宽版版心

详情外框最大 1120px、固定左右 16px 页边距，与主页一致。900px / 640px 不再覆盖页边距；转写内层保留 100% 宽。总结与转写正文行高 1.75，转写段落和发言分段间距均为 24px。1440px 下正文每行较长，按要求保持容器宽度。

Chrome 实测合成数据页面（当前应用模板由 TestClient 渲染，非生产录音截图）：

| 视口 | 主页 x / width | 详情 summary x / width | 详情 transcript x / width | scrollWidth（三页） |
| --- | --- | --- | --- | --- |
| 1440 | 176 / 1088 | 176 / 1088 | 176 / 1088 | 1440 |
| 1100 | 16 / 1068 | 16 / 1068 | 16 / 1068 | 1100 |
| 900 | 16 / 868 | 16 / 868 | 16 / 868 | 900 |
| 640 | 16 / 608 | 16 / 608 | 16 / 608 | 640 |
| 390 | 16 / 358 | 16 / 358 | 16 / 358 | 390 |
| 320 | 16 / 288 | 16 / 288 | 16 / 288 | 320 |

六种视口断言：**三列 x 相等、width 相等**；三页 `scrollWidth === viewportWidth`。1440px 下 summary / transcript section 均为 1088px，`width > 1000` 通过。转写内层与 section 同宽；两种正文计算行高均为 28px。

## 回归运行

原有 428 项 Python + 14 项 JavaScript 测试保留。更新旧版心合同为新规格，新增正文可读性测试及独立 Chrome 六视口回归。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-layout
LAYOUT_PAGES=/tmp/qwennote-layout node --test tests/detail_layout.cjs
```

浏览器回归使用外部 Playwright，不增加应用运行依赖。若模块未在 Node 搜索路径，设置 `PLAYWRIGHT_MODULE` 为已安装 Playwright 路径；若无默认浏览器，设置 `CHROME_PATH` 为 Chrome 可执行文件路径。缺依赖时测试失败，不跳过。它检查六种视口的三页矩形、页面溢出、内层宽度、行高、段间距及桌面宽度下限，同时生成四张截图和 `layout-measurements.json`。
