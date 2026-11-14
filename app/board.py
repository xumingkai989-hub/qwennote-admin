"""Shanghai meeting statistics and one-shot day/week report CLI."""
import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path

from app.minutes import SHANGHAI


def _date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def _records(store, start, end):
    """Read only board fields and tags in one snapshot; normalize before filtering."""
    with store._connect() as conn:
        conn.execute('BEGIN')
        rows = conn.execute(
            'SELECT recording_id, title, duration_min, created_at, '
            'substr(summary_md, 1, 300) AS summary FROM recordings').fetchall()
        tags = {}
        for row in conn.execute('SELECT recording_id, tag FROM recording_tags ORDER BY position, tag'):
            tags.setdefault(row['recording_id'], []).append(row['tag'])
    result = []
    for row in rows:
        stamp = datetime.fromisoformat(row['created_at'])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=SHANGHAI)
        stamp = stamp.astimezone(SHANGHAI)
        if start <= stamp.date() <= end:
            result.append({'recording_id': row['recording_id'], 'title': row['title'],
                           'minutes': row['duration_min'], 'created_at': stamp,
                           'tags': tags.get(row['recording_id'], []), 'summary': row['summary'] or ''})
    return sorted(result, key=lambda item: (item['created_at'], item['recording_id']))


def _numbers(items):
    return {'count': len(items), 'minutes': sum(item['minutes'] or 0 for item in items)}


def totals(store) -> dict:
    with store._connect() as conn:
        row = conn.execute('SELECT count(*), coalesce(sum(duration_min), 0) FROM recordings').fetchone()
    return {'count': row[0], 'minutes': row[1]}


def monday_of(day):
    day = _date(day)
    return day - timedelta(days=day.weekday())


def load_report(store, day, scope='day'):
    """Read an archive without using its numeric snapshot for live statistics."""
    with store._connect() as conn:
        row = conn.execute('SELECT * FROM board_reports WHERE day = ? AND scope = ?',
                           (_date(day).isoformat(), scope)).fetchone()
    return dict(row) if row else None


def report_days(store):
    with store._connect() as conn:
        return [row['day'] for row in conn.execute(
            "SELECT day FROM board_reports WHERE scope = 'day' ORDER BY day DESC LIMIT 30")]


def _day_stats(items):
    return {**_numbers(items), 'items': [
        {'recording_id': item['recording_id'], 'title': item['title'], 'minutes': item['minutes'],
         'created_at': item['created_at'].strftime('%H:%M'), 'tags': item['tags'],
         'summary_head': item['summary'][:120]} for item in items]}


def day_stats(store, day) -> dict:
    day = _date(day)
    return _day_stats(_records(store, day, day))


def _week_bounds(monday, today):
    monday, today = _date(monday), _date(today)
    if monday.weekday() != 0 or not 0 <= (today - monday).days <= 6:
        raise ValueError('monday must be Monday and today must be in that week')
    return monday, today


def _week_stats(items, monday, today):
    # Empty ranges have an empty distribution. Otherwise include zero-meeting days.
    by_day = []
    if items:
        for offset in range((today - monday).days + 1):
            day = monday + timedelta(days=offset)
            by_day.append({'day': day.isoformat(), **_numbers(
                [item for item in items if item['created_at'].date() == day])})
    return {**_numbers(items), 'by_day': by_day}


def week_stats(store, monday, today) -> dict:
    monday, today = _week_bounds(monday, today)
    return _week_stats(_records(store, monday, today), monday, today)


def main(argv=None):
    from app.board_analysis import build_report
    from app.board_generation import generate
    from app.store import SQLiteStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=date.fromisoformat, default=None, help='截止日期，默认上海今天')
    parser.add_argument('--db', type=Path, default=None, help='数据库路径，默认 DB_PATH')
    parser.add_argument('--print', dest='print_only', action='store_true', help='打印数字与分析，不迁移、不写库')
    parser.add_argument('--no-analysis', action='store_true', help='离线核对数字，不调用模型（仅限 --print）')
    args = parser.parse_args(argv)
    if args.no_analysis and not args.print_only:
        parser.error('--no-analysis requires --print')
    db_path = args.db or (Path(os.environ['DB_PATH']) if os.environ.get('DB_PATH') else None)
    if db_path is None:
        parser.error('set DB_PATH or --db')
    db_path = db_path.expanduser()
    if args.print_only and not db_path.is_file():
        parser.error('database does not exist')
    store = SQLiteStore(db_path)
    if not args.print_only:
        store.migrate()
    day = args.day or datetime.now(SHANGHAI).date()
    reports = {}
    for scope in ('day', 'week'):
        reports[scope] = (build_report(store, scope=scope, day=day, analyze=not args.no_analysis)
                          if args.print_only else generate(store, scope=scope, day=day))
    print(json.dumps({'totals': totals(store), **reports}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
