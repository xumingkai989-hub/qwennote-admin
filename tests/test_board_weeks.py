"""Selectable comparison window: SSR links, scale and unchanged board data."""
from datetime import date, timedelta
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest

from app import tag_time
from test_board_page import board_client
from test_tag_time import meeting


@pytest.mark.parametrize('query,weeks', [('', 8), ('?weeks=4', 4), ('?weeks=8', 8),
    ('?weeks=12', 12), ('?weeks=0', 8), ('?weeks=abc', 8), ('?weeks=100', 8),
    ('?weeks=', 8), ('?weeks=-4', 8), ('?weeks=4.0', 8), ('?weeks=04', 8)])
def test_window_rows_title_height_and_selection(board_client, query, weeks):
    with patch.object(tag_time, 'weekly_allocation', wraps=tag_time.weekly_allocation) as aggregate:
        response = board_client.get('/board' + query)
    assert response.status_code == 200
    assert aggregate.call_args.kwargs['weeks'] == weeks
    html = response.text
    assert html.count('data-week="') == weeks
    assert f'id="board-comparison-title">近 {weeks} 周对比</h2>' in html
    assert f'最近 {weeks} 周分类时间分配' in html
    assert f'近 {weeks} 周没有已知时长的会议' in html
    assert f'class="board-comparison-svg" width="100%" height="{weeks * 28 - 10}"' in html
    links = re.findall(r'<a href="([^"]*)" aria-pressed="(true|false)">(\d+) 周</a>', html)
    assert len(links) == 3
    for href, pressed, count in links:
        assert parse_qs(urlsplit(unescape(href)).query) == {'weeks': [count]}
        assert pressed == ('true' if int(count) == weeks else 'false')


def test_links_keep_day_repeated_and_encoded_parameters(board_client):
    response = board_client.get('/board?day=2026-10-30&weeks=12&filter=a%26b&filter=%E4%BC%9A%E8%AE%AE&empty=')
    assert response.status_code == 200
    assert response.context['day'] == date(2026, 10, 30)
    for href in re.findall(r'<a href="([^"]*)" aria-pressed=', response.text):
        params = parse_qs(urlsplit(unescape(href)).query, keep_blank_values=True)
        assert params.pop('weeks') in [['4'], ['8'], ['12']]
        assert params == {'day': ['2026-10-30'], 'filter': ['a&b', '会议'], 'empty': ['']}


@pytest.mark.parametrize('weeks,maximum', [(4, 120), (8, 240), (12, 480)])
def test_scale_uses_only_selected_weeks(board_client, store, weeks, maximum):
    # Distinct maxima in each window expose accidental fixed-eight normalization.
    totals = [480, 30, 30, 30, 240, 30, 30, 30, 120, 30, 30, 60]
    for index, minutes in enumerate(totals):
        monday = date(2026, 8, 17) + timedelta(weeks=index)
        for part, category in enumerate(['产品', '技术']):
            meeting(store, f'{index}-{part}', category, category, minutes / 2,
                    stamp=monday.isoformat() + 'T09:00:00+08:00')
    baseline = board_client.get('/board').context
    response = board_client.get(f'/board?weeks={weeks}')
    chart = response.context['allocation_comparison']
    assert len(chart['rows']) == weeks
    assert chart['max_minutes'] == maximum
    widths = [sum(s['width'] for s in row['segments']) for row in chart['rows']]
    assert max(widths) == pytest.approx(1)
    for row, width in zip(chart['rows'], widths):
        assert width == pytest.approx(row['total_minutes'] / maximum)
    assert len(chart['connections']) == (weeks - 1) * 2
    for band in chart['connections']:
        assert band['y2'] - band['y1'] == 14
    rendered = re.findall(r'<g data-week="[^"]+" data-minutes="[^"]+">(.*?)</g>', response.text, re.S)
    assert max(sum(map(float, re.findall(r'data-width="([^"]+)"', row))) for row in rendered) == pytest.approx(1)
    for key in ('daily', 'weekly', 'chart', 'week_days', 'allocation', 'allocation_charts'):
        assert response.context[key] == baseline[key]


@pytest.mark.parametrize('weeks', [4, 12])
def test_failure_copy_uses_selected_window(board_client, weeks):
    with patch.object(tag_time, 'weekly_allocation', side_effect=RuntimeError('private detail')):
        response = board_client.get(f'/board?weeks={weeks}')
    assert response.status_code == 200
    assert f'近 {weeks} 周对比暂不可用' in response.text
    assert 'private detail' not in response.text
    assert 'id="board-day-title"' in response.text
