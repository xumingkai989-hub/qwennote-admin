import re
import sqlite3
from dataclasses import replace
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.feishu import FeishuAuthError, FeishuUser, HttpFeishuAuth
from app.main import create_app
from app.session import SESSION_MAX_AGE, SignedCookie
from test_login import login


def users(settings):
    with sqlite3.connect(settings.db_path) as conn:
        return conn.execute('SELECT * FROM users ORDER BY open_id').fetchall()


def csrf(client):
    response = client.get('/admin')
    assert response.status_code == 200
    return unescape(re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1])


def replace_session(client, settings):
    signer = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE)
    old_session = client.cookies['session']
    _, issued_at = signer.serializer.make_signer().unsign(old_session, return_timestamp=True)
    # Derive a distinct timestamp from the cookie, never from wall-clock time:
    # now - 1 can equal the old timestamp if signing crosses a second boundary.
    with patch.object(TimestampSigner, 'get_timestamp', return_value=int(issued_at.timestamp()) - 1):
        new_session = signer.encode(settings.admin_open_id)
    assert new_session != old_session
    assert signer.decode(new_session) == settings.admin_open_id
    client.cookies.delete('session')
    client.cookies.set('session', new_session)


def add(client, open_id='ou_new_contact', **extra):
    return client.post('/admin/users', data={
        'csrf_token': csrf(client), 'open_id': open_id, 'name': '新联系人',
        'avatar_url': 'https://example.com/avatar.png', **extra,
    })


def test_anonymous_admin_uses_existing_login_redirect(client):
    response = client.get('/admin')
    assert response.status_code == 302
    assert urlsplit(response.headers['location']).netloc == 'accounts.feishu.cn'
    assert parse_qs(urlsplit(response.headers['location']).query)['state']


def test_admin_lists_all_users_with_truncated_id_and_status(client, settings):
    login(client)
    assert add(client).status_code == 303
    response = client.get('/admin')
    assert response.status_code == 200
    for text in ('白名单', '测试用户', '新联系人', '加入时间', '状态', '已启用', 'ou_new_c…'):
        assert text in response.text
    assert 'title="ou_new_contact"' in response.text
    assert users(settings)[0][-1] in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('method,path', [('get', '/admin'), ('get', '/admin?q=张'),
    ('post', '/admin/users'), ('post', '/admin/users/anything/remove')])
def test_regular_user_named_admin_is_not_administrator(client, fake_auth, method, path):
    login(client)
    add(client, name='管理员')
    client.cookies.clear()
    fake_auth.user = FeishuUser('ou_new_contact', '管理员', '')
    login(client)
    response = getattr(client, method)(path)
    assert response.status_code == 403
    assert '无访问权限' in response.text


def test_search_is_transient_and_exposes_only_required_fields(client, fake_auth, settings):
    login(client)
    before = users(settings)
    fake_auth.search_results = [FeishuUser('ou_search_result', '张三', '', department='研发部', email='zhang@example.com')]
    response = client.get('/admin', params={'q': '张三'})
    assert response.status_code == 200
    for text in ('张三', '研发部', 'zhang@example.com', '添加'):
        assert text in response.text
    assert users(settings) == before


def test_search_results_and_whitelist_names_are_escaped(client, fake_auth):
    login(client)
    evil = '<script>alert("x")</script>'
    fake_auth.search_results = [FeishuUser('ou_evil', evil, '" onerror="alert(1)', department=evil, email=evil)]
    response = client.get('/admin', params={'q': evil})
    assert evil not in response.text
    assert '&lt;script&gt;' in response.text
    assert 'value="&#34; onerror=&#34;alert(1)"' in response.text
    assert add(client, name=evil).status_code == 303
    assert evil not in client.get('/admin').text
    assert '&lt;script&gt;' in client.get('/admin').text


def test_add_is_idempotent_and_new_user_can_complete_oauth(client, fake_auth, settings):
    login(client)
    assert add(client).status_code == 303
    added_at = next(r[-1] for r in users(settings) if r[0] == 'ou_new_contact')
    assert add(client).status_code == 303
    assert len(users(settings)) == 2
    assert next(r[-1] for r in users(settings) if r[0] == 'ou_new_contact') == added_at
    client.cookies.clear()
    fake_auth.user = FeishuUser('ou_new_contact', '新联系人', '')
    assert login(client).status_code == 302
    assert client.get('/').status_code == 200


def test_remove_revokes_existing_session_and_readd_restores_access(client, fake_auth, settings):
    login(client)
    add(client)
    admin_cookies = dict(client.cookies)
    client.cookies.clear()
    fake_auth.user = FeishuUser('ou_new_contact', '新联系人', '')
    login(client)
    member_cookies = dict(client.cookies)
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    response = client.post('/admin/users/ou_new_contact/remove', data={'csrf_token': csrf(client)})
    assert response.status_code == 303
    assert '已停用' in client.get('/admin').text
    client.cookies.clear()
    client.cookies.update(member_cookies)
    for path in ('/', '/search?q=test', '/recordings/nonexistent', '/recordings/nonexistent.md', '/admin'):
        assert client.get(path).status_code == 403
    assert client.post('/sync').status_code == 403
    client.cookies.clear()
    assert login(client).status_code == 403
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    assert add(client).status_code == 303
    assert len(users(settings)) == 2
    client.cookies.clear()
    assert login(client).status_code == 302


@pytest.mark.parametrize('path', ['/admin/users', '/admin/users/victim/remove'])
@pytest.mark.parametrize('kind', ['missing', 'tampered', 'cookie-missing', 'unsigned', 'session-as-token', 'other-session'])
def test_mutations_require_signed_session_bound_csrf(client, settings, path, kind):
    login(client)
    valid = csrf(client)
    value = valid
    if kind == 'missing': value = ''
    if kind == 'tampered': value = 'bad-' + valid
    if kind == 'cookie-missing': client.cookies.delete('admin_csrf')
    if kind == 'unsigned':
        client.cookies.delete('admin_csrf')
        client.cookies.set('admin_csrf', 'unsigned')
        value = 'unsigned'
    if kind == 'session-as-token':
        value = client.cookies['session']
        client.cookies.delete('admin_csrf')
        client.cookies.set('admin_csrf', value)
    if kind == 'other-session':
        # Independently signed session with the same identity, as after relogin.
        replace_session(client, settings)
    before = users(settings)
    response = client.post(path, data={'csrf_token': value, 'open_id': 'victim', 'name': '测试', 'avatar_url': ''})
    assert response.status_code == 403
    assert users(settings) == before


@pytest.mark.parametrize('path', ['/admin/users', '/admin/users/victim/remove'])
@pytest.mark.parametrize('elapsed', [0, 1])
def test_other_session_csrf_rejected_across_second_boundary(client, settings, monkeypatch, path, elapsed):
    now = 1_800_000_000
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: now)
    assert login(client).status_code == 302
    assert add(client, open_id='victim').status_code == 303
    token = csrf(client)
    now += elapsed
    replace_session(client, settings)
    # A valid replacement session must reach CSRF validation, not fail auth.
    assert client.get('/').status_code == 200
    before = users(settings)
    response = client.post(path, data={'csrf_token': token, 'open_id': 'victim', 'name': '测试'})
    assert response.status_code == 403
    assert 'CSRF 校验失败' in response.text
    assert users(settings) == before


@pytest.mark.parametrize('path', ['/admin/users', '/admin/users/victim/remove'])
def test_get_cannot_mutate_whitelist(client, settings, path):
    login(client)
    before = users(settings)
    assert client.get(path).status_code == 405
    assert users(settings) == before


def test_real_search_without_credentials_is_explicit_on_page(settings, fake_auth, store):
    auth = HttpFeishuAuth(replace(settings, feishu_app_id='', feishu_app_secret=''))
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, store=store), follow_redirects=False) as client:
        login(client)
        client.app.state.feishu_auth = auth
        response = client.get('/admin?q=test')
        assert response.status_code == 200
        assert '通讯录搜索未配置' in response.text
        assert '未找到联系人' not in response.text


def test_search_failure_is_readable_without_provider_details(client, fake_auth):
    login(client)
    fake_auth.search_error = FeishuAuthError('provider-private-body')
    response = client.get('/admin?q=test')
    assert response.status_code == 200
    assert '通讯录搜索失败' in response.text
    assert 'provider-private-body' not in response.text
    assert '未找到联系人' not in response.text


@pytest.mark.parametrize('data', [
    {'open_id': ''}, {'open_id': '   '}, {'open_id': 'bad/id'}, {'name': 'a' * 1001},
])
def test_invalid_add_does_not_write(client, settings, data):
    login(client)
    before = users(settings)
    assert add(client, **data).status_code == 400
    assert users(settings) == before


def test_expired_csrf_is_rejected_without_expiring_session(client, settings):
    from itsdangerous import TimestampSigner
    from unittest.mock import patch
    from app.session import SESSION_MAX_AGE, SignedCookie
    login(client)
    signer = SignedCookie(settings.session_secret, salt='admin-csrf', max_age=SESSION_MAX_AGE)
    original = TimestampSigner.get_timestamp
    with patch.object(TimestampSigner, 'get_timestamp', lambda self: original(self) - SESSION_MAX_AGE - 1):
        import hashlib
        binding = hashlib.sha256(client.cookies['session'].encode()).hexdigest()
        expired = signer.encode(binding + ':nonce')
    client.cookies.set('admin_csrf', expired, path='/admin')
    before = users(settings)
    response = client.post('/admin/users', data={'csrf_token': expired, 'open_id': 'ou_expired'})
    assert response.status_code == 403
    assert users(settings) == before
    assert client.get('/').status_code == 200


def test_duplicate_csrf_fields_are_rejected(client, settings):
    from urllib.parse import urlencode
    login(client)
    token = csrf(client)
    before = users(settings)
    response = client.post('/admin/users', content=urlencode([
        ('csrf_token', token), ('csrf_token', token), ('open_id', 'ou_duplicate'),
    ]), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 403
    assert users(settings) == before


def test_admin_csrf_cookie_has_security_attributes_on_https(settings, fake_auth, store):
    secure_settings = replace(settings, feishu_redirect_uri='https://testserver/auth/callback')
    with TestClient(create_app(settings=secure_settings, feishu_auth=fake_auth, store=store),
                    base_url='https://testserver', follow_redirects=False) as client:
        login(client)
        response = client.get('/admin')
        cookie = next(c for c in response.headers.get_list('set-cookie') if c.startswith('admin_csrf='))
        for attribute in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/admin', 'Max-Age=28800'):
            assert attribute in cookie
        assert add(client).status_code == 303


def test_empty_query_does_not_search_and_real_no_match_is_distinct(client, fake_auth):
    login(client)
    fake_auth.search_error = FeishuAuthError('must not search empty query')
    assert '通讯录搜索失败' not in client.get('/admin?q=%20').text
    fake_auth.search_error = None
    assert '未找到联系人' in client.get('/admin?q=nonexistent').text


def test_search_sensitive_provider_fields_never_reach_html(settings, fake_auth, store):
    import httpx
    from test_feishu import directory_employee
    def handle(request):
        if request.url.path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock-private-token'})
        return httpx.Response(200, json={'code': 0, 'data': {'employees': [directory_employee()]}})
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, store=store), follow_redirects=False) as client:
        login(client)
        client.app.state.feishu_auth = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle))
        before = users(settings)
        response = client.get('/admin?q=test')
        assert '联系人0' in response.text
        for value in ('private-mobile-never-render', 'mock-private-token', settings.feishu_app_secret):
            assert value not in response.text
        assert users(settings) == before



def test_csrf_form_token_does_not_contain_session_credential(client, settings):
    from app.session import SESSION_MAX_AGE, SignedCookie
    login(client)
    token = csrf(client)
    payload = SignedCookie(settings.session_secret, salt='admin-csrf', max_age=SESSION_MAX_AGE).decode(token)
    assert client.cookies['session'] not in payload
    assert settings.admin_open_id not in payload
