import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime

import pytest

from app.minutes import RecordingDetail, RecordingMeta


def cli(settings, *args):
    return subprocess.run(
        [sys.executable, '-m', 'app.apikeys', *args],
        env={**os.environ, 'DB_PATH': str(settings.db_path)},
        capture_output=True, text=True, timeout=10,
    )


def rows(settings, table):
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]


@pytest.fixture
def api(client, store, settings):
    result = cli(settings, 'create', '--name', 'codex')
    assert result.returncode == 0, result.stderr
    key = result.stdout.strip()
    assert re.fullmatch(r'qn_[A-Za-z0-9]{32}', key)
    client.headers['Authorization'] = f'Bearer {key}'
    return client, key


@pytest.fixture
def add_recording(store):
    def add(recording_id, *, title='看板例会', summary='看板总结', transcript='转写原文',
            created='2026-10-29T10:01:00+08:00', duration=49):
        store.upsert_recording(
            RecordingMeta(recording_id, title, datetime.fromisoformat(created), duration),
            RecordingDetail(recording_id, summary, transcript, bool(transcript), bool(summary)),
            '2026-10-29T12:00:00+08:00',
        )
    return add


CARD = {
    'recording_id': 'one', 'title': '看板例会', 'created_at': '2026-10-29T10:01:00+08:00',
    'duration_min': 49, 'status': 'complete', 'has_transcript': True, 'has_summary': True,
}


@pytest.mark.parametrize('path,expected', [
    ('/api/v1/recordings', {'total': 1, 'limit': 20, 'offset': 0, 'items': [CARD]}),
    ('/api/v1/recordings/one', {**CARD, 'summary_md': '看板总结', 'transcript_md': '转写原文'}),
    ('/api/v1/daily/2026-10-29?format=json', {'date': '2026-10-29', 'count': 1, 'recordings': [CARD]}),
    ('/api/v1/search?q=看板', {'q': '看板', 'total': 1, 'items': [{
        'recording_id': 'one', 'title': '看板例会', 'created_at': CARD['created_at'],
        'matched': ['title', 'summary'],
    }]}),
])
def test_four_endpoint_contracts_and_audit(api, add_recording, settings, path, expected):
    client, _ = api
    add_recording('one')
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers['cache-control'] == 'no-store'
    audit, = rows(settings, 'api_audit')
    assert audit['key_id'] == rows(settings, 'api_keys')[0]['id']
    assert audit['path'] == path.split('?')[0] and audit['status_code'] == 200
    assert datetime.fromisoformat(audit['at']).tzinfo is not None
    assert rows(settings, 'api_keys')[0]['last_used_at'] is not None


@pytest.mark.parametrize('path', [
    '/api/v1/recordings', '/api/v1/recordings/one', '/api/v1/daily/2026-10-29', '/api/v1/search?q=看板',
])
@pytest.mark.parametrize('auth', ['missing', 'wrong-prefix', 'random', 'revoked', 'malformed'])
def test_unauthorized_is_uniform_and_audited(api, settings, path, auth):
    client, key = api
    key_id = rows(settings, 'api_keys')[0]['id']
    if auth == 'revoked':
        assert client.get('/api/v1/recordings').status_code == 200
        assert cli(settings, 'revoke', str(key_id)).returncode == 0
    client.headers.pop('Authorization')
    headers = {
        'missing': {}, 'wrong-prefix': {'Authorization': f'Basic {key}'},
        'random': {'Authorization': 'Bearer qn_' + secrets.token_hex(16)},
        'revoked': {'Authorization': f'Bearer {key}'},
        'malformed': {'Authorization': f'Bearer {key} extra'},
    }[auth]
    response = client.get(path, headers=headers)
    assert response.status_code == 401 and response.json() == {'detail': 'unauthorized'}
    audits = rows(settings, 'api_audit')
    assert len(audits) == (2 if auth == 'revoked' else 1)
    assert audits[-1]['status_code'] == 401
    assert audits[-1]['key_id'] == (key_id if auth == 'revoked' else None)
    assert audits[-1]['path'] == path.split('?')[0]


def test_duplicate_authorization_and_cookie_cannot_bypass_key(api, settings):
    client, key = api
    response = client.get('/api/v1/recordings', headers=[
        ('Authorization', f'Bearer {key}'), ('Authorization', f'Bearer {key}'),
    ])
    assert response.status_code == 401
    client.headers.pop('Authorization')
    from test_login import login
    login(client)
    assert client.get('/api/v1/recordings').json() == {'detail': 'unauthorized'}
    assert len(rows(settings, 'api_audit')) == 2


def test_daily_markdown_has_full_contents_table_and_no_pagination(api, add_recording):
    client, _ = api
    for i in range(23):
        add_recording(f'row-{i:02}', title=f'标题{i}', transcript='' if i == 0 else f'原文{i}')
    add_recording('outside', title='别日录音', created='2026-10-28T23:59:59+08:00')
    response = client.get('/api/v1/daily/2026-10-29?format=md')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'text/markdown; charset=utf-8'
    for text in ['# 2026-10-29 工作记录', '共 23 条录音，合计 1127 分钟。',
                 '## 录音清单', '| 时间 | 标题 | 时长 | 状态 |', '|---|---|---|---|',
                 '## 详细内容', '#### 总结', '看板总结', '#### 转写原文', '（未转录）']:
        assert text in response.text
    for i in range(23):
        assert f'### 10:01 标题{i}（49 分钟）' in response.text
    assert '别日录音' not in response.text
    assert client.get('/api/v1/daily/2026-10-29').json()['count'] == 23


@pytest.mark.parametrize('format', ['json', 'md'])
def test_empty_daily(api, format):
    response = api[0].get(f'/api/v1/daily/2000-01-01?format={format}')
    assert response.status_code == 200
    if format == 'json':
        assert response.json() == {'date': '2000-01-01', 'count': 0, 'recordings': []}
    else:
        assert '共 0 条录音，合计 0 分钟。' in response.text and '当天没有录音' in response.text


def test_list_pagination_dates_and_total(api, add_recording):
    client, _ = api
    for i in range(25):
        add_recording(f'row-{i:02}', created='2026-10-29T23:59:59.999999+08:00')
    add_recording('start', created='2026-10-29T00:00:00+08:00')
    add_recording('outside', created='2026-10-30T00:00:00+08:00')
    result = client.get('/api/v1/recordings?date=2026-10-29&limit=3&offset=2').json()
    assert result['total'] == 26 and result['limit'] == 3 and result['offset'] == 2
    assert [i['recording_id'] for i in result['items']] == ['row-22', 'row-21', 'row-20']
    assert client.get('/api/v1/recordings?offset=99').json() == {
        'total': 27, 'limit': 20, 'offset': 99, 'items': [],
    }
    for query in ['from=2026-10-29&to=2026-10-29', 'from=2026-10-29', 'to=2026-10-29']:
        expected = 27 if query == 'from=2026-10-29' else 26
        assert client.get('/api/v1/recordings?' + query).json()['total'] == expected


@pytest.mark.parametrize('summary,transcript,status', [
    ('', '', 'empty'), ('总结', '', 'summary_only'), ('', '原文', 'transcript_only'),
])
def test_status_derivation(api, add_recording, summary, transcript, status):
    add_recording('one', summary=summary, transcript=transcript, duration=None)
    result = api[0].get('/api/v1/recordings/one').json()
    assert result['status'] == status and result['duration_min'] is None
    assert result['summary_md'] == summary and result['transcript_md'] == transcript


@pytest.mark.parametrize('query', ['看板', '数据看板', 'ROADMAP', '%', '_', '" OR ', "' OR 1=1 --"])
def test_search_is_literal_filtered_limited_and_matches_fields(api, add_recording, query):
    client, _ = api
    for i in range(3):
        add_recording(str(i), title=query.lower(), summary=query, transcript=query,
                      created='2026-10-28T23:59:59.999999+08:00')
    add_recording('outside', title=query, created='2026-10-29T00:00:00+08:00')
    result = client.get('/api/v1/search', params={
        'q': query, 'from': '2026-10-28', 'to': '2026-10-28', 'limit': 2,
    }).json()
    assert result['total'] == 3 and result['q'] == query.strip()
    assert [item['recording_id'] for item in result['items']] == ['2', '1']
    assert all(item['matched'] == ['title', 'summary', 'transcript'] for item in result['items'])


@pytest.mark.parametrize('path', [
    '/api/v1/recordings?date=20261029', '/api/v1/recordings?date=2026-02-30',
    '/api/v1/recordings?date=2026-10-29&from=2026-10-01',
    '/api/v1/recordings?from=2026-10-30&to=2026-10-29',
    '/api/v1/recordings?limit=0', '/api/v1/recordings?limit=101',
    '/api/v1/recordings?offset=-1', '/api/v1/recordings?offset=bad',
    '/api/v1/daily/bad', '/api/v1/daily/2026-10-29?format=html',
    '/api/v1/search', '/api/v1/search?q=%20%20', '/api/v1/search?q=x&limit=0',
])
def test_invalid_parameters_are_audited(api, settings, path):
    response = api[0].get(path)
    assert response.status_code == 400
    audit, = rows(settings, 'api_audit')
    assert audit['status_code'] == 400 and audit['key_id'] is not None


@pytest.mark.parametrize('method,path,status', [
    ('get', '/api/v1/recordings/missing', 404), ('post', '/api/v1/recordings', 405),
    ('delete', '/api/v1/recordings/one', 405), ('get', '/api/v1/missing', 404),
])
def test_errors_and_write_attempts_are_audited(api, settings, method, path, status):
    response = getattr(api[0], method)(path)
    assert response.status_code == status
    assert rows(settings, 'api_audit')[0]['status_code'] == status


def test_keys_only_print_once_hash_only_storage_no_header_log_or_audit_leak(api, settings, caplog):
    client, key = api
    key_row, = rows(settings, 'api_keys')
    assert key_row['key_hash'] == hashlib.sha256(key.encode()).hexdigest()
    assert key_row['name'] == 'codex' and key_row['revoked_at'] is None
    result = cli(settings, 'list')
    assert result.returncode == 0
    for value in ['id', 'name', 'created_at', 'last_used_at', 'status', 'codex', 'active']:
        assert value in result.stdout
    response = client.get('/api/v1/recordings')
    revoked = cli(settings, 'revoke', str(key_row['id']))
    listed = cli(settings, 'list')
    assert 'revoked' in listed.stdout and revoked.returncode == 0
    assert client.get('/api/v1/recordings').status_code == 401
    assert key not in str(response.headers) + response.text + caplog.text + result.stdout + listed.stdout + revoked.stdout
    with sqlite3.connect(settings.db_path) as conn:
        assert key not in '\n'.join(conn.iterdump())
    for file in settings.db_path.parent.glob('*sqlite3*'):
        assert key.encode() not in file.read_bytes()


def test_cli_independent_database_and_idempotent_migration(settings):
    first = cli(settings, 'create', '--name', 'one')
    second = cli(settings, 'create', '--name', 'two')
    assert first.returncode == second.returncode == 0 and first.stdout != second.stdout
    keys = rows(settings, 'api_keys')
    assert len(keys) == 2 and all(key['last_used_at'] is None for key in keys)
    assert cli(settings, 'revoke', str(keys[0]['id'])).returncode == 0
    revoked_at = rows(settings, 'api_keys')[0]['revoked_at']
    assert cli(settings, 'revoke', str(keys[0]['id'])).returncode == 0
    assert rows(settings, 'api_keys')[0]['revoked_at'] == revoked_at
    assert cli(settings, 'revoke', '999').returncode != 0
    assert cli(settings, 'create', '--name', '   ').returncode != 0


def test_cli_requires_database(monkeypatch):
    monkeypatch.delenv('DB_PATH', raising=False)
    result = subprocess.run([sys.executable, '-m', 'app.apikeys', 'list'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'DB_PATH is required' in result.stderr


def test_revoked_key_does_not_update_last_used_and_bearer_cannot_sync(api, settings):
    client, _ = api
    assert client.get('/api/v1/recordings').status_code == 200
    assert client.post('/sync').status_code == 401
    key, = rows(settings, 'api_keys')
    assert cli(settings, 'revoke', str(key['id'])).returncode == 0
    assert client.get('/api/v1/recordings?limit=bad').json() == {'detail': 'unauthorized'}
    assert rows(settings, 'api_keys')[0]['last_used_at'] == key['last_used_at']
    assert rows(settings, 'sync_runs') == []


def test_internal_failure_is_also_audited(api, store, settings, monkeypatch):
    from fastapi.testclient import TestClient

    def unavailable(recording_id):
        raise RuntimeError('storage unavailable')

    monkeypatch.setattr(store, 'get_recording', unavailable)
    client, key = api
    response = TestClient(client.app, raise_server_exceptions=False).get(
        '/api/v1/recordings/one', headers={'Authorization': f'Bearer {key}'},
    )
    assert response.status_code == 500
    audit, = rows(settings, 'api_audit')
    assert audit['status_code'] == 500 and audit['key_id'] is not None


def test_daily_markdown_handles_table_characters_and_unknown_duration(api, add_recording):
    add_recording('one', title='标题|一\n第二行', summary='', transcript='', duration=None)
    response = api[0].get('/api/v1/daily/2026-10-29?format=md')
    assert response.status_code == 200
    assert '| 10:01 | 标题\\|一 第二行 | 时长未知 | 未转录 |' in response.text
    assert '#### 总结\n\n（暂无总结）' in response.text
    assert '合计仅包含已知时长' in response.text


def test_search_has_readable_empty_result(api):
    assert api[0].get('/api/v1/search?q=没有结果').json() == {'q': '没有结果', 'total': 0, 'items': []}
