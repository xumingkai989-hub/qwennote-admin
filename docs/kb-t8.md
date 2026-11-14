# T8 知识库页面与导航入口

导航位于看板之后，列表与详情均高亮。`GET /knowledge` 按生成索引的分类顺序显示标签，空分类不展示；总会议数取 source.recordings，避免多标签重复计数。标签行显示会议、分钟、补充数及上海时区最近会议日期。默认从仓库 knowledge 读取，沿用 KB_ROOT 以与 CLI、同步和调度一致。除会话白名单验证外，GET 不查询数据库。

`GET /knowledge/{tag}` 对 URL 编码后的原始标签名精确匹配索引，使用索引分配的文件路径，不自行猜测文件名；中文、空格及清洗文件名后的标签均可访问。文件路径必须位于 knowledge/tags，拒绝越界和外部符号链接。缺失标签、缺失文件或不可读正文返回现有错误模板的 404。正文复用 render_summary，保留会议链接；页面头部已呈现标题和元数据，因此隐藏 Markdown 中重复的首个标题及紧邻引用块。

登录且在活动白名单内的用户可阅读，匿名、未列入白名单和停用账户返回 403。管理员可见重建按钮和空状态的 CLI 提示。普通白名单用户 POST 重建返回 403。重建使用现有会话绑定 CSRF，在线程池执行 kb.build(store, root, None)，成功 302 返回列表；异常与部分失败返回列表顶部脱敏错误，不抛 500、不泄露供应商或服务器信息。索引缺失、JSON 损坏或结构损坏时显示「知识库尚未生成」。生成逻辑保持不变，损坏旧索引导致构建失败也按同一方式展示。

列表底部始终保留四条 Agent 示例。复制按钮使用 navigator.clipboard，拒绝或不可用时聚焦并选中命令，同时提示手动复制。命令保持单行并在代码区域内滚动，移动端复制按钮不被挤出屏幕。包装脚本 scripts/kb.sh 权限 755，仅供服务器执行；显式设置 LD_LIBRARY_PATH，切换 qwennote 用户后运行 CLI，参数原样传递。

Python 验收在 tests/test_knowledge_page.py，含真实构建往返、文件读取与数据库查询边界、分类排序、权限、会话绑定 CSRF、空状态、路径安全与错误脱敏。浏览器验收 tests/knowledge_layout.cjs 使用 LAYOUT_PAGES 中的 knowledge.html 和 knowledge-detail.html，覆盖 1440、390、320 宽度及深浅主题、复制成功及降级；可通过 PLAYWRIGHT_MODULE 和 CHROME_PATH 选择现有浏览器依赖。

本票应用发布仅覆盖已提交差异，复用前一 release 的依赖及已生成知识库，执行 app-only activate；不执行 prepare，不修改 .scratch、nginx 或 /etc。截图与真实业务验收材料存放仓库外私有目录。
