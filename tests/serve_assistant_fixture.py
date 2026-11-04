"""Temporary SQLite + fake OAuth server for real assistant browser interactions.

Only binds loopback; never loads production settings or modifies existing data.
"""
import secrets
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from app.config import Settings
from app.feishu import FeishuUser
from app.main import create_app
from app.store import SQLiteStore
from conftest import FakeFeishuAuth


def main():
    with tempfile.TemporaryDirectory(prefix='assistant-tree-') as temp:
        settings = Settings(feishu_app_id='browser-fixture', feishu_app_secret=secrets.token_hex(16),
                            feishu_redirect_uri='http://localhost/auth/callback', session_secret=secrets.token_hex(32),
                            db_path=Path(temp) / 'fixture.db', admin_open_id='ou_fixture',
                            minutes_source='fixture', minutes_fixture_dir=ROOT / 'tests/fixtures/minutes')
        store = SQLiteStore(settings.db_path)
        store.migrate()
        app = create_app(settings=settings, store=store,
                         feishu_auth=FakeFeishuAuth(FeishuUser('ou_fixture', '演示用户', '')))
        # Sample counts are for layout verification, not production analytics.
        with store._connect() as conn:
            for name, count, category in [('AI Tutor', 16, '产品'), ('导师中心', 4, '产品'),
                                          ('模考系统', 1, '产品'), ('AI Passport', 1, '产品'),
                                          ('产品设计', 1, '产品'), ('组织与培训', 10, '公司管理'),
                                          ('述职', 5, '公司管理'), ('周会/例会', 5, '公司管理')]:
                conn.execute("INSERT INTO tags(name,status,source,usage_count,category,created_at,updated_at) "
                             "VALUES (?,'active','manual',?,?,datetime('now'),datetime('now')) "
                             "ON CONFLICT(name) DO UPDATE SET usage_count=excluded.usage_count,category=excluded.category",
                             (name, count, category))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        print(f'FIXTURE_URL=http://127.0.0.1:{sock.getsockname()[1]}', flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
