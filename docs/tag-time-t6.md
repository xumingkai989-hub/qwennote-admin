# T6：判定健壮性与对比图比例尺

零标签与单标签直接生成 `source=rule`，分别记录 `rule_no_tag`、`rule_single_tag`，不解析模型配置、不请求模型。单标签缺少词表 ID 时仍保留该标签名；时间分配维持原有缺失词表处理。`week_allocation` 识别 model/rule 两种有效来源，两项分配接口的返回结构不变。

多标签模型异常仅保存 `model_error:<异常类型>`；空文本为 `empty_response`，非法或集合外结果为 `out_of_set:<脱敏后的原返回前80字>`。脱敏在截断前处理配置中的 key、URL 及常见凭据标记，不存异常消息。未配置模型时没有响应，记 empty_response，不计模型异常。

迁移先检查 sqlite_master 中现有建表 SQL，旧 CHECK 通过事务内重建表加入 rule，保留已有记录、显式索引和触发器；重复迁移不重建。旧零／单标签判定以及缺 reason 的 fallback 在 ensure_week 时刷新。可显式 `force=True` 重判一周，默认继续使用指纹缓存。

`ensure_week` 的 judged/fallback 为本次写入数，新增 rule 计数；errors 为当前周判定中的模型异常数，包含仍缓存的异常，避免再次生成时错误被隐去。读写故障单列 failed/failures。只有 errors>0 才生成「主标签判定部分失败（N 场）」；纯 fallback 不产生周报错误。读写故障另报「主标签记录读写失败」。

基线 b6dd1ec 的 allocation_comparison 已按全局最大周总时长计算坐标。本票明确输出 minutes/max_minutes 宽度，SVG 与 resize 共用该比例，连接仍使用相同全局坐标；刷新脚本缓存版本。增加 828/1284 分钟（13.8h/21.4h）的断言和 1440/390 浏览器实际像素检查，最长条 100%、短条 64.486%。不修改今日饼图、本周柱状图、时间分配饼图的绘制代码。

验证日志、线上重判结果、1440 截图和本地 HTML 说明位于仓库外 `../qwennote-admin-artifacts/tag-time-t6-20261114/`。发布只复制现有运行依赖并覆盖本票提交文件，执行 app-only activate，不安装依赖，不改 .scratch、nginx 或 /etc。项目内容没有公开存储授权，不上传公开站点。

验证：Python 1722 项通过（原 1705 + 新增 17），43 项 JavaScript、16 项浏览器通过；1 条既有 Starlette/AnyIO 弃用警告。迁移测试覆盖旧 CHECK 重建、记录及索引保留、非法 source 拒绝和重复迁移。
