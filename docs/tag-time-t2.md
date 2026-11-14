# T2 周度时间分配聚合

`app.tag_time.week_allocation(store, monday, today)` 接受 ISO 日期或 `date`，与现有看板一致验证周一及同周截止日；返回 `total_minutes / unknown_duration_count / by_category / by_tag`。

- 上海时区范围是周一 00:00（含）到截止日次日 00:00（不含）。带时区时间戳按实际时刻比较，无时区历史值按上海本地时间解释。截止日内晚于当前时刻的记录也符合本票范围。
- 同一 SQLite 读事务取得启用分类和会议、主标签、词表的多对一连接；不连接多标签关系表，单场时长只计一次。聚合不写库、不调用模型，缺失判定、fallback 或主标签无法关联词表时归入「未分类」。
- NULL 时长不贡献分钟，另计 `unknown_duration_count`；`sessions` 包括 NULL 时长会议。空周及总分钟为零时，两层均返回空列表。
- 一级分类从 `tag_categories` 读取 `enabled=1`，按 `position, id` 初始化；当前词表分类缺失或禁用时计入「其他」。低于 2% 按整数 `minutes * 100 < total * 2` 比较；恰为 2% 保留。「其他」原分类与合并项共用一行，「未分类」有会议时始终单列。
- 二级标签按分钟降序取前 6（同分钟按名称确定顺序）；「未分类」参与排名，若未入前 6 则额外保留。剩余标签并入「其他」，跨分类合并项的 `category` 也设为「其他」。词表中名为「其他」的标签与合并项共用一行。
- 两层普通项按分钟降序，同分钟分类按数据库 position；「其他」固定倒数第二，「未分类」固定最后，只返回有会议的行。二级标签保留其原一级分类，一级分类的小项合并不改变标签层的分类名称。
- 合并分钟为总量减去保留项整数分钟和；份额最后计算，保留 4 位小数。函数返回前对两层分钟和作运行时断言，失败抛带实际差值的 `ValueError`，在 `python -O` 下仍生效。

T1 实际签名为 `judge(store, recording_id, *, model=None)` 和 `ensure_week(store, monday, today, *, model=None)`，无需适配。T1 的 `ensure_week` 会排除当前时刻之后的会议；本票聚合遵循用户指定的次日零点边界，不更改 T1。需要补齐主标签时，调用方应先显式调用 `ensure_week`。

本票不接入页面或调度；保持 `board.py` 的 totals、day_stats、week_stats 与 T1 判定逻辑原样。部署复用 T1 release 依赖，只覆盖本票提交文件并执行 app-only activate，不执行 prepare，不修改 nginx、`/etc/` 或 `.scratch/`。生产库副本结果与发布核验保存在仓库外交付目录。
