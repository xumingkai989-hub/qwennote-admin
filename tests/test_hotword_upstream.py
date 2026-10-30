"""Ticket 19: no live network, including real MCP JSON-RPC and OAuth transports."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from app.hotword_upstream import (HttpHotwordSource, McpHotwordSource, execute, import_preview,
                                  plan, refresh, save_snapshot, sync_rows)
from app.minutes import MinutesSourceError
from test_hotwords import csrf
from test_login import login


class Server:
    def __init__(self):
        self.requests = []
        self.calls = []
        self.words = []
        self.maximum = 200
        self.failure = None
        self.payload = None
        self.sse = False
        self.entered = None
        self.release = None

    def __call__(self, request):
        data = json.loads(request.content)
        self.requests.append(data)
        method = data['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif method == 'notifications/initialized':
            return httpx.Response(202)
        elif method == 'tools/list':
            result = {'tools': [{'name': name} for name in HttpHotwordSource.TOOLS]}
        else:
            name, args = data['params']['name'], data['params']['arguments']
            self.calls.append((name, args))
            if name == 'list_hot_words':
                payload = self.payload if self.payload is not None else dict(
                    hotWords=self.words.copy(), currentCount=len(self.words), maxCount=self.maximum)
            else:
                assert name == 'add_personal_hot_word'
                assert set(args) == {'hotWordList'}
                if self.entered:
                    self.entered.set()
                    assert self.release.wait(5)
                if self.failure == 'timeout':
                    raise httpx.ReadTimeout('sensitive provider text must not escape', request=request)
                if self.failure == '500':
                    return httpx.Response(500, text='sensitive provider text')
                if self.failure == 'tool':
                    return httpx.Response(200, json={'jsonrpc':'2.0', 'id':data['id'], 'result':{
                        'isError':True, 'content':[{'type':'text', 'text':'sensitive provider text'}]}})
                self.words.extend(args['hotWordList'])
                payload = {'success': True}
            result = {'content': [{'type': 'text', 'text': json.dumps(payload)}]}
        envelope = {'jsonrpc':'2.0', 'id':data['id'], 'result':result}
        if self.sse:
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                  text='data: '+json.dumps(envelope)+'\n\n')
        return httpx.Response(200, json=envelope)


@pytest.fixture
def upstream(client):
    server = Server()
    source = HttpHotwordSource('fake-offline-only', transport=httpx.MockTransport(server))
    client.app.state.hotword_source = source
    return server, source


def seed(store, term, *, enabled=1):
    # Historical words may exceed today's local input constraints.
    with store._connect() as conn:
        return conn.execute("INSERT INTO hotwords(term,enabled,created_at,updated_at) VALUES (?,?,'old','old')",
                            (term, enabled)).lastrowid


def rows(store, table):
    with store._connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM '+table)]


def post(client, path='/hotwords/sync-qwennote', **fields):
    return client.post(path, data={'csrf_token':csrf(client), **fields})


def test_preview_sends_zero_transport_requests_and_writes_nothing(client, store, upstream):
    server, _ = upstream
    login(client)
    store.add_hotword('小鹿直播', '小鹅直播')
    save_snapshot(store, dict(hotWords=[], currentCount=0, maxCount=200, checked_at='test'))
    before = rows(store, 'hotwords')
    result = post(client, confirm='0').json()
    assert [r['term'] for r in result['to_push']] == ['小鹿直播']
    assert result['afterCount'] == 1 and result['can_confirm']
    assert server.requests == []
    assert rows(store, 'hotwords') == before
    assert not sync_rows(store) and not rows(store, 'hotword_upstream_runs')


def test_unknown_quota_never_assumes_empty_upstream(client, store, upstream):
    login(client)
    store.add_hotword('标准词')
    result = post(client, confirm='0').json()
    assert result['error'] == 'quota_unknown' and result['afterCount'] is None
    assert not result['can_confirm'] and upstream[0].requests == []


def test_confirm_batches_terms_only_and_persists_all_results(client, store, upstream):
    login(client)
    store.add_hotword('小鹿直播', '小鹅直播,别名')
    store.add_hotword('AI Tutor', '人工智能')
    result = post(client, confirm='1').json()
    assert result['status'] == 'ok' and result['afterCount'] == 2
    writes = [args for name, args in upstream[0].calls if name == 'add_personal_hot_word']
    assert writes == [{'hotWordList':['AI Tutor', '小鹿直播']}]
    assert {r['upstream_value'] for r in sync_rows(store).values()} == {'小鹿直播','AI Tutor'}
    assert {r['status'] for r in sync_rows(store).values()} == {'ok'}
    assert all(r['synced_at'] for r in sync_rows(store).values())
    run, = rows(store, 'hotword_upstream_runs')
    assert run['status'] == 'ok' and run['finished_at']
    result = post(client, confirm='1').json()
    assert not result['to_push'] and {r['reason'] for r in result['skipped']} == {'already_synced'}
    assert len([1 for name, _ in upstream[0].calls if name == 'add_personal_hot_word']) == 1
    assert {r['status'] for r in sync_rows(store).values()} == {'ok'}
    assert len(rows(store, 'hotword_upstream_runs')) == 2


@pytest.mark.parametrize('term,enabled,reason', [
    ('AI-Tutor',1,'invalid_chars'), ('A_B',1,'invalid_chars'), ('词😊',1,'invalid_chars'),
    ('中文。',1,'invalid_chars'), ('é',1,'invalid_chars'), ('全角Ａ',1,'invalid_chars'),
    ('词\t语',1,'invalid_chars'), ('词\n语',1,'invalid_chars'), ('词\u00a0语',1,'invalid_chars'),
    ('中'*21,1,'too_long'), ('A'*41,1,'too_long'), ('1'*41,1,'too_long'),
    ('已停用',0,'disabled'),
])
def test_each_skip_reason_with_no_write_call(client, store, upstream, term, enabled, reason):
    login(client)
    identifier = seed(store, term, enabled=enabled)
    result = post(client, confirm='1').json()
    skipped, = result['skipped']
    assert skipped['reason'] == reason and skipped['term'] == term and skipped['message']
    assert not result['to_push']
    assert not [1 for name, _ in upstream[0].calls if name == 'add_personal_hot_word']
    assert sync_rows(store)[identifier]['status'] == 'skipped'
    assert rows(store, 'hotwords')[0]['term'] == term


@pytest.mark.parametrize('term', ['中'*20, 'A'*40, 'AI Tutor 2026', '㐀词'])
def test_upstream_length_boundaries_are_inclusive(client, store, upstream, term):
    login(client)
    seed(store, term)
    result = post(client, confirm='1').json()
    assert result['status'] == 'ok' and len(result['to_push']) == 1


def test_changed_term_is_unsynced_and_alias_change_is_not(client, store, upstream):
    login(client)
    store.add_hotword('旧标准词', '旧别名')
    post(client, confirm='1')
    word, = store.list_hotwords()
    store.update_hotword(word.id, word.term, '新别名', '')
    assert plan(store)['skipped'][0]['reason'] == 'already_synced'
    store.update_hotword(word.id, '新标准词', '新别名', '')
    assert [r['term'] for r in plan(store)['to_push']] == ['新标准词']
    post(client, confirm='1')
    assert sync_rows(store)[word.id]['upstream_value'] == '新标准词'
    assert len(rows(store, 'hotword_upstream_runs')) == 2


def test_insufficient_quota_aborts_entire_batch_without_local_writes(client, store, upstream):
    login(client)
    server, source = upstream
    server.words = [str(i) for i in range(199)]
    refresh(store, source)
    store.add_hotword('甲词')
    store.add_hotword('乙词')
    before = rows(store, 'hotword_upstream_state')
    for confirm in ['0','1']:
        result = post(client, confirm=confirm).json()
        assert result['error'] == 'quota_insufficient' and result['afterCount'] == 201
    assert not sync_rows(store) and not rows(store, 'hotword_upstream_runs')
    assert rows(store, 'hotword_upstream_state') == before
    assert not [1 for name, _ in server.calls if name == 'add_personal_hot_word']


def test_confirmation_rechecks_real_quota_before_writing(client, store, upstream):
    login(client)
    server, source = upstream
    refresh(store, source)
    store.add_hotword('甲词')
    preview = post(client, confirm='0').json()
    server.words = [str(i) for i in range(200)]
    result = post(client, confirm='1', plan_hash=preview['plan_hash']).json()
    assert result['error'] == 'quota_insufficient'
    assert len(server.calls) == 2 and not sync_rows(store)


def test_changed_plan_requires_another_preview(client, store, upstream):
    login(client)
    refresh(store, upstream[1])
    store.add_hotword('甲词')
    preview = post(client, confirm='0').json()
    store.add_hotword('乙词')
    result = post(client, confirm='1', plan_hash=preview['plan_hash']).json()
    assert result['error'] == 'plan_changed' and not sync_rows(store)
    assert not [1 for name, _ in upstream[0].calls if name == 'add_personal_hot_word']


@pytest.mark.parametrize('failure', ['timeout','500','tool'])
def test_failure_is_safe_recorded_and_does_not_mark_words_synced(client, store, upstream, failure):
    login(client)
    upstream[0].failure = failure
    store.add_hotword('甲词')
    store.add_hotword('乙词')
    response = post(client, confirm='1')
    assert response.json()['error'] == 'upstream_failed'
    assert {r['status'] for r in sync_rows(store).values()} == {'failed'}
    assert rows(store, 'hotword_upstream_runs')[0]['status'] == 'failed'
    assert len([1 for name, _ in upstream[0].calls if name == 'add_personal_hot_word']) == 1
    assert 'sensitive' not in response.text


def test_import_preview_is_read_only_and_selection_deduplicates_aliases(client, store, upstream):
    login(client)
    store.add_hotword('AI Tutor', '小鹿直播')
    upstream[0].words = ['ＡＩ tutor', '小 鹿 直播', '候选词', '候 选 词', '另一个词']
    before = rows(store, 'hotwords')
    result = post(client, '/hotwords/import-preview').json()
    assert result['candidates'] == ['候选词', '另一个词']
    assert rows(store, 'hotwords') == before
    assert not rows(store, 'hotword_upstream_state')
    assert not sync_rows(store) and not rows(store, 'hotword_upstream_runs')
    assert post(client, '/hotwords', from_upstream='1', term='候选词').json()['added']
    assert post(client, '/hotwords', from_upstream='1', term='候 选 词').json()['existing']
    assert post(client, '/hotwords', from_upstream='1', term='小鹿直播').json()['existing']
    assert {r.term for r in store.list_hotwords()} == {'AI Tutor','候选词'}


def test_invalid_import_selection_has_inline_safe_error_and_no_write(client, store, upstream):
    login(client)
    assert post(client, '/hotwords', from_upstream='1', term='长'*21).status_code == 400
    assert store.list_hotwords() == []


@pytest.mark.parametrize('path', ['/hotwords/sync-qwennote','/hotwords/import-preview','/hotwords/qwennote-status'])
def test_admin_and_csrf_boundaries(client, settings, store, upstream, path):
    assert client.post(path, data={'confirm':'1'}).status_code == 403
    login(client)
    token = csrf(client)
    assert client.post(path, data={'confirm':'1','csrf_token':'wrong'}).status_code == 403
    from app.feishu import FeishuUser
    store.add_user(FeishuUser('not-admin', '普通用户', ''))
    from app.session import SignedCookie, SESSION_MAX_AGE
    client.cookies.set('session', SignedCookie(settings.session_secret, salt='web-session',
                       max_age=SESSION_MAX_AGE).encode('not-admin'))
    assert client.post(path, data={'confirm':'1','csrf_token':token}).status_code == 403
    assert upstream[0].requests == []


@pytest.mark.parametrize('confirm', ['', 'true', '2'])
def test_invalid_confirm_has_no_effect(client, store, upstream, confirm):
    login(client)
    assert post(client, confirm=confirm).status_code == 400
    assert upstream[0].requests == []


def test_migration_is_idempotent_and_preserves_history(client, store, upstream):
    login(client)
    store.add_hotword('标准词')
    post(client, confirm='1')
    before = [rows(store, table) for table in ['hotwords','hotword_upstream_sync','hotword_upstream_runs']]
    store.migrate()
    store.migrate()
    assert [rows(store, table) for table in ['hotwords','hotword_upstream_sync','hotword_upstream_runs']] == before


def test_serialized_confirmations_only_publish_once(client, store, upstream):
    server, source = upstream
    store.add_hotword('标准词')
    server.entered, server.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, store, source)
        assert server.entered.wait(5)
        second = pool.submit(execute, store, HttpHotwordSource('fake', transport=httpx.MockTransport(server)))
        server.release.set()
        assert first.result()['status'] == second.result()['status'] == 'ok'
    assert len([1 for name, _ in server.calls if name == 'add_personal_hot_word']) == 1
    assert len(rows(store, 'hotword_upstream_runs')) == 2


@pytest.mark.parametrize('failure', ['timeout', '500'])
def test_oauth_mutation_does_not_retry_non_idempotent_call(client, store, tmp_path, failure):
    path = tmp_path / 'token.json'
    path.write_text(json.dumps(dict(client_id='offline',access_token='offline',refresh_token='offline',
                                    obtained_at=9999999999,expires_in=3600)))
    server = Server()
    server.failure = failure
    source = McpHotwordSource(token_file=path, transport=httpx.MockTransport(server),
                             sleep=lambda _: pytest.fail('append must not back off or retry'))
    store.add_hotword('标准词')
    assert execute(store, source)['error'] == 'upstream_failed'
    assert len([1 for name, _ in server.calls if name == 'add_personal_hot_word']) == 1


@pytest.mark.parametrize('payload', [
    {'hotWords':[], 'currentCount':True, 'maxCount':200},
    {'hotWords':[], 'currentCount':1, 'maxCount':200},
    {'hotWords':[], 'currentCount':0, 'maxCount':201},
    {'hotWords':[{}], 'currentCount':1, 'maxCount':200},
    {'hotWords':[], 'currentCount':-1, 'maxCount':200},
    {},
])
def test_malformed_quota_fails_closed(client, store, upstream, payload):
    login(client)
    upstream[0].payload = payload
    store.add_hotword('标准词')
    result = post(client, confirm='1')
    assert result.status_code == 502 and result.json()['error'] == 'upstream_unavailable'
    assert not sync_rows(store) and not rows(store, 'hotword_upstream_runs')
    assert not [1 for name, _ in upstream[0].calls if name == 'add_personal_hot_word']


def test_sse_transport_and_status_cache(client, store, upstream):
    login(client)
    upstream[0].sse = True
    upstream[0].words = ['上游词']
    response = post(client, '/hotwords/qwennote-status')
    assert response.status_code == 200 and response.json()['currentCount'] == 1
    assert 'hotWords' not in response.json()
    assert '千问热词：已用 1 / 200' in client.get('/hotwords').text
