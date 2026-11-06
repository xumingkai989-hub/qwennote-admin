"""Board presentation and forms using the existing session/CSRF policy."""
import secrets
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import board, board_analysis
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
            'week_days': shares(weekly['by_day'], weekly['minutes']),
            'unknown_count': sum(item['minutes'] is None for item in daily['items']),
            'day_report': board.load_report(store, day),
            'week_report': board.load_report(store, monday, 'week'),
            'history': board.report_days(store), 'csrf_token': token,
            'duration': duration, 'weekdays': '一二三四五六日',
        }, headers={'Cache-Control': 'no-store'})
        if admin:
            response.set_cookie(cookie, token, max_age=SESSION_MAX_AGE, httponly=True,
                                secure=settings.secure_cookies, samesite='lax', path='/board')
        return response

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
            day = parse_day(fields.get('day', [''])[0])
        except ValueError:
            return error_page(request, 400, '日期无效', '日期格式须为 YYYY-MM-DD。')
        scope = fields.get('scope', ['both'])[0]
        if scope not in ('day', 'week', 'both'):
            return error_page(request, 400, '范围无效', '请选择日报或周报。')
        for report_scope in (('day', 'week') if scope == 'both' else (scope,)):
            await run_in_threadpool(board_analysis.generate, app.state.store, scope=report_scope, day=day)
        return RedirectResponse('/board?' + urlencode({'day': day.isoformat()}), status_code=303,
                                headers={'Cache-Control': 'no-store'})
