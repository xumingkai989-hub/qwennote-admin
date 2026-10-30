# QwenNote Admin

工单 01–08：飞书登录白名单、手动同步、幂等入库、同步状态、录音列表、详情与 Markdown 导出、全文搜索、对外只读 API、API Key 命令行管理与审计，以及 SWAS HTTPS 部署。Python 3.11 + FastAPI + Jinja2 + 标准库 sqlite3，无 ORM、前端构建。包含管理员白名单管理与飞书联系人搜索；生产定时任务使用 systemd。

后续所有 UI 工单须遵循 [UI 文案原则：让布局自己说话](docs/ui-principles.md)。全站逐条审计见 [工单 04g 文案表](docs/ui-copy-audit-04g.md)。

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
| `ADMIN_OPEN_ID` | 管理员 ID（union_id 或 open_id），支持逗号分隔多值（如 `on_a,ou_b`）；各项去空白、忽略空项与重复项，启动时均加入白名单，必填 |
| `QWENNOTE_SOURCE` | `http`（默认）或显式指定 `fixture` |
| `QWENNOTE_MCP_TOKEN` | HTTP 模式必填的 Bearer 凭据；仅从环境读取，不落库 |
| `QWENNOTE_FIXTURE_DIR` | fixture 模式必填，本地 JSON 目录；示例 `./tests/fixtures/minutes` |
| `BACKFILL_DAYS` | 正整数，默认 90，包含上海时区今天和此前 89 天 |

应用只读取环境变量；`--env-file` 由 Uvicorn 将本地配置载入环境。生产使用 HTTPS 回调地址，cookie 自动带 `Secure`；本地 HTTP 不带。HTTP 模式未配置千问 token 时，`HttpMinutesSource` 构造及应用创建立即抛出明确异常，不回退到 fixture 或空列表。

## 登录与同步

- `GET /`：未登录跳飞书授权；有效会话且仍在活动白名单内显示日期分组卡片流和“同步”图标按钮。成功显示 1 秒对勾后刷新当前 URL；失败显示一行安全错误说明，可再次点击同步重试。页面不再显示最近批次时间、状态或计数。时间均按 Asia/Shanghai，录音时间是笔记创建时间。
- `POST /sync`：活动白名单会话可触发，未登录返回 401，有效 session 的用户被停用返回 403，跨域 Origin 返回 403。同步完成返回 HTTP 200 和 `status/fetched/inserted/updated/started_at/finished_at/error` JSON；`error` 成功为 null。按钮通过 fetch 调用，禁用当前按钮直到请求结束，在按钮内反馈结果并刷新当前列表；请求失败时显示错误并允许重试。
- `POST /auth/logout`：清除当前浏览器的 session cookie 并返回首页，之后重新走飞书登录。拒绝带外站 Origin 的请求。
- 每个获准执行的同步恰好新增一行 `sync_runs`，开始为 running，结束写 success 或 failed。同一数据库旁的 `*.sync.lock` 使用非阻塞 `flock`，跨线程、应用实例及进程互斥；并发请求返回 409，不新增批次。锁文件不能在运行中删除。进程退出会释放锁，下次同步将中断的 running 批次标为 failed，再创建新批次。
- 每天的列表与每条详情通过 `MinutesSource` 注入；同一批按 `recording_id` 去重。`fetched` 是唯一录音数（含详情失败的条目），`inserted` 是实际新增数，`updated` 是已有录音业务字段发生变化的数量。完全相同的数据只写 `synced_at`，updated=0，保留已有 `owner_open_id`。
- 每条成功录音独立提交。详情失败或校验/入库失败记录安全说明、跳过该条、继续其余，整批 failed；列表失败终止后续拉取，整批 failed。已提交内容和旧数据均保留，不删除源中消失的录音。异常原文及服务商响应不写数据库或页面，避免泄漏凭据。
- 启动以 WAL 模式幂等迁移；兼容工单 01 的 NOT NULL 录音表，保留旧行并放开可空时长、总结及转写字段。管理员种子不覆盖现有姓名、停用状态或加入时间。

`GET /auth/callback` 校验非空 code 和与浏览器匹配的 state，换身份后检查白名单。成功 302 到 `/` 并写 HttpOnly、SameSite=Lax session cookie；白名单外或停用返回 403。Session 8 小时，state 10 分钟；每次首页及同步重新检查白名单。飞书失败返回通用 502 页面，不输出服务商响应或凭据。

登录同时保留飞书返回的 `union_id`、`open_id`，规范身份为 `subject = union_id or open_id`。`users.subject` 为主键，`union_id`、`open_id` 为可空别名；两个返回 ID 分别与这三列交叉匹配，任一启用记录命中即授权。旧表自动迁移：原 `open_id` 同时复制到 `subject` 和 `open_id`，保留姓名、头像、启用状态与加入时间，不推测历史 ID 类型。历史/配置行的 subject 保持稳定，成功登录补齐别名；session 使用命中的白名单 subject。新建用户优先用 union_id 作 subject。启动时逐个检查 `ADMIN_OPEN_ID` 的任意格式 ID，已有记录跳过（包括停用记录），新记录只填 subject。

白名单外或停用的 OAuth 回调 403 页面展示飞书姓名、union_id 和 open_id，缺失 ID 显示“未返回”；姓名与 ID 均自动 HTML 转义。拒绝时记录一条 WARNING，包含 union_id、open_id、name（使用 repr 转义换行，避免伪造日志行），不输出授权码、token、请求头或 cookie。403 响应禁止缓存并禁用 Referer 转发。

`create_app(settings=..., feishu_auth=..., store=..., minutes_source=...)` 支持替换依赖。路由也可通过 `get_feishu_auth`、`get_store`、`get_minutes_source` 与 `dependency_overrides` 注入；迁移始终使用工厂传入的 Store。

## 录音列表

登录后首页显示全部笔记，按 `created_at DESC, recording_id DESC` 稳定排序。`GET /` 与 `GET /search?q=...` 忽略 `from`、`to`、`date` 和旧 `yesterday` 参数（包括空值、非法值及冲突组合），直接返回全量列表或全日期关键词匹配，不重定向、不报日期错误。页码仍须为正整数，超出末页时显示最后一页。

工单 05c 删除日期输入框、“筛选 / 清除”按钮、当前筛选提示和日期专用样式；搜索、同步、日期分组、卡片、四态标签与分页保留。分页和新生成的详情链接仅保留关键词与页码。服务端 `list_filters(..., include_dates=True)` 的日期解析路径及数据层日期查询接口保留，以便将来恢复；首页和搜索页显式关闭日期解析。

全部无数据时显示“还没有同步任何录音”和“第 1 / 1 页”。列表底部保留 12px 灰字“时间为笔记创建时间”。时间按 Asia/Shanghai 展示。

每页 20 条，由现有 SQL `COUNT(*)` 与 `LIMIT 20 OFFSET ?` 在同一读事务完成。主页展示层按 Asia/Shanghai 本地日期分组，日期与组内记录均倒序，同一时刻保持 ID 倒序。为了保留数据层接口，展示层仅为当前页有总结的记录调用现有 `get_recording`（最多 20 次），将 Markdown 转为前 120 字纯文本摘录；完整正文与转写不传入模板。

每张卡片显示标题、可选摘要、可选时长胶囊和创建时间；底部行左侧按现有四态显示“已转录 / 仅转写 / 仅总结 / 未转录”，右侧保留创建时间。现有数据只保存整数分钟，胶囊据实显示“录音 · 1小时30分”，不补造秒数；null 不显示胶囊，0 显示“录音 · 0分”。不实现播放或其他分类标签。搜索结果保留原有命中高亮和四态徽标。

页面由 Jinja2 渲染，样式集中于 `app/static/app.css`。主页为单列日期时间轴卡片，手机端保留左侧 20px 时间轴留白；明暗模式共用 1120px 版心与 16px 页边距。整卡链接到 `/recordings/{id}` 并保留页码；搜索框提交 `/search?q=...`。测试与验收说明见 [工单 05](docs/home-cards-05.md)。

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

334 项测试通过（工单 03c 保留原有 331 项，新增 3 项：跨天完整时间、乱序数据的创建时间与 ID 倒序、Apple 分组与深色 CSS 标记）。使用 TestClient、FakeMinutesSource、假 FeishuAuth、临时 SQLite 和 httpx.MockTransport；自动阻断 socket、DNS 及真实 HTTP transport，不需要环境密钥。覆盖已有登录与同步、MCP 握手/JSON/SSE/分页/异常，以及上海跨日默认筛选、指定日期/昨天/含首尾区间、四态与时长、创建时间排序与同时间分页、20/5 分页不重复及筛选保留、空状态、非法参数、正文不进入 HTML、静态 CSS、搜索表单和退出。API 与 CLI 的新增覆盖见下文。保留 Starlette 对 AnyIO 别名的既有弃用警告。

工单 03c 使用系统字体和纯 CSS 实现 Apple 风格：34px Large Title 随页面滚出，紧凑品牌栏固定；12px 圆角 inset grouped 列表、44px 桌面行、日期分段控件与四态胶囊徽标；`prefers-color-scheme: dark` 提供独立深色色值。列表时间始终为 `MM-DD HH:MM`，详情保留 `YYYY-MM-DD HH:MM`，时间口径均为笔记创建时间，排序沿用 `created_at DESC, recording_id DESC`。原有日期区间筛选收在可展开的「日期区间」中，选中区间时自动展开。

本机 Chrome 对 TestClient 渲染的跨天合成数据进行了独立布局检查（不加入 pytest 或项目依赖）：1440px 浅色/深色、720px 桌面边界、390px 浅色和 320px 深色均无横向溢出；手机标题最多两行，交互目标至少 44×44px，键盘焦点描边 2px。另核对详情时间、空状态和第 2 页。三张交付截图来自合成数据页面，不包含生产业务数据，也不代表真实飞书、千问联调或实体手机验收。


## 详情与 Markdown 导出（工单 04）

登录后点击列表标题进入 `/recordings/{recording_id}`。返回列表链接保留日期筛选与页码，直接访问或筛选无效时回到默认列表。详情通过吸顶紧凑头部展示返回、标题、状态徽标和导出菜单，标题下方为一行创建时间与时长；时间口径说明只保留在列表页。头部下方通过分段控件切换总结 / 转写原文，间距 12px，手机满宽、触控高度 44px。`?view=summary` / `?view=transcript` 由服务端只渲染当前视图；默认优先总结，仅有转写时默认转写，两者皆无时默认总结，非法 view 回落默认。切换保留来源筛选与搜索参数，支持刷新、分享及浏览器前进后退。缺内容时显示“这条录音还没有 AI 总结”或“这条录音还没有转写内容”，对应选项灰显但仍可点击。转写按原有时间戳、发言人和空行段落展示。详情和下载都重新检查登录与白名单状态，未知 ID 返回 HTML 404。数据库按 `recording_id` 主键查询单条，不加载列表。

总结使用锁定的 `markdown-it-py==4.0.0`，显式设置 `html=False`，将原始 HTML 转义并阻止危险链接协议；仅该渲染器的输出在 Python 边界标为 Markup。标题、发言人和转写正文均由 Jinja 自动转义，不使用 `|safe`。支持标题、列表、粗体、引用和代码块。

`GET /recordings/{recording_id}.md` 下载 UTF-8 Markdown，包含 YAML frontmatter、标题、总结和转写；字符串用兼容 YAML 的 JSON 引号与转义，未知时长留空。文件名删除非法字符及控制字符，空名回退为“录音”；响应通过 RFC 5987 `filename*=UTF-8''...` 保留中文，附带百分号编码的 quoted `filename` 兼容值。导出和详情均禁止缓存。

“复制 Markdown”点击后才请求现有 `.md` 下载接口，检查响应状态与 Markdown 类型后，通过 `navigator.clipboard.writeText` 复制与下载相同的完整文档；首屏不内嵌隐藏 Markdown，未选中正文不进入 HTML，也不执行相应 Markdown 渲染或转写分段。浏览器不支持或拒绝剪贴板时展开只读原文并全选，提示手动复制，无前端框架。新增离线 TestClient 测试覆盖安全渲染及危险链接、转写段落、缺内容分支、登录/白名单失效、404、筛选返回、复制源、导出字段与特殊字符和文件名清洗。

工单 04c 将转写展示改为妙记风格：36px 首字头像、姓名、`mm:ss` 起始偏移和独立正文；连续同一发言人只在首段显示头像与姓名。显示用时间由原始毫秒精度时间戳换算，超过一小时累计分钟，缺失时间不虚构。桌面发言人栏 120px、正文不超过 780px，900px 以下元信息同行、正文独占一行。正文 16px / 1.7，段落间距 20px，支持深色模式、hover 和 Tab 焦点；Enter、空格或点击复制当前段，右下角提示结果。

工具条显示总段数；“搜索本场”仅在现有 DOM 内按姓名、时间与正文过滤，命中淡黄底并滚到首处，不请求服务端、不改 URL。“复制全文”包含所有转写段（不受筛选影响）；“下载 .md”及原“复制 Markdown”的接口和文件内容保持不变。无转写时无工具条。姓名与正文均使用 Jinja 自动转义。

姓名的 UTF-8 SHA-256 首字节对 8 取模选择调色板；头像背景 / 字色依次为 `#E8EFFF/#3553A3`、`#E5F4EF/#24664F`、`#F3EAFE/#71469B`、`#FCE9EF/#9B3D60`、`#FFF0DF/#85551E`、`#E3F3F8/#226477`、`#F0EDE6/#665B43`、`#EEEAFE/#57469B`，头像配色在深色模式保持不变，姓名另用浅色字。测试覆盖全部配对的 4.5:1 对比度。运行 `.venv/bin/python -m pytest -q` 验证服务端，`node --test tests/detail_interactions.cjs` 验证前端搜索、复制、键盘和回退行为（无额外依赖）。

工单 04d 将总结改为最大宽度 760px 的文档版式：主题 / 时间元信息、AI 声明、分级标题、嵌套列表、引用、代码、横向滚动表格及深色模式。首个 Markdown H1 不加分割线，后续 H1 上方加线；不替原文补写标题、摘要或双栏内容。录音时间沿用既有笔记创建时间，未知时长明确显示“时长未知”。

`app/summary.py` 在 Markdown token 层装饰展示，保持 `html=False` 和危险链接协议过滤。`- [ ]` / `- [x]` 与 `## 待办` 下的列表显示为 disabled 复选项（待办节到下一个 H1/H2 截止）；以 `mm:ss` 或 `hh:mm:ss` 开始的段落行使用等宽时间戳，保留后续格式及引用。代码块和代码片段不作待办 / 章节转换。外链带 `rel="noopener noreferrer"`。所有规则仅作用于总结展示，不更改存储、同步、复制、下载或转写视图。

本工单完整回归为 **403 项 Python 测试 + 7 项 JavaScript 测试**，保留原有 391 项并新增 12 项，覆盖元信息、声明、标题与分隔、嵌套列表、只读待办、章节、表格、XSS、原文导出及转写隔离。真实同步录音的桌面浅色 / 深色（1440px）及手机宽度（390px）截图在仓库外保存，不将业务原文或截图提交至 Git。


工单 04e 将详情头部改为单行吸顶：46×46 返回箭头保留来源筛选，17px / 600 标题省略并通过 `title` 提供全称，状态徽标与标题同行，13px 时间 / 时长位于标题下方 4px。导出按钮右下方为 180px 白底菜单，仅含“复制 Markdown”“下载 .md”，总结与转写均只保留这一个下载入口。下载沿用原链接属性、`.md` 路由及服务端文件名。

菜单支持 Tab、上下键循环、Home / End、Enter / 空格，以及 Esc、点击外部和焦点移出关闭；选择项后立即关闭并恢复触发按钮焦点。Markdown 复制成功只将菜单项改为“已复制”，800ms 后恢复，不弹 toast；失败仍保留原文全选回退。转写全文和段落复制、搜索不变；段数仅显示“N 段”，无转写时不渲染。总结仅收紧文档入口处外边距与首元素顶端留白，正文层级、列表、表格、声明和内容顺序保持不变。

04e 回归为 **409 项 Python 测试 + 14 项 JavaScript 测试**。Chrome 在 1440×1100、390×844 及 320px 深色模式检查菜单、键盘、剪贴板、真实下载响应、吸顶和溢出。1440px 下真实总结正文起点为 **259.5px**；该原文以导语开头，首个章节标题为 **369.9px**，不能将正文起点等同于章节标题。截图为真实同步数据在本机使用同版模板渲染，部署后核对远端源文和资产哈希，截图和业务原文保留在仓库外。

### 工单 04f：共用详情版心

总结和转写统一使用 `main.detail-layout`：`--content-max: 760px` 为含内边距的 border-box 上限，水平居中；`--gutter` 在 <640px、640–899px、≥900px 分别为 16px、24px、32px。头部、独立一行的元信息、分段控件和内容 section 共用内侧左边界；顶栏至控件 16px，底部 48px。移除总结和转写各自的限宽及水平内边距，保留总结文档排版与转写 900px 以下堆叠。

回归 **412 项 Python + 14 项 JavaScript 测试**，保留原有 409 项并增加共用结构和 CSS 变量约束。Chrome 对同一条真实录音的两个模板视图逐个切换实测 `.detail-section`：1440px 为 x=372 / width=696，1024px 为 x=164 / width=696，390px 为 x=16 / width=358，同视口两视图完全相同。外框分别为 x=340 / width=760、x=132 / width=760、x=0 / width=390；另检查 320、639、640、899、900、1100px 的边距和横向溢出。四张截图、完整测量 JSON 与验收报告保存于仓库外 `../qwennote-admin-artifacts/04f-20261030/`，不将真实业务原文加入 Git。



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

`GET /admin` 仅允许配置 `ADMIN_OPEN_ID` 中任一 ID 对应的活动白名单用户访问，不依赖数据库角色或姓名；普通用户返回现有 error 模板的 403 页面，未登录使用现有飞书授权跳转。管理页与搜索响应均禁止缓存，并设置 `Referrer-Policy: no-referrer`。

- `GET /admin?q=姓名或邮箱`：显示至多 20 个联系人，包含姓名、部门、邮箱与“添加”按钮。姓名、部门、邮箱、查询和隐藏表单字段均经过 Jinja 自动转义；不展示手机号、性别、令牌等其他敏感字段。搜索结果只用于本次响应，不写入数据库。
- `POST /admin/users`：URL 编码表单包含 `open_id`、`name`、`avatar_url`、`csrf_token`。只保存被选中者的 ID、姓名、头像及白名单状态与加入时间。以 `subject` 为唯一主键、按两种 ID 的三列交叉匹配幂等添加；重复添加保留原加入时间，已移除者再次添加恢复启用。
- `POST /admin/users/{open_id}/remove`：校验管理员及 CSRF 后将 `is_active` 设为 0，保留加入时间与停用状态。现有 session 在下次访问首页、搜索、详情、下载或同步时返回 403；重新登录也不能绕过白名单。管理员种子不会在重启时重新启用被停用的记录。
- 白名单展示全部已添加用户的姓名、截断为前 8 位加 `…` 的 subject、加入时间和启用/停用状态，`title` 属性提供完整 subject。添加与移除成功均 303 返回管理页。

**CSRF 方案：签名双提交 cookie。** 复用 `SignedCookie`，独立 `admin-csrf` salt，token 绑定当前已签名 session 的 SHA-256 摘要并附带随机值（不携带 session 本体），最长有效期 8 小时。管理页设置 HttpOnly、SameSite=Lax、Path=/admin cookie，HTTPS 配置下添加 Secure；表单包含相同 token。两个管理 POST 必须通过签名、时效、session 绑定和恒定时间比对，缺失、伪造、过期、重复字段或其他 session 的 token 返回 403，GET 不执行写操作。此方案无需依赖反向代理转发 Origin 的方式，普通 HTML 表单不带 Origin 时也能严格验证。

`HttpFeishuAuth.search_users(query)` 获取企业自建应用的 tenant_access_token，调用飞书 [Directory 搜索员工接口](https://open.feishu.cn/document/directory-v1/employee/search)，明确指定 `employee_id_type=open_id`、`page_size=20`，不追随下一页。该接口支持应用身份；旧版 `search/v1/user` 要求用户令牌，不适用于仅配置 App ID/Secret 的服务。仅请求 ID、姓名、头像、部门、邮箱字段，并将响应转换为 `FeishuUser`；扩展的部门和邮箱仅供临时展示，不写 users 表。

真实应用需开通 `directory:employee:search`，并按[官方接口文档](https://open.feishu.cn/document/directory-v1/employee/search)授权姓名、头像、部门、邮箱字段及所需通讯录数据范围。凭据缺失时适配器抛出明确的 `FeishuSearchNotConfigured`，页面显示“通讯录搜索未配置”；权限、网络、HTTP 或响应格式失败均显示可读错误，不输出提供方原始响应，不伪装成空结果。`Settings.from_env` 沿用启动时必填环境变量校验。测试只使用假 FeishuAuth 和 httpx.MockTransport，未进行真实租户联调。

工单 07 新增 52 项离线测试，覆盖管理员边界、搜索不落库、敏感字段过滤与 HTML 转义、添加后完整 OAuth 回调、重复添加与恢复、旧 session 撤权、POST/CSRF（缺失、伪造、过期、跨 session、重复字段、Secure cookie）、未配置提示，以及真实适配器的应用 token、open_id、20 人上限、空结果与两阶段错误。已有停用用户测试按本工单要求更新为明确的 403。

## 生产部署（工单 08）

目标 `https://qwennote.mingkai.xin`，SWAS 实例 `74ee3f09792c48649cf8aad318702dd5`（香港，`8.210.78.233`）。仅使用已有 `/usr/bin/python3.11`，应用监听 `127.0.0.1:8090`。不修改系统 Python、8081、dashboard-shell 或其他站点的 nginx 配置。

在已配置阿里云 CLI 的本机仓库执行：

```sh
python3 scripts/deploy.py
```

已部署站点只更新应用时运行 `python3 scripts/deploy.py --app-only`：沿用相同归档、SHA-256 校验、新 release 准备与 `current` 原子切换，保留 `previous` 供回滚；仅重启应用并验证 HTTPS，不改 nginx、证书、systemd 单元或定时器，并核对全部 nginx conf 的部署前后哈希。

脚本复用 `~/.hermes/digital-agent/tools/swas_run.py`，显式指定实例；记录主站部署前状态和其他 nginx conf 的哈希。打包排除 Git、venv、data、env、scratch 和缓存，base64 按 48,000 字符分 part；该实例实测整块命令被 `CmdContent.ExceedLimit` 拒绝，因此每 part 用最多 12,000 字符的命令顺序追加。服务端 SHA-256 校验成功才解包到 UTC 命名的 `/opt/qwennote/releases/YYYYMMDDTHHMMSSZ/`。日志在本机 `/tmp/qwennote-deploy-logs/<UTC时间戳>/`，不包含生成的环境变量值。远端失败即停止；修复后可重新执行，产生新的 release。

安装使用 Python 3.11 venv 和阿里云 pip 源，创建无登录 shell 的系统用户 `qwennote`。release、`data` 与 `backups` 归它所有，数据库固定为 `/opt/qwennote/data/qwennote.db`。原子切换 `current`；升级时将原 release 保存到 `previous`。服务日志进 journald，systemd 只允许写持久数据目录。

服务器实测 Python 与系统 CLI 的 SQLite 均为 3.26.0，缺少应用全文索引需要的 trigram tokenizer。`scripts/build_sqlite.sh` 从 [SQLite 官方下载页](https://www.sqlite.org/download.html)锁定 3.54.1 amalgamation 和 SHA3-256，核对后使用已有 gcc 构建到 release 的 `.sqlite/`；应用和同步单元通过 `LD_LIBRARY_PATH` 使用它，备份单元的 PATH 选择配套 `sqlite3` CLI。不替换系统 SQLite 或 Python。手工检查数据库前先执行 `export PATH=/opt/qwennote/current/.sqlite/bin:$PATH`。

环境文件仅在服务器首次创建：`/etc/qwennote/qwennote.env`，`root:root`、600。`SESSION_SECRET` 由服务器 `openssl rand -hex 32` 生成；飞书 ID、密钥、管理员 ID、千问 token 均留空，默认 `QWENNOTE_SOURCE=fixture`，fixture 路径为 `/opt/qwennote/current/tests/fixtures/minutes`。后续部署保留已有 env。通过服务器安全终端编辑实际凭据，不把 env、数据库或证书提交 Git。

当前代码的标准入口是 `create_app`，标准配置校验要求真实飞书字段。`scripts/deployment_runtime.py` 只向已解包 release 的 `app/main.py` 追加 `app` 导出，让 systemd 使用约定的 `uvicorn app.main:app`；checkout 中的应用文件不变。仅部署 fixture 模式允许飞书字段为空，不创建空管理员或虚构身份，核心密钥、数据库、回调和 fixture 路径仍校验。首页返回飞书 302，但缺真实凭据时无法完成登录；API 无有效 Key 时返回 401。切换到 HTTP 源后恢复标准必填校验。

`deploy/nginx/qwennote-http.conf` 先用于 ACME webroot；取得受信任证书后替换为 `deploy/nginx/qwennote.conf`，始终只写 `/etc/nginx/conf.d/qwennote.conf`。80 保留 ACME，其他请求 301 HTTPS；443 使用专属证书、反代头、安全头和 2MB 请求限制。先尝试 dnf certbot；dnf 不可用时使用独立 `/opt/qwennote/acme` 的 acme.sh 与 Let's Encrypt，不填写虚构邮箱，不使用自签证书。失败时停在 HTTP 引导配置并报告。certbot 使用续期 timer 与专属 nginx reload hook；acme.sh 使用专属 systemd 续期 timer。

服务和检查：

```sh
systemctl status qwennote
journalctl -u qwennote -n 50 --no-pager
ss -lntp | grep 8090
curl -sS https://qwennote.mingkai.xin/ -o /dev/null -w '%{http_code} %{ssl_verify_result}\n'
curl -sS https://qwennote.mingkai.xin/api/v1/recordings -o /dev/null -w '%{http_code}\n'
curl -sS https://qwennote.mingkai.xin/api/v1/recordings -H 'Authorization: Bearer test' -o /dev/null -w '%{http_code}\n'
nginx -t
```

`qwennote-sync.timer` 每天上海时间 03:00 运行 `python -m scripts.sync_once`，直接复用 `app.sync.run_sync`，失败退出非零。`qwennote-backup.timer` 每天上海时间 04:00 运行 `scripts/backup.sh`，用 SQLite `.backup` 在线备份、校验后原子命名为 `backups/qwennote-YYYYMMDD.db`，保留最近七个日期。两个 timer 都启用 `Persistent=true`，停机错过时间后补跑。手动验收启动的是 timer 对应的 service：

```sh
export PATH=/opt/qwennote/current/.sqlite/bin:$PATH
systemctl start qwennote-sync.service
journalctl -u qwennote-sync.service -n 10 --no-pager
sqlite3 /opt/qwennote/data/qwennote.db 'SELECT count(*) FROM recordings; SELECT id,status,fetched,inserted,updated FROM sync_runs ORDER BY id DESC LIMIT 1;'
systemctl start qwennote-backup.service
ls -l /opt/qwennote/backups/qwennote-*.db
systemctl list-timers --all 'qwennote-*'
```

回滚以 root 执行 `/opt/qwennote/current/scripts/rollback.sh`，原子指回 `previous` 并重启 `qwennote`，打印实际版本目录。无有效前一版则明确失败且不动 current；重启失败恢复原指向。首次部署没有可回滚版本。回滚只切应用代码，持久数据库不回退；涉及不兼容数据库迁移的后续升级须另行安排数据恢复。

## 真实千问 MCP 与 OAuth 续期（工单 09）

生产设置 `QWENNOTE_SOURCE=mcp`、`QWENNOTE_MCP_URL=https://minutes.qwennote.cn/mcp`、`QWENNOTE_TOKEN_FILE=/opt/qwennote/data/mcp_token.json`。Token 文件为 `qwennote:qwennote`、600，包含注册所得 `client_id`、`scope`、`obtained_at`、`access_token`、`refresh_token`、`expires_in`；凭据不进入 Git、日志或页面。`QWENNOTE_MCP_TOKEN` 仅供保留的旧 HTTP 适配器兼容，MCP 源不读取它。缺失或无效的 token 文件使启动明确失败，不回退 fixture。

应用启动读取 token 文件；按 `obtained_at + expires_in - 120` 判断是否需续期。每次请求重新读取磁盘凭据，在稳定的 `.lock` 文件上获取排他文件锁后再次检查，避免多进程同时消费旧 refresh token。续期使用 `/token` 的表单编码；新 access token、轮换的 refresh token 和取得时间以 600 临时文件原子覆盖原文件。401 在锁内刷新后重试一次；超时、408、429、5xx 最多额外重试两次，退避 1、2 秒，每次请求超时 30 秒。提供方响应、异常原文及凭据不写日志或同步摘要。

网页 `POST /sync` 和 `scripts.sync_once` 都复用 `run_sync`，MCP 模式调用 `sync_window(90)`（可通过现有 `BACKFILL_DAYS` 调整）。范围从上海时间 90 天前零点到当前时刻，使用 epoch 毫秒 `startTime/endTime`，列表后续页用 `cursor=nextToken`。本端再次校验时间范围，并按 `gmtCreate` 转成上海时间归档，避免接口返回跨天记录导致误归日期。UUID 去重及 upsert 保证幂等；详情失败仅跳过该条并在 `sync_runs.error` 记录安全摘要，续期失败立即终止本次同步并记 failed，保留已有录音。

每条详情读取 basic info、AI summary 和全部转写页；`mediaType` 保存为 `recordings.media_type`，不实现播放器，日常同步无需请求媒体地址。可选媒体元数据查询对 `available=false` / `INCOGNITO_MODE` 正常返回，不记录错误。任一转写页 `filtered=true` 或没有段落时视为未转录；`hasSummary=false` 显示“暂无总结”。转写以录音内毫秒偏移保存为 `[mm:ss.mmm–mm:ss.mmm] 发言人`、空行、正文、空行，详情同时兼容旧小时格式。数据库启动自动补充 nullable `media_type` 及 `sync_runs.refreshed_token`（0/1），保留历史数据和全文索引。

切换生产源后清理历史示例数据（可重复，只要求 DB_PATH，不要求 OAuth 或飞书配置）：

```sh
DB_PATH=/opt/qwennote/data/qwennote.db \
LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib \
/opt/qwennote/current/.venv/bin/python -m app.purge_fixture
```

命令只删除 ID 以 `fixture-` 开头的记录，打印实际删除条数；已有全文索引触发器同步删除搜索条目。生产使用现有 `python3 scripts/deploy.py --app-only` release 流程，保留 previous，不修改 nginx 或其他站点。


## 工单 04g：全站文案审计

按 [UI 原则](docs/ui-principles.md) 删除日期帮助、重复筛选摘要、品牌副标题、无批次时重复状态与零计数、联系人数量上限及重复占位符；详情删除重复的主题/时间卡片，原文标签去掉“可全选复制”。保留 AI 声明、全部空状态与错误下一步；时间口径放列表底部，详情头部保留时长未知和 GMT+08 提示。逐句判定及理由见 [完整审计表](docs/ui-copy-audit-04g.md)。

清理无用途容器、引用和样式；功能性动态反馈节点空时不占布局。空白名单不渲染表头分隔线，短身份标识不附多余省略号。管理页手机表格纵向排列，长错误 ID 可换行。数据、同步、导出接口与 Markdown 渲染器均未变更。

回归 **428 项 Python + 14 项 JavaScript 测试**。保留原有 412 项 Python 用例，更新与本次删改相关的展示断言，新增 16 项；删除项和指定保留项逐条验证。Chrome 对 20 种正常/空/错误状态分别检查 1440、1024、390px，共 60 组，无页面或管理表格横向溢出、可见空说明、孤立标点，空状态无无内容分隔线。

1440px 的 home/detail/admin 新旧对比截图、390px 截图、测量 JSON、完整 HTML 报告保存于仓库外 `../qwennote-admin-artifacts/04g-20261030/`。截图使用同一组合成数据；上线经 `scripts/deploy.py --app-only`，发布目录与线上哈希核验记录在该交付目录。

工单 04h 将详情版心恢复为与主页一致的 **1120px 外框 / 固定 16px 页边距**，1440px 视口的 section 宽 1088px；总结和转写正文行高 1.75，转写段间距 24px。该规格替代上文 04d / 04f 的窄版参数。六视口实测表与浏览器回归运行方式见 [04h 版心验收](docs/detail-layout-04h.md)。

工单 04i 保持上述版心不变，在详情卡片内侧增加桌面左右 32px、手机左右 16px 留白；表格和代码块长内容换行。四视口实测表与回归证据见 [04i 内边距验收](docs/detail-padding-04i.md)。

## 工单 05c：主页去掉日期筛选

Python 回归 **458 passed**（保留原有 455 项，日期筛选用例改为忽略参数，新增 3 项搜索日期参数用例）；JavaScript **19 passed**；Chrome 首页布局回归 **1 passed**，涵盖 1440 / 390 / 320px、浅色 / 深色。无日期控件、无筛选/清除文案、带日期的 total 与全量相同、分页不携带日期、搜索往返、分组、标签和空状态均通过。

部署命令为 `python3 scripts/deploy.py --app-only`。真实数据主页截图（1440 / 390）、测试日志、发布目录与文件哈希验收保存在仓库外 `../qwennote-admin-artifacts/05c-20261030/`；不公开业务截图。
