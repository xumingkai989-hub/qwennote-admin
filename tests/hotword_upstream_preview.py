"""Local synthetic UI acceptance; fake MCP transport only, no real credentials."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import httpx
import uvicorn
from recording_edit_preview import preview
from test_hotword_upstream import Server, seed
from app.hotword_upstream import HttpHotwordSource


def build(db_path):
    app = preview(db_path)
    server = Server()
    server.words = ['上游候选', '另一个词', '小 鹅 直播', '已同步词']
    app.state.hotword_source = HttpHotwordSource('offline-only', transport=httpx.MockTransport(server))
    base = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base(application):
            store = app.state.store
            store.add_hotword('小鹿直播', '小鹅直播')
            store.add_hotword('AI Tutor')
            seed(store, '停用词', enabled=0)
            seed(store, 'bad-word')
            seed(store, '字'*21)
            identifier = seed(store, '已同步词')
            with store._connect() as conn:
                conn.execute("INSERT INTO hotword_upstream_sync VALUES (?,'已同步词','old','ok')", (identifier,))
            yield
    app.router.lifespan_context = lifespan

    @app.get('/synthetic-calls')
    def calls():
        return {'calls':server.calls}
    return app


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qwennote-upstream-preview-') as directory:
        uvicorn.run(build(Path(directory)/'preview.db'), host='127.0.0.1', port=8772, log_level='warning')
