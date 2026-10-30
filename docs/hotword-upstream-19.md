# 工单 19：单向推送千问热词

管理员在热词中心点击「同步到千问」先看到预演，明确确认才新增。只提交 `term`；别名仅用于本地模型判断和导入查重。千问无删除或修改接口，误推后需在千问 App 手工清理。本次部署验收只预演，未执行真实推送。

## 接口与配额

所有接口复用管理员鉴权、会话绑定 CSRF 和同源边界，使用 URL 编码表单。没有定时、同步后或页面加载时自动推送的路径。

- `POST /hotwords/qwennote-status`：只调用上游 `list_hot_words`，将有效完整快照缓存到本地；页面加载和打开同步弹层前刷新状态。未知配额显示未读取，不假定为 0。
- `POST /hotwords/sync-qwennote`，`confirm=0`：只读本地热词、同步记录和配额缓存，**零 MCP 请求、零数据库写入**；返回 `to_push`、`skipped`（含 reason/message）、`currentCount`、`maxCount`、`afterCount`、`quota_checked_at`、`can_confirm`、`plan_hash`。没有缓存时返回 `quota_unknown`、占用未知，不允许页面确认。
- 同一接口 `confirm=1`：数据库旁稳定文件锁串行执行；先重新读取真实配额，再整批新增。页面提交预演的 `plan_hash`，词表或配额变化返回 `plan_changed`，要求重新预演。缺少/非法 confirm 返回 400；API 的显式 `confirm=1` 也可用于已由管理员确认的调用。
- `POST /hotwords/import-preview`：读取千问词表，与本地 term 及逗号分隔 aliases 比较，返回仅上游有的候选；不写应用数据库，不自动导入。候选默认不选。勾选后才逐条 `POST /hotwords`，带 `from_upstream=1`，继续本地 1–20 字校验，归一化去重在写事务内完成。归一化使用 NFKC、英文 casefold 和去空白；保留 term 原文。

按表中顺序判定：成功同步且标准词未变 → `already_synced`；非中英文/数字/普通空格 → `invalid_chars`；汉字超过 20 或 ASCII 字符（含数字、空格）超过 40 → `too_long`；停用 → `disabled`。已同步后仅编辑 aliases 不会重推，编辑 term 会成为新候选。剩余配额不足返回 `quota_insufficient`，没有新增调用，也没有部分结果写入。页面每行以 ✓ / — / ! 展示当前判定，跳过原因可悬停或通过可访问名称读取。

一次 `add_personal_hot_word` 调用打包所有合格标准词，空批次不调用。复用原有 HTTP/OAuth MCP transport，使用独立的热词工具清单。该追加调用在超时、429/5xx 等情况下**不自动重试**，避免响应丢失后重复新增；401 仍可在明确未授权时续期凭据。凭据只复用部署环境/标准文件，不入业务响应或审计。

## 数据与追溯

增量迁移增加三张表，不改录音原文、手动正文、term/aliases/note：

- `hotword_upstream_sync(hotword_id PRIMARY KEY, upstream_value, synced_at, status)`：每词当前结果，status 为 ok/skipped/failed；已成功且未变的跳过保留 ok 证据。
- `hotword_upstream_runs(id, started_at, finished_at, status, plan_json)`：每次执行追加一行，保留完整计划、跳过原因、结果和时间，避免每词表覆盖后丢失历史。预演和配额中止不记执行；进程中断留下 running，供人工核对。
- `hotword_upstream_state(id=1, snapshot_json)`：配额及上游词表快照。推送成功后更新预计占用，下次刷新以千问真实值为准。

成功后所有逐词结果在一个本地事务内写入。上游接口和 SQLite 无分布式事务；连接中断或进程在上游成功后退出可能留下不确定结果。页面禁用再次确认并提示先从千问导入核对，不能将超时等同于上游未写入。其他客户端仍可同时占用千问配额，执行前复查只能缩小竞争窗口；上游拒绝时记录 failed，不部分本地标 ok。

## 验收与部署

保留原有 1050 项 Python 测试，新增 46 项，合计 **1096 passed**；21 项 JavaScript 单测通过。全部 Python 测试由全局 fixture 阻断外网，新增用例使用假 MCP HTTP/OAuth transport，覆盖零请求预演、确认才写、只推 term、各跳过原因、40/20 长度边界、真实配额复查、配额中止无部分写入、成功及失败记录、历史幂等、并发确认、导入预览无写入、勾选与归一化查重、权限和 CSRF、SSE 与坏配额响应。

新增浏览器验收覆盖 1440/390/320px、浅深色、预演三组、取消不写、显式勾选导入和一次假 MCP 确认。另执行原词粒度、模型判断浏览器回归；截图仅用合成数据。报告、日志与生产只读验收保存在仓库外 `../qwennote-admin-artifacts/hotword-upstream-19-20261030/`。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/hotword_upstream_preview.py
# 另一个终端，PLAYWRIGHT_MODULE/CHROME_PATH 指向已安装运行时：
node --test tests/hotword_upstream_browser.cjs
python3 scripts/deploy.py --app-only
```

部署前备份 SQLite，部署后核对 release、源文件哈希、数据库完整性、录音/热词原字段与手动正文不变；通过生产已授权管理员会话先刷新状态，再 `confirm=0`，不调用真实新增。只使用 app-only，不改 nginx、其他站点或 `.scratch/`。新增表不改变工单 18 已有表结构，可切回该 release；先保留新增表中的审计，不应回滚生产业务库。

本任务为项目功能实现，不新增通用技能。代码 Git 归档、技能状态、私有 HTML 报告和 Issue 上报分别记录；当前公开授权仅限技能说明，业务预演结果保持私有。
