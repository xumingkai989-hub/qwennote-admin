"""Eight-week aggregation and SVG geometry contracts."""
from datetime import date, datetime, timedelta
import re
from unittest.mock import patch

import pytest

from app import board_page, tag_time
from app.minutes import SHANGHAI
from test_board_allocation import board_client
from test_tag_time import meeting


@pytest.fixture(autouse=True)
def migrated_store(store):
    store.migrate()


def test_eight_weeks_chronological_and_empty(store):
    meeting(store, minutes=61)
    rows = tag_time.weekly_allocation(store, today='2026-11-06')
    assert len(rows) == 8
    assert [r['monday'] for r in rows] == [
        (date(2026, 9, 14) + timedelta(weeks=i)).isoformat() for i in range(8)]
    assert all(r['total_minutes'] == 0 and r['by_category'] == [] for r in rows[:-1])
    assert rows[-1] == {'monday': '2026-11-02', 'label': '11-02 周', 'total_minutes': 61,
                        'by_category': [{'name': '产品', 'minutes': 61, 'share': 1}]}


def test_reuses_original_aggregation_and_partial_current_week(store):
    meeting(store, 'prior-sunday', minutes=90, stamp='2026-11-01T23:59:00+08:00')
    meeting(store, 'current', minutes=120, stamp='2026-11-02T00:00:00+08:00')
    meeting(store, 'future', minutes=999, stamp='2026-11-07T00:00:00+08:00')
    with patch.object(tag_time, 'week_allocation', wraps=tag_time.week_allocation) as aggregate:
        rows = tag_time.weekly_allocation(store, weeks=2, today=date(2026, 11, 6))
    assert [c.args[1:] for c in aggregate.call_args_list] == [
        (date(2026, 10, 26), date(2026, 11, 1)), (date(2026, 11, 2), date(2026, 11, 6))]
    assert [r['total_minutes'] for r in rows] == [90, 120]


def test_cross_year_and_shanghai_default_clock(store):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz == SHANGHAI
            return cls(2027, 1, 4, tzinfo=SHANGHAI)
    with patch.object(tag_time, 'datetime', Clock):
        rows = tag_time.weekly_allocation(store, weeks=2)
    assert [r['monday'] for r in rows] == ['2026-12-28', '2027-01-04']


@pytest.mark.parametrize('weeks', [0, -1, 1.5, True, '8'])
def test_invalid_week_count(store, weeks):
    with pytest.raises(ValueError, match='positive integer'):
        tag_time.weekly_allocation(store, weeks=weeks)


def fixture_rows():
    def week(day, *items):
        total = sum(minutes for _, minutes in items)
        return {'monday': day, 'label': day[5:] + ' 周', 'total_minutes': total,
                'by_category': [{'name': name, 'minutes': minutes, 'share': minutes / total}
                                for name, minutes in items]}
    return [week('2026-10-12', ('产品', 60), ('技术', 30)),
            week('2026-10-19', ('技术', 90), ('产品', 30)),
            week('2026-10-26'), week('2026-11-02', ('产品', 60))]


def test_shared_scale_order_connections_and_color_without_mutation():
    weeks = fixture_rows()
    current = board_page.allocation_chart(weeks[-1]['by_category'], 60, 'by_category')
    result = board_page.allocation_comparison(weeks, current)
    rows = result['rows']
    assert [r['end'] for r in rows[0]['segments']] == [.5, .75]
    assert [r['end'] for r in rows[1]['segments']] == [.75, 1]
    assert rows[3]['segments'][0]['end'] == .5
    assert [c['name'] for c in result['connections']] == ['技术', '产品']
    assert all((c['y1'], c['y2']) == (16, 30) for c in result['connections'])
    product = result['connections'][1]
    assert (product['start1'], product['end1'], product['start2'], product['end2']) == (0, .5, .75, 1)
    for row in rows:
        for segment in row['segments']:
            if segment['name'] == '产品':
                assert segment['color'] == current[0]['color']
    assert weeks == fixture_rows()


def test_svg_count_empty_rows_and_missing_week_break(board_client, monkeypatch):
    weeks = fixture_rows()
    monkeypatch.setattr(tag_time, 'weekly_allocation', lambda *a, **kw: weeks)
    response = board_client.get('/board')
    html = re.search(r'<section class="board-section board-allocation-comparison".*?</section>', response.text, re.S)[0]
    assert html.count('<rect ') == sum(len(w['by_category']) for w in weeks)
    assert html.count('<polygon ') == 2
    assert html.count('fill-opacity="0.18"') == 2
    assert 'data-week="2026-10-26"' in html
    assert '>0.0h</text>' in html
    assert html.index('<polygon ') < html.index('<rect ')
    assert response.text.index('id="board-allocation-title"') < response.text.index('id="board-comparison-title"')


def test_present_zero_minute_category_has_connection():
    weeks = fixture_rows()[:2]
    weeks[1]['by_category'].append({'name': '未分类', 'minutes': 0, 'share': 0})
    weeks[0]['by_category'].append({'name': '未分类', 'minutes': 0, 'share': 0})
    chart = board_page.allocation_comparison(weeks, [])
    assert [item['name'] for item in chart['connections']] == ['技术', '产品', '未分类']


def test_comparison_failure_keeps_existing_board(board_client, monkeypatch):
    def fail(*a, **kw):
        raise ValueError('private database detail')
    monkeypatch.setattr(tag_time, 'weekly_allocation', fail)
    response = board_client.get('/board')
    assert response.status_code == 200
    assert '近 8 周对比暂不可用' in response.text
    assert 'private database detail' not in response.text
    assert 'id="board-day-title"' in response.text


def test_category_name_is_escaped(board_client, store):
    with store._connect() as conn:
        conn.execute("UPDATE tag_categories SET name='<img src=x>' WHERE name='产品'")
    meeting(store, category='<img src=x>')
    response = board_client.get('/board')
    assert 'data-category="&lt;img src=x&gt;"' in response.text
    assert '<img src=x>' not in response.text


def test_script_and_styles_have_updated_cache_keys(board_client):
    html = board_client.get('/board').text
    assert 'board.js?v=20261114-tag-time-t5' in html
    assert 'app.css?v=20261114-tag-time-t5' in html
