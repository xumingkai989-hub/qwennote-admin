"""Loopback-only synthetic server for recording_edit_browser.cjs; no upstream calls."""
import argparse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import uvicorn
from app.config import Settings
from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.session import SESSION_MAX_AGE, SignedCookie
from app.store import SQLiteStore
from fakes import FakeMinutesSource


def preview(db_path):
    settings = Settings(feishu_app_id='synthetic-edit-test', feishu_app_secret=secrets.token_hex(16),
                        feishu_redirect_uri='http://localhost/auth/callback', session_secret=secrets.token_hex(32),
                        db_path=db_path, admin_open_id='ou_synthetic', minutes_source='fixture',
                        minutes_fixture_dir=ROOT / 'tests/fixtures/minutes')
    store = SQLiteStore(db_path)
    app = create_app(settings=settings, store=store, minutes_source=FakeMinutesSource())
    session = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE).encode('ou_synthetic')
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base_lifespan(application):
            for suffix in ['summary', 'transcript', 'mobile', 'dark']:
                body = '小红直播\n\nAI 助手'
                store.upsert_recording(
                    RecordingMeta('preview-' + suffix, '产品例会：内容修订验收', datetime(2026, 10, 30, 16, 12, tzinfo=SHANGHAI), 25),
                    RecordingDetail('preview-' + suffix, body, body, True, True), '2026-10-30T16:12:00+08:00')
            rich_summary = ('# 项目例会\n\n## 讨论结论\n\n小红直播与 **AI 助手** 进入本周试用。{origin:1,2}\n\n'
                            '- 第一项\n- 第二项\n  - 子任务\n\n> 引用结论\n\n'
                            '| 事项 | 状态 |\n| --- | --- |\n| 试用 | 进行中 |\n\n'
                            '```python\nprint("ok")\n```\n\n[方案](https://example.com)\n\n## 待办\n\n- [ ] 确认时间\n')
            rich_transcript = '\n\n'.join(
                f'[{index:02d}:00.000–{index:02d}:59.000] 发言人 {index % 2 + 1}\n\n'
                f'第 {index + 1} 段：小红直播与 AI 助手进入本周试用。{{origin:1,2}}\n\n记录讨论结论与行动事项。'
                for index in range(12))
            for suffix in ['rich-summary', 'rich-transcript', 'rich-mobile', 'rich-dark', 'empty-summary', 'empty-transcript']:
                summary, transcript = ('', '') if suffix.startswith('empty-') else (rich_summary, rich_transcript)
                store.upsert_recording(
                    RecordingMeta('preview-' + suffix, '产品例会：就地编辑与导航验收', datetime(2026, 10, 30, 16, 12, tzinfo=SHANGHAI), 25),
                    RecordingDetail('preview-' + suffix, summary, transcript, bool(summary), bool(transcript)), '2026-10-30T16:12:00+08:00')
            yield
    app.router.lifespan_context = lifespan

    # Synthetic preview identity only. This helper is never installed in the application.
    @app.middleware('http')
    async def synthetic_session(request, call_next):
        cookie = request.headers.get('cookie', '')
        headers = [(key, value) for key, value in request.scope['headers'] if key != b'cookie']
        headers.append((b'cookie', (cookie + '; session=' + session).encode()))
        request.scope['headers'] = headers
        return await call_next(request)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='qwennote-edit-preview-') as directory:
        uvicorn.run(preview(Path(directory) / 'preview.db'), host='127.0.0.1', port=args.port, log_level='warning')
