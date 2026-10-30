from app.mcp import McpMinutesSource
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.recording_edit import EDIT_COOKIE, edit_timestamp
from app.admin import install_admin
from app.config import Settings
from app.feishu import FeishuAuth, FeishuAuthError, HttpFeishuAuth
from app.home_presentation import date_groups
from app.minutes import FixtureMinutesSource, HttpMinutesSource, MinutesSource, SHANGHAI
from app.public_api import install_public_api
from app.recording_content import (apply_recording_hotwords, download_disposition, export_markdown, render_summary,
                                   transcript_segments, transcript_presentation)
from app.search import search_snippets
from app.session import SESSION_MAX_AGE, STATE_MAX_AGE, SignedCookie
from app.store import SQLiteStore, Store
from app.sync import SyncInProgress, run_sync

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
logger = logging.getLogger(__name__)


def list_filters(request: Request, *, include_dates: bool = True) -> dict:
    query = request.query_params
    page = int(query.get('page', '1'))
    if page < 1:
        raise ValueError('页码须为正整数。')
    # Home and search intentionally ignore even malformed legacy date parameters.
    # Retain the date-aware path for detail links and future restoration.
    if not include_dates:
        return {'start': None, 'end': None, 'mode': 'all', 'params': {}, 'page': page}
    today = datetime.now(SHANGHAI).date()
    groups = sum(('date' in query, 'yesterday' in query, 'from' in query or 'to' in query))
    if groups > 1:
        raise ValueError('请选择一种日期筛选方式。')

    def parse_date(value: str) -> date:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError('日期格式须为 YYYY-MM-DD。')
        return result

    params = {}
    if 'date' in query:
        start = end = parse_date(query['date'])
        mode = 'date'
        params = {'date': start.isoformat()}
    elif 'from' in query or 'to' in query:
        # The one-row form supports a single date and clearing both fields.
        # Missing endpoints remain invalid; empty values come from the form.
        if 'from' not in query or 'to' not in query:
            raise ValueError('日期区间须提供两端。')
        if not query['from'] and not query['to']:
            start = end = None
            mode = 'all'
        elif query['from'] and not query['to']:
            start = end = parse_date(query['from'])
            mode = 'date'
            params = {'date': start.isoformat()}
        else:
            start, end = parse_date(query['from']), parse_date(query['to'])
            if start > end:
                raise ValueError('起始日期不能晚于结束日期。')
            mode = 'range'
            params = {'from': start.isoformat(), 'to': end.isoformat()}
    elif 'yesterday' in query:
        if query['yesterday'] != '1':
            raise ValueError('昨天筛选须使用 yesterday=1。')
        start = end = today - timedelta(days=1)
        mode = 'yesterday'
        params = {'yesterday': '1'}
    else:
        start = end = None
        mode = 'all'
    return {'start': start, 'end': end, 'mode': mode, 'params': params, 'page': page}


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_feishu_auth(request: Request) -> FeishuAuth:
    return request.app.state.feishu_auth


def get_minutes_source(request: Request) -> MinutesSource:
    return request.app.state.minutes_source


def create_app(
    *, settings: Settings | None = None,
    feishu_auth: FeishuAuth | None = None,
    store: Store | None = None,
    minutes_source: MinutesSource | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    repository = store if store is not None else SQLiteStore(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store.migrate()
        for subject in settings.admin_open_ids:
            app.state.store.seed_admin(subject)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
    app.state.store = repository
    install_public_api(app)
    app.state.feishu_auth = feishu_auth if feishu_auth is not None else HttpFeishuAuth(settings)
    if minutes_source is None:
        minutes_source = (FixtureMinutesSource(settings.minutes_fixture_dir)
                          if settings.minutes_source == 'fixture' else
                          McpMinutesSource() if settings.minutes_source == 'mcp' else HttpMinutesSource())
    app.state.minutes_source = minutes_source
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    sessions = SignedCookie(settings.session_secret, salt="web-session", max_age=SESSION_MAX_AGE)
    states = SignedCookie(settings.session_secret, salt="oauth-state", max_age=STATE_MAX_AGE)

    def navigation_context(request: Request):
        subject = sessions.decode(request.cookies.get('session'))
        user = app.state.store.get_user(subject) if subject else None
        active = user is not None and user.is_active
        return {'navigation_user': user if active else None,
                'navigation_admin': bool(active and set(settings.admin_open_ids).intersection(
                    (user.subject, user.union_id, user.open_id)))}

    templates.context_processors.append(navigation_context)

    def set_cookie(response: Response, name: str, value: str, max_age: int):
        response.set_cookie(
            name, value, max_age=max_age, httponly=True,
            secure=settings.secure_cookies, samesite="lax", path="/",
        )

    def finish_callback(response: Response) -> Response:
        response.delete_cookie(
            "oauth_state", path="/", httponly=True,
            secure=settings.secure_cookies, samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def error_page(request: Request, status: int, title: str, message: str, *,
                   denied_open_id: str | None = None, denied_union_id: str | None = None,
                   denied_name: str = "") -> Response:
        return finish_callback(templates.TemplateResponse(
            request=request, name="error.html",
            context={"title": title, "message": message,
                     "denied_open_id": denied_open_id, "denied_union_id": denied_union_id,
                     "denied_name": denied_name}, status_code=status,
        ))

    def web_access_error(request: Request, open_id: str | None, message: str) -> Response:
        return error_page(request, 403 if open_id else 401,
                          '无访问权限' if open_id else '请先登录', message)

    def login_redirect():
        state = secrets.token_urlsafe(32)
        response = RedirectResponse(AUTHORIZE_URL + "?" + urlencode({
            "client_id": settings.feishu_app_id,
            "redirect_uri": settings.feishu_redirect_uri,
            "response_type": "code",
            "state": state,
        }), status_code=302)
        set_cookie(response, "oauth_state", states.encode(state), STATE_MAX_AGE)
        response.headers["Cache-Control"] = "no-store"
        return response

    edit_token = install_admin(app, settings=settings, templates=templates, sessions=sessions,
                  error_page=error_page, login_redirect=login_redirect)

    @app.get('/search', response_class=HTMLResponse)
    def search(request: Request, store: Annotated[Store, Depends(get_store)]):
        open_id = sessions.decode(request.cookies.get('session'))
        user = store.get_user(open_id) if open_id else None
        if user is None or not user.is_active:
            return web_access_error(request, open_id, '请登录后搜索录音。')
        query = request.query_params.get('q', '').strip()
        if not query:
            return RedirectResponse('/', status_code=302, headers={'Cache-Control': 'no-store'})
        try:
            filters = list_filters(request, include_dates=False)
        except ValueError:
            return error_page(request, 400, '页码无效',
                              '请使用正整数页码。')
        recordings = store.search_recordings(query, filters['start'], filters['end'], filters['page'])
        params = {'q': query, **filters['params']}

        def page_url(page: int) -> str:
            return '/search?' + urlencode({**params, 'page': page})

        return templates.TemplateResponse(request=request, name='home.html', context={
            'title': f'搜索「{query}」', 'user': user, 'sync_run': store.latest_sync(),
            'search_query': query, 'filters': filters, 'recordings': recordings,
            'snippets': {item.recording_id: search_snippets(item, query) for item in recordings.items},
            'detail_query': '?' + urlencode({**params, 'page': recordings.page}),
            'previous_url': page_url(recordings.page - 1) if recordings.page > 1 else None,
            'next_url': page_url(recordings.page + 1) if recordings.page < recordings.pages else None,
        }, headers={'Cache-Control': 'no-store'})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, store: Annotated[Store, Depends(get_store)]):
        open_id = sessions.decode(request.cookies.get("session"))
        user = store.get_user(open_id) if open_id else None
        if user is not None and user.is_active:
            try:
                filters = list_filters(request, include_dates=False)
            except ValueError:
                return error_page(request, 400, '页码无效',
                                  '请使用正整数页码。')
            # Reuse the lightweight, paginated list query without changing the
            # data layer. Full representable bounds include every stored date.
            recordings = store.list_recordings(
                filters['start'] or date.min, filters['end'] or date.max, filters['page'])

            def page_url(page: int) -> str:
                return '/?' + urlencode({**filters['params'], 'page': page})

            response = templates.TemplateResponse(request=request, name="home.html", context={
                "user": user, "sync_run": store.latest_sync(),
                'filters': filters, 'recordings': recordings,
                'date_groups': date_groups(recordings.items, store),
                'detail_query': ('?' + urlencode({**filters['params'], 'page': recordings.page}))
                                if filters['params'] or recordings.page > 1 else '',
                'previous_url': page_url(recordings.page - 1) if recordings.page > 1 else None,
                'next_url': page_url(recordings.page + 1) if recordings.page < recordings.pages else None,
            })
            response.headers["Cache-Control"] = "no-store"
            return response
        if open_id:
            return error_page(request, 403, '无访问权限', '你的账号不在访问白名单中。')
        return login_redirect()

    def recording_for_request(request: Request, store: Store, recording_id: str):
        open_id = sessions.decode(request.cookies.get('session'))
        user = store.get_user(open_id) if open_id else None
        if user is None or not user.is_active:
            return None, web_access_error(request, open_id, '请登录后查看录音。')
        recording = store.get_recording(recording_id)
        if recording is None:
            return None, error_page(request, 404, '录音不存在', '找不到该录音，请返回列表。')
        return recording, None

    # Register the suffix route first, before the generic detail route.
    @app.get('/recordings/{recording_id}.md')
    def recording_markdown(request: Request, recording_id: str,
                           store: Annotated[Store, Depends(get_store)]):
        recording, error = recording_for_request(request, store, recording_id)
        if error is not None:
            return error
        recording = apply_recording_hotwords(recording, store.apply_hotwords,
                                            overrides=store.get_overrides(recording_id))
        return Response(export_markdown(recording), media_type='text/markdown', headers={
            'Content-Disposition': download_disposition(recording.title),
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })

    @app.get('/recordings/{recording_id}', response_class=HTMLResponse)
    def recording_detail(request: Request, recording_id: str,
                         store: Annotated[Store, Depends(get_store)]):
        recording, error = recording_for_request(request, store, recording_id)
        if error is not None:
            return error
        try:
            query = request.query_params.get('q', '').strip()
            filters = list_filters(request)
            params = {'q': query} if query else {}
            back_url = ('/search?' if query else '/?') + urlencode(
                {**params, **filters['params'], 'page': filters['page']})
        except ValueError:
            back_url = '/'
        overrides = store.get_overrides(recording_id)
        recording = apply_recording_hotwords(recording, store.apply_hotwords, fields=(), overrides=overrides)
        default_view = 'summary' if recording.has_summary or not recording.has_transcript else 'transcript'
        view = request.query_params.get('view', default_view)
        if view not in ('summary', 'transcript'):
            view = default_view
        recording = apply_recording_hotwords(
            recording, lambda text: store.apply_hotwords(text, markdown=view != 'transcript'), fields=(view,), overrides=overrides)
        can_edit = navigation_context(request)['navigation_admin']
        token = edit_token(request) if can_edit else ''
        response = templates.TemplateResponse(request=request, name='detail.html', context={
            'title': recording.title or '未命名录音', 'recording': recording, 'back_url': back_url,
            'view': view, 'can_edit': can_edit, 'edit_csrf': token,
            'edit_content': getattr(recording, view + '_md') if getattr(recording, 'has_' + view) else '',
            'edited_at': edit_timestamp(overrides[view]['updated_at']) if view in overrides else None,
            'view_urls': {name: request.url.include_query_params(view=name) for name in ('summary', 'transcript')},
            'summary_html': render_summary(recording.summary_md) if view == 'summary' and recording.has_summary else None,
            'segments': transcript_presentation(transcript_segments(recording.transcript_md)) if view == 'transcript' and recording.has_transcript else [],
        }, headers={'Cache-Control': 'no-store'})
        if can_edit:
            set_cookie(response, EDIT_COOKIE, token, SESSION_MAX_AGE)
        return response

    @app.post('/auth/logout')
    def logout(request: Request):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': '请求来源无效'}, status_code=403)
        response = RedirectResponse('/', status_code=302)
        response.delete_cookie('session', path='/', httponly=True,
                               secure=settings.secure_cookies, samesite='lax')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/sync')
    def sync(
        request: Request,
        source: Annotated[MinutesSource, Depends(get_minutes_source)],
        store: Annotated[Store, Depends(get_store)],
    ):
        open_id = sessions.decode(request.cookies.get('session'))
        user = store.get_user(open_id) if open_id else None
        if user is None or not user.is_active:
            return JSONResponse({'detail': '无访问权限' if open_id else '请先登录'},
                                status_code=403 if open_id else 401)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': '请求来源无效'}, status_code=403)
        try:
            run = run_sync(source, store, backfill_days=settings.backfill_days)
        except SyncInProgress:
            return JSONResponse({'detail': '同步正在进行，请稍后重试'}, status_code=409,
                                headers={'Cache-Control': 'no-store'})
        return JSONResponse(run.result(), headers={'Cache-Control': 'no-store'})

    @app.get("/auth/callback")
    def callback(
        request: Request,
        auth: Annotated[FeishuAuth, Depends(get_feishu_auth)],
        store: Annotated[Store, Depends(get_store)],
        code: str | None = None,
        state: str | None = None,
    ):
        if not code or not code.strip():
            return error_page(request, 400, "登录失败", "缺少授权码，请重新登录。")
        expected_state = states.decode(request.cookies.get("oauth_state"))
        if not state or not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
            return error_page(request, 400, "登录失败", "登录请求已失效，请重新登录。")
        try:
            identity = auth.exchange_code(code)
        except FeishuAuthError:
            return error_page(request, 502, "登录失败", "飞书认证暂时失败，请重新登录。")
        user = store.get_user(identity.subject, identity.open_id)
        if user is None or not user.is_active:
            logger.warning("auth: whitelist miss union_id=%r open_id=%r name=%r",
                           identity.union_id, identity.open_id, identity.name)
            return error_page(request, 403, "无访问权限", "你没有访问权限，你的账号不在访问白名单中。",
                              denied_open_id=identity.open_id, denied_union_id=identity.union_id,
                              denied_name=identity.name)
        store.update_profile(identity)
        response = RedirectResponse("/", status_code=302)
        set_cookie(response, "session", sessions.encode(user.subject), SESSION_MAX_AGE)
        return finish_callback(response)

    return app
