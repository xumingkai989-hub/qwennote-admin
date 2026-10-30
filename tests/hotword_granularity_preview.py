"""Synthetic localhost preview for ticket 18; never touches production content."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import uvicorn
from recording_edit_preview import preview
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from datetime import datetime


def build(db_path):
    app = preview(db_path)
    base = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base(application):
            store = app.state.store
            body = '本次会议中小鹅直播的转化率'
            store.upsert_recording(RecordingMeta('word-preview', '热词粒度验收（合成数据）', datetime.now(SHANGHAI), 5),
                                   RecordingDetail('word-preview', body, body, True, True), 'now')
            store.add_hotword('小鹿直播')
            # Simulate a pre-existing sentence, then run the real startup migration.
            with store._connect() as conn:
                conn.execute("INSERT INTO hotwords(term,created_at,updated_at) VALUES (?,'now','now')",
                             ('本次会议中小鹿直播的转化率，后续还要复盘。',))
            store.migrate()
            yield
    app.router.lifespan_context = lifespan
    return app


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qwennote-word-preview-') as directory:
        uvicorn.run(build(Path(directory) / 'preview.db'), host='127.0.0.1', port=8771, log_level='warning')
