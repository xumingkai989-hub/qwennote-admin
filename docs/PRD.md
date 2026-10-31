# 产品需求文档

系统把千问笔记内容保存在自建单机服务，供获准用户检索、阅读和导出，由管理员校正内容与维护自动化规则。
本文以当前代码为现状依据；未实现内容单独标明，优先级表示产品重要程度，不代表承诺排期。

## 1. 背景与目标

会议录音分散在千问笔记中，回找讨论需要重复打开笔记；术语误识别会继续进入复制与导出结果。
目标是保留上游原文，建立统一检索入口，并让人工修改和模型校正可撤销、可追溯。

容量判断按千条级录音起步，不把生产数据量写成未经核实的数字。
`scripts/benchmark_search.py` 用 1,000 条合成录音验证搜索路径；每条总结 600 字、转写约 2,700 字。
长会议转写按万字级考虑；编辑接口允许单字段最多 200,000 字，这只是输入上限，不是该规模的性能证明。
AI 打标仅取总结前 1,500 字和转写前 2,000 字；热词判断每词每字段最多取 5 个上下文窗口，不能保证长文覆盖。
成功标准是能找到原始讨论、修正具体词语并导出当前显示内容，不增加另一套音视频系统。

## 2. 用户与角色

| 角色 | 可以做什么 | 边界 |
| --- | --- | --- |
| 普通使用者 | 飞书登录、查看全部已同步录音、手动同步、搜索、复制与下载 | 必须在活动白名单中；没有按录音所有者隔离数据 |
| 管理员 | 普通能力，加白名单管理、正文编辑与撤回、热词管理与推送、助手/设置/模型配置 | 身份须匹配 `ADMIN_OPEN_ID`；管理操作重查活动状态及角色 |

API Key 是服务凭据，不是第三种网页角色；持有有效 Key 可读取整个录音集合，当前不细分 scope。
API Key 创建、列出和撤销使用服务器 CLI，不提供网页签发入口。

## 3. 核心场景

1. 他打开页面想找上周讨论过的词 → 系统搜索标题、总结和转写，给出命中片段及可进入的详情。
2. 他打开一场会议想核对总结与原话 → 系统提供双视图，转写保留发言人、偏移时间和本场搜索。
3. 管理员打开详情想改一处术语 → 系统保存本地覆盖，标注编辑时间，可撤回；可选择将短词存入热词表。
4. 管理员打开设置想改变每日同步时间 → 系统保存校验后的时间，展示下次执行时间，由后续 tick 生效。

## 4. 功能需求

验收按可观察行为判定；F5 中的完整编辑历史仍未实现，不能用最新覆盖值代替多版本记录。

| 编号 | 需求描述 | 验收标准 | 优先级 |
| --- | --- | --- | --- |
| F1 | 飞书登录及白名单准入 | 未登录首页跳授权；错误 state 返回 400；停用/未授权身份被拒绝；管理员可增停用户 | P0 |
| F2 | 手动同步千问录音与详情 | 活动用户可触发 `/sync`；批次有状态与计数；详情失败后其他条目仍提交，批次标为 failed | P0 |
| F3 | 幂等与同步互斥 | 相同输入二次同步新增/更新均为 0；重复 ID 只处理一次；同库并发返回 409 且不增批次 | P0 |
| F4 | 列表与总结/转写阅读 | 每页最多 20 条，日期分组且倒序；详情仅渲染当前视图；缺内容有明确提示；未知 ID 返回 404 | P0 |
| F5 | 管理员就地编辑、撤回与编辑历史 | 当前：最多 200,000 字/字段，保存覆盖与更新时间、撤回恢复同步内容；待定：逐版本历史及作者查询，当前无此表/接口 | P1 |
| F6 | AI 自动打标 | 新增或正文/标题变化后尝试打标，最多 5 个去重标签；无模型凭据跳过；打标失败不使录音同步失败 | P1 |
| F7 | 词表每日迭代与助手配置 | 规范化重复可合并；语义相似仅生成提案；管理员可接受/拒绝、手动合并/停用；反馈指令超过 2,000 字拒绝 | P1 |
| F8 | 标准热词维护 | 标准词唯一，新增校验为 1–20 字且无句式标点；可编辑/停用/删除；历史长句标记待整理 | P1 |
| F9 | 模型判断后按位置校正与撤销 | 仅接受真实且可唯一定位的短片段；失败保留原文；详情展示轨迹；撤销指定 decision_id 后该处恢复，其他位置不受影响 | P0 |
| F10 | 向千问推送标准词 | 单词提交只含一个标准词；已成功且词未变可跳过；批量串行；保存尝试结果；配额满显示原因 | P1 |
| F11 | Markdown 导出与复制 | 总结/转写/全部对应所选内容；含 frontmatter；文件名支持中文；人工覆盖与生效热词进入导出；无权限拒绝 | P0 |
| F12 | 全文检索 | 标题/总结/转写原文可命中；同条不重复；3 字以上用 FTS5，1–2 字用转义 LIKE；空搜索回首页 | P0 |
| F13 | 对外只读 API 与审计 | 有效 Bearer Key 可读四类端点；无效/撤销 Key 返回 401；每次调用记录路径、状态，不存请求凭据或查询正文 | P1 |
| F14 | 设置中心与定时任务 | 保存启停、每日同步模式、日时刻/小时间隔、补拉及保留天数、每周全盘开关/星期/时间；非法值拒绝；tick 读新值；按上海时区自然周比较 full_sync 最近领取时刻，每周最多领取一次，失败或撞锁也消耗槽位，修改星期/时间不重复领取 | P1 |
| F15 | 模型中心 | 可增模型、测试、切换默认、启停/删除；默认模型不可直接停用/删除；Key 仅显示尾四位，数据库存密文 | P1 |
| F16 | 同步模式与水位 | 快速同步只处理水位回退 10 分钟后的窗口内容，完整记录跳过；水位缺失、异常或查询失败退化全盘；无新内容且窗口内没有待补全记录时不发起详情请求；全盘同步忽略水位、逐条拉取补拉范围内详情 | P0 |

F14 的同步策略默认值如下；星期按 0=周一、6=周日保存，时间均为上海时区。

| 配置项 | 默认值 | 行为 |
| --- | --- | --- |
| `sync_mode_default` | `quick` | 每日/间隔同步及未指定模式的 CLI 使用，可选 `quick` / `full`；列表两个按钮各自固定模式 |
| `full_sync_enabled` | `true` | 独立启停每周全盘任务 |
| `full_sync_weekday` | `6`（周日） | 仅在配置的星期运行；停机错过该日不在次日补跑 |
| `full_sync_time` | `03:30` | 配置日到时后由每 10 分钟的 tick 领取 |

F16 的水位来自最近一次成功批次的 `started_at`，不是结束时间；窗口起点为水位减 10 分钟且不早于补拉范围，不再固定回退 2 天。
千问列表实测按创建时间降序，快速同步遇到早于窗口起点的记录即停止后续翻页；已发现非降序时禁用早停。
列表快速同步为主按钮、全盘同步为次按钮；同步时禁用两者，全盘显示「正在扫描…」和耗时提示；「上次」摘要显示实际模式及检查/新增/更新/跳过计数，退化全盘时显示全盘。
转写段落缺少或为空的 `speakerName` 显示「未标注发言人」，其他无效段落字段只跳过该段；响应结构损坏仍按详情失败处理。

搜索索引基于同步原文和清理后的溯源标记，不索引人工覆盖或热词展示结果；F12 不承诺查到编辑后的新增词。
标签目前用于卡片展示，未实现按标签筛选；有此需求时先验证其检索价值再排期。

## 5. 非功能需求

### 性能

首屏分页固定 20 条，详情只渲染所选视图，避免整批正文进入首页 HTML。
建议验收门槛：在固定硬件、1,000 条上述合成录音、应用预热、同机访问条件下，首屏响应 P95 ≤ 2 秒，搜索首批 P95 ≤ 500 毫秒。
这些是待测目标，仓库没有首屏 SLA 或生产压测结果；搜索可运行 `python -m scripts.benchmark_search` 测 100 次并报告 P95。
基准分别覆盖 3 字以上 FTS5 和 2 字 LIKE，不把内存索引或单次 SQL 耗时当成浏览器首屏时间。

### 安全

凭据只在本地受限配置、OAuth 文件或加密字段中；API Key 只存 SHA-256，模型密钥由独立 Fernet 文件保护。
会话 8 小时，OAuth state 10 分钟；cookie 为 HttpOnly、SameSite=Lax，HTTPS 回调时附 Secure。
管理员表单使用与当前 session 绑定的签名 CSRF cookie，校验表单相等及大小/重复字段；同步与退出校验存在的 Origin。
后者不是完整的统一 CSRF 中间件；不能把管理员表单保护泛化成所有 POST 的同一实现。
Markdown 禁用原始 HTML，模板自动转义；API 审计、标签变更和热词历史保留操作依据，正文编辑尚无完整审计历史。

### 可用性

日备份使用 SQLite 在线备份并 quick_check，保留最近 7 个日期；同日重跑覆盖当日备份。
发布通过 `current` 软链切换，应用健康检查失败可回到旧 release；数据库 schema 不随代码自动降级。
数据库备份不包含 OAuth token、环境配置、模型加密密钥或 TLS 私钥，灾难恢复必须另行保管这些文件。
同机备份不能覆盖整机丢失；没有异地自动备份和高可用承诺。

### 兼容

390px 宽度下列表、详情、设置和热词页面无整页横向溢出，主要操作可点击；宽代码块/表格可在局部滚动。
桌面与移动宽度覆盖明暗模式；剪贴板失败提供手动复制回退，浏览器检查使用合成页面，不等于实体手机认证验收。

## 6. 数据模型

逻辑关系不等于数据库外键；下表特别注明实际 FK。完整字段以迁移代码为准。

| 表名 | 关键字段 | 关系 / 用途 |
| --- | --- | --- |
| `users` | subject PK、union_id、open_id、is_active | 飞书别名交叉匹配白名单；owner_open_id 不作为录音可见性过滤 |
| `recordings` | recording_id PK、created_at、summary_md、transcript_md、search_text | 同步原文主表，含内容状态、时长和 media_type |
| `sync_runs` | id PK、started_at、status、fetched、inserted、updated、skipped、mode、refreshed_token | 一次获准同步一行；mode 为实际 quick/full（含退化），skipped 统计窗口内已完整而跳过详情的记录；成功批次提供水位 |
| `recording_overrides` | (recording_id, field) PK、content_md、updated_at | 逻辑关联录音，每字段仅保留最新人工覆盖，无 FK |
| `recording_tags` | (recording_id, tag) PK、position、model | FK → recordings，删除级联；tag 按名称逻辑关联 tags |
| `tag_runs` | id PK、attempted、succeeded、failed、input_tokens、output_tokens | 打标运行与 token 用量统计 |
| `tags` | id PK、name UNIQUE、status、merged_into、usage_count | merged_into 自引用 FK，表示合并去向 |
| `tag_aliases` | alias PK、tag_id | FK → tags，保存规范化别名 |
| `tag_merge_proposals` | id PK、from_tag、to_tag、reason、status | 按标签名逻辑关联，提案对唯一 |
| `tag_semantic_checks` | tag_id PK、checked_at | FK → tags，避免重复语义检查 |
| `tag_change_log` | id PK、at、action、detail | 词表、助手指令与维护批次记录 |
| `assistant_config` | id=1、instructions、updated_at | 单行助手反馈指令 |
| `hotwords` | id PK、term UNIQUE、enabled、needs_review | 标准词表，与以下记录用 hotword_id 逻辑关联 |
| `recording_hotword_decisions` | id PK、recording_id、hotword_id、field、span、start_offset、replacement、revoked | 一处替换一条；位置组合唯一；无 FK |
| `recording_hotword_checks` | (recording_id, hotword_id, field) PK、status、reason | 每词每字段判断结果，含跳过情况 |
| `recording_hotword_decision_history` | history_id PK、id、archived_at、operation | decision 更新/删除前由触发器存旧值 |
| `recording_hotword_check_history` | history_id PK、recording_id、archived_at、operation | check 更新/删除前存旧值 |
| `recording_hotword_legacy_decisions` | id PK、recording_id、hotword_id、target、archived_at | 仅旧版迁移时可能创建，保存旧决策；新库无此表 |
| `hotword_upstream_sync` | hotword_id PK、upstream_value、synced_at、status | 每词最近推送状态 |
| `hotword_upstream_errors` | hotword_id PK、message | 每词推送安全错误摘要 |
| `hotword_upstream_runs` | id PK、started_at、finished_at、status、plan_json | 追加式推送尝试历史 |
| `hotword_upstream_state` | id=1、snapshot_json | 上游词表及配额快照 |
| `settings` | key PK、value | 同步/打标时间、开关、补拉/保留天数 |
| `scheduler_state` | job PK、last_run_at | tick 各任务最近领取时刻 |
| `models` | id PK、provider、base_url、model_id、api_key_enc、key_last4、is_default | 默认模型唯一；独立于环境后备配置 |
| `api_keys` | id PK、key_hash UNIQUE、revoked_at、last_used_at | 仅存摘要，撤销后不可再鉴权 |
| `api_audit` | id PK、key_id、path、status_code、at | key_id FK → api_keys，失败鉴权可为空 |
| `recordings_fts` | rowid、search_text | FTS5 外部内容表，rowid → recordings.rowid；影子表由 SQLite 管理 |

## 7. 对外 API

下表均使用 `Authorization: Bearer <api-key>`，与网页登录 cookie 分离；接口只读。

| 端点 | 方法 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| `/api/v1/recordings` | GET | API Key | 列表；date 或 from/to，limit 默认 20、最大 100，offset 默认 0 |
| `/api/v1/recordings/{recording_id}` | GET | API Key | 详情；返回当前覆盖与热词生效后的总结/转写 Markdown |
| `/api/v1/daily/{day}` | GET | API Key | YYYY-MM-DD 当日汇总，format=json 或 md；Markdown 包含完整内容 |
| `/api/v1/search` | GET | API Key | 必填 q，可传日期与 limit；返回匹配录音和原文字段名 |

列表和 daily JSON 返回元数据；详情与 daily Markdown 返回正文。非法参数返回 400，未知详情返回 404。
所有 API 调用经过鉴权审计边界并禁止缓存；无写入 API、按用户隔离或细粒度 Key 权限。

## 8. 明确的非目标

- 不做播放器：当前持久化核心是文本，不承担音频托管、播放与时间轴跳转。
- 不做多人协作编辑：没有锁定/冲突合并/作者版本模型，当前面向管理员维护。
- 不做 Obsidian 同步：Markdown 下载与只读 API 已提供集成出口，双向文件同步会引入额外状态。
- 不做新的转写引擎：转写和总结来自千问，不接管音频识别任务。
- 不做多租户与组织隔离：当前白名单用户共享录音库，租户隔离需要重新设计权限和迁移。

## 9. 已知边界与风险

- 千问 MCP 是外部依赖，工具字段、分页、限流和 OAuth 授权可能变化；当前续期/重试不能保证一直可用。
- 快速同步不会发现上游对老会议的修改（如重新生成总结）；窗口内已完整记录也跳过详情，需等每周全盘或手动全盘同步。
- 若上游列表降序维护失败，且异常未在早停前被发现，早停可能提前结束，本次结果可能不完整；下次全盘可在补拉范围内补齐。
- 热词采用保守策略：短上下文、唯一位置、长度与 JSON 校验都通过才替换；宁可保留原文，仍可能漏判或误判。
- 千问当前只接入读取与新增热词工具，上游热词不可删；本地删除或改词不等于撤回旧推送，配额按返回值且最多接受 200。
- 本地热词支持 1–20 字词语；上游还限制字符类型与中文/非中文数量，因此本地有效词不一定可推送。
- 人工覆盖优先于自动校正，后续同步不覆盖人工值；搜索仍基于同步原文，展示与搜索可能不同。
- tick 在外部操作前领取周期；普通失败、崩溃或同步锁冲突均消耗该周期，下一周期再试，每周全盘等下一周配置日；可手动同步。
- 单机 SQLite 和同机备份无高可用；旧代码回滚不能逆转数据库迁移或上游新增热词。
- 模型中心虽然记录 provider，当前请求按 Messages 协议发送；仅支持 OpenAI 协议的端点不能直接视为兼容。

## 10. 路线图

### 已完成

F1–F4、F6–F16 的上述当前验收行为，以及 F5 的就地编辑、最新覆盖与撤回已有实现和回归测试。
具备单机 release 发布、数据库日备份、代码回滚和分层测试工具。
10-30 已完成快速/全盘同步、水位回退 10 分钟、列表早停、跳过完整记录、每周全盘调度、批次 mode/skipped、段落缺发言人容错，以及同步按钮主次/全盘进行中/「上次」摘要。
已采用 MIT 许可证；部署域名使用 `<domain>` 占位符，实例 ID 和域名由环境变量传入。

### 进行中

暂无已确认的进行中功能项；产品、架构与运维说明已同步上述行为，并由文档一致性测试校验。

### 待定

完整编辑历史（含作者）、覆盖内容检索、密钥与 token 的独立恢复演练、固定硬件性能门槛实测尚待决策。
新增功能先判断是否减少回找、校正或导出的步骤，并明确权限、持久状态与验收方式；不默认扩展到多人协作或播放器。
