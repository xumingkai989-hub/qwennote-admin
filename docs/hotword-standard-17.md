# 工单 17：标准词与 AI 识别变体

热词中心只录入 `term`（标准答案），`aliases` 是可选提示，`note` 是备注。列表为标准词、别名提示、命中次数、状态、操作。编辑正文后的可选热词建议及 bulk 接口同步改为 `{term, aliases}`，按标准词去重。

## 迁移与历史

启动在同一 `BEGIN IMMEDIATE` 事务中重建旧 hotwords：target 作为 term，source 作为 aliases；相同 target 合并，distinct source 追加、hits 累加、备注保留，启用状态取任一启用，时间取最早创建及最后更新，保留首行 ID。最终表没有 source/target。迁移异常回滚全部热词变化。旧无 span 的决策先完整归档至 recording_hotword_legacy_decisions（附原 source/target 和理由），再清空重建当前表。上线期间新增的旧格式 apply 同样保留，不按新位置模型自动生效，随后重新判断。

新 `recording_hotword_decisions` 保存工单指定的 id、recording_id、hotword_id、field、span、replacement、reason、decided_at、revoked，并增加 `start_offset/end_offset`（Python 字符偏移，基于去除 origin 标记后的正文）。

**位置约束的必要补充**：仅 `UNIQUE(recording_id,hotword_id,field,span)` 无法保存相同 span 两次出现的独立判断。本实现将唯一键扩充为 `(recording_id,hotword_id,field,span,start_offset)`，同一处幂等、不同处可独立撤销。替换前同时验证坐标与原片段，不会把同一字符串的其他位置一起替换。

`recording_hotword_checks` 按热词/字段保存 apply/skip 与判断完成状态，更新/删除前通过触发器写入 `recording_hotword_check_history` 保留历次 skip 与失败记录；空数组、失败都不创建虚假的替换决策。`recording_hotword_decision_history` 配合 UPDATE/DELETE 触发器归档旧决策；撤销保留原理由并设 revoked=1，重判、词条修改/删除、同步正文变化前的决策均有历史。显式 force 也保留已撤销位置。

## 判断

每个热词、每个有效字段取最多 5 处窗口，已知提示附近前后各至多 60 字；相邻提示的窗口在中点分隔，确保相同片段分别判断。标准词片段与别名仅用于优先取上下文，不构成替换规则；其余正文窗口补足名额。没有别名或字面命中时仍发送非空文本。长文受五处窗口上限约束，不能保证覆盖所有未列出变体。

每处上下文单独请求模型，输入标准词、可选别名及文本，输出严格 `{"replacements":[{"span":"星河直播","reason":"上下文明确指代同一产品"}]}`；空数组即无替换。服务端绑定该窗口的绝对坐标，只接受真实出现且唯一可定位的连续片段（1–20字）。相同片段在该窗口多次出现时丢弃，不能猜测其位置。标准词本身不再替换。

非法 JSON/结构、HTTP 错误或热词/字段总计 30 秒超时：该热词字段不替换，check 记 `judge_failed`。不存在或无法定位的 span 单条丢弃，check 同样记 `judge_failed`；同一输出中的其他有效 span 可保留。禁止提供方错误原文进入数据库或页面。空数组记 `skip/no_replacement`，无有效正文记 `skip/no_content`。

判断期间继续保留录音、启用词条版本与旧决策快照校验；内容、规则或撤销变化时拒绝写入迟到响应。CLI 与同步通过数据库旁文件锁串行判断，展示不等待模型。

## 展示、撤销与出口

只读取指定 recording_id + field 的启用、未撤销决策。按 span 长度降序选择不重叠位置，再从后向前应用原文坐标，替换结果不再扫描。保留原有 Markdown 结构、代码、URL 保护与纯文本转义。人工 override 优先；标题、原始数据库正文、搜索索引均不改。

总结视图、转写视图、`.md`、单条正文 API、daily Markdown 使用同一展示管线。顶部“热词已应用 N 处”统计两个字段实际生效位置，逐行显示 span → replacement、理由和撤销按钮。撤销必须提交 decision_id，不能一次误撤整个字段；权限与 CSRF 沿用现有管理员边界。hits 继续按实际展示/导出次数累计。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
# 测试默认阻断真实网络，模型使用 MockTransport。
python -m app.hotword_judge --all
# 重判启用词条；历史与人工撤销保留
python -m app.hotword_judge --all --force
python3 scripts/deploy.py --app-only
```

离线验收：原 984 项按新字段及位置决策更新前置与断言，保留数量和既有保护、权限、并发、出口覆盖；新增 23 项标准词、迁移合并/回滚、同词多位置、未知变体、精确撤销与表单测试，总计 1007 项 Python。另有 21 项 JavaScript、热词浏览器及编辑浏览器回归；发布前实际结果与生产判断分布另存私有验收目录。

部署前备份 SQLite 并记录 recordings 全行哈希，迁移、补判后核对哈希与完整性。只用 app-only，不改 nginx 与其他站点。由于旧代码依赖 source/target，不能将旧 release 直接切回新 schema；回滚须停本应用写入、备份新决策，再用部署前数据库备份配套恢复旧 release，不能丢弃已归档判断。

本次是项目功能改造，技能索引查重后不新增通用个人技能。业务报告/截图超出已授权的公开技能说明范围，保持私有；技能状态、Git 归档、公开发布、Issue 上报分别记录。
