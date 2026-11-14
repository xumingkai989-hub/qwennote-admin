"""Weekly allocation presentation: real T2 data, current week, and empty states."""
import re
from datetime import date

import pytest

from app import board_page, tag_time
from test_board import add
from test_board_page import board_client
from test_tag_time import meeting


def block(response):
    return re.search(r'<section class="board-section board-allocation".*?</section>', response.text, re.S)[0]


def test_route_exposes_both_levels_for_current_week_even_on_history(board_client, store, monkeypatch):
    meeting(store, minutes=61)
    add(store, 'old', '2026-10-30T09:00:00+08:00', 999)
    original = tag_time.week_allocation
    calls = []
    def capture(store, monday, today):
        calls.append((monday, today))
        return original(store, monday, today)
    monkeypatch.setattr(tag_time, 'week_allocation', capture)
    response = board_client.get('/board?day=2026-10-30')
    assert calls[0] == (date(2026, 11, 2), date(2026, 11, 6))
    assert calls[-1] == calls[0]
    assert len(calls) == 9  # Current allocation plus the eight comparison weeks.
    assert response.context['allocation']['total_minutes'] == 61
    assert response.context['daily']['minutes'] == 999
    html = block(response)
    assert 'data-allocation-panel="by_category"' in html
    assert 'data-allocation-panel="by_tag" hidden' in html
    assert html.count('<strong>1.0</strong>') == 2
    assert '产品' in html and '<small>产品</small>' in html
    assert response.text.index('id="board-week-analysis-title"') < response.text.index('id="board-allocation-title"')


def test_seven_categories_plus_unclassified_and_top_six(board_client, store):
    for i, category in enumerate(['产品', '中台', '师资', '市场', '技术', '公司管理', '其他']):
        meeting(store, str(i), f'标签{i}', category, 60)
    meeting(store, 'extra', '额外标签', '产品', 20)
    meeting(store, 'unknown', name=None, minutes=1)
    response = board_client.get('/board')
    charts = response.context['allocation_charts']
    assert len(charts['by_category']) == 8
    assert len(charts['by_tag']) == 8
    html = block(response)
    for key, rows in charts.items():
        svg = re.search(rf'<svg[^>]*aria-labelledby="allocation-{key}-title".*?</svg>', html, re.S)[0]
        assert svg.count('class="board-donut-sector"') == len(rows)
        assert sum(r['percent'] for r in rows) == 100
        assert sum(r['minutes'] for r in rows) == 441
        assert rows[-1]['title'] == '未分类'
        assert len({r['color'] for r in rows}) == len(rows)
    assert html.count('>未分类</span>') == 1
    assert '未分类<small>未分类</small>' in html


@pytest.mark.parametrize('minutes', [None, 0])
def test_zero_total_renders_no_svg_or_switch(board_client, store, minutes):
    meeting(store, minutes=minutes)
    html = block(board_client.get('/board'))
    assert '本周还没有会议' in html
    assert '<svg' not in html and '<button' not in html
    assert ('1 场时长未知，未计入' in html) == (minutes is None)


def test_empty_week(board_client):
    html = block(board_client.get('/board'))
    assert '本周还没有会议' in html and '<svg' not in html


def test_unknown_duration_note_and_zero_unclassified_retained(board_client, store):
    meeting(store, minutes=60)
    meeting(store, 'unknown', name=None, minutes=None)
    html = block(board_client.get('/board'))
    assert html.count('1 场时长未知，未计入') == 2
    assert '未分类' in html and '0.0 小时' in html


@pytest.mark.parametrize('values', [[1, 1, 1], [1, 2, 3], [999, 1], [60], [1]*8])
def test_display_shares_conserve_without_mutating_aggregation(values):
    rows = [{'name': str(i), 'minutes': n, 'share': n/sum(values)} for i, n in enumerate(values)]
    original = [r.copy() for r in rows]
    chart = board_page.allocation_chart(rows, sum(values), 'by_tag')
    assert sum(r['percent'] for r in chart) == 100
    assert chart[-1]['offset'] + chart[-1]['bar_percent'] == pytest.approx(100)
    assert rows == original


def test_allocation_names_are_escaped(board_client, store):
    meeting(store, name='<img src=x onerror=alert(1)>')
    html = block(board_client.get('/board'))
    assert '<img' not in html and '&lt;img' in html
