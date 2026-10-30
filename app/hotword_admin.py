"""Small administrator interface sharing the session-bound admin CSRF boundary."""
import secrets
import sqlite3

from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.session import SESSION_MAX_AGE
from app.hotwords import validate_hotword, word_error
from app import hotword_upstream as upstream
from app.minutes import MinutesSourceError


def install_hotwords(app, *, settings, templates, sessions, authorize, valid_csrf,
                     csrf, session_binding, parse_form, error_page):
    def access(request):
        if sessions.decode(request.cookies.get('session')) is None:
            return error_page(request, 403, '无访问权限', '仅管理员可以管理热词。')
        return authorize(request, message='仅管理员可以管理热词。')

    def render(request, *, error='', status=200, draft=None, editing_id=None, errors=None, notice=''):
        token = request.cookies.get('admin_csrf', '')
        if not valid_csrf(request, token):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        response = templates.TemplateResponse(request=request, name='hotwords.html', context={
            'title': '热词中心', 'hotwords': app.state.store.list_hotwords(),
            'csrf_token': token, 'error': error, 'draft': draft or {},
            'editing_id': editing_id, 'errors': errors or {}, 'notice': notice,
            'upstream_plan': upstream.plan(app.state.store),
            'upstream_sync': upstream.sync_rows(app.state.store),
            'upstream_errors': upstream.error_rows(app.state.store),
        }, status_code=status, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('admin_csrf', token, max_age=SESSION_MAX_AGE, httponly=True,
                            secure=settings.secure_cookies, samesite='lax', path='/hotwords')
        return response

    @app.get('/hotwords')
    def hotwords(request: Request):
        denied = access(request)
        return denied if denied is not None else render(request)

    def source():
        return app.state.hotword_source or upstream.source_for(settings)

    async def upstream_request(request, action, hotword_id=None):
        denied = access(request)
        if denied is not None:
            return denied
        fields = await parse_form(request)
        if not isinstance(fields, dict):
            return fields
        store = app.state.store
        try:
            if action == 'sync':
                if 'confirm' in fields or 'plan_hash' in fields:
                    return JSONResponse({'error': 'stale_client', 'message': '页面已更新，请刷新后点击全部推送。'},
                                        status_code=400, headers={'Cache-Control': 'no-store'})
                result = await run_in_threadpool(upstream.execute, store, source())
            elif action == 'push':
                result = await run_in_threadpool(upstream.execute_one, store, source(), hotword_id,
                                                fields.get('force', ['0'])[0] == '1')
            elif action == 'status':
                result = await run_in_threadpool(upstream.refresh, store, source())
                result = {k: v for k, v in result.items() if k != 'hotWords'}
            else:
                result = await run_in_threadpool(upstream.import_preview, store, source())
        except (MinutesSourceError, ValueError, OSError):
            return JSONResponse({'error': 'upstream_unavailable', 'message': '千问热词读取失败，请检查连接后重试。'},
                                status_code=502, headers={'Cache-Control': 'no-store'})
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/hotwords/sync-qwennote')
    async def sync_qwennote(request: Request):
        return await upstream_request(request, 'sync')

    @app.post('/hotwords/{hotword_id}/push-qwennote')
    async def push_qwennote(request: Request, hotword_id: int):
        return await upstream_request(request, 'push', hotword_id)

    @app.post('/hotwords/import-preview')
    async def import_preview(request: Request):
        return await upstream_request(request, 'import')

    @app.post('/hotwords/qwennote-status')
    async def qwennote_status(request: Request):
        return await upstream_request(request, 'status')

    async def mutate(request, action, hotword_id=None):
        denied = access(request)
        if denied is not None:
            return denied
        fields = await parse_form(request, max_bytes=32768)
        if not isinstance(fields, dict):
            return fields
        values = {name: fields.get(name, [''])[0] for name in ('term', 'note')}
        if action == 'add' and fields.get('from_upstream', [''])[0] == '1':
            try:
                added = await run_in_threadpool(upstream.import_selected, app.state.store, values['term'])
            except ValueError as exc:
                return JSONResponse({'error': str(exc)}, status_code=400)
            return JSONResponse({'added': added, 'existing': not added}, headers={'Cache-Control': 'no-store'})
        if action in ('add', 'update'):
            errors = {}
            if word_error(values['term']):
                errors['term'] = word_error(values['term'])
            if len(values['note']) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in values['note']):
                errors['note'] = '备注最多 1000 字，须为单行。'
            if errors:
                return render(request, status=400, draft=values, editing_id=hotword_id, errors=errors)
            values = {name: value.strip() for name, value in values.items()}
            try:
                validate_hotword(**values)
            except ValueError as exc:
                return render(request, status=400, draft=values, editing_id=hotword_id, errors={'term': str(exc)})
        store = app.state.store

        def apply():
            if action == 'add':
                store.add_hotword(**values)
                return True
            if action == 'update':
                return store.update_hotword(hotword_id, **values)
            return getattr(store, action + '_hotword')(hotword_id)

        try:
            found = await run_in_threadpool(apply)
        except sqlite3.IntegrityError:
            return render(request, status=409, draft=values, editing_id=hotword_id,
                          errors={'term': '标准词已存在，请编辑已有热词。'})
        if not found:
            return render(request, error='热词不存在，请刷新后重试。', status=404)
        return RedirectResponse('/hotwords', status_code=303, headers={'Cache-Control': 'no-store'})

    @app.post('/hotwords')
    async def add(request: Request):
        return await mutate(request, 'add')

    @app.post('/hotwords/{hotword_id}/update')
    async def update(request: Request, hotword_id: int):
        return await mutate(request, 'update', hotword_id)

    @app.post('/hotwords/{hotword_id}/delete')
    async def delete(request: Request, hotword_id: int):
        return await mutate(request, 'delete', hotword_id)

    @app.post('/hotwords/{hotword_id}/toggle')
    async def toggle(request: Request, hotword_id: int):
        return await mutate(request, 'toggle', hotword_id)
