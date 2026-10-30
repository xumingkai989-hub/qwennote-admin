# 工单 14：设置中心与统一调度

管理员通过左侧「设置中心」进入 `/settings`，按「同步 / 标签 / 数据保留」维护七项设置。默认自动同步开启、每天 03:00、间隔 0、补拉 90 天，自动打标签开启、词表维护 05:00、永久保留。表单 POST 复用管理员鉴权和会话绑定的签名 CSRF，使用独立的 `settings_csrf` HttpOnly cookie（`/settings`），避免其他管理页的局部 cookie 干扰。成功 303 后显示「已保存 · 下次同步 MM-DD HH:MM」；关闭时显示「自动同步已关闭」。所有页面禁止缓存。

时间严格接受 `HH:MM`。间隔 0 表示固定时间，正整数表示每 N 小时；负数、小数、空值和非数字拒绝。补拉天数至少 1，保留天数至少 0；整数最多六位（999999）。一次验证整份设置后用单个事务保存，非法输入不写任何设置。未保存补拉设置时，网页和旧同步 CLI 仍兼容原 `BACKFILL_DAYS` 默认值；保存后以数据库为准。手动同步不受自动同步开关限制，同样使用已保存补拉天数。关闭自动打标签同时停止同步后的自动打标和每日词表维护，保留已有标签。

## 调度口径

新增 `settings(key TEXT PRIMARY KEY, value TEXT)` 和 `scheduler_state(job TEXT PRIMARY KEY, last_run_at TEXT)`；不修改既有表结构。各设置值为有类型的 JSON 标量，缺项回落默认值，迁移不会覆盖已保存设置。

`python -m app.scheduler_tick` 使用上海时区与可注入时钟：

- `sync`：固定时间到点后当天补跑一次，不追补更早日期；间隔模式从上次实际执行的上海小时起计 N 小时，没有历史时当前小时即可执行。间隔支持跨天及大于 24 小时，固定时间在间隔模式下不参与判断。
- `tag`：每天 `tag_time` 后维护一次，复用原 `run_maintenance`，自动打标签关闭时不执行。
- `retention`：保留天数大于 0 时，每天 00:00 后清理一次。按创建时间严格早于「执行时刻减 N 天」删除，边界时刻保留；删除同事务内的人工 override，外键级联删除标签，原 FTS 触发器移除索引，并刷新词表使用量。清理与同步 / 标签维护使用相同锁序，事务失败整体回滚。补拉天数大于保留天数时，源中仍存在的旧录音可被再次拉回。

数据库旁 `.scheduler.lock` 通过非阻塞 `flock` 防止多个 tick 并发，进程退出自动释放。任务执行前先持久化本周期的 `last_run_at`，因此进程重启、同小时重复 tick、同日修改固定时间都不重复执行。异常消耗当次周期，写安全错误日志并使 CLI 非零，下个周期再运行；其他到点任务继续。若同步锁已被手动同步持有且任务尚未开始，恢复原运行标记，下次 tick 重试。

页面下次同步时间与调度共用计算函数，按 tick 的十分钟刻度向上取整；当天已经到点而未执行时显示最近的检查时间。计时器只负责每十分钟唤醒，不随设置修改。

## 部署与回滚

```sh
python3 scripts/deploy.py --app-only
# 在目标服务器以 root 执行：
bash /opt/qwennote/current/scripts/install_scheduler.sh
systemctl start qwennote-tick.service
systemctl list-timers --all 'qwennote-*' --no-pager
systemctl is-enabled qwennote-sync.timer qwennote-tag-maintenance.timer
```

新 `qwennote-tick.timer` 使用 `OnCalendar=*-*-* *:0/10:00 Asia/Shanghai`、`Persistent=true`、`AccuracySec=1s`，唤醒 `qwennote-tick.service`。service 使用原应用用户、环境文件、SQLite 动态库与数据目录写入限制。安装脚本先验证单元，再停用原 03:00 / 05:00 两个 timer，等待已经运行的旧任务自然结束，然后启用 tick。旧 timer 文件保留供回滚，不再启用。旧 `install_tag_maintenance.sh` 转到统一安装脚本，完整安装路径也只启用 tick 与原备份 timer。

回滚到旧 release 前先 `systemctl disable --now qwennote-tick.timer`，等待正在运行的 tick 完成；切回旧 release 后按需恢复旧同步与标签 timer。新增表可保留，旧版忽略它们。应用部署沿用 release SHA 校验、原子切换和健康检查；本单不改 nginx、其他站点或 `.scratch/`。

## 验证

- Python：871 项通过，原 807 项保留，新增 64 项覆盖设置读写和非法值、鉴权与 CSRF、可控时钟的到点 / 未到点、每日和间隔幂等、跨日与时区、崩溃后的运行标记、并发锁、忙碌重试、开关及级联清理 / FTS / 事务回滚。
- JavaScript：21 项原有交互检查通过。
- 设置浏览器：1 项，含 1440px 浅色、390px 浅色、320px 深色的保存、校验、CSRF 隔离与截图。
- 既有编辑与导出浏览器：3 项通过；仅将原来写死的 3 个导航入口断言更新为校验包含设置页的四个具体链接。
- 布局浏览器：8 项通过，覆盖首页、详情、导航、助手与标签的布局和交互。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/recording_edit_preview.py
# 另一个终端，设置已安装 Playwright / Chrome 的路径：
node --test tests/settings_browser.cjs tests/recording_edit_browser.cjs tests/export_browser.cjs
.venv/bin/python tests/render_detail_layout.py /tmp/qwennote-layout
LAYOUT_PAGES=/tmp/qwennote-layout node --test tests/navigation_layout.cjs tests/detail_layout.cjs tests/home_layout.cjs tests/tags_layout.cjs
```

设置页截图使用本地合成管理员和临时数据库，不包含生产业务数据。报告、截图、测试与部署证据放仓库外 `../qwennote-admin-artifacts/settings-14-20261030/`。现有公开授权仅涵盖技能说明，本单业务报告与截图保持本地私有，不声称公开发布。
