from dataclasses import replace
from datetime import datetime
from html import unescape
from pathlib import Path
import re
import sqlite3

import pytest

from app.automation import AutomationSettings
from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.scheduler_tick import next_runs, read_state, retain_recordings, tick, tick_lock
from app.store import SQLiteStore
from app.sync import SyncInProgress, run_sync
from fakes import FakeMinutesSource
from test_login import login


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI)


def settings_form(client):
    response = client.get('/settings')
    token = unescape(re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1])
    return dict(csrf_token=token, sync_enabled='true', sync_time='03:00',
                sync_interval_hours='0', backfill_days='90', tag_enabled='true',
                tag_time='05:00', retention_days='0', sync_mode_default='quick',
                full_sync_enabled='true', full_sync_weekday='6', full_sync_time='03:30')


def test_defaults_and_roundtrip_survive_restart(client, store):
    assert store.get_settings() == AutomationSettings()
    values = AutomationSettings(False, '23:59', 7, 31, False, '00:01', 365)
    store.save_settings(values)
    restarted = SQLiteStore(store.db_path)
    restarted.migrate()
    assert restarted.get_settings() == values
    with restarted._connect() as conn:
        assert conn.execute('SELECT count(*) FROM settings').fetchone()[0] == 11


@pytest.mark.parametrize('field,value', [
    ('sync_time', '3:00'), ('sync_time', '24:00'), ('sync_time', '12:60'),
    ('sync_time', '03:00:00'), ('sync_time', ' 03:00'), ('tag_time', 'noon'),
    ('tag_time', ''), ('sync_interval_hours', -1), ('sync_interval_hours', 1.5),
    ('sync_interval_hours', True), ('backfill_days', 0), ('backfill_days', -1),
    ('retention_days', -1), ('retention_days', '10'), ('retention_days', 1000000),
    ('sync_enabled', 1), ('tag_enabled', 'true'),
])
def test_invalid_typed_settings_rejected(field, value):
    with pytest.raises(ValueError):
        replace(AutomationSettings(), **{field: value})


@pytest.mark.parametrize('field,value', [
    ('sync_time', '3:00'), ('sync_time', '24:00'), ('tag_time', '15:70'),
    ('sync_interval_hours', '-1'), ('sync_interval_hours', '0.5'),
    ('sync_interval_hours', ''), ('sync_interval_hours', '1e2'),
    ('retention_days', '-1'), ('retention_days', '3.5'),
    ('backfill_days', '0'), ('backfill_days', '-1'), ('backfill_days', '9999999'),
    ('sync_enabled', 'false'), ('tag_enabled', '<script>'),
])
def test_bad_post_leaves_all_settings_unchanged(client, store, field, value):
    login(client)
    original = replace(AutomationSettings(), backfill_days=17, retention_days=30)
    store.save_settings(original)
    form = settings_form(client)
    form[field] = value
    response = client.post('/settings', data=form)
    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert store.get_settings() == original


def test_settings_save_redirect_and_next_run(client, store):
    login(client)
    form = settings_form(client)
    form.update(sync_time='21:23', sync_interval_hours='3', backfill_days='42', retention_days='14')
    response = client.post('/settings', data=form)
    assert response.status_code == 303
    page = client.get(response.headers['location'])
    assert '已保存' in page.text and '下次同步' in page.text
    assert page.headers['cache-control'] == 'no-store'
    assert store.get_settings() == AutomationSettings(True, '21:23', 3, 42, True, '05:00', 14)
    form = settings_form(client)
    del form['sync_enabled']
    del form['tag_enabled']
    assert client.post('/settings', data=form).status_code == 303
    assert not store.get_settings().sync_enabled and not store.get_settings().tag_enabled
    assert '自动同步已关闭' in client.get('/settings?saved=1').text


def test_settings_auth_csrf_and_cookie_isolation(client, store, settings):
    assert client.get('/settings').status_code == 302
    assert client.post('/settings', data={}).status_code == 302
    login(client)
    form = settings_form(client)
    assert any(cookie.name == 'settings_csrf' and cookie.path == '/settings' for cookie in client.cookies.jar)
    client.get('/admin')
    client.get('/hotwords')
    assert client.post('/settings', data=form).status_code == 303
    assert client.post('/settings', data={**form, 'csrf_token': 'bad'}).status_code == 403
    assert client.post('/settings', json=form).status_code == 403
    body = '&'.join(f'{key}={value}' for key, value in form.items()) + '&backfill_days=1'
    assert client.post('/settings', content=body, headers={'content-type':'application/x-www-form-urlencoded'}).status_code == 403
    store.remove_user(settings.admin_open_id)
    assert client.get('/settings').status_code == 403
    assert client.post('/settings', data=form).status_code == 403


def test_nonadmin_cannot_read_or_save(client, store, fake_auth):
    user = FeishuUser('ou_reader', '读者', '')
    store.add_user(user)
    fake_auth.user = user
    login(client)
    assert client.get('/settings').status_code == 403
    assert client.post('/settings', data={}).status_code == 403
    assert 'href="/settings"' not in client.get('/').text


def test_settings_nav_and_descriptions(client):
    login(client)
    page = client.get('/settings').text
    assert 'href="/settings" aria-label="设置中心"' in page
    for name in AutomationSettings().values():
        assert f'id="{name}"' in page
    assert page.count('aria-describedby=') == 11
    assert all(f'<legend>{group}</legend>' in page for group in ('同步', '同步策略', '标签', '数据保留'))


def actions():
    calls = []
    return calls, {name + '_job': lambda store, prefs, name=name: calls.append((name, prefs))
                   for name in ('sync', 'full_sync', 'tag', 'retention')}


@pytest.mark.parametrize('time,expected', [
    ('2026-10-30T02:50', []), ('2026-10-30T03:00', ['sync']),
    ('2026-10-30T03:10', ['sync']), ('2026-10-30T04:59', ['sync']),
    ('2026-10-30T05:00', ['sync', 'tag']), ('2026-10-30T23:59', ['sync', 'tag']),
])
def test_tick_due_and_not_due(client, store, time, expected):
    calls, jobs = actions()
    assert tick(store, now=at(time), **jobs)['executed'] == expected
    assert [name for name, _ in calls] == expected
    assert set(read_state(store)) == set(expected)


def test_daily_idempotence_restart_and_next_day(client, store):
    calls, jobs = actions()
    assert tick(store, now=at('2026-10-30T05:00'), **jobs)['executed'] == ['sync', 'tag']
    restarted = SQLiteStore(store.db_path)
    assert tick(restarted, now=at('2026-10-30T05:10'), **jobs)['executed'] == []
    assert tick(restarted, now=at('2026-10-30T23:50'), **jobs)['executed'] == []
    assert tick(restarted, now=at('2026-10-31T02:50'), **jobs)['executed'] == []
    assert tick(restarted, now=at('2026-10-31T03:00'), **jobs)['executed'] == ['sync']
    assert tick(restarted, now=at('2026-10-31T05:00'), **jobs)['executed'] == ['tag']
    assert len(calls) == 4


@pytest.mark.parametrize('interval', [1, 2, 7, 25])
def test_interval_across_days_and_same_hour(client, store, interval):
    from datetime import timedelta
    store.save_settings(replace(AutomationSettings(), sync_interval_hours=interval, tag_enabled=False))
    calls, jobs = actions()
    start = at('2026-10-30T23:10')
    assert tick(store, now=start, **jobs)['executed'] == ['sync']
    assert tick(SQLiteStore(store.db_path), now=start + timedelta(minutes=40), **jobs)['executed'] == []
    due = start.replace(minute=0) + timedelta(hours=interval)
    assert tick(store, now=due - timedelta(minutes=1), **jobs)['executed'] == []
    assert tick(store, now=due, **jobs)['executed'] == ['sync']
    assert len(calls) == 2


@pytest.mark.parametrize('sync_enabled,tag_enabled,expected', [
    (False, True, ['tag']), (True, False, ['sync']), (False, False, []),
])
def test_disabled_jobs(client, store, sync_enabled, tag_enabled, expected):
    store.save_settings(replace(AutomationSettings(), sync_enabled=sync_enabled, tag_enabled=tag_enabled))
    calls, jobs = actions()
    assert tick(store, now=at('2026-10-30T06:00'), **jobs)['executed'] == expected
    assert [name for name, _ in calls] == expected


def test_daily_settings_edit_does_not_repeat_same_day(client, store):
    calls, jobs = actions()
    tick(store, now=at('2026-10-30T06:00'), **jobs)
    store.save_settings(replace(AutomationSettings(), sync_time='08:00', tag_time='09:00'))
    assert tick(store, now=at('2026-10-30T10:00'), **jobs)['executed'] == []
    assert next_runs(store, store.get_settings(), at('2026-10-30T10:00'))['sync'] == at('2026-10-31T08:00')


def test_next_runs_tick_rounding_and_timezone(client, store):
    prefs = replace(AutomationSettings(), sync_time='03:07')
    assert next_runs(store, prefs, at('2026-10-30T02:00'))['sync'] == at('2026-10-30T03:10')
    utc = datetime.fromisoformat('2026-10-29T19:11:01+00:00')
    assert next_runs(store, prefs, utc)['sync'] == at('2026-10-30T03:20')
    calls, jobs = actions()
    assert tick(store, now=utc, **jobs)['executed'] == ['sync']
    assert read_state(store)['sync'] == at('2026-10-30T03:11:01')
    with pytest.raises(ValueError):
        tick(store, now=datetime(2026, 10, 30))


def test_tick_overlap_has_no_claim_and_busy_sync_consumes_slot(client, store):
    calls, jobs = actions()
    with tick_lock(store):
        assert tick(SQLiteStore(store.db_path), now=at('2026-10-30T06:00'), **jobs)['skipped'] == ['tick_locked']
    assert not read_state(store)
    def busy(*args):
        raise SyncInProgress
    result = tick(store, now=at('2026-10-30T06:00'), **{**jobs, 'sync_job': busy})
    assert result['skipped'] == ['sync'] and 'sync' in read_state(store)
    assert tick(store, now=at('2026-10-30T06:10'), **jobs)['executed'] == []


def test_failure_consumes_slot_and_other_jobs_continue(client, store):
    calls, jobs = actions()
    def fail(*args):
        raise RuntimeError('provider secret must not be logged')
    result = tick(store, now=at('2026-10-30T06:00'), **{**jobs, 'sync_job': fail})
    assert result['failed'] == ['sync'] and result['executed'] == ['tag']
    assert tick(SQLiteStore(store.db_path), now=at('2026-10-30T06:10'), **jobs)['executed'] == []


def add_recording(store, recording_id, created_at):
    store.upsert_recording(RecordingMeta(recording_id, '保留策略 searchable', created_at, 1),
                           RecordingDetail(recording_id, 'summary', 'transcript', True, True), created_at.isoformat())
    store.save_override(recording_id, 'summary', '人工修订')
    with store._connect() as conn:
        conn.execute('INSERT INTO recording_tags VALUES (?, ?, 1, ?, ?)',
                     (recording_id, 'AI Tutor', 'test', created_at.isoformat()))


def test_retention_cascades_override_tags_fts_and_boundary(client, store):
    now = at('2026-10-30T00:00')
    # Same instant in a different offset; delete strictly older than 10 days.
    old = datetime.fromisoformat('2026-10-19T15:59:59+00:00')
    add_recording(store, 'old', old)
    add_recording(store, 'boundary', at('2026-10-20T00:00'))
    add_recording(store, 'new', at('2026-10-29T00:00'))
    store.save_settings(replace(AutomationSettings(), retention_days=10))
    calls, jobs = actions()
    jobs.pop('retention_job')
    assert tick(store, now=now, **jobs)['executed'] == ['retention']
    assert store.get_recording('old') is None
    assert store.get_overrides('old') == {}
    assert store.get_recording('boundary') is not None
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM recording_tags').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM recording_overrides').fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM recordings_fts WHERE recordings_fts MATCH 'searchable'").fetchone()[0] == 2
    assert tick(store, now=at('2026-10-30T00:10'), **jobs)['executed'] == []
    assert retain_recordings(store, 0, now) == 0
    assert retain_recordings(store, 999999, now) == 0


def test_retention_transaction_rolls_back(client, store):
    add_recording(store, 'old', at('2026-10-01T00:00'))
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON recordings BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        retain_recordings(store, 10, at('2026-10-30T00:00'))
    assert store.get_recording('old') is not None
    assert store.get_overrides('old')['summary']['content_md'] == '人工修订'


def test_tag_switch_stops_sync_auto_tags(client, store, monkeypatch):
    now = datetime.now(SHANGHAI)
    source = FakeMinutesSource()
    source.recordings = [RecordingMeta('sync-new', '标题', now, 1)]
    source.details = {'sync-new': RecordingDetail('sync-new', '内容', '', True, False)}
    calls = []
    monkeypatch.setattr('app.tag.run_tags', lambda *a, **k: calls.append(k))
    store.save_settings(replace(AutomationSettings(), tag_enabled=False))
    assert run_sync(source, store, backfill_days=1).status == 'success'
    assert not calls
    store.save_settings(AutomationSettings())
    source.recordings = [replace(source.recordings[0], title='新标题')]
    assert run_sync(source, store, backfill_days=1).status == 'success'
    assert calls[0]['recording_ids'] == ['sync-new']


def test_manual_sync_uses_saved_backfill_when_auto_sync_disabled(client, store):
    login(client)
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, backfill_days=1, tag_enabled=False))
    assert client.post('/sync').status_code == 200
    assert store.latest_sync() is not None


def test_tick_defaults_execute_existing_sync_pipeline(client, store, settings, monkeypatch):
    import app.scheduler_tick as scheduler
    now = datetime.now(SHANGHAI)
    source = FakeMinutesSource()
    source.recordings = [RecordingMeta('actual-sync', '标题', now, 1)]
    source.details = {'actual-sync': RecordingDetail('actual-sync', '内容', '', True, False)}
    monkeypatch.setattr('scripts.deployment_runtime.deployment_settings', lambda: settings)
    monkeypatch.setattr('app.minutes.FixtureMinutesSource', lambda _: source)
    store.save_settings(replace(AutomationSettings(), sync_interval_hours=1, tag_enabled=False, backfill_days=1))
    assert scheduler.tick(store, now=now)['executed'] == ['sync']
    assert store.get_recording('actual-sync') is not None
    assert scheduler.tick(SQLiteStore(store.db_path), now=now)['executed'] == []
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM sync_runs').fetchone()[0] == 1


def test_tick_unit_and_install_contract():
    root = Path(__file__).resolve().parents[1]
    timer = (root / 'deploy/systemd/qwennote-tick.timer').read_text()
    service = (root / 'deploy/systemd/qwennote-tick.service').read_text()
    installer = (root / 'scripts/install_scheduler.sh').read_text()
    assert 'OnCalendar=*-*-* *:0/10:00 Asia/Shanghai' in timer
    assert 'Persistent=true' in timer
    assert 'python -m app.scheduler_tick' in service
    assert 'User=qwennote' in service and 'ReadWritePaths=/opt/qwennote/data' in service
    assert 'systemctl disable --now qwennote-sync.timer qwennote-tag-maintenance.timer' in installer
    assert 'systemctl enable --now qwennote-tick.timer' in installer
    assert 'nginx' not in installer


def test_crash_after_claim_does_not_replay_after_restart(client, store):
    calls, jobs = actions()
    def interrupted(*args):
        raise SystemExit(9)
    with pytest.raises(SystemExit):
        tick(store, now=at('2026-10-30T03:00'), **{**jobs, 'sync_job': interrupted})
    assert 'sync' in read_state(SQLiteStore(store.db_path))
    assert tick(SQLiteStore(store.db_path), now=at('2026-10-30T03:10'), **jobs)['executed'] == []


def test_busy_existing_interval_claim_is_consumed(client, store):
    calls, jobs = actions()
    store.save_settings(replace(AutomationSettings(), sync_interval_hours=2, tag_enabled=False))
    tick(store, now=at('2026-10-30T01:00'), **jobs)
    previous = read_state(store)
    def busy(*args):
        raise SyncInProgress
    assert tick(store, now=at('2026-10-30T03:00'), **{**jobs,'sync_job':busy})['skipped'] == ['sync']
    assert read_state(store)['sync'] > previous['sync']
    assert tick(store, now=at('2026-10-30T03:10'), **jobs)['executed'] == []


def test_defaults_tag_runner_calls_maintenance_and_flags_failure(client, store, monkeypatch):
    store.save_settings(replace(AutomationSettings(), sync_enabled=False))
    calls = []
    def maintenance(store):
        calls.append(store)
        return {'failed': 1}
    monkeypatch.setattr('app.tag_maintenance.run_maintenance', maintenance)
    assert tick(store, now=at('2026-10-30T05:00'))['failed'] == ['tag']
    assert calls == [store]


def test_tick_cli_requires_db_and_prints_safe_summary(client, store, monkeypatch, capsys):
    from app.scheduler_tick import main
    monkeypatch.delenv('DB_PATH')
    assert main() == 1
    monkeypatch.setenv('DB_PATH', str(store.db_path))
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, tag_enabled=False))
    assert main() == 0
    assert capsys.readouterr().out.strip() == '{"executed": [], "failed": [], "skipped": []}'
