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
from app.sync import SyncRun
from conftest import FakeFeishuAuth
from test_login import login


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'static').mkdir(exist_ok=True)
    for name in ['app.css', 'detail.js', 'recording-edit.js', 'home.js', 'navigation.js', 'assistant.js']:
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
            started = '2026-10-30T21:40:00+08:00'
            store.finish_sync(SyncRun(
                id=store.start_sync(started), started_at=started,
                finished_at='2026-10-30T21:40:01+08:00', status='success',
                fetched=0, inserted=0, updated=0, skipped=0, mode='quick',
            ))
            paragraph = '本次检查首页列表与详情页在不同屏幕下的左边界和内容宽度。长文本保持完整展示，通过行高和段间距帮助阅读。' * 4
            summary = '# 项目例会\n\n' + paragraph + '\n\n## 本周进展\n\n- 完成页面检查\n- 核对移动端布局\n\n> 保留完整内容。\n\n## 待办\n\n- [ ] 确认上线验收\n\n```text\n' + 'long_code_' * 30 + '\n```\n\n| 项目 | 状态 | 备注 |\n| --- | --- | --- |\n| 页面检查 | 完成 | 六种视口 |'
            transcript = '\n\n'.join(f'[00:0{i}:00.000–00:0{i}:30.000] 发言人 {i % 2 + 1}\n\n{paragraph}\n\n补充说明：继续核对完整内容与段落间距。' for i in range(6))
            for i in range(8):
                key = f'layout-{i}'
                has_transcript, has_summary = [(True, True), (False, False), (True, False), (False, True)][i % 4]
                store.upsert_recording(
                    RecordingMeta(key, '项目例会：页面布局与长文本阅读验收' if i == 0 else f'项目讨论 {i}', datetime(2026, 10, 30-i//3, 12-i, tzinfo=SHANGHAI), 30),
                    RecordingDetail(key, summary if has_summary else '', transcript if has_transcript else '', has_transcript, has_summary), '2026-10-30T12:00:00+08:00',
                )
            for name, url in [('home', '/'), ('summary', '/recordings/layout-0?view=summary'), ('transcript', '/recordings/layout-0?view=transcript')]:
                response = client.get(url)
                assert response.status_code == 200
                html = response.text.replace('http://testserver/static/', 'static/')
                html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="layout-fixture"', html)
                (output / f'{name}.html').write_text(html)
            for recording in store.read_recordings(limit=None).items:
                store.replace_tags(recording, ['AI Tutor', '中台与数据看板', '模考系统', '导师中心', '述职'],
                                   'fixture-model', '2026-10-30T12:00:00+08:00')
            html = client.get('/').text.replace('http://testserver/static/', 'static/')
            html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="layout-fixture"', html)
            (output / 'home-tags.html').write_text(html)

            with store._connect() as conn:
                conn.execute("UPDATE assistant_config SET instructions = '按业务线归类，不按会议形式。\n涉及产品规划时优先使用具体产品标签。' WHERE id = 1")
                conn.execute("INSERT INTO tags (name,status,source,usage_count,created_at,updated_at) VALUES ('AI教学','active','model',2,'2026-10-30T06:22:00+00:00','2026-10-30T06:22:00+00:00')")
                conn.execute("INSERT INTO tag_merge_proposals (from_tag,to_tag,reason,status,created_at) VALUES ('AI教学','AI Tutor','model','pending','2026-10-30T06:22:00+00:00')")
                conn.execute("UPDATE tags SET usage_count = 8 WHERE name IN ('AI Tutor','中台与数据看板','模考系统')")
            html = client.get('/assistant').text.replace('http://testserver/static/', 'static/')
            html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="layout-fixture"', html)
            (output / 'assistant.html').write_text(html)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    render(parser.parse_args().output)
