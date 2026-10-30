# 工单 12：溯源清理、就地编辑与导航精简

`app.recording_content` 是共享展示管线：`strip_origin_markers(text)` 先删除完整的 `{origin:...}`，再交给 `apply_hotwords`。无闭合括号的内容保留；清理后压缩正文内连续空格、去行尾空白和空标记段落，同时保留 Markdown 缩进。详情的两个视图、Markdown 下载/复制、单条录音正文 API 和 daily Markdown 共用该投影。手工 override 仍优先于热词，但也清理标记。基础内容和 override 的存储值均不因展示而改写。

管理员铅笔入口位于卡片工具条，转写视图紧挨「搜索本场」左侧。编辑直接打开现有正文的 contenteditable，保留原有 DOM、排版及白色/深色背景；卡片用内侧 1px 主色边线提示。控件预留空间，显示对勾和取消时不改变卡片尺寸。取消还原原有 HTML，无请求；Esc 取消，Cmd/Ctrl+Enter 提交。保存仍走工单 11 的 override、diff 与可选热词确认接口。无正文时也可编辑。

总结只序列化发生变化的块，未动的标题、列表、表格、引用、代码及链接保留原 Markdown。转写保存完整原始时间戳和发言人信息，编辑期间禁用点击复制和筛选，退出后恢复。粘贴只接收纯文本。复制失败仍可全选回退文本，容器改为 pre + DOM Range；页面没有 textarea。

导航只保留会议、热词中心、AI 助手配置，原管理员可见性不变；首页搜索框和 `/search` 路由保留。

## 验证

- Python：777 passed，保留工单 11 的全部 749 项，新增 28 项；不访问外网。
- JavaScript：19 passed，保留复制、选择回退、搜索和导出菜单检查。
- 编辑浏览器：2 passed，覆盖四组普通编辑、四组富文档编辑、两个空文档编辑；1440/390/320px、明暗模式、前后尺寸一致、取消、快捷键、失败重试、询问与还原；富文档保存内容与仅替换目标词的原 Markdown 精确比对。手机缩短视口模拟键盘占用后仍能滚到最后一段；未接真实手机软键盘。
- 布局浏览器：8 passed，覆盖九档详情版心、首页、标签、导航与配置页。

命令：

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/recording_edit_preview.py
# 另一终端，使用本机已安装 Playwright/Chrome：
node --test tests/recording_edit_browser.cjs
```

截图与报告保存在仓库外 `../qwennote-admin-artifacts/ui-12-20261030/`，均使用本地合成数据。部署沿用 `python3 scripts/deploy.py --app-only`，发布目录与源码校验、原始正文哈希和服务健康结果记入该目录；不改 nginx、同步、数据层、`.scratch/` 或其他站点。
