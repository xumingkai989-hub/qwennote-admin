# T11 独立知识库命令行工具

`scripts/qwennote-kb` 是可执行 bash wrapper，解析自身软链、定位仓库并切换目录，按存在性加载环境文件和项目 SQLite 库，再 exec 项目虚拟环境的 `app.kb_cli`。`scripts/install_kb_cli.sh` 安装到 `/usr/local/bin`，缺少目录写权限时提示并使用 sudo，不覆盖同名普通文件。生产 release 安装使用稳定的 `/opt/qwennote/current/scripts/qwennote-kb` 软链。

`app/kb_cli.py` 复用既有知识库业务函数，新增统一包络、分层 help、schema、排序、有限 jq、格式选择、prompt 和 dry-run。`app/kb.py` 仅修改 `__main__` 转调；原有业务函数与 `kb.main()` 接口不变。`python -m app.kb list --json` 继续接受原参数，模块 CLI JSON 输出现在统一为 `{ok, data, meta}`，旧的直接 `kb.main()` 调用仍保留原输出。

成功退出 0；空列表退出 1，仍返回成功空集合；不存在的标签或记录返回 not_found、退出 1；参数、I/O、模型/执行错误退出 2。错误同时写 stderr，显式 md/table 格式只写一行文本。原文 show 和 prompt 默认 Markdown，其余默认 JSON；显式 `--format json` 可获取正文的 `data.content`。

jq 有限支持字段、引号字段、整数/负索引、数组展开、length/keys 管道；不用 eval、shell 或额外依赖。写操作先校验 jq 语法与参数；dry-run 在数据库初始化前返回动作计划，不建目录、不迁移、不落库、不调模型。预览不校验标签或 ID 是否存在。真实写入后重建失败返回 `error.committed` 和 `error.id`，便于先核验再补建。

知识库页面与 CLI 复用 `build_agent_prompt`，页面命令统一使用 qwennote-kb，并说明 `data.content`。只修改知识库页面文案，不改其他页面或知识库文件格式。已有项目技能同步参数及输出契约，不新增同义个人技能。

验收覆盖新旧模块入口、相对软链与异地工作目录、全局参数位置、四种错误/无结果路径、CRUD 与重建、写入已提交时失败反馈、dry-run 副作用、jq 安全边界、schema 与分层 help、原文完整性和页面提示词。真实输出与完整测试记录保存在仓库外 `../qwennote-admin-artifacts/kb-t11-20261114/`。
