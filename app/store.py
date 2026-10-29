import fcntl
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import SyncInProgress, SyncRun, SyncStore


@dataclass(frozen=True)
class User:
    open_id: str
    name: str
    avatar_url: str
    is_active: bool
    added_at: str


@dataclass(frozen=True)
class RecordingCard:
    recording_id: str
    title: str
    created_at: datetime
    duration_min: int | None
    has_transcript: bool
    has_summary: bool

    @property
    def status(self) -> tuple[str, str]:
        return {
            (True, True): ('complete', '已转录'),
            (True, False): ('transcript-only', '仅转写'),
            (False, True): ('summary-only', '仅总结'),
            (False, False): ('empty', '未转录'),
        }[self.has_transcript, self.has_summary]


@dataclass(frozen=True)
class StoredRecording(RecordingCard):
    summary_md: str
    transcript_md: str


@dataclass(frozen=True)
class RecordingPage:
    items: list[RecordingCard]
    total: int
    page: int
    pages: int


class Store(SyncStore, Protocol):
    def get_recording(self, recording_id: str) -> StoredRecording | None: ...

    def migrate(self) -> None: ...

    def seed_admin(self, open_id: str) -> None: ...

    def get_user(self, open_id: str) -> User | None: ...

    def update_profile(self, user: FeishuUser) -> None: ...

    def list_recordings(self, start: date, end: date, page: int = 1) -> RecordingPage: ...

    def search_recordings(self, query: str, start: date | None = None,
                          end: date | None = None, page: int = 1) -> RecordingPage: ...


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    open_id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    duration_min INTEGER,
                    summary_md TEXT,
                    transcript_md TEXT,
                    has_transcript INTEGER NOT NULL DEFAULT 0 CHECK (has_transcript IN (0, 1)),
                    has_summary INTEGER NOT NULL DEFAULT 0 CHECK (has_summary IN (0, 1)),
                    owner_open_id TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
                    fetched INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    updated INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
            """)
            # Work order 01 used NOT NULL on optional values. Rebuild only that
            # schema, preserving every existing recording and its owner.
            columns = conn.execute("PRAGMA table_info(recordings)").fetchall()
            if any(row['name'] == 'duration_min' and row['notnull'] for row in columns):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""
                    CREATE TABLE recordings_nullable (
                        recording_id TEXT PRIMARY KEY NOT NULL,
                        title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                        duration_min INTEGER, summary_md TEXT, transcript_md TEXT,
                        has_transcript INTEGER NOT NULL DEFAULT 0 CHECK (has_transcript IN (0, 1)),
                        has_summary INTEGER NOT NULL DEFAULT 0 CHECK (has_summary IN (0, 1)),
                        owner_open_id TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL
                    )
                """)
                conn.execute("INSERT INTO recordings_nullable SELECT * FROM recordings")
                conn.execute("DROP TABLE recordings")
                conn.execute("ALTER TABLE recordings_nullable RENAME TO recordings")
            conn.execute('CREATE INDEX IF NOT EXISTS recordings_created_at_idx '
                         'ON recordings (created_at DESC, recording_id DESC)')
            # Keep the schema change, backfill and index creation in one write
            # transaction. Existing databases need no export/import.
            if not conn.in_transaction:
                conn.execute('BEGIN IMMEDIATE')
            columns = conn.execute('PRAGMA table_info(recordings)').fetchall()
            new_column = not any(row['name'] == 'search_text' for row in columns)
            new_index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recordings_fts'",
            ).fetchone() is None
            if new_column:
                conn.execute("ALTER TABLE recordings ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
                self._backfill_search_text(conn)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS recordings_fts USING fts5(
                    search_text, content='recordings', content_rowid='rowid', tokenize='trigram'
                )
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS recordings_fts_insert AFTER INSERT ON recordings BEGIN
                    INSERT INTO recordings_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS recordings_fts_delete AFTER DELETE ON recordings BEGIN
                    INSERT INTO recordings_fts(recordings_fts, rowid, search_text)
                    VALUES ('delete', old.rowid, old.search_text);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS recordings_fts_update AFTER UPDATE OF search_text ON recordings
                WHEN old.search_text IS NOT new.search_text BEGIN
                    INSERT INTO recordings_fts(recordings_fts, rowid, search_text)
                    VALUES ('delete', old.rowid, old.search_text);
                    INSERT INTO recordings_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
                END
            """)
            if new_column or new_index:
                conn.execute("INSERT INTO recordings_fts(recordings_fts) VALUES ('rebuild')")

    @staticmethod
    def _backfill_search_text(conn: sqlite3.Connection) -> None:
        conn.execute("""
            UPDATE recordings SET search_text =
                coalesce(title, '') || char(10) || coalesce(summary_md, '') || char(10) || coalesce(transcript_md, '')
        """)

    def rebuild_search(self) -> None:
        self.migrate()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._backfill_search_text(conn)
            conn.execute("INSERT INTO recordings_fts(recordings_fts) VALUES ('rebuild')")

    def search_recordings(self, query: str, start: date | None = None,
                          end: date | None = None, page: int = 1) -> RecordingPage:
        if not query:
            return RecordingPage([], 0, 1, 1)
        if len(query) >= 3:
            # Quoting the entire phrase makes operators and quotes literal.
            where = 'rowid IN (SELECT rowid FROM recordings_fts WHERE recordings_fts MATCH ?)'
            params = ['"' + query.replace('"', '""') + '"']
        else:
            # Trigram MATCH cannot find 1–2 character queries. Escape LIKE
            # metacharacters so %, _ and backslash remain literal keywords.
            where = "search_text LIKE ? ESCAPE '\\'"
            params = ['%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%']
        if start is not None:
            where += ' AND created_at >= ?'
            params.append(start.isoformat())
        if end is not None:
            where += ' AND created_at <= ?'
            params.append(end.isoformat() + 'T23:59:59.999999+08:00')
        with self._connect() as conn:
            conn.execute('BEGIN')
            # Membership selects each recording once, including matches in
            # several fields. Both paths share sorting and pagination.
            total = conn.execute(f'SELECT COUNT(*) FROM recordings WHERE {where}', params).fetchone()[0]
            pages = max(1, (total + 19) // 20)
            page = min(max(1, page), pages)
            rows = conn.execute(
                'SELECT recording_id, title, created_at, duration_min, has_transcript, has_summary, '
                f'summary_md, transcript_md FROM recordings WHERE {where} '
                'ORDER BY created_at DESC, recording_id DESC LIMIT 20 OFFSET ?',
                (*params, (page - 1) * 20),
            ).fetchall()
        return RecordingPage([
            StoredRecording(row['recording_id'], row['title'], datetime.fromisoformat(row['created_at']),
                            row['duration_min'], bool(row['has_transcript']), bool(row['has_summary']),
                            row['summary_md'] or '', row['transcript_md'] or '')
            for row in rows
        ], total, page, pages)

    def list_recordings(self, start: date, end: date, page: int = 1) -> RecordingPage:
        # Stored timestamps already have Shanghai wall-clock semantics. Do not
        # use SQLite date()/datetime(): they would shift +08:00 values to UTC.
        bounds = (start.isoformat(), end.isoformat() + 'T23:59:59.999999+08:00')
        where = 'created_at >= ? AND created_at <= ?'
        with self._connect() as conn:
            # Count and items share a read snapshot if a sync commits meanwhile.
            conn.execute('BEGIN')
            total = conn.execute(f'SELECT COUNT(*) FROM recordings WHERE {where}', bounds).fetchone()[0]
            pages = max(1, (total + 19) // 20)
            page = min(max(1, page), pages)
            rows = conn.execute(
                'SELECT recording_id, title, created_at, duration_min, has_transcript, has_summary '
                f'FROM recordings WHERE {where} '
                'ORDER BY created_at DESC, recording_id DESC LIMIT 20 OFFSET ?',
                (*bounds, (page - 1) * 20),
            ).fetchall()
        return RecordingPage([
            RecordingCard(row['recording_id'], row['title'], datetime.fromisoformat(row['created_at']),
                          row['duration_min'], bool(row['has_transcript']), bool(row['has_summary']))
            for row in rows
        ], total, page, pages)

    @contextmanager
    def sync_lock(self) -> Iterator[None]:
        # OS releases flock on process exit; works across app instances/workers.
        # Keep the lock file: unlinking it can let a second inode bypass flock.
        with self.db_path.with_suffix(self.db_path.suffix + '.sync.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncInProgress from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def start_sync(self, started_at: str) -> int:
        with self._connect() as conn:
            # With the OS lock held, any running row belongs to an interrupted
            # process. Retain it as a failed batch so a restart can safely retry.
            conn.execute(
                "UPDATE sync_runs SET status = 'failed', finished_at = ?, error = ? WHERE status = 'running'",
                (started_at, '上次同步进程中断，请重试'),
            )
            cursor = conn.execute(
                "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (started_at,),
            )
            return cursor.lastrowid

    def finish_sync(self, run: SyncRun) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, fetched = ?, inserted = ?, updated = ?, error = ? WHERE id = ?",
                (run.finished_at, run.status, run.fetched, run.inserted, run.updated, run.error, run.id),
            )

    def latest_sync(self) -> SyncRun | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1').fetchone()
            return SyncRun(**dict(row)) if row else None

    def upsert_recording(self, meta: RecordingMeta, detail: RecordingDetail, synced_at: str) -> str:
        if detail.recording_id != meta.recording_id or not meta.recording_id or meta.created_at.tzinfo is None:
            raise ValueError('Invalid recording identity or timestamp')
        fields = ('title', 'created_at', 'duration_min', 'summary_md', 'transcript_md', 'has_transcript', 'has_summary',
                  'search_text')
        values = (meta.title, meta.created_at.astimezone(SHANGHAI).isoformat(), meta.duration_min,
                  detail.summary_md, detail.transcript_md, int(detail.has_transcript), int(detail.has_summary),
                  '\n'.join((meta.title or '', detail.summary_md or '', detail.transcript_md or '')))
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM recordings WHERE recording_id = ?', (meta.recording_id,)).fetchone()
            if old is not None and tuple(old[field] for field in fields) == values:
                conn.execute('UPDATE recordings SET synced_at = ? WHERE recording_id = ?', (synced_at, meta.recording_id))
                return 'unchanged'
            conn.execute(
                'INSERT INTO recordings (recording_id, title, created_at, duration_min, summary_md, transcript_md, '
                'has_transcript, has_summary, search_text, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(recording_id) DO UPDATE SET '
                'title = excluded.title, created_at = excluded.created_at, duration_min = excluded.duration_min, '
                'summary_md = excluded.summary_md, transcript_md = excluded.transcript_md, '
                'has_transcript = excluded.has_transcript, has_summary = excluded.has_summary, '
                'search_text = excluded.search_text, synced_at = excluded.synced_at',
                (meta.recording_id, *values, synced_at),
            )
            return 'updated' if old else 'inserted'

    def get_recording(self, recording_id: str) -> StoredRecording | None:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT recording_id, title, created_at, duration_min, has_transcript, has_summary, '
                'summary_md, transcript_md FROM recordings WHERE recording_id = ?', (recording_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredRecording(
            recording_id=row['recording_id'], title=row['title'],
            created_at=datetime.fromisoformat(row['created_at']).astimezone(SHANGHAI),
            duration_min=row['duration_min'], has_transcript=bool(row['has_transcript']),
            has_summary=bool(row['has_summary']), summary_md=row['summary_md'] or '',
            transcript_md=row['transcript_md'] or '',
        )

    def seed_admin(self, open_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (open_id, name, avatar_url, is_active, added_at) "
                "VALUES (?, ?, ?, 1, ?) ON CONFLICT(open_id) DO NOTHING",
                (open_id, "管理员", "", datetime.now(timezone.utc).isoformat()),
            )

    def get_user(self, open_id: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE open_id = ?", (open_id,)).fetchone()
        if row is None:
            return None
        return User(row["open_id"], row["name"], row["avatar_url"], bool(row["is_active"]), row["added_at"])

    def update_profile(self, user: FeishuUser) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET name = ?, avatar_url = ? WHERE open_id = ? AND is_active = 1",
                (user.name, user.avatar_url, user.open_id),
            )
