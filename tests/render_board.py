"""Synthetic board fixtures for browser checks, without network or model calls."""
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import secrets
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from fastapi.testclient import TestClient
from app.config import Settings
from app.feishu import FeishuUser
from app.main import create_app
from app.minutes import SHANGHAI
from app.store import SQLiteStore
from conftest import FakeFeishuAuth
from test_board import add
from test_board_page import archive
from test_tag_time import meeting
from test_login import login


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 6, 18, tzinfo=SHANGHAI).astimezone(tz)


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'static').mkdir(exist_ok=True)
    for source in (ROOT / 'app/static').iterdir():
        if source.suffix in ('.css', '.js'):
            (output / 'static' / source.name).write_bytes(source.read_bytes())
    with tempfile.TemporaryDirectory() as tmp, patch('app.board_page.datetime', Clock), patch('app.login_sync.Thread'):
        settings = Settings(feishu_app_id='fixture', feishu_app_secret=secrets.token_hex(16),
                            session_secret=secrets.token_hex(32), feishu_redirect_uri='http://testserver/auth/callback',
                            admin_open_id='ou_board_fixture', db_path=Path(tmp) / 'board.db',
                            minutes_source='fixture', minutes_fixture_dir=ROOT / 'tests/fixtures/minutes')
        store = SQLiteStore(settings.db_path)
        app = create_app(settings=settings, store=store, feishu_auth=FakeFeishuAuth(
            FeishuUser(settings.admin_open_id, '演示用户', '')))
        with TestClient(app, follow_redirects=False) as client:
            assert login(client).status_code == 302
            def save(name):
                response = client.get('/board')
                assert response.status_code == 200
                html = response.text.replace('http://testserver/static/', 'static/')
                html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="synthetic-fixture"', html)
                (output / f'{name}.html').write_text(html)
            save('empty')
            for i, (stamp, minutes) in enumerate([
                ('2026-10-30T09:00:00+08:00', 863),
                ('2026-11-02T09:00:00+08:00', 300),
                ('2026-11-03T09:00:00+08:00', 179),
                ('2026-11-04T09:00:00+08:00', 90),
                ('2026-11-05T09:00:00+08:00', 60),
            ]):
                add(store, f'previous-{i}', stamp, minutes)
            for i, (time, title, minutes, tags) in enumerate([
                ('09:30', '项目例会：页面布局验收', 46, ('产品',)),
                ('11:00', '数据看板：本周指标口径确认', 38, ('数据',)),
                ('14:00', '与供应商对齐硬件方案', 32, ('产品',)),
                ('15:30', '用户访谈：首次体验与导航反馈', 28, ('用户研究',)),
                ('17:00', '研发排期与上线检查', 16, ('研发',)),
                ('17:40', '临时沟通：后续事项确认', None, ('协作',)),
            ]):
                add(store, f'today-{i}', f'2026-11-06T{time}:00+08:00', minutes, title, tags=tags)
            archive(store, analysis='今天的时间主要用于项目例会、数据看板与硬件方案对齐。页面验收和用户访谈围绕产品体验展开，指标确认与研发排期各自推进。整体以方案确认和交付协调为主。')
            archive(store, '2026-11-02', 'week', analysis='本周时间主要用于产品方案与交付讨论，周一和周二的会议时长较多。周五进一步核对了页面、数据口径和硬件方案。这些讨论围绕项目推进展开，整体属于阶段性对齐。')
            with store._connect() as conn:
                conn.execute("UPDATE board_reports SET generated_at='2026-11-06T18:00:00+08:00' WHERE scope='week'")
            for day in ['2026-11-05', '2026-11-04', '2026-11-03', '2026-11-02', '2026-10-30']:
                archive(store, day)
            save('data')
            add(store, 'tiny', minutes=1, title='LongUnbrokenTitle' * 30, tags=('LongTag' * 40,))
            add(store, 'long', minutes=3000, title='长时间会议：' + 'LongUnbrokenTitle' * 30)
            save('stress')
            with store._connect() as conn:
                conn.execute('DELETE FROM recordings')
            add(store, 'allocation-unclassified', '2026-11-02T09:00:00+08:00', 54)
            add(store, 'allocation-unknown-duration', '2026-11-02T09:00:00+08:00', None)
            # Separate allocation fixture keeps the original daily chart fixtures intact.
            for i, (name, category, minutes) in enumerate([
                ('产品规划', '产品', 372), ('平台建设', '中台', 264),
                ('师资匹配', '师资', 126), ('渠道复盘', '市场', 108),
                ('技术方案', '技术', 96), ('组织协作', '公司管理', 84),
                ('其他事项', '其他', 72), ('体验研究', '产品', 60),
                ('课程设计', '产品', 48),
            ]):
                meeting(store, f'allocation-{i}', name, category, minutes,
                        stamp='2026-11-02T09:00:00+08:00')
            # Exercise the real archive/presentation flow with an offline provider.
            from app import board_generation
            with patch('app.board_analysis._analyze', return_value=('本周会议回顾。各主题独立。属于工作讨论。', 'fixture', None)), patch(
                    'app.board_generation._requester', return_value=lambda prompt: '{"comparison":[]}'):
                board_generation.generate(store, scope='week', day='2026-11-06')
            save('allocation')
            # Eight-week example with rank changes, absent categories and one
            # empty week, separate from the existing allocation/daily fixtures.
            for week in range(7):
                if week == 3:
                    continue
                monday = date(2026, 9, 14) + timedelta(weeks=week)
                for i, (name, category, minutes) in enumerate([
                    ('产品规划', '产品', [420, 300, 540, 0, 280, 500, 380][week]),
                    ('平台建设', '中台', 160 + week * 35),
                    ('师资匹配', '师资', 190 - week * 15),
                    ('渠道复盘', '市场', 60 + week * 20),
                ]):
                    if week == 5 and category == '师资':
                        continue
                    meeting(store, f'comparison-{week}-{i}', name, category, minutes,
                            stamp=monday.isoformat() + 'T09:00:00+08:00')
            save('comparison')



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    render(parser.parse_args().output)
