# 工单 35：增强模型兜底与 SQLite 兼容

同步 `run_tags`、`run_judgments` 与异步 worker 均使用 `app.models.resolve_model`。
当前 HEAD 原本已共用这个入口，但只支持数据库默认配置和进程环境，裸 CLI
未加载模型环境时会错误判定未配置。共享入口现按以下顺序解析：

1. 数据库中启用的默认模型；密钥损坏仍报错，不静默切换。
2. 含 `ANTHROPIC_AUTH_TOKEN` 的进程环境，保留原有行为。
3. `/etc/claude/deepseek.qwennote.env` 文件；仅取三个模型字段，不执行 shell、不插值。
4. 都无凭据时，增强任务终止为 failed / 未配置模型，不提供重试。

配置文件已存在且服务用户可读，不需要修改 `/etc/`。提示词和模型协议未变。
生产现有服务进程带有模型环境，因此本工单中的直接 CLI 失败不能单独证明服务
进程也缺少环境；文件兜底确保两种启动方式使用同一解析规则。

全仓 SQL 中两处 `RETURNING` 均位于增强层，现已移除。run 插入仍取
`lastrowid`；task upsert 后按唯一键查询 ID（更新分支不能使用 lastrowid）；
领取任务用条件 UPDATE 的 rowcount，再在同一 BEGIN IMMEDIATE 事务内读取。
因此保持原子领取、重复入队及旧租约隔离，兼容生产原生 SQLite 3.26.0。

## OAuth 复核

生产同步 28、29 分别始于 `2026-11-03T16:40:30.364683+08:00`、
`16:41:26.882601+08:00`，均在约 7 毫秒失败。token 的 obtained_at 为
`1793695229.91271`（`16:40:29.912710+08:00`），expires_in=3600，
到期为 `17:40:29.912710+08:00`，远未进入提前 120 秒续期窗口。
同步 30 在 `16:43:27.977110+08:00` 成功，refreshed_token=0。

前一工单保留的 token-access 实测记录显示凭据文件当时为 root:root 0600，
服务 uid=988 无法读取；restore-token-owner 记录显示恢复 qwennote 属主、
保留 0600、内容哈希不变，随后同步恢复。这是 root 基准进程续期后原子替换
共享文件带来的权限问题；证据不支持并发消费 refresh token 或瞬时网络解释。
本单所有访问共享 OAuth 的验收进程均以服务用户运行。

`OAuthTokenManager.access_token` 已对稳定 sidecar 文件加 flock 排他锁，
取得锁后重读 token；现有并发 manager 测试断言仅一次刷新。
普通 MCP 请求的两次重试和一次 401 刷新不包住 token 的读写/续期失败，
TokenRefreshError 直接终止同步。这对上述本地权限失败合理；盲目重试也无法修复
属主，并且不能假定已发出的刷新请求可安全重放。因此本单不改 OAuth 代码。

## 验收

新增测试直接使用真实共享解析器、真实 SQLite 与离线模型 HTTP transport，
验证空 models 表的环境/文件兜底、标签与热词检查落库、默认模型优先级、无配置
终止且不可重试。兼容连接拒绝任何 RETURNING SQL，覆盖入队、重复入队和领取。
离线测试统一隔离本机模型配置文件，避免读取真实凭据。

生产验收使用最新一条录音入队并执行，再发起真实同步、重新请求页面和状态接口，
核对批次、任务、标签与热词检查时间。具体 SQL、实际输出、部署日志与私有 HTML
报告保存在仓库外 `../qwennote-admin-artifacts/enrichment-35-20261103/`。
