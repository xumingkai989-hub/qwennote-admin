"""Ten-minute systemd entry point; schedule decisions always use Asia/Shanghai."""
import fcntl
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

from app.minutes import SHANGHAI
from app.sync import SyncInProgress

logger = logging.getLogger(__name__)


def local_time(value):
    if value.tzinfo is None:
        raise ValueError('scheduler clock must be timezone-aware')
    return value.astimezone(SHANGHAI)


def read_state(store):
    with store._connect() as conn:
        return {row['job']: local_time(datetime.fromisoformat(row['last_run_at']))
                for row in conn.execute('SELECT job, last_run_at FROM scheduler_state')
                if row['last_run_at']}


def scheduled_time(job, settings, now, last):
    """Return the next due instant, including today's unclaimed overdue work.

    Intervals are measured from the last claimed Shanghai hour, across midnight.
    On first enabling an interval, the current hour is eligible immediately.
    Daily jobs do not replay older days, or repeat after a same-day time edit.
    """
    now = local_time(now)
    if job == 'sync' and not settings.sync_enabled:
        return None
    if job == 'tag' and not settings.tag_enabled:
        return None
    if job == 'retention' and not settings.retention_days:
        return None
    if job == 'sync' and settings.sync_interval_hours:
        return (last.replace(minute=0, second=0, microsecond=0)
                + timedelta(hours=settings.sync_interval_hours) if last else
                now.replace(minute=0, second=0, microsecond=0))
    text = {'sync': settings.sync_time, 'tag': settings.tag_time, 'retention': '00:00'}[job]
    hour, minute = map(int, text.split(':'))
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if last and last.date() >= now.date():
        due = due.replace(year=last.year, month=last.month, day=last.day) + timedelta(days=1)
    return due


def next_runs(store, settings, now):
    now = local_time(now)
    state = read_state(store)
    result = {}
    for job in ('sync', 'tag', 'retention'):
        due = scheduled_time(job, settings, now, state.get(job))
        if due is not None:
            due = max(due, now)
            # The timer wakes on wall-clock multiples of ten minutes.
            rounded = due.replace(second=0, microsecond=0)
            if due > rounded:
                rounded += timedelta(minutes=1)
            due = rounded + timedelta(minutes=(-rounded.minute) % 10)
        result[job] = due
    return result


@contextmanager
def tick_lock(store):
    with store.db_path.with_suffix(store.db_path.suffix + '.scheduler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def retain_recordings(store, days, now):
    if not days:
        return 0
    # No possible recording predates year 1. Avoid overflowing datetime for a
    # deliberately large retention value accepted by the settings form.
    if days >= now.toordinal():
        return 0
    cutoff = (now - timedelta(days=days)).isoformat()
    # Same lock order as sync's optional tagging; no writers race the deletion.
    with store.sync_lock(), store.tag_lock(), store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM recording_overrides WHERE recording_id IN '
                     '(SELECT recording_id FROM recordings WHERE julianday(created_at) < julianday(?))',
                     (cutoff,))
        deleted = conn.execute('DELETE FROM recordings WHERE julianday(created_at) < julianday(?)',
                               (cutoff,)).rowcount
        # recording_tags uses ON DELETE CASCADE; existing FTS triggers delete its index.
        from app.tag_vocabulary import refresh_usage
        refresh_usage(conn)
    return deleted


def _sync(store, preferences):
    from scripts.deployment_runtime import deployment_settings
    from app.minutes import FixtureMinutesSource, HttpMinutesSource
    from app.mcp import McpMinutesSource
    from app.sync import run_sync
    config = deployment_settings()
    source = (FixtureMinutesSource(config.minutes_fixture_dir) if config.minutes_source == 'fixture'
              else McpMinutesSource() if config.minutes_source == 'mcp' else HttpMinutesSource())
    result = run_sync(source, store, backfill_days=preferences.backfill_days)
    if result.status != 'success':
        raise RuntimeError('sync_failed')


def _tag(store, preferences):
    from app.tag_maintenance import run_maintenance
    result = run_maintenance(store)
    if result['failed']:
        raise RuntimeError('tag_maintenance_failed')


def tick(store, *, now=None, sync_job=None, tag_job=None, retention_job=None):
    now = local_time(now if now is not None else datetime.now(SHANGHAI))
    result = {'executed': [], 'failed': [], 'skipped': []}
    try:
        with tick_lock(store):
            preferences = store.get_settings()
            state = read_state(store)
            for job, callback in (
                ('sync', sync_job or _sync), ('tag', tag_job or _tag),
                ('retention', retention_job or
                 (lambda store, settings: retain_recordings(store, settings.retention_days, now))),
            ):
                due = scheduled_time(job, preferences, now, state.get(job))
                if due is None or due > now:
                    continue
                # Commit the claim BEFORE external effects. A crash consumes the
                # slot rather than replaying network calls/deletions after reboot.
                with store._connect() as conn:
                    conn.execute('INSERT INTO scheduler_state (job, last_run_at) VALUES (?, ?) '
                                 'ON CONFLICT(job) DO UPDATE SET last_run_at=excluded.last_run_at',
                                 (job, now.isoformat()))
                try:
                    callback(store, preferences)
                except SyncInProgress:
                    # No work started; the manual sync owns the lock. Retry next tick.
                    with store._connect() as conn:
                        if job in state:
                            conn.execute('UPDATE scheduler_state SET last_run_at=? WHERE job=?',
                                         (state[job].isoformat(), job))
                        else:
                            conn.execute('DELETE FROM scheduler_state WHERE job=?', (job,))
                    result['skipped'].append(job)
                except Exception:
                    logger.error('定时任务 %s 失败；请检查运行环境，下个周期再执行', job)
                    result['failed'].append(job)
                else:
                    result['executed'].append(job)
    except BlockingIOError:
        result['skipped'].append('tick_locked')
    return result


def main():
    import os
    from pathlib import Path
    from app.store import SQLiteStore
    path = os.environ.get('DB_PATH', '').strip()
    if not path:
        logger.error('DB_PATH is required')
        return 1
    store = SQLiteStore(Path(path).expanduser())
    store.migrate()
    result = tick(store)
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result['failed']))


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
