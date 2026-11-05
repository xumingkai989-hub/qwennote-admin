# 工单 42：登录触发快速同步

飞书 OAuth 校验 state、换取身份和白名单检查成功后，先设置 session cookie，再尝试领取全库共享的一小时窗口。普通用户与管理员共用同一个窗口。

`app/login_sync.py` 在 `BEGIN IMMEDIATE` 事务中读取、比较并写入 `settings.login_sync_at`。时间戳为带时区的 ISO8601，沿用 settings 的 JSON 值编码；无需迁移。新增 `get_setting` / `set_setting` 可复用事务连接，现有 `get_settings` / `save_settings` 继续只处理调度配置，保存设置不会覆盖登录窗口。

先提交占位，再启动 daemon 线程。线程读取当前 backfill_days（未持久化时使用应用配置），调用 `run_sync(full=False)`。锁冲突静默结束，异常或失败结果只记录不含上游异常文本的日志，线程启动失败也不撤销占位。回调捕获触发异常，始终保留成功登录的 302 和 session。成功同步沿用现有增强任务队列和后台入口。

窗口边界为大于等于一小时；缺失或无效时间重新建立窗口，未来时间继续去抖，避免时钟回拨后重复请求。空库没有成功水位时，沿用 run_sync 的全量补拉保护；有有效水位时记录为 quick。不改变定时任务、同步锁或同步算法。

`GET /api/sync/status` 仅供有效白名单 session 访问，未登录 401、撤权 403，禁止缓存。返回 `running` 及最近批次的 `mode/status/finished_at`（无历史时 last 为 null）。同时满足文件锁占用和最近批次 running 才视为正在同步；崩溃遗留行不会永久轮询。

首页及搜索列表沿用同步摘要行，移动端显示「正在同步…」，桌面显示「正在同步，稍后自动更新」。仅在服务端检测到运行时加载独立轮询，每 3 秒请求状态，结束后刷新一次。网络或服务临时失败继续重试，401/403 停止轮询；不改增强任务轮询。

## 验证

```sh
.venv/bin/python -m pytest -q
node --test tests/login_sync_interactions.cjs tests/home_interactions.cjs tests/detail_interactions.cjs tests/assistant_interactions.cjs tests/enrichment_interactions.cjs
```

专项测试覆盖无记录、30/55/60/61 分钟、回拨、无效时间、两个真实 SQLite 连接并发占位、配置保存、线程/同步/数据库失败、非管理员、全局共享、白名单拒绝、真实阻塞线程下的立即 302、fixture 同步、锁冲突、鉴权、遗留 running 行以及页面提示。前端测试检查 3 秒周期、只刷新一次、错误重试和撤权停止。

部署沿用既有依赖，复制生产 release 并覆盖本提交文件，只执行 app-only activate，不执行 prepare，不写 `.scratch/`、nginx 或 `/etc/`。真实登录时间、批次记录和 release 证据保存在仓库外的本工单交付目录。
