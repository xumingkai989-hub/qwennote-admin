"""Regression coverage for union/open IDs, including ambiguous historical rows."""
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.feishu import FeishuUser, HttpFeishuAuth
from app.main import create_app
from app.session import SignedCookie, SESSION_MAX_AGE
from test_login import login


@pytest.mark.parametrize('column', ['subject', 'union_id', 'open_id'])
@pytest.mark.parametrize('identifier', ['on_login', 'ou_login'])
def test_either_provider_id_matches_any_whitelist_column(client, store, fake_auth, column, identifier):
    fake_auth.user = FeishuUser('ou_login', '许铭凯', '', union_id='on_login')
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('INSERT INTO users (subject, union_id, open_id, name, avatar_url, is_active, added_at) '
                     'VALUES (?, ?, ?, ?, ?, 1, ?)',
                     (identifier if column == 'subject' else 'legacy',
                      identifier if column == 'union_id' else None,
                      identifier if column == 'open_id' else None, '旧姓名', '', 'original'))
    assert login(client).status_code == 302
    assert client.get('/').status_code == 200
    user = store.get_user(identifier)
    assert user.name == '许铭凯'
    assert user.union_id == 'on_login'
    assert user.open_id == 'ou_login'
    assert user.added_at == 'original'
    store.remove_user(user.subject)
    assert client.get('/').status_code == 403
    client.cookies.clear()
    assert login(client).status_code == 403


@pytest.mark.parametrize('union_id,open_id', [('on_denied', 'ou_denied'), ('', 'ou_denied'), ('on_denied', '')])
def test_denied_page_shows_both_id_types(client, fake_auth, union_id, open_id):
    fake_auth.user = FeishuUser(open_id, '未授权用户', '', union_id=union_id)
    response = login(client)
    assert response.status_code == 403
    assert f'你的 union_id：<code>{union_id or "未返回"}</code>' in response.text
    assert f'你的 open_id：<code>{open_id or "未返回"}</code>' in response.text


def test_mixed_admin_ids_seed_subject_only_and_authorize_both(settings, fake_auth):
    app = create_app(settings=replace(settings, admin_open_id='on_a,ou_b'), feishu_auth=fake_auth)
    with TestClient(app, follow_redirects=False) as client:
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute('SELECT subject, union_id, open_id FROM users ORDER BY subject').fetchall() == [
                ('on_a', None, None), ('ou_b', None, None)]
        for union_id, open_id in [('on_a', 'ou_a'), ('on_b', 'ou_b')]:
            client.cookies.clear()
            fake_auth.user = FeishuUser(open_id, '管理员', '', union_id=union_id)
            assert login(client).status_code == 302
            assert client.get('/').status_code == 200
            assert client.get('/admin').status_code == 200
    with TestClient(app):
        assert len(app.state.store.list_users()) == 2


@pytest.mark.parametrize('legacy_id', ['on_old', 'ou_old'])
def test_legacy_users_migrate_idempotently_without_data_loss(settings, fake_auth, legacy_id):
    settings.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('CREATE TABLE users (open_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL, '
                     'avatar_url TEXT NOT NULL, is_active INTEGER NOT NULL, added_at TEXT NOT NULL)')
        conn.executemany('INSERT INTO users VALUES (?, ?, ?, ?, ?)', [
            (legacy_id, '旧姓名', 'avatar', 1, 'original'), ('disabled', '停用', '', 0, 'older')])
    app = create_app(settings=settings, feishu_auth=fake_auth)
    for _ in range(2):
        with TestClient(app):
            with sqlite3.connect(settings.db_path) as conn:
                assert conn.execute('SELECT subject, union_id, open_id, name, avatar_url, is_active, added_at '
                                    'FROM users WHERE subject = ?', (legacy_id,)).fetchone() == (
                                        legacy_id, None, legacy_id, '旧姓名', 'avatar', 1, 'original')
                assert conn.execute('SELECT subject, union_id, open_id, name, avatar_url, is_active, added_at '
                                    "FROM users WHERE subject = 'disabled'").fetchone() == (
                                        'disabled', None, 'disabled', '停用', '', 0, 'older')
    with TestClient(app, follow_redirects=False) as client:
        fake_auth.user = FeishuUser('ou_old', '旧姓名', 'avatar', union_id='on_old')
        assert login(client).status_code == 302
        assert client.get('/').status_code == 200


@pytest.mark.parametrize('union_id,open_id', [('on_provider', 'ou_provider'), ('', 'ou_provider'), ('on_provider', '')])
def test_exchange_code_keeps_both_ids_and_uses_union_subject(settings, union_id, open_id):
    def handle(request):
        if request.url.path.endswith('oauth/token'):
            return httpx.Response(200, json={'code': 0, 'access_token': 'test-token'})
        return httpx.Response(200, json={'code': 0, 'data': {
            'union_id': union_id, 'open_id': open_id, 'name': '姓名', 'avatar_url': ''}})
    identity = HttpFeishuAuth(settings, transport=httpx.MockTransport(handle)).exchange_code('test-code')
    assert identity.union_id == union_id
    assert identity.open_id == open_id
    assert identity.subject == (union_id or open_id)


def test_login_stores_whitelist_subject_for_session(client, store, fake_auth, settings):
    store.add_user(FeishuUser('ou_new', '姓名', '', union_id='on_new'))
    fake_auth.user = FeishuUser('ou_new', '姓名', '', union_id='on_new')
    assert login(client).status_code == 302
    signer = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE)
    assert signer.decode(client.cookies['session']) == 'on_new'
    assert client.get('/').status_code == 200


def test_duplicate_config_identities_revoke_together(settings, fake_auth):
    app = create_app(settings=replace(settings, admin_open_id='on_same,ou_same'), feishu_auth=fake_auth)
    with TestClient(app, follow_redirects=False) as client:
        fake_auth.user = FeishuUser('ou_same', '同一用户', '', union_id='on_same')
        assert login(client).status_code == 302
        app.state.store.remove_user('ou_same')
        assert all(not user.is_active for user in app.state.store.list_users())
        assert client.get('/').status_code == 403
        client.cookies.clear()
        assert login(client).status_code == 403


def test_active_match_is_allowed_even_when_another_match_is_disabled(settings, fake_auth):
    app = create_app(settings=replace(settings, admin_open_id='on_same,ou_same'), feishu_auth=fake_auth)
    with TestClient(app, follow_redirects=False) as client:
        app.state.store.remove_user('on_same')
        fake_auth.user = FeishuUser('ou_same', '同一用户', '', union_id='on_same')
        assert login(client).status_code == 302
        assert client.get('/').status_code == 200


def test_seed_by_alias_preserves_disabled_row_without_duplicate(client, store):
    store.add_user(FeishuUser('ou_alias', '保留姓名', '', union_id='on_alias'))
    store.remove_user('on_alias')
    before = store.list_users()
    store.seed_admin('ou_alias')
    assert store.list_users() == before


def test_denied_union_id_is_html_escaped(client, fake_auth):
    from markupsafe import escape
    value = 'on_<script>&"'
    fake_auth.user = FeishuUser('ou_denied', '姓名', '', union_id=value)
    response = login(client)
    assert response.status_code == 403
    assert value not in response.text
    assert escape(value) in response.text


def test_empty_ids_do_not_match_null_aliases(client, store):
    assert store.get_user('', '') is None
