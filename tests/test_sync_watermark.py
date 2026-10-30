"""Watermark paging, weekly claims and CLI mode contracts; entirely offline."""
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from app.automation import AutomationSettings
from app.mcp import McpMinutesSource
from app.minutes import SHANGHAI
from app.scheduler_tick import next_runs, read_state, tick
from app.sync import SyncRun
from test_mcp import setup_source

NOW = datetime(2026, 11, 1, 3, 30, tzinfo=SHANGHAI)  # Sunday


@pytest.mark.parametrize('ordered,early_stop,expected_pages', [
    (True, True, 1), (False, True, 3), (True, False, 3),
])
def test_list_paging_stops_only_when_confirmed_descending(setup_source, monkeypatch,
                                                         ordered, early_stop, expected_pages):
    source, _, _ = setup_source
    start = NOW - timedelta(minutes=10)
    # Old item followed by a newer one makes the first page non-descending.
    dates = [[NOW, start, start - timedelta(seconds=1)],
             [start - timedelta(minutes=1)], [start - timedelta(minutes=2)]]
    if not ordered:
        dates[0] = [start - timedelta(seconds=1), NOW, start]
        dates[2] = [NOW - timedelta(minutes=1)]
    calls = []
    def call(name, arguments):
        assert name == 'list_my_minutes'
        assert arguments['startTime'] == int(start.timestamp() * 1000)
        assert arguments['endTime'] == int(NOW.timestamp() * 1000)
        assert arguments['pageSize'] == 100
        index = int(arguments.get('cursor', 0))
        calls.append(index)
        return {'items': [{'uuid': f'{index}-{i}', 'title': '录音',
                          'gmtCreate': int(t.timestamp() * 1000)}
                         for i, t in enumerate(dates[index])],
                'hasMore': index < 2, 'nextToken': str(index + 1)}
    monkeypatch.setattr(source, '_initialize', lambda: None)
    monkeypatch.setattr(source, '_call', call)
    records = source.sync_range(start, NOW, early_stop=early_stop)
    assert len(calls) == expected_pages
    assert all(start <= record.created_at <= NOW for record in records)
    assert len(records) == (2 if ordered else 3)


def test_non_descending_later_page_disables_early_stop(setup_source, monkeypatch):
    source, _, _ = setup_source
    start = NOW - timedelta(minutes=10)
    pages = [[NOW - timedelta(minutes=1)],
             [NOW - timedelta(minutes=2), NOW, start - timedelta(seconds=1)],
             [NOW - timedelta(minutes=3)]]
    calls = []
    def call(name, args):
        index = int(args.get('cursor', 0)); calls.append(index)
        return {'items': [{'uuid': f'{index}-{i}', 'title': '',
                          'gmtCreate': int(at.timestamp()*1000)} for i, at in enumerate(pages[index])],
                'hasMore': index < 2, 'nextToken': str(index + 1)}
    monkeypatch.setattr(source, '_initialize', lambda: None)
    monkeypatch.setattr(source, '_call', call)
    assert len(source.sync_range(start, NOW, early_stop=True)) == 4
    assert calls == [0, 1, 2]


def jobs(calls, fail=False):
    def weekly(store, preferences):
        calls.append(preferences.sync_mode_default)
        if fail:
            raise RuntimeError('provider failure')
    return dict(sync_job=lambda *_: None, full_sync_job=weekly, tag_job=lambda *_: None)


@pytest.mark.parametrize('weekday', range(7))
def test_weekly_full_sync_only_due_on_configured_day(client, store, weekday):
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, tag_enabled=False,
                                full_sync_weekday=weekday))
    calls = []
    result = tick(store, now=NOW, **jobs(calls))
    assert result['executed'] == (['full_sync'] if weekday == 6 else [])
    assert calls == (['full'] if weekday == 6 else [])


@pytest.mark.parametrize('fail', [False, True])
def test_weekly_claim_dedupes_restart_and_repeats_next_week(client, store, fail):
    store.save_settings(replace(AutomationSettings(), sync_enabled=False, tag_enabled=False))
    calls = []
    assert tick(store, now=NOW - timedelta(minutes=1), **jobs(calls, fail))['executed'] == []
    result = tick(store, now=NOW, **jobs(calls, fail))
    assert result['failed' if fail else 'executed'] == ['full_sync']
    for when in [NOW + timedelta(minutes=10), NOW + timedelta(hours=15),
                 NOW + timedelta(days=1), NOW + timedelta(days=6)]:
        assert tick(store, now=when, **jobs(calls, fail))['executed'] == []
    assert calls == ['full']
    assert next_runs(store, store.get_settings(), NOW)['full_sync'] == NOW + timedelta(days=7)
    tick(store, now=NOW + timedelta(days=7), **jobs(calls, fail))
    assert calls == ['full', 'full']


def test_weekly_edit_cannot_create_second_run_in_same_week(client, store):
    prefs = replace(AutomationSettings(), sync_enabled=False, tag_enabled=False,
                    full_sync_weekday=0)
    store.save_settings(prefs)
    calls = []
    monday = NOW - timedelta(days=6)
    tick(store, now=monday, **jobs(calls))
    store.save_settings(replace(prefs, full_sync_weekday=6))
    assert tick(store, now=NOW, **jobs(calls))['executed'] == []
    assert next_runs(store, store.get_settings(), NOW)['full_sync'] == NOW + timedelta(days=7)
    assert calls == ['full']


def test_disabled_weekly_has_no_next_run(client, store):
    prefs = replace(AutomationSettings(), full_sync_enabled=False)
    store.save_settings(prefs)
    assert next_runs(store, prefs, NOW)['full_sync'] is None
    assert 'full_sync' not in tick(store, now=NOW, **jobs([]))['executed']


@pytest.mark.parametrize('mode', ['quick', 'full'])
def test_daily_runner_uses_saved_mode(client, store, settings, monkeypatch, mode):
    import app.scheduler_tick as scheduler
    from fakes import FakeMinutesSource
    calls = []
    store.save_settings(replace(AutomationSettings(), sync_mode_default=mode,
                                full_sync_enabled=False, tag_enabled=False))
    monkeypatch.setattr('scripts.deployment_runtime.deployment_settings', lambda: settings)
    monkeypatch.setattr('app.minutes.FixtureMinutesSource', lambda *_: FakeMinutesSource())
    def sync(source, repository, **kwargs):
        calls.append(kwargs)
        return SyncRun(1, NOW.isoformat(), NOW.isoformat(), 'success', mode=mode)
    monkeypatch.setattr('app.sync.run_sync', sync)
    assert scheduler.tick(store, now=NOW)['executed'] == ['sync']
    assert calls == [{'backfill_days': 90, 'full': mode == 'full'}]


def test_weekly_and_daily_share_actual_sync_lock_and_consume_collision(client, store, settings, monkeypatch):
    from fakes import FakeMinutesSource
    monkeypatch.setattr('scripts.deployment_runtime.deployment_settings', lambda: settings)
    monkeypatch.setattr('app.minutes.FixtureMinutesSource', lambda *_: FakeMinutesSource())
    store.save_settings(replace(AutomationSettings(), tag_enabled=False))
    with store.sync_lock():
        result = tick(store, now=NOW)
    assert result['skipped'] == ['sync', 'full_sync']
    assert store.latest_sync() is None
    assert set(read_state(store)) == {'sync', 'full_sync'}
    assert tick(store, now=NOW + timedelta(minutes=10))['executed'] == []


@pytest.mark.parametrize('saved,args,expected', [
    ('quick', [], 'quick'), ('full', [], 'full'),
    ('full', ['--mode', 'quick'], 'quick'), ('quick', ['--mode', 'full'], 'full'),
])
def test_cli_uses_saved_default_and_explicit_override(client, store, settings, monkeypatch,
                                                      capsys, saved, args, expected):
    from scripts import sync_once
    store.save_settings(replace(AutomationSettings(), sync_mode_default=saved, backfill_days=17))
    calls = []
    monkeypatch.setattr(sync_once, 'deployment_settings', lambda: settings)
    def run(source, repository, **kwargs):
        calls.append(kwargs)
        return SyncRun(1, NOW.isoformat(), NOW.isoformat(), 'success', mode=expected)
    monkeypatch.setattr(sync_once, 'run_sync', run)
    assert sync_once.main(args) == 0
    assert calls == [{'backfill_days': 17, 'full': expected == 'full'}]
    assert json.loads(capsys.readouterr().out)['mode'] == expected


@pytest.mark.parametrize('field,value', [
    ('sync_mode_default', 'invalid'), ('full_sync_enabled', 1),
    ('full_sync_weekday', -1), ('full_sync_weekday', 7), ('full_sync_weekday', True),
    ('full_sync_time', '25:00'), ('full_sync_time', '3:30'),
])
def test_new_settings_validate(field, value):
    with pytest.raises(ValueError):
        replace(AutomationSettings(), **{field: value})


@pytest.mark.parametrize('field,value', [
    ('sync_mode_default', 'invalid'), ('full_sync_enabled', 'false'),
    ('full_sync_weekday', '-1'), ('full_sync_weekday', '7'),
    ('full_sync_weekday', 'x'), ('full_sync_weekday', '1.5'),
    ('full_sync_time', '25:00'), ('full_sync_time', '3:30'),
])
def test_strategy_bad_form_preserves_settings(client, store, field, value):
    from test_login import login
    from test_settings import settings_form
    login(client)
    before = store.get_settings()
    form = settings_form(client); form[field] = value
    assert client.post('/settings', data=form).status_code == 400
    assert store.get_settings() == before


def test_strategy_roundtrip_and_two_next_times(client, store):
    from test_login import login
    from test_settings import settings_form
    login(client)
    form = settings_form(client)
    form.update(sync_mode_default='full', full_sync_weekday='2', full_sync_time='04:20')
    assert client.post('/settings', data=form).status_code == 303
    prefs = store.get_settings()
    assert (prefs.sync_mode_default, prefs.full_sync_enabled, prefs.full_sync_weekday,
            prefs.full_sync_time) == ('full', True, 2, '04:20')
    page = client.get('/settings').text
    assert '下次同步' in page and '下次全盘同步' in page and '<legend>同步策略</legend>' in page
    del form['full_sync_enabled']
    assert client.post('/settings', data=form).status_code == 303
    assert not store.get_settings().full_sync_enabled
