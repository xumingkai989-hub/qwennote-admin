"""Durable AI work. SQLite claims and two process-wide file slots survive restarts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import logging
from threading import Event, Thread
from uuid import uuid4

from app.models import resolve_model

NO_MODEL = '未配置模型'
MAX_ATTEMPTS = 3
LEASE_MINUTES = 10
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def migrate(conn):
    # Separate statements preserve the caller's migration transaction.
    statements = (
        """CREATE TABLE IF NOT EXISTS enrichment_tasks (
            id INTEGER PRIMARY KEY, recording_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('tag','hotword')),
            status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, claim_token TEXT,
            UNIQUE(recording_id, kind))""",
        """CREATE TABLE IF NOT EXISTS enrichment_runs (
            id INTEGER PRIMARY KEY, sync_run_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('pending','running','done','partial','failed')),
            total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, finished_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS enrichment_run_tasks (
            run_id INTEGER NOT NULL REFERENCES enrichment_runs(id),
            task_id INTEGER NOT NULL REFERENCES enrichment_tasks(id),
            PRIMARY KEY(run_id, task_id))""",
        'CREATE INDEX IF NOT EXISTS enrichment_pending_idx ON enrichment_tasks(status, id)',
        'CREATE INDEX IF NOT EXISTS enrichment_task_runs_idx ON enrichment_run_tasks(task_id)',
    )
    for statement in statements:
        conn.execute(statement)
    if 'claim_token' not in {r['name'] for r in conn.execute('PRAGMA table_info(enrichment_tasks)')}:
        conn.execute('ALTER TABLE enrichment_tasks ADD COLUMN claim_token TEXT')
    if 'enrichment_run_id' not in {r['name'] for r in conn.execute('PRAGMA table_info(sync_runs)')}:
        conn.execute('ALTER TABLE sync_runs ADD COLUMN enrichment_run_id INTEGER')


def _refresh_runs(conn, stamp):
    rows = conn.execute('''SELECT r.id, count(t.id) total,
        coalesce(sum(t.status='done'),0) done, coalesce(sum(t.status='failed'),0) failed,
        coalesce(sum(t.status='running' OR t.attempts>0),0) begun
        FROM enrichment_runs r LEFT JOIN enrichment_run_tasks m ON m.run_id=r.id
        LEFT JOIN enrichment_tasks t ON t.id=m.task_id GROUP BY r.id''').fetchall()
    for row in rows:
        terminal = row['done'] + row['failed'] == row['total']
        status = (('partial' if row['done'] else 'failed') if row['failed'] else 'done') if terminal else (
            'running' if row['begun'] or row['done'] or row['failed'] else 'pending')
        conn.execute('''UPDATE enrichment_runs SET total=?, done=?, failed=?, status=?,
            finished_at=CASE WHEN ? THEN coalesce(finished_at,?) ELSE NULL END WHERE id=?''',
            (row['total'], row['done'], row['failed'], status, terminal, stamp, row['id']))


def enqueue(store, recording_ids, *, sync_run_id=None, tag_ids=None, hotword_ids=None):
    """One transaction links reusable unique tasks to a durable batch."""
    ids = list(dict.fromkeys(recording_ids))
    tag_ids = set(ids if tag_ids is None else tag_ids)
    hotword_ids = set(ids if hotword_ids is None else hotword_ids)
    if not store.get_settings().tag_enabled:
        tag_ids.clear()
    stamp = now()
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        run_id = conn.execute("INSERT INTO enrichment_runs(sync_run_id,status,started_at) VALUES (?,'pending',?)",
                              (sync_run_id, stamp)).lastrowid
        total = 0
        for recording_id in ids:
            if not conn.execute('SELECT 1 FROM recordings WHERE recording_id=?', (recording_id,)).fetchone():
                continue
            for kind, selected in (('tag', tag_ids), ('hotword', hotword_ids)):
                if recording_id not in selected:
                    continue
                conn.execute('''INSERT INTO enrichment_tasks
                    (recording_id,kind,status,created_at,updated_at) VALUES (?,?,'pending',?,?)
                    ON CONFLICT(recording_id,kind) DO UPDATE SET status='pending',attempts=0,
                    last_error=NULL,claim_token=NULL,updated_at=excluded.updated_at''',
                    (recording_id, kind, stamp, stamp))
                # lastrowid is stale on an upsert's UPDATE branch. Read the
                # unique key in this transaction; supports SQLite before 3.35.
                task_id = conn.execute('SELECT id FROM enrichment_tasks WHERE recording_id=? AND kind=?',
                                       (recording_id, kind)).fetchone()[0]
                conn.execute('INSERT INTO enrichment_run_tasks VALUES (?,?)', (run_id, task_id))
                total += 1
        conn.execute('UPDATE enrichment_runs SET total=? WHERE id=?', (total, run_id))
        if sync_run_id is not None:
            conn.execute('UPDATE sync_runs SET enrichment_run_id=? WHERE id=?', (run_id, sync_run_id))
        # Existing batches may share requeued tasks.
        if total:
            _refresh_runs(conn, stamp)
    return run_id


def recover_running(store):
    stamp = now()
    cutoff = (datetime.fromisoformat(stamp) - timedelta(minutes=LEASE_MINUTES)).isoformat()
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute("""UPDATE enrichment_tasks SET status='pending',claim_token=NULL,updated_at=?
            WHERE status='running' AND julianday(updated_at)<=julianday(?)""", (stamp, cutoff)).rowcount
        _refresh_runs(conn, stamp)
    return count


def has_work(store):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LEASE_MINUTES)).isoformat()
    with store._connect() as conn:
        return bool(conn.execute("""SELECT 1 FROM enrichment_tasks WHERE status='pending'
            OR (status='running' AND julianday(updated_at)<=julianday(?))
            UNION ALL SELECT 1 FROM enrichment_runs WHERE status='pending' AND total=0 LIMIT 1""", (cutoff,)).fetchone())


def _claim(store, task_id):
    stamp = now()
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        claimed = conn.execute("""UPDATE enrichment_tasks SET status='running',updated_at=?,claim_token=?
            WHERE id=? AND status='pending'""", (stamp, uuid4().hex, task_id)).rowcount
        if not claimed:
            return None
        row = conn.execute('SELECT * FROM enrichment_tasks WHERE id=?', (task_id,)).fetchone()
        _refresh_runs(conn, stamp)
        return dict(row)


def _execute(store, task):
    settings = resolve_model(store)
    if not settings or not settings.auth_token:
        return NO_MODEL
    if task['kind'] == 'tag':
        from app.tag import run_tags
        result = run_tags(store, recording_ids=[task['recording_id']], force=True, settings=settings)
        if result['error'] or result['failed'] or result['succeeded'] != 1:
            raise RuntimeError('tag_failed')
    else:
        from app.hotword_judge import run_judgments
        result = run_judgments(store, recording_ids=[task['recording_id']], force=True, settings=settings)
        # A successful judgment may have no replacements to save.
        if len(result) != 1 or result[0]['request_failed']:
            raise RuntimeError('hotword_failed')
    return None


def _process_one(store, task_id):
    # flock slots cap *all* web/timer processes at two, including competing ticks.
    for slot in range(2):
        path = store.db_path.with_suffix(store.db_path.suffix + f'.enrichment-{slot}.lock')
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                task = _claim(store, task_id)
                if not task:
                    return False
                stop_heartbeat = Event()
                def heartbeat():
                    while not stop_heartbeat.wait(30):
                        try:
                            with store._connect() as conn:
                                conn.execute("UPDATE enrichment_tasks SET updated_at=? WHERE id=? AND status='running' AND claim_token=?",
                                             (now(), task_id, task['claim_token']))
                        except Exception:
                            logger.warning('增强任务心跳写入失败')
                heartbeat_thread = Thread(target=heartbeat, daemon=True, name='enrichment-lease')
                heartbeat_thread.start()
                try:
                    error = _execute(store, task)
                except Exception:
                    # Never serialize exception type, text, URL, provider body or prompt.
                    error = '分析失败，请重试'
                    logger.warning('AI 增强任务失败；保留录音，等待重试')
                finally:
                    stop_heartbeat.set()
                    heartbeat_thread.join()
                attempts = task['attempts'] + bool(error)
                status = ('failed' if error == NO_MODEL or attempts >= MAX_ATTEMPTS else 'pending') if error else 'done'
                stamp = now()
                with store._connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    # Re-enqueue invalidates this lease; an older worker cannot
                    # mark a new generation done or overwrite its retry budget.
                    conn.execute('''UPDATE enrichment_tasks SET status=?,attempts=?,last_error=?,updated_at=?,claim_token=NULL
                        WHERE id=? AND status='running' AND claim_token=?''',
                        (status, attempts, error, stamp, task_id, task['claim_token']))
                    _refresh_runs(conn, stamp)
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def process_pending(store, limit=100):
    recover_running(store)
    with store._connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM enrichment_tasks WHERE status='pending' ORDER BY id LIMIT ?", (limit,))]
    # Snapshot once: failed tasks retry on the next tick, rather than spinning.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='enrichment') as pool:
        return sum(pool.map(lambda task_id: _process_one(store, task_id), ids))


def start_background(store):
    def work():
        try:
            process_pending(store)
        except Exception:
            logger.warning('AI 增强后台批次中断；持久任务由定时扫描恢复')
    thread = Thread(target=work, daemon=True, name='enrichment-dispatch')
    thread.start()
    return thread


def retry_failed(store, recording_id=None):
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute("""UPDATE enrichment_tasks SET status='pending',attempts=0,
            last_error=NULL,updated_at=? WHERE status='failed' AND coalesce(last_error,'')<>?
            AND (? IS NULL OR recording_id=?)""", (now(), NO_MODEL, recording_id, recording_id)).rowcount
        _refresh_runs(conn, now())
    return count


def record_state(store, recording_id, *, conn=None):
    if conn is None:
        with store._connect() as connection:
            return record_state(store, recording_id, conn=connection)
    tasks = conn.execute('SELECT kind,status,last_error FROM enrichment_tasks WHERE recording_id=?', (recording_id,)).fetchall()
    result = {'recording_id': recording_id}
    for task in tasks:
        kind = task['kind']
        result[kind] = task['status']
        result[kind + '_error'] = task['last_error']
        result[kind + '_retryable'] = task['status'] == 'failed' and task['last_error'] != NO_MODEL
    return result


def status(store, run_id):
    with store._connect() as conn:
        conn.execute('BEGIN')
        run = conn.execute('SELECT id AS run,status,total,done,failed FROM enrichment_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return None
        ids = [r[0] for r in conn.execute('''SELECT DISTINCT t.recording_id FROM enrichment_run_tasks m
            JOIN enrichment_tasks t ON t.id=m.task_id WHERE m.run_id=? ORDER BY t.recording_id''', (run_id,))]
        result = {**dict(run), 'records': [record_state(store, rid, conn=conn) for rid in ids]}
        for record in result['records']:
            record['tags'] = [r[0] for r in conn.execute('SELECT tag FROM recording_tags WHERE recording_id=? ORDER BY position', (record['recording_id'],))]
    # Only presentation data; no model credentials, prompts or exceptions.
    for record in result['records']:
        if store.get_recording(record['recording_id']):
            record['hotword_count'] = store.hotword_presentation(record['recording_id'])['count']
    return result


def runs_for_records(store, ids):
    ids = list(ids)
    if not ids:
        return []
    with store._connect() as conn:
        return [r[0] for r in conn.execute('''SELECT DISTINCT m.run_id FROM enrichment_run_tasks m
            JOIN enrichment_tasks t ON t.id=m.task_id WHERE t.recording_id IN (''' + ','.join('?' for _ in ids) +
            ") AND t.status IN ('pending','running')", ids)]
