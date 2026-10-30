# 工单 13：导出拆分为总结与转写

`GET /recordings/{id}.md?part=summary|transcript|all` 在既有登录及白名单校验后选择导出分区。省略、空值及非法值回退为 `all`；默认合并文档与原格式逐字一致。

三个分区均保留完整 frontmatter（`title`、`created_at`、`duration_min`、`recording_id` 以及现有内容状态字段）和 `# 标题`。总结仅输出 `## 总结`，转写仅输出 `## 转写原文`，空内容保留各自占位。文件名沿用原有清洗与 RFC 5987 编码，分区后缀为 `-总结.md`、`-转写.md`、`.md`。

内容继续使用 `apply_recording_hotwords`：先 `strip_origin_markers` 再热词替换，人工 override 仍优先且清理溯源标记。部分导出仅对实际导出的字段计热词命中；原始正文与 override 不改写。菜单依次为“导出总结 / 导出转写原文 / 导出全部 / 复制 Markdown”，复制仍请求默认合并文档。所有下载项保持键盘操作、关闭菜单与焦点恢复，更新脚本版本以避免旧缓存。

## 验证

- Python：807 passed，原有 777 项保留，新增 30 项覆盖分区、元信息、文件名、非法值、特殊字符、空内容、展示管线及 override。
- JavaScript：21 passed，原有 19 项保留并更新菜单结构，新增两个下载项的键盘激活检查。
- 浏览器：1 passed，覆盖 1440px 浅色、390px 浅色、320px 深色；三个配置各下载三份真实文件，检查文件名、分区正文、溯源清理、菜单顺序、键盘、焦点与溢出；保留菜单展开截图。页面使用本地合成数据。

```sh
.venv/bin/python -m pytest -q
node --test tests/detail_interactions.cjs tests/home_interactions.cjs
.venv/bin/python tests/recording_edit_preview.py
# 在另一终端，使用已安装的 Playwright/Chrome：
node --test tests/export_browser.cjs
```

部署使用 `python3 scripts/deploy.py --app-only`，沿用独立 release、归档 SHA 校验与原子切换。报告、截图、测试日志及部署证据保存在仓库外 `../qwennote-admin-artifacts/export-13-20261030/`。不改数据层、`.scratch/`、nginx 或其他站点。
