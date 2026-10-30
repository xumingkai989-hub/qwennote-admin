"""Loopback-only model demo; temporary key/database and a fake transport."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from cryptography.fernet import Fernet
import httpx
import uvicorn
from app import models
from recording_edit_preview import preview


def build(directory):
    directory = Path(directory)
    models.SECRET_KEY_PATH = directory / 'secret.key'
    models.SECRET_KEY_PATH.write_bytes(Fernet.generate_key())
    models.SECRET_KEY_PATH.chmod(0o600)
    models.CLAUDE_ENV_PATH = directory / 'claude.env'
    models.CLAUDE_ENV_PATH.write_text('ANTHROPIC_MODEL=deepseek-flash\nANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n')
    app = preview(directory / 'preview.db')
    app.state.model_transport = httpx.MockTransport(lambda _: httpx.Response(200,
        json={'content':[{'type':'text','text':'ok'}]}))
    base = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base(application):
            registry = models.ModelRegistry(app.state.store)
            for name, provider, model_id, endpoint in [
                ('DeepSeek Flash','deepseek','deepseek-flash','https://api.deepseek.com/anthropic'),
                ('Claude Sonnet','anthropic','claude-sonnet-example','https://api.anthropic.com'),
                ('团队分析','openai','team-analysis','https://models.example.invalid/v1')]:
                ident = registry.add(name=name,provider=provider,model_id=model_id,base_url=endpoint,
                                     api_key='synthetic-preview-credential-abcd')
                if provider == 'deepseek': registry.mutate(ident, 'default')
                if provider == 'openai': registry.mutate(ident, 'toggle')
            yield
    app.router.lifespan_context = lifespan
    return app


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qwennote-models-preview-') as directory:
        uvicorn.run(build(directory), host='127.0.0.1', port=8768, log_level='warning')
