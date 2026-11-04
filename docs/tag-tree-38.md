# 工单 38：标签词表树形列表

`/assistant` 分类始终展开，以标题和 `ul/li` 展示标签；不再重复表头。一级标题 15px/600，标签 14px/400、左缩进 28px；桌面普通行高 44px，分类间距 28px、标题与列表间距 8px。窄屏把合并和停用操作放在第二行，保留同一缩进。

只有 24px 铅笔按钮进入内联编辑，标题文字不可编辑。输入框随内容宽度变化，Enter/失焦保存，Esc 取消，中文输入法确认不会提前提交。复用既有带 CSRF 的重命名 POST 和重定向页面，验证返回分类名后更新标题、下拉项和最近变更；失败恢复原文并使用页面顶部原错误位置提示。分类顺序继续使用后端 position 排序，移动标签后按使用次数降序、ID 升序插入目标分类。

保留分类新增、启停、标签分类即时保存、合并确认、标签停用、最近变更与反馈指令。数据模型、后端路由、列表卡片与详情模板未改。CSS 和 JS 资源版本同步更新到 `20261104-38`。

## 验证

- Python 全量测试：1358 项（原 1356 项保留，增加树形渲染和重命名重定向合同测试）。
- Node 交互测试：`node --test tests/*_interactions.cjs`。
- 真实浏览器：`node --test tests/assistant_tree_browser.cjs`，通过临时 SQLite 和本地假 OAuth 登录，分类移动和重命名走原后端接口。覆盖 Enter、失焦、Esc、点击标题、输入框自适应、中文输入法、重复/空名称、网络/CSRF/忙碌/登录页错误，以及 1440/390/320px 明暗模式布局。
- 运行浏览器测试前可设置 `PLAYWRIGHT_MODULE` 与 `CHROME_PATH` 指向本机已有安装，无新增项目依赖。`ASSISTANT_SCREENSHOTS` 可指定三张截图的输出目录，默认不保存截图。截图样本为测试数据。
- 导航、合并确认和原列表标签样式回归继续使用 `tests/render_detail_layout.py`、`tests/navigation_layout.cjs` 与 `tests/tags_layout.cjs`。

发布使用独立 release、现有依赖和应用激活流程；上线前备份数据库，保留 previous 供回退，并比对配置文件哈希。禁止修改 `.scratch/`、nginx 或 `/etc/`。
