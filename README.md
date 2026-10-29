# QwenNote Admin

工单 01–02：飞书登录白名单、手动同步、幂等入库与同步状态。Python 3.11 + FastAPI + Jinja2 + 标准库 sqlite3，无 ORM、前端构建或定时任务。录音列表、详情、搜索、只读 API 和后台管理由后续工单实现。

## 本地运行

```sh
uv venv --python 3.11
uv pip sync requirements.txt
cp .env.example .env
# 在本机填写配置；无千问 token 时显式使用 QWENNOTE_SOURCE=fixture。
.venv/bin/python -m uvicorn app.main:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

依赖锁定在 `requirements.txt`。更新依赖时运行 `uv pip compile pyproject.toml --extra test --output-file requirements.txt`。

| 环境变量 | 用途 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用 ID，必填 |
| `FEISHU_APP_SECRET` | 飞书应用密钥，必填 |
| `FEISHU_REDIRECT_URI` | 完整回调 URL，路径 `/auth/callback`，必填 |
| `SESSION_SECRET` | 至少 32 字节的随机签名密钥，必填 |
| `DB_PATH` | SQLite 文件位置，自动创建父目录与表，必填 |
| `ADMIN_OPEN_ID` | 首次启动加入白名单的管理员 open_id，必填 |
| `QWENNOTE_SOURCE` | `http`（默认）或显式指定 `fixture` |
| `QWENNOTE_MCP_TOKEN` | HTTP 模式必填的 Bearer 凭据；仅从环境读取，不落库 |
| `QWENNOTE_FIXTURE_DIR` | fixture 模式必填，本地 JSON 目录；示例 `./tests/fixtures/minutes` |
| `BACKFILL_DAYS` | 正整数，默认 90，包含上海时区今天和此前 89 天 |

应用只读取环境变量；`--env-file` 由 Uvicorn 将本地配置载入环境。生产使用 HTTPS 回调地址，cookie 自动带 `Secure`；本地 HTTP 不带。HTTP 模式未配置千问 token 时，`HttpMinutesSource` 构造及应用创建立即抛出明确异常，不回退到 fixture 或空列表。

## 登录与同步

- `GET /`：未登录跳飞书授权；有效会话且仍在活动白名单内显示同步状态和“更新”按钮。显示最近批次时间、running/success/failed、拉取/新增/更新计数及安全错误说明；失败可重试。时间均按 Asia/Shanghai，录音时间是笔记创建时间。
- `POST /sync`：活动白名单会话可触发，未登录或被停用返回 401，跨域 Origin 返回 403。同步完成返回 HTTP 200 和 `status/fetched/inserted/updated/started_at/finished_at/error` JSON；`error` 成功为 null。按钮通过 fetch 调用，禁用当前按钮直到请求结束，再直接更新页面文字与计数。
- 每个获准执行的同步恰好新增一行 `sync_runs`，开始为 running，结束写 success 或 failed。同一数据库旁的 `*.sync.lock` 使用非阻塞 `flock`，跨线程、应用实例及进程互斥；并发请求返回 409，不新增批次。锁文件不能在运行中删除。进程退出会释放锁，下次同步将中断的 running 批次标为 failed，再创建新批次。
- 每天的列表与每条详情通过 `MinutesSource` 注入；同一批按 `recording_id` 去重。`fetched` 是唯一录音数（含详情失败的条目），`inserted` 是实际新增数，`updated` 是已有录音业务字段发生变化的数量。完全相同的数据只写 `synced_at`，updated=0，保留已有 `owner_open_id`。
- 每条成功录音独立提交。详情失败或校验/入库失败记录安全说明、跳过该条、继续其余，整批 failed；列表失败终止后续拉取，整批 failed。已提交内容和旧数据均保留，不删除源中消失的录音。异常原文及服务商响应不写数据库或页面，避免泄漏凭据。
- 启动以 WAL 模式幂等迁移；兼容工单 01 的 NOT NULL 录音表，保留旧行并放开可空时长、总结及转写字段。管理员种子不覆盖现有姓名、停用状态或加入时间。

`GET /auth/callback` 校验非空 code 和与浏览器匹配的 state，换身份后检查白名单。成功 302 到 `/` 并写 HttpOnly、SameSite=Lax session cookie；白名单外或停用返回 403。Session 8 小时，state 10 分钟；每次首页及同步重新检查白名单。飞书失败返回通用 502 页面，不输出服务商响应或凭据。

`create_app(settings=..., feishu_auth=..., store=..., minutes_source=...)` 支持替换依赖。路由也可通过 `get_feishu_auth`、`get_store`、`get_minutes_source` 与 `dependency_overrides` 注入；迁移始终使用工厂传入的 Store。

## 数据源

`app/minutes.py` 定义 `MinutesSource` Protocol、冻结的 `RecordingMeta` 和 `RecordingDetail`。

- `HttpMinutesSource`：固定指向 `https://minutes.qwennote.cn/mcp`，在第一次读取时执行 initialize → notifications/initialized → tools/list → tools/call；支持 JSON/SSE 响应及 MCP session。使用 `list_my_minutes` 按上海日期的毫秒边界完整分页，`gmtCreate` 转为带 +08:00 的创建时间，`durationMillis` 四舍五入到整数分钟。分别调用 `get_minutes_ai_summary` 获取完整 Markdown 和 `get_minutes_transcription` 分页获取全部段落，保留发言人、文本与毫秒相对偏移。游标异常或缺工具时明确失败。
- `FixtureMinutesSource`：读取目录下的 `*.json`，每个文件含 `recordings` 数组，每条包含两个 dataclass 的字段。空日期返回空列表，目录缺失、格式错误或详情缺失明确失败。
- `tests/fakes.py` 的 `FakeMinutesSource`：可修改记录/详情，编排列表及单条失败、重复记录和阻塞，用于离线测试。

MCP 协议依据[初始化规范](https://modelcontextprotocol.io/specification/2025-03-26/basic/lifecycle)及[HTTP 传输规范](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)。工具字段依据已连接 QwenNote 工具说明；本工单没有调用真实千问 MCP，真实凭据和服务端响应尚未联调。飞书实现依据[授权码文档](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)及[用户令牌文档](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)，本工单也未连接真实飞书。

## 用 fixture 跑一次完整同步

已有飞书登录配置时，在 `.env` 设置 `QWENNOTE_SOURCE=fixture`、`QWENNOTE_FIXTURE_DIR=./tests/fixtures/minutes`，启动应用，登录后点击“更新”。第一次显示拉取 2、新增 2、更新 0；再次点击新增和更新均为 0。

也可完全离线，直接把 fixture 经过同一同步管道写入独立演示数据库，无需飞书或千问凭据：

```sh
DB_PATH=./data/fixture-demo.db QWENNOTE_FIXTURE_DIR=./tests/fixtures/minutes BACKFILL_DAYS=90 .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from app.minutes import FixtureMinutesSource
from app.store import SQLiteStore
from app.sync import run_sync

store = SQLiteStore(Path(os.environ['DB_PATH']).expanduser())
store.migrate()
source = FixtureMinutesSource(os.environ['QWENNOTE_FIXTURE_DIR'])
result = run_sync(source, store, backfill_days=int(os.environ['BACKFILL_DAYS']))
print(json.dumps(result.result(), ensure_ascii=False, indent=2))
PY
```

这是实际 SQLite 入库与批次记录，数据源为本地 JSON。重复执行验证幂等。示例日期固定为 2026-10-29；该日期需位于最近 `BACKFILL_DAYS` 天，否则按规则拉取 0 条。样例来源和脱敏说明见 [fixture 说明](tests/fixtures/minutes/README.md)。

## 测试

```sh
.venv/bin/python -m pytest -q
```

82 项测试通过。使用 TestClient、FakeMinutesSource、假 FeishuAuth、临时 SQLite 和 httpx.MockTransport；自动阻断 socket、DNS 及真实 HTTP transport，不需要环境密钥。覆盖已有登录行为、首次入库、重复同步、标题与内容变化、部分/整批失败保留旧数据、跨应用并发 409、批次一一对应、90 天边界、配置、旧表迁移、中断恢复、fixture 完整链路及 MCP 握手/JSON/SSE/分页/异常。保留 Starlette 对 AnyIO 别名的既有弃用警告。
