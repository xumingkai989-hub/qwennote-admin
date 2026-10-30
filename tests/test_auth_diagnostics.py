import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from app.config import Settings
from app.feishu import FeishuUser
from app.main import create_app
from test_admin import add
from test_login import begin_login, login


def test_whitelist_miss_shows_identity_without_credentials(client, fake_auth, settings):
    fake_auth.user = FeishuUser('on_not_whitelisted', '未授权的张三', '')
    state = begin_login(client)
    state_cookie = client.cookies['oauth_state']
    response = client.get('/auth/callback', params={'code': 'valid-test-code', 'state': state})
    assert response.status_code == 403
    for text in ('你没有访问权限', '你的 open_id：', fake_auth.user.open_id,
                 fake_auth.user.name, '把上面这串交给管理员加入白名单即可'):
        assert text in response.text
    for value in ('valid-test-code', state, state_cookie, settings.session_secret,
                  settings.feishu_app_secret, '点此进入'):
        assert value not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'session' not in response.cookies
    assert 'oauth_state' not in client.cookies


def test_whitelist_miss_identity_is_html_escaped(client, fake_auth):
    fake_auth.user = FeishuUser('on_<bad&"id>', '<script>alert("姓名")</script>', '')
    response = login(client)
    assert response.status_code == 403
    for value in (fake_auth.user.open_id, fake_auth.user.name):
        assert value not in response.text
        assert escape(value) in response.text


@pytest.mark.parametrize('name', ['张三\nforged log', ''])
def test_whitelist_miss_logs_both_ids_and_safe_name(client, fake_auth, caplog, name):
    fake_auth.user = FeishuUser('ou_denied', name, '', union_id='on_denied')
    client.cookies.set('session', 'private-session-sentinel')
    with caplog.at_level(logging.WARNING):
        state = begin_login(client)
        response = client.get('/auth/callback', params={'code': 'valid-test-code', 'state': state},
                              headers={'X-Private': 'private-header-sentinel',
                                       'Authorization': 'Bearer private-token-sentinel'})
    assert response.status_code == 403
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        f'auth: whitelist miss union_id={fake_auth.user.union_id!r} '
        f'open_id={fake_auth.user.open_id!r} name={name!r}')
    assert '\n' not in warnings[0].getMessage()
    for value in ('valid-test-code', 'private-token-sentinel', 'private-header-sentinel',
                  'private-session-sentinel', state):
        assert value not in caplog.text


def test_whitelist_hit_does_not_render_id_or_warn(client, fake_auth, caplog):
    with caplog.at_level(logging.WARNING):
        response = login(client)
        assert response.status_code == 302
        assert fake_auth.user.open_id not in response.text
        home = client.get('/')
    assert home.status_code == 200
    assert fake_auth.user.open_id not in home.text
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize('configured', ['on_a,on_b', ' on_a, ,on_b,on_a, '])
def test_multiple_admins_are_seeded_can_login_and_manage(settings, fake_auth, monkeypatch, configured):
    monkeypatch.setenv('ADMIN_OPEN_ID', configured)
    settings = Settings.from_env()
    app = create_app(settings=settings, feishu_auth=fake_auth)
    with TestClient(app, follow_redirects=False) as client:
        assert {u.subject for u in app.state.store.list_users()} == {'on_a', 'on_b'}
        assert all(u.is_active for u in app.state.store.list_users())
        for open_id in ('on_a', 'on_b'):
            client.cookies.clear()
            fake_auth.user = FeishuUser(open_id, '管理员姓名', '')
            assert login(client).status_code == 302
            assert client.get('/').status_code == 200
            assert client.get('/admin').status_code == 200
            assert add(client, open_id='user_added_by_' + open_id).status_code == 303


def test_multiple_admins_restart_preserves_disabled_user(settings, fake_auth):
    settings = replace(settings, admin_open_id='on_a,on_b')
    app = create_app(settings=settings, feishu_auth=fake_auth)
    with TestClient(app):
        app.state.store.remove_user('on_b')
        before = app.state.store.get_user('on_b')
        assert before is not None
    with TestClient(app, follow_redirects=False) as client:
        assert app.state.store.get_user('on_b') == before
        fake_auth.user = FeishuUser('on_b', '已停用管理员', '')
        response = login(client)
        assert response.status_code == 403
        assert 'on_b' in response.text
        assert client.get('/admin').status_code == 302


@pytest.mark.parametrize('value', [',', ' , , '])
def test_empty_admin_list_is_rejected(settings, monkeypatch, value):
    monkeypatch.setenv('ADMIN_OPEN_ID', value)
    with pytest.raises(ValueError, match='ADMIN_OPEN_ID'):
        Settings.from_env()
