# 工单 34：AI 增强移出同步请求

同步只负责拉取和落库，释放 `sync.lock` 后写入增强队列，返回 `enrichment_run_id`。`duration_ms` 不再包含模型耗时。同步失败不创建增强批次；没有候选项的成功批次也创建 pending run，由下一次处理结清为 done。旧版同步性能说明中的耗时口径由本单替代。

`store.migrate()` 幂等新增 `enrichment_tasks`、`enrichment_runs`，以及用于准确关联多个批次与复用任务的 `enrichment_run_tasks`。任务按 `(recording_id, kind)` 唯一，重复入队重置 pending 和重试预算。`sync_runs.enrichment_run_id` 保存关联。任务额外的 `claim_token` 用于隔离重新入队和旧工作线程的完成回写。

网页响应发送后通过后台线程立即处理；`scheduler_tick` 每十分钟扫描 pending 和超时 running。CLI 先打印同步结果，再等待自己的后台批次退出，防止正常结束丢弃线程。SQLite 的单条条件 UPDATE 原子领取任务；两把非阻塞文件槽位锁将网页、CLI 和 tick 的总增强并发限制为 2。任务列表每批取快照（默认 100 条），一次批次只尝试各任务一次，失败在后续 tick 重试。运行中的工作线程每三十秒更新租约心跳；进程退出释放文件锁，失去心跳十分钟后可重新领取。

增强层调用现有 `run_tags` / `run_judgments`，检查它们原先内部消化的失败返回值。未修改模型协议、提示词和解析规则。异常只存固定摘要「分析失败，请重试」，第三次失败转 failed；无模型配置直接 failed，摘要「未配置模型」，不会自动或手动重试。模型配置完成后可通过重新入队（例如全盘发现内容变化）重新分析。标签和热词增强失败均不影响同步原文、详情和导出使用。

`GET /api/enrichment/status?run=ID` 要求登录且仍在活动白名单中，返回批次计数、任务状态、标签与热词计数，无凭据、提示词或原始异常。`POST /api/enrichment/retry` 要求管理员与 session 绑定的 `recording_csrf`，表单参数为 `csrf_token` 和可选 `recording_id`；只重置可重试 failed 任务并在响应后触发线程。

列表和详情打开时从数据库恢复状态。同步后局部获取并替换列表，不刷新页面；增强期间每两秒轮询，直接更新标签和热词提示。终态、404 或权限失效均停止轮询；401 提示登录失效。详情完成后局部更新轨迹及当前正文，同时更新编辑器基线。用户正在编辑时暂存完成通知，退出编辑再更新，保留草稿和滚动位置。

## 验证

```sh
.venv/bin/python -m pytest -q
node --test tests/home_interactions.cjs tests/enrichment_interactions.cjs tests/detail_interactions.cjs
```

新增后端测试覆盖同步无 AI、真实离线模型写标签、吞掉的模型失败、失败预算及脱敏、无模型不可重试、过期租约、重启 tick、重复入队、失败同步、空批次、partial 计数、全局并发、旧租约隔离、后台线程和 API 权限/CSRF。Node 验证占位到标签、停轮询、恢复、热词提示、权限退出和无整页刷新。

`tests/enrichment_preview.py` 与 `tests/enrichment_browser.cjs` 提供独立合成录音的浏览器验收，使用现有 Playwright 安装，不添加应用依赖。验证关闭再进入、编辑草稿、终态停轮询、滚动位置及 1440/390/320px，保存列表占位/完成、详情分析中/完成截图。原有布局回归保持覆盖。

部署仅 app-only，复用上一工单的安全部署适配器，prepare 不写 `/etc/`；备份业务库并记录 `/etc/` 文件哈希，发布后核对原录音/手工内容、SQLite 完整性、权限接口及 release。实测使用同一生产备份和真实上游录音的隔离数据库，仅模拟一次本地不存在查询，不删除生产或测试数据。报告分别记录旧请求耗时、新请求耗时、后台标签延迟、实际新录音是否存在及真实 kill + 十分钟租约恢复结果，不把合成模型延迟当成生产指标。

业务验收 HTML、截图、部署日志和原始证据保存在仓库外 `../qwennote-admin-artifacts/enrichment-34-20261103/`，未获得公开业务内容授权，保持私有。新增的 `sync_runs.enrichment_run_id` 会被旧版 `SyncRun(**row)` 读取，因此不能直接把旧 release 接到新库回滚。应先给旧版 `SyncRun` 加上可选关联字段（或显式忽略未知列），再切回已验证兼容的 release；保留新增表、任务审计和原始录音。兼容旧版仍会重新启用同步内的 AI 路径，不应丢弃或恢复覆盖当前业务库。
