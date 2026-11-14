"""Read-only weekly time allocation from persisted primary-topic decisions."""
from datetime import date, datetime, time, timedelta

from app.board import _week_bounds
from app.minutes import SHANGHAI

UNKNOWN = '未分类'
OTHER = '其他'


def weekly_allocation(store, *, weeks: int = 8, today=None) -> list[dict]:
    """Recent Shanghai calendar weeks, oldest first, including empty weeks.

    Completed weeks end on Sunday; the current week stops at today. Delegate
    all grouping, rounding and fallback rules to week_allocation unchanged.
    """
    if isinstance(weeks, bool) or not isinstance(weeks, int) or weeks < 1:
        raise ValueError('weeks must be a positive integer')
    today = today or datetime.now(SHANGHAI).date()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    monday = today - timedelta(days=today.weekday())
    result = []
    for offset in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=offset)
        allocation = week_allocation(store, start, min(start + timedelta(days=6), today))
        result.append({'monday': start.isoformat(), 'label': start.strftime('%m-%d 周'),
                       'total_minutes': allocation['total_minutes'],
                       'by_category': [{key: row[key] for key in ('name', 'minutes', 'share')}
                                       for row in allocation['by_category']]})
    return result


def _merge_other(rows, kept, total, *, tag=False):
    """Use the integer remainder, including any existing '其他', exactly once."""
    result = [row.copy() for row in kept]
    remainder_sessions = sum(row['sessions'] for row in rows) - sum(row['sessions'] for row in kept)
    if remainder_sessions:
        other = {'name': OTHER, 'minutes': total - sum(row['minutes'] for row in kept),
                 'sessions': remainder_sessions}
        if tag:
            other['category'] = OTHER
        result.append(other)
    # Stable sorting preserves category position for equal minute counts.
    result.sort(key=lambda row: (2 if row['name'] == UNKNOWN else 1 if row['name'] == OTHER else 0,
                                 -row['minutes']))
    for row in result:
        row['share'] = round(row['minutes'] / total, 4) if total else 0
    return result


def week_allocation(store, monday, today) -> dict:
    """Allocate [Monday 00:00, tomorrow 00:00) in Asia/Shanghai.

    Accept ISO dates or date objects, as board.week_stats does. This does not
    invoke a model or refresh T1 decisions. NULL durations still count as
    sessions; a zero-minute week has empty distributions. Disabled/unrecognized
    categories map to 其他; missing decisions or missing tags map to 未分类.
    """
    monday, today = _week_bounds(monday, today)
    start = datetime.combine(monday, time.min, SHANGHAI)
    end = datetime.combine(today + timedelta(days=1), time.min, SHANGHAI)
    with store._connect() as conn:
        conn.execute('BEGIN')
        categories = [row['name'] for row in conn.execute(
            'SELECT name FROM tag_categories WHERE enabled = 1 ORDER BY position, id')]
        # Both joins are many-to-one: recording_tags must not multiply duration.
        records = conn.execute('''
            SELECT r.created_at, r.duration_min, p.source, t.id AS tag_id,
                   t.name AS tag_name, t.category
            FROM recordings r
            LEFT JOIN recording_primary_tag p ON p.recording_id = r.recording_id
            LEFT JOIN tags t ON t.id = p.tag_id''').fetchall()

    category_rows = {name: {'name': name, 'minutes': 0, 'sessions': 0} for name in categories}
    for name in (OTHER, UNKNOWN):
        category_rows.setdefault(name, {'name': name, 'minutes': 0, 'sessions': 0})
    tag_rows = {}
    total = unknown_duration_count = 0
    for record in records:
        stamp = datetime.fromisoformat(record['created_at'])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=SHANGHAI)
        if not start <= stamp < end:
            continue
        minutes = record['duration_min']
        if minutes is None:
            unknown_duration_count += 1
            minutes = 0
        total += minutes
        if record['source'] not in ('model', 'rule') or record['tag_id'] is None or record['tag_name'] == UNKNOWN:
            name = category = UNKNOWN
        else:
            name = record['tag_name']
            category = record['category'] if record['category'] in categories else OTHER
        tag = tag_rows.setdefault(name, {'name': name, 'category': category, 'minutes': 0, 'sessions': 0})
        for row in (category_rows[category], tag):
            row['minutes'] += minutes
            row['sessions'] += 1

    by_category = by_tag = []
    if total:
        rows = list(category_rows.values())
        kept = [row for row in rows if row['sessions'] and (
            row['name'] == UNKNOWN or (row['name'] != OTHER and row['minutes'] * 100 >= total * 2))]
        by_category = _merge_other(rows, kept, total)
        rows = sorted(tag_rows.values(), key=lambda row: (-row['minutes'], row['name']))
        # Rank all tags first; preserve 未分类 even if it falls outside the six.
        kept = [row for index, row in enumerate(rows)
                if row['name'] != OTHER and (index < 6 or row['name'] == UNKNOWN)]
        by_tag = _merge_other(rows, kept, total, tag=True)

    # Explicit runtime assertions remain effective under python -O.
    for label, rows in (('by_category', by_category), ('by_tag', by_tag)):
        difference = sum(row['minutes'] for row in rows) - total
        if difference != 0:
            raise ValueError(f'{label} minutes conservation failed: difference={difference:+d}')
    return {'total_minutes': total, 'unknown_duration_count': unknown_duration_count,
            'by_category': by_category, 'by_tag': by_tag}
