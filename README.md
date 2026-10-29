# QwenNote Admin

工单 01–07：飞书登录白名单、手动同步、幂等入库、同步状态、录音列表与日期筛选、详情与 Markdown 导出、全文搜索、对外只读 API、API Key 命令行管理与审计。Python 3.11 + FastAPI + Jinja2 + 标准库 sqlite3，无 ORM、前端构建或定时任务。包含管理员白名单管理与飞书联系人搜索；部署由工单 08 处理。

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
| `ADMIN_OPEN_ID` | 唯一管理员身份（同时首次启动加入白名单），必填 |
| `QWENNOTE_SOURCE` | `http`（默认）或显式指定 `fixture` |
| `QWENNOTE_MCP_TOKEN` | HTTP 模式必填的 Bearer 凭据；仅从环境读取，不落库 |
| `QWENNOTE_FIXTURE_DIR` | fixture 模式必填，本地 JSON 目录；示例 `./tests/fixtures/minutes` |
| `BACKFILL_DAYS` | 正整数，默认 90，包含上海时区今天和此前 89 天 |

应用只读取环境变量；`--env-file` 由 Uvicorn 将本地配置载入环境。生产使用 HTTPS 回调地址，cookie 自动带 `Secure`；本地 HTTP 不带。HTTP 模式未配置千问 token 时，`HttpMinutesSource` 构造及应用创建立即抛出明确异常，不回退到 fixture 或空列表。

## 登录与同步

- `GET /`：未登录跳飞书授权；有效会话且仍在活动白名单内显示录音列表、同步状态和“更新”按钮。显示最近批次时间、running/success/failed、拉取/新增/更新计数及安全错误说明；失败可重试。时间均按 Asia/Shanghai，录音时间是笔记创建时间。
- `POST /sync`：活动白名单会话可触发，未登录返回 401，有效 session 的用户被停用返回 403，跨域 Origin 返回 403。同步完成返回 HTTP 200 和 `status/fetched/inserted/updated/started_at/finished_at/error` JSON；`error` 成功为 null。按钮通过 fetch 调用，禁用当前按钮直到请求结束，更新状态文字并刷新当前筛选下的列表；请求失败时显示错误并允许重试。
- `POST /auth/logout`：清除当前浏览器的 session cookie 并返回首页，之后重新走飞书登录。拒绝带外站 Origin 的请求。
- 每个获准执行的同步恰好新增一行 `sync_runs`，开始为 running，结束写 success 或 failed。同一数据库旁的 `*.sync.lock` 使用非阻塞 `flock`，跨线程、应用实例及进程互斥；并发请求返回 409，不新增批次。锁文件不能在运行中删除。进程退出会释放锁，下次同步将中断的 running 批次标为 failed，再创建新批次。
- 每天的列表与每条详情通过 `MinutesSource` 注入；同一批按 `recording_id` 去重。`fetched` 是唯一录音数（含详情失败的条目），`inserted` 是实际新增数，`updated` 是已有录音业务字段发生变化的数量。完全相同的数据只写 `synced_at`，updated=0，保留已有 `owner_open_id`。
- 每条成功录音独立提交。详情失败或校验/入库失败记录安全说明、跳过该条、继续其余，整批 failed；列表失败终止后续拉取，整批 failed。已提交内容和旧数据均保留，不删除源中消失的录音。异常原文及服务商响应不写数据库或页面，避免泄漏凭据。
- 启动以 WAL 模式幂等迁移；兼容工单 01 的 NOT NULL 录音表，保留旧行并放开可空时长、总结及转写字段。管理员种子不覆盖现有姓名、停用状态或加入时间。

`GET /auth/callback` 校验非空 code 和与浏览器匹配的 state，换身份后检查白名单。成功 302 到 `/` 并写 HttpOnly、SameSite=Lax session cookie；白名单外或停用返回 403。Session 8 小时，state 10 分钟；每次首页及同步重新检查白名单。飞书失败返回通用 502 页面，不输出服务商响应或凭据。

`create_app(settings=..., feishu_auth=..., store=..., minutes_source=...)` 支持替换依赖。路由也可通过 `get_feishu_auth`、`get_store`、`get_minutes_source` 与 `dependency_overrides` 注入；迁移始终使用工厂传入的 Store。

## 录音列表与筛选

登录后首页默认显示上海时区今天创建的笔记，按 `created_at DESC, recording_id DESC` 稳定排序。所有日期均包含当天 00:00 至 23:59:59.999999，存储已是 UTC+8 语义，查询和展示不再平移时间。页面明确标注“时间均为笔记创建时间，非录音开始时间”。

| URL | 筛选 |
| --- | --- |
| `/` 或 `/?page=1` | 今天 |
| `/?yesterday=1` | 昨天 |
| `/?date=2026-10-29` | 指定日期 |
| `/?from=2026-10-01&to=2026-10-29` | 含首尾日期的区间 |
| `/?date=2026-10-29&page=2` | 同一筛选的第 2 页 |

选择新日期时回到第 1 页；前后翻页保留当前筛选，刷新和分享 URL 可恢复。日期必须为 `YYYY-MM-DD`，区间两端必填且起始不晚于结束；筛选方式互斥，页码必须为正整数，错误返回可读的 400 页面。超出末页的页码显示最后一页，空列表显示“这一天没有录音”和“第 1 / 1 页”。

每页 20 条，由 SQL `COUNT(*)` 与 `LIMIT 20 OFFSET ?` 在同一读事务完成；使用创建时间索引，列表查询只选 ID、标题、创建时间、时长和两个状态标记，不读取或渲染 `summary_md`、`transcript_md`。四态依次为：两者都有→已转录（绿）、仅转写→仅转写（蓝）、仅总结→仅总结（黄）、两者都无→未转录（灰）。时长为 null 时显示“—”，0 显示“0 分钟”。

页面由 Jinja2 渲染，样式集中于 `app/static/app.css`。640px 及以下卡片为单列，较宽屏幕为两列，字号不小于 14px。标题链接到 `/recordings/{id}`；顶部搜索框按回车或点击“搜索”提交 `/search?q=...`。

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

290 项测试通过，其中工单 03 新增 30 项、工单 04 新增 27 项、工单 05 新增 36 项、工单 06 新增 63 项、工单 07 新增 52 项。使用 TestClient、FakeMinutesSource、假 FeishuAuth、临时 SQLite 和 httpx.MockTransport；自动阻断 socket、DNS 及真实 HTTP transport，不需要环境密钥。覆盖已有登录与同步、MCP 握手/JSON/SSE/分页/异常，以及上海跨日默认筛选、指定日期/昨天/含首尾区间、四态与时长、创建时间排序与同时间分页、20/5 分页不重复及筛选保留、空状态、非法参数、正文不进入 HTML、静态 CSS、搜索表单和退出。API 与 CLI 的新增覆盖见下文。保留 Starlette 对 AnyIO 别名的既有弃用警告。

工单 03 在本机 Chrome 对 TestClient 渲染的合成数据页面进行了独立布局检查（不加入 pytest 或项目依赖）：320/375/390px 下卡片单列、1280px 下两列；指定日期、区间、第 2 页与空状态均无横向溢出，实际文字最小 14px。长标题与长用户名也不撑宽页面。该检查不包含真实飞书、千问联调或实体手机。


## 详情与 Markdown 导出（工单 04）

登录后点击列表标题进入 `/recordings/{recording_id}`。返回列表链接保留日期筛选与页码，直接访问或筛选无效时回到默认列表。详情展示创建时间、时长、四态徽标及创建时间口径，总结在上，转写按时间戳、发言人和空行段落展示；缺少总结、转写或两者时分别提示。详情和下载都重新检查登录与白名单状态，未知 ID 返回 HTML 404。数据库按 `recording_id` 主键查询单条，不加载列表。

总结使用锁定的 `markdown-it-py==4.0.0`，显式设置 `html=False`，将原始 HTML 转义并阻止危险链接协议；仅该渲染器的输出在 Python 边界标为 Markup。标题、发言人、转写正文和复制用的 textarea 均由 Jinja 自动转义，不使用 `|safe`。支持标题、列表、粗体、引用和代码块。

`GET /recordings/{recording_id}.md` 下载 UTF-8 Markdown，包含 YAML frontmatter、标题、总结和转写；字符串用兼容 YAML 的 JSON 引号与转义，未知时长留空。文件名删除非法字符及控制字符，空名回退为“录音”；响应通过 RFC 5987 `filename*=UTF-8''...` 保留中文，附带百分号编码的 quoted `filename` 兼容值。导出和详情均禁止缓存。

“复制 Markdown”通过 `navigator.clipboard.writeText` 复制与下载相同的内容；浏览器不支持或拒绝剪贴板时展开只读原文并全选，提示手动复制，无前端框架。新增离线 TestClient 测试覆盖安全渲染及危险链接、转写段落、缺内容分支、登录/白名单失效、404、筛选返回、复制源、导出字段与特殊字符和文件名清洗。


## 全文搜索（工单 05）

`GET /search?q=数据看板` 搜索所有日期的标题、总结和转写；空查询（含纯空白）跳回列表。输入被视为一个连续的字面关键词，去除首尾空白；不拆词，FTS 操作符和引号也按文字处理。搜索结果复用列表卡片，显示“搜索「xxx」，共 N 条”，按创建时间倒序、同时间按 ID 倒序，每页 20 条；同一条录音命中多个字段也只显示一次。

按 Python Unicode 字符数选择查询路径：

- **至少 3 字**：FTS5 `trigram` 的 `MATCH`，整串加引号并转义内嵌引号。
- **1–2 字**：对 `recordings.search_text` 执行参数化 `LIKE '%q%'`；转义 `%`、`_`、反斜杠，按字面搜索。SQLite trigram 无法用 `MATCH` 命中两字词，因此“看板”“模考”“师资”必须使用此路径。

每张卡片分别标注“标题命中”“总结命中”“转写命中”，每个命中字段展示首个命中及前后各至多 20 字，片段内其他命中也高亮。Python 只生成文本片段与高亮标记，Jinja 自动转义每段内容后放入固定 `<mark>` 标签，不使用 `|safe`，原文及关键词里的 HTML 均不能执行。无结果显示“没有找到包含「xxx」的录音”，提示换关键词或调整日期。

可用 `/search?q=看板&from=2026-10-01&to=2026-10-29&page=2` 收窄日期；复用列表的参数校验、整日边界、分页规则，也支持指定日期和昨天。日期表单、分页及详情返回链接保留关键词与筛选。结果页“全部日期”可清除日期限制。搜索和详情均检查会话及活动白名单，响应禁止缓存。

启动时检测并补齐 `search_text` 列，回填 `title + "\n" + summary_md + "\n" + transcript_md`（null 当空串），新建 external-content FTS5 表及插入、更新、删除触发器，并重建已有数据的索引。应用 upsert 在同一事务写入源字段和 `search_text`，触发器维护索引；完全相同的数据只更新时间，不重写索引。已有工单 01/04 数据库均可直接启动升级；原录音及所有者保留。

全量重建命令（只需数据库路径，不需要飞书或千问凭据）：

```sh
DB_PATH=./data/qwennote.db .venv/bin/python -m app.rebuild_search
```

该命令从源字段重新计算全部 `search_text`，在写事务中重建 FTS 索引；可重复执行，结果不变。`.env` 的加载与应用运行方式一致：命令本身只读取环境变量。

复现离线性能测试（仅创建临时数据库，执行后自动删除）：

```sh
.venv/bin/python -m scripts.benchmark_search
```

本机 Python 3.11 / SQLite **3.54.1**，1000 条合成录音，每条约 3300 字，两种关键词各命中 100 条；每种预热 1 次后查询 100 次，测量真实 Store 查询的连接、总数、排序和首页 20 条正文读取，不含 HTTP 与 HTML 渲染：

| 路径 | 关键词 | 中位数 | P95 |
| --- | --- | --- | --- |
| FTS5 trigram MATCH | 数据看板 | 0.771 ms | 0.933 ms |
| LIKE 扫描 search_text | 看板 | 10.343 ms | 10.601 ms |

查询计划分别包含 `SCAN recordings_fts VIRTUAL TABLE INDEX 0:M1` 和 `SCAN recordings USING INDEX recordings_created_at_idx`；后者使用时间索引按顺序扫描录音，并逐条检查搜索文本。数据量和正文长度变化会影响耗时，以上仅为本机合成数据测量。

工单 05 测试先行（实现前相关测试 38 失败、46 通过），新增 36 项：中文四字/两字/单字、英数与大小写、命中字段及去重、危险 HTML 与字面操作符/通配符、片段上下文、空查询/无结果、登录与白名单、两条路径的分页/日期边界/详情返回、插入更新删除同步、重建恢复与幂等、旧库及缺失索引启动修复、重建命令缺路径。新增文件：`app/search.py`、`app/rebuild_search.py`、`scripts/benchmark_search.py`、`tests/test_search.py`。

## 对外只读 API 与 Key 管理（工单 06）

以下端点全部要求 `Authorization: Bearer <key>`，不依赖飞书会话。无日期筛选时读取全部日期；时间仍为 Asia/Shanghai 笔记创建时间。

| GET 端点 | 返回与参数 |
| --- | --- |
| `/api/v1/recordings?date=2026-10-29&limit=20&offset=0` | `total/limit/offset/items`；也支持 `from/to`，与 `date` 互斥；可只指定区间一端 |
| `/api/v1/recordings/{recording_id}` | 列表项字段加 `summary_md/transcript_md`；未知 ID 返回 404 |
| `/api/v1/daily/2026-10-29?format=json` | 默认 json，`date/count/recordings`，包含当天全部录音 |
| `/api/v1/daily/2026-10-29?format=md` | `text/markdown; charset=utf-8`；总数、总时长、清单表格及每条总结和原文 |
| `/api/v1/search?q=看板&from=2026-10-01&to=2026-10-29&limit=20` | `q/total/items`，每项含 ID、标题、创建时间及命中字段 `matched`（`title/summary/transcript`） |

列表项字段为 `recording_id/title/created_at/duration_min/status/has_transcript/has_summary`，状态码为 `complete/transcript_only/summary_only/empty`。列表和搜索默认 limit=20，允许 1–100；offset 默认 0，允许非负整数。`total` 为筛选后总数，超过末尾的 offset 返回空 items，排序为创建时间倒序、同时间按 ID 倒序。日期必须为 `YYYY-MM-DD`，区间不能倒置，参数无效返回通用 400。搜索复用工单 05 的 FTS5 trigram/短词字面 LIKE。

当天无数据仍返回 200：JSON 为 `count=0, recordings=[]`；Markdown 显示“共 0 条录音，合计 0 分钟”和“当天没有录音”。缺转写显示“（未转录）”，未知时长显示“时长未知”，合计仅累加已知时长。标题中的换行和表格分隔符经过处理，总结与转写保留原始 Markdown。

CLI 仅要求显式设置 `DB_PATH`，不读取 `.env`，不需要飞书/千问凭据。对应用所用数据库执行：

```sh
export DB_PATH=./data/qwennote.db
.venv/bin/python -m app.apikeys create --name "codex"
.venv/bin/python -m app.apikeys list
.venv/bin/python -m app.apikeys revoke 1
```

`create` 的标准输出只有一次明文 Key（`qn_` + 32 位随机十六进制字符，128 位随机熵）。`list` 仅显示 id、name、created_at、last_used_at、active/revoked；`revoke` 可重复执行且保留首次吊销时间，未知 ID 返回非零退出码。

`api_keys` 只存 SHA-256 哈希和管理字段，逐次使用 `hmac.compare_digest` 比较全部固定长度摘要，不缓存授权；CLI 吊销提交后，下一次调用立即失效。缺失、错误前缀、格式错误、未知、已吊销及重复 Authorization 头统一返回 `401 {"detail":"unauthorized"}`。正常返回的 API 响应禁止缓存。

每个 `/api/v1` 请求写一条 `api_audit(key_id,path,status_code,at)`，包含成功、401、400、404、405 及处理异常的 500。已吊销 Key 保留可识别的 key_id，未知/缺失/格式错误为 null。只记录 URL 路径，省略 query、Authorization、正文和异常原文；不记录 Key 明文。有效 Key 认证时更新 last_used_at，已吊销 Key 不更新。API 仅注册 GET 读取端点，写入方法返回 405，Bearer Key 也不能授权网页的 `/sync`。

实际 curl 验证使用独立临时 SQLite 与现有脱敏 fixture，在本机 Uvicorn 临时端口运行；测试 Key 只在内存中传给 curl，不在报告中打印。执行方式如下（`TEST_API_KEY` 为该测试库刚创建的 Key）：

```sh
curl --silent --show-error --include --header @- \
  'http://127.0.0.1:52307/api/v1/recordings?date=2026-10-29&limit=1&offset=0' \
  <<< "Authorization: Bearer ${TEST_API_KEY}"
```

实际返回 `HTTP/1.1 200 OK`，正文为：

```json
{"total":2,"limit":1,"offset":0,"items":[{"recording_id":"fixture-20261029-02","title":"示例：待转录笔记","created_at":"2026-10-29T20:17:00+08:00","duration_min":null,"status":"empty","has_transcript":false,"has_summary":false}]}
```

四种端点均为 200，缺 Key 和吊销后各为 401，共六次调用对应六行审计；响应头、服务日志、数据库均未发现明文 Key。临时服务已停止，测试库已删除；复测时应使用新测试库、Key 和实际监听端口。

工单 06 测试先行：CLI 尚未实现时 57 项初始化错误、2 项失败；完成 CLI 后、实现 API 前为 57 失败、2 通过。新增 63 项覆盖四个契约、五种失败鉴权、重复头/会话隔离、即时吊销、分页及上海日期边界、四态、完整每日导出和空状态、字面搜索及命中字段、参数错误、只读限制、500 审计、Key 哈希及日志/响应头/数据库无明文、CLI 重复迁移和错误输入。新增文件：`app/apikeys.py`、`app/public_api.py`、`tests/test_public_api.py`。


## 后台白名单管理（工单 07）

`GET /admin` 仅允许配置 `ADMIN_OPEN_ID` 对应的活动白名单用户访问，不依赖数据库角色或姓名；普通用户返回现有 error 模板的 403 页面，未登录使用现有飞书授权跳转。管理页与搜索响应均禁止缓存，并设置 `Referrer-Policy: no-referrer`。

- `GET /admin?q=姓名或邮箱`：显示至多 20 个联系人，包含姓名、部门、邮箱与“添加”按钮。姓名、部门、邮箱、查询和隐藏表单字段均经过 Jinja 自动转义；不展示手机号、性别、令牌等其他敏感字段。搜索结果只用于本次响应，不写入数据库。
- `POST /admin/users`：URL 编码表单包含 `open_id`、`name`、`avatar_url`、`csrf_token`。只保存被选中者的 ID、姓名、头像及白名单状态与加入时间。以 `open_id` 为唯一主键幂等 upsert；重复添加保留原加入时间，已移除者再次添加恢复启用。
- `POST /admin/users/{open_id}/remove`：校验管理员及 CSRF 后将 `is_active` 设为 0，保留加入时间与停用状态。现有 session 在下次访问首页、搜索、详情、下载或同步时返回 403；重新登录也不能绕过白名单。管理员种子不会在重启时重新启用被停用的记录。
- 白名单展示全部已添加用户的姓名、截断为前 8 位加 `…` 的 open_id、加入时间和启用/停用状态，`title` 属性提供完整 open_id。添加与移除成功均 303 返回管理页。

**CSRF 方案：签名双提交 cookie。** 复用 `SignedCookie`，独立 `admin-csrf` salt，token 绑定当前已签名 session 的 SHA-256 摘要并附带随机值（不携带 session 本体），最长有效期 8 小时。管理页设置 HttpOnly、SameSite=Lax、Path=/admin cookie，HTTPS 配置下添加 Secure；表单包含相同 token。两个管理 POST 必须通过签名、时效、session 绑定和恒定时间比对，缺失、伪造、过期、重复字段或其他 session 的 token 返回 403，GET 不执行写操作。此方案无需依赖反向代理转发 Origin 的方式，普通 HTML 表单不带 Origin 时也能严格验证。

`HttpFeishuAuth.search_users(query)` 获取企业自建应用的 tenant_access_token，调用飞书 [Directory 搜索员工接口](https://open.feishu.cn/document/directory-v1/employee/search)，明确指定 `employee_id_type=open_id`、`page_size=20`，不追随下一页。该接口支持应用身份；旧版 `search/v1/user` 要求用户令牌，不适用于仅配置 App ID/Secret 的服务。仅请求 ID、姓名、头像、部门、邮箱字段，并将响应转换为 `FeishuUser`；扩展的部门和邮箱仅供临时展示，不写 users 表。

真实应用需开通 `directory:employee:search`，并按[官方接口文档](https://open.feishu.cn/document/directory-v1/employee/search)授权姓名、头像、部门、邮箱字段及所需通讯录数据范围。凭据缺失时适配器抛出明确的 `FeishuSearchNotConfigured`，页面显示“通讯录搜索未配置”；权限、网络、HTTP 或响应格式失败均显示可读错误，不输出提供方原始响应，不伪装成空结果。`Settings.from_env` 沿用启动时必填环境变量校验。测试只使用假 FeishuAuth 和 httpx.MockTransport，未进行真实租户联调。

工单 07 新增 52 项离线测试，覆盖管理员边界、搜索不落库、敏感字段过滤与 HTML 转义、添加后完整 OAuth 回调、重复添加与恢复、旧 session 撤权、POST/CSRF（缺失、伪造、过期、跨 session、重复字段、Secure cookie）、未配置提示，以及真实适配器的应用 token、open_id、20 人上限、空结果与两阶段错误。已有停用用户测试按本工单要求更新为明确的 403。
