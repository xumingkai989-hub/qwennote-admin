from datetime import date, datetime, timedelta
import re
from uuid import uuid4

import pytest

from app import board, board_page
from app.feishu import FeishuUser
from app.minutes import SHANGHAI
from app.session import SESSION_MAX_AGE, SignedCookie
from test_board import add, GOOD
from test_login import login


@pytest.fixture
def board_client(client, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 6, 0, 30, tzinfo=SHANGHAI).astimezone(tz)
    monkeypatch.setattr(board_page, 'datetime', Clock)
    assert login(client).status_code == 302
    return client


def archive(store, day='2026-11-06', scope='day', analysis=GOOD):
    with store._connect() as conn:
        conn.execute('INSERT INTO board_reports VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (day, scope, '{"count":999,"minutes":99999}', analysis, 'fixture',
                      day + 'T22:00:00+08:00', None))


def token(client):
    return client.get('/board').context['csrf_token']


def test_anonymous_forbidden(client):
    assert client.get('/board').status_code == 403
    assert client.post('/board/generate').status_code == 403


@pytest.mark.parametrize('active', [None, False])
def test_outside_whitelist_forbidden(client, store, settings, active):
    subject = uuid4().hex
    if active is False:
        with store._connect() as conn:
            conn.execute('INSERT INTO users (subject,name,avatar_url,is_active,added_at) VALUES (?,?,?,0,?)',
                         (subject, '停用', '', 'now'))
    client.cookies.set('session', SignedCookie(settings.session_secret, salt='web-session',
                                              max_age=SESSION_MAX_AGE).encode(subject))
    assert client.get('/board').status_code == 403


def test_live_totals_details_archive_and_shanghai_default(board_client, store, monkeypatch):
    add(store, 'old', '2026-11-01T09:30:00+08:00', 71)
    add(store, 'mon', '2026-11-02T09:30:00+08:00', 60)
    add(store, 'early', '2026-11-05T16:05:00Z', 46)
    add(store, 'late', minutes=32)
    add(store, 'unknown', '2026-11-06T14:30:00+08:00', None)
    archive(store)
    archive(store, '2026-11-02', 'week')
    monkeypatch.setattr(board_page.board_analysis, 'generate', lambda *a, **k: pytest.fail('GET generated AI'))
    response = board_client.get('/board')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    ctx = response.context
    assert ctx['day'] == date(2026, 11, 6)
    assert ctx['totals'] == board.totals(store)
    assert ctx['daily'] == board.day_stats(store, '2026-11-06')
    assert ctx['weekly'] == board.week_stats(store, '2026-11-02', '2026-11-06')
    assert 'board-stat-value">3.5</span>' in response.text and '3 场 · 1.3 小时' in response.text
    assert [i['recording_id'] for i in ctx['items']] == ['early', 'late', 'unknown']
    assert len(re.findall('class="board-meeting"', response.text)) == 3
    assert sum(i['percent'] for i in ctx['items']) == 100
    assert '1 场时长未知，未计入占比' in response.text and '时长未知' in response.text
    assert response.text.count(GOOD) == 2
    assert '11月2日 — 11月9日' in response.text
    with store._connect() as conn:
        conn.execute("UPDATE recordings SET duration_min=100 WHERE recording_id='late'")
    assert board_client.get('/board').context['daily']['minutes'] == 146


def test_empty_and_missing_analysis(board_client):
    response = board_client.get('/board')
    assert '今天没有会议' in response.text and '本周没有会议' in response.text
    assert response.text.count('分析未生成') == 2
    assert response.text.count('>生成</button>') == 2
    assert '暂无存档' in response.text


def test_null_analysis_and_empty_durations(board_client, store):
    archive(store, analysis=None)
    add(store, minutes=None)
    add(store, 'zero', minutes=0)
    response = board_client.get('/board')
    assert response.status_code == 200 and '分析未生成' in response.text
    assert all(i['bar_percent'] == 0 for i in response.context['items'])


def test_history_only_archived_days_descending_limit_and_selection(board_client, store):
    end = date(2026, 11, 6)
    for i in range(35):
        archive(store, (end - timedelta(days=i)).isoformat())
    archive(store, '2026-12-07', 'week')
    add(store, stamp='2026-09-01T09:00:00+08:00')
    response = board_client.get('/board?day=2026-11-05')
    assert response.context['history'] == [(end - timedelta(days=i)).isoformat() for i in range(30)]
    assert 'aria-label="2026-11-05" aria-current="date"' in response.text
    assert '当天没有会议' in response.text and '回到今天' in response.text
    assert 'href="/board?day=2026-09-01"' not in response.text
    assert 'href="/board?day=2026-12-07"' not in response.text


@pytest.mark.parametrize('value', ['', '20261106', '2026-W45-5', '2026-11-31', 'junk'])
def test_invalid_date(board_client, value):
    assert board_client.get('/board', params={'day': value}).status_code == 400


def test_regular_user_no_buttons_but_navigation(client, fake_auth, store):
    subject = uuid4().hex
    with store._connect() as conn:
        conn.execute('INSERT INTO users (subject,name,avatar_url,is_active,added_at) VALUES (?,?,?,1,?)',
                     (subject, '白名单', '', 'now'))
    fake_auth.user = FeishuUser(subject, '白名单', '')
    assert login(client).status_code == 302
    response = client.get('/board')
    assert response.status_code == 200
    assert '重新生成' not in response.text and 'class="board-generate"' not in response.text
    assert 'href="/board" aria-label="看板" data-label="看板" aria-current="page"' in response.text
    assert client.post('/board/generate', data={'day': '2026-11-06'}).status_code == 403


def test_navigation_order_and_highlight(board_client):
    html = board_client.get('/board').text
    labels = re.findall(r'class="navigation-link" href="([^"]+)"', html)
    assert labels[:3] == ['/', '/board', '/hotwords']
    assert html.count('aria-current="page"') == 1
    assert 'href="/board" aria-label="看板" data-label="看板" aria-current="page"' in html


@pytest.mark.parametrize('minutes,expected', [(None, '时长未知'), (0, '0 分钟'), (59, '59 分钟'),
                                             (60, '1.0 小时'), (143, '2.4 小时')])
def test_duration_format(minutes, expected):
    assert board_page.duration(minutes) == expected


def test_many_rows_rounding_tiny_shares_and_unknown():
    values = [1] * 7 + [2000, None, 0]
    items = board_page.shares([{'minutes': x} for x in values], sum(x or 0 for x in values))
    assert sum(i['percent'] for i in items) == 100
    assert 0 < items[0]['bar_percent'] < 1
    assert items[-1]['bar_percent'] == items[-2]['bar_percent'] == 0


def test_escaped_content(board_client, store):
    add(store, title='<script>alert(1)</script>', tags=('<img src=x onerror=alert(1)>',))
    archive(store, analysis='<script>alert(2)</script>')
    html = board_client.get('/board').text
    assert '<script>alert' not in html and '<img src=x' not in html
    assert '&lt;script&gt;' in html


@pytest.mark.parametrize('scope,expected', [('day', ['day']), ('week', ['week']), ('both', ['day', 'week'])])
def test_generate_authorized_csrf_and_selected_date(board_client, store, monkeypatch, scope, expected):
    calls = []
    monkeypatch.setattr(board_page.board_analysis, 'generate', lambda db, **kw: calls.append((db, kw)))
    response = board_client.post('/board/generate', data={
        'csrf_token': token(board_client), 'scope': scope, 'day': '2026-11-04'})
    assert response.status_code == 303 and response.headers['location'] == '/board?day=2026-11-04'
    assert [kw['scope'] for _, kw in calls] == expected
    assert all(db is store and kw['day'] == date(2026, 11, 4) for db, kw in calls)


@pytest.mark.parametrize('mode', ['missing', 'wrong', 'different-session', 'duplicate'])
def test_generate_rejects_invalid_csrf(board_client, settings, monkeypatch, mode):
    monkeypatch.setattr(board_page.board_analysis, 'generate', lambda *a, **k: pytest.fail('invalid CSRF'))
    signed = token(board_client)
    fields = {'day': '2026-11-06', 'scope': 'day', 'csrf_token': signed}
    if mode == 'missing': fields.pop('csrf_token')
    if mode == 'wrong': fields['csrf_token'] = 'bad'
    if mode == 'different-session':
        from itsdangerous import TimestampSigner
        from unittest.mock import patch
        with patch.object(TimestampSigner, 'get_timestamp', return_value=int(datetime.now().timestamp()) - 30):
            new = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE).encode(settings.admin_open_id)
        board_client.cookies.set('session', new, domain='testserver.local', path='/')
    if mode == 'duplicate': fields['csrf_token'] = [signed, signed]
    assert board_client.post('/board/generate', data=fields).status_code == 403


@pytest.mark.parametrize('fields', [{'day': 'bad'}, {'day': '2026-11-06', 'scope': 'month'}])
def test_generate_invalid_parameters(board_client, fields):
    assert board_client.post('/board/generate', data={'csrf_token': token(board_client), **fields}).status_code == 400


def test_model_failure_keeps_page_available(board_client, store, monkeypatch):
    from app.models import ModelError
    add(store)
    def unavailable(_):
        raise ModelError('private failure')
    monkeypatch.setattr(board_page.board_analysis, 'resolve_model', unavailable)
    response = board_client.post('/board/generate', data={'csrf_token': token(board_client), 'day': '2026-11-06'})
    assert response.status_code == 303
    html = board_client.get(response.headers['location']).text
    assert '分析未生成' in html and '1 场 · 0.5 小时' in html
    assert 'private failure' not in html


def test_week_chart_seven_aligned_days_future_and_maximum(board_client, store):
    add(store, 'mon', '2026-11-02T10:00:00+08:00', 300)
    add(store, 'tue', '2026-11-03T10:00:00+08:00', 150)
    add(store, 'fri', minutes=60)
    add(store, 'future', '2026-11-07T10:00:00+08:00', 999)
    response = board_client.get('/board')
    rows = response.context['week_days']
    assert [r['day'] for r in rows] == [f'2026-11-0{i}' for i in range(2, 9)]
    assert [r['bar_percent'] for r in rows] == [100, 50, 0, 0, 20, 0, 0]
    assert rows[4]['is_today'] and all(r['future'] for r in rows[5:])
    html_rows = re.findall(r'<div class="board-week-row.*?</div>', response.text, re.S)
    assert len(html_rows) == 7
    for row, width in zip(html_rows, [100, 50, 0, 0, 20, 0, 0]):
        assert f'style="width: {width}%"' in row
    assert 'is-today' in html_rows[4] and '今天' in html_rows[4]
    assert all('>—</span>' in row for row in html_rows[5:])
    assert '1 场 · 5.0 小时' in html_rows[0]


@pytest.mark.parametrize('selected,start,end', [('2026-11-02', '2026-11-02', '2026-11-08'),
                                               ('2026-11-01', '2026-10-26', '2026-11-01'),
                                               ('2025-01-01', '2024-12-30', '2025-01-05')])
def test_empty_week_slots_across_boundaries(board_client, selected, start, end):
    response = board_client.get('/board', params={'day': selected})
    rows = response.context['week_days']
    assert len(rows) == 7 and rows[0]['day'] == start and rows[-1]['day'] == end
    assert all(row['bar_percent'] == 0 and row['count'] == 0 for row in rows)
    assert response.text.count('class="board-bar-fill" style="width: 0%"') == 7


@pytest.mark.parametrize('values', [[30], [1, 1, 1], [4, 3, 2, 1, 1], [10, 9, 8, 7, 6, 5],
                                  [1] * 12, [2000, 1, 1, 1, 1, 1, 1]])
def test_donut_sector_counts_totals_and_other(board_client, store, values):
    for i, minutes in enumerate(values):
        add(store, f'meeting-{i}', minutes=minutes, title=f'会议 {i}')
    response = board_client.get('/board')
    sectors = response.context['chart']['sectors']
    assert len(sectors) == min(len(values), 6)
    assert response.text.count('class="board-donut-sector"') == len(sectors)
    assert sum(item['percent'] for item in sectors) == 100
    assert sum(item['bar_percent'] for item in sectors) == pytest.approx(100)
    assert sum(item['minutes'] for item in sectors) == sum(values)
    assert sectors[-1]['offset'] + sectors[-1]['bar_percent'] == pytest.approx(100)
    assert len({item['color'] for item in sectors}) == len(sectors)
    if len(values) > 5:
        assert sectors[-1]['title'] == '其他' and sectors[-1]['color'] == 6
        assert sectors[-1]['minutes'] == sum(sorted(values, reverse=True)[5:])
    assert '<svg viewBox="0 0 120 120" width="120" height="120" role="img"' in response.text


def test_unknown_and_zero_never_enter_donut_but_remain_in_legend(board_client, store):
    add(store, 'known', minutes=10, title='已知会议')
    add(store, 'unknown', minutes=None, title='未知会议')
    add(store, 'zero', minutes=0, title='零分钟会议')
    html = board_client.get('/board').text
    svg = re.search(r'<svg viewBox="0 0 120 120".*?</svg>', html, re.S)[0]
    legend = re.search(r'<ul class="board-legend".*?</ul>', html, re.S)[0]
    assert svg.count('class="board-donut-sector"') == 1
    assert '未知会议' not in svg and '零分钟会议' not in svg
    assert '未知会议' in legend and '时长未知' in legend and '零分钟会议' in legend
    assert legend.count('is-uncharted') == 2
    assert html.count('class="board-meeting"') == 3


def test_all_unknown_has_empty_ring_and_no_nan(board_client, store):
    add(store, minutes=None)
    html = board_client.get('/board').text
    assert 'class="board-donut-sector"' not in html
    assert '暂无可计算占比的时长' in html
    assert 'NaN' not in html and '今日 1 场会议，共 0.0 小时' in html


def test_donut_sort_is_stable_and_does_not_change_detail_order(board_client, store):
    for i, minutes in enumerate([1, 30, 20, 10, 5, 30]):
        add(store, f'id-{i}', f'2026-11-06T{9+i:02d}:00:00+08:00', minutes)
    ctx = board_client.get('/board').context
    assert [i['recording_id'] for i in ctx['items']] == [f'id-{i}' for i in range(6)]
    assert [i['recording_id'] for i in ctx['chart']['sectors']] == ['id-1', 'id-5', 'id-2', 'id-3', 'id-4', None]


def test_chart_theme_pairs_and_distinct_chromatic_hues():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / 'app/static/app.css').read_text()
    values = re.findall(r'--chart-(\d): oklch\(([\d.]+) ([\d.]+) (\d+)\)', css)
    assert len(values) == 12
    for light, dark in zip(values[:6], values[6:]):
        assert light[0] == dark[0] and light[2:] == dark[2:]
        assert float(dark[1]) - float(light[1]) == pytest.approx(.08)
    # The sixth supplied token is intentionally achromatic Other, not a sixth hue.
    hues = sorted(int(value[3]) for value in values[:5])
    assert all((hues[(i + 1) % 5] - hue) % 360 >= 30 for i, hue in enumerate(hues))


def test_future_query_cannot_shrink_visible_bars(board_client, store):
    add(store, 'mon', '2026-11-02T10:00:00+08:00', 60)
    add(store, 'future', '2026-11-07T10:00:00+08:00', 300)
    rows = board_client.get('/board?day=2026-11-08').context['week_days']
    assert rows[0]['bar_percent'] == 100
    assert rows[5]['future'] and rows[5]['bar_percent'] == 0
