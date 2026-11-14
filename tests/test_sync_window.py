"""T9: normal window fetches bypass backoff; detail cooldown is process-local."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import run_sync
from test_sync_backfill import queue, state, NOW
from test_login import login


def window_record(queue, age=timedelta(minutes=1), complete=False):
    source, store, clock = queue
    meta = RecordingMeta('window', '窗口测试', NOW - age, 60)
    detail = RecordingDetail('window', '总结', '转写' if complete else None, complete, True)
    store.upsert_recording(meta, detail, NOW.isoformat())
    store.finish_backfill('window', NOW, status='pending_upstream')
    source.recordings = [meta]
    source.details['window'] = replace(detail, transcript_md='完整转写', has_transcript=True)
    source.get_detail = Mock(wraps=source.get_detail)
    return source, store, clock


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('age', [timedelta(minutes=1), timedelta(minutes=10)])
def test_in_window_incomplete_bypasses_future_backoff(queue, full, age):
    source, store, _ = window_record(queue, age)
    assert 'window' not in {m.recording_id for m in store.backfill_candidates(NOW, 10)}
    result = run_sync(source, store, full=full)
    assert result.status == 'success'
    assert 'window' in source.detail_calls
    assert store.get_recording('window').has_transcript
    assert state(store, 'window') is None


def test_outside_window_incomplete_preserves_future_backoff(queue):
    source, store, _ = window_record(queue, timedelta(minutes=11))
    before = state(store, 'window')
    result = run_sync(source, store)
    assert 'window' not in source.detail_calls and result.skipped == 1
    assert state(store, 'window') == before


def test_in_window_complete_quick_skips(queue):
    source, store, _ = window_record(queue, complete=True)
    run_sync(source, store)
    assert 'window' not in source.detail_calls


def test_detail_cooldown_expires_at_60_seconds(queue):
    source, store, clock = window_record(queue)
    source.details['window'] = RecordingDetail('window', None, None, False, False)
    run_sync(source, store)
    before = state(store, 'window')
    first_count = source.get_detail.call_count
    clock.current += timedelta(seconds=59)
    run_sync(source, store)
    assert source.get_detail.call_count == first_count + 1  # Fourth old queued row only.
    assert source.detail_calls.count('window') == 1
    assert state(store, 'window') == before
    clock.current += timedelta(seconds=1)
    run_sync(source, store)
    assert source.detail_calls.count('window') == 2
    assert state(store, 'window')['last_attempt_at'] == clock.current.isoformat()


def test_failed_window_attempt_updates_timestamp_and_is_throttled(queue):
    source, store, clock = window_record(queue)
    clock.current += timedelta(seconds=60)
    source.detail_errors['window'] = ValueError('bad response')
    run_sync(source, store)
    assert state(store, 'window')['last_attempt_at'] == clock.current.isoformat()
    assert state(store, 'window')['last_status'] == 'bad_response'
    run_sync(source, store)
    assert source.detail_calls.count('window') == 1


@pytest.mark.parametrize('url', ['/', '/search?q=窗口测试', '/recordings/attempt'])
@pytest.mark.parametrize('offset', [SHANGHAI, timezone.utc])
def test_backfill_attempt_time_display(client, store, url, offset):
    login(client)
    now = datetime.now(SHANGHAI)
    meta = RecordingMeta('attempt', '窗口测试', now, 60)
    detail = RecordingDetail('attempt', None, None, False, False)
    store.upsert_recording(meta, detail, now.isoformat())
    assert '最近尝试' not in client.get(url).text
    attempted = now.replace(hour=14, minute=52, second=0, microsecond=0).astimezone(offset)
    store.finish_backfill('attempt', attempted, status='pending_upstream')
    html = client.get(url).text
    assert '转写生成中' in html and '最近尝试 14:52' in html
    store.upsert_recording(meta, replace(detail, has_transcript=True, transcript_md='完成'), now.isoformat())
    assert '最近尝试' not in client.get(url).text


def test_serial_fallback_records_actual_start_time(queue, monkeypatch):
    from app.minutes import MinutesRetryableError
    from app import sync
    source, store, clock = queue
    # Four fresh window records; first two exhausted 429s trigger serial fallback.
    source.recordings = []
    for i in range(4):
        meta = RecordingMeta('serial-' + str(i), '', NOW - timedelta(seconds=i), 1)
        source.recordings.append(meta)
        store.upsert_recording(meta, RecordingDetail(meta.recording_id, None, None, False, False), NOW.isoformat())
    starts = {}

    def fetch(source, meta):
        starts[meta.recording_id] = sync.time.monotonic()
        if meta.recording_id in {'serial-0', 'serial-1'}:
            return None, MinutesRetryableError(429)
        if meta.recording_id == 'serial-2':
            clock.current += timedelta(seconds=90)
        return RecordingDetail(meta.recording_id, None, None, False, False), None

    monkeypatch.setattr(sync, '_fetch_detail', fetch)
    assert run_sync(source, store).detail_serialized == 1
    assert sync._last_detail_attempt['serial-3'] == starts['serial-3']
    assert starts['serial-3'] - starts['serial-2'] == 90
    assert not sync._claim_detail_attempt('serial-3')
