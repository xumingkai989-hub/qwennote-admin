"""Durable enrichment with real SQLite and offline model transports."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Barrier, Event, Lock
import time

import httpx
import pytest

from app import enrichment as e
from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.session import SignedCookie, SESSION_MAX_AGE
from app.store import SQLiteStore
from app.sync import run_sync
from app.tag import TagSettings, run_tags
from fakes import FakeMinutesSource
from test_login import login

REAL_BACKGROUND = e.start_background


@pytest.fixture
def queued(store):
    store.migrate()
    meta = RecordingMeta('enhance', '增强验收', datetime.now(SHANGHAI) - timedelta(minutes=1), 1)
    source = FakeMinutesSource([meta], {'enhance': RecordingDetail('enhance', '内容', '转写', True, True)})
    run = run_sync(source, store, backfill_days=1)
    return store, source, run


def tasks(store):
    with store._connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM enrichment_tasks ORDER BY id')]


def configured(monkeypatch):
    monkeypatch.setattr(e, 'resolve_model', lambda _: TagSettings('offline-key'))


def test_sync_is_pending_without_any_ai_and_lock_is_released(queued, monkeypatch):
    store, source, _ = queued
    source.recordings[0] = replace(source.recordings[0], title='changed')
    monkeypatch.setattr('app.tag.run_tags', lambda *a, **kw: pytest.fail('AI in sync'))
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda *a, **kw: pytest.fail('AI in sync'))
    run = run_sync(source, store, full=True, backfill_days=1)
    assert e.status(store, run.enrichment_run_id)['status'] == 'pending'
    assert store.latest_sync().enrichment_run_id == run.enrichment_run_id
    with store.sync_lock():
        pass


def test_worker_saves_real_tags_and_run_counts(queued, monkeypatch):
    store, _, run = queued
    configured(monkeypatch)
    def tag(store, **kw):
        with store.sync_lock():
            pass
        return run_tags(store, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            'content': [{'type': 'text', 'text': '{"tags":["述职"]}'}]})), **kw)
    monkeypatch.setattr('app.tag.run_tags', tag)
    assert e.process_pending(store) == 2
    result = e.status(store, run.enrichment_run_id)
    assert (result['status'], result['total'], result['done'], result['failed']) == ('done', 2, 2, 0)
    assert result['records'][0]['tags'] == ['述职']
    assert result['records'][0]['hotword'] == 'done'


def test_existing_swallowed_model_failures_are_not_success(queued, monkeypatch):
    store, _, run = queued
    configured(monkeypatch)
    monkeypatch.setattr('app.tag.run_tags', lambda *a, **kw: {'failed': 1, 'error': 'safe', 'succeeded': 0})
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda *a, **kw: [{'request_failed': True, 'saved': True}])
    e.process_pending(store)
    assert all(t['status'] == 'pending' and t['attempts'] == 1 for t in tasks(store))
    assert e.status(store, run.enrichment_run_id)['status'] == 'running'


def test_empty_hotword_decisions_finish_task_without_retry(queued, monkeypatch):
    store, _, run = queued
    configured(monkeypatch)
    monkeypatch.setattr('app.tag.run_tags', lambda *a, **kw: {'error': None, 'failed': 0, 'succeeded': 1})
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda *a, **kw: [
        {'request_failed': False, 'saved': False}])

    assert e.process_pending(store) == 2

    result = e.status(store, run.enrichment_run_id)
    assert (result['status'], result['done'], result['failed']) == ('done', 2, 0)
    hotword = next(t for t in tasks(store) if t['kind'] == 'hotword')
    assert (hotword['status'], hotword['attempts'], hotword['last_error']) == ('done', 0, None)
    assert e.process_pending(store) == 0


@pytest.mark.parametrize('result', [
    [],
    [{'request_failed': False, 'saved': False}] * 2,
    [{'request_failed': True, 'saved': False}],
    [{'request_failed': True, 'saved': True}],
])
def test_hotword_request_failure_or_wrong_result_count_still_raises(queued, monkeypatch, result):
    store, _, _ = queued
    configured(monkeypatch)
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda *a, **kw: result)
    with pytest.raises(RuntimeError, match='^hotword_failed$'):
        e._execute(store, {'kind': 'hotword', 'recording_id': 'enhance'})


def test_three_failures_are_safe_and_retry_resets_budget(queued, monkeypatch):
    store, _, run = queued
    def fail(*_):
        raise RuntimeError('Bearer secret-key https://user:password@private.invalid raw prompt')
    monkeypatch.setattr(e, '_execute', fail)
    for attempt in range(1, 4):
        assert e.process_pending(store) == 2
        assert {t['attempts'] for t in tasks(store)} == {attempt}
    result = e.status(store, run.enrichment_run_id)
    assert result['status'] == 'failed' and result['failed'] == 2
    assert all(t['status'] == 'failed' and t['last_error'] == '分析失败，请重试' for t in tasks(store))
    assert not any(word in json.dumps(result) for word in ['Bearer', 'secret-key', 'password', 'private.invalid', 'prompt'])
    assert e.process_pending(store) == 0
    assert e.retry_failed(store, 'other') == 0
    assert e.retry_failed(store, 'enhance') == 2
    assert {t['attempts'] for t in tasks(store)} == {0}
    assert e.status(store, run.enrichment_run_id)['status'] == 'pending'


def test_no_model_is_terminal_and_not_retryable(queued, monkeypatch):
    store, _, run = queued
    # Exercise the real resolver with neither a database default nor env/file.
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)
    assert e.process_pending(store) == 2
    assert {t['status'] for t in tasks(store)} == {'failed'}
    assert {t['last_error'] for t in tasks(store)} == {e.NO_MODEL}
    assert e.retry_failed(store) == e.process_pending(store) == 0
    assert not e.status(store, run.enrichment_run_id)['records'][0]['hotword_retryable']
    assert not e.status(store, run.enrichment_run_id)['records'][0]['tag_retryable']


@pytest.mark.parametrize('fallback', ['process', 'file'])
def test_empty_models_worker_uses_shared_fallback_and_persists_both_kinds(queued, monkeypatch, tmp_path, fallback):
    from app import models
    store, _, run = queued
    values = {'ANTHROPIC_AUTH_TOKEN': 'offline-fallback-key',
              'ANTHROPIC_BASE_URL': 'https://fallback.invalid/v1', 'ANTHROPIC_MODEL': 'fallback-model'}
    if fallback == 'process':
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    else:
        path = tmp_path / 'model.env'
        path.write_text('\n'.join(f'{key}={value}' for key, value in values.items()))
        monkeypatch.setattr(models, 'CLAUDE_ENV_PATH', path)
        monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)
    store.add_hotword('标准词')
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM models').fetchone()[0] == 0
    seen = []
    def respond(request):
        body = json.loads(request.content)
        assert request.headers['x-api-key'] == values['ANTHROPIC_AUTH_TOKEN']
        assert str(request.url) == 'https://fallback.invalid/v1/messages'
        assert body['model'] == 'fallback-model'
        seen.append(body['max_tokens'])
        answer = '{"tags":["述职"]}' if body['max_tokens'] == 256 else '{"replacements":[]}'
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': answer}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(**{**kw, 'transport': httpx.MockTransport(respond)}))
    assert e.process_pending(store) == 2
    result = e.status(store, run.enrichment_run_id)
    assert (result['status'], result['total'], result['done'], result['failed']) == ('done', 2, 2, 0)
    assert result['records'][0]['tags'] == ['述职']
    assert {t['status'] for t in tasks(store)} == {'done'}
    assert store.hotword_checks('enhance')
    assert 256 in seen and 2048 in seen


def test_enqueue_requeue_and_claim_without_sqlite_returning(queued, monkeypatch):
    store, _, first = queued
    original_ids = [t['id'] for t in tasks(store)]
    statements = []
    class LegacyConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append(sql)
            if 'RETURNING' in sql.upper():
                raise sqlite3.OperationalError('near "RETURNING": syntax error')
            return super().execute(sql, *args)
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=LegacyConnection))
    run = e.enqueue(store, ['enhance'])
    assert [t['id'] for t in tasks(store)] == original_ids
    assert run != first.enrichment_run_id
    monkeypatch.setattr(e, '_execute', lambda *_: None)
    assert e.process_pending(store) == 2
    assert e.status(store, run)['status'] == 'done'
    assert e.status(store, first.enrichment_run_id)['status'] == 'done'
    assert e._claim(store, original_ids[0]) is None
    assert statements and all('RETURNING' not in sql.upper() for sql in statements)


def test_timeout_recovery_preserves_fresh_lease(queued):
    store, _, _ = queued
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE enrichment_tasks SET status='running',updated_at=? WHERE kind='tag'", (old,))
        conn.execute("UPDATE enrichment_tasks SET status='running' WHERE kind='hotword'")
    assert e.recover_running(SQLiteStore(store.db_path)) == 1
    assert {t['kind']: t['status'] for t in tasks(store)} == {'tag': 'pending', 'hotword': 'running'}


def test_tick_recovers_a_restarted_store(queued, monkeypatch):
    from app.automation import AutomationSettings
    from app.scheduler_tick import tick
    store, _, run = queued
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, tag_enabled=False))
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE enrichment_tasks SET status='running',updated_at=?", (old,))
    monkeypatch.setattr(e, '_execute', lambda *_: None)
    result = tick(SQLiteStore(store.db_path))
    assert 'enrichment' in result['executed']
    assert e.status(store, run.enrichment_run_id)['status'] == 'done'
    assert 'enrichment' not in tick(store)['executed']


def test_duplicate_enqueue_reuses_unique_tasks_and_links_runs(queued):
    store, _, run = queued
    with store._connect() as conn:
        conn.execute("UPDATE enrichment_tasks SET status='done'")
    second = e.enqueue(store, ['enhance', 'enhance'])
    assert len(tasks(store)) == 2
    assert {t['status'] for t in tasks(store)} == {'pending'}
    assert e.status(store, second)['total'] == e.status(store, run.enrichment_run_id)['total'] == 2
    store.migrate(); store.migrate()
    assert len(tasks(store)) == 2


def test_failed_sync_creates_no_run_or_tasks(store):
    store.migrate()
    source = FakeMinutesSource()
    source.list_error = RuntimeError('private')
    run = run_sync(source, store, backfill_days=1)
    assert run.status == 'failed' and run.enrichment_run_id is None
    assert tasks(store) == []
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM enrichment_runs').fetchone()[0] == 0


def test_zero_task_run_finishes(queued):
    store, _, _ = queued
    run = e.enqueue(store, [])
    assert e.status(store, run)['status'] == 'pending'
    e.process_pending(store)
    assert e.status(store, run)['status'] == 'done'


def test_partial_counts(queued, monkeypatch):
    store, _, run = queued
    monkeypatch.setattr(e, '_execute', lambda _, task: e.NO_MODEL if task['kind'] == 'tag' else None)
    e.process_pending(store)
    status = e.status(store, run.enrichment_run_id)
    assert (status['status'], status['total'], status['done'], status['failed']) == ('partial', 2, 1, 1)


def test_atomic_claim_and_global_two_slots(queued, monkeypatch):
    store, _, _ = queued
    for i in range(10):
        rid = f'many-{i}'
        store.upsert_recording(RecordingMeta(rid, rid, datetime.now(SHANGHAI), 1),
                               RecordingDetail(rid, '正文', '', True, False), e.now())
    e.enqueue(store, [f'many-{i}' for i in range(10)])
    mutex = Lock()
    seen = []
    active = peak = 0
    def work(_, task):
        nonlocal active, peak
        with mutex:
            active += 1; peak = max(peak, active); seen.append(task['id'])
        time.sleep(.02)
        with mutex:
            active -= 1
    monkeypatch.setattr(e, '_execute', work)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: e.process_pending(SQLiteStore(store.db_path)), range(4)))
    e.process_pending(store)
    assert len(seen) == len(set(seen)) == 22
    assert peak == 2


def test_reenqueue_invalidates_old_completion(queued, monkeypatch):
    store, _, run = queued
    def work(_, task):
        if task['kind'] == 'tag':
            e.enqueue(store, ['enhance'], hotword_ids=[])
    monkeypatch.setattr(e, '_execute', work)
    e.process_pending(store)
    assert e.record_state(store, 'enhance')['tag'] == 'pending'
    assert e.status(store, run.enrichment_run_id)['status'] != 'done'


def test_background_thread_returns_while_ai_is_blocked(queued, monkeypatch):
    store, _, run = queued
    entered, release = Event(), Event()
    def work(*_):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(e, '_execute', work)
    thread = REAL_BACKGROUND(store)
    try:
        assert entered.wait(2)
        assert thread.is_alive()
        assert e.status(store, run.enrichment_run_id)['status'] == 'running'
    finally:
        release.set(); thread.join(5)
    assert e.status(store, run.enrichment_run_id)['status'] == 'done'


def test_status_auth_and_shape(client, store, queued, settings):
    _, _, run = queued
    url = f'/api/enrichment/status?run={run.enrichment_run_id}'
    assert client.get(url).status_code == 401
    assert login(client).status_code == 302
    response = client.get(url)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert (body['run'], body['status'], body['total'], body['done'], body['failed']) == (run.enrichment_run_id, 'pending', 2, 0, 0)
    assert body['records'][0]['recording_id'] == 'enhance'
    assert body['records'][0]['tag'] == body['records'][0]['hotword'] == 'pending'
    assert client.get('/api/enrichment/status?run=99999').status_code == 404
    assert client.get('/api/enrichment/status?run=bad').status_code == 422
    store.remove_user(settings.admin_open_id)
    assert client.get(url).status_code == 403


def test_retry_csrf_admin_and_pending(client, queued, store, settings, monkeypatch):
    _, _, run = queued
    assert client.post('/api/enrichment/retry').status_code == 401
    login(client)
    monkeypatch.setattr(e, '_execute', lambda *_: e.NO_MODEL)
    with store._connect() as conn:
        conn.execute("UPDATE enrichment_tasks SET status='failed',attempts=3,last_error='分析失败，请重试'")
    assert client.post('/api/enrichment/retry', data={'csrf_token': 'bad'}).status_code == 403
    html = client.get('/recordings/enhance').text
    import re
    token = re.search(r'data-csrf="([^"]+)"', html)[1]
    response = client.post('/api/enrichment/retry', data={'csrf_token': token, 'recording_id': 'enhance'})
    assert response.status_code == 200 and response.json()['pending'] == 2
    assert run.enrichment_run_id in response.json()['runs']
    assert {t['status'] for t in tasks(store)} == {'pending'}
    # A token bound to the admin session cannot be used by another logged-in user.
    person = FeishuUser('ordinary', '普通用户', '')
    store.add_user(person)
    session = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE)
    client.cookies.set('session', session.encode(person.subject), domain='testserver.local', path='/')
    assert client.post('/api/enrichment/retry', data={'csrf_token': token}).status_code == 403


@pytest.mark.parametrize('state', ['pending', 'running', 'failed', 'done'])
def test_reopened_pages_use_persisted_state(client, queued, store, state):
    login(client)
    with store._connect() as conn:
        conn.execute('UPDATE enrichment_tasks SET status=?,last_error=?', (state, '分析失败，请重试' if state == 'failed' else None))
    home = client.get('/').text
    detail = client.get('/recordings/enhance').text
    if state in ('pending', 'running'):
        assert '标签生成中…' in home and '热词分析中…' in detail
        assert '<span class="tag-pill enrichment-pending">标签生成中…</span>' in home
    elif state == 'failed':
        assert '分析失败 · 重试' in home and 'data-enrichment-retry="enhance"' in detail
        assert '<span class="tag-pill enrichment-failed">' in home
    else:
        assert '标签生成中…' not in home and '热词分析中…' not in detail


def test_missing_model_page_has_no_retry_button(client, queued, store):
    e.process_pending(store)
    login(client)
    for url in ('/', '/recordings/enhance'):
        html = client.get(url).text
        assert e.NO_MODEL in html
        assert 'data-enrichment-retry=' not in html


def test_tick_has_ten_minute_schedule(queued):
    from app.scheduler_tick import scheduled_time
    store, _, _ = queued
    at = datetime.now(SHANGHAI)
    assert scheduled_time('enrichment', store.get_settings(), at, None) == at
    assert scheduled_time('enrichment', store.get_settings(), at, at) == at + timedelta(minutes=10)


def test_tick_finishes_empty_run_after_restart(queued):
    from app.automation import AutomationSettings
    from app.scheduler_tick import tick
    store, _, _ = queued
    e.process_pending(store)
    empty = e.enqueue(store, [])
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, tag_enabled=False))
    assert 'enrichment' in tick(SQLiteStore(store.db_path))['executed']
    assert e.status(store, empty)['status'] == 'done'
