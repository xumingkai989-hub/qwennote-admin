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
    """Minimal replacements, optionally completed to a bounded lexical unit.

    Character diffs split a one-character typo inside a word. Complete that unit
    only between punctuation / function-word boundaries, never a fixed context
    window. If that unit is too long, retain the minimal opcode instead.
    """
    def normalized(value):
        return ''.join(c for c in value if not c.isspace()
                       and not unicodedata.category(c).startswith('P')).casefold()

    def trim(value):
        left, right = 0, len(value)
        while left < right and (value[left].isspace() or unicodedata.category(value[left]).startswith('P')):
            left += 1
        while right > left and (value[right-1].isspace() or unicodedata.category(value[right-1]).startswith('P')):
            right -= 1
        return value[left:right]

    def too_long(value):
        return len(value) > (24 if value.isascii() else 12)

    def boundary(char):
        return (char in '\n\r\t的了和与在中把将是为及跟对从到'
                or unicodedata.category(char)[0] in ('P', 'S'))

    def unit(text, left, right):
        while left > 0 and not boundary(text[left-1]):
            left -= 1
        while right < len(text) and not boundary(text[right]):
            right += 1
        return trim(text[left:right])

    if normalized(original) == normalized(content):
        return []
    start = 0
    while start < min(len(original), len(content)) and original[start] == content[start]:
        start += 1
    old_end, new_end = len(original), len(content)
    while old_end > start and new_end > start and original[old_end-1] == content[new_end-1]:
        old_end -= 1
        new_end -= 1
    matcher = SequenceMatcher(None, original[start:old_end], content[start:new_end], autojunk=False)
    suggestions = []
    for kind, i, end_i, j, end_j in matcher.get_opcodes():
        if kind not in ('replace', 'delete'):
            continue
        i, end_i, j, end_j = i+start, end_i+start, j+start, end_j+start
        old, new = trim(original[i:end_i]), trim(content[j:end_j])
        if normalized(old) == normalized(new) or not normalized(old):
            continue
        if too_long(old) or too_long(new):
            # Defensive common-end minimization for coarse replacement opcodes.
            while old and new and old[0] == new[0]:
                old, new = old[1:], new[1:]
            while old and new and old[-1] == new[-1]:
                old, new = old[:-1], new[:-1]
            old, new = trim(old), trim(new)
            if too_long(old) or too_long(new):
                continue
        else:
            old_unit, new_unit = unit(original, i, end_i), unit(content, j, end_j)
            if (not too_long(old_unit) and not too_long(new_unit)
                    and old_unit and new_unit and kind == 'replace'):
                old, new = old_unit, new_unit
        if normalized(old) == normalized(new):
            continue
        item = {'term': new}
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
            for index, item in enumerate(items):
                if (not isinstance(item, dict) or set(item) != {'term'}
                        or not isinstance(item['term'], str)):
                    raise ValueError
                validate_hotword(**item)
        except (ValueError, TypeError) as exc:
            return result({'detail': str(exc) or '请提供 1–3 条有效热词。'}, 400)
        counts = await run_in_threadpool(app.state.store.bulk_add_hotwords, items)
        message = f'已存入 {counts["added"]} 条，已存在 {counts["existing"]} 条'
        return result({**counts, 'message': message})

    return token_for
