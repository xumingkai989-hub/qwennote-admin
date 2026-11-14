# T9：窗口内未完成会议正常拉取

同步按 `meta.created_at >= start` 判定窗口（包含水位线边界）。快速同步仍跳过已有转写；窗口内未完成会议每轮可以进入 pending，不受 `next_attempt_at` 和补齐队列数量限制；窗口外已知未完成会议仍须在到期的 `backfill_ids` 中。全盘同步使用自己的 backfill 窗口，窗口内同样适用。

`app/sync.py` 的 `_last_detail_attempt` 保存 recording_id → monotonic 时间，`_claim_detail_attempt` 由 `_fetch_detail_if_due` 在详情实际开始执行时加线程锁检查并登记（包括限流后串行执行）。距上次尝试小于 60 秒记为 skipped，不改尝试时间；60 秒时可重试，过期项在后续检查时清理。请求失败也受节流；原有单次拉取内部的 HTTP 重试预算保持不变。窗口内已有未完成记录的请求/入库失败也更新 backfill_state，避免“最近尝试”仍停留在旧值。

缓存仅属于当前 Python 进程，不落库；服务重启、独立 CLI、不同 worker 会分别拥有缓存。生产 tick 是独立 systemd 进程，因此不能靠此缓存跨进程合并登录和 tick；数据库同步锁仍避免同库并发同步。此限制与单进程要求一致，不新增跨进程存储。

`pending_upstream` 新安排的等待档位为 5、15、60 分钟，后续持续 60 分钟。已有未来 next_attempt_at 不批量重写；窗口内可立即绕过，窗口外按原到期时间重试后采用新档位。`no_upstream_content` 仍为 1440 分钟，真实错误策略不变。

列表、搜索及详情的缺转写标记下显示小字「最近尝试 HH:MM」，取现有 backfill_state.last_attempt_at 并转换为上海时区；没有记录或已有转写时不显示。不新增数据库字段或依赖，不修改看板、week_allocation、标签业务逻辑。

测试覆盖窗口内未来退避、精确边界、窗口外退避、完整记录跳过、59/60 秒节流、失败尝试时间、5/15/60 分钟与 24 小时档位，以及三种页面的有/无时间、时区和补齐后隐藏。既有主动再次拉取的用例显式推进模拟单调时钟，保留原断言。

发布复制已验证的当前 release 和既有依赖，只覆盖本票提交文件，备份生产库后执行 app-only activate，不执行 prepare，不写 .scratch、nginx 或 /etc。实测同步前后状态、长度、源码哈希和测试日志保存于仓库外 `../qwennote-admin-artifacts/backfill-t9-20261114/`。业务报告保持私有，技能、Git、公开 HTML 和 Issue 上报分别记录状态。
