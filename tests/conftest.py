import secrets
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.feishu import FeishuAuthError, FeishuUser
from app.main import create_app
from app.store import SQLiteStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    values = {
        "FEISHU_APP_ID": uuid4().hex,
        "FEISHU_APP_SECRET": secrets.token_urlsafe(32),
        "FEISHU_REDIRECT_URI": "http://testserver/auth/callback",
        "SESSION_SECRET": secrets.token_urlsafe(32),
        "DB_PATH": str(tmp_path / "db" / "test.sqlite3"),
        "ADMIN_OPEN_ID": uuid4().hex,
        "QWENNOTE_SOURCE": "fixture",
        "QWENNOTE_FIXTURE_DIR": str(Path(__file__).parent / "fixtures/minutes"),
        "BACKFILL_DAYS": "90",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


class FakeFeishuAuth:
    def __init__(self, user):
        self.user = user
        self.error = False

    def exchange_code(self, code):
        if self.error or code != "valid-test-code":
            raise FeishuAuthError("Authorization failed")
        return self.user

    def search_users(self, query):
        raise NotImplementedError


@pytest.fixture
def fake_auth(settings):
    return FakeFeishuAuth(FeishuUser(settings.admin_open_id, "测试用户", ""))


@pytest.fixture
def store(settings):
    return SQLiteStore(settings.db_path)


@pytest.fixture
def client(settings, fake_auth, store):
    with TestClient(
        create_app(settings=settings, feishu_auth=fake_auth, store=store),
        follow_redirects=False,
    ) as client:
        yield client
