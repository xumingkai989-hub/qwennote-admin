# QwenNote Admin

工单 01：FastAPI + Jinja2 服务端渲染骨架、飞书登录与 SQLite 白名单。Python 3.11，使用标准库 `sqlite3`，无 ORM 或前端构建。

## 本地运行

```sh
uv venv --python 3.11
uv pip sync requirements.txt
cp .env.example .env
# 在本机填写 .env 中的六项配置后运行：
.venv/bin/python -m uvicorn app.main:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

环境使用 `/opt/homebrew/bin/python3.11`（3.11.12）创建；依赖及测试依赖完整锁定在 `requirements.txt`。更新依赖时运行 `uv pip compile pyproject.toml --extra test --output-file requirements.txt`。

| 环境变量 | 用途 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用密钥 |
| `FEISHU_REDIRECT_URI` | 飞书后台登记的完整回调 URL，路径 `/auth/callback` |
| `SESSION_SECRET` | 至少 32 字节的随机签名密钥 |
| `DB_PATH` | SQLite 文件位置，启动时自动创建父目录与表 |
| `ADMIN_OPEN_ID` | 首次启动加入白名单的管理员 open_id |

六项均必填。应用只读取环境变量；上述 `--env-file` 由 Uvicorn 将本地配置载入环境。可用 `.env.example` 中的命令在本机生成签名密钥。生产使用 HTTPS 回调地址，cookie 自动带 `Secure`；本地 HTTP 不带 `Secure`。

## 行为与接口

- `GET /`：未登录跳飞书授权页；有效会话且仍在活动白名单内返回“已登录”和“暂无录音”占位。
- `GET /auth/callback`：校验非空 `code` 和与发起登录浏览器匹配的 `state`，换取身份，再检查白名单。成功返回 302 到 `/` 并写 `HttpOnly`、`SameSite=Lax` 的签名 `session` cookie；白名单外或停用用户返回 403，不设置 session。
- Session 有效期 8 小时；签名错误、过期或用户被停用后重新引导登录。每次首页请求都会重新检查白名单。OAuth state 有效期 10 分钟，回调后清除；直接访问仅含 `code` 的回调不构成有效登录。
- 飞书失败返回通用 502 页面，不将服务商响应、授权码或凭据输出到页面。启动、首页重定向不调用飞书 API；真实 HTTP 请求仅发生在有效回调中。
- 启动以 WAL 模式幂等建立 `users` 与空 `recordings` 表。管理员仅在对应用户不存在时插入，不覆盖已有姓名、停用状态或加入时间。`recordings.created_at` 为笔记创建时间，后续按 Asia/Shanghai 使用。
- `create_app(settings=..., feishu_auth=..., store=...)` 可替换配置与两个依赖。`FeishuAuth`、`Store` 是 Protocol；路由也支持通过 `get_feishu_auth` / `get_store` 配合 FastAPI `dependency_overrides` 替换。启动迁移使用传给工厂的 Store；需要替换整个仓储时用工厂参数。

飞书适配器按[飞书授权码文档](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)及[获取用户令牌文档](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)实现；本工单没有连接真实飞书 API 或验证真实凭据。

## 测试

```sh
.venv/bin/python -m pytest -q
```

测试使用 `fastapi.testclient.TestClient`、假 FeishuAuth、独立临时 SQLite，以及 `httpx.MockTransport`。自动阻断 socket 网络连接、DNS 查询和真实 HTTP transport；不需要环境密钥。覆盖放行、拒绝、缺 code、有效/过期/篡改 session、撤销白名单、OAuth state、HTTPS cookie、HTML 转义、启动迁移、管理员种子及依赖替换。

## 后续范围

同步、录音列表、详情、搜索、只读 API、后台白名单管理、联系人搜索、部署配置均由后续工单实现；本次 `search_users` 仅保留接口。真实飞书联调尚未进行，需要后续提供环境配置并另行授权。
