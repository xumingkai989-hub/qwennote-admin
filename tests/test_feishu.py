import httpx
import pytest
from fastapi.testclient import TestClient

from app.feishu import HttpFeishuAuth
from app.main import create_app
from test_login import login


def test_real_adapter_can_login_with_mock_http_only(settings):
    def handle(request):
        assert request.url.scheme == "https"
        assert request.url.host == "open.feishu.cn"
        if request.url.path == "/open-apis/authen/v2/oauth/token":
            import json

            assert request.method == "POST"
            assert json.loads(request.content) == {
                "grant_type": "authorization_code",
                "client_id": settings.feishu_app_id,
                "client_secret": settings.feishu_app_secret,
                "redirect_uri": settings.feishu_redirect_uri,
                "code": "valid-test-code",
            }
            return httpx.Response(200, json={"code": 0, "access_token": "mock-access-token"})
        assert request.url.path == "/open-apis/authen/v1/user_info"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer mock-access-token"
        return httpx.Response(200, json={
            "code": 0,
            "data": {"open_id": settings.admin_open_id, "name": "模拟飞书用户", "avatar_url": ""},
        })

    auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings=settings, feishu_auth=auth), follow_redirects=False) as client:
        assert login(client).status_code == 302
        assert "模拟飞书用户" in client.get("/").text


@pytest.mark.parametrize("failure", [
    "timeout", "http-error", "token-error", "missing-token", "user-error", "missing-open-id", "invalid-json",
])
def test_adapter_failures_do_not_issue_session_or_leak_details(settings, failure):
    sensitive_detail = "provider-internal-detail"

    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(sensitive_detail, request=request)
        if failure == "http-error":
            return httpx.Response(500, text=sensitive_detail)
        if failure == "invalid-json":
            return httpx.Response(200, text=sensitive_detail)
        if request.url.path.endswith("oauth/token"):
            if failure == "token-error":
                return httpx.Response(200, json={"code": 1, "error_description": sensitive_detail})
            if failure == "missing-token":
                return httpx.Response(200, json={"code": 0})
            return httpx.Response(200, json={"code": 0, "access_token": "mock-access-token"})
        if failure == "user-error":
            return httpx.Response(200, json={"code": 1, "msg": sensitive_detail})
        return httpx.Response(200, json={"code": 0, "data": {"name": sensitive_detail}})

    auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
    with TestClient(create_app(settings=settings, feishu_auth=auth), follow_redirects=False) as client:
        response = login(client)
        assert response.status_code == 502
        assert sensitive_detail not in response.text
        assert "session" not in response.cookies
