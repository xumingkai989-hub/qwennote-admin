"""Administrator settings form, sharing the existing signed CSRF mechanism."""
import secrets
from datetime import datetime

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.automation import parse_settings_form
from app.minutes import SHANGHAI
from app.scheduler_tick import next_runs
from app.session import SESSION_MAX_AGE


def install_settings(app, *, settings, templates, authorize, valid_csrf, csrf,
                     session_binding, parse_form):
    cookie = 'settings_csrf'

    def render(request, *, error='', status=200, draft=None):
        token = request.cookies.get(cookie, '')
        if not valid_csrf(request, token, cookie_name=cookie):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        preferences = app.state.store.get_settings(backfill_days=settings.backfill_days)
        response = templates.TemplateResponse(request=request, name='settings.html', context={
            'title': '设置中心', 'preferences': draft or preferences.values(),
            'csrf_token': token, 'error': error,
            'saved': request.query_params.get('saved') == '1' and not error,
            'next_runs': next_runs(app.state.store, preferences, datetime.now(SHANGHAI)),
        }, status_code=status, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie(cookie, token, max_age=SESSION_MAX_AGE, httponly=True,
                            secure=settings.secure_cookies, samesite='lax', path='/settings')
        return response

    @app.get('/settings')
    def settings_page(request: Request):
        denied = authorize(request, message='仅管理员可以管理设置。')
        return denied if denied is not None else render(request)

    @app.post('/settings')
    async def save(request: Request):
        denied = authorize(request, message='仅管理员可以管理设置。')
        if denied is not None:
            return denied
        fields = await parse_form(request, cookie_name=cookie)
        if not isinstance(fields, dict):
            return fields
        try:
            preferences = parse_settings_form(fields)
        except ValueError as error:
            draft = {key: values[0] for key, values in fields.items()
                     if key not in ('csrf', 'csrf_token', 'sync_enabled', 'tag_enabled')}
            draft.update({name: fields.get(name) == ['true'] for name in ('sync_enabled', 'tag_enabled')})
            return render(request, error=str(error), status=400, draft=draft)
        app.state.store.save_settings(preferences)
        return RedirectResponse('/settings?saved=1', status_code=303, headers={'Cache-Control': 'no-store'})
