# 工单 41：页面内部的移动端信息密度与控件收纳

沿用工单 40 的 `max-width: 719.98px` 断点。只修改模板、CSS 和浏览器交互；后端逻辑、路由、数据库模型保持不变。

## 文案与布局

- 首页摘要：`HH:MM 模式 · 结论`。失败优先显示 `HH:MM 同步失败 · 查看详情`，点开错误；成功时新增优先于更新，都为零显示「无变化」。同时有新增和更新只显示新增；完整计数在桌面仍保留。静态首屏取批次开始时间；即时响应无开始时间时用上海时区当前时间。没有历史批次时不显示摘要。
- 快速同步保持蓝色主按钮和图标；全盘同步移入同步行的「⋯」，复用原按钮与并发禁用状态。首页搜索保留输入框，提交按钮改为右侧放大镜；详情搜索图标展开整行输入框并聚焦，收起时清除筛选。
- 列表标题最多两行；移动端两个标签后接 `+N`，服务端渲染与异步标签更新一致。状态和创建时间在同一行右对齐。桌面仍为三个标签及原布局。
- 详情标题允许两行，取消单行省略样式；状态换到标题下方。视图切换在页头下吸顶，ResizeObserver 随标题高度和安全区调整偏移；选中态用下划线。发言人头像 28px、姓名 14px、时间戳 12px，长姓名单行省略。
- 助手标签行只保留名称、数量和「⋯」，二级缩进 20px，一级/二级字号 15/14px。菜单提供修改分类、合并到、停用；分类以单选按钮列表展示，合并在二级面板中选择并沿用原确认。一级分类的启停同样收进菜单。
- 设置标签、控件、13px 次要说明纵向排列，保存条 sticky bottom 并预留底部安全区。时间和星期控件随容器宽度伸缩。
- 热词保留标准词、命中、千问状态和「⋯」四列；推送、重推、编辑、启停、删除移入动作表，已推送按钮禁用。模型卡片保留名称、provider、默认徽标和「⋯」，原操作表单移入菜单，新增表单全宽。

## 交互与回归

统一使用原生模态 dialog 底部动作表：点击遮罩或按 Escape 关闭、焦点限制在动作表内，关闭后回到触发按钮。每项至少 48px，安全区内边距可随系统变化。操作表单保留原节点、CSRF 字段及事件监听；跨断点时恢复原桌面位置，二级合并面板也能恢复。热词推送和模型连通结果在动作表内可见。

```sh
.venv/bin/python -m pytest -q
node --test tests/home_interactions.cjs tests/detail_interactions.cjs tests/assistant_interactions.cjs tests/enrichment_interactions.cjs
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-mobile41-pages
LAYOUT_PAGES=/tmp/qwennote-mobile41-pages node --test tests/mobile_density.cjs tests/mobile_layout.cjs tests/home_layout.cjs tests/detail_layout.cjs tests/tags_layout.cjs tests/navigation_layout.cjs tests/assistant_tree_browser.cjs
```

浏览器测试使用现有 Playwright 和 Chrome；可用 `PLAYWRIGHT_MODULE`、`CHROME_PATH` 指定路径。设置 `BASELINE_PAGES` 运行桌面截图对照，`MOBILE_ARTIFACTS` 指定截图与测量输出。日期类动态测试文字统一后，9 页×明暗两种主题逐像素一致。新增测试覆盖四类首屏/即时摘要、搜索聚焦、吸顶、48px 菜单、遮罩/Escape、分类保存及失败、取消合并、跨断点恢复、推送状态和模型反馈。

验证结果：Python 1362 项，Node 交互 37 项，浏览器 46 项；桌面 18 组截图一致。移动触摸审计覆盖 68 个状态、852 个目标，无尺寸/间距/页面溢出违规。

原触摸间距测试按当前需求识别 sticky 保存条和原生模态框的独立交互层；旧三标签断言改为桌面三标签、移动端两标签。标签层级、复制、编辑、同步、导航与后台原有回归仍执行。

390px 六页截图、测试日志、HTML 报告和 release 证据保存在仓库外 `../qwennote-admin-artifacts/mobile-41-20261104/`。截图使用合成数据；安全区采用 Chrome 模拟，未声称实体手机验收。复制现有生产 release，覆盖已提交文件、复用依赖，仅执行 app-only activate；不执行 prepare，不修改 `.scratch/`、nginx 或 `/etc/`。
