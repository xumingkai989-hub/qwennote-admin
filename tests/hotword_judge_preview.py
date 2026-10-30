"""Synthetic approved/skipped/pending records for hotword browser acceptance."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import uvicorn
from recording_edit_preview import preview
from test_hotwords import approve_rules


def build(directory):
    app = preview(Path(directory) / 'preview.db')
    base = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with base(application):
            store = app.state.store
            store.add_hotword('小鹿直播', '小红直播')
            for suffix in ['summary', 'transcript', 'mobile', 'dark']:
                approve_rules(store, 'preview-' + suffix)
            yield
    app.router.lifespan_context = lifespan
    return app


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qwennote-judge-preview-') as directory:
        uvicorn.run(build(directory), host='127.0.0.1', port=8769, log_level='warning')
