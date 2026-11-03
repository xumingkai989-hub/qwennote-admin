"""Bounded detail workers, retry budgets, serial fallback and wall-clock metrics."""
from collections import Counter
from datetime import datetime, timedelta
from threading import Barrier, Lock, get_ident
import time

import pytest

from app.minutes import MinutesRetryableError, RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import run_sync
from test_mcp import setup_source
from test_login import login

REAL_SLEEP = time.sleep


class ConcurrentSource:
    def __init__(self, count=10):
        now = datetime.now(SHANGHAI) - timedelta(minutes=1)
        self.records = [RecordingMeta(str(i), f'title-{i}', now - timedelta(seconds=i), 1, 'AUDIO')
                        for i in range(count)]
        self.lock = Lock()
        self.active = self.peak = 0
        self.calls = Counter()
        self.peaks = {}
        self.errors = {}
        self.first_wave = None

    def sync_range(self, *args, **kwargs):
        return self.records

    def get_detail(self, rid, *, meta=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls[rid] += 1
            self.peaks[rid] = self.active
        try:
            if self.first_wave and int(rid) < 3:
                self.first_wave.wait(timeout=3)
            REAL_SLEEP(0.005 * (3 - int(rid) % 3))  # Finish out of order.
            error = self.errors.get(rid)
            if error:
                raise error
            assert meta.recording_id == rid and meta.title == f'title-{rid}'
            return RecordingDetail(rid, f'summary-{rid}', f'transcript-{rid}', True, True, meta.media_type)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def prepared(store, monkeypatch):
    store.migrate()
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda *a, **kw: None)
    monkeypatch.setattr('app.tag.run_tags', lambda *a, **kw: None)
    return store


def test_ten_records_overlap_with_three_workers_and_single_thread_writes(prepared, monkeypatch):
    source = ConcurrentSource()
    source.first_wave = Barrier(3)
    main_thread = get_ident()
    writes = []
    upsert = prepared.upsert_recording

    def write(meta, detail, *args):
        assert get_ident() == main_thread
        assert meta.recording_id == detail.recording_id
        writes.append(meta.recording_id)
        return upsert(meta, detail, *args)

    monkeypatch.setattr(prepared, 'upsert_recording', write)
    run = run_sync(source, prepared)
    assert run.status == 'success' and run.inserted == run.fetched == 10
    assert source.peak == 3 and len(writes) == 10
    for meta in source.records:
        saved = prepared.get_recording(meta.recording_id)
        assert saved.summary_md == f'summary-{meta.recording_id}'
        assert saved.transcript_md == f'transcript-{meta.recording_id}'


def test_one_detail_failure_keeps_two_successes(prepared):
    source = ConcurrentSource(3)
    source.errors['1'] = RuntimeError('Bearer must-not-persist')
    run = run_sync(source, prepared)
    assert run.inserted == 2 and len(run.error.splitlines()) == 1
    assert '1' in run.error and 'must-not-persist' not in run.error
    assert prepared.get_recording('0') and prepared.get_recording('2')
    assert prepared.get_recording('1') is None


@pytest.mark.parametrize('status', [429, 500, 503])
def test_record_retries_twice_then_succeeds(prepared, monkeypatch, status):
    source = ConcurrentSource(1)
    original = source.get_detail
    attempts, sleeps = [], []

    def get(rid, **kwargs):
        attempts.append(rid)
        if len(attempts) < 3:
            raise MinutesRetryableError(status)
        return original(rid, **kwargs)

    monkeypatch.setattr(source, 'get_detail', get)
    monkeypatch.setattr('app.sync.time.sleep', sleeps.append)
    run = run_sync(source, prepared)
    assert run.status == 'success' and run.inserted == 1 and run.error is None
    assert attempts == ['0'] * 3
    assert len(sleeps) == 2 and 1 <= sleeps[0] <= 1.25 and 3 <= sleeps[1] <= 3.25


def test_three_throttled_failures_serialize_remaining_and_persist_report(prepared, monkeypatch, caplog):
    source = ConcurrentSource(10)
    source.first_wave = Barrier(3)
    source.errors = {str(i): MinutesRetryableError(429) for i in range(3)}
    monkeypatch.setattr('app.sync.time.sleep', lambda _: None)
    run = run_sync(source, prepared)
    assert run.inserted == 7 and len(run.error.splitlines()) == 3
    assert run.detail_serialized == prepared.latest_sync().detail_serialized == 1
    assert [source.calls[str(i)] for i in range(3)] == [3] * 3
    assert all(source.peaks[str(i)] == 1 for i in range(3, 10))
    assert '降为串行' in caplog.text


@pytest.mark.parametrize('statuses', [(429, None, 429), (503, 503, 503), (429, 503, 429)])
def test_only_consecutive_throttled_failures_trigger_fallback(prepared, monkeypatch, statuses):
    source = ConcurrentSource(6)
    source.errors = {str(i): MinutesRetryableError(s) for i, s in enumerate(statuses) if s}
    monkeypatch.setattr('app.sync.time.sleep', lambda _: None)
    run = run_sync(source, prepared)
    assert run.detail_serialized == 0
    assert source.peak > 1


def test_duplicates_are_deduped_before_workers_and_full_retry_is_idempotent(prepared):
    source = ConcurrentSource(10)
    source.records += source.records * 2
    first = run_sync(source, prepared, full=True)
    assert (first.fetched, first.inserted) == (10, 10)
    assert source.calls == Counter({str(i): 1 for i in range(10)})
    second = run_sync(source, prepared, full=True)
    assert (second.fetched, second.inserted, second.updated) == (10, 0, 0)
    with prepared._connect() as conn:
        assert conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 10


def test_duration_includes_enrichment_and_is_persisted(prepared, monkeypatch):
    elapsed = [0.0]
    events = []
    monkeypatch.setattr('app.sync.time.perf_counter', lambda: elapsed[0])

    def judge(*args, **kwargs):
        assert prepared.latest_sync().finished_at is not None
        events.append('judge')
        elapsed[0] += 1

    def tags(*args, **kwargs):
        events.append('tags')
        elapsed[0] += 2

    monkeypatch.setattr('app.hotword_judge.run_judgments', judge)
    monkeypatch.setattr('app.tag.run_tags', tags)
    result = run_sync(ConcurrentSource(1), prepared)
    assert events == ['judge', 'tags']
    assert result.duration_ms == prepared.latest_sync().duration_ms == 3000
    assert result.result()['duration_ms'] == 3000


def test_duration_migration_preserves_old_rows_and_is_idempotent(store):
    store.db_path.parent.mkdir(parents=True)
    with store._connect() as conn:
        conn.execute('''CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT, fetched INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0, error TEXT)''')
        conn.execute("INSERT INTO sync_runs (started_at, status, fetched) VALUES ('old', 'success', 45)")
    store.migrate()
    store.migrate()
    old = store.latest_sync()
    assert old.fetched == 45 and old.duration_ms == old.detail_serialized == 0
    with store._connect() as conn:
        assert next(r for r in conn.execute('PRAGMA table_info(sync_runs)') if r['name'] == 'duration_ms')['type'] == 'INTEGER'


def test_result_page_shows_duration(client, prepared):
    login(client)
    client.app.state.minutes_source = ConcurrentSource(1)
    data = client.post('/sync').json()
    assert type(data['duration_ms']) is int and 0 < data['duration_ms'] < 10000
    assert f"耗时 {data['duration_ms']} 毫秒" in client.get('/').text


def test_mcp_retry_budget_is_per_record_not_nested(setup_source, prepared, monkeypatch):
    source, server, _ = setup_source
    server.failure = ('get_minutes_transcription', 429)
    sleeps = []
    monkeypatch.setattr('app.sync.time.sleep', sleeps.append)
    result = run_sync(source, prepared)
    assert result.status == 'failed' and len(result.error.splitlines()) == 2
    counts = Counter((name, args.get('taskUuid')) for name, args in server.calls)
    for rid in ['real-1', 'real-2']:
        assert counts['get_minutes_transcription', rid] == 3
        assert counts['get_minutes_ai_summary', rid] == 3
        assert counts['get_minutes_basic_info', rid] == 0
    assert len(sleeps) == 4
