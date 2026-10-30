"""Administrator UI over the existing vocabulary and assistant tables."""
from datetime import datetime
import json
import secrets

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from app.minutes import SHANGHAI
from app.session import SESSION_MAX_AGE
from app.sync import SyncInProgress
from app.tag_vocabulary import TagVocabulary, log_change, merge_tags, now, refresh_usage


def change_label(row):
    detail = json.loads(row['detail'])
    source, target = detail.get('from', ''), detail.get('to', '')
    action = row['action']
    if action == 'add':
        return f'新增 {target}'
    if action == 'merge':
        return f'合并 {source} → {target}'
    if action == 'deactivate':
        return f'停用 {source}'
    if action == 'instructions':
        return '更新反馈指令'
    if action == 'run':
        return '词表维护 · ' + {'success': '完成', 'partial': '部分完成', 'running': '进行中',
                           'failed': '失败'}.get(detail.get('status'), '完成')
    return '词表变更'


def install_assistant(app, *, settings, templates, sessions, authorize, valid_csrf,
                      csrf, session_binding, parse_form, error_page):
    def access(request):
        if sessions.decode(request.cookies.get('session')) is None:
            return error_page(request, 403, '无访问权限', '仅管理员可以配置 AI 助手。')
        return authorize(request)

    def render(request, *, instructions=None, error='', status=200, saved=False):
        with app.state.store._connect() as conn:
            conn.execute('BEGIN')
            config = conn.execute('SELECT * FROM assistant_config WHERE id = 1').fetchone()
            tags = conn.execute("SELECT * FROM tags WHERE status = 'active' ORDER BY usage_count DESC, id").fetchall()
            proposals = conn.execute("SELECT * FROM tag_merge_proposals WHERE status = 'pending' ORDER BY id").fetchall()
            changes = [{'time': datetime.fromisoformat(row['at']).astimezone(SHANGHAI).strftime('%m-%d %H:%M'),
                        'label': change_label(row)} for row in conn.execute(
                            'SELECT * FROM tag_change_log ORDER BY id DESC LIMIT 20')]
        token = request.cookies.get('admin_csrf', '')
        if not valid_csrf(request, token):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        response = templates.TemplateResponse(request=request, name='assistant.html', context={
            'title': 'AI 助手配置', 'instructions': config['instructions'] if instructions is None else instructions,
            'tags': tags, 'proposals': proposals, 'changes': changes, 'csrf_token': token, 'error': error,
            'saved_at': datetime.fromisoformat(config['updated_at']).astimezone(SHANGHAI).strftime('%H:%M') if saved else '',
        }, status_code=status, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('admin_csrf', token, max_age=SESSION_MAX_AGE, httponly=True,
                            secure=settings.secure_cookies, samesite='lax', path='/assistant')
        return response

    @app.get('/assistant')
    def assistant(request: Request):
        denied = access(request)
        return denied if denied is not None else render(request, saved=request.query_params.get('saved') == '1')

    async def mutate(request, action, proposal_id=None):
        denied = access(request)
        if denied is not None:
            return denied
        # URL-encoded CJK/emoji can use up to 12 bytes per codepoint.
        fields = await parse_form(request, max_bytes=65536)
        if not isinstance(fields, dict):
            return fields
        value = lambda name: fields.get(name, [''])[0]
        instructions = value('instructions')
        if action == 'instructions' and len(instructions) > 2000:
            return render(request, instructions=instructions, error='反馈指令最多 2000 字，请缩短后保存。', status=400)
        store = app.state.store
        def apply():
            if action in ('accept', 'reject'):
                getattr(TagVocabulary(store), action + '_proposal')(proposal_id)
            else:
                with store.tag_lock(), store._connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if action == 'instructions':
                        conn.execute('UPDATE assistant_config SET instructions = ?, updated_at = ? WHERE id = 1',
                                     (instructions, now()))
                        log_change(conn, 'instructions')
                    else:
                        source = conn.execute("SELECT * FROM tags WHERE id = ? AND status = 'active'",
                                              (value('from') if action == 'merge' else value('tag_id'),)).fetchone()
                        if source is None:
                            raise ValueError('Source unavailable')
                        if action == 'merge':
                            target = conn.execute("SELECT * FROM tags WHERE id = ? AND status = 'active'", (value('to'),)).fetchone()
                            if target is None or source['id'] == target['id']:
                                raise ValueError('Target unavailable')
                            merge_tags(conn, source, target, reason='manual')
                            refresh_usage(conn)
                        else:
                            conn.execute("UPDATE tags SET status = 'deactivated', updated_at = ? WHERE id = ?", (now(), source['id']))
                            log_change(conn, 'deactivate', **{'from': source['name']})
        try:
            await run_in_threadpool(apply)
        except ValueError:
            return render(request, error='标签或提案已变更，请刷新后重试，并选择不同的启用标签。', status=400)
        except SyncInProgress:
            return render(request, instructions=instructions if action == 'instructions' else None,
                          error='标签任务正在运行，请稍后重试。', status=409)
        return RedirectResponse('/assistant?saved=1' if action == 'instructions' else '/assistant',
                                status_code=303, headers={'Cache-Control': 'no-store'})

    @app.post('/assistant/instructions')
    async def instructions(request: Request):
        return await mutate(request, 'instructions')

    @app.post('/assistant/tags/merge')
    async def merge(request: Request):
        return await mutate(request, 'merge')

    @app.post('/assistant/tags/deactivate')
    async def deactivate(request: Request):
        return await mutate(request, 'deactivate')

    @app.post('/assistant/proposals/{proposal_id}/accept')
    async def accept(request: Request, proposal_id: int):
        return await mutate(request, 'accept', proposal_id)

    @app.post('/assistant/proposals/{proposal_id}/reject')
    async def reject(request: Request, proposal_id: int):
        return await mutate(request, 'reject', proposal_id)
