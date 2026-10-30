"""Administrator-only overrides and opt-in hotword suggestions."""
import json
import secrets
import unicodedata
from urllib.parse import quote
from datetime import datetime
from difflib import SequenceMatcher

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.hotwords import validate_hotword
from app.minutes import SHANGHAI
from app.recording_content import apply_recording_hotwords

EDIT_COOKIE = 'recording_csrf'


def edit_timestamp(value):
    return datetime.fromisoformat(value).astimezone(SHANGHAI).strftime('%m-%d %H:%M')


def diff_suggestions(original: str, content: str) -> list[dict[str, str]]:
    """Character opcodes with six characters of context on each side, at most 3."""
    def meaningful(value):
        return ''.join(char for char in value if not char.isspace()
                       and not unicodedata.category(char).startswith('P'))
    if meaningful(original) == meaningful(content):
        return []
    # Trim identical ends before matching, keeping offsets into the full strings.
    # Disable popular-character heuristics: repeated Chinese characters are text,
    # not junk. Trimming also keeps a small correction in a long note inexpensive.
    start = 0
    while start < min(len(original), len(content)) and original[start] == content[start]:
        start += 1
    old_end, new_end = len(original), len(content)
    while old_end > start and new_end > start and original[old_end - 1] == content[new_end - 1]:
        old_end -= 1
        new_end -= 1
    matcher = SequenceMatcher(None, original[start:old_end], content[start:new_end], autojunk=False)
    suggestions = []
    for kind, i, end_i, j, end_j in matcher.get_opcodes():
        i, end_i, j, end_j = i + start, end_i + start, j + start, end_j + start
        if kind not in ('replace', 'insert'):
            continue
        if meaningful(original[i:end_i]) == meaningful(content[j:end_j]):
            continue
        # Hotwords are single-line: context stops at a line boundary.
        def snippet(text, start, end):
            left = text[max(0, start - 6):start].split('\n')[-1].split('\r')[-1].split('\t')[-1]
            right = text[end:end + 6].split('\n')[0].split('\r')[0].split('\t')[0]
            return left + text[start:end] + right
        item = {'aliases': snippet(original, i, end_i), 'term': snippet(content, j, end_j)}
        if not meaningful(item['aliases']):
            continue
        try:
            validate_hotword(**item)
        except ValueError:
            continue
        if item not in suggestions:
            suggestions.append(item)
        if len(suggestions) == 3:
            break
    return suggestions


def install_recording_edit(app, *, sessions, authorize, csrf, valid_csrf, session_binding, parse_form):
    def token_for(request):
        token = request.cookies.get(EDIT_COOKIE, '')
        if not valid_csrf(request, token, cookie_name=EDIT_COOKIE):
            token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        return token

    def result(data, status=200):
        return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})

    async def fields_for(request):
        if sessions.decode(request.cookies.get('session')) is None:
            return result({'detail': '仅管理员可以编辑内容。'}, 403)
        denied = authorize(request, message='仅管理员可以编辑内容。')
        if denied is not None:
            return denied
        return await parse_form(request, max_bytes=2_000_000, cookie_name=EDIT_COOKIE)

    async def mutate(request, recording_id, *, revert=False):
        fields = await fields_for(request)
        if not isinstance(fields, dict):
            return fields
        field = fields.get('field', [''])[0]
        if field not in ('summary', 'transcript') or (not revert and 'content' not in fields):
            return result({'detail': '请检查编辑字段与正文。'}, 400)
        content = fields.get('content', [''])[0]
        if len(content) > 200_000:
            return result({'detail': '正文最多 200000 字。'}, 400)
        store = app.state.store

        def apply():
            recording = store.get_recording(recording_id)
            if recording is None:
                return result({'detail': '录音不存在。'}, 404)
            if revert:
                store.revert_override(recording_id, field)
                return result({'reverted': True})
            current = apply_recording_hotwords(
                recording, lambda text, **kw: store.apply_hotwords(text, markdown=field != 'transcript', **kw),
                fields=(field,), overrides=store.get_overrides(recording_id))
            # Save before deriving suggestions. No hotwords are written here.
            updated = store.save_override(recording_id, field, content)
            return result({'saved': True, 'updated_at': updated, 'edited_at': edit_timestamp(updated),
                           'suggestions': diff_suggestions(getattr(current, field + '_md') if getattr(current, 'has_' + field) else '', content)})
        return await run_in_threadpool(apply)

    @app.post('/recordings/{recording_id}/edit')
    async def edit(request: Request, recording_id: str):
        return await mutate(request, recording_id)

    @app.post('/recordings/{recording_id}/revert')
    async def revert(request: Request, recording_id: str):
        return await mutate(request, recording_id, revert=True)

    @app.post('/recordings/{recording_id}/hotwords/{hotword_id}/revoke')
    async def revoke_hotword(request: Request, recording_id: str, hotword_id: int):
        fields = await fields_for(request)
        if not isinstance(fields, dict):
            return fields
        field = fields.get('field', [''])[0]
        view = fields.get('view', [field])[0]
        if field not in ('summary', 'transcript') or view not in ('summary', 'transcript'):
            return result({'detail': '请检查热词字段。'}, 400)
        try:
            decision_id = int(fields.get('decision_id', [''])[0])
        except ValueError:
            return result({'detail': '请指定要撤销的热词位置。'}, 400)
        store = app.state.store
        if await run_in_threadpool(store.get_recording, recording_id) is None:
            return result({'detail': '录音不存在。'}, 404)
        await run_in_threadpool(store.revoke_hotword, recording_id, hotword_id, field, decision_id)
        return RedirectResponse('/recordings/' + quote(recording_id, safe='') + '?view=' + view,
                                status_code=303, headers={'Cache-Control': 'no-store'})

    @app.post('/hotwords/bulk')
    async def bulk(request: Request):
        fields = await fields_for(request)
        if not isinstance(fields, dict):
            return fields
        try:
            items = json.loads(fields.get('items', [''])[0])
            if not isinstance(items, list) or not 1 <= len(items) <= 3:
                raise ValueError
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'term', 'aliases'}
                        or not all(isinstance(value, str) for value in item.values())):
                    raise ValueError
                validate_hotword(**item)
        except (ValueError, TypeError):
            return result({'detail': '请提供 1–3 条有效热词。'}, 400)
        counts = await run_in_threadpool(app.state.store.bulk_add_hotwords, items)
        return result({**counts, 'message': f'已存入 {counts["added"]} 条，已存在 {counts["existing"]} 条'})

    return token_for
