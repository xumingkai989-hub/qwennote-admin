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


def directory_employee(index=0):
    return {'base_info': {
        'employee_id': f'ou_contact_{index}',
        'name': {'name': {'default_value': f'联系人{index}'}},
        'avatar': {'avatar_240': 'https://example.com/avatar.png'},
        'departments': [{'name': {'default_value': '研发部'}}],
        'email': f'user{index}@example.com',
        'mobile': 'private-mobile-never-render', 'gender': 1,
    }}


def test_contact_search_uses_app_token_open_id_and_caps_results(settings):
    import json
    def handle(request):
        assert request.url.host == 'open.feishu.cn'
        assert request.method == 'POST'
        body = json.loads(request.content)
        if request.url.path.endswith('/tenant_access_token/internal'):
            assert body == {'app_id': settings.feishu_app_id, 'app_secret': settings.feishu_app_secret}
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock-tenant-token'})
        assert request.url.path == '/open-apis/directory/v1/employees/search'
        assert request.headers['authorization'] == 'Bearer mock-tenant-token'
        assert request.url.params['employee_id_type'] == 'open_id'
        assert body['query'] == '联系人'
        assert body['page_request'] == {'page_size': 20}
        assert set(body['required_fields']) == {
            'base_info.employee_id', 'base_info.name', 'base_info.avatar',
            'base_info.departments', 'base_info.email',
        }
        return httpx.Response(200, json={'code': 0, 'data': {
            'employees': [directory_employee(i) for i in range(25)],
            'page_response': {'has_more': True, 'page_token': 'do-not-follow'},
        }})
    auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
    result = auth.search_users(' 联系人 ')
    assert len(result) == 20
    assert result[0].open_id == 'ou_contact_0'
    assert result[0].name == '联系人0'
    assert result[0].department == '研发部'
    assert result[0].email == 'user0@example.com'
    assert result[0].avatar_url == 'https://example.com/avatar.png'
    assert not hasattr(result[0], 'mobile')


@pytest.mark.parametrize('missing', ['feishu_app_id', 'feishu_app_secret'])
def test_contact_search_missing_config_raises_explicit_error(settings, missing):
    from dataclasses import replace
    from app.feishu import FeishuAuthError
    auth = HttpFeishuAuth(replace(settings, **{missing: ''}))
    with pytest.raises(FeishuAuthError, match='通讯录搜索未配置'):
        auth.search_users('test')


@pytest.mark.parametrize('stage', ['token', 'search'])
@pytest.mark.parametrize('failure', ['timeout', 'http', 'invalid-json', 'api-error', 'missing-data', 'bad-shape'])
def test_contact_search_errors_are_readable_and_safe(settings, stage, failure):
    from app.feishu import FeishuAuthError
    def handle(request):
        token = request.url.path.endswith('/tenant_access_token/internal')
        if token == (stage == 'token'):
            if failure == 'timeout': raise httpx.ReadTimeout('provider-private-body', request=request)
            if failure == 'http': return httpx.Response(500, text='provider-private-body')
            if failure == 'invalid-json': return httpx.Response(200, text='provider-private-body')
            if failure == 'api-error': return httpx.Response(200, json={'code': 99991672, 'msg': 'provider-private-body'})
            if failure == 'missing-data': return httpx.Response(200, json={'code': 0})
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 12, 'data': {'employees': [None]}})
        return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock-token'})
    auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
    with pytest.raises(FeishuAuthError, match='通讯录搜索失败') as caught:
        auth.search_users('test')
    assert 'provider-private-body' not in str(caught.value)


def test_contact_search_empty_results_and_optional_fields(settings):
    from app.feishu import FeishuUser
    employees = [{'base_info': {'employee_id': 'ou_minimal'}}]
    def handle(request):
        if request.url.path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock-token'})
        return httpx.Response(200, json={'code': 0, 'data': {'employees': employees}})
    auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
    assert auth.search_users('test') == [FeishuUser('ou_minimal', '', '')]
    employees.clear()
    assert auth.search_users('test') == []
