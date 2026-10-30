import hashlib
import re
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.feishu import FeishuAuthError, FeishuSearchNotConfigured, FeishuUser
from app.session import SESSION_MAX_AGE, SignedCookie


def install_admin(app: FastAPI, *, settings: Settings, templates: Jinja2Templates,
                  sessions: SignedCookie, error_page, login_redirect) -> Callable[[Request], str]:
    csrf = SignedCookie(settings.session_secret, salt='admin-csrf', max_age=SESSION_MAX_AGE)

    def authorize(request: Request, *, message='仅管理员可以管理白名单。'):
        open_id = sessions.decode(request.cookies.get('session'))
        if open_id is None:
            return login_redirect()
        user = app.state.store.get_user(open_id)
        if user is None or not user.is_active or not set(settings.admin_open_ids).intersection(
                (user.subject, user.union_id, user.open_id)):
            return error_page(request, 403, '无访问权限', message)
        return None

    def session_binding(request: Request) -> str:
        # Bind to a digest so the form token never carries the session itself.
        return hashlib.sha256(request.cookies.get('session', '').encode()).hexdigest()

    def valid_csrf(request: Request, token: str, *, cookie_name='admin_csrf') -> bool:
        cookie = request.cookies.get(cookie_name, '')
        value = csrf.decode(cookie)
        return bool(token and cookie and value
                    and secrets.compare_digest(token.encode(), cookie.encode())
                    and value.startswith(session_binding(request) + ':'))

    async def parse_form(request: Request, *, max_bytes=16384, cookie_name='admin_csrf'):
        # Native HTML forms use URL encoding; no multipart dependency or JSON
        # fallback. Reject duplicate fields, including ambiguous CSRF values.
        try:
            if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                raise ValueError
            body = await request.body()
            if len(body) > max_bytes:
                raise ValueError
            fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=8)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError
        except (ValueError, UnicodeError):
            return error_page(request, 403, '请求无效', 'CSRF 校验失败，请刷新管理页后重试。')
        if 'csrf' in fields and 'csrf_token' in fields:
            return error_page(request, 403, '请求无效', 'CSRF 校验失败，请刷新后重试。')
        if not valid_csrf(request, fields.get('csrf_token', fields.get('csrf', ['']))[0], cookie_name=cookie_name):
            return error_page(request, 403, '请求无效', 'CSRF 校验失败，请刷新管理页后重试。')
        return fields

    from app.assistant import install_assistant
    install_assistant(app, settings=settings, templates=templates, sessions=sessions,
                      authorize=authorize, valid_csrf=valid_csrf, csrf=csrf,
                      session_binding=session_binding, parse_form=parse_form, error_page=error_page)

    from app.hotword_admin import install_hotwords
    install_hotwords(app, settings=settings, templates=templates, sessions=sessions,
                     authorize=authorize, valid_csrf=valid_csrf, csrf=csrf,
                     session_binding=session_binding, parse_form=parse_form, error_page=error_page)

    from app.recording_edit import install_recording_edit
    edit_token = install_recording_edit(app, sessions=sessions, authorize=authorize, csrf=csrf,
                                       valid_csrf=valid_csrf, session_binding=session_binding,
                                       parse_form=parse_form)

    from app.settings_admin import install_settings
    install_settings(app, settings=settings, templates=templates, authorize=authorize,
                     valid_csrf=valid_csrf, csrf=csrf, session_binding=session_binding,
                     parse_form=parse_form)

    from app.model_admin import install_models
    install_models(app, settings=settings, templates=templates, authorize=authorize,
                   valid_csrf=valid_csrf, csrf=csrf, session_binding=session_binding,
                   parse_form=parse_form)

    @app.get('/admin')
    def admin(request: Request):
        denied = authorize(request)
        if denied is not None:
            return denied
        query = request.query_params.get('q', '').strip()
        results, search_error = [], None
        if query:
            try:
                results = app.state.feishu_auth.search_users(query)[:20]
            except FeishuSearchNotConfigured:
                search_error = '通讯录搜索未配置'
            except FeishuAuthError:
                search_error = '通讯录搜索失败，请检查飞书应用的通讯录权限与配置，或稍后重试。'
        token = request.cookies.get('admin_csrf', '')
        if not valid_csrf(request, token):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        response = templates.TemplateResponse(request=request, name='admin.html', context={
            'title': '白名单管理', 'query': query, 'results': results,
            'search_error': search_error, 'users': app.state.store.list_users(), 'csrf_token': token,
        }, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('admin_csrf', token, max_age=SESSION_MAX_AGE, httponly=True,
                            secure=settings.secure_cookies, samesite='lax', path='/admin')
        return response

    async def mutation(request: Request, *, open_id: str | None = None):
        denied = authorize(request)
        if denied is not None:
            return denied
        fields = await parse_form(request)
        if not isinstance(fields, dict):
            return fields
        if open_id is None:
            identity = {key: fields.get(key, [''])[0] for key in ('open_id', 'name', 'avatar_url')}
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', identity['open_id'])
                    or len(identity['name']) > 1000 or len(identity['avatar_url']) > 2048):
                return error_page(request, 400, '用户信息无效', '请检查联系人信息。')
            app.state.store.add_user(FeishuUser(**identity))
        else:
            app.state.store.remove_user(open_id)
        return RedirectResponse('/admin', status_code=303, headers={'Cache-Control': 'no-store'})

    @app.post('/admin/users')
    async def add_user(request: Request):
        return await mutation(request)

    @app.post('/admin/users/{open_id}/remove')
    async def remove_user(request: Request, open_id: str):
        return await mutation(request, open_id=open_id)

    return edit_token
