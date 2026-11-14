# T10 页面内可复制的 Agent 提示词

`/knowledge` 底部默认显示完整提示词和主按钮「复制提示词」。提示词由 `app/knowledge_page.py` 的 `build_agent_prompt(kb_cli)` 生成，保留约定的读取、检索、写入与读回核验规则，末行为 `<在这里写你的具体任务>`。非空 `KB_ROOT` 使用 `/opt/qwennote/current/scripts/kb.sh`；未配置或为空时使用 `python -m app.kb`，并说明在项目根目录执行。四条快捷命令沿用相同路径，收进默认折叠的「只用命令的话」。

提示词采用等宽字体，内容区域最多 22 行，超出内部滚动；长行自动换行。复制全文使用 textContent，保留换行与占位符。复制成功显示「已复制」，两秒后恢复原按钮文案，重复点击重新计时；剪贴板缺失或拒绝时聚焦、选中文本并提示手动复制。

验收：`tests/test_knowledge_page.py` 覆盖路径分支、模板转义、命令齐全、末行和折叠区；`tests/knowledge_layout.cjs` 覆盖 1440/390/320、深浅色、22 行上限、页面溢出、整段与四条命令复制、两秒复原、重复点击和两种降级路径。运行全量 pytest，并对本地样例及生产响应分别执行浏览器验收。截图和验收 HTML 保存在仓库外 `../qwennote-admin-artifacts/kb-t10-20261114/`。

发布仅归档本票提交差异，复用现有 release 依赖并运行 app-only activate；不执行 prepare，不修改 `app/kb.py`、`.scratch/`、nginx 或 `/etc/`。发布前后核验当前配置哈希和未改动的应用源码。
