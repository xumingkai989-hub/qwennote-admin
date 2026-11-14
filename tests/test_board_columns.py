"""Vertical comparison geometry, including empty and zero-duration segments."""
from copy import deepcopy
from datetime import date, timedelta
import re

import pytest

from app.board_page import allocation_comparison
from app import tag_time
from test_board_page import board_client


def weeks(count):
    return [{'monday': (date(2026, 8, 17) + timedelta(weeks=i)).isoformat(),
             'total_minutes': (i + 1) * 90,
             'by_category': [{'name': '产品', 'minutes': (i + 1) * 60},
                             {'name': '技术', 'minutes': (i + 1) * 30}]}
            for i in range(count)]


@pytest.mark.parametrize('count', [4, 8, 12])
def test_columns_are_chronological_and_stacked_on_one_scale(count):
    source = weeks(count)[::-1]
    original = deepcopy(source)
    chart = allocation_comparison(source, [])
    rows = chart['rows']
    assert source == original
    assert len(rows) == count
    assert [r['monday'] for r in rows] == sorted(r['monday'] for r in rows)
    assert [r['cx'] for r in rows] == sorted(r['cx'] for r in rows)
    assert chart['axis_max_minutes'] == pytest.approx(count * 90 * 1.1)
    assert max(r['height'] for r in rows) == pytest.approx(240 / 1.1)
    for row in rows:
        assert row['width'] == pytest.approx(.6 / count)
        assert [s['name'] for s in row['segments']] == ['产品', '技术']
        bottom = chart['bottom']
        for segment in row['segments']:
            assert segment['bottom'] == pytest.approx(bottom)
            assert segment['top'] + segment['height'] == pytest.approx(bottom)
            bottom = segment['top']
        assert sum(s['height'] for s in row['segments']) == pytest.approx(row['height'])
        assert row['height'] == pytest.approx(row['total_minutes'] / chart['axis_max_minutes'] * 240)
    assert len(chart['ticks']) == 5


@pytest.mark.parametrize('middle', ['missing', 'zero'])
def test_missing_breaks_connection_but_zero_keeps_both_neighbors(middle):
    source = weeks(3)
    source[1]['by_category'] = [] if middle == 'missing' else [{'name': '产品', 'minutes': 0}]
    source[1]['total_minutes'] = 0
    chart = allocation_comparison(source, [])
    bands = chart['connections']
    assert len(bands) == (0 if middle == 'missing' else 2)
    if bands:
        assert bands[0]['top2'] == bands[0]['bottom2'] == 264
        assert bands[1]['top1'] == bands[1]['bottom1'] == 264
        assert all(band['name'] == '产品' for band in bands)


@pytest.mark.parametrize('source', [[], [{'monday': '2026-11-02', 'total_minutes': 0, 'by_category': []}],
    [{'monday': '2026-11-02', 'total_minutes': 0, 'by_category': [{'name': '产品', 'minutes': 0}]}]])
def test_empty_chart_has_finite_zero_baseline(source):
    chart = allocation_comparison(source, [])
    assert chart['ticks'] == [{'y': 264, 'label': '0h'}]
    assert chart['axis_max_minutes'] == 0
    assert all(row['height'] == 0 for row in chart['rows'])


def test_tiny_column_omits_total_label_but_retains_accessible_total(board_client, monkeypatch):
    source = weeks(4)
    source[0]['total_minutes'] = 1
    source[0]['by_category'] = [{'name': '产品', 'minutes': 1}]
    monkeypatch.setattr(tag_time, 'weekly_allocation', lambda *args, **kwargs: source)
    html = board_client.get('/board?weeks=4').text
    labels = re.findall(r'<g class="board-comparison-label".*?</g>', html, re.S)
    assert len(labels) == 4
    assert 'data-total-label="2026-08-17"' not in html
    assert html.count('data-total-label=') == 3
    assert '08-17 · 0.0h</title>' in html
