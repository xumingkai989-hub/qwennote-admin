from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from unittest.mock import Mock

import pytest

from app import login_sync
from app.feishu import FeishuUser
from app.store import SQLiteStore
from app.sync import SyncRun
from test_login import login


@pytest.fixture
def ready_store(store):
    store.migrate()
    return store


def dispatch(thread):
    kwargs = thread.call_args.kwargs
    kwargs['target'](*kwargs['args'])


def test_first_login_reserves_before_daemon_dispatch(ready_store, controlled_login_sync_dispatch, monkeypatch):
    source = object()
    run = Mock(return_value=SyncRun(1, '', '', 'success'))
    monkeypatch.setattr(login_sync, 'run_sync', run)
    def start():
        assert ready_store.get_setting('login_sync_at')
        dispatch(controlled_login_sync_dispatch)
    controlled_login_sync_dispatch.return_value.start.side_effect = start
    assert login_sync.maybe_trigger_login_sync(ready_store, source)
    assert controlled_login_sync_dispatch.call_args.kwargs['daemon'] is True
    run.assert_called_once_with(source, ready_store, backfill_days=90, full=False)


@pytest.mark.parametrize('minutes,expected', [(30, False), (55, False), (60, True), (61, True), (-5, False)])
def test_hour_window(ready_store, monkeypatch, controlled_login_sync_dispatch, minutes, expected):
    now = datetime.now(timezone.utc)
    class Clock:
        @staticmethod
        def now(zone): return now
        fromisoformat = datetime.fromisoformat
    monkeypatch.setattr(login_sync, 'datetime', Clock)
    old = (now - timedelta(minutes=minutes)).isoformat()
    ready_store.set_setting('login_sync_at', old)
    run = Mock(return_value=SyncRun(1, '', '', 'success'))
    monkeypatch.setattr(login_sync, 'run_sync', run)
    assert login_sync.maybe_trigger_login_sync(ready_store, object()) is expected
    if expected:
        dispatch(controlled_login_sync_dispatch)
        run.assert_called_once()
        assert ready_store.get_setting('login_sync_at') == now.isoformat()
    else:
        run.assert_not_called()
        controlled_login_sync_dispatch.assert_not_called()
        assert ready_store.get_setting('login_sync_at') == old


def test_concurrent_connections_reserve_only_once(ready_store, controlled_login_sync_dispatch):
    # Real SQLite transaction contention verifies the guarantee across store instances.
    barrier = Barrier(2)
    def trigger(_):
        barrier.wait(timeout=5)
        return login_sync.maybe_trigger_login_sync(SQLiteStore(ready_store.db_path), object())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(trigger, range(2))) == [False, True]
    controlled_login_sync_dispatch.assert_called_once()


@pytest.mark.parametrize('value', ['invalid', '2026-01-01T00:00:00', 123])
def test_invalid_timestamp_reestablishes_window(ready_store, value):
    ready_store.set_setting('login_sync_at', value)
    assert login_sync.maybe_trigger_login_sync(ready_store, object())
    assert datetime.fromisoformat(ready_store.get_setting('login_sync_at')).tzinfo


def test_settings_save_preserves_login_window_and_backfill(ready_store, monkeypatch, controlled_login_sync_dispatch):
    ready_store.save_settings(replace(ready_store.get_settings(), backfill_days=17))
    run = Mock(return_value=SyncRun(1, '', '', 'success'))
    monkeypatch.setattr(login_sync, 'run_sync', run)
    assert login_sync.maybe_trigger_login_sync(ready_store, object(), backfill_days=31)
    stamp = ready_store.get_setting('login_sync_at')
    dispatch(controlled_login_sync_dispatch)
    assert run.call_args.kwargs == {'backfill_days': 17, 'full': False}
    ready_store.save_settings(ready_store.get_settings())
    assert ready_store.get_setting('login_sync_at') == stamp


@pytest.mark.parametrize('failure', ['exception', 'result', 'thread'])
def test_sync_failure_never_fails_login(client, store, monkeypatch, controlled_login_sync_dispatch, caplog, failure):
    run = Mock(side_effect=RuntimeError('secret-upstream-value')) if failure == 'exception' else Mock(
        return_value=SyncRun(1, '', '', 'failed'))
    monkeypatch.setattr(login_sync, 'run_sync', run)
    if failure == 'thread':
        controlled_login_sync_dispatch.return_value.start.side_effect = RuntimeError('secret-upstream-value')
    response = login(client)
    assert response.status_code == 302 and response.cookies.get('session')
    stamp = store.get_setting('login_sync_at')
    if failure != 'thread': dispatch(controlled_login_sync_dispatch)
    assert store.get_setting('login_sync_at') == stamp
    assert '失败' in caplog.text and 'secret-upstream-value' not in caplog.text
    client.cookies.clear()
    assert login(client).status_code == 302
    controlled_login_sync_dispatch.assert_called_once()


def test_transaction_failure_does_not_fail_login(client, store, monkeypatch):
    monkeypatch.setattr(store, 'set_setting', Mock(side_effect=RuntimeError()))
    assert login(client).status_code == 302
    assert store.get_setting('login_sync_at') is None


def test_different_non_admin_login_shares_global_window(client, fake_auth, store, controlled_login_sync_dispatch):
    assert login(client).status_code == 302
    stamp = store.get_setting('login_sync_at')
    fake_auth.user = FeishuUser('regular-user', '普通用户', '')
    store.seed_admin('regular-user')  # Whitelist insertion; admin identity remains config-only.
    client.cookies.clear()
    assert login(client).status_code == 302
    assert store.get_setting('login_sync_at') == stamp
    controlled_login_sync_dispatch.assert_called_once()


def test_non_admin_can_trigger_first_login(client, fake_auth, store, controlled_login_sync_dispatch):
    fake_auth.user = FeishuUser('regular-user', '普通用户', '')
    store.seed_admin('regular-user')
    assert login(client).status_code == 302
    assert store.get_setting('login_sync_at')
    controlled_login_sync_dispatch.assert_called_once()


@pytest.mark.parametrize('revoked', [False, True])
def test_denied_login_never_dispatches(client, fake_auth, store, controlled_login_sync_dispatch, revoked):
    if revoked:
        with store._connect() as conn: conn.execute('UPDATE users SET is_active=0')
    else:
        fake_auth.user = FeishuUser('outsider', '无权限', '')
    assert login(client).status_code == 403
    assert store.get_setting('login_sync_at') is None
    controlled_login_sync_dispatch.assert_not_called()


def test_running_scheduler_silently_consumes_window(ready_store, controlled_login_sync_dispatch, caplog):
    with ready_store.sync_lock():
        ready_store.start_sync(datetime.now(timezone.utc).isoformat())
        assert login_sync.maybe_trigger_login_sync(ready_store, object())
        dispatch(controlled_login_sync_dispatch)
    assert ready_store.get_setting('login_sync_at')
    assert caplog.text == ''
    assert ready_store.latest_sync().id == 1


def test_callback_returns_while_real_worker_is_blocked(client, store, monkeypatch):
    entered, release, completed = Event(), Event(), Event()
    def slow(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            return SyncRun(1, '', '', 'success')
        finally: completed.set()
    monkeypatch.setattr(login_sync, 'Thread', Thread)
    monkeypatch.setattr(login_sync, 'run_sync', slow)
    try:
        response = login(client)
        assert entered.wait(2)
        assert response.status_code == 302 and not completed.is_set()
        assert client.get('/').status_code == 200
    finally:
        release.set()
        assert completed.wait(5)


def test_fixture_source_runs_in_worker(client, store, controlled_login_sync_dispatch):
    assert login(client).status_code == 302
    dispatch(controlled_login_sync_dispatch)
    assert store.latest_sync().status == 'success'


def test_sync_status_auth_and_lock(client, store):
    assert client.get('/api/sync/status').status_code == 401
    login(client)
    response = client.get('/api/sync/status')
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'running': False, 'last': None}
    with store.sync_lock():
        store.start_sync(datetime.now(timezone.utc).isoformat())
        assert client.get('/api/sync/status').json() == {
            'running': True, 'last': {'mode': 'quick', 'status': 'running', 'finished_at': None}}
    assert client.get('/api/sync/status').json()['running'] is False
    with store._connect() as conn: conn.execute('UPDATE users SET is_active=0')
    assert client.get('/api/sync/status').status_code == 403


def test_home_only_polls_when_lock_and_running_row_exist(client, store):
    login(client)
    with store.sync_lock():
        assert 'login-sync.js' not in client.get('/').text
        store.start_sync(datetime.now(timezone.utc).isoformat())
        html = client.get('/').text
        assert '正在同步…' in html and '正在同步，稍后自动更新' in html
        assert 'login-sync.js' in html
        assert 'login-sync.js' in client.get('/search?q=test').text
    html = client.get('/').text
    assert 'login-sync.js' not in html and '正在同步…' not in html
