"""Offline model lifecycle, secret boundaries, and the real tagging call path."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
from html import unescape
import json
import logging
import re
import secrets
import sqlite3

from cryptography.fernet import Fernet
import httpx
import pytest

from app import models, tag, tag_maintenance
from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import run_sync
from test_admin import replace_session
from test_login import login
from fakes import FakeMinutesSource


@pytest.fixture
def registry(client, store, tmp_path, monkeypatch):
    key = tmp_path / 'secret.key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    monkeypatch.setattr(models, 'SECRET_KEY_PATH', key)
    monkeypatch.setattr(models, 'CLAUDE_ENV_PATH', tmp_path / 'claude.env')
    assert login(client).status_code == 302
    return models.ModelRegistry(store)


def token(client):
    page = client.get('/models')
    assert page.status_code == 200
    return unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1])


def post(client, path='/models', **fields):
    return client.post(path, data={'csrf_token': token(client), **fields}, headers={'Accept': 'application/json'})


def values(**overrides):
    return dict(name='DeepSeek Flash', provider='deepseek', base_url='https://models.invalid/anthropic',
                model_id='selected-model', api_key='synthetic-private-' + secrets.token_hex(16), **overrides)


def add(registry, name='DeepSeek Flash', **overrides):
    config = values()
    config.update(name=name, **overrides)
    return registry.add(**config)


def test_encryption_roundtrip_and_no_plaintext_in_db_or_projection(registry, store):
    config = values()
    ident = registry.add(**config)
    with store._connect() as conn:
        row = dict(conn.execute('SELECT * FROM models WHERE id = ?', (ident,)).fetchone())
    key = config['api_key']
    encrypted = row['api_key_enc']
    assert encrypted != key
    assert not encrypted.startswith(key) and not encrypted.endswith(key)
    assert models.decrypt_key(encrypted) == key
    assert models.encrypt_key(key) != encrypted  # randomized IV
    assert row['key_last4'] == key[-4:]
    assert key.encode() not in store.db_path.read_bytes()
    for sidecar in store.db_path.parent.glob(store.db_path.name + '-*'):
        assert key.encode() not in sidecar.read_bytes()
    assert 'api_key_enc' not in asdict(registry.list()[0])
    assert key not in repr(registry.credentials(ident))


def test_page_and_api_only_expose_last4_and_never_ciphertext(registry, client, store):
    config = values()
    ident = registry.add(**config)
    with store._connect() as conn:
        ciphertext = conn.execute('SELECT api_key_enc FROM models WHERE id = ?', (ident,)).fetchone()[0]
    for path in ('/models', '/models/data'):
        response = client.get(path)
        assert response.status_code == 200
        assert config['api_key'] not in response.text and ciphertext not in response.text
        assert 'api_key_enc' not in response.text
        assert config['api_key'][-4:] in response.text
        assert response.headers['cache-control'] == 'no-store'
    assert '••••' + config['api_key'][-4:] in client.get('/models').text
    assert 'type="password"' in client.get('/models').text


def test_full_create_test_default_tagging_flow(registry, client, store):
    config = values()
    response = post(client, **config)
    assert response.status_code == 200 and response.json()['ok']
    ident = response.json()['id']
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url == 'https://models.invalid/anthropic/v1/messages'
        assert request.headers['x-api-key'] == config['api_key']
        assert request.headers['anthropic-version'] == '2023-06-01'
        assert body['model'] == config['model_id']
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"tags":["述职"]}'}]})
    transport = httpx.MockTransport(handler)
    client.app.state.model_transport = transport
    assert post(client, f'/models/{ident}/test').json()['ok']
    assert seen[0]['max_tokens'] == 8
    assert post(client, f'/models/{ident}/default').json()['ok']
    store.upsert_recording(RecordingMeta('model-flow', '例会', datetime(2026, 10, 30, tzinfo=SHANGHAI), 1),
        RecordingDetail('model-flow', '总结', '转写', True, True), tag.now())
    result = tag.run_tags(store, transport=transport)
    assert result['succeeded'] == 1 and result['failed'] == 0
    assert seen[1]['max_tokens'] == 256
    assert '严格JSON' in seen[1]['system'] and json.loads(seen[1]['messages'][0]['content'])['标题'] == '例会'
    with store._connect() as conn:
        assert conn.execute('SELECT model FROM recording_tags WHERE recording_id = ?', ('model-flow',)).fetchone()[0] == config['model_id']


@pytest.mark.parametrize('status', [301, 400, 401, 403, 429, 500])
def test_test_errors_never_echo_provider_body_headers_or_key(registry, client, caplog, status):
    config = values()
    ident = registry.add(**config)
    client.app.state.model_transport = httpx.MockTransport(lambda _: httpx.Response(status,
        text=config['api_key'], headers={'x-provider-secret': config['api_key']}))
    with caplog.at_level(logging.DEBUG):
        response = post(client, f'/models/{ident}/test')
    assert response.status_code == 502
    assert response.json() == {'ok': False, 'error': f'测试失败：HTTP {status}'}
    assert config['api_key'] not in response.text + caplog.text


@pytest.mark.parametrize('failure', ['timeout', 'connect', 'malformed', 'wrong_protocol', 'unexpected'])
def test_safe_transport_and_protocol_failures(registry, client, caplog, failure):
    config = values()
    ident = registry.add(**config)
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout(config['api_key'], request=request)
        if failure == 'connect':
            raise httpx.ConnectError(config['api_key'], request=request)
        if failure == 'unexpected':
            raise RuntimeError(config['api_key'])
        return (httpx.Response(200, text=config['api_key']) if failure == 'malformed' else
                httpx.Response(200, json={'choices': [], 'message': config['api_key']}))
    client.app.state.model_transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.DEBUG):
        response = post(client, f'/models/{ident}/test')
    assert response.status_code == 502 and not response.json()['ok']
    assert config['api_key'] not in response.text + caplog.text


def test_switch_default_delete_and_disabled_default_guard(registry, client):
    a, b = add(registry), add(registry, 'Second')
    assert post(client, f'/models/{a}/default').status_code == 200
    assert post(client, f'/models/{a}/delete').status_code == 409
    assert post(client, f'/models/{a}/toggle').status_code == 409
    assert post(client, f'/models/{b}/toggle').status_code == 200
    assert post(client, f'/models/{b}/default').status_code == 409
    assert [m.id for m in registry.list() if m.is_default] == [a]
    assert post(client, f'/models/{b}/toggle').status_code == 200
    assert post(client, f'/models/{b}/default').status_code == 200
    assert [m.id for m in registry.list() if m.is_default] == [b]
    assert post(client, f'/models/{a}/delete').status_code == 200
    assert [m.id for m in registry.list()] == [b]


def test_concurrent_default_changes_and_db_unique_constraint(registry, store):
    ids = [add(registry, str(i)) for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda ident: registry.mutate(ident, 'default'), ids * 3))
    assert sum(m.is_default for m in registry.list()) == 1
    other = next(m.id for m in registry.list() if not m.is_default)
    with pytest.raises(sqlite3.IntegrityError), store._connect() as conn:
        conn.execute('UPDATE models SET is_default = 1 WHERE id = ?', (other,))
    assert sum(m.is_default for m in registry.list()) == 1


@pytest.mark.parametrize('path', ['/models/999/test', '/models/999/default', '/models/999/delete', '/models/999/toggle'])
def test_unknown_model(registry, client, path):
    assert post(client, path).status_code == 404


@pytest.mark.parametrize('overrides', [
    {'name': ''}, {'provider': 'unknown'}, {'model_id': ''}, {'base_url': 'missing-scheme'},
    {'base_url': 'https://user:pass@models.invalid'}, {'base_url': 'https://models.invalid?key=bad'},
    {'base_url': 'https://models.invalid#bad'}, {'base_url': 'ftp://models.invalid'},
    {'api_key': ''}, {'api_key': '1234'}, {'api_key': 'contains space'}, {'api_key': '非ASCII密钥测试'},
    {'name': 'a\nb'}, {'model_id': 'a\tb'}, {'name': 'x'*101}, {'api_key':'x'*4097},
])
def test_invalid_create_never_reflects_key_or_writes(registry, client, overrides):
    config = values(); config.update(overrides)
    response = post(client, **config)
    assert response.status_code == 400
    if config['api_key']:
        assert config['api_key'] not in response.text
    assert registry.list() == []


def test_duplicate_name_and_failure_form_drops_secret(registry, client):
    config = values(); registry.add(**config)
    response = post(client, **config)
    assert response.status_code == 409 and config['api_key'] not in response.text
    config['name'] = ''
    response = client.post('/models', data={'csrf_token': token(client), **config})
    assert response.status_code == 400 and config['api_key'] not in response.text
    assert len(registry.list()) == 1


@pytest.mark.parametrize('path', ['/models', '/models/data'])
def test_admin_read_boundary(registry, client, store, settings, path):
    store.add_user(FeishuUser('ordinary-model-user', '普通用户', ''))
    replace_session(client, replace(settings, admin_open_id='ordinary-model-user'))
    assert client.get(path).status_code == 403
    client.cookies.clear()
    assert client.get(path).status_code == 302


@pytest.mark.parametrize('path', ['/models', '/models/1/test', '/models/1/default', '/models/1/delete', '/models/1/toggle'])
def test_write_auth_csrf_and_inactive_admin(registry, client, store, settings, path):
    csrf = token(client)
    assert client.post(path, data={}).status_code == 403
    assert client.post(path, data={'csrf_token': 'wrong'}).status_code == 403
    assert client.post(path, json={'csrf_token': csrf, **values()}).status_code == 403
    store.add_user(FeishuUser('ordinary-model-user', '普通用户', ''))
    replace_session(client, replace(settings, admin_open_id='ordinary-model-user'))
    assert client.post(path, data={'csrf_token': csrf}).status_code == 403
    replace_session(client, settings)
    with store._connect() as conn:
        conn.execute('UPDATE users SET is_active = 0')
    assert client.post(path, data={'csrf_token': csrf}).status_code == 403
    assert registry.list() == []


def test_csrf_isolation_binding_and_duplicate_rejection(registry, client, settings):
    csrf = token(client)
    for path in ['/admin', '/hotwords', '/settings', '/assistant']:
        assert client.get(path).status_code == 200
    assert client.post('/models', data={'csrf_token': csrf, **values()}).status_code == 303
    response = client.post('/models/1/delete', content='csrf_token='+csrf+'&csrf_token='+csrf,
                          headers={'Content-Type':'application/x-www-form-urlencoded'})
    assert response.status_code == 403
    client.cookies.clear(); login(client)
    assert client.post('/models/1/delete', data={'csrf_token': csrf}).status_code == 403


@pytest.mark.parametrize('contents,expected', [
    ('ANTHROPIC_MODEL=deepseek-flash\nANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n',
     {'model':'deepseek-flash','endpoint':'api.deepseek.com/anthropic'}),
    ('ANTHROPIC_MODEL="deepseek-flash"\nANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"\n',
     {'model':'deepseek-flash','endpoint':'api.deepseek.com/anthropic'}),
    ('ANTHROPIC_MODEL=${ANTHROPIC_AUTH_TOKEN}\nANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n',
     None),
    ('ANTHROPIC_MODEL=x\nANTHROPIC_BASE_URL=https://user:pass@models.invalid\n', None),
    ('ANTHROPIC_MODEL=x\nANTHROPIC_BASE_URL=https://models.invalid?token=secret\n', None),
    ('', None),
])
def test_claude_allowlist_no_interpolation_or_auth_token(registry, client, contents, expected):
    secret = 'synthetic-auth-' + secrets.token_hex(16)
    models.CLAUDE_ENV_PATH.write_text(contents + 'ANTHROPIC_AUTH_TOKEN=' + secret + '\n')
    assert models.claude_configuration() == expected
    for path in ('/models', '/models/data'):
        response = client.get(path)
        assert secret not in response.text
    if expected is None:
        assert '未配置' in client.get('/models').text


def test_claude_missing_file_does_not_read_process_env(registry, client, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_MODEL', 'not-claude-file')
    assert models.claude_configuration() is None
    assert '未配置' in client.get('/models').text
    assert 'not-claude-file' not in client.get('/models').text


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'wrong'])
def test_secret_failure_does_not_overwrite_key_and_tagging_degrades(registry, client, store, failure, monkeypatch, caplog):
    ident = add(registry)
    registry.mutate(ident, 'default')
    original = models.SECRET_KEY_PATH.read_bytes()
    if failure == 'missing': models.SECRET_KEY_PATH.unlink()
    elif failure == 'invalid': models.SECRET_KEY_PATH.write_bytes(b'invalid')
    else: models.SECRET_KEY_PATH.write_bytes(Fernet.generate_key())
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'synthetic-fallback-key')
    result = tag.run_tags(store)
    assert result['error'] == 'tag_batch_failed' and result['attempted'] == 0
    assert post(client, f'/models/{ident}/test').status_code == 503
    assert len(registry.list()) == 1
    if failure != 'missing': assert models.SECRET_KEY_PATH.read_bytes() != original
    assert 'synthetic-fallback-key' not in caplog.text


def test_env_fallback_without_default_and_no_configuration_skip(registry, store, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'synthetic-env-key')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'env-model')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://env.invalid/v1')
    resolved = models.resolve_model(store)
    assert (resolved.auth_token,resolved.model,resolved.base_url) == ('synthetic-env-key','env-model','https://env.invalid/v1')
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN')
    assert tag.run_tags(store)['error'] is None
    assert tag.run_tags(store)['attempted'] == 0


def test_maintenance_uses_default_model(registry, store):
    ident = add(registry, model_id='maintenance-model', base_url='https://semantic.invalid/v1')
    registry.mutate(ident, 'default')
    with store._connect() as conn:
        for name in ['直播计划', '直播规划']:
            conn.execute("INSERT INTO tags (name, status, source, created_at, updated_at, usage_count) VALUES (?, 'active', 'model', ?, ?, 2)", (name, tag.now(), tag.now()))
    def handler(request):
        assert json.loads(request.content)['model'] == 'maintenance-model'
        assert str(request.url) == 'https://semantic.invalid/v1/messages'
        return httpx.Response(200, json={'content':[{'type':'text','text':'{"duplicate_of":null}'}]})
    # Deterministic candidates are independently covered by existing maintenance tests.
    settings = models.resolve_model(store)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert tag_maintenance.semantic_duplicate(client, settings, '直播计划', ['直播规划']) is None


def test_migration_repeat_preserves_cipher_and_default(registry, store):
    ident = add(registry)
    registry.mutate(ident, 'default')
    before = registry.credentials(ident)
    store.migrate(); store.migrate()
    assert registry.credentials(ident) == before and registry.list()[0].is_default


@pytest.mark.parametrize('base,expected', [
    ('https://models.invalid/anthropic', 'https://models.invalid/anthropic/v1/messages'),
    ('https://models.invalid/v1/', 'https://models.invalid/v1/messages'),
])
def test_messages_url(base, expected):
    assert models.messages_url(base) == expected


@pytest.mark.parametrize('configuration', ['none', 'broken_default'])
def test_sync_survives_absent_or_broken_model(registry, store, configuration):
    if configuration == 'broken_default':
        ident = add(registry)
        registry.mutate(ident, 'default')
        models.SECRET_KEY_PATH.unlink()
    record = RecordingMeta('degraded-sync', '仍然同步', datetime.now(SHANGHAI), 5)
    source = FakeMinutesSource([record], {
        record.recording_id: RecordingDetail(record.recording_id, '总结', '转写', True, True)})
    result = run_sync(source, store, backfill_days=1)
    assert result.status == 'success' and result.inserted == 1
    assert store.get_recording(record.recording_id).title == '仍然同步'
    with store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tag_runs').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM recording_tags').fetchone()[0] == 0


def test_run_maintenance_resolves_registry_default(registry, store, monkeypatch):
    ident = add(registry, model_id='default-semantic', base_url='https://semantic.invalid/anthropic')
    registry.mutate(ident, 'default')
    # Inject only candidate discovery; exercise configuration resolution, HTTP, and persistence.
    with store._connect() as conn:
        source = dict(conn.execute("SELECT * FROM tags WHERE name = '述职'").fetchone())
    monkeypatch.setattr(tag_maintenance, 'semantic_candidates', lambda _: [(source, ['周会'])])
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == 'https://semantic.invalid/anthropic/v1/messages'
        return httpx.Response(200, json={'content':[{'type':'text','text':'{"duplicate_of":null}'}]})
    result = tag_maintenance.run_maintenance(store, transport=httpx.MockTransport(handler))
    assert result['checked'] == 1 and result['failed'] == 0
    assert seen[0]['model'] == 'default-semantic'


def test_env_fallback_makes_tag_request(registry, store, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'synthetic-env-credential')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://env.invalid/v1')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'env-fallback-model')
    store.upsert_recording(RecordingMeta('env-tag', '例会', datetime.now(SHANGHAI), 1),
        RecordingDetail('env-tag', '总结', '转写', True, True), tag.now())
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == 'https://env.invalid/v1/messages'
        assert request.headers['x-api-key'] == 'synthetic-env-credential'
        return httpx.Response(200, json={'content':[{'type':'text','text':'{"tags":["述职"]}'}]})
    result = tag.run_tags(store, transport=httpx.MockTransport(handler))
    assert result['succeeded'] == 1 and seen[0]['model'] == 'env-fallback-model'
