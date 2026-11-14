from dataclasses import replace
import json

import pytest

from app import board, board_analysis, board_generation
from app.automation import AutomationSettings
from app.feishu import FeishuUser
from app.scheduler_tick import tick, read_state, next_runs, board_is_current
from app.store import SQLiteStore
from test_board import add, GOOD, reports
from test_board_page import board_client, token
from test_login import login
from test_settings import at, settings_form


@pytest.mark.parametrize('value', ['25:00', '9:5', '', '22:60'])
def test_board_time_rejected(client, store, value):
    with pytest.raises(ValueError):
        AutomationSettings(board_time=value)
    login(client)
    before = store.get_settings()
    response = client.post('/settings', data={**settings_form(client), 'board_time': value})
    assert response.status_code == 400
    assert store.get_settings() == before
    assert response.context['preferences']['board_enabled'] is True


def test_board_settings_roundtrip_and_disabled_next_run(client, store):
    login(client)
    form = settings_form(client)
    assert client.post('/settings', data=form).status_code == 303
    restarted = SQLiteStore(store.db_path)
    assert restarted.get_settings().board_time == '22:00'
    assert restarted.get_settings().board_enabled is True
    assert '下次生成看板' in client.get('/settings').text
    del form['board_enabled']
    assert client.post('/settings', data=form).status_code == 303
    assert restarted.get_settings().board_enabled is False
    assert next_runs(store, store.get_settings(), at('2026-11-06T21:59'))['board'] is None
    assert '自动生成看板已关闭' in client.get('/settings').text


def board_only(store, **kw):
    store.save_settings(replace(AutomationSettings(), sync_enabled=False,
                               full_sync_enabled=False, tag_enabled=False, **kw))


def test_due_claim_idempotence_restart_and_week_cutoff(client, store, monkeypatch):
    board_only(store)
    add(store, 'monday', '2026-11-02T09:00:00+08:00', 20)
    add(store, 'friday', minutes=30)
    add(store, 'future', '2026-11-07T09:00:00+08:00', 80)
    calls = []
    def analyze(*args):
        calls.append(True)
        assert 'board' in read_state(store)  # claim persisted before the model
        return GOOD, 'fixture', None
    monkeypatch.setattr(board_analysis, '_analyze', analyze)
    monkeypatch.setattr(board_generation, '_requester', lambda *a: lambda p: '{"comparison":[]}')
    from app import primary_tag
    monkeypatch.setattr(primary_tag, 'ensure_week', lambda *a, **kw:
                        {'failed': 0, 'fallback': 0, 'items': []})
    assert tick(store, now=at('2026-11-06T21:59'))['executed'] == []
    assert reports(store) == []
    now = at('2026-11-06T22:01')
    assert tick(store, now=now)['executed'] == ['board']
    assert tick(SQLiteStore(store.db_path), now=now)['executed'] == []
    assert len(calls) == 2
    assert read_state(store)['board'] == now
    assert json.loads(board.load_report(store, '2026-11-06')['stats_json'])['minutes'] == 30
    week = json.loads(board.load_report(store, '2026-11-02', 'week')['stats_json'])
    assert week['minutes'] == 50 and week['count'] == 2
    assert next_runs(store, store.get_settings(), now)['board'] == at('2026-11-07T22:00')
    store.save_settings(replace(store.get_settings(), board_time='23:00'))
    assert tick(store, now=at('2026-11-06T23:10'))['executed'] == []


def test_disabled_board_never_claims(client, store):
    board_only(store, board_enabled=False)
    assert tick(store, now=at('2026-11-06T22:01'), board_job=lambda *a: pytest.fail('disabled'))['executed'] == []
    assert 'board' not in read_state(store)


@pytest.mark.parametrize('generated,finished,error,expected', [
    ('21:00', '20:00', None, True),
    ('19:00', '20:00', None, False),
    ('20:00', '20:00', None, False),
    ('21:00', '20:00', 'request_failed', False),
    ('21:00', None, None, False),
    ('23:00', '20:00', None, False),
])
def test_freshness_after_last_sync(client, store, generated, finished, error, expected):
    with store._connect() as conn:
        conn.execute('INSERT INTO board_reports VALUES (?, ?, ?, ?, ?, ?, ?)',
                     ('2026-11-06', 'day', '{}', GOOD, 'fixture',
                      f'2026-11-06T{generated}:00+08:00', error))
        conn.execute('INSERT INTO sync_runs(started_at,finished_at,status) VALUES (?,?,?)',
                     ('2026-11-06T18:00:00+08:00',
                      f'2026-11-06T{finished}:00+08:00' if finished else None,
                      'success' if finished else 'running'))
    assert board_is_current(store, at('2026-11-06T22:01')) is expected


def test_fresh_report_skip_consumes_daily_slot(client, store, monkeypatch):
    board_only(store)
    board_generation.generate(store, scope='week', day='2026-11-06')
    with store._connect() as conn:
        conn.execute('INSERT INTO board_reports VALUES (?, ?, ?, ?, ?, ?, ?)',
                     ('2026-11-06', 'day', '{}', GOOD, 'fixture', '2026-11-06T13:00:00Z', None))
    result = tick(store, now=at('2026-11-06T22:01'), board_job=lambda *a: pytest.fail('fresh'))
    assert result == {'executed': [], 'failed': [], 'skipped': ['board']}
    assert read_state(store)['board'] == at('2026-11-06T22:01')


def test_model_exception_archived_and_other_jobs_continue(client, store, monkeypatch, caplog):
    add(store)
    store.save_settings(replace(AutomationSettings(), retention_days=90))
    def fail(*args):
        raise RuntimeError('api_key=private-token https://private-provider')
    monkeypatch.setattr(board_analysis, '_analyze', fail)
    monkeypatch.setattr('app.enrichment.has_work', lambda store: True)
    calls = []
    jobs = {name + '_job': lambda *args, name=name: calls.append(name)
            for name in ('sync', 'tag', 'retention', 'enrichment')}
    result = tick(store, now=at('2026-11-06T22:01'), **jobs)
    assert result['failed'] == ['board']
    assert calls == ['sync', 'tag', 'retention', 'enrichment']
    rows = reports(store)
    assert len(rows) == 2
    assert all(row['error'].startswith('analysis_failed') and row['analysis_md'] is None for row in rows)
    assert all(json.loads(row['stats_json'])['count'] == 1 for row in rows)
    assert 'private' not in json.dumps(rows) + caplog.text
    assert tick(store, now=at('2026-11-06T22:01'), **jobs)['failed'] == []


@pytest.mark.parametrize('day', [None, '2026-11-04', '2026-11-02'])
def test_regenerate_default_selected_day_overwrite_and_week(board_client, store, monkeypatch, day):
    selected = day or '2026-11-06'
    add(store, stamp=selected + 'T09:00:00+08:00')
    monkeypatch.setattr(board_analysis, '_analyze', lambda *a: (GOOD, 'fixture', None))
    fields = {'csrf_token': token(board_client)}
    if day: fields['day'] = day
    for minutes in (30, 45):
        with store._connect() as conn:
            conn.execute('UPDATE recordings SET duration_min=?', (minutes,))
        response = board_client.post('/board/regenerate', data={**fields, 'scope': 'day'})
        assert response.status_code == 302
        assert response.headers['location'] == '/board?day=' + selected
        rows = reports(store)
        assert len(rows) == 2
        assert {row['scope'] for row in rows} == {'day', 'week'}
        assert all(json.loads(row['stats_json'])['minutes'] == minutes for row in rows)
        assert board.load_report(store, board.monday_of(selected), 'week')


@pytest.mark.parametrize('mode', ['missing', 'wrong', 'duplicate', 'different-session'])
def test_regenerate_csrf(board_client, store, settings, mode):
    fields = {'csrf_token': token(board_client)}
    if mode == 'missing': fields = {}
    if mode == 'wrong': fields['csrf_token'] = 'wrong'
    if mode == 'duplicate': fields['csrf_token'] = [fields['csrf_token']] * 2
    if mode == 'different-session':
        from app.session import SignedCookie, SESSION_MAX_AGE
        board_client.cookies.set('session', SignedCookie(settings.session_secret, salt='web-session',
                                                        max_age=SESSION_MAX_AGE).encode('other'))
    assert board_client.post('/board/regenerate', data=fields).status_code == 403
    assert reports(store) == []


def test_regenerate_nonadmin_and_anonymous(client, store, fake_auth):
    assert client.post('/board/regenerate').status_code == 403
    user = FeishuUser('ou_board_reader', '读者', '')
    store.add_user(user)
    fake_auth.user = user
    login(client)
    assert client.post('/board/regenerate').status_code == 403


def test_regenerate_error_still_updates_numbers_and_shows_notice(board_client, store, monkeypatch):
    add(store, minutes=45)
    def fail(*args):
        raise RuntimeError('Bearer private-key')
    monkeypatch.setattr(board_analysis, '_analyze', fail)
    response = board_client.post('/board/regenerate', data={'csrf_token': token(board_client)})
    assert response.status_code == 302
    page = board_client.get(response.headers['location'])
    assert page.status_code == 200
    assert '1 场 · 0.8 小时' in page.text
    assert '数字已更新，部分分析未生成' in page.text
    assert 'private-key' not in page.text
    assert all(row['error'].startswith('analysis_failed') for row in reports(store))


@pytest.mark.parametrize('day', ['', 'bad', '20261106', '2026-02-30'])
def test_regenerate_invalid_date(board_client, day):
    assert board_client.post('/board/regenerate', data={
        'csrf_token': token(board_client), 'day': day}).status_code == 400
