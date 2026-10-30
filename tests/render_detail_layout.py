"""Render synthetic pages for detail_layout.cjs without production credentials."""
import argparse
import re
import secrets
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from fastapi.testclient import TestClient
from app.config import Settings
from app.feishu import FeishuUser
from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.store import SQLiteStore
from conftest import FakeFeishuAuth
from test_login import login


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'static').mkdir(exist_ok=True)
    for name in ['app.css', 'detail.js']:
        (output / 'static' / name).write_bytes((ROOT / 'app/static' / name).read_bytes())
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(
            feishu_app_id='layout-fixture', feishu_app_secret=secrets.token_hex(16),
            feishu_redirect_uri='http://testserver/auth/callback', session_secret=secrets.token_hex(32),
            db_path=Path(temp) / 'fixture.db', admin_open_id='ou_layout',
            minutes_source='fixture', minutes_fixture_dir=ROOT / 'tests/fixtures/minutes',
        )
        store = SQLiteStore(settings.db_path)
        auth = FakeFeishuAuth(FeishuUser('ou_layout', '演示用户', ''))
        with TestClient(create_app(settings=settings, store=store, feishu_auth=auth), follow_redirects=False) as client:
            assert login(client).status_code == 302
            paragraph = '本次检查首页列表与详情页在不同屏幕下的左边界和内容宽度。长文本保持完整展示，通过行高和段间距帮助阅读。' * 4
            summary = '# 项目例会\n\n' + paragraph + '\n\n## 本周进展\n\n- 完成页面检查\n- 核对移动端布局\n\n> 保留完整内容。\n\n## 待办\n\n- [ ] 确认上线验收\n\n```text\n' + 'long_code_' * 30 + '\n```\n\n| 项目 | 状态 | 备注 |\n| --- | --- | --- |\n| 页面检查 | 完成 | 六种视口 |'
            transcript = '\n\n'.join(f'[00:0{i}:00.000–00:0{i}:30.000] 发言人 {i % 2 + 1}\n\n{paragraph}\n\n补充说明：继续核对完整内容与段落间距。' for i in range(6))
            for i in range(8):
                key = f'layout-{i}'
                store.upsert_recording(
                    RecordingMeta(key, '项目例会：页面布局与长文本阅读验收' if i == 0 else f'项目讨论 {i}', datetime(2026, 10, 30, 12-i, tzinfo=SHANGHAI), 30),
                    RecordingDetail(key, summary, transcript, True, True), '2026-10-30T12:00:00+08:00',
                )
            for name, url in [('home', '/'), ('summary', '/recordings/layout-0?view=summary'), ('transcript', '/recordings/layout-0?view=transcript')]:
                response = client.get(url)
                assert response.status_code == 200
                html = response.text.replace('http://testserver/static/', 'static/')
                html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="layout-fixture"', html)
                (output / f'{name}.html').write_text(html)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    render(parser.parse_args().output)
