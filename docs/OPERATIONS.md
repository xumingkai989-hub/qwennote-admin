# 运维手册

发布与恢复都围绕隔离 release 和共享数据目录执行；先保留可回退版本与备份，再切换服务。
所有服务器地址写作 `<server-ip>`，云实例标识写作 `<instance-id>`；域名用 `<domain>`，执行前在本机替换占位符。
本文给出基于现有脚本的人工 SSH 流程，不表示本工单实际登录服务器或执行部署。

## 1. 首次部署

### 1.1 确认前置条件

服务器流程面向提供 `dnf` 的 Linux，需 root、systemd、nginx、curl、openssl、gcc、tar、ACL 工具和 `/usr/bin/python3.11`。
`install_release.sh` 不安装 Python，也不替换系统解释器；`build_sqlite.sh` 编译 release 内私有 SQLite，需能下载官方源包。
DNS 的 `<domain>` 须指向 `<server-ip>`，防火墙开放 80/443；8090 仅本机使用。飞书应用回调配置为 `https://<domain>/auth/callback`。
云控制台若用于排障，人工选择 `<instance-id>`；下列流程走 SSH，不调用固化实例的 `scripts/deploy.py`。

### 1.2 本地打包与传输

在已通过回归且准备发布的仓库执行；`git archive HEAD` 只打包已提交内容，未提交修改不会进入 release。

```sh
git status --short
git rev-parse HEAD
release_stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_file="/tmp/qwennote-${release_stamp}.tar.gz"
git archive --format=tar.gz --output="$archive_file" HEAD
shasum -a 256 "$archive_file"
ssh root@<server-ip> "install -d -m 755 /opt/qwennote/releases/$release_stamp"
scp "$archive_file" "root@<server-ip>:/tmp/qwennote-${release_stamp}.tar.gz"
```

记下时间戳、提交 SHA 和归档 SHA-256。另开服务器终端，设置刚才的时间戳与摘要：

```sh
ssh root@<server-ip>
release_stamp='<release-stamp>'
archive_sha='<archive-sha256>'
release_dir="/opt/qwennote/releases/$release_stamp"
printf '%s  %s\n' "$archive_sha" "/tmp/qwennote-${release_stamp}.tar.gz" | sha256sum -c -
tar xzf "/tmp/qwennote-${release_stamp}.tar.gz" -C "$release_dir"
cd "$release_dir"
```

### 1.3 适配域名并准备 release

现有安装脚本与 nginx 模板固化了原站点域名。只在解包后的发布副本统一替换，包含证书路径和健康检查 URL；不修改源仓库业务代码。
以下命令从模板提取原域名，不需要在文档中写入原环境信息：

```sh
/usr/bin/python3.11 - <<'PY'
from pathlib import Path
import re
files = [Path('scripts/install_release.sh'), *Path('deploy/nginx').glob('qwennote*.conf')]
old = re.search(r'server_name\s+([^;\s]+);', Path('deploy/nginx/qwennote.conf').read_text()).group(1)
new = '<domain>'
assert '<' not in new and re.fullmatch(r'[A-Za-z0-9.-]+', new), '请先替换域名占位符'
for path in files:
    path.write_text(path.read_text().replace(old, new))
PY
bash scripts/install_release.sh prepare "$release_dir"
```

prepare 创建 qwennote 系统用户、数据/备份目录、独立虚拟环境、模型加密密钥与 FTS5 SQLite，并生成 release 专用 `app.main:app`。
`/etc/qwennote/qwennote.env` 首次模板使用 fixture，身份字段留空；此时不要直接将其当正式同步配置。

### 1.4 安全配置凭据

```sh
sudoedit /etc/qwennote/qwennote.env
chown root:root /etc/qwennote/qwennote.env
chmod 600 /etc/qwennote/qwennote.env
```

按 [README 配置表](../README.md#配置) 填写飞书身份、管理员、HTTPS 回调及数据源；保留 prepare 已生成的会话密钥。
生产 mcp 模式需通过既有授权渠道获取 OAuth JSON，在服务器受限文件中保存，不能放进终端命令或仓库。
文件格式来自 `app/mcp.py`：client_id、access_token、refresh_token 为非空字符串，obtained_at 为秒级 Unix 时间，expires_in 为非负秒数。
本仓库没有 OAuth 初次授权 CLI，不能凭空生成有效 token；需由千问授权流程提供文件。
文件安全放置后执行以下权限检查，不输出内容：

```sh
chown qwennote:qwennote /opt/qwennote/data/mcp_token.json
chmod 600 /opt/qwennote/data/mcp_token.json
runuser -u qwennote -- test -r /opt/qwennote/data/mcp_token.json
runuser -u qwennote -- test -w /opt/qwennote/data/mcp_token.json
runuser -u qwennote -- test -w /opt/qwennote/data
```

需要 AI 时可在模型中心添加配置；若用环境后备配置，文件为 `/etc/claude/deepseek.qwennote.env`，owner root:qwennote、权限 640。
无 AI 凭据可先上线阅读和同步。所有 Key 在安全编辑器或页面输入，不经命令参数传入。

### 1.5 证书、激活与验证

```sh
cd "$release_dir"
bash scripts/install_release.sh certificate "$release_dir"
bash scripts/install_release.sh activate "$release_dir"
systemctl is-active qwennote.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://<domain>/
nginx -t
systemctl list-timers --all 'qwennote-*'
readlink -f /opt/qwennote/current
```

未登录首页本机应为 302，HTTPS 应为 `302 0`。登录检查白名单，手动同步一次并核对列表/详情，再确认重复同步无重复新增。
激活启用 tick 与备份 timer，禁用旧同步/词表 timer；首次启动前数据库会自动迁移。
证书阶段可能使用 certbot 或隔离 acme.sh；核对实际启用的续期 timer，不能把申请成功当作自动续期已验证。

## 2. 日常发布与回滚

发布前完成 Python/JS 回归，并记录提交 SHA；按 1.2 打包传输、1.3 在新 release 替换域名。
先在服务器执行一次数据库备份（见第 3 节），prepare 新版本后用应用更新模式激活，保留现有 nginx、证书和 unit：

```sh
cd "$release_dir"
bash scripts/install_release.sh prepare "$release_dir"
QWENNOTE_APP_ONLY=1 bash scripts/install_release.sh activate "$release_dir"
readlink -f /opt/qwennote/current
readlink -f /opt/qwennote/previous
journalctl -u qwennote.service -n 100 --no-pager
```

prepare 不替换既有密钥；app-only 不安装更新后的 unit。如发布包含调度 unit 变化，需另行安装并核验：

```sh
bash /opt/qwennote/current/scripts/install_scheduler.sh
```

常规代码回滚：

```sh
bash /opt/qwennote/current/scripts/rollback.sh
systemctl is-active qwennote.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/
readlink -f /opt/qwennote/current
```

回滚成功交换 current/previous；不存在有效 previous 时拒绝回滚，失败则恢复原 current 并重启。
activate 本机检查失败会自动回链或停止首次服务；TLS 检查失败只退出，需人工检查 current 并决定运行回滚脚本。
这只是代码回滚。数据库迁移、人工编辑、上游热词新增不会被反向恢复；旧版不兼容现有表结构时按恢复流程停写处理。

## 3. 备份与恢复

### 3.1 日常数据库备份

```sh
systemctl start qwennote-backup.service
journalctl -u qwennote-backup.service -n 50 --no-pager
ls -lt /opt/qwennote/backups/qwennote-*.db
/opt/qwennote/current/.sqlite/bin/sqlite3 /opt/qwennote/backups/qwennote-<YYYYMMDD>.db 'PRAGMA quick_check;'
```

`backup.sh` 在线 `.backup` 到临时文件，校验为 ok 后重命名，独立备份锁避免重叠；只保留最近 7 个日期。
**已知缺口：只备份了数据库，token 未纳入。** 环境文件、secret.key、TLS 私钥也不在该脚本范围；备份只在本机，无异地副本。
恢复模型密文必须有备份时匹配的 `/etc/qwennote/secret.key`；token 可续期变化，旧副本不保证仍有效。
应通过受限的独立备份渠道保管这些文件并演练；当前仓库没有完整密钥备份/恢复自动化，不能只凭数据库备份宣布灾备完成。

### 3.2 恢复数据库

先通知使用者停止写入，确认没有人工 CLI 任务；停 timer 阻止新任务，等待当前定时工作自然完成，再停网页进程。
以下在 root 的服务器会话执行，尖括号路径须替换为已确认的快照日期；循环最多等 180 秒，未排空则中止恢复并排查。

```sh
systemctl stop qwennote-tick.timer qwennote-backup.timer
for unit in qwennote-tick.service qwennote-backup.service qwennote-sync.service qwennote-tag-maintenance.service; do
    for attempt in {1..180}; do
        unit_state=$(systemctl show "$unit" -p ActiveState --value)
        [[ "$unit_state" != active && "$unit_state" != activating ]] && break
        sleep 1
    done
    [[ "$unit_state" != active && "$unit_state" != activating ]] || exit 1
done
systemctl stop qwennote.service
snapshot_file='/opt/qwennote/backups/qwennote-<YYYYMMDD>.db'
test "$(/opt/qwennote/current/.sqlite/bin/sqlite3 "$snapshot_file" 'PRAGMA quick_check;')" = ok || exit 1
recovery_dir="/opt/qwennote/backups/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$recovery_dir"
for database_file in /opt/qwennote/data/qwennote.db /opt/qwennote/data/qwennote.db-wal /opt/qwennote/data/qwennote.db-shm; do
    if [[ -f "$database_file" ]]; then mv "$database_file" "$recovery_dir/"; fi
done
install -o qwennote -g qwennote -m 600 "$snapshot_file" /opt/qwennote/data/qwennote.db
systemctl start qwennote.service
systemctl is-active qwennote.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/
```

先核对登录、列表、详情及模型解密，再启动 timer；不要将旧 WAL 文件留在新快照旁。原数据库及 sidecar 已保留，可用于恢复失败时回退。
若数据库回到较早时间，settings 和 scheduler_state 也随之回退，重新启用 tick 前须确认保留天数及下次工作不会误删。

```sh
systemctl start qwennote-tick.timer qwennote-backup.timer
systemctl list-timers --all 'qwennote-*'
```

整机恢复还需重新准备 release、配置凭据和证书；补齐 token 与模型加密密钥之后才能恢复对应能力。

## 4. 定时任务清单

| 任务 | 时刻与作用 | 查看方式 |
| --- | --- | --- |
| qwennote-tick.timer → service | 上海每 10 分钟；读设置执行到期同步、词表维护和可选保留期清理 | `systemctl status qwennote-tick.timer`；`journalctl -u qwennote-tick.service` |
| qwennote-backup.timer → service | 上海每日 04:00；数据库在线备份并保留 7 个日期 | `systemctl status qwennote-backup.timer`；`journalctl -u qwennote-backup.service` |
| certbot-renew.timer 或 qwennote-acme.timer | 由 certificate 阶段实际选择；证书续期及 nginx 重载 | `systemctl list-timers --all`，检查实际续期服务日志 |
| qwennote-sync.timer、qwennote-tag-maintenance.timer | 已退役，统一 tick 安装时禁用，避免重复运行 | `systemctl is-enabled qwennote-sync.timer qwennote-tag-maintenance.timer` |

设置中心默认同步 03:00、词表维护 05:00；tag 定时任务维护词表，新增录音的打标由同步触发。
tick 先领取后执行；普通失败等下个周期，同步锁冲突才留给下一 tick 重试。不要用删除锁文件的方式强行重跑。
要手动验证调度，可执行 `systemctl start qwennote-tick.service`；它仍遵循数据库中的到期判断，不是强制同步命令。

## 5. 日志与故障排查

应用、tick 和备份标准输出均进入 journal，没有单独应用日志文件；nginx 日志位置由服务器主配置决定，仓库未自定义。
现有 `scripts/deploy.py` 将步骤日志写入本机 `/tmp/qwennote-deploy-logs/<UTC时间戳>/`；本文 SSH 发布需自行保存无凭据操作记录。

```sh
journalctl -u qwennote.service -n 100 --no-pager
journalctl -u qwennote-tick.service -n 100 --no-pager
journalctl -u qwennote-backup.service -n 50 --no-pager
systemctl status qwennote.service qwennote-tick.timer qwennote-backup.timer
```

| 症状 | 常见原因 | 处理 |
| --- | --- | --- |
| 启动失败 / Missing environment variables | env 未填、unit 未加载或权限错误 | 检查 EnvironmentFile 路径与必填项，不输出整个环境文件 |
| mcp token 文件缺失、续期失败 | 文件格式/权限、授权失效或网络问题 | 检查第 1.4 节字段和可写目录；通过千问重新授权，不回退假数据 |
| 登录被拒绝 | 不在活动白名单，或管理员 ID 类型不匹配 | 依据拒绝页核对 union_id/open_id，检查管理员配置和停用状态 |
| 登录 state / 管理表单 CSRF 失败 | cookie 过期、会话变更、回调域名或 HTTPS 设置不一致 | 核对回调后重新登录、刷新表单；不要绕过校验 |
| 同步返回 409 | 同库已有同步持锁 | 等现有批次结束，查 journal；不删除 *.sync.lock |
| 一部分录音未更新 | 详情/校验失败或超出补拉窗口 | 查看 sync_runs 安全摘要、核对设置补拉天数，再重试 |
| 修改的词搜不到 | 索引仅覆盖同步原文 | 使用原词检索；人工覆盖索引尚未实现，rebuild 也不会索引覆盖值 |
| FTS5/trigram 不可用 | 解释器加载了系统旧 SQLite | 检查 prepare 结果、release/.sqlite 与 unit 的 LD_LIBRARY_PATH |
| 搜索原文漏项且索引疑似损坏 | search_text 或 FTS 索引需要重建 | 备份后运行下方 rebuild 命令；不要手动修改影子表 |
| 无标签 / 热词未校正 | 无模型配置、判断失败、未覆盖到上下文或已撤销 | 核对默认模型和安全日志；原文保持可读，保守跳过属预期 |
| 模型无法解密 | secret.key 不匹配、无效或不可读 | 恢复与数据库匹配的密钥和权限；不要直接生成新密钥覆盖旧文件 |
| 热词推送失败/跳过 | 字符/长度、配额或已推送未变 | 看逐词结果；改为合规标准词；本地删除不能释放上游配额 |
| 改时间后任务没立刻执行 | tick 边界、当天已领取、任务关闭 | 看设置页下次时间及 scheduler_state，最多等到下一适用 tick |
| 发布失败 / 502 | 服务未启动、证书或域名错误 | 先查本机 302、journal、nginx -t，再核对 current；必要时 rollback |
| 备份失败 | 磁盘满、目录权限、锁冲突或源库缺失 | 查备份 journal 和磁盘/权限，排除后手动启动 backup.service |

重建命令只重算同步原文索引，使用 release 私有 SQLite 库，避免系统版本差异：

```sh
cd /opt/qwennote/current
runuser -u qwennote -- env DB_PATH=/opt/qwennote/data/qwennote.db LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib .venv/bin/python -m app.rebuild_search
```

## 6. 密钥管理

| 文件 / 凭据 | 权限与用途 | 轮换方式 |
| --- | --- | --- |
| `/etc/qwennote/qwennote.env` | root:root 600；systemd 读取飞书凭据、会话签名等；目录 root:root 700，应用仅有穿越 ACL | 安全编辑后重启应用，tick 下次启动加载新环境 |
| `/etc/qwennote/secret.key` | qwennote:qwennote 600；模型数据库密文的独立 Fernet 密钥 | 当前无批量重加密工具；先保全旧密钥与数据库，安全重录模型配置后才能切换，禁止直接覆盖 |
| `/etc/claude/deepseek.qwennote.env` | 建议 root:qwennote 640，仅需要时配置；模型环境后备值 | 在服务商撤换凭据，安全编辑，再重启应用并测试默认配置 |
| `/opt/qwennote/data/mcp_token.json` | qwennote:qwennote 600；续期需目录可写，稳定 .lock 协调原子换文件 | 自动续期；重新授权替换时先停新任务并排空在途调用，安全安装新文件再重启 |
| `/etc/letsencrypt/live/<domain>/privkey.pem` | 证书工具维护，通常 root 管理；不得扩大读取权限 | 使用实际证书工具续期与 nginx 重载钩子，检查 timer 和 HTTPS 验证结果 |
| 数据库中的 API Key 摘要 | 明文只在创建时输出一次，记录调用审计 | 创建替代 Key、更新调用方、再撤销旧 ID |
| 数据库中的模型 Key 密文 | 只向页面返回尾四位；默认模型优先于环境后备 | 新增替代模型并测试，切换默认后删除旧配置，再按服务商流程撤销旧 Key |

API Key CLI 仅在受控终端运行，不录屏、不重定向明文输出到日志；下列 `<key-id>` 替换为 list 返回 ID：

```sh
cd /opt/qwennote/current
runuser -u qwennote -- env DB_PATH=/opt/qwennote/data/qwennote.db LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib .venv/bin/python -m app.apikeys create --name '<用途>'
runuser -u qwennote -- env DB_PATH=/opt/qwennote/data/qwennote.db LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib .venv/bin/python -m app.apikeys list
runuser -u qwennote -- env DB_PATH=/opt/qwennote/data/qwennote.db LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib .venv/bin/python -m app.apikeys revoke <key-id>
```

SESSION_SECRET 轮换会使现有会话与 CSRF 失效，需重新登录；它与模型加密密钥是两个独立用途，不能互相代替。
日志排障只核对字段、状态、路径和权限，不复制凭据、cookie、授权头或完整服务商响应。
