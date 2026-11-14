"""Allocation contracts, conservation, Shanghai bounds and read-only behavior."""
from datetime import date

import pytest

from app import board, primary_tag, tag_time
from test_board import add


@pytest.fixture
def db(store):
    store.migrate()
    return store


def meeting(db, ident='r1', name='AI Tutor', category='产品', minutes=30, **kwargs):
    tags = kwargs.pop('tags', (name,) if name else ())
    add(db, ident, minutes=minutes, tags=tags, **kwargs)
    with db._connect() as conn:
        tag_id = None
        if name:
            conn.execute("INSERT INTO tags (name, category, created_at, updated_at, status, source) "
                         "VALUES (?, ?, ?, ?, 'active', 'manual') ON CONFLICT(name) DO NOTHING",
                         (name, category, '2026-11-06', '2026-11-06'))
            conn.execute('UPDATE tags SET category = ? WHERE name = ?', (category, name))
            tag_id = conn.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()[0]
        conn.execute('INSERT INTO recording_primary_tag '
                     '(recording_id, tag_id, tag_name, source, tag_fingerprint, generated_at) '
                     'VALUES (?, ?, ?, ?, ?, ?)', (ident, tag_id, name or '未分类',
                         'model' if name else 'fallback', primary_tag.fingerprint(list(tags)), '2026-11-06'))


def allocation(db):
    return tag_time.week_allocation(db, '2026-11-02', '2026-11-06')


def conserved(result, total):
    assert result['total_minutes'] == total
    for key in ('by_category', 'by_tag'):
        assert sum(row['minutes'] for row in result[key]) == total
        assert all(row['share'] == round(row['minutes'] / total, 4) for row in result[key])


def test_single_meeting_single_tag(db):
    meeting(db, minutes=31)
    assert allocation(db) == {
        'total_minutes': 31, 'unknown_duration_count': 0,
        'by_category': [{'name': '产品', 'minutes': 31, 'share': 1, 'sessions': 1}],
        'by_tag': [{'name': 'AI Tutor', 'category': '产品', 'minutes': 31, 'share': 1, 'sessions': 1}]}


def test_multitag_meeting_is_counted_once(db):
    meeting(db, minutes=43, tags=('AI Tutor', '模考系统', '导师中心', '市场与投放'))
    result = allocation(db)
    conserved(result, 43)
    assert result['by_tag'][0]['sessions'] == 1
    assert len(result['by_tag']) == 1


def test_ten_meetings_with_null_duration_conserve_both_levels(db):
    durations = [1, 2, 3, 7, 11, 19, 29, 61, 97, None]
    for index, minutes in enumerate(durations):
        meeting(db, str(index), name=f'tag{index}' if index != 8 else None,
                category=['产品', '中台', '师资'][index % 3], minutes=minutes)
    result = allocation(db)
    conserved(result, 230)
    assert result['unknown_duration_count'] == 1
    assert sum(row['sessions'] for row in result['by_category']) == 10
    assert sum(row['sessions'] for row in result['by_tag']) == 10
    assert result['total_minutes'] == board.week_stats(db, '2026-11-02', '2026-11-06')['minutes']


def test_small_categories_merge_but_unclassified_does_not(db):
    meeting(db, 'large', minutes=960)
    meeting(db, 'small', name='技术会', category='技术', minutes=19)
    meeting(db, 'threshold', name='市场会', category='市场', minutes=20)
    meeting(db, 'unknown', name=None, minutes=1)
    result = allocation(db)
    conserved(result, 1000)
    assert [(r['name'], r['minutes']) for r in result['by_category']] == [
        ('产品', 960), ('市场', 20), ('其他', 19), ('未分类', 1)]


def test_threshold_uses_unrounded_share(db):
    meeting(db, 'large', minutes=98001)
    meeting(db, 'small', name='技术会', category='技术', minutes=1999)
    result = allocation(db)
    assert result['by_category'][1]['name'] == '其他'
    assert result['by_category'][1]['share'] == .02


def test_existing_other_category_and_small_categories_form_one_row(db):
    meeting(db, 'large', minutes=970)
    meeting(db, 'small', name='技术会', category='技术', minutes=10)
    meeting(db, 'other', name='杂事', category='其他', minutes=20)
    result = allocation(db)
    conserved(result, 1000)
    assert result['by_category'][1] == {'name': '其他', 'minutes': 30, 'share': .03, 'sessions': 2}


@pytest.mark.parametrize('unknown_minutes', [None, 1, 200])
def test_top_six_and_unclassified_retention(db, unknown_minutes):
    for index, minutes in enumerate([100, 90, 80, 70, 60, 50, 40, 30]):
        meeting(db, str(index), name=f'tag{index}', minutes=minutes)
    if unknown_minutes is not None:
        meeting(db, 'unknown', name=None, minutes=unknown_minutes)
    result = allocation(db)
    conserved(result, 520 + (unknown_minutes or 0))
    known_count = 5 if unknown_minutes == 200 else 6
    assert [row['name'] for row in result['by_tag'][:known_count]] == [f'tag{i}' for i in range(known_count)]
    other = next(row for row in result['by_tag'] if row['name'] == '其他')
    assert other['minutes'] == (120 if unknown_minutes == 200 else 70)
    assert other['sessions'] == (3 if unknown_minutes == 200 else 2)
    assert other['category'] == '其他'
    if unknown_minutes is not None:
        assert result['by_tag'][-1]['name'] == '未分类'
        assert result['by_tag'][-1]['minutes'] == unknown_minutes


@pytest.mark.parametrize('minutes', [None, 0])
def test_zero_minute_week_has_empty_distributions(db, minutes):
    meeting(db, minutes=minutes)
    assert allocation(db) == {'total_minutes': 0, 'unknown_duration_count': int(minutes is None),
                              'by_category': [], 'by_tag': []}


def test_empty_week(db):
    meeting(db, stamp='2026-10-31T09:00:00+08:00')
    assert allocation(db) == {'total_minutes': 0, 'unknown_duration_count': 0,
                              'by_category': [], 'by_tag': []}


def test_special_rows_are_last_even_when_larger(db):
    meeting(db, 'small', name='a', minutes=50)
    meeting(db, 'large', name='b', category='市场', minutes=60)
    meeting(db, 'other', name='其他', category='其他', minutes=200)
    meeting(db, 'unknown', name=None, minutes=300)
    result = allocation(db)
    conserved(result, 610)
    assert [r['name'] for r in result['by_category']] == ['市场', '产品', '其他', '未分类']
    assert [r['name'] for r in result['by_tag']] == ['b', 'a', '其他', '未分类']


def test_category_ties_follow_enabled_database_positions(db):
    meeting(db, 'product', minutes=20)
    meeting(db, 'tech', name='技术会', category='技术', minutes=20)
    with db._connect() as conn:
        conn.execute("UPDATE tag_categories SET position = 0 WHERE name = '技术'")
    assert [r['name'] for r in allocation(db)['by_category']] == ['技术', '产品']


@pytest.mark.parametrize('category', ['技术', '旧分类'])
def test_disabled_or_missing_category_is_other_without_losing_tag(db, category):
    meeting(db, category=category)
    with db._connect() as conn:
        conn.execute("UPDATE tag_categories SET enabled = 0 WHERE name = '技术'")
    result = allocation(db)
    conserved(result, 30)
    assert result['by_category'][0]['name'] == '其他'
    assert result['by_tag'][0]['name'] == 'AI Tutor'
    assert result['by_tag'][0]['category'] == '其他'


@pytest.mark.parametrize('decision', ['missing', 'fallback', 'null_id'])
def test_missing_or_failed_primary_tag_is_unclassified(db, decision):
    meeting(db)
    with db._connect() as conn:
        if decision == 'missing':
            conn.execute('DELETE FROM recording_primary_tag')
        elif decision == 'fallback':
            conn.execute("UPDATE recording_primary_tag SET source = 'fallback'")
        else:
            conn.execute('UPDATE recording_primary_tag SET tag_id = NULL')
    result = allocation(db)
    conserved(result, 30)
    assert result['by_tag'][0]['name'] == result['by_category'][0]['name'] == '未分类'


@pytest.mark.parametrize('stamp,included', [
    ('2026-11-01T15:59:59Z', False), ('2026-11-01T16:00:00Z', True),
    ('2026-11-02T00:00:00', True), ('2026-11-06T23:59:59.999999+08:00', True),
    ('2026-11-06T16:00:00Z', False), ('2026-11-07T00:00:00', False),
    ('2026-11-01T17:00:00-07:00', True), ('2026-11-09T00:00:00+08:00', False)])
def test_shanghai_half_open_week_boundary(db, stamp, included):
    meeting(db, stamp=stamp)
    result = tag_time.week_allocation(db, date(2026, 11, 2), date(2026, 11, 6))
    assert result['total_minutes'] == (30 if included else 0)


@pytest.mark.parametrize('monday,today', [('2026-11-03', '2026-11-06'),
                                       ('2026-11-02', '2026-11-09'), ('2026-11-02', '2026-11-01')])
def test_invalid_week_rejected(db, monday, today):
    with pytest.raises(ValueError, match='Monday'):
        tag_time.week_allocation(db, monday, today)


@pytest.mark.parametrize('layer,delta', [('by_category', -1), ('by_tag', 3)])
def test_conservation_failure_reports_actual_difference(db, monkeypatch, layer, delta):
    meeting(db)
    merge = tag_time._merge_other
    def corrupt(*args, **kwargs):
        result = merge(*args, **kwargs)
        if bool(kwargs.get('tag')) == (layer == 'by_tag'):
            result[0]['minutes'] += delta
        return result
    monkeypatch.setattr(tag_time, '_merge_other', corrupt)
    with pytest.raises(ValueError, match=rf'{layer}.*difference={delta:+d}'.replace('+', r'\+')):
        allocation(db)


def test_read_only_and_existing_statistics_unchanged(db, monkeypatch):
    meeting(db)
    def no_model(*args, **kwargs):
        pytest.fail('Aggregation must not invoke T1')
    monkeypatch.setattr(primary_tag, 'judge', no_model)
    monkeypatch.setattr(primary_tag, 'ensure_week', no_model)
    def snapshot():
        with db._connect() as conn:
            data = list(conn.iterdump())
        return data, board.totals(db), board.day_stats(db, '2026-11-06'), board.week_stats(db, '2026-11-02', '2026-11-06')
    before = snapshot()
    allocation(db)
    assert snapshot() == before
