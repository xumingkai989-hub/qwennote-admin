# T5：查询 CLI、增量更新与调度接入

`python -m app.kb` 提供 build、list、show、search、note add/list/remove 七个入口。命令、退出码及回写示例见 [README 知识库](../README.md#知识库)。默认根目录为代码仓库的 knowledge；KB_ROOT 可统一覆盖同步、调度、CLI，--root 可临时覆盖单次命令。DB_PATH 必须指向已有 SQLite 文件，查询索引的 list/show 不依赖数据库配置。

`build(store, root, tags=None)` 返回三个数组：rebuilt/skipped 为标签名，errors 为脱敏错误对象。全量构建所有标签；指定 tags 只渲染对应标签、相关新旧分类，并更新索引。不存在标签且没有旧产物时视为空结果。删除孤立标签计入 rebuilt。未选中的文件与已有标签元数据保留到其被构建时再更新，避免提前将未构建内容标为最新。新目录只做增量时其余标签尚无文件，首次使用应先全量构建。

content_hash 升级为最终 Markdown UTF-8 的 SHA-256，包含会议标题、展示的总结、主议题、补充记录和综述。综述沿用 T4 输入缓存。hash 与旧索引及实际文件内容一致时跳过写盘；分类和索引逐字节比较，原子替换变化文件，索引数据相同则保留 generated_at。根目录文件锁串行化 CLI、同步和调度；临时文件写入失败不会截断旧文件。删除最后一条孤立补充后只清理已索引的对应文件，同名冲突保留其他标签现有文件名。单标签和分类渲染失败写 index error，继续处理其他文件；索引写入错误由调用方处理。

search 匹配全部关联会议标题、全部 SQLite 补充正文，而不受标签文件 20 条显示上限影响；综述来自已生成文件，搜索本身不调用模型。标签名与正文使用 Unicode casefold 字面包含匹配。JSON 空列表为 []，show 未命中为 null；运行错误只在 stderr 输出脱敏提示。补充记录提交先于构建，构建失败退出 2 但 stdout 保留已提交 id，避免调用方重复回写。

同步只收集实际 inserted/updated 记录，复用 get_recording 已读取的新旧状态；旧标签必须保留，因为原有 upsert 在正文变化时会清理 recording_tags。结束并释放同步锁后，把更新前标签与当前标签的规范名取并集，无标签会议使用未分类。构建失败仅记日志，不写 sync.error、不改变同步结果；kb_auto_build 默认 true，可在设置中心关闭。异步打标逻辑原样保留，后续词表/关联变化由每日全量覆盖。

scheduler_tick 在 board 后、enrichment 前增加独立 kb_full_build。时间与 board_time 一致，默认上海 22:00，沿用既有至多 10 分钟 tick 延迟；每日先落 claim 再执行，避免重启重复。看板关闭、已新鲜或失败不阻断知识库；知识库失败不阻断 enrichment。kb_auto_build 仅控制同步后的增量，不关闭每日全量。

验收覆盖七命令正常/空/错误路径、JSON 解析、完整内容 hash、所有文件 mtime 幂等、单标签和分类范围、路径冲突保留、损坏/缺失文件修复、部分失败与重试、并发构建、回写立即重建与 no-build、显示上限外搜索、同步更新前后标签与失败隔离、每日顺序及独立 claim。原有测试保留，并按新返回协议从 index.json 读取元数据；旧时间戳和调度结果断言改为本票幂等及新增任务合同。

发布只覆盖本票应用与文档测试文件，复用现有 release 运行依赖，执行 app-only activate，不执行 prepare，不修改 .scratch、nginx 或 /etc。真实数据和 CLI 验收输出保存在仓库外私有验收目录，不进入 Git 或公开存储。
