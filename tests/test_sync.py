import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta
from fakes import FakeMinutesSource
from test_login import login

SHANGHAI = ZoneInfo('Asia/Shanghai')


def rows(settings, table):
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]


@pytest.fixture
def source():
    day = datetime.now(SHANGHAI).replace(hour=10, minute=0, second=0, microsecond=0)
    return FakeMinutesSource(
        [RecordingMeta('demo-1', '示例会议', day, 18),
         RecordingMeta('demo-2', '待转录', day, None)],
        {'demo-1': RecordingDetail('demo-1', '## 总结', '[00:00:26.600–00:00:30.000] 发言人 1：测试', True, True),
         'demo-2': RecordingDetail('demo-2', None, None, False, False)},
    )


@pytest.fixture
def sync_client(settings, fake_auth, source):
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, minutes_source=source), follow_redirects=False) as client:
        assert login(client).status_code == 302
        yield client


def test_first_sync_inserts_records_and_one_finished_run(sync_client, settings):
    response = sync_client.post('/sync')
    assert response.status_code == 200
    result = response.json()
    assert {k: result[k] for k in ('status', 'fetched', 'inserted', 'updated')} == {
        'status': 'success', 'fetched': 2, 'inserted': 2, 'updated': 0,
    }
    assert datetime.fromisoformat(result['finished_at']) >= datetime.fromisoformat(result['started_at'])
    records = rows(settings, 'recordings')
    assert len(records) == 2
    assert records[0]['created_at'].endswith('+08:00')
    assert records[0]['transcript_md'].startswith('[00:00:26.600')
    assert records[1]['duration_min'] is None
    assert records[1]['summary_md'] is None
    runs = rows(settings, 'sync_runs')
    assert len(runs) == 1
    assert all(runs[0][key] == result[key] for key in result)


def test_identical_sync_only_changes_synced_at(sync_client, settings):
    sync_client.post('/sync')
    before = rows(settings, 'recordings')
    result = sync_client.post('/sync').json()
    assert (result['fetched'], result['inserted'], result['updated']) == (2, 0, 0)
    after = rows(settings, 'recordings')
    for old, new in zip(before, after):
        assert new['synced_at'] >= old['synced_at']
        assert {k: v for k, v in old.items() if k != 'synced_at'} == {k: v for k, v in new.items() if k != 'synced_at'}
    assert len(rows(settings, 'sync_runs')) == 2


def test_title_change_updates_exactly_one_record(sync_client, settings, source):
    sync_client.post('/sync')
    source.recordings[0] = replace(source.recordings[0], title='更正标题')
    result = sync_client.post('/sync').json()
    assert (result['inserted'], result['updated']) == (0, 1)
    assert rows(settings, 'recordings')[0]['title'] == '更正标题'
    assert len(rows(settings, 'sync_runs')) == 2


def test_detail_failure_keeps_other_records_and_records_safe_error(sync_client, settings, source):
    source.detail_errors['demo-1'] = RuntimeError('Bearer do-not-persist-this-secret')
    result = sync_client.post('/sync').json()
    assert (result['status'], result['fetched'], result['inserted']) == ('failed', 2, 1)
    assert [row['recording_id'] for row in rows(settings, 'recordings')] == ['demo-2']
    run, = rows(settings, 'sync_runs')
    assert run['status'] == 'failed' and 'demo-1' in run['error']
    assert 'do-not-persist' not in str(run)
    html = sync_client.get('/').text
    assert 'failed' in html and 'demo-1' in html and '重试' in html


def test_whole_batch_failure_preserves_old_records_and_allows_retry(sync_client, settings, source):
    sync_client.post('/sync')
    before = rows(settings, 'recordings')
    source.list_error = RuntimeError('invalid credential')
    result = sync_client.post('/sync').json()
    assert result['status'] == 'failed'
    assert rows(settings, 'recordings') == before
    assert len(rows(settings, 'sync_runs')) == 2
    source.list_error = None
    assert sync_client.post('/sync').json()['status'] == 'success'
    assert len(rows(settings, 'sync_runs')) == 3


def test_duplicates_are_fetched_once(sync_client, settings, source):
    source.recordings += source.recordings[:1] * 2
    result = sync_client.post('/sync').json()
    assert (result['fetched'], result['inserted'], result['updated']) == (2, 2, 0)
    assert len(rows(settings, 'recordings')) == 2


def test_backfill_includes_today_and_89_previous_shanghai_days(sync_client, settings, source):
    today = datetime.now(SHANGHAI).date()
    source.recordings += [
        replace(source.recordings[0], recording_id='boundary', created_at=datetime.combine(today-timedelta(days=89), datetime.min.time(), SHANGHAI)),
        replace(source.recordings[0], recording_id='old', created_at=datetime.combine(today-timedelta(days=90), datetime.min.time(), SHANGHAI)),
    ]
    source.details['boundary'] = replace(source.details['demo-1'], recording_id='boundary')
    assert sync_client.post('/sync').json()['fetched'] == 3
    assert {r['recording_id'] for r in rows(settings, 'recordings')} == {'demo-1', 'demo-2', 'boundary'}


def test_concurrent_trigger_across_app_instances_returns_409(settings, fake_auth, source):
    entered, release = Event(), Event()
    source.detail_entered, source.detail_release = entered, release
    apps = [create_app(settings=settings, feishu_auth=fake_auth, minutes_source=source) for _ in range(2)]
    with TestClient(apps[0], follow_redirects=False) as first, TestClient(apps[1], follow_redirects=False) as second:
        login(first)
        login(second)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(first.post, '/sync')
            try:
                assert entered.wait(5)
                run, = rows(settings, 'sync_runs')
                assert run['status'] == 'running' and run['finished_at'] is None
                assert second.post('/sync').status_code == 409
                assert len(rows(settings, 'sync_runs')) == 1
                assert 'running' in second.get('/').text
            finally:
                release.set()
            assert future.result(5).json()['status'] == 'success'
    assert len(rows(settings, 'sync_runs')) == 1


def test_sync_requires_active_whitelist_session(client, settings):
    assert client.post('/sync').status_code == 401
    login(client)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('UPDATE users SET is_active = 0')
    assert client.post('/sync').status_code == 401
    assert rows(settings, 'sync_runs') == []


def test_home_has_button_status_counts_and_immediate_result_script(sync_client):
    assert '尚未同步' in sync_client.get('/').text
    sync_client.post('/sync')
    html = sync_client.get('/').text
    assert all(text in html for text in ('上次同步时间', 'success', '拉取 2', '新增 2', '更新 0', '更新', 'fetch(', '/sync', '笔记创建时间'))


def test_detail_id_mismatch_is_skipped(sync_client, settings, source):
    source.details['demo-1'] = replace(source.details['demo-1'], recording_id='other')
    assert sync_client.post('/sync').json()['status'] == 'failed'
    assert [r['recording_id'] for r in rows(settings, 'recordings')] == ['demo-2']


@pytest.mark.parametrize('field,value', [
    ('summary_md', '新总结'), ('transcript_md', '新转写'),
    ('has_summary', False), ('has_transcript', False),
])
def test_detail_changes_update_one_record(sync_client, settings, source, field, value):
    sync_client.post('/sync')
    source.details['demo-1'] = replace(source.details['demo-1'], **{field: value})
    result = sync_client.post('/sync').json()
    assert (result['inserted'], result['updated']) == (0, 1)
    assert rows(settings, 'recordings')[0][field] == value


def test_failed_refresh_keeps_previous_detail(sync_client, settings, source):
    sync_client.post('/sync')
    before = rows(settings, 'recordings')[0]
    source.detail_errors['demo-1'] = RuntimeError('unavailable')
    assert sync_client.post('/sync').json()['status'] == 'failed'
    assert rows(settings, 'recordings')[0] == before


def test_configured_backfill_range(settings, fake_auth, source):
    yesterday = datetime.now(SHANGHAI) - timedelta(days=1)
    source.recordings = [replace(source.recordings[0], created_at=yesterday)]
    with TestClient(create_app(settings=replace(settings, backfill_days=1), feishu_auth=fake_auth, minutes_source=source), follow_redirects=False) as client:
        login(client)
        assert client.post('/sync').json()['fetched'] == 0
    assert rows(settings, 'recordings') == []
    assert len(rows(settings, 'sync_runs')) == 1


def test_cross_origin_trigger_is_rejected(sync_client, settings):
    assert sync_client.post('/sync', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert rows(settings, 'sync_runs') == []


def test_interrupted_batch_can_be_retried(sync_client, settings):
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("INSERT INTO sync_runs (started_at, status) VALUES ('2026-01-01T00:00:00+08:00', 'running')")
    assert sync_client.post('/sync').json()['status'] == 'success'
    runs = rows(settings, 'sync_runs')
    assert len(runs) == 2
    assert runs[0]['status'] == 'failed' and runs[0]['finished_at']
    assert '中断' in runs[0]['error']


def test_fixture_to_http_to_sqlite_is_idempotent(settings, fake_auth, tmp_path):
    import json
    from pathlib import Path
    from app.minutes import FixtureMinutesSource

    payload = json.loads((Path(__file__).parent / 'fixtures/minutes/2026-10-29.json').read_text())
    for item in payload['recordings']:
        item['created_at'] = datetime.now(SHANGHAI).isoformat()
    (tmp_path / 'example.json').write_text(json.dumps(payload))
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, minutes_source=FixtureMinutesSource(tmp_path)), follow_redirects=False) as client:
        login(client)
        assert client.post('/sync').json()['inserted'] == 2
        assert client.post('/sync').json()['updated'] == 0
        assert client.post('/sync').json()['inserted'] == 0
    assert len(rows(settings, 'recordings')) == 2
    assert len(rows(settings, 'sync_runs')) == 3
