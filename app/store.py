import fcntl
import hmac
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import SyncInProgress, SyncRun, SyncStore


@dataclass(frozen=True)
class User:
    subject: str
    name: str
    avatar_url: str
    is_active: bool
    added_at: str
    union_id: str | None = None
    open_id: str | None = None


@dataclass(frozen=True)
class RecordingCard:
    recording_id: str
    title: str
    created_at: datetime
    duration_min: int | None
    has_transcript: bool
    has_summary: bool
    tags: tuple[str, ...] = field(default=(), kw_only=True)

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


@dataclass(frozen=True)
class RecordingBatch:
    items: list[StoredRecording]
    total: int


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str
    created_at: str
    revoked_at: str | None
    last_used_at: str | None


@dataclass(frozen=True)
class Hotword:
    id: int
    term: str
    aliases: str
    note: str
    enabled: bool
    hits: int
    created_at: str
    updated_at: str
    applied_records: int = 0


class Store(SyncStore, Protocol):
    def hotword_presentation(self, recording_id: str) -> dict: ...

    def hotword_decisions(self, recording_id: str) -> list[dict]: ...

    def get_overrides(self, recording_id: str) -> dict[str, dict]: ...

    def save_override(self, recording_id: str, field: str, content: str) -> str: ...

    def revert_override(self, recording_id: str, field: str) -> None: ...

    def bulk_add_hotwords(self, items: list[dict]) -> dict[str, int]: ...

    def apply_hotwords(self, text: str, *, recording_id=None, field=None, markdown: bool = True) -> str: ...

    def list_hotwords(self) -> list[Hotword]: ...

    def add_hotword(self, term: str, aliases: str = '', note: str = '') -> None: ...

    def update_hotword(self, hotword_id: int, term: str, aliases: str, note: str) -> bool: ...

    def delete_hotword(self, hotword_id: int) -> bool: ...

    def toggle_hotword(self, hotword_id: int) -> bool: ...

    def create_api_key(self, name: str, key_hash: str) -> ApiKey: ...

    def list_api_keys(self) -> list[ApiKey]: ...

    def revoke_api_key(self, key_id: int) -> bool: ...

    def authenticate_api_key(self, key_hash: str) -> tuple[int | None, bool]: ...

    def audit_api_call(self, key_id: int | None, path: str, status_code: int) -> None: ...

    def read_recordings(self, start: date | None = None, end: date | None = None,
                        *, limit: int | None = 20, offset: int = 0,
                        query: str | None = None) -> RecordingBatch: ...

    def get_recording(self, recording_id: str) -> StoredRecording | None: ...

    def migrate(self) -> None: ...

    def seed_admin(self, subject: str) -> None: ...

    def get_user(self, subject: str, other_id: str | None = None) -> User | None: ...

    def list_users(self) -> list[User]: ...

    def add_user(self, user: FeishuUser) -> None: ...

    def remove_user(self, subject: str) -> None: ...

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
        conn.execute("PRAGMA foreign_keys = ON")
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
                    subject TEXT PRIMARY KEY NOT NULL,
                    union_id TEXT,
                    open_id TEXT,
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
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS api_audit (
                    id INTEGER PRIMARY KEY,
                    key_id INTEGER REFERENCES api_keys(id),
                    path TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recording_overrides (
                    recording_id TEXT NOT NULL,
                    field TEXT NOT NULL CHECK (field IN ('summary','transcript')),
                    content_md TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (recording_id, field)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_state (
                    job TEXT PRIMARY KEY,
                    last_run_at TEXT
                );
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL CHECK (provider IN ('anthropic', 'openai', 'deepseek')),
                    base_url TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    api_key_enc TEXT NOT NULL,
                    key_last4 TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (is_default = 0 OR enabled = 1)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS models_one_default ON models(is_default) WHERE is_default = 1;
            """)
            # Rebuild the legacy primary key atomically. Its ID type is unknown:
            # retain the original value in both subject and open_id, without guessing.
            conn.execute('BEGIN IMMEDIATE')
            from app.hotword_migration import migrate_hotwords
            migrate_hotwords(conn)
            user_columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
            if 'subject' not in user_columns:
                conn.execute("""
                    CREATE TABLE users_identity (
                        subject TEXT PRIMARY KEY NOT NULL, union_id TEXT, open_id TEXT,
                        name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
                        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                        added_at TEXT NOT NULL
                    )
                """)
                conn.execute('INSERT INTO users_identity '
                             '(subject, open_id, name, avatar_url, is_active, added_at) '
                             'SELECT open_id, open_id, name, avatar_url, is_active, added_at FROM users')
                conn.execute('DROP TABLE users')
                conn.execute('ALTER TABLE users_identity RENAME TO users')
            # Work order 01 used NOT NULL on optional values. Rebuild only that
            # schema, preserving every existing recording and its owner.
            columns = conn.execute("PRAGMA table_info(recordings)").fetchall()
            if any(row['name'] == 'duration_min' and row['notnull'] for row in columns):
                if not conn.in_transaction:
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recording_tags (
                    recording_id TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
                    tag TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (recording_id, tag)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tag_runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    attempted INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0, error TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0
                )
            """)
            from app.tag_vocabulary import migrate as migrate_vocabulary
            migrate_vocabulary(conn)
            if 'media_type' not in {row['name'] for row in conn.execute('PRAGMA table_info(recordings)')}:
                conn.execute('ALTER TABLE recordings ADD COLUMN media_type TEXT')
            if 'refreshed_token' not in {row['name'] for row in conn.execute('PRAGMA table_info(sync_runs)')}:
                conn.execute('ALTER TABLE sync_runs ADD COLUMN refreshed_token INTEGER NOT NULL DEFAULT 0 CHECK (refreshed_token IN (0, 1))')
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

    def get_settings(self, *, backfill_days=90):
        from app.automation import AutomationSettings
        values = AutomationSettings(backfill_days=backfill_days).values()
        with self._connect() as conn:
            values.update({row['key']: json.loads(row['value']) for row in
                           conn.execute('SELECT key, value FROM settings') if row['key'] in values})
        return AutomationSettings(**values)

    def save_settings(self, values):
        from app.automation import AutomationSettings
        # Validate the entire snapshot before opening a write transaction.
        validated = AutomationSettings(**values.values())
        with self._connect() as conn:
            conn.executemany('INSERT INTO settings (key, value) VALUES (?, ?) '
                             'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             [(key, json.dumps(value)) for key, value in validated.values().items()])

    def list_hotwords(self) -> list[Hotword]:
        with self._connect() as conn:
            return [Hotword(**{**dict(row), 'enabled': bool(row['enabled'])}) for row in conn.execute(
                """SELECT h.*, (SELECT COUNT(DISTINCT d.recording_id)
                   FROM recording_hotword_decisions d JOIN recordings r USING(recording_id)
                   WHERE d.hotword_id=h.id AND d.revoked=0 AND h.enabled=1
                   AND NOT EXISTS (SELECT 1 FROM recording_overrides o
                     WHERE o.recording_id=d.recording_id AND o.field=d.field)) AS applied_records
                   FROM hotwords h ORDER BY length(term) DESC, h.id""")]

    def hotword_decisions(self, recording_id):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM recording_hotword_decisions WHERE recording_id=? ORDER BY hotword_id, field, start_offset, id',
                (recording_id,))]

    def hotword_checks(self, recording_id):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM recording_hotword_checks WHERE recording_id=? ORDER BY hotword_id, field', (recording_id,))]

    def save_hotword_decisions(self, recording, rules, decisions, previous, checks=None):
        """Commit only the unchanged recording/rule/decision snapshot. Archive changes via triggers."""
        from app.hotwords import validate_decision
        from app.recording_content import strip_origin_markers
        for d in decisions:
            rule = next(r for r in rules if r.id == d['hotword_id'])
            validate_decision(d, strip_origin_markers(getattr(recording, d['field'] + '_md')), rule.term)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM recordings WHERE recording_id=?', (recording.recording_id,)).fetchone()
            if current is None or any((current[key] or '') != (getattr(recording, key) or '')
                    for key in ('title', 'summary_md', 'transcript_md', 'has_summary', 'has_transcript')):
                return False
            active = [(r['id'], r['term'], r['aliases'], r['updated_at']) for r in conn.execute(
                'SELECT * FROM hotwords WHERE enabled=1 ORDER BY length(term) DESC, id')]
            if active != [(r.id, r.term, r.aliases, r.updated_at) for r in rules]:
                return False
            existing = [dict(row) for row in conn.execute(
                'SELECT * FROM recording_hotword_decisions WHERE recording_id=? ORDER BY hotword_id, field, start_offset, id',
                (recording.recording_id,))]
            if existing != previous:
                return False
            at = datetime.now(timezone.utc).isoformat()
            # A forced rejudge archives former approvals and never erases manual revocation.
            for check in checks or []:
                conn.execute('DELETE FROM recording_hotword_decisions WHERE recording_id=? AND hotword_id=? AND field=? AND revoked=0',
                             (recording.recording_id, check['hotword_id'], check['field']))
                conn.execute("""INSERT INTO recording_hotword_checks VALUES (?,?,?,?,?,?)
                    ON CONFLICT(recording_id,hotword_id,field) DO UPDATE SET
                    status=excluded.status, reason=excluded.reason, decided_at=excluded.decided_at""",
                    (recording.recording_id, check['hotword_id'], check['field'], check['status'], check['reason'], at))
            for d in decisions:
                conn.execute("""INSERT INTO recording_hotword_decisions
                    (recording_id,hotword_id,field,span,replacement,reason,decided_at,start_offset,end_offset)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(recording_id,hotword_id,field,span,start_offset)
                    DO NOTHING""", (recording.recording_id, d['hotword_id'], d['field'], d['span'], d['replacement'],
                    d['reason'], at, d['start_offset'], d['end_offset']))
        return True

    def revoke_hotword(self, recording_id, hotword_id, field, decision_id):
        with self._connect() as conn:
            return conn.execute("""UPDATE recording_hotword_decisions SET revoked=1
                WHERE id=? AND recording_id=? AND hotword_id=? AND field=? AND revoked=0""",
                (decision_id, recording_id, hotword_id, field)).rowcount > 0

    def hotword_presentation(self, recording_id):
        from app.hotwords import HotwordApplier
        from app.recording_content import strip_origin_markers
        recording = self.get_recording(recording_id)
        rules = [r for r in self.list_hotwords() if r.enabled]
        checks = {(d['hotword_id'], d['field']) for d in self.hotword_checks(recording_id)}
        overrides = self.get_overrides(recording_id)
        applied = []
        pending = False
        for field in ('summary', 'transcript'):
            if field in overrides or not getattr(recording, 'has_' + field):
                continue
            pending |= any((r.id, field) not in checks for r in rules)
            applier = HotwordApplier(self._active_hotword_decisions(recording_id, field))
            applier.apply_hotwords(strip_origin_markers(getattr(recording, field + '_md')), markdown=field != 'transcript')
            applied.extend({**d, 'count': 1} for d in applier.decisions if d['id'] in applier.applied_ids)
        return {'pending': pending, 'applied': applied, 'count': len(applied)}

    def get_overrides(self, recording_id: str) -> dict[str, dict]:
        with self._connect() as conn:
            return {row['field']: dict(row) for row in conn.execute(
                'SELECT field, content_md, updated_at FROM recording_overrides WHERE recording_id=?',
                (recording_id,))}

    def save_override(self, recording_id: str, field: str, content: str) -> str:
        if field not in ('summary', 'transcript'):
            raise ValueError('Invalid recording field')
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM recordings WHERE recording_id=?', (recording_id,)).fetchone():
                raise ValueError('Recording not found')
            conn.execute('INSERT INTO recording_overrides (recording_id, field, content_md, updated_at) '
                         'VALUES (?, ?, ?, ?) ON CONFLICT(recording_id, field) DO UPDATE SET '
                         'content_md=excluded.content_md, updated_at=excluded.updated_at',
                         (recording_id, field, content, now))
        return now

    def revert_override(self, recording_id: str, field: str) -> None:
        if field not in ('summary', 'transcript'):
            raise ValueError('Invalid recording field')
        with self._connect() as conn:
            conn.execute('DELETE FROM recording_overrides WHERE recording_id=? AND field=?', (recording_id, field))

    def bulk_add_hotwords(self, items: list[dict]) -> dict[str, int]:
        from app.hotwords import validate_hotword
        for item in items:
            validate_hotword(**item)
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in items:
                added += conn.execute(
                    'INSERT INTO hotwords (term,aliases,created_at,updated_at) VALUES (?,?,?,?) '
                    'ON CONFLICT(term) DO NOTHING',
                    (item['term'], item.get('aliases', ''), now, now)).rowcount
        return {'added': added, 'existing': len(items) - added}

    def add_hotword(self, term: str, aliases: str = '', note: str = '') -> None:
        from app.hotwords import validate_hotword
        validate_hotword(term, aliases, note)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute('INSERT INTO hotwords (term, aliases, note, created_at, updated_at) '
                         'VALUES (?, ?, ?, ?, ?)', (term, aliases, note, now, now))

    def update_hotword(self, hotword_id: int, term: str, aliases: str, note: str) -> bool:
        from app.hotwords import validate_hotword
        validate_hotword(term, aliases, note)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT term, aliases FROM hotwords WHERE id=?', (hotword_id,)).fetchone()
            if old and (old['term'], old['aliases']) != (term, aliases):
                conn.execute('DELETE FROM recording_hotword_decisions WHERE hotword_id=?', (hotword_id,))
                conn.execute('DELETE FROM recording_hotword_checks WHERE hotword_id=?', (hotword_id,))
            return conn.execute('UPDATE hotwords SET term=?, aliases=?, note=?, updated_at=? WHERE id=?',
                                (term, aliases, note, datetime.now(timezone.utc).isoformat(), hotword_id)).rowcount > 0

    def delete_hotword(self, hotword_id: int) -> bool:
        with self._connect() as conn:
            conn.execute('DELETE FROM recording_hotword_decisions WHERE hotword_id=?', (hotword_id,))
            conn.execute('DELETE FROM recording_hotword_checks WHERE hotword_id=?', (hotword_id,))
            return conn.execute('DELETE FROM hotwords WHERE id=?', (hotword_id,)).rowcount > 0

    def toggle_hotword(self, hotword_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute('UPDATE hotwords SET enabled=1-enabled, updated_at=? WHERE id=?',
                                (datetime.now(timezone.utc).isoformat(), hotword_id)).rowcount > 0

    def _active_hotword_decisions(self, recording_id, field):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT d.* FROM recording_hotword_decisions d
                JOIN hotwords h ON d.hotword_id=h.id WHERE h.enabled=1 AND d.revoked=0
                AND d.recording_id=? AND d.field=? ORDER BY length(d.span) DESC, d.id""", (recording_id, field))]

    def apply_hotwords(self, text: str, *, recording_id=None, field=None, markdown: bool = True) -> str:
        from app.hotwords import HotwordApplier
        if recording_id is None or field not in ('summary', 'transcript'):
            return text
        applier = HotwordApplier(self._active_hotword_decisions(recording_id, field))
        result = applier.apply_hotwords(text, markdown=markdown)
        if applier.hits:
            with self._connect() as conn:
                conn.executemany('UPDATE hotwords SET hits=hits+? WHERE id=?', list((n, i) for i, n in applier.hits.items()))
        return result

    def rebuild_search(self) -> None:
        self.migrate()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._backfill_search_text(conn)
            conn.execute("INSERT INTO recordings_fts(recordings_fts) VALUES ('rebuild')")

    @staticmethod
    def _search_predicate(query: str) -> tuple[str, list[str]]:
        if len(query) >= 3:
            # Quoting the entire phrase makes operators and quotes literal.
            where = 'rowid IN (SELECT rowid FROM recordings_fts WHERE recordings_fts MATCH ?)'
            params = ['"' + query.replace('"', '""') + '"']
        else:
            # Trigram MATCH cannot find 1–2 character queries. Escape LIKE
            # metacharacters so %, _ and backslash remain literal keywords.
            where = "search_text LIKE ? ESCAPE '\\'"
            params = ['%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%']
        return where, params

    def search_recordings(self, query: str, start: date | None = None,
                          end: date | None = None, page: int = 1) -> RecordingPage:
        if not query:
            return RecordingPage([], 0, 1, 1)
        where, params = self._search_predicate(query)
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
            tags = self._tags_for(conn, rows)
        return RecordingPage([
            StoredRecording(row['recording_id'], row['title'], datetime.fromisoformat(row['created_at']),
                            row['duration_min'], bool(row['has_transcript']), bool(row['has_summary']),
                            row['summary_md'] or '', row['transcript_md'] or '',
                            tags=tags.get(row['recording_id'], ()))
            for row in rows
        ], total, page, pages)

    def read_recordings(self, start: date | None = None, end: date | None = None,
                        *, limit: int | None = 20, offset: int = 0,
                        query: str | None = None) -> RecordingBatch:
        where, params = self._search_predicate(query) if query is not None else ('1 = 1', [])
        if start is not None:
            where += ' AND created_at >= ?'
            params.append(start.isoformat())
        if end is not None:
            where += ' AND created_at <= ?'
            params.append(end.isoformat() + 'T23:59:59.999999+08:00')
        with self._connect() as conn:
            conn.execute('BEGIN')
            total = conn.execute(f'SELECT COUNT(*) FROM recordings WHERE {where}', params).fetchone()[0]
            rows = conn.execute(
                'SELECT recording_id, title, created_at, duration_min, has_transcript, has_summary, '
                f'summary_md, transcript_md FROM recordings WHERE {where} '
                'ORDER BY created_at DESC, recording_id DESC LIMIT ? OFFSET ?',
                (*params, limit if limit is not None else -1, offset),
            ).fetchall()
            tags = self._tags_for(conn, rows)
        return RecordingBatch([
            StoredRecording(row['recording_id'], row['title'], datetime.fromisoformat(row['created_at']),
                            row['duration_min'], bool(row['has_transcript']), bool(row['has_summary']),
                            row['summary_md'] or '', row['transcript_md'] or '',
                            tags=tags.get(row['recording_id'], ()))
            for row in rows
        ], total)

    def create_api_key(self, name: str, key_hash: str) -> ApiKey:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                'INSERT INTO api_keys (name, key_hash, created_at) VALUES (?, ?, ?)',
                (name, key_hash, created_at),
            )
            return ApiKey(cursor.lastrowid, name, created_at, None, None)

    def list_api_keys(self) -> list[ApiKey]:
        with self._connect() as conn:
            return [ApiKey(**dict(row)) for row in conn.execute(
                'SELECT id, name, created_at, revoked_at, last_used_at FROM api_keys ORDER BY id',
            )]

    def revoke_api_key(self, key_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                'UPDATE api_keys SET revoked_at = coalesce(revoked_at, ?) WHERE id = ?',
                (datetime.now(timezone.utc).isoformat(), key_id),
            )
            return cursor.rowcount > 0

    def authenticate_api_key(self, key_hash: str) -> tuple[int | None, bool]:
        # Never cache authorization: a committed revocation affects the next
        # call, across processes. Serialize the last-used update with revoke.
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            key_id, active = None, False
            # Compare every fixed-length digest; no SQL string equality or
            # early return that depends on the position of the matching key.
            for row in conn.execute('SELECT id, key_hash, revoked_at FROM api_keys'):
                if hmac.compare_digest(key_hash, row['key_hash']):
                    key_id, active = row['id'], row['revoked_at'] is None
            if active:
                conn.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?',
                             (datetime.now(timezone.utc).isoformat(), key_id))
            return key_id, active

    def audit_api_call(self, key_id: int | None, path: str, status_code: int) -> None:
        with self._connect() as conn:
            conn.execute('INSERT INTO api_audit (key_id, path, status_code, at) VALUES (?, ?, ?, ?)',
                         (key_id, path, status_code, datetime.now(timezone.utc).isoformat()))

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
            tags = self._tags_for(conn, rows)
        return RecordingPage([
            RecordingCard(row['recording_id'], row['title'], datetime.fromisoformat(row['created_at']),
                          row['duration_min'], bool(row['has_transcript']), bool(row['has_summary']),
                          tags=tags.get(row['recording_id'], ()))
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
                "UPDATE sync_runs SET finished_at = ?, status = ?, fetched = ?, inserted = ?, updated = ?, error = ?, refreshed_token = ? WHERE id = ?",
                (run.finished_at, run.status, run.fetched, run.inserted, run.updated, run.error, run.refreshed_token, run.id),
            )

    def latest_sync(self) -> SyncRun | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1').fetchone()
            return SyncRun(**dict(row)) if row else None

    def upsert_recording(self, meta: RecordingMeta, detail: RecordingDetail, synced_at: str) -> str:
        if detail.recording_id != meta.recording_id or not meta.recording_id or meta.created_at.tzinfo is None:
            raise ValueError('Invalid recording identity or timestamp')
        fields = ('title', 'created_at', 'duration_min', 'summary_md', 'transcript_md', 'has_transcript', 'has_summary',
                  'search_text', 'media_type')
        values = (meta.title, meta.created_at.astimezone(SHANGHAI).isoformat(), meta.duration_min,
                  detail.summary_md, detail.transcript_md, int(detail.has_transcript), int(detail.has_summary),
                  '\n'.join((meta.title or '', detail.summary_md or '', detail.transcript_md or '')),
                  detail.media_type or meta.media_type)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM recordings WHERE recording_id = ?', (meta.recording_id,)).fetchone()
            if old is not None and tuple(old[field] for field in fields) == values:
                conn.execute('UPDATE recordings SET synced_at = ? WHERE recording_id = ?', (synced_at, meta.recording_id))
                return 'unchanged'
            conn.execute(
                'INSERT INTO recordings (recording_id, title, created_at, duration_min, summary_md, transcript_md, '
                'has_transcript, has_summary, search_text, media_type, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(recording_id) DO UPDATE SET '
                'title = excluded.title, created_at = excluded.created_at, duration_min = excluded.duration_min, '
                'summary_md = excluded.summary_md, transcript_md = excluded.transcript_md, '
                'has_transcript = excluded.has_transcript, has_summary = excluded.has_summary, '
                'search_text = excluded.search_text, media_type = excluded.media_type, synced_at = excluded.synced_at',
                (meta.recording_id, *values, synced_at),
            )
            if old and any((old[key] or '') != (value or '') for key, value in (
                    ('title', meta.title), ('summary_md', detail.summary_md), ('transcript_md', detail.transcript_md))):
                conn.execute('DELETE FROM recording_tags WHERE recording_id = ?', (meta.recording_id,))
            if old and any(old[key] != value for key, value in (
                    ('summary_md', detail.summary_md), ('transcript_md', detail.transcript_md),
                    ('has_summary', int(detail.has_summary)), ('has_transcript', int(detail.has_transcript)))):
                conn.execute('DELETE FROM recording_hotword_decisions WHERE recording_id=?', (meta.recording_id,))
                conn.execute('DELETE FROM recording_hotword_checks WHERE recording_id=?', (meta.recording_id,))
            return 'updated' if old else 'inserted'

    def get_recording(self, recording_id: str) -> StoredRecording | None:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT recording_id, title, created_at, duration_min, has_transcript, has_summary, '
                'summary_md, transcript_md FROM recordings WHERE recording_id = ?', (recording_id,),
            ).fetchone()
            tags = self._tags_for(conn, [row] if row else [])
        if row is None:
            return None
        return StoredRecording(
            recording_id=row['recording_id'], title=row['title'],
            created_at=datetime.fromisoformat(row['created_at']).astimezone(SHANGHAI),
            duration_min=row['duration_min'], has_transcript=bool(row['has_transcript']),
            has_summary=bool(row['has_summary']), summary_md=row['summary_md'] or '',
            transcript_md=row['transcript_md'] or '', tags=tags.get(recording_id, ()),
        )

    @staticmethod
    def _tags_for(conn, rows) -> dict[str, tuple[str, ...]]:
        ids = [row['recording_id'] for row in rows]
        if not ids:
            return {}
        grouped = {}
        for row in conn.execute(
            'SELECT recording_id, tag FROM recording_tags WHERE recording_id IN (' +
            ','.join('?' for _ in ids) + ') ORDER BY position', ids,
        ):
            grouped.setdefault(row['recording_id'], []).append(row['tag'])
        return {key: tuple(value) for key, value in grouped.items()}

    @contextmanager
    def tag_lock(self):
        # Serialize CLI and web batches; each batch permits two HTTP requests.
        with self.db_path.with_suffix(self.db_path.suffix + '.tag.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def tag_candidates(self, recording_ids=None, *, force=False):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT recording_id FROM recordings
                WHERE ? OR NOT EXISTS (
                    SELECT 1 FROM recording_tags t WHERE t.recording_id = recordings.recording_id)
                ORDER BY created_at DESC, recording_id DESC
            """, (force,)).fetchall()
        selected = None if recording_ids is None else set(recording_ids)
        return [recording for row in rows
                if selected is None or row['recording_id'] in selected
                if (recording := self.get_recording(row['recording_id'])) is not None]

    def replace_tags(self, recording, tags, model, created_at):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute(
                'SELECT title, summary_md, transcript_md FROM recordings WHERE recording_id = ?',
                (recording.recording_id,),
            ).fetchone()
            if current is None or tuple(value or '' for value in current) != (
                    recording.title, recording.summary_md, recording.transcript_md):
                raise ValueError('Recording changed during tagging')
            conn.execute('DELETE FROM recording_tags WHERE recording_id = ?', (recording.recording_id,))
            conn.executemany(
                'INSERT INTO recording_tags (recording_id, tag, position, model, created_at) VALUES (?, ?, ?, ?, ?)',
                [(recording.recording_id, tag, position, model, created_at)
                 for position, tag in enumerate(tags, 1)],
            )

    def start_tag_run(self, started_at):
        with self._connect() as conn:
            conn.execute("UPDATE tag_runs SET finished_at = ?, error = '上次打标进程中断' WHERE finished_at IS NULL",
                         (started_at,))
            return conn.execute('INSERT INTO tag_runs (started_at) VALUES (?)', (started_at,)).lastrowid

    def finish_tag_run(self, run):
        with self._connect() as conn:
            conn.execute("""
                UPDATE tag_runs SET finished_at = :finished_at, attempted = :attempted,
                    succeeded = :succeeded, failed = :failed, error = :error,
                    input_tokens = :input_tokens, output_tokens = :output_tokens,
                    cache_read_input_tokens = :cache_read_input_tokens,
                    cache_creation_input_tokens = :cache_creation_input_tokens WHERE id = :id
            """, run)

    @staticmethod
    def _matching_users(conn: sqlite3.Connection, *identifiers: str | None) -> list[sqlite3.Row]:
        ids = tuple(dict.fromkeys(value for value in identifiers if value))
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        return conn.execute(
            f'SELECT * FROM users WHERE subject IN ({placeholders}) '
            f'OR union_id IN ({placeholders}) OR open_id IN ({placeholders}) '
            'ORDER BY is_active DESC, subject', ids * 3,
        ).fetchall()

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(row['subject'], row['name'], row['avatar_url'], bool(row['is_active']),
                    row['added_at'], row['union_id'], row['open_id'])

    def seed_admin(self, subject: str) -> None:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not self._matching_users(conn, subject):
                conn.execute(
                    "INSERT INTO users (subject, name, avatar_url, is_active, added_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (subject, "管理员", "", datetime.now(timezone.utc).isoformat()),
                )

    def get_user(self, subject: str, other_id: str | None = None) -> User | None:
        with self._connect() as conn:
            rows = self._matching_users(conn, subject, other_id)
        return self._user(rows[0]) if rows else None

    def list_users(self) -> list[User]:
        with self._connect() as conn:
            return [self._user(row) for row in conn.execute(
                'SELECT * FROM users ORDER BY added_at, subject')]

    def add_user(self, user: FeishuUser) -> None:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = self._matching_users(conn, user.union_id, user.open_id)
            if rows:
                for row in rows:
                    conn.execute(
                        'UPDATE users SET name = ?, avatar_url = ?, is_active = 1, '
                        'union_id = coalesce(?, union_id), open_id = coalesce(?, open_id) '
                        'WHERE subject = ?',
                        (user.name, user.avatar_url, user.union_id or None,
                         user.open_id or None, row['subject']),
                    )
            else:
                conn.execute(
                    'INSERT INTO users (subject, union_id, open_id, name, avatar_url, is_active, added_at) '
                    'VALUES (?, ?, ?, ?, ?, 1, ?)',
                    (user.subject, user.union_id or None, user.open_id or None, user.name,
                     user.avatar_url, datetime.now(timezone.utc).isoformat()),
                )

    def remove_user(self, subject: str) -> None:
        with self._connect() as conn:
            # Legacy/config entries may represent the same person twice. Revoke
            # their known aliases too, so another matching row cannot bypass removal.
            conn.execute('BEGIN IMMEDIATE')
            rows = self._matching_users(conn, subject)
            ids = [value for row in rows for value in
                   (row['subject'], row['union_id'], row['open_id']) if value]
            for row in self._matching_users(conn, *ids):
                conn.execute('UPDATE users SET is_active = 0 WHERE subject = ?', (row['subject'],))

    def update_profile(self, user: FeishuUser) -> None:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in self._matching_users(conn, user.union_id, user.open_id):
                # Keep existing subjects stable for previously issued sessions.
                if row['is_active']:
                    conn.execute(
                        'UPDATE users SET name = ?, avatar_url = ?, '
                        'union_id = coalesce(?, union_id), open_id = coalesce(?, open_id) '
                        'WHERE subject = ?',
                        (user.name, user.avatar_url, user.union_id or None,
                         user.open_id or None, row['subject']),
                    )
