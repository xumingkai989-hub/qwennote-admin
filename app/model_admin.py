"""Administrator-only model management with session-bound CSRF."""
import secrets

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.models import ModelError, ModelRegistry, claude_configuration, messages_url
from app.session import SESSION_MAX_AGE

HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


def install_models(app, *, settings, templates, authorize, valid_csrf, csrf, session_binding, parse_form):
    cookie = 'models_csrf'

    def render(request, error='', status=200):
        token = request.cookies.get(cookie, '')
        if not valid_csrf(request, token, cookie_name=cookie):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        response = templates.TemplateResponse(request=request, name='models.html', context={
            'title': '模型中心', 'models': ModelRegistry(app.state.store).list(),
            'claude': claude_configuration(), 'csrf_token': token, 'error': error,
        }, status_code=status, headers=HEADERS)
        response.set_cookie(cookie, token, max_age=SESSION_MAX_AGE, httponly=True,
                            secure=settings.secure_cookies, samesite='lax', path='/models')
        return response

    @app.get('/models')
    def page(request: Request):
        denied = authorize(request, message='仅管理员可以管理模型。')
        return denied if denied is not None else render(request)

    @app.get('/models/data')
    def data(request: Request):
        denied = authorize(request, message='仅管理员可以管理模型。')
        if denied is not None:
            return denied
        return JSONResponse({'models': [row.public() for row in ModelRegistry(app.state.store).list()],
                             'claude': claude_configuration()}, headers=HEADERS)

    async def mutate(request, action, ident=None):
        denied = authorize(request, message='仅管理员可以管理模型。')
        if denied is not None:
            return denied
        fields = await parse_form(request, cookie_name=cookie)
        if not isinstance(fields, dict):
            return fields
        registry = ModelRegistry(app.state.store)
        wants_json = action == 'test' or 'application/json' in request.headers.get('accept', '')
        try:
            if action == 'add':
                ident = await run_in_threadpool(registry.add, **{
                    name: fields.get(name, [''])[0] for name in ('name', 'provider', 'base_url', 'model_id', 'api_key')})
            elif action == 'test':
                credentials = await run_in_threadpool(registry.credentials, ident)
                try:
                    async with httpx.AsyncClient(transport=getattr(app.state, 'model_transport', None),
                            timeout=30, follow_redirects=False, trust_env=False) as client:
                        result = await client.post(messages_url(credentials.base_url),
                            headers={'x-api-key': credentials.auth_token, 'anthropic-version': '2023-06-01'},
                            json={'model': credentials.model, 'max_tokens': 8,
                                  'messages': [{'role': 'user', 'content': 'Hi'}]})
                    if result.status_code != 200:
                        raise ModelError('测试失败：HTTP ' + str(result.status_code), 502)
                    try:
                        body = result.json()
                        if not isinstance(body, dict) or not isinstance(body.get('content'), list) or not body['content']:
                            raise ValueError
                    except (ValueError, TypeError):
                        raise ModelError('测试失败：响应不符合 Anthropic Messages 协议。', 502) from None
                except httpx.TimeoutException:
                    raise ModelError('测试失败：请求超时。', 502) from None
                except httpx.RequestError:
                    raise ModelError('测试失败：无法连接端点。', 502) from None
                except ModelError:
                    raise
                except Exception:
                    raise ModelError('测试失败：请求失败，请检查端点与协议。', 502) from None
            else:
                await run_in_threadpool(registry.mutate, ident, action)
        except ModelError as error:
            return (JSONResponse({'ok': False, 'error': str(error)}, status_code=error.status, headers=HEADERS)
                    if wants_json else render(request, str(error), error.status))
        # No credentials or request fields are ever reflected in mutation responses.
        return (JSONResponse({'ok': True, 'id': ident}, headers=HEADERS) if wants_json else
                RedirectResponse('/models', status_code=303, headers=HEADERS))

    @app.post('/models')
    async def add(request: Request):
        return await mutate(request, 'add')

    @app.post('/models/{model_id}/test')
    async def test(request: Request, model_id: int):
        return await mutate(request, 'test', model_id)

    @app.post('/models/{model_id}/default')
    async def default(request: Request, model_id: int):
        return await mutate(request, 'default', model_id)

    @app.post('/models/{model_id}/toggle')
    async def toggle(request: Request, model_id: int):
        return await mutate(request, 'toggle', model_id)

    @app.post('/models/{model_id}/delete')
    async def delete(request: Request, model_id: int):
        return await mutate(request, 'delete', model_id)
