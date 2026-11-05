"""Globally debounce login syncs; reserve the window before dispatching work."""
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread

from app import enrichment
from app.sync import SyncInProgress, run_sync

LOGIN_SYNC_INTERVAL = timedelta(hours=1)
logger = logging.getLogger(__name__)


def maybe_trigger_login_sync(store, source, *, backfill_days=90) -> bool:
    """距上次触发不足 1 小时则跳过；否则启动后台线程并返回 True。"""
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now = datetime.now(timezone.utc)
        value = store.get_setting('login_sync_at', conn=conn)
        if value is not None:
            try:
                last = datetime.fromisoformat(value)
                if last.tzinfo is None:
                    raise ValueError('Timezone required')
            except (TypeError, ValueError):
                logger.warning('登录同步时间无效，重新建立窗口')
            else:
                if now - last < LOGIN_SYNC_INTERVAL:
                    return False
        store.set_setting('login_sync_at', now.isoformat(), conn=conn)
    # Commit before starting a thread. Failed dispatches also consume the window.
    Thread(target=_sync, args=(store, source, backfill_days),
           name='login-sync', daemon=True).start()
    return True


def _sync(store, source, backfill_days):
    try:
        backfill = store.get_settings(backfill_days=backfill_days).backfill_days
        run = run_sync(source, store, backfill_days=backfill, full=False)
        if run.status != 'success':
            logger.warning('登录后同步失败；将在下一个登录同步窗口重试')
        if run.enrichment_run_id:
            enrichment.start_background(store)
    except SyncInProgress:
        pass
    except Exception:
        # Exception text may contain upstream credentials; never log it.
        logger.warning('登录后同步失败；将在下一个登录同步窗口重试')


def sync_status(store):
    """A stale running row alone must not keep browsers polling forever."""
    last = store.latest_sync()
    running = False
    if last and last.status == 'running':
        try:
            with store.sync_lock():
                last = store.latest_sync()
        except SyncInProgress:
            last = store.latest_sync()
            running = bool(last and last.status == 'running')
    return {'running': running, 'last': None if last is None else {
        'mode': last.mode, 'status': last.status, 'finished_at': last.finished_at}}
