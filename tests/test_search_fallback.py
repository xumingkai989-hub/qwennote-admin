"""Exercise the old-SQLite path without changing existing FTS expectations."""
import logging
import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.store import fts_available
from app.sync import run_sync
from fakes import FakeMinutesSource
from test_login import login


def add(store, recording_id='match', title='战略', transcript='数据看板',
        created='2026-10-29T09:30:00+08:00'):
    store.upsert_recording(
        RecordingMeta(recording_id, title, datetime.fromisoformat(created), 18),
        RecordingDetail(recording_id, '', transcript, bool(transcript), False), created)


@pytest.fixture
def fallback(store, monkeypatch):
    monkeypatch.setattr('app.store.fts_available', lambda conn: False)
    store.migrate()
    return store


@pytest.mark.parametrize('failure', [RuntimeError('probe failed'),
                                    sqlite3.OperationalError('no such tokenizer: trigram')])
def test_probe_failure_does_not_escape(failure):
    conn = Mock()
    conn.execute.side_effect = failure
    assert fts_available(conn) is False


def test_probe_drop_failure_does_not_escape():
    conn = Mock()
    conn.execute.side_effect = [None, RuntimeError('drop failed')]
    assert fts_available(conn) is False


def test_probe_success_cleans_up_temp_table():
    with sqlite3.connect(':memory:') as conn:
        assert fts_available(conn) is True
        assert conn.execute('SELECT name FROM sqlite_temp_master').fetchall() == []


@pytest.mark.parametrize('available', [True, False])
def test_one_probe_per_migration_and_idempotent_schema(store, monkeypatch, available, caplog):
    probe = Mock(return_value=available)
    monkeypatch.setattr('app.store.fts_available', probe)
    with caplog.at_level(logging.INFO):
        store.migrate()
    assert store.fts_available is available
    with store._connect() as conn:
        before = conn.execute('SELECT type, name, sql FROM sqlite_master ORDER BY name').fetchall()
        triggers = [r['name'] for r in before if r['type'] == 'trigger' and r['name'].startswith('recordings_fts')]
        assert len(triggers) == (3 if available else 0)
        assert any(r['name'] == 'recordings_fts' for r in before) is available
    add(store)
    assert store.search_recordings('数据看板').total == 1
    assert store.read_recordings(query='数据看板').total == 1
    assert probe.call_count == 1
    store.migrate()
    assert probe.call_count == 2
    with store._connect() as conn:
        assert conn.execute('SELECT type, name, sql FROM sqlite_master ORDER BY name').fetchall() == before
    if not available:
        assert '搜索降级为 LIKE 模式（SQLite 不支持 trigram）' in caplog.text


@pytest.mark.parametrize('field', ['title', 'transcript', 'summary'])
def test_fallback_matches_each_field(fallback, field):
    meta = RecordingMeta('match', '战略' if field == 'title' else '例会', datetime.now(SHANGHAI), 18)
    detail = RecordingDetail('match', '战略' if field == 'summary' else '',
                             '战略' if field == 'transcript' else '', field == 'transcript', field == 'summary')
    fallback.upsert_recording(meta, detail, datetime.now(SHANGHAI).isoformat())
    assert fallback.search_recordings('战略').total == 1


def test_fallback_title_even_when_derived_text_is_empty(fallback):
    add(fallback)
    with fallback._connect() as conn:
        conn.execute("UPDATE recordings SET search_text='' WHERE recording_id='match'")
    assert fallback.search_recordings('战略').total == 1


@pytest.mark.parametrize('query', ['战略 看板', '看板 战略', '  看板   战略  ', '看板\t战略'])
def test_fallback_keywords_are_and_across_fields(fallback, query):
    add(fallback)
    add(fallback, 'only-title', transcript='其他内容')
    add(fallback, 'only-body', title='其他事项')
    assert [r.recording_id for r in fallback.search_recordings(query).items] == ['match']
    assert [r.recording_id for r in fallback.read_recordings(query=query).items] == ['match']


@pytest.mark.parametrize('query', ['%', '_', '\\', '" OR ', "' OR 1=1 --", '100%', 'a_b', 'a\\b'])
def test_fallback_keywords_remain_literal(fallback, query):
    add(fallback, transcript=query)
    add(fallback, 'miss', transcript='unrelated 1000 acb')
    assert [r.recording_id for r in fallback.search_recordings(query).items] == ['match']


def test_fallback_pagination_total_dates_and_tiebreaker(fallback):
    for i in range(25):
        add(fallback, f'match-{i:02}')
    add(fallback, 'old', created='2026-10-28T09:30:00+08:00')
    add(fallback, 'new', created='2026-10-30T09:30:00+08:00')
    day = date(2026, 10, 29)
    first = fallback.search_recordings('战略 看板', day, day)
    second = fallback.search_recordings('战略 看板', day, day, page=2)
    assert (first.total, first.pages, first.page) == (25, 2, 1)
    assert [r.recording_id for r in first.items] == [f'match-{i:02}' for i in range(24, 4, -1)]
    assert [r.recording_id for r in second.items] == [f'match-{i:02}' for i in range(4, -1, -1)]
    assert fallback.search_recordings('战略 看板', day, day, page=999) == second
    batch = fallback.read_recordings(day, day, limit=5, offset=20, query='战略 看板')
    assert batch.total == 25 and batch.items == second.items


def test_fallback_retains_fts_table_removes_writers_and_restores_index(store, monkeypatch):
    store.migrate()
    add(store, transcript='旧版索引词')
    with store._connect() as conn:
        before = conn.execute('SELECT * FROM recordings_fts_data').fetchall()
    with monkeypatch.context() as patch:
        patch.setattr('app.store.fts_available', lambda conn: False)
        store.migrate()
        add(store, transcript='新版索引词')
        add(store, 'deleted')
        with store._connect() as conn:
            conn.execute("DELETE FROM recordings WHERE recording_id='deleted'")
        store.rebuild_search()
        with store._connect() as conn:
            assert conn.execute('SELECT * FROM recordings_fts_data').fetchall() == before
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'recordings_fts_%'").fetchall() == []
        assert store.search_recordings('新版索引词').total == 1
    store.migrate()
    assert store.search_recordings('新版索引词').total == 1
    assert store.search_recordings('旧版索引词').total == 0


def test_legacy_database_backfills_text_in_fallback(store, monkeypatch):
    store.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('''CREATE TABLE recordings (
            recording_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            duration_min INTEGER, summary_md TEXT, transcript_md TEXT,
            has_transcript INTEGER NOT NULL DEFAULT 0, has_summary INTEGER NOT NULL DEFAULT 0,
            owner_open_id TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL)''')
        conn.execute("INSERT INTO recordings VALUES ('legacy', '战略', '2026-10-01T00:00:00+08:00', NULL, NULL, '看板', 1, 0, 'owner', 'old')")
    monkeypatch.setattr('app.store.fts_available', lambda conn: False)
    store.migrate()
    assert store.search_recordings('战略 看板').total == 1
    store.rebuild_search()
    assert store.search_recordings('战略 看板').total == 1


@pytest.mark.parametrize('available,label', [(True, 'FTS5 trigram'), (False, 'LIKE 降级')])
def test_settings_shows_actual_search_mode(client, store, monkeypatch, available, label):
    monkeypatch.setattr('app.store.fts_available', lambda conn: available)
    store.migrate()
    login(client)
    page = client.get('/settings')
    assert page.status_code == 200
    assert f'当前搜索模式 {label}' in page.text


def test_fallback_search_page(client, store, monkeypatch):
    monkeypatch.setattr('app.store.fts_available', lambda conn: False)
    store.migrate()
    add(store)
    login(client)
    response = client.get('/search', params={'q': '战略 看板'})
    assert response.status_code == 200
    assert '共 1 条' in response.text


@pytest.mark.parametrize('operation', ['get_recording', 'upsert_recording'])
@pytest.mark.parametrize('error,expected', [
    (sqlite3.OperationalError('no such tokenizer: trigram'), 'no such tokenizer: trigram'),
    (sqlite3.OperationalError('no such module: fts5'), 'no such module: fts5'),
    (sqlite3.OperationalError('no such tokenizer: trigram; Bearer private-test-value'), 'no such tokenizer: trigram'),
    (ValueError('private-test-value'), '请重试'),
])
def test_sync_persists_infrastructure_keywords_only(fallback, monkeypatch, operation, error, expected):
    meta = RecordingMeta('new', '例会', datetime.now(SHANGHAI) - timedelta(hours=1), 18)
    source = FakeMinutesSource([meta], {'new': RecordingDetail('new', '', '正文', True, False)})
    monkeypatch.setattr(fallback, operation, Mock(side_effect=error))
    run_sync(source, fallback, backfill_days=90)
    result = fallback.latest_sync()
    assert result.status == 'failed'
    assert expected in result.error
    assert 'private-test-value' not in result.error


def test_fallback_sync_upserts_successfully(fallback):
    meta = RecordingMeta('new', '战略', datetime.now(SHANGHAI) - timedelta(hours=1), 18)
    source = FakeMinutesSource([meta], {'new': RecordingDetail('new', '', '数据看板', True, False)})
    run_sync(source, fallback, backfill_days=90)
    assert fallback.latest_sync().inserted == 1
    assert fallback.latest_sync().status == 'success'
    assert fallback.search_recordings('战略 看板').total == 1
