"""Board presentation and forms using the existing session/CSRF policy."""
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import board, board_analysis, board_generation, tag_time
from app.minutes import SHANGHAI
from app.session import SESSION_MAX_AGE


def duration(minutes):
    if minutes is None:
        return '时长未知'
    return f'{minutes / 60:.1f} 小时' if minutes >= 60 else f'{minutes:g} 分钟'


def shares(items, total):
    """Largest remainders keep displayed integer shares at exactly 100%."""
    weights = [item['minutes'] or 0 for item in items]
    raw = [weight * 100 / total if total else 0 for weight in weights]
    rounded = [int(value) for value in raw]
    if total:
        order = sorted(range(len(items)), key=lambda i: raw[i] - rounded[i], reverse=True)
        for i in order[:100 - sum(rounded)]:
            rounded[i] += 1
    return [{**item, 'percent': rounded[i], 'bar_percent': raw[i]}
            for i, item in enumerate(items)]


def week_chart(weekly, monday, cutoff, today):
    """Pad the presentation to seven slots; statistics still stop at cutoff."""
    days = {item['day']: item for item in weekly['by_day']}
    maximum = max((item['minutes'] for key, item in days.items()
                   if key <= min(cutoff, today).isoformat()), default=0)
    rows = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        item = days.get(day.isoformat(), {'count': 0, 'minutes': 0})
        future = day > min(cutoff, today)
        rows.append({**item, 'day': day.isoformat(), 'is_today': day == today,
                     'future': future, 'bar_percent':
                     100 * item['minutes'] / maximum if maximum and not future else 0})
    return rows


def donut_chart(items):
    """Top five known positive durations plus Other; unknowns never get arcs."""
    known = sorted((item for item in items if (item['minutes'] or 0) > 0),
                   key=lambda item: (-item['minutes'], item['recording_id']))
    sectors = [{**item, 'color': i + 1} for i, item in enumerate(known[:5])]
    if len(known) > 5:
        sectors.append({'title': '其他', 'minutes': sum(item['minutes'] for item in known[5:]),
                        'color': 6, 'recording_id': None})
    sectors = shares(sectors, sum(item['minutes'] for item in known))
    offset = 0
    for i, item in enumerate(sectors):
        item.update(key=f'sector-{i}', offset=offset)
        offset += item['bar_percent']
    uncharted = [item for item in items if not item['minutes']]
    return {'sectors': sectors, 'uncharted': uncharted}


def allocation_chart(rows, total, level):
    """Decorate T2 rows for SVG; never regroup or change allocation minutes."""
    sectors = shares(rows, total)
    offset = 0
    for i, item in enumerate(sectors):
        color = ('allocation-unknown' if item['name'] == '未分类' else
                 'allocation-other' if item['name'] == '其他' else str(i % 6 + 1))
        item.update(title=item['name'], color=color, key=f'{level}-{i}', offset=offset)
        offset += item['bar_percent']
    return sectors


def allocation_comparison(weeks, current_sectors):
    """One category palette shared with the unchanged current-week donut.

    Use the largest weekly total as the common scale, retaining each week's
    aggregation order. Connections only join categories in adjacent rows.
    """
    colors = {item['name']: item['color'] for item in current_sectors}
    colors.update({'其他': 'allocation-other', '未分类': 'allocation-unknown'})
    names = dict.fromkeys(item['name'] for week in weeks for item in week['by_category'])
    used = {color for color in colors.values() if color.isdigit()}
    available = [str(i) for i in range(1, 7) if str(i) not in used]
    for name in names:
        if name not in colors:
            colors[name] = available.pop(0) if available else str(len(colors) % 6 + 1)
    maximum = max((week['total_minutes'] for week in weeks), default=0)
    rows, connections, previous = [], [], {}
    for index, week in enumerate(weeks):
        segments, current, offset = [], {}, 0
        for item in week['by_category']:
            start = offset / maximum if maximum else 0
            offset += item['minutes']
            segment = {**item, 'start': start, 'end': offset / maximum if maximum else 0,
                       'y': index * 28 + 2, 'color': colors[item['name']]}
            segments.append(segment)
            current[item['name']] = segment
            prior = previous.get(item['name'])
            if prior and prior['minutes'] and item['minutes']:
                connections.append({'name': item['name'], 'color': segment['color'],
                                    'start1': prior['start'], 'end1': prior['end'],
                                    'start2': segment['start'], 'end2': segment['end'],
                                    'y1': prior['y'] + 14, 'y2': segment['y']})
        rows.append({**week, 'y': index * 28, 'segments': segments})
        previous = current
    return {'rows': rows, 'connections': connections, 'height': len(rows) * 28 - 10,
            'legend': [{'name': name, 'color': colors[name]} for name in names]}


def parse_day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError('日期格式须为 YYYY-MM-DD。')
    return day


def install_board(app, *, settings, templates, sessions, authorize, valid_csrf,
                  csrf, session_binding, parse_form, error_page):
    cookie = 'board_csrf'

    def access(request):
        subject = sessions.decode(request.cookies.get('session'))
        user = app.state.store.get_user(subject) if subject else None
        return user if user and user.is_active else None

    @app.get('/board')
    def page(request: Request):
        user = access(request)
        if user is None:
            return error_page(request, 403, '无访问权限', '请使用白名单账户登录后查看看板。')
        today = datetime.now(SHANGHAI).date()
        try:
            day = parse_day(request.query_params['day']) if 'day' in request.query_params else today
        except ValueError:
            return error_page(request, 400, '日期无效', '日期格式须为 YYYY-MM-DD。')
        monday = board.monday_of(day)
        store = app.state.store
        daily, weekly = board.day_stats(store, day), board.week_stats(store, monday, day)
        allocation, allocation_text, allocation_error = board_generation.allocation_view(
            store, board.monday_of(today), today)
        allocation_charts = {key: allocation_chart(allocation[key], allocation['total_minutes'], key)
                             for key in ('by_category', 'by_tag')}
        try:
            comparison = allocation_comparison(tag_time.weekly_allocation(store, today=today),
                                               allocation_charts['by_category'])
        except Exception:
            # A failed historical query must not break today's cards or the
            # existing current-week fallback. Never mislabel failure as zero.
            comparison = None
        admin = bool(set(settings.admin_open_ids).intersection((user.subject, user.union_id, user.open_id)))
        token = ''
        if admin:
            token = request.cookies.get(cookie, '')
            if not valid_csrf(request, token, cookie_name=cookie):
                token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        response = templates.TemplateResponse(request=request, name='board.html', context={
            'title': '看板', 'day': day, 'today': today, 'monday': monday,
            'totals': board.totals(store), 'daily': daily, 'weekly': weekly,
            'items': shares(daily['items'], daily['minutes']),
            'week_days': week_chart(weekly, monday, day, today),
            'week_end': monday + timedelta(days=7),
            'chart': donut_chart(daily['items']),
            'allocation': allocation, 'allocation_text': allocation_text,
            'allocation_error': allocation_error,
            'allocation_charts': allocation_charts, 'allocation_comparison': comparison,
            'unknown_count': sum(item['minutes'] is None for item in daily['items']),
            'day_report': board.load_report(store, day),
            'week_report': board_generation.meeting_review(board.load_report(store, monday, 'week')),
            'history': board.report_days(store), 'csrf_token': token,
            'duration': duration, 'weekdays': '一二三四五六日',
        }, headers={'Cache-Control': 'no-store'})
        if admin:
            response.set_cookie(cookie, token, max_age=SESSION_MAX_AGE, httponly=True,
                                secure=settings.secure_cookies, samesite='lax', path='/board')
        return response

    @app.post('/board/regenerate')
    @app.post('/board/generate')
    async def generate(request: Request):
        if access(request) is None:
            return error_page(request, 403, '无访问权限', '仅管理员可以生成分析。')
        denied = authorize(request, message='仅管理员可以生成分析。')
        if denied is not None:
            return denied
        fields = await parse_form(request, cookie_name=cookie)
        if not isinstance(fields, dict):
            return fields
        try:
            day = parse_day(fields.get('day', [datetime.now(SHANGHAI).date().isoformat()])[0])
        except ValueError:
            return error_page(request, 400, '日期无效', '日期格式须为 YYYY-MM-DD。')
        regenerate = request.url.path == '/board/regenerate'
        scope = 'both' if regenerate else fields.get('scope', ['both'])[0]
        if scope not in ('day', 'week', 'both'):
            return error_page(request, 400, '范围无效', '请选择日报或周报。')
        for report_scope in (('day', 'week') if scope == 'both' else (scope,)):
            await run_in_threadpool(board_generation.generate, app.state.store, scope=report_scope, day=day)
        return RedirectResponse('/board?' + urlencode({'day': day.isoformat()}), status_code=302 if regenerate else 303,
                                headers={'Cache-Control': 'no-store'})
