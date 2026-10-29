import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import User
from test_login import login


def test_startup_migrates_only_required_tables_and_seeds_admin(client, settings):
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"users", "recordings"}
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0
        assert {r[1] for r in conn.execute("PRAGMA table_info(recordings)")} == {
            "recording_id", "title", "created_at", "duration_min", "summary_md",
            "transcript_md", "has_transcript", "has_summary", "owner_open_id", "synced_at",
        }
        assert {r[1] for r in conn.execute("PRAGMA table_info(users)")} == {
            "open_id", "name", "avatar_url", "is_active", "added_at",
        }
        row = conn.execute("SELECT open_id, is_active, added_at FROM users").fetchone()
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

        def get_user(self, open_id):
            return self.user if self.user.open_id == open_id else None

        def update_profile(self, user):
            pass

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
