import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import RecordingPage, User
from test_login import login


def test_startup_migrates_only_required_tables_and_seeds_admin(client, settings):
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"users", "recordings", "sync_runs", "api_keys", "api_audit", "recording_tags", "tag_runs",
                    "tags", "tag_aliases", "assistant_config", "tag_change_log",
                    "tag_merge_proposals", "tag_semantic_checks", "hotwords", "recording_overrides",
                    "settings", "scheduler_state", "models", "recording_hotword_decisions",
                    "recording_hotword_checks", "recording_hotword_decision_history", "recording_hotword_check_history", "sqlite_sequence"}
        assert required | {"recordings_fts"} <= tables
        assert all(t in required or t.startswith("recordings_fts") for t in tables)
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0
        assert {r[1] for r in conn.execute("PRAGMA table_info(recordings)")} == {
            "recording_id", "title", "created_at", "duration_min", "summary_md",
            "transcript_md", "has_transcript", "has_summary", "owner_open_id", "synced_at", "search_text", "media_type",
        }
        assert {r[1] for r in conn.execute("PRAGMA table_info(users)")} == {
            "subject", "union_id", "open_id", "name", "avatar_url", "is_active", "added_at",
        }
        row = conn.execute("SELECT subject, is_active, added_at FROM users").fetchone()
        assert row[0] == settings.admin_open_id
        assert row[1] == 1
        assert row[2]


def test_repeated_startup_preserves_existing_whitelist(settings, fake_auth):
    app = create_app(settings=settings, feishu_auth=fake_auth)
    with TestClient(app):
        with sqlite3.connect(settings.db_path) as conn:
            original_added_at = conn.execute("SELECT added_at FROM users").fetchone()[0]
            conn.execute("UPDATE users SET name = ?, is_active = 0", ("已编辑姓名",))
    with TestClient(app):
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute("SELECT name, is_active, added_at FROM users").fetchall() == [
                ("已编辑姓名", 0, original_added_at)
            ]


def test_store_can_be_replaced_without_sqlite(settings, fake_auth):
    class FakeStore:
        user = None

        def migrate(self):
            pass

        def seed_admin(self, open_id):
            self.user = User(open_id, "管理员", "", True, "")

        def get_user(self, open_id, other_id=None):
            return self.user if self.user.subject == open_id else None

        def update_profile(self, user):
            pass

        def latest_sync(self):
            return None

        def list_recordings(self, start, end, page=1):
            return RecordingPage([], 0, 1, 1)

    with TestClient(
        create_app(settings=settings, feishu_auth=fake_auth, store=FakeStore()),
        follow_redirects=False,
    ) as client:
        assert login(client).status_code == 302
        assert client.get("/").status_code == 200
    assert not settings.db_path.exists()


@pytest.mark.parametrize("key", [
    "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_REDIRECT_URI",
    "SESSION_SECRET", "DB_PATH", "ADMIN_OPEN_ID",
])
def test_missing_environment_setting_fails_fast(settings, monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(ValueError, match=key):
        Settings.from_env()


def test_weak_session_secret_is_rejected(settings, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings.from_env()


@pytest.mark.parametrize("uri", ["relative/callback", "ftp://testserver/auth/callback"])
def test_invalid_redirect_uri_is_rejected(settings, monkeypatch, uri):
    monkeypatch.setenv("FEISHU_REDIRECT_URI", uri)
    with pytest.raises(ValueError, match="FEISHU_REDIRECT_URI"):
        Settings.from_env()


def test_migration_preserves_work_order_01_recordings(settings, fake_auth):
    from app.store import SQLiteStore

    settings.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('''CREATE TABLE recordings (
            recording_id TEXT PRIMARY KEY NOT NULL,
            title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            duration_min REAL NOT NULL DEFAULT 0,
            summary_md TEXT NOT NULL DEFAULT '', transcript_md TEXT NOT NULL DEFAULT '',
            has_transcript INTEGER NOT NULL DEFAULT 0 CHECK (has_transcript IN (0, 1)),
            has_summary INTEGER NOT NULL DEFAULT 0 CHECK (has_summary IN (0, 1)),
            owner_open_id TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL
        )''')
        conn.execute("INSERT INTO recordings VALUES ('legacy', '旧标题', '2026-10-01T12:00:00+08:00', 18, '旧总结', '旧原文', 1, 1, 'owner', 'previous')")
        before = conn.execute('SELECT * FROM recordings').fetchone()
    store = SQLiteStore(settings.db_path)
    store.migrate()
    store.migrate()
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute('SELECT recording_id, title, created_at, duration_min, summary_md, transcript_md, '
                            'has_transcript, has_summary, owner_open_id, synced_at FROM recordings').fetchone() == before
        assert conn.execute('SELECT media_type FROM recordings').fetchone()[0] is None
        columns = {row[1]: row for row in conn.execute('PRAGMA table_info(recordings)')}
        assert all(columns[field][3] == 0 for field in ('duration_min', 'summary_md', 'transcript_md'))
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth, store=store), follow_redirects=False) as client:
        login(client)
        result = client.post('/sync').json()
        assert result['status'] == 'success'
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT recording_id, title, created_at, duration_min, summary_md, transcript_md, "
                            "has_transcript, has_summary, owner_open_id, synced_at FROM recordings "
                            "WHERE recording_id = 'legacy'").fetchone() == before


@pytest.mark.parametrize('value', ['0', '-1', 'abc'])
def test_invalid_backfill_is_rejected(settings, monkeypatch, value):
    monkeypatch.setenv('BACKFILL_DAYS', value)
    with pytest.raises(ValueError, match='BACKFILL_DAYS'):
        Settings.from_env()


def test_missing_http_token_fails_app_construction(settings, fake_auth, monkeypatch):
    from dataclasses import replace

    monkeypatch.delenv('QWENNOTE_MCP_TOKEN', raising=False)
    with pytest.raises(ValueError, match='QWENNOTE_MCP_TOKEN'):
        create_app(settings=replace(settings, minutes_source='http'), feishu_auth=fake_auth)


@pytest.mark.parametrize('key,value', [('QWENNOTE_SOURCE', 'invalid'), ('QWENNOTE_FIXTURE_DIR', '')])
def test_invalid_source_configuration_is_rejected(settings, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        Settings.from_env()
