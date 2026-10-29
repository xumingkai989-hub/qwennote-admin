import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

from app.feishu import FeishuUser


@dataclass(frozen=True)
class User:
    open_id: str
    name: str
    avatar_url: str
    is_active: bool
    added_at: str


class Store(Protocol):
    def migrate(self) -> None: ...

    def seed_admin(self, open_id: str) -> None: ...

    def get_user(self, open_id: str) -> User | None: ...

    def update_profile(self, user: FeishuUser) -> None: ...


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
                    duration_min REAL NOT NULL DEFAULT 0,
                    summary_md TEXT NOT NULL DEFAULT '',
                    transcript_md TEXT NOT NULL DEFAULT '',
                    has_transcript INTEGER NOT NULL DEFAULT 0 CHECK (has_transcript IN (0, 1)),
                    has_summary INTEGER NOT NULL DEFAULT 0 CHECK (has_summary IN (0, 1)),
                    owner_open_id TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL
                );
            """)

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
