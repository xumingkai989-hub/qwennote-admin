# QwenNote Admin

把千问笔记里的会议录音同步到自建服务，可搜索、可编辑、可导出，并按热词自动校正。

## 它解决什么问题

- 录音积累后，用标题、总结和转写全文查找过去的讨论。
- 总结与转写在同一详情页切换，保留发言人和时间戳。
- 管理员可以修正转写错误，并将标准词用于后续校正。
- 将单场会议或某日记录导出给其他工具，减少重复复制。

## 功能

### 同步

- 手动同步与设置驱动的定时同步，按录音 ID 幂等入库。
- 数据库旁文件锁阻止并发同步，逐条提交并记录批次结果。

### 阅读

- 列表按上海日期分组，每页 20 条，按笔记创建时间倒序。
- 详情提供总结 / 转写双视图，支持本场转写搜索与段落复制。

### 编辑

- 管理员就地编辑总结或转写原文，可撤回到同步内容。
- 编辑历史目前仅有最新覆盖值与更新时间，不提供多版本历史列表。

### 标签

- AI 自动打标，词表每日维护，语义合并提案由管理员确认。
- AI 助手配置支持反馈指令、标签合并和停用。

### 热词

- 维护标准词，模型判断具体位置后替换，支持逐条撤销。
- 单条或批量推送标准词到千问，记录结果；上游没有删除接口。

### 导出

- Markdown 可导出总结、转写或全部，也可复制完整文档。

### 检索

- 标题、总结和转写全文搜索使用 FTS5，1–2 字查询回退 LIKE。

### 对外 API

- 只读列表、详情、每日汇总及搜索，以 API Key 鉴权并记录审计。

### 配置

- 设置中心管理同步与打标时间，模型中心管理模型和默认配置。
- 飞书登录配合活动白名单，管理员管理用户及上述配置。

## 技术栈与取舍

Python 3.11、FastAPI、SQLite、Jinja2 服务端渲染；少量原生 JavaScript 处理交互，无前端构建。
FastAPI 负责路由与依赖注入，SQLite 提供事务、WAL 和 FTS5；单机部署只需维护一个数据文件。
服务端渲染让登录、权限与模板共用 Python 逻辑，当前页面无需 React 的客户端状态体系。
当前按千条级录音判断容量，已有 1,000 条合成数据搜索基准；尚无必须使用 Postgres 的并发写入证据。
单条长转写的规模与性能验收边界见 [PRD](docs/PRD.md)，模块关系见 [架构说明](docs/ARCHITECTURE.md)。

## 快速开始

需要 Python 3.11、uv，以及支持 FTS5 trigram 的 SQLite；依赖固定在 `requirements.txt`。

```sh
git clone https://github.com/xumingkai989-hub/qwennote-admin.git
cd qwennote-admin
uv venv --python 3.11
uv pip sync requirements.txt
cp .env.example .env
chmod 600 .env
```

在本机编辑 `.env`：填写飞书应用、管理员身份和随机会话密钥；回调填写 `http://127.0.0.1:8000/auth/callback`。
用下列命令在本机生成会话密钥，直接存入 `.env`，不要提交或贴到日志：

```sh
.venv/bin/python - <<'PY'
import secrets
from pathlib import Path
p = Path('.env')
p.write_text(p.read_text().replace('SESSION_SECRET=\n', 'SESSION_SECRET=' + secrets.token_urlsafe(32) + '\n'))
PY
```

没有千问凭据时，在 `.env` 设置 `QWENNOTE_SOURCE=fixture` 和 `QWENNOTE_FIXTURE_DIR=./tests/fixtures/minutes`。
fixture 仅替代千问源，网页仍需真实飞书登录及白名单；它不是免登录演示模式。
fixture 的固定日期为 2026-10-29，须落在 `BACKFILL_DAYS` 窗口内，才会被同步。

```sh
.venv/bin/python -m uvicorn app.main:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`，登录后点击同步；重复同步相同样例，新增及更新数应为 0。
不配置模型时跳过 AI 增强，录音同步、阅读和导出仍可使用。
完全离线验证请运行测试；测试使用假飞书认证和临时数据库，不调用外部服务。

## 配置

下表逐项对应 `.env.example`；“必填”按本地 `Settings.from_env()` 入口说明。
Uvicorn 的 `--env-file` 加载本地文件；生产由 systemd 注入，应用不会自行读取仓库 `.env`。

| 变量名 | 是否必填 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_ID` | 是 | 飞书应用 ID。 |
| `FEISHU_APP_SECRET` | 是 | 飞书应用密钥，仅存本机或服务器配置。 |
| `FEISHU_REDIRECT_URI` | 是 | 完整 HTTP(S) 回调地址，路径为 `/auth/callback`。 |
| `SESSION_SECRET` | 是 | 至少 32 字节的会话签名密钥。 |
| `DB_PATH` | 是 | SQLite 路径，示例 `./data/qwennote.db`；自动建目录及迁移。 |
| `ADMIN_OPEN_ID` | 是 | 管理员 union_id 或 open_id，逗号分隔；去空白、去重，启动时加入白名单。 |
| `QWENNOTE_SOURCE` | 否 | 示例为 `mcp`；未设时代码默认 `http`；也支持显式 `fixture`。 |
| `QWENNOTE_MCP_URL` | 否 | MCP 端点，默认 `https://minutes.qwennote.cn/mcp`；仅 mcp 源可配置。 |
| `QWENNOTE_TOKEN_FILE` | mcp 时是 | OAuth JSON 文件，默认 `/opt/qwennote/data/mcp_token.json`，须可读写续期。 |
| `QWENNOTE_MCP_TOKEN` | http 时是 | 兼容 HTTP 源的 Bearer 凭据，mcp 源不用。 |
| `QWENNOTE_FIXTURE_DIR` | fixture 时是 | 本地 JSON 目录，示例 `./tests/fixtures/minutes`。 |
| `BACKFILL_DAYS` | 否 | 正整数，默认 90；运行时设置中心的持久值优先。 |
| `ANTHROPIC_AUTH_TOKEN` | 否 | 模型中心无默认模型时的 AI 凭据；未配置则跳过相应增强。 |
| `ANTHROPIC_BASE_URL` | 否 | 默认 `https://api.deepseek.com/anthropic`，请求使用 Messages 协议。 |
| `ANTHROPIC_MODEL` | 否 | 默认 `deepseek-flash`，模型中心默认配置优先。 |

会话有效期 8 小时，OAuth state 有效期 10 分钟；HTTPS 回调启用 Secure cookie。
模型中心的 Key 加密保存在数据库，独立密钥位于 `/etc/qwennote/secret.key`。
模型中心显示的 provider 类型不代表已实现各厂商全部协议；当前调用仍走 Messages 接口。
生产模型环境文件为 `/etc/claude/deepseek.qwennote.env`，不要将其放进发布包。

## 部署

目标服务器用 `<server-ip>` 表示，云实例用 `<instance-id>` 表示；不要在文档中填真实值。
采用 nginx → Uvicorn → SQLite 单机拓扑，应用仅监听 `127.0.0.1:8090`。
发布目录在 `/opt/qwennote/releases/`，`current` 指向当前版本，`previous` 用于回滚。
首次部署、域名替换、密钥权限、备份恢复及定时任务命令见 [运维手册](docs/OPERATIONS.md)。

`scripts/deploy.py` 的目标实例由 `QWENNOTE_SWAS_INSTANCE_ID` 指定，站点域名由 `QWENNOTE_DOMAIN` 指定；缺少任一变量时拒绝执行。安装脚本要求显式设置 `QWENNOTE_DOMAIN`，并自动替换 nginx 模板中的 `<domain>`，详见运维手册。

## 测试

Python 全量回归与文档专项检查：

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_docs.py -q
```

JavaScript 交互测试只使用 Node 内置测试框架，不需要浏览器：

```sh
node --test tests/home_interactions.cjs tests/detail_interactions.cjs
```

浏览器层使用仓库已有的布局 / 交互脚本，检查桌面、390px 手机和明暗模式。
它依赖环境已有的 Playwright 与 Chrome，不是 Python 全量测试的一部分，也不新增项目依赖。
例如生成脱敏页面后执行布局回归（先将 `PLAYWRIGHT_MODULE`、`CHROME_PATH` 指向本机已有安装）：

```sh
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-layout
LAYOUT_PAGES=/tmp/qwennote-layout node --test tests/home_layout.cjs tests/detail_layout.cjs tests/navigation_layout.cjs tests/tags_layout.cjs
```

Python 层用 TestClient、假数据源、MockTransport 和临时 SQLite，阻断真实网络；覆盖权限、同步、搜索及配置等逻辑。
JS 层验证同步按钮、搜索、复制和菜单等交互；浏览器层验证实际布局及完整交互路径。
文档测试核对环境变量集合、全部 Python 模块、IP 与密钥模式，防止后续修改遗漏文档。

## 目录结构

- `app/`：路由、存储、同步与 AI 逻辑，以及 Jinja2 模板和静态资源。
- `scripts/`：发布、回滚、备份、运行入口与搜索基准工具。
- `deploy/`：nginx 和 systemd 配置模板。
- `tests/`：Python 回归、Node 交互测试、浏览器检查和脱敏样例。
- `docs/`：PRD、架构、运维手册及下列历史过程记录。

## 开发记录

以下均为**过程记录，非面向使用者的说明**；使用与维护以本 README、PRD、架构和运维手册为准。

- [列表卡片（05）](docs/home-cards-05.md)
- [详情布局（04h）](docs/detail-layout-04h.md)
- [详情间距（04i）](docs/detail-padding-04i.md)
- [UI 文案审计（04g）](docs/ui-copy-audit-04g.md)
- [UI 文案原则](docs/ui-principles.md)
- [CSRF 测试抖动排查](docs/csrf-test-flake.md)
- [UI 修复（12）](docs/ui-fixes-12.md)
- [导出（13）](docs/export-13.md)
- [设置中心（14）](docs/settings-14.md)
- [模型中心（15）](docs/models-15.md)
- [热词判断（16）](docs/hotword-judge-16.md)
- [标准词（17）](docs/hotword-standard-17.md)
- [词级粒度（18）](docs/hotword-granularity-18.md)
- [上游热词（19）](docs/hotword-upstream-19.md)
- [热词交互（20）](docs/hotwords-20.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。
