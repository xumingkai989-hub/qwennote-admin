# QwenNote Admin

工单 01–05：飞书登录白名单、手动同步、幂等入库、同步状态、录音列表与日期筛选、详情与 Markdown 导出、全文搜索。Python 3.11 + FastAPI + Jinja2 + 标准库 sqlite3，无 ORM、前端构建或定时任务。只读 API 和后台管理由后续工单实现。

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

- `GET /`：未登录跳飞书授权；有效会话且仍在活动白名单内显示录音列表、同步状态和“更新”按钮。显示最近批次时间、running/success/failed、拉取/新增/更新计数及安全错误说明；失败可重试。时间均按 Asia/Shanghai，录音时间是笔记创建时间。
- `POST /sync`：活动白名单会话可触发，未登录或被停用返回 401，跨域 Origin 返回 403。同步完成返回 HTTP 200 和 `status/fetched/inserted/updated/started_at/finished_at/error` JSON；`error` 成功为 null。按钮通过 fetch 调用，禁用当前按钮直到请求结束，更新状态文字并刷新当前筛选下的列表；请求失败时显示错误并允许重试。
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

175 项测试通过，其中工单 03 新增 30 项、工单 04 新增 27 项、工单 05 新增 36 项。使用 TestClient、FakeMinutesSource、假 FeishuAuth、临时 SQLite 和 httpx.MockTransport；自动阻断 socket、DNS 及真实 HTTP transport，不需要环境密钥。覆盖已有登录与同步、MCP 握手/JSON/SSE/分页/异常，以及上海跨日默认筛选、指定日期/昨天/含首尾区间、四态与时长、创建时间排序与同时间分页、20/5 分页不重复及筛选保留、空状态、非法参数、正文不进入 HTML、静态 CSS、搜索表单和退出。保留 Starlette 对 AnyIO 别名的既有弃用警告。

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
