# CSRF 测试偶发失败排查

基线：`c790bd7`，290 项测试。结论：测试构造的“另一个 session”在特定时间边界会等于原 cookie；本次不需要修改 `app/admin.py`。

## 根因与失败条件

`itsdangerous==2.2.0` 的 `TimestampSigner.get_timestamp()` 返回 `int(time.time())`，精度为秒。`SignedCookie.encode()` 对同一密钥、salt、身份和整数时间戳生成确定性的签名，没有额外随机 nonce。

设登录签发时间戳为 T，原测试重签时的整数时间戳为 R。旧写法使用 `original(self) - 1`，因此当 **R = T + 1** 时，新 cookie 的签发时间仍是 T，整个 cookie 与原值相同。签名有效期校验也通过：此时年龄为 1 秒，远小于 `SESSION_MAX_AGE = 28800`。这不是 `max_age` 恰好过期的问题。

`app/admin.py` 将 CSRF 绑定到 session cookie 的 SHA-256 摘要。替换为相同字节的 cookie 后，绑定自然仍然匹配。成功修改接口返回 **303**，而原断言要求 **403**，所以测试失败。客户端关闭自动重定向，复现中没有返回 200。

没有当时失败的完整 traceback，无法还原历史那一次响应；下述实验在原测试源码上确定性触发同一测试节点失败，验证了足以解释偶发现象的具体条件。没有发现不同 cookie 绕过绑定的路径。

## 确定性最小复现

在仓库根目录执行，不访问网络、不依赖真实时间、不修改 `.scratch/`：

```sh
.venv/bin/python -m scripts.reproduce_csrf_session_collision
```

输出：

```text
elapsed=0s same_session=False session_valid=True
elapsed=1s same_session=True session_valid=True
elapsed=2s same_session=False session_valid=True
```

脚本固定登录签发时间 T，分别模拟重签时推进 0、1、2 秒，运行原来的减 1 秒代码，断言 cookie 相等的条件和有效性。没有真实等待，结果可重复。

另外，在内存中加载 `git show c790bd7:tests/test_admin.py`，用受控的 `TimestampSigner.get_timestamp` 在 `csrf(client)` 返回后推进 1 秒，运行原测试节点，观察到：

```text
cookie_scopes=[('session', 'testserver.local', '/'), ('admin_csrf', 'testserver.local', '/admin')]
same_session=True form_matches_csrf_cookie=True
sent_session_cookies=1 sent_csrf_cookies=1 response=303
victim_written=True
FAILED test_mutations_require_signed_session_bound_csrf[other-session-/admin/users]
```

首次直接运行原节点时的断言输出是 `assert 303 == 403`。这也确认了数据库确实发生修改，而不是仅仅响应码不同。

## 其他排查方向

- `login()` 依次同步执行取 OAuth state 和回调；`csrf()` 随后同步 GET `/admin`，没有后台并发操作 cookie jar。
- `client`、`settings`、`store` 是函数级 fixture，每个测试创建新的客户端、临时数据库和随机密钥，客户端上下文结束后关闭；没有跨用例复用 cookie jar。
- 原复现中 session 作用域是 `testserver.local` 的 `/`，CSRF cookie 是该域的 `/admin`。替换前删除原 session，POST 实际只带一个 session 和一个 CSRF cookie；没有残留 cookie 的选择歧义。
- CSRF 签名使用独立 salt，校验 cookie 与表单 token 相等，再校验绑定摘要。此次复现的摘要没有变化，因为所谓新 session 实际与原值相同。

## 修复和断言强度

`replace_session()` 从原 session 中读取签发时间，将替代 session 固定签发于“原签发时间减 1 秒”，不再使用重签时的真实时间。当前依赖的 `TimestampSigner.sign(value)` 没有 `timestamp=` 参数，因此只在同步编码期间将 `get_timestamp` 固定为指定整数；HTTP 请求在该 patch 退出后执行。

辅助函数额外断言新旧 session 不同、替代 session 能解码为同一管理员身份。原有的“POST 必须 403”和“数据库不得变化”断言全部保留。

新增 4 个回归场景：新增/移除两个接口 × 不跨秒/推进 1 秒。整个签名时钟固定，测试先成功创建真实 victim，随后替换 session，确认 GET `/` 仍能通过认证，再要求旧 CSRF 返回 403、错误页包含 CSRF 校验失败、数据库完全不变。这确保拒绝发生在 CSRF 校验处，也使移除路径检查真实存在的目标。

修复后，同样的“取得 CSRF 后推进 1 秒”原测试节点通过。另做内存回退实验：只将辅助函数恢复为旧时间模拟，新增场景得到 **2 passed、2 failed**；两个推进 1 秒的接口均以 `303 != 403` 稳定失败。实验没有修改磁盘源码。

## 验证

环境：Python 3.11.12，pytest 8.3.5，itsdangerous 2.2.0，httpx 0.28.1，Starlette 0.46.2。

修复后执行 `.venv/bin/python -m pytest -q`，连续启动 20 个独立进程，按顺序执行，没有失败重试。结果如下（每轮 294 项，新增 4 项）：

| 轮次 | 完整 pytest 结果 | 退出码 |
| --- | --- | --- |
| 01/20 | 294 passed, 1 warning in 7.92s | 0 |
| 02/20 | 294 passed, 1 warning in 8.14s | 0 |
| 03/20 | 294 passed, 1 warning in 8.03s | 0 |
| 04/20 | 294 passed, 1 warning in 8.10s | 0 |
| 05/20 | 294 passed, 1 warning in 7.97s | 0 |
| 06/20 | 294 passed, 1 warning in 7.95s | 0 |
| 07/20 | 294 passed, 1 warning in 8.02s | 0 |
| 08/20 | 294 passed, 1 warning in 8.07s | 0 |
| 09/20 | 294 passed, 1 warning in 8.02s | 0 |
| 10/20 | 294 passed, 1 warning in 8.07s | 0 |
| 11/20 | 294 passed, 1 warning in 8.08s | 0 |
| 12/20 | 294 passed, 1 warning in 8.01s | 0 |
| 13/20 | 294 passed, 1 warning in 8.10s | 0 |
| 14/20 | 294 passed, 1 warning in 8.13s | 0 |
| 15/20 | 294 passed, 1 warning in 8.06s | 0 |
| 16/20 | 294 passed, 1 warning in 8.05s | 0 |
| 17/20 | 294 passed, 1 warning in 8.15s | 0 |
| 18/20 | 294 passed, 1 warning in 8.18s | 0 |
| 19/20 | 294 passed, 1 warning in 8.08s | 0 |
| 20/20 | 294 passed, 1 warning in 8.05s | 0 |

每轮出现同一条现有的 Starlette/AnyIO `BlockingPortal` 弃用警告；无 skip、xfail 或断言放宽。`.scratch/` 未改动。完整逐轮原始日志保存在仓库外的个人任务记录目录。
