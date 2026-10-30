import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.feishu import FeishuUser
from app.main import create_app


def begin_login(client):
    response = client.get("/")
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def login(client):
    return client.get(
        "/auth/callback", params={"code": "valid-test-code", "state": begin_login(client)}
    )


def test_anonymous_home_redirects_to_feishu(client, settings):
    response = client.get("/")
    url = urlsplit(response.headers["location"])
    assert response.status_code == 302
    assert url.scheme == "https"
    assert url.netloc == "accounts.feishu.cn"
    assert url.path == "/open-apis/authen/v1/authorize"
    params = parse_qs(url.query)
    assert params["client_id"] == [settings.feishu_app_id]
    assert params["redirect_uri"] == [settings.feishu_redirect_uri]
    assert params["response_type"] == ["code"]
    assert params["state"]
    assert "session" not in response.cookies


def test_whitelisted_callback_sets_httponly_cookie(client):
    response = login(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.cookies.get("session")
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("session="))
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=" in cookie


def test_non_whitelisted_user_is_forbidden(client, fake_auth):
    fake_auth.user = FeishuUser(uuid4().hex, "未授权用户", "")
    response = login(client)
    assert response.status_code == 403
    assert "无访问权限" in response.text
    assert "session" not in response.cookies
    assert not any(c.startswith("session=") for c in response.headers.get_list("set-cookie"))


def test_regular_whitelisted_user_can_login(client, fake_auth, settings):
    fake_auth.user = FeishuUser(uuid4().hex, "普通白名单用户", "")
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO users (subject, name, avatar_url, is_active, added_at) VALUES (?, ?, ?, 1, ?)",
            (fake_auth.user.open_id, "", "", "2026-10-29T00:00:00+00:00"),
        )
    assert login(client).status_code == 302
    response = client.get("/")
    assert response.status_code == 200
    assert "普通白名单用户" in response.text


def test_oauth_state_cookie_cannot_be_used_as_session(client):
    begin_login(client)
    client.cookies.set("session", client.cookies["oauth_state"])
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("params", [{}, {"code": ""}, {"code": "   "}])
def test_missing_or_blank_code_is_bad_request(client, params):
    response = client.get("/auth/callback", params=params)
    assert response.status_code == 400
    assert "session" not in response.cookies


def test_valid_session_can_visit_home(client):
    assert login(client).status_code == 302
    response = client.get("/")
    assert response.status_code == 200
    assert "测试用户" in response.text
    assert "录音列表" in response.text


def test_tampered_session_redirects_to_login(client):
    cookie = login(client).cookies["session"]
    client.cookies.clear()
    client.cookies.set("session", "tampered-" + cookie)
    response = client.get("/")
    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).netloc == "accounts.feishu.cn"


def test_expired_session_redirects_to_login(client, monkeypatch):
    login(client)
    original_time = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: original_time(self) + 86400)
    assert client.get("/").status_code == 302


def test_revoked_user_cannot_reuse_session(client, settings):
    login(client)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE open_id = ?", (settings.admin_open_id,))
    assert client.get("/").status_code == 403
    client.cookies.clear()
    assert login(client).status_code == 403


@pytest.mark.parametrize("state", [None, "wrong-state"])
def test_callback_rejects_missing_or_mismatched_state(client, state):
    begin_login(client)
    params = {"code": "valid-test-code"}
    if state is not None:
        params["state"] = state
    response = client.get("/auth/callback", params=params)
    assert response.status_code == 400
    assert "session" not in response.cookies


def test_callback_cannot_reuse_consumed_state(client):
    state = begin_login(client)
    params = {"code": "valid-test-code", "state": state}
    assert client.get("/auth/callback", params=params).status_code == 302
    response = client.get("/auth/callback", params=params)
    assert response.status_code == 400
    assert "session" not in response.cookies


def test_feishu_failure_returns_generic_error_without_session(client, fake_auth):
    fake_auth.error = True
    response = login(client)
    assert response.status_code == 502
    assert "登录失败" in response.text
    assert "session" not in response.cookies


def test_https_configuration_sets_secure_cookies(settings, fake_auth, store):
    secure_settings = replace(settings, feishu_redirect_uri="https://testserver/auth/callback")
    with TestClient(
        create_app(settings=secure_settings, feishu_auth=fake_auth, store=store),
        base_url="https://testserver", follow_redirects=False,
    ) as client:
        assert "Secure" in client.get("/").headers["set-cookie"]
        response = login(client)
        assert "Secure" in next(c for c in response.headers.get_list("set-cookie") if c.startswith("session="))
        assert client.get("/").status_code == 200


def test_user_name_is_html_escaped(client, fake_auth):
    fake_auth.user = replace(fake_auth.user, name="<script>alert(1)</script>")
    login(client)
    response = client.get("/")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
