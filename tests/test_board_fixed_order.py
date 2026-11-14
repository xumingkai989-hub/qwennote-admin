"""Global category ranks prevent crossing without changing allocated durations."""
from copy import deepcopy
from datetime import date, timedelta
from itertools import combinations
import random

import pytest

from app.board_page import allocation_comparison


ORDER = ['产品', '中台', '师资', '市场', '技术', '公司管理', '其他', '未分类']


def weekly_rows(count):
    rows = []
    for i in range(count):
        items = [{'name': name, 'minutes': 60 + (rank if i % 2 else 7 - rank)}
                 for rank, name in enumerate(ORDER)]
        # Mimic upstream duration sorting, with opposite ranks every week.
        items.sort(key=lambda item: -item['minutes'])
        rows.append({'monday': (date(2026, 8, 17) + timedelta(weeks=i)).isoformat(),
                     'total_minutes': sum(item['minutes'] for item in items),
                     'by_category': items})
    return rows


def assert_no_crossings(chart):
    for lower, upper in combinations(chart['connections'], 2):
        if (lower['week1'], lower['week2']) != (upper['week1'], upper['week2']):
            continue
        # Linear polygon edges cannot intersect when the same vertical ordering
        # holds at both ends. Sample their full thickness as well as endpoints.
        for t in (0, .25, .5, .75, 1):
            lower_top = lower['top1'] * (1 - t) + lower['top2'] * t
            upper_bottom = upper['bottom1'] * (1 - t) + upper['bottom2'] * t
            assert lower_top >= upper_bottom - 1e-9


@pytest.mark.parametrize('count', [4, 8, 12])
def test_fixed_ranks_legend_and_shallow_bands_across_reversed_duration_orders(count):
    source = weekly_rows(count)
    original = deepcopy(source)
    chart = allocation_comparison(source, [])
    assert source == original
    assert [item['name'] for item in chart['legend']] == ORDER
    assert [item['rank'] for item in chart['legend']] == list(range(8))
    for row in chart['rows']:
        assert [item['name'] for item in row['segments']] == ORDER
        assert [item['rank'] for item in row['segments']] == list(range(8))
        assert {s['name']: s['minutes'] for s in row['segments']} == {
            s['name']: s['minutes'] for s in row['by_category']}
    assert len(chart['connections']) == (count - 1) * 8
    # On comparable-height layers, a rank reversal used to jump seven layers.
    two_layers = 2 * min(s['height'] for row in chart['rows'] for s in row['segments'])
    for band in chart['connections']:
        assert abs(band['top2'] - band['top1']) <= two_layers
        assert abs(band['bottom2'] - band['bottom1']) <= two_layers
    assert_no_crossings(chart)


@pytest.mark.parametrize('missing', ['产品', '市场', '未分类'])
def test_missing_category_closes_gap_but_preserves_global_rank(missing):
    source = weekly_rows(4)
    source[1]['by_category'] = [s for s in source[1]['by_category'] if s['name'] != missing]
    source[1]['total_minutes'] = sum(s['minutes'] for s in source[1]['by_category'])
    chart = allocation_comparison(source, [])
    segments = chart['rows'][1]['segments']
    assert [s['name'] for s in segments] == [name for name in ORDER if name != missing]
    assert [s['rank'] for s in segments] == [i for i, name in enumerate(ORDER) if name != missing]
    bottom = chart['bottom']
    for segment in segments:
        assert segment['bottom'] == pytest.approx(bottom)
        bottom = segment['top']
    assert len(chart['connections']) == 7 + 7 + 8
    assert_no_crossings(chart)


def test_extreme_duration_changes_and_sparse_weeks_never_cross():
    rng = random.Random(11)
    source = weekly_rows(12)
    for row in source:
        row['by_category'] = [dict(s, minutes=rng.randrange(0, 10000))
                              for s in row['by_category'] if rng.random() > .25]
        rng.shuffle(row['by_category'])
        row['total_minutes'] = sum(s['minutes'] for s in row['by_category'])
    source[5]['by_category'] = []
    source[5]['total_minutes'] = 0
    chart = allocation_comparison(source, [])
    expected = sum(len({s['name'] for s in a['by_category']} & {s['name'] for s in b['by_category']})
                   for a, b in zip(source, source[1:]))
    assert len(chart['connections']) == expected
    for band in chart['connections']:
        assert (date.fromisoformat(band['week2']) - date.fromisoformat(band['week1'])).days == 7
    assert_no_crossings(chart)


def test_legend_is_fixed_even_if_largest_category_and_first_seen_week_change():
    source = weekly_rows(4)
    current = [{'name': name, 'color': str(i + 1)} for i, name in enumerate(ORDER[:6][::-1])]
    chart = allocation_comparison(source[::-1], current)
    assert [s['name'] for s in chart['legend']] == ORDER
    for item in current:
        assert next(s for s in chart['legend'] if s['name'] == item['name'])['color'] == item['color']
    assert chart['legend'] == allocation_comparison(source, current)['legend']


def test_future_category_names_have_deterministic_ranks():
    source = weekly_rows(2)
    for i, row in enumerate(source):
        row['by_category'] += [{'name': name, 'minutes': 10} for name in (['Z', 'A'] if i else ['A', 'Z'])]
        row['total_minutes'] += 20
    chart = allocation_comparison(source, [])
    for row in chart['rows']:
        assert [s['name'] for s in row['segments']] == ORDER + ['A', 'Z']
    assert_no_crossings(chart)
