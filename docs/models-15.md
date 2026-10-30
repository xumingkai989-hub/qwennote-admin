# 工单 15：模型中心

管理员通过 `/models` 管理标签与分析模型。Claude Code 只读区每次从 `/etc/claude/deepseek.qwennote.env` 读取 `ANTHROPIC_MODEL` 与 `ANTHROPIC_BASE_URL`，显示模型和无协议前缀端点；文件不存在、不可读或配置不完整时显示「未配置」。dotenv 禁用插值，不执行 shell，不回落进程环境，不返回认证变量。含认证信息、查询参数或片段的端点不展示。

新增模型需名称、provider（anthropic / openai / deepseek）、Base URL、模型 ID 和密码框中的 API Key。所有 provider 均使用 Anthropic Messages 协议；provider 是来源标识，openai 端点也必须提供兼容协议。Base URL 不允许内嵌认证、查询参数或片段；已有 `/v1` 时只追加 `/messages`，否则追加 `/v1/messages`。

## 密钥与数据边界

`models` 表按工单定义保存模型元信息；仅 `api_key_enc` 保存凭据密文，`key_last4` 保存展示用末四位。新增先验证、Fernet 加密，再 INSERT，明文不会进入 SQLite 或 WAL。模型 Key 至少 8 字符，防止末四位等于完整 Key。页面、新增错误和所有接口都不回显提交的 Key；失败后密码框为空。

加密依赖锁定 `cryptography==46.0.3`，使用库提供的 Fernet（AES-128-CBC + HMAC-SHA256，随机 IV 的认证加密），加密密钥只从 `/etc/qwennote/secret.key` 读取。算法依据 [Fernet 官方说明](https://cryptography.io/en/46.0.3/fernet/)。不复用会话签名密钥。密钥不进入仓库、API、页面、日志或发布包。应用内部只有执行请求时才解密；凭据对象的 repr 隐藏认证值，列表通过明确字段投影取数，不加载密文。

部署 prepare 阶段由 `scripts/prepare_model_secrets.py` 使用 `Fernet.generate_key()` 和排他创建生成密钥，owner 为 qwennote，权限 600。已有合法密钥保留；已有无效文件或 symlink 拒绝，不会悄悄轮换。只授予 qwennote 对 `/etc/qwennote` 的目录穿越 ACL，原 root:root 600 的应用 env 保持原权限。Claude env 沿用已有 root:qwennote 640 的只读权限。`secret.key` 不随 release 回滚；数据库备份恢复时需匹配原加密密钥。

## 页面与接口

所有页面和接口复用管理员、活动白名单与会话鉴权。POST 仅接受 URL 编码表单，复用会话绑定签名 CSRF，独立 HttpOnly cookie `models_csrf`，路径 `/models`。非管理员 403，未登录跳转登录；CSRF 缺失、重复、错误或其他会话均拒绝。响应禁止缓存并设置 no-referrer。

- `GET /models`：页面、只读 Claude 配置、模型列表与新增表单。
- `GET /models/data`：明确公开字段的 JSON 列表和只读 Claude 配置，无密文和完整 Key。
- `POST /models`：新增；原生表单成功 303；`Accept: application/json` 时返回 `{ok, id}`。
- `POST /models/{id}/test`：发一次 Messages 请求，`max_tokens=8`、固定最小输入、30 秒超时、禁止重定向；始终返回 JSON `{ok, id}` 或 `{ok:false,error}`。HTTP 状态、连接失败、超时与协议错误仅用固定摘要，不返回服务商响应正文、响应头或异常原文。
- `POST /models/{id}/default`：事务内清除原默认并设置当前模型，停用模型拒绝。部分唯一索引约束最多一个 `is_default=1`，CHECK 禁止默认模型为停用状态。
- `POST /models/{id}/toggle`：启用 / 停用；默认模型拒绝停用，提示先切换。
- `POST /models/{id}/delete`：删除；默认模型 409，提示先切换。页面删除先确认。

除测试外，修改成功默认返回 303，设置 `Accept: application/json` 时返回 `{ok,id}`；错误对应 400 / 404 / 409 / 503。列表仅显示 `••••abcd`，没有 Key 读取或导出接口。

打标 `run_tags` 和语义词表维护 `run_maintenance` 通过 `resolve_model(store)` 优先读取当前启用的默认模型。后续分析可共用该入口。没有默认时回退原环境配置；没有认证则跳过打标，配置解密失败也降级，录音同步正常完成。显式传入 settings 的测试与 CLI 调用保持可注入。选择默认只影响后续批次，不自动重打历史标签。

## 验证与部署

全量 **938 项 Python 测试通过**，原有 871 项保留，新增 67 项覆盖加密往返 / SQLite 与 WAL 无明文、页面与 JSON 脱敏、假 transport 测试 / 超时 / HTTP 错误、默认模型的打标与词表维护参数、env 回退、同步降级、默认删除保护、并发唯一约束、启停、鉴权 / CSRF、迁移与部署密钥幂等。沿用全套禁止真实网络 fixture。

另有 21 项 JavaScript 交互测试、1 项模型浏览器完整操作（1440px 浅色 / 390px 浅色 / 320px 深色）、4 项原有编辑 / 导出 / 设置浏览器与 8 项布局回归通过。仅更新既有浏览器测试的导航名单，加入模型中心。模型截图来自合成管理员与临时数据库，三组 provider 均为演示数据，测试请求使用假 transport；不代表真实服务商连通性验证。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/models_preview.py
# 使用本机已安装的 Playwright 与 Chrome：
node --test tests/models_browser.cjs
python3 scripts/deploy.py --app-only --log-dir ../qwennote-admin-artifacts/models-15-20261030/deploy
```

部署沿用校验归档 SHA、隔离 release、原子切换、健康检查与失败回滚，不修改 nginx、其他站点或 `.scratch/`。新增表可在回滚时保留，旧版忽略该表并继续使用环境配置；保留 `secret.key`。

本次报告、截图、测试与部署证据在仓库外 `../qwennote-admin-artifacts/models-15-20261030/`。个人技能索引已检查，此为项目功能，没有新增技能。公开授权只涵盖技能说明，本单业务报告和截图保持本地私有；Git 与 Issue 上报结果另记，不将本地文件冒充公开 URL。
