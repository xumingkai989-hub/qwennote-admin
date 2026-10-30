"""Offline incremental windows, detail-call budgets and full-sync recovery."""
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import OVERLAP, SyncRun, run_sync
from fakes import FakeMinutesSource
from test_login import login

NOW = datetime(2026, 10, 30, 12, tzinfo=SHANGHAI)


class WindowSource(FakeMinutesSource):
    """Return deliberately overinclusive/unsorted pages to exercise skip safety.

    Requested windows are recorded separately: an upstream list can include older
    entries, and those must not cause unnecessary detail requests or writes.
    """
    def __init__(self):
        super().__init__()
        self.windows = []

    def sync_range(self, start, end, *, early_stop=False):
        self.windows.append((start, end, early_stop))
        return self.recordings


def add(source, ident, days_ago=10, **detail):
    meta = RecordingMeta(ident, ident, NOW - timedelta(days=days_ago), 10)
    source.recordings.append(meta)
    source.details[ident] = replace(RecordingDetail(ident, '总结', '转写', True, True), **detail)
    return meta


def successful_run(store, at=NOW):
    ident = store.start_sync(at.isoformat())
    store.finish_sync(SyncRun(ident, at.isoformat(), at.isoformat(), 'success'))


def recordings(store):
    with store._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM recordings ORDER BY recording_id')]


@pytest.fixture
def incremental(store, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr('app.sync.datetime', Clock)
    store.migrate()
    return WindowSource(), store


def test_first_sync_backfills_even_if_records_already_exist_without_success(incremental):
    source, store = incremental
    old = add(source, 'old')
    add(source, 'new', 0)
    store.upsert_recording(old, source.details['old'], 'previous stamp')
    result = run_sync(source, store, backfill_days=90)
    assert source.windows == [(NOW - timedelta(days=90), NOW, False)]
    assert source.detail_calls == ['new', 'old']
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (2, 1, 0, 0)


def test_second_sync_shrinks_window_and_skips_complete_records_without_any_writes(incremental):
    source, store = incremental
    add(source, 'old')
    add(source, 'older', 20)
    run_sync(source, store)
    before = recordings(store)
    source.detail_calls.clear()
    # Changes in list/detail data must not leak through the skip path.
    source.recordings[0] = replace(source.recordings[0], title='未检查的新标题')
    source.details['old'] = replace(source.details['old'], summary_md='未检查的总结')
    result = run_sync(source, store)
    assert source.windows == [(NOW - timedelta(days=90), NOW, False), (NOW - OVERLAP, NOW, True)]
    assert source.detail_calls == []
    assert recordings(store) == before  # Includes synced_at and all original text.
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (0, 0, 0, 0)
    assert store.latest_sync() == result
    assert result.result()['skipped'] == 0


def test_only_new_record_needs_details(incremental):
    source, store = incremental
    add(source, 'old')
    run_sync(source, store)
    source.detail_calls.clear()
    add(source, 'new', 0)
    result = run_sync(source, store)
    assert source.detail_calls == ['new']
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (1, 1, 0, 0)


@pytest.mark.parametrize('missing', ['has_transcript', 'has_summary'])
def test_incomplete_old_record_is_refetched_and_updated(incremental, missing):
    source, store = incremental
    add(source, 'late', days_ago=5 / 1440, **{missing: False})
    add(source, 'complete', days_ago=6 / 1440)
    run_sync(source, store)
    source.detail_calls.clear()
    source.details['late'] = replace(source.details['late'], **{missing: True})
    result = run_sync(source, store)
    assert source.detail_calls == ['late']
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (2, 0, 1, 1)
    assert getattr(store.get_recording('late'), missing)


@pytest.mark.parametrize('offset,expected_calls', [
    (OVERLAP + timedelta(microseconds=1), []),
    (OVERLAP, []),
    (timedelta(minutes=1), []),
])
def test_complete_records_are_skipped_including_exact_boundary(incremental, offset, expected_calls):
    source, store = incremental
    meta = add(source, 'recent')
    source.recordings[0] = replace(meta, created_at=NOW - offset)
    run_sync(source, store)
    source.detail_calls.clear()
    run_sync(source, store)
    assert source.detail_calls == expected_calls


def test_full_sync_bypasses_window_and_skip_rules(incremental):
    source, store = incremental
    add(source, 'old')
    add(source, 'older', 20)
    run_sync(source, store)
    source.detail_calls.clear()
    source.details['old'] = replace(source.details['old'], summary_md='修复总结')
    result = run_sync(source, store, full=True, backfill_days=60)
    assert [w[0] for w in source.windows] == [NOW - timedelta(days=90), NOW - timedelta(days=60)]
    assert source.detail_calls == ['old', 'older']
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (2, 0, 1, 0)


def test_newest_first_across_unsorted_pages_and_duplicates(incremental, monkeypatch):
    source, store = incremental
    add(source, 'old', 20)
    add(source, 'new', 0)
    add(source, 'middle', 5)
    source.recordings.append(source.recordings[0])
    writes = []
    original = store.upsert_recording

    def upsert(meta, *args):
        writes.append(meta.recording_id)
        return original(meta, *args)

    monkeypatch.setattr(store, 'upsert_recording', upsert)
    result = run_sync(source, store)
    assert source.detail_calls == writes == ['new', 'middle', 'old']
    assert (result.fetched, result.inserted, result.updated, result.skipped) == (3, 3, 0, 0)


@pytest.mark.parametrize('elapsed,backfill,expected', [
    (0, 90, 10 / 1440), (3, 90, 3 + 10 / 1440), (100, 90, 90), (0, 1, 10 / 1440), (-1, 90, 90),
])
def test_window_uses_latest_success_started_at_and_ignores_newer_failures(incremental, elapsed, backfill, expected):
    source, store = incremental
    successful_run(store, NOW - timedelta(days=200))
    successful_run(store, NOW - timedelta(days=elapsed))
    ident = store.start_sync(NOW.isoformat())
    store.finish_sync(SyncRun(ident, NOW.isoformat(), NOW.isoformat(), 'failed'))
    result = run_sync(source, store, backfill_days=backfill)
    assert result.status == 'success'
    assert source.windows[0][0] == NOW - timedelta(days=expected)


def test_watermark_query_failure_falls_back_to_full_without_skipping(incremental, monkeypatch):
    source, store = incremental
    add(source, 'old')
    run_sync(source, store)
    source.detail_calls.clear()

    def broken():
        raise sqlite3.OperationalError('private provider error')

    monkeypatch.setattr(store, 'latest_successful_sync', broken)
    result = run_sync(source, store)
    assert result.status == 'success' and result.error is None
    assert [w[0] for w in source.windows] == [NOW - timedelta(days=90)] * 2
    assert source.detail_calls == ['old'] and result.skipped == 0


@pytest.mark.parametrize('started_at', ['invalid', '2026-10-30T12:00:00', '2026-10-31T12:00:00+08:00'])
def test_invalid_watermark_falls_back_to_full(incremental, started_at):
    source, store = incremental
    with store._connect() as conn:
        conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'success')", (started_at,))
    assert run_sync(source, store).status == 'success'
    assert source.windows == [(NOW - timedelta(days=90), NOW, False)]


def test_date_source_shrinks_window_and_sorts_within_each_day(incremental):
    _, store = incremental
    source = FakeMinutesSource()
    older = add(source, 'morning', 0)
    source.recordings[0] = replace(older, created_at=NOW - timedelta(hours=1))
    add(source, 'noon', 0)
    add(source, 'yesterday', 1)
    run_sync(source, store, backfill_days=7)
    assert len(source.list_calls) == 8
    assert source.detail_calls == ['noon', 'morning', 'yesterday']
    source.list_calls.clear()
    source.detail_calls.clear()
    run_sync(source, store, backfill_days=7)
    assert source.list_calls == [NOW.date()]
    assert source.detail_calls == []


def test_skipped_migration_is_idempotent_and_preserves_old_runs(store):
    store.db_path.parent.mkdir(parents=True)
    with store._connect() as conn:
        conn.execute('''CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT, fetched INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0, error TEXT)''')
        conn.execute("INSERT INTO sync_runs (started_at, status, fetched) VALUES (?, 'success', 12)", (NOW.isoformat(),))
    store.migrate()
    store.migrate()
    run = store.latest_successful_sync()
    assert run.mode == 'quick' and run.skipped == 0 and run.fetched == 12 and run.started_at == NOW.isoformat()


def test_http_full_sync_uses_saved_backfill_and_pulls_complete_history(client, store):
    login(client)
    source = WindowSource()
    source.recordings.append(RecordingMeta('old', 'old', datetime.now(SHANGHAI), 10))
    source.details['old'] = RecordingDetail('old', '总结', '转写', True, True)
    client.app.state.minutes_source = source
    # The first run has no successful watermark; the next is incremental.
    assert client.post('/sync').json()['inserted'] == 1
    source.detail_calls.clear()
    assert client.post('/sync').json()['skipped'] == 1
    assert source.detail_calls == []
    store.save_settings(replace(store.get_settings(), backfill_days=30))
    result = client.post('/sync?full=1')
    assert result.status_code == 200 and result.json()['skipped'] == 0
    assert source.detail_calls == ['old'] and len(source.windows) == 3
    assert '全盘同步' in client.get('/').text
    assert '用于补历史或修复漏数据，耗时较长' in client.get('/').text
    for page in ['/', '/settings']:
        assert '检查 1 条 · 新增 0 · 更新 0 · 跳过 0' in client.get(page).text


def test_full_sync_shares_auth_and_origin_protection(client, store):
    assert client.post('/sync?full=1').status_code == 401
    login(client)
    assert client.post('/sync?full=1', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    with store._connect() as conn:
        conn.execute('UPDATE users SET is_active=0')
    assert client.post('/sync?full=1').status_code == 403
    assert store.latest_sync() is None


def test_outside_watermark_even_incomplete_history_is_untouched(incremental):
    source, store = incremental
    old = add(source, 'old', has_summary=False)
    run_sync(source, store)
    before = recordings(store)
    source.detail_calls.clear()
    source.details['old'] = replace(source.details['old'], has_summary=True)
    result = run_sync(source, store)
    assert result.mode == 'quick' and result.fetched == 0
    assert source.detail_calls == [] and recordings(store) == before


def test_new_records_at_exact_watermark_boundary(incremental):
    source, store = incremental
    successful_run(store)
    for ident, offset in [('before', OVERLAP + timedelta(microseconds=1)),
                          ('boundary', OVERLAP), ('after', timedelta(minutes=1)),
                          ('future', -timedelta(seconds=1))]:
        add(source, ident, offset.total_seconds() / 86400)
    result = run_sync(source, store)
    assert source.detail_calls == ['after', 'boundary']
    assert result.mode == store.latest_sync().mode == 'quick'


def test_full_and_fallback_mode_persisted(incremental):
    source, store = incremental
    assert run_sync(source, store).mode == store.latest_sync().mode == 'full'
    assert run_sync(source, store).mode == store.latest_sync().mode == 'quick'
    assert run_sync(source, store, full=True).mode == store.latest_sync().mode == 'full'
