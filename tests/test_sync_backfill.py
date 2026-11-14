"""T6: persistent bounded recovery, status-only diagnostics and safe early stop."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from collections import Counter

import httpx
import pytest

from app.minutes import HttpMinutesSource, MinutesRetryableError, RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import run_sync, SyncRun, _fetch_detail
from test_mcp import setup_source
from test_login import login
from fakes import FakeMinutesSource

NOW = datetime(2026, 11, 6, 12, tzinfo=SHANGHAI)


@pytest.fixture
def queue(store, monkeypatch):
    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr('app.sync.datetime', Clock)
    monkeypatch.setattr('app.sync.time.monotonic', lambda: Clock.current.timestamp())
    monkeypatch.setattr('app.sync.time.sleep', lambda _: None)
    store.migrate()
    rid = store.start_sync(NOW.isoformat())
    store.finish_sync(SyncRun(rid, NOW.isoformat(), NOW.isoformat(), 'success'))
    source = FakeMinutesSource()
    for i in range(4):
        meta = RecordingMeta(str(i), f'会议{i}', NOW - timedelta(days=150+i), 5, 'AUDIO')
        store.upsert_recording(meta, RecordingDetail(str(i), None, None, False, False), NOW.isoformat())
        source.details[str(i)] = RecordingDetail(str(i), '总结', '转写', True, True)
    return source, store, Clock


def state(store, rid='0'):
    with store._connect() as conn:
        row = conn.execute('SELECT * FROM backfill_state WHERE recording_id=?', (rid,)).fetchone()
        return dict(row) if row else None


@pytest.mark.parametrize('full', [False, True])
def test_three_old_incomplete_rows_then_fourth_next_round(queue, full):
    source, store, _ = queue
    result = run_sync(source, store, full=full)
    assert result.updated == 3 and result.note == 'backfilled=3'
    assert source.detail_calls == ['0', '1', '2']
    assert result.mode == ('full' if full else 'quick')
    assert store.get_recording('3').has_transcript is False
    source.detail_calls.clear()
    result = run_sync(source, store, full=full)
    assert source.detail_calls == ['3'] and result.updated == 1 and result.note == 'backfilled=1'
    assert store.latest_sync() == result


@pytest.mark.parametrize('kind', ['incomplete', 'exception', '429', '500', 'timeout'])
def test_backoff_first_third_fifth_and_daily_retry(queue, kind):
    source, store, clock = queue
    if kind == 'incomplete':
        source.details['0'] = RecordingDetail('0', None, None, False, False)
    else:
        source.detail_errors['0'] = (MinutesRetryableError(int(kind)) if kind in ('429', '500')
                                     else TimeoutError() if kind == 'timeout' else ValueError('private body'))
    first_delay = 1440 if kind == 'incomplete' else 360 if kind == 'exception' else 30
    for attempt, delay in enumerate([first_delay] * 4 + [1440, 1440], 1):
        run_sync(source, store)
        saved = state(store)
        assert saved['attempts'] == (0 if kind == 'incomplete' else attempt)
        assert datetime.fromisoformat(saved['next_attempt_at']) == clock.current + timedelta(minutes=delay)
        assert saved['last_status'] == ('no_upstream_content' if kind == 'incomplete' else
                                        'rate_limited' if kind == '429' else
                                        'upstream_error' if kind in ('500', 'timeout') else 'bad_response')
        assert saved['last_http_status'] == (int(kind) if kind in ('429', '500') else None)
        assert 'private' not in str(saved)
        source.detail_calls.clear()
        # Repeated quick AND full sync cannot bypass queue backoff.
        for full in (False, True):
            run_sync(source, store, full=full)
            assert '0' not in source.detail_calls
            assert state(store)['attempts'] == (0 if kind == 'incomplete' else attempt)
        clock.current += timedelta(minutes=delay)


def test_success_removes_state_and_counts_update(queue):
    source, store, clock = queue
    source.detail_errors['0'] = MinutesRetryableError(500)
    run_sync(source, store)
    assert state(store)['last_status'] == 'upstream_error'
    source.detail_errors.clear()
    clock.current += timedelta(minutes=30)
    result = run_sync(source, store)
    assert result.updated == 2 and result.note == 'backfilled=2'
    assert state(store) is None and store.get_recording('0').has_summary


@pytest.mark.parametrize('full', [False, True])
def test_outside_window_list_duplicates_cannot_bypass_cap_or_backoff(queue, full):
    source, store, clock = queue
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET created_at=?', ((NOW-timedelta(days=100)).isoformat(),))
    source.recordings = store.backfill_candidates(NOW, 10) * 2
    source.detail_errors = {str(i): ValueError('private') for i in range(4)}
    run_sync(source, store, full=full)
    assert len(source.detail_calls) == 3 and len(set(source.detail_calls)) == 3
    tried = set(source.detail_calls)
    source.detail_calls.clear()
    run_sync(source, store, full=full)
    assert len(source.detail_calls) == 1 and not tried.intersection(source.detail_calls)
    source.detail_calls.clear()
    run_sync(source, store, full=full)
    assert source.detail_calls == []


def test_list_failure_does_not_block_queue(queue):
    source, store, _ = queue
    source.list_error = MinutesRetryableError(503)
    result = run_sync(source, store)
    assert result.status == 'failed' and result.updated == 3
    assert result.note == 'backfilled=3' and result.http_status == '503×1'


def test_wrong_detail_identity_is_backed_off(queue):
    source, store, _ = queue
    source.details['0'] = replace(source.details['0'], recording_id='wrong')
    result = run_sync(source, store)
    assert result.status == 'failed' and result.updated == 2
    assert state(store)['attempts'] == 1 and state(store)['last_status'] == 'bad_response'
    assert store.get_recording('wrong') is None


def test_due_timestamp_compares_instants_across_offsets(queue):
    _, store, _ = queue
    store.finish_backfill('0', NOW - timedelta(minutes=10), status='bad_response')
    with store._connect() as conn:
        conn.execute('UPDATE backfill_state SET next_attempt_at=?', (NOW.astimezone(timezone.utc).isoformat(),))
    assert store.backfill_candidates(NOW, 1)[0].recording_id == '0'


@pytest.mark.parametrize('missing', ['has_summary', 'has_transcript'])
def test_only_missing_transcript_queues(queue, missing):
    _, store, _ = queue
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET has_summary=1, has_transcript=1')
        conn.execute(f'UPDATE recordings SET {missing}=0 WHERE recording_id="0"')
    assert [m.recording_id for m in store.backfill_candidates(NOW, 10)] == (['0'] if missing == 'has_transcript' else [])
    assert store.complete_recording_ids() == {'1', '2', '3'}


@pytest.mark.parametrize('backend', ['http', 'mcp'])
@pytest.mark.parametrize('code,waits', [(429, [30, 60]), (500, [2, 6]), (503, [2, 6]),
                                      ('timeout', [2, 6]), (403, []), (404, []), (408, [])])
def test_transport_retry_budget_and_status_preservation(setup_source, monkeypatch, backend, code, waits):
    source, _, _ = setup_source
    if backend == 'http':
        source = HttpMinutesSource('test-only')
    source._initialized = True
    calls, sleeps = [], []

    def respond(request):
        calls.append(request)
        if code == 'timeout':
            raise httpx.ReadTimeout('private bearer body', request=request)
        return httpx.Response(code, text='private bearer body')

    source._transport = httpx.MockTransport(respond)
    monkeypatch.setattr('app.sync.time.sleep', sleeps.append)
    detail, error = _fetch_detail(source, RecordingMeta('one', '', NOW, 1))
    assert detail is None and sleeps == waits and len(calls) == len(waits)+1
    assert error.status_code == (None if code == 'timeout' else code)
    assert 'private' not in str(error)


def test_failure_http_aggregation_per_record_and_safe_ui(client, store, monkeypatch):
    monkeypatch.setattr('app.sync.time.sleep', lambda _: None)
    source = FakeMinutesSource()
    now = datetime.now(SHANGHAI) - timedelta(minutes=1)
    source.recordings = [RecordingMeta(str(i), '', now, 1) for i in range(18)]
    source.detail_errors = {str(i): MinutesRetryableError(429 if i < 16 else 500) for i in range(18)}
    result = run_sync(source, store, full=True)
    assert result.http_status == store.latest_sync().http_status == '429×16,500×2'
    assert Counter(source.detail_calls) == Counter({str(i): 3 for i in range(18)})
    login(client)
    assert '失败状态码：429×16,500×2' in client.get('/').text
    source.detail_errors.clear()
    source.recordings.clear()
    run_sync(source, store)
    assert '失败状态码：' not in client.get('/').text
    assert 'result.http_status' in client.get('/static/home.js').text


@pytest.mark.parametrize('complete', [False, True])
def test_old_incomplete_or_missing_records_do_not_stop_paging(setup_source, store, monkeypatch, complete):
    source, _, _ = setup_source
    store.migrate()
    now = datetime.now(SHANGHAI)
    watermark = now - timedelta(minutes=2)
    start = watermark - timedelta(minutes=10)
    rid = store.start_sync(watermark.isoformat())
    store.finish_sync(SyncRun(rid, watermark.isoformat(), watermark.isoformat(), 'success'))
    metas = [RecordingMeta(str(i), str(i), start-timedelta(minutes=i+1), 1) for i in range(3)]
    store.upsert_recording(metas[0], RecordingDetail('0', None, None, complete, complete), now.isoformat())
    store.upsert_recording(metas[2], RecordingDetail('2', 's', 't', True, True), now.isoformat())
    pages, details = [], []

    def page(name, args):
        i = int(args.get('nextToken', 0)); pages.append(i)
        m = metas[i]
        return {'items': [{'uuid': m.recording_id, 'title': m.title,
                           'gmtCreate': int(m.created_at.timestamp()*1000)}],
                'hasMore': i < 2, 'nextToken': str(i+1)}

    def detail(rid, **kw):
        details.append(rid)
        return RecordingDetail(rid, 's', 't', True, True)

    monkeypatch.setattr(source, '_initialize', lambda: None)
    monkeypatch.setattr(source, '_call', page)
    monkeypatch.setattr(source, 'get_detail', detail)
    result = run_sync(source, store)
    assert result.status == 'success'
    assert pages == ([0] if complete else [0, 1, 2])
    assert details == ([] if complete else ['0', '1'])
    assert result.note == ('backfilled=0' if complete else 'backfilled=1')


def test_unknown_local_completeness_disables_early_stop(setup_source, monkeypatch):
    source, _, _ = setup_source
    pages = []
    def page(name, args):
        i = int(args.get('nextToken', 0)); pages.append(i)
        return {'items': [{'uuid': str(i), 'title': '', 'gmtCreate': int((NOW-timedelta(days=2)).timestamp()*1000)}],
                'hasMore': i < 1, 'nextToken': '1'}
    monkeypatch.setattr(source, '_initialize', lambda: None)
    monkeypatch.setattr(source, '_call', page)
    assert source.sync_range(NOW-timedelta(days=1), NOW, early_stop=True) == []
    assert pages == [0, 1]


@pytest.mark.parametrize('flag', ['has_transcript', 'has_summary'])
def test_list_search_and_detail_backfill_markers(client, store, flag):
    login(client)
    meta = RecordingMeta('pending', 'waiting', datetime.now(SHANGHAI), 1)
    detail = replace(RecordingDetail('pending', 'summary', 'transcript', True, True), **{flag: False})
    store.upsert_recording(meta, detail, NOW.isoformat())
    for url in ['/', '/search?q=waiting', '/recordings/pending']:
        html = client.get(url).text
        assert ('转写生成中' in html) == (flag == 'has_transcript')
        assert '无转写内容' not in html
    store.finish_backfill('pending', NOW, status='no_upstream_content')
    for url in ['/', '/search?q=waiting', '/recordings/pending']:
        html = client.get(url).text
        assert ('无转写内容' in html) == (flag == 'has_transcript')
        assert '转写生成中' not in html
    store.upsert_recording(meta, replace(detail, **{flag: True}), NOW.isoformat())
    for url in ['/', '/search?q=waiting', '/recordings/pending']:
        html = client.get(url).text
        assert '转写生成中' not in html and '无转写内容' not in html


def test_migration_preserves_history_and_backoff(queue):
    _, store, _ = queue
    store.finish_backfill('0', NOW, status='rate_limited', http_status=429)
    before = state(store)
    for _ in range(2):
        store.migrate()
    assert state(store) == before
    assert store.latest_sync().http_status is None and store.latest_sync().note is None
    assert len(store.backfill_candidates(NOW, 10)) == 3


@pytest.mark.parametrize('age,status,delay', [
    (timedelta(hours=1), 'pending_upstream', 5),
    (timedelta(hours=24)-timedelta(microseconds=1), 'pending_upstream', 5),
    (timedelta(hours=24), 'no_upstream_content', 1440),
    (timedelta(hours=30), 'no_upstream_content', 1440),
])
def test_successful_empty_response_classifies_age(queue, age, status, delay):
    source, store, _ = queue
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET created_at=? WHERE recording_id="0"',
                     ((NOW-age).astimezone(timezone.utc).isoformat(),))
    source.details['0'] = RecordingDetail('0', None, None, False, False)
    result = run_sync(source, store)
    assert result.status == 'success' and result.http_status is None
    saved = state(store)
    assert saved['last_status'] == status and saved['attempts'] == 0
    assert datetime.fromisoformat(saved['next_attempt_at']) == NOW + timedelta(minutes=delay)
    assert '0' not in {m.recording_id for m in store.backfill_candidates(NOW, 10)}


def test_pending_schedule_survives_restart_then_becomes_empty_and_recovers(queue):
    source, store, clock = queue
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET created_at=? WHERE recording_id="0"',
                     ((NOW-timedelta(hours=1)).isoformat(),))
    source.details['0'] = RecordingDetail('0', None, None, False, False)
    for delay in [5, 15, 60, 60, 60, 60]:
        run_sync(source, store)
        store.migrate()
        saved = state(store)
        assert saved['attempts'] == 0 and saved['last_status'] == 'pending_upstream'
        assert datetime.fromisoformat(saved['next_attempt_at']) == clock.current + timedelta(minutes=delay)
        clock.current += timedelta(minutes=delay)
    clock.current = NOW + timedelta(hours=23)
    run_sync(source, store)
    assert state(store)['last_status'] == 'no_upstream_content'
    assert state(store)['attempts'] == 0
    source.details['0'] = RecordingDetail('0', None, '迟到的转写', True, False)
    clock.current += timedelta(days=1)
    run_sync(source, store)
    assert state(store) is None and store.get_recording('0').has_transcript
    assert store.get_recording('0').backfill_label is None


def test_true_error_budget_does_not_cap_pending(queue):
    _, store, _ = queue
    for _ in range(5):
        store.finish_backfill('0', NOW, status='bad_response')
    store.finish_backfill('0', NOW, status='pending_upstream')
    assert state(store)['attempts'] == 5
    assert datetime.fromisoformat(state(store)['next_attempt_at']) == NOW + timedelta(minutes=5)


def test_migration_resets_legacy_false_failure_once_and_removes_complete(queue):
    _, store, _ = queue
    for rid in ('0', '1', '2', '3'):
        store.finish_backfill(rid, NOW, status='bad_response')
    store.finish_backfill('1', NOW, status='bad_response')
    store.finish_backfill('2', NOW, status='rate_limited', http_status=429)
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET has_transcript=1 WHERE recording_id="3"')
        conn.execute('ALTER TABLE backfill_state DROP COLUMN pending_checks')
    before1, before2 = state(store, '1'), state(store, '2')
    store.migrate()
    reset = state(store)
    assert reset['last_status'] == 'pending_upstream' and reset['attempts'] == 0
    assert reset['next_attempt_at'] is None and reset['pending_checks'] == 0
    assert state(store, '3') is None
    for rid, before in [('1', before1), ('2', before2)]:
        after = state(store, rid)
        after.pop('pending_checks')
        assert after == before
    assert '0' in {m.recording_id for m in store.backfill_candidates(NOW, 10)}
    store.finish_backfill('0', NOW, status='bad_response')
    genuine_failure = state(store)
    store.migrate()
    assert state(store) == genuine_failure
