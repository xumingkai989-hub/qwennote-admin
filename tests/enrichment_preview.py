"""Isolated browser fixture; no upstream traffic or production credentials.

Run: python tests/enrichment_preview.py OUTPUT_DIR [PORT]
Then: ENRICHMENT_URL=http://127.0.0.1:8774 ENRICHMENT_ARTIFACTS=OUTPUT_DIR
      node --test tests/enrichment_browser.cjs
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import httpx
import uvicorn
from app.config import Settings
from app.feishu import FeishuUser
from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.store import SQLiteStore
from app.tag import TagSettings
from app import enrichment, tag, hotword_judge
from conftest import FakeFeishuAuth
from fakes import FakeMinutesSource

out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8774
settings = Settings(feishu_app_id='enrichment-browser', feishu_app_secret=secrets.token_hex(16),
    feishu_redirect_uri=f'http://127.0.0.1:{port}/auth/callback', session_secret=secrets.token_hex(32),
    db_path=out / 'preview.db', admin_open_id='browser-admin', minutes_source='fixture',
    minutes_fixture_dir=ROOT / 'tests/fixtures/minutes')
store = SQLiteStore(settings.db_path)
store.migrate()
for i in range(14):
    rid = f'past-{i}'
    store.upsert_recording(RecordingMeta(rid, f'项目例会 {i+1}', datetime.now(SHANGHAI)-timedelta(days=2, minutes=i), 30),
        RecordingDetail(rid, '## 本周进展\n\n完成页面验收和同步优化，确认后续交付。', '历史会议转写', True, True), enrichment.now())
store.add_hotword('AI导师', note='产品标准名称')
meta = RecordingMeta('browser-new', '产品例会：AI 增强自动补齐', datetime.now(SHANGHAI)-timedelta(seconds=10), 25)
source = FakeMinutesSource([meta], {'browser-new': RecordingDetail('browser-new',
    '## 本周进展\n\nAI Tutor 项目完成同步性能优化。\n\n## 待办\n\n- 确认标签与热词自动出现。',
    '[00:00:10.000–00:00:30.000] 发言人 1\n\nAI Tutor 项目已经完成，今天验收自动补齐功能。', True, True)})
enrichment.resolve_model = lambda _: TagSettings('offline-browser-key')

def gate():
    deadline = time.monotonic() + 90
    while not (out / 'release-ai').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('browser fixture gate')
        time.sleep(.05)

original_tag, original_judge = tag.run_tags, hotword_judge.run_judgments

def tags(store, **kw):
    gate()
    return original_tag(store, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'content': [{'type': 'text', 'text': '{"tags":["AI Tutor","中台与数据看板"]}'}]})), **kw)

def judgments(store, **kw):
    gate()
    def answer(request):
        prompt = json.loads(request.content)['messages'][0]['content']
        rows = [{'span': 'AI Tutor', 'reason': '上下文明确指代产品标准名称'}] if 'AI Tutor' in prompt else []
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': json.dumps({'replacements': rows})}]})
    return original_judge(store, transport=httpx.MockTransport(answer), **kw)

tag.run_tags, hotword_judge.run_judgments = tags, judgments
app = create_app(settings=settings, store=store, minutes_source=source,
                 feishu_auth=FakeFeishuAuth(FeishuUser('browser-admin', '验收用户', '')))
uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
