from hotword_fixtures import seed_rule
"""Ticket 19: no live network, including real MCP JSON-RPC and OAuth transports."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from app.hotword_upstream import (HttpHotwordSource, McpHotwordSource, execute, import_preview,
                                  plan, refresh, save_snapshot, sync_rows, execute_one, error_rows)
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
        self.failing_terms = set()
        self.error_text = "千问拒绝：该词暂不可添加"
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
                if self.failure == 'rpc':
                    return httpx.Response(200, json={'jsonrpc':'2.0','id':data['id'],
                        'error':{'code':-32000,'message':self.error_text}})
                if self.failure == 'tool' or args['hotWordList'][0] in self.failing_terms:
                    return httpx.Response(200, json={'jsonrpc':'2.0', 'id':data['id'], 'result':{
                        'isError':True, 'content':[{'type':'text', 'text':self.error_text}]}})
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


def push(client, identifier, **fields):
    return post(client, f'/hotwords/{identifier}/push-qwennote', **fields).json()


def writes(server):
    return [args for name, args in server.calls if name == 'add_personal_hot_word']


def test_page_standard_fields_and_no_automatic_push(client, store, upstream):
    login(client)
    store.add_hotword('标准词', note='备注')
    text = client.get('/hotwords').text
    assert 'name="aliases"' not in text and '别名' not in text and '替换原词' not in text
    assert '标准词（必填）' in text and '备注（选填）' in text
    assert re_columns(text) == ['标准词', '备注', '命中次数', '千问', '操作']
    assert '全部推送' in text and '推送到千问' in text and '未推送' in text
    assert not upstream[0].requests


def re_columns(text):
    import re
    return re.findall(r'<th scope="col">(.*?)</th>', text)


@pytest.mark.parametrize('term', ['标准词', 'AI Tutor', '㐀词', '词2026'])
def test_single_calls_once_with_only_term_and_ok(client, store, upstream, term):
    login(client)
    identifier = seed(store, term)
    before = rows(store, 'hotwords')
    result = push(client, identifier)
    assert result['status'] == 'ok'
    assert writes(upstream[0]) == [{'hotWordList': [term]}]
    assert sync_rows(store)[identifier]['status'] == 'ok'
    assert sync_rows(store)[identifier]['upstream_value'] == term
    assert sync_rows(store)[identifier]['synced_at']
    assert rows(store, 'hotwords') == before
    assert '已推送' in client.get('/hotwords').text
    assert not error_rows(store)


@pytest.mark.parametrize('term,enabled,reason', [
    ('AI-Tutor',1,'invalid_chars'), ('A_B',1,'invalid_chars'), ('词😊',1,'invalid_chars'),
    ('中文。',1,'invalid_chars'), ('é',1,'invalid_chars'), ('全角Ａ',1,'invalid_chars'),
    ('词\t语',1,'invalid_chars'), ('词\n语',1,'invalid_chars'), ('词\u00a0语',1,'invalid_chars'),
    ('中'*21,1,'too_long'), ('A'*41,1,'too_long'), ('1'*41,1,'too_long'),
    ('已停用',0,'disabled'),
])
def test_invalid_or_disabled_rejected_before_any_mcp(client, store, upstream, term, enabled, reason):
    login(client)
    identifier = seed(store, term, enabled=enabled)
    result = push(client, identifier)
    assert result['error'] == reason and result['status'] == 'failed'
    assert upstream[0].requests == []
    assert rows(store, 'hotwords')[0]['term'] == term


@pytest.mark.parametrize('term', ['中'*20, 'A'*40, 'AI Tutor 2026', '㐀词', '中'*20+'A'*40])
def test_upstream_length_boundaries(client, store, upstream, term):
    login(client)
    assert push(client, seed(store, term))['status'] == 'ok'
    assert writes(upstream[0]) == [{'hotWordList':[term]}]


@pytest.mark.parametrize('maximum', [0, 1, 200])
def test_full_quota_refuses_without_append(client, store, upstream, maximum):
    login(client)
    upstream[0].maximum = maximum
    upstream[0].words = [str(i) for i in range(maximum)]
    identifier = seed(store, '标准词')
    assert push(client, identifier)['error'] == 'quota_full'
    assert writes(upstream[0]) == []
    assert '配额已满' in client.get('/hotwords').text


@pytest.mark.parametrize('failure', ['timeout', '500', 'tool', 'rpc'])
def test_failure_persists_visible_reason_no_retry(client, store, upstream, failure):
    login(client)
    upstream[0].failure = failure
    identifier = seed(store, '标准词')
    result = push(client, identifier)
    assert result['status'] == 'failed' and result['message']
    assert sync_rows(store)[identifier]['status'] == 'failed'
    assert len(writes(upstream[0])) == 1
    assert result['message'] in client.get('/hotwords').text
    if failure in ('tool','rpc'):
        assert result['message'] == upstream[0].error_text
    else:
        assert 'sensitive' not in result['message']


def test_error_summary_truncated_escaped_and_credentials_redacted(client, store, upstream):
    login(client)
    upstream[0].failure = 'tool'
    upstream[0].error_text = '<script>oops</script> fake-offline-only ' + '原因'*300
    identifier = seed(store, '标准词')
    result = push(client, identifier)
    assert len(result['message']) == 200 and 'fake-offline-only' not in result['message']
    assert len(error_rows(store)[identifier]) == 200
    text = client.get('/hotwords').text
    assert '<script>oops</script>' not in text and '&lt;script&gt;oops&lt;/script&gt;' in text


def test_batch_three_one_fails_continues_and_correct_summary(client, store, upstream):
    login(client)
    for term in ['甲词', '乙词', '丙词']: store.add_hotword(term)
    upstream[0].failing_terms = {'乙词'}
    result = post(client).json()
    assert (result['success'], result['failed'], result['skipped']) == (2, 1, 0)
    assert result['message'] == '成功 2 / 失败 1'
    assert writes(upstream[0]) == [{'hotWordList':[t]} for t in ['甲词', '乙词', '丙词']]
    assert [r['status'] for r in result['results']] == ['ok', 'failed', 'ok']
    assert [r['status'] for r in sync_rows(store).values()] == ['ok', 'failed', 'ok']


def test_partial_quota_batch_keeps_first_success(client, store, upstream):
    login(client)
    upstream[0].maximum = 1
    for term in ['甲词','乙词']: store.add_hotword(term)
    result = post(client).json()
    assert (result['success'],result['failed']) == (1,1)
    assert len(writes(upstream[0])) == 1


def test_repeat_is_deduplicated_explicit_repush_works_and_error_clears(client, store, upstream):
    login(client)
    identifier = seed(store, '标准词')
    assert push(client, identifier)['status'] == 'ok'
    assert push(client, identifier)['status'] == 'skipped'
    assert len(writes(upstream[0])) == 1
    upstream[0].failure = 'tool'
    assert push(client, identifier, force='1')['status'] == 'failed'
    upstream[0].failure = None
    assert push(client, identifier)['status'] == 'ok'
    assert len(writes(upstream[0])) == 3 and not error_rows(store)


def test_term_change_needs_new_push_note_change_does_not(client, store, upstream):
    login(client)
    identifier = seed(store, '旧词')
    push(client, identifier)
    store.update_hotword(identifier, '旧词', '新备注')
    assert push(client, identifier)['status'] == 'skipped'
    store.update_hotword(identifier, '新词', '')
    assert push(client, identifier)['status'] == 'ok'
    assert sync_rows(store)[identifier]['upstream_value'] == '新词'


def test_import_uses_standard_terms_only_and_is_read_only(client, store, upstream):
    login(client)
    store.add_hotword('AI Tutor')
    upstream[0].words = ['ＡＩ tutor', '小 鹿 直播', '候选词', '候 选 词', '另一个词']
    before = rows(store, 'hotwords')
    result = post(client, '/hotwords/import-preview').json()
    assert result['candidates'] == ['小 鹿 直播','候选词','另一个词']
    assert rows(store, 'hotwords') == before and not writes(upstream[0])
    assert post(client, '/hotwords', from_upstream='1', term='候选词').json()['added']
    assert post(client, '/hotwords', from_upstream='1', term='候 选 词').json()['existing']


def test_invalid_import_no_write(client, store, upstream):
    login(client)
    assert post(client, '/hotwords', from_upstream='1', term='长'*21).status_code == 400
    assert store.list_hotwords() == []


@pytest.mark.parametrize('path', ['/hotwords/1/push-qwennote','/hotwords/sync-qwennote',
                                   '/hotwords/import-preview','/hotwords/qwennote-status'])
@pytest.mark.parametrize('role', ['anonymous','member','csrf'])
def test_admin_and_csrf(client, settings, store, upstream, path, role):
    seed(store, '标准词')
    if role == 'anonymous':
        response = client.post(path)
    else:
        login(client)
        token = csrf(client)
        if role == 'member':
            store.remove_user(settings.admin_open_id)
        response = client.post(path, data={'csrf_token':'wrong' if role == 'csrf' else token})
    assert response.status_code == 403 and upstream[0].requests == []


@pytest.mark.parametrize('payload', [
    {'hotWords':[], 'currentCount':True, 'maxCount':200},
    {'hotWords':[], 'currentCount':1, 'maxCount':200},
    {'hotWords':[], 'currentCount':0, 'maxCount':201},
    {'hotWords':[{}], 'currentCount':1, 'maxCount':200},
    {'hotWords':[], 'currentCount':-1, 'maxCount':200}, {},
])
def test_malformed_quota_fails_closed(client, store, upstream, payload):
    login(client)
    upstream[0].payload = payload
    result = push(client, seed(store, '标准词'))
    assert result['status'] == 'failed' and not writes(upstream[0])


@pytest.mark.parametrize('failure', ['timeout','500'])
def test_oauth_does_not_retry_append(client, store, tmp_path, failure):
    path = tmp_path/'token.json'
    path.write_text(json.dumps(dict(client_id='offline',access_token='offline',refresh_token='offline',
                                   obtained_at=9999999999,expires_in=3600)))
    server = Server(); server.failure = failure
    source = McpHotwordSource(token_file=path, transport=httpx.MockTransport(server),
                             sleep=lambda _: pytest.fail('must not retry'))
    identifier = seed(store, '标准词')
    assert execute_one(store,source,identifier)['status'] == 'failed'
    assert len(writes(server)) == 1


def test_serialized_single_requests_publish_once(client, store, upstream):
    server, source = upstream
    identifier = seed(store, '标准词')
    server.entered, server.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute_one, store, source, identifier)
        assert server.entered.wait(5)
        second = pool.submit(execute_one, store, source, identifier)
        server.release.set()
        assert first.result()['status'] == 'ok' and second.result()['status'] == 'skipped'
    assert len(writes(server)) == 1


def test_migration_idempotent_retains_sync(client, store, upstream):
    identifier = seed(store, '标准词')
    execute_one(store,upstream[1],identifier)
    before = [rows(store,t) for t in ['hotwords','hotword_upstream_sync','hotword_upstream_runs']]
    store.migrate();store.migrate()
    assert before == [rows(store,t) for t in ['hotwords','hotword_upstream_sync','hotword_upstream_runs']]


def test_sse_status_and_cache(client, store, upstream):
    login(client)
    upstream[0].sse = True; upstream[0].words = ['上游词']
    result = post(client, '/hotwords/qwennote-status')
    assert result.status_code == 200 and result.json()['currentCount'] == 1
    assert 'hotWords' not in result.json()
    assert '千问热词：已用 1 / 200' in client.get('/hotwords').text


def test_missing_word_no_mcp(client, store, upstream):
    login(client)
    assert push(client, 999)['error'] == 'not_found' and not upstream[0].requests


@pytest.mark.parametrize('confirm', ['0','1','','true'])
def test_old_preview_client_rejected_without_any_mcp(client, store, upstream, confirm):
    login(client)
    store.add_hotword('标准词')
    response = post(client, confirm=confirm)
    assert response.status_code == 400 and response.json()['error'] == 'stale_client'
    assert not upstream[0].requests and not sync_rows(store)
