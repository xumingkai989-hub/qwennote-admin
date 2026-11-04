# 工单 40：移动端导航、安全区和触摸目标

移动端断点为 `max-width: 719.98px`。用 `body:has(...)` 和页面既有 class 隐藏详情、设置、热词、模型及助手的应用栏，原页面头成为 `top: 0` 的 sticky 顶栏，并增加 1px 分隔线。配置页补充仅在移动端显示的返回链接；详情页沿用返回链接。

首页与搜索列表保留应用栏，用户名首字显示为圆形头像，退出登录沿用既有 POST 路由，移动到抽屉底部。桌面仍保留用户名与退出文字，抽屉新增入口隐藏。焦点循环测试以新的最后一个按钮为边界。

viewport 增加 `viewport-fit=cover`，四侧安全区由 `env(safe-area-inset-*)` 提供：页面底部、移动顶栏、抽屉以及复制提示分别预留对应空间。未使用设备判断或后端分支。

移动端实际交互盒子至少 44×44；禁用的正文待办复选框是只读标记，保持原 16px 尺寸。重点覆盖铅笔、编辑/取消、停用/合并、分类操作、下拉框、开关、文字链接和导出菜单。导航菜单、分段切换、内联编辑、分类操作和相邻正文链接留出至少 8px 间隔。热词宽表在自身容器内横向滚动。只将 input/select/textarea、正文编辑区及分类输入测量镜像设为至少 16px，按钮字号保持原值。

## 验证

使用环境已有 Playwright 和 Chrome，无新增依赖：

```sh
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-mobile40-pages
LAYOUT_PAGES=/tmp/qwennote-mobile40-pages MOBILE_ARTIFACTS=/tmp/qwennote-mobile40-results node --test tests/mobile_layout.cjs
.venv/bin/python -m pytest -q
```

必要时用 `PLAYWRIGHT_MODULE` 和 `CHROME_PATH` 指定既有安装。检查覆盖 320、390、393、719px 的首页、搜索、总结、转写、设置、热词、模型、助手和管理页，以及抽屉、导出菜单、正文编辑、分类重命名、热词编辑与相邻正文链接。逐元素 JSON 包含标签、标识、文字、矩形和字号；关闭的 details/dialog、hidden 和 inert 内容不计入可操作元素。同一浮层内相邻独立目标检查 8px 距离。

390×844 与 393×852 检查导航容器数量为 1、滚动后顶栏仍在 y=0、最后内容位于可见安全区内。393 的安全区通过 Chrome DevTools `Emulation.setSafeAreaInsetsOverride` 设置顶部 59px、底部 34px，并断言真实 computed padding；这是浏览器模拟，未声称实体 iPhone 验收。1440 检查应用导航及页面头共 2 个。

原有 Python 1358 项通过；JS 和浏览器回归包含桌面导航、抽屉焦点、复制、编辑、同步、分类及明暗模式。原铅笔尺寸断言按新要求改为移动端 44px、桌面 24px。桌面七页×两种主题的截图对照统一测试数据动态时间后逐像素一致。

截图、完整测量清单、测试日志、HTML 报告及部署证据放在仓库外 `../qwennote-admin-artifacts/mobile-40-20261104/`。生产复制当前 release、覆盖本次已提交的前端/测试/文档，复用已有依赖并执行既有 app-only 激活。跳过 prepare 安装流程，不写 `/etc/`、nginx、`.scratch/`，不改业务逻辑、路由或数据模型。
