"""Real Qwen MCP contracts exercised through a completely offline transport."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.mcp import McpMinutesSource, OAuthTokenManager, TokenRefreshError
from app.minutes import SHANGHAI
from app.store import SQLiteStore
from app.sync import run_sync
from test_login import login

NOW = datetime(2026, 10, 30, 12, tzinfo=SHANGHAI)


class Server:
    def __init__(self):
        self.calls = []
        self.requests = []
        self.refreshes = 0
        self.fail_refresh = False
        self.unauthorized = False
        self.failure = None
        self.filtered = False
        self.summary = True
        self.sse = True

    def __call__(self, request):
        self.requests.append(request)
        assert request.extensions['timeout']['read'] == 30
        if request.url.path == '/token':
            self.refreshes += 1
            assert request.headers['content-type'].startswith('application/x-www-form-urlencoded')
            assert parse_qs(request.content.decode()) == {
                'grant_type': ['refresh_token'], 'client_id': ['client-test'],
                'refresh_token': ['refresh-old'],
            }
            return httpx.Response(400 if self.fail_refresh else 200, json={
                'access_token': 'access-new', 'refresh_token': 'refresh-new', 'expires_in': 3600,
            })
        if self.unauthorized and request.headers['authorization'] == 'Bearer access-old':
            return httpx.Response(401)
        data = json.loads(request.content)
        method = data['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif method == 'notifications/initialized':
            return httpx.Response(202)
        elif method == 'tools/list':
            result = {'tools': [{'name': x} for x in McpMinutesSource.TOOLS]}
        else:
            name, args = data['params']['name'], data['params']['arguments']
            self.calls.append((name, args))
            if self.failure and name == self.failure[0]:
                if self.failure[1] == 'timeout':
                    raise httpx.ReadTimeout('must not persist private body', request=request)
                return httpx.Response(self.failure[1])
            if name == 'list_my_minutes':
                record_id = 'real-2' if 'cursor' in args else 'real-1'
                day = 30 if record_id == 'real-2' else 29
                payload = {'items': [{'uuid': record_id, 'title': record_id,
                    'gmtCreate': int(datetime(2026, 10, day, 10, tzinfo=SHANGHAI).timestamp()*1000),
                    'durationMillis': 123456, 'mediaType': 'AUDIO'}],
                    'total': 2, 'hasMore': record_id == 'real-1', 'nextToken': 'page-two'}
            elif name == 'get_minutes_basic_info':
                payload = {'taskUuid': args['taskUuid'], 'title': 'basic',
                           'durationMillis': 123456, 'mediaType': 'VIDEO', 'shareUrl': ''}
            elif name == 'get_minutes_ai_summary':
                payload = {'taskUuid': args['taskUuid'], 'hasSummary': self.summary, 'fullSummary': '# 总结'}
            elif name == 'get_minutes_media_url':
                payload = {'available': False, 'filterReason': 'INCOGNITO_MODE'}
            else:
                payload = {'filtered': self.filtered, 'paragraphs': [{
                    'speakerName': '发言人', 'speakerUid': 'one',
                    'startTime': 1234, 'endTime': 65001, 'content': '正文'}],
                    'hasMore': 'nextToken' not in args, 'nextToken': 'transcript-two'}
            result = {'content': [{'type': 'text', 'text': json.dumps(payload)}]}
        envelope = {'jsonrpc': '2.0', 'id': data['id'], 'result': result}
        if self.sse:
            # Includes a heartbeat and a final data line without an empty terminator.
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                text=': heartbeat\n\ndata: '+json.dumps(envelope)+'\n')
        return httpx.Response(200, json=envelope)


@pytest.fixture
def setup_source(tmp_path):
    token = tmp_path/'mcp_token.json'
    token.write_text(json.dumps({'client_id': 'client-test', 'scope': 'minutes',
        'obtained_at': NOW.timestamp(), 'expires_in': 3600,
        'access_token': 'access-old', 'refresh_token': 'refresh-old'}))
    token.chmod(0o600)
    server = Server()
    source = McpMinutesSource(token_file=token, transport=httpx.MockTransport(server),
                              now=lambda: NOW, sleep=lambda _: None)
    return source, server, token


def db_rows(store, table):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT * FROM '+table)]


@pytest.mark.parametrize('sse', [True, False])
def test_window_two_pages_idempotent_and_local_dates(setup_source, store, sse):
    source, server, _ = setup_source
    server.sse = sse
    store.migrate()
    first = run_sync(source, store)
    second = run_sync(source, store)
    assert (first.status, first.fetched, first.inserted) == ('success', 2, 2)
    assert (second.inserted, second.updated) == (0, 0)
    assert len(db_rows(store, 'recordings')) == 2
    assert store.list_recordings(NOW.date().replace(day=29), NOW.date().replace(day=29)).total == 1
    assert store.list_recordings(NOW.date(), NOW.date()).items[0].recording_id == 'real-2'
    calls = [a for n,a in server.calls if n == 'list_my_minutes']
    assert len(calls) == 4 and calls[1]['cursor'] == 'page-two'
    assert calls[0]['startTime'] == int(datetime(2026,8,1,tzinfo=SHANGHAI).timestamp()*1000)
    assert calls[0]['endTime'] == int(NOW.timestamp()*1000)
    assert db_rows(store, 'recordings')[0]['media_type'] == 'VIDEO'


def test_date_query_filters_provider_cross_day(setup_source):
    source, _, _ = setup_source
    assert [m.recording_id for m in source.list_by_date(NOW.date().replace(day=29))] == ['real-1']


def test_full_transcription_format_and_basic_info(setup_source):
    source, server, _ = setup_source
    detail = source.get_detail('real-1')
    assert detail.transcript_md == '[00:01.234–01:05.001] 发言人\n\n正文\n\n' * 2
    assert detail.has_summary and detail.has_transcript and detail.media_type == 'VIDEO'
    calls = [a for n,a in server.calls if n == 'get_minutes_transcription']
    assert len(calls) == 2 and calls[1]['nextToken'] == 'transcript-two'


def test_filtered_and_no_summary_page(setup_source, settings, fake_auth):
    source, server, _ = setup_source
    server.filtered, server.summary = True, False
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, minutes_source=source), follow_redirects=False) as client:
        login(client)
        assert client.post('/sync').json()['status'] == 'success'
        detail = client.get('/recordings/real-1').text
        assert '未转录' in detail and '这条录音还没有 AI 总结' in detail
        transcript = client.get('/recordings/real-1?view=transcript').text
        assert '这条录音还没有转写内容' in transcript
        assert not source.get_detail('real-1').has_transcript
        assert '正文' not in detail


def test_401_refresh_rotation_persisted_and_run_flag(setup_source, store):
    source, server, token = setup_source
    server.unauthorized = True
    store.migrate()
    result = run_sync(source, store)
    assert result.status == 'success' and result.refreshed_token == 1
    saved = json.loads(token.read_text())
    assert saved['refresh_token'] == 'refresh-new' and saved['access_token'] == 'access-new'
    assert saved['client_id'] == 'client-test' and saved['scope'] == 'minutes'
    assert token.stat().st_mode & 0o777 == 0o600
    assert store.latest_sync().refreshed_token == 1
    assert run_sync(source, store).refreshed_token == 0
    assert server.refreshes == 1


def test_failed_refresh_fails_sync_preserves_data(setup_source, store):
    source, server, token = setup_source
    store.migrate()
    assert run_sync(source, store).inserted == 2
    before = db_rows(store, 'recordings')
    old_token = token.read_bytes()
    server.unauthorized, server.fail_refresh = True, True
    result = run_sync(source, store)
    assert result.status == 'failed' and '续期失败' in result.error
    assert db_rows(store, 'recordings') == before and token.read_bytes() == old_token
    assert 'access-old' not in result.error and 'refresh-old' not in result.error
    with pytest.raises(TokenRefreshError, match='续期失败'):
        source.get_detail('real-1')


@pytest.mark.parametrize('age', [3480, 3601])
def test_expiry_margin_and_file_lock_across_managers(setup_source, age):
    source, server, token = setup_source
    saved = json.loads(token.read_text())
    saved['obtained_at'] -= age
    token.write_text(json.dumps(saved))
    managers = [OAuthTokenManager(token, transport=httpx.MockTransport(server), now=lambda: NOW) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda m: m.access_token(), managers)) == ['access-new', 'access-new']
    assert server.refreshes == 1


@pytest.mark.parametrize('failure', [404, 403, 'timeout', 503])
def test_single_detail_failure_does_not_stop_other_records(setup_source, store, failure):
    source, server, _ = setup_source
    original = server.__call__
    def handler(request):
        data = json.loads(request.content)
        if data.get('params', {}).get('arguments', {}).get('taskUuid') == 'real-1':
            server.failure = ('get_minutes_basic_info', failure)
        else:
            server.failure = None
        return original(request)
    source._transport = httpx.MockTransport(handler)
    store.migrate()
    result = run_sync(source, store)
    assert result.status == 'failed' and result.fetched == 2 and result.inserted == 1
    assert 'real-1' in result.error
    assert db_rows(store, 'recordings')[0]['recording_id'] == 'real-2'
    attempts = [a for n,a in server.calls if n == 'get_minutes_basic_info' and a['taskUuid'] == 'real-1']
    assert len(attempts) == (3 if failure in ('timeout', 503) else 1)


def test_private_media_is_a_normal_result_without_logs(setup_source, caplog):
    source, _, _ = setup_source
    assert source.get_media_url('real-1') == {'available': False, 'filterReason': 'INCOGNITO_MODE'}
    assert not [r for r in caplog.records if r.levelname in ('WARNING', 'ERROR')]


def test_purge_fixture_only_and_fts_consistent(setup_source, store, monkeypatch, capsys):
    from app.purge_fixture import main
    source, _, _ = setup_source
    store.migrate()
    run_sync(source, store)
    meta = replace(source.sync_window(90)[0], recording_id='fixture-test')
    detail = replace(source.get_detail('real-1'), recording_id=meta.recording_id)
    store.upsert_recording(meta, detail, NOW.isoformat())
    monkeypatch.setenv('DB_PATH', str(store.db_path))
    assert main() == 0
    assert '1' in capsys.readouterr().out
    assert len(db_rows(store, 'recordings')) == 2
    assert store.search_recordings('real-1').total == 1
    assert main() == 0
    assert '0' in capsys.readouterr().out


def test_real_transcript_renders_timestamp_and_speaker(setup_source):
    from app.recording_content import transcript_segments
    source, _, _ = setup_source
    segments = transcript_segments(source.get_detail('real-1').transcript_md)
    assert len(segments) == 2
    assert segments[0] == {'timestamp': '[00:01.234–01:05.001]', 'speaker': '发言人', 'paragraphs': ['正文']}


def test_401_is_only_refreshed_once(setup_source):
    source, server, _ = setup_source
    def handler(request):
        return server(request) if request.url.path == '/token' else httpx.Response(401)
    source._transport = httpx.MockTransport(handler)
    from app.minutes import MinutesSourceError
    with pytest.raises(MinutesSourceError):
        source.sync_window(90)
    assert server.refreshes == 1


def test_empty_or_later_filtered_transcript_is_unavailable(setup_source):
    source, server, _ = setup_source
    original = server.__call__
    def handler(request):
        data = json.loads(request.content)
        if data.get('params', {}).get('name') == 'get_minutes_transcription':
            server.filtered = 'nextToken' in data['params']['arguments']
        return original(request)
    source._transport = httpx.MockTransport(handler)
    detail = source.get_detail('real-1')
    assert not detail.has_transcript and detail.transcript_md is None


def test_refresh_without_rotation_preserves_refresh_token(setup_source):
    source, server, token = setup_source
    def handler(request):
        return httpx.Response(200, json={'access_token': 'access-new', 'expires_in': 3600})
    source.tokens._transport = httpx.MockTransport(handler)
    assert source.tokens.access_token(rejected_token='access-old') == 'access-new'
    assert json.loads(token.read_text())['refresh_token'] == 'refresh-old'
