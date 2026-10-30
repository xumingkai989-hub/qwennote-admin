from hotword_fixtures import seed_rule
"""Offline integration checks for opt-in edits, synchronization and CSRF."""
import hashlib
import json
import re
import secrets
from datetime import datetime
from html import unescape
from urllib.parse import urlencode

import pytest

from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.recording_edit import diff_suggestions
from app.session import SESSION_MAX_AGE, SignedCookie
from fakes import FakeMinutesSource
from test_admin import replace_session
from test_login import login

ORIGINAL = '小红直播\n\nAI 助手'
NEW = '星火直播\n\nAI Tutor'
PATH = '/recordings/edit-test'


@pytest.fixture
def editing(client, store):
    meta = RecordingMeta('edit-test', '编辑验收', datetime.now(SHANGHAI), 10)
    store.upsert_recording(meta, RecordingDetail('edit-test', ORIGINAL, ORIGINAL, True, True), 'before')
    assert login(client).status_code == 302
    return client


def token(client, field='summary'):
    page = client.get(PATH + '?view=' + field)
    return unescape(re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page.text)[1])


def post(client, action='edit', field='summary', content=NEW, **extra):
    return client.post(PATH + '/' + action, data={
        'csrf_token': token(client, field), 'field': field, 'content': content, **extra})


def base_rows(store):
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute('SELECT * FROM recordings')]


def visible(html):
    return html.split('<div id="detail-display">')[1]


@pytest.mark.parametrize('field', ['summary', 'transcript'])
def test_save_display_trace_and_revert(editing, store, field):
    before = base_rows(store)
    response = post(editing, field=field)
    assert response.status_code == 200
    data = response.json()
    assert data['saved'] is True and data['suggestions'] == [
        {'term': '星火直播'}, {'term': 'AI Tutor'}]
    saved = store.get_overrides('edit-test')[field]
    assert saved['content_md'] == NEW and saved['updated_at'] == data['updated_at']
    html = editing.get(PATH + '?view=' + field).text
    assert '星火直播' in visible(html) and '小红直播' not in visible(html)
    assert '已手动编辑 · ' + data['edited_at'] in html and 'id="revert-button"' in html
    assert base_rows(store) == before and store.list_hotwords() == []
    assert post(editing, action='revert', field=field).json() == {'reverted': True}
    assert store.get_overrides('edit-test') == {}
    html = editing.get(PATH + '?view=' + field).text
    assert '小红直播' in visible(html) and '已手动编辑 ·' not in html
    assert base_rows(store) == before


@pytest.mark.parametrize('field', ['summary', 'transcript'])
def test_actual_sync_preserves_override_then_revert_uses_latest_base(editing, store, field):
    post(editing, field=field)
    saved = store.get_overrides('edit-test')
    original = store.get_recording('edit-test')
    meta = RecordingMeta('edit-test', original.title, original.created_at, 10)
    editing.app.state.minutes_source = FakeMinutesSource([meta], {
        'edit-test': RecordingDetail('edit-test', '同步更新后的总结', '同步更新后的转写', True, True)})
    result = editing.post('/sync').json()
    assert result['status'] == 'success' and result['updated'] == 1
    assert store.get_overrides('edit-test') == saved
    assert '星火直播' in visible(editing.get(PATH + '?view=' + field).text)
    post(editing, action='revert', field=field)
    assert '同步更新后的' in visible(editing.get(PATH + '?view=' + field).text)
    assert store.get_overrides('edit-test') == {}


def test_fields_are_independent_and_second_save_diffs_previous_override(editing, store):
    post(editing, field='summary')
    result = post(editing, field='summary', content='星火直播\n\nAI Mentor').json()
    assert result['suggestions'] == [{'term': 'AI Mentor'}]
    post(editing, field='transcript', content='人工修改转写')
    assert len(store.get_overrides('edit-test')) == 2
    post(editing, action='revert')
    assert set(store.get_overrides('edit-test')) == {'transcript'}


def test_editor_starts_with_hotword_display_and_override_is_authoritative(editing, store):
    seed_rule(store, '星火直播', '小红直播')
    from test_hotwords import approve_rules
    approve_rules(store, 'edit-test')
    html = editing.get(PATH).text
    aliases = json.loads(re.search(r'<script id="edit-source"[^>]*>(.*?)</script>', html, re.S)[1])
    assert aliases == '星火直播\n\nAI 助手'
    result = post(editing, content='星火直播\n\nAI Tutor').json()
    assert result['suggestions'] == [{'term': 'AI Tutor'}]
    seed_rule(store, '再次替换', '星火直播')
    assert '星火直播' in visible(editing.get(PATH).text)
    assert '再次替换' not in visible(editing.get(PATH).text)


@pytest.mark.parametrize('field', ['summary', 'transcript'])
def test_empty_override_is_saved_and_not_replaced_by_base(editing, store, field):
    result = post(editing, field=field, content='').json()
    assert result['saved'] and result['suggestions'] == []
    assert store.get_overrides('edit-test')[field]['content_md'] == ''
    html = editing.get(PATH + '?view=' + field).text
    assert '小红直播' not in visible(html) and '已手动编辑' in html


def test_empty_base_can_gain_override_and_safe_editor_content(editing, store):
    meta = RecordingMeta('edit-test', '编辑验收', datetime.now(SHANGHAI), 10)
    store.upsert_recording(meta, RecordingDetail('edit-test', '', '', False, False), 'empty')
    value = '\n</textarea><script>alert(1)</script> & "'
    assert post(editing, content=value).status_code == 200
    html = editing.get(PATH).text
    assert 'aria-current="page">总结' in html
    assert '&lt;/textarea&gt;&lt;script&gt;' in html
    assert json.loads(re.search(r'<script id="edit-source"[^>]*>(.*?)</script>', html, re.S)[1]) == value
    assert '<script>alert(1)</script>' not in html


@pytest.mark.parametrize('path', [PATH + '.md', '/api/v1/recordings/edit-test',
                                      '/api/v1/daily/' + datetime.now(SHANGHAI).strftime('%Y-%m-%d') + '?format=md'])
def test_exports_and_body_api_use_overrides(editing, store, path):
    post(editing, content='手动总结')
    post(editing, field='transcript', content='手动转写')
    key = 'qn_' + secrets.token_hex(16)
    store.create_api_key('edit-export-test', hashlib.sha256(key.encode()).hexdigest())
    response = editing.get(path, headers={'Authorization': 'Bearer ' + key})
    assert response.status_code == 200
    assert '手动总结' in response.text and '手动转写' in response.text
    assert '小红直播' not in response.text


@pytest.mark.parametrize('old,new,expected', [
    ('abcdef错ghijkl', 'abcdef对ghijkl', [{'term': '对'}]),
    ('abcXYZdef', 'abcNEWXYZdef', []),
    ('小红直播', '星火直播', [{'term': '星火直播'}]),
    ('AI 助手', 'AI Tutor', [{'term': 'AI Tutor'}]),
    ('相同', '相同', []), ('你好，世界！', '你好 世界。', []),
    ('AI 助手', 'AI  助手', []), ('a,b,a', 'ab,a', []),
    ('删除词', '', []), ('', '新增全文没有可替换原词', []),
])
def test_character_diff(old, new, expected):
    assert diff_suggestions(old, new) == expected


def test_diff_caps_three_and_ignores_punctuation_before_valid_changes():
    old = '，\n' + '\n'.join('abcdefghijkl' + char + 'mnopqrstuvwx' for char in '甲乙丙丁戊')
    new = '。\n' + '\n'.join('abcdefghijkl' + char + 'mnopqrstuvwx' for char in '一二三四五')
    assert diff_suggestions(old, new) == [
        {'term': b}
        for a, b in zip('甲乙丙', '一二三')]


def test_bulk_confirmation_skips_existing_and_is_idempotent(editing, store):
    response = post(editing)
    assert store.list_hotwords() == []  # Closing/ignoring suggestions cannot insert.
    items = response.json()['suggestions']
    seed_rule(store, items[0]['term'], '已有提示')
    data = {'csrf_token': token(editing), 'items': json.dumps(items)}
    counts = editing.post('/hotwords/bulk', data=data).json()
    assert counts == {'added': 1, 'existing': 1, 'message': '已存入 1 条，已存在 1 条'}
    assert editing.post('/hotwords/bulk', data=data).json()['existing'] == 2
    assert not hasattr(next(word for word in store.list_hotwords() if word.term == items[0]['term']), 'aliases')
    assert store.get_overrides('edit-test')['summary']['content_md'] == NEW


@pytest.mark.parametrize('items', [[], [{}], [{'term': ''}],
                                   [{'term': 'c\nd'}],
                                   [{'term': 1}], ['a'],
                                   [{'term': 'b'}] * 4])
def test_invalid_bulk_is_atomic(editing, store, items):
    assert editing.post('/hotwords/bulk', data={'csrf_token': token(editing), 'items': json.dumps(items)}).status_code == 400
    assert store.list_hotwords() == []


@pytest.mark.parametrize('path', [PATH + '/edit', PATH + '/revert', '/hotwords/bulk'])
@pytest.mark.parametrize('identity', ['anonymous', 'member', 'inactive'])
def test_write_requires_active_admin(editing, store, settings, path, identity):
    edit_token = token(editing)
    if identity == 'anonymous':
        editing.cookies.delete('session')
    elif identity == 'member':
        store.add_user(FeishuUser('ou_reader', '读者', ''))
        signer = SignedCookie(settings.session_secret, salt='web-session', max_age=SESSION_MAX_AGE)
        editing.cookies.delete('session')
        editing.cookies.set('session', signer.encode('ou_reader'))
        html = editing.get(PATH).text
        assert 'id="edit-button"' not in html and 'id="recording-editor"' not in html
    else:
        store.remove_user(settings.admin_open_id)
    assert editing.post(path, data={'csrf_token': edit_token, 'field': 'summary', 'content': NEW}).status_code == 403
    assert store.get_overrides('edit-test') == {} and store.list_hotwords() == []


@pytest.mark.parametrize('path', [PATH + '/edit', PATH + '/revert', '/hotwords/bulk'])
@pytest.mark.parametrize('bad', ['missing', 'wrong', 'session', 'duplicate'])
def test_write_rejects_bad_csrf(editing, store, settings, path, bad):
    fields = {'csrf_token': token(editing), 'field': 'summary', 'content': NEW}
    if bad == 'missing': del fields['csrf_token']
    if bad == 'wrong': fields['csrf_token'] = 'wrong'
    if bad == 'session': replace_session(editing, settings)
    body = urlencode(fields)
    if bad == 'duplicate': body += '&csrf_token=other'
    assert editing.post(path, content=body, headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 403
    assert store.get_overrides('edit-test') == {} and store.list_hotwords() == []


@pytest.mark.parametrize('field', ['', 'title', 'summary_md', 'SUMMARY'])
def test_invalid_field(editing, field):
    assert editing.post(PATH + '/edit', data={'csrf_token': token(editing), 'field': field, 'content': NEW}).status_code == 400


def test_missing_recording_and_invalid_content(editing):
    data = {'csrf': token(editing), 'field': 'summary', 'content': NEW}
    assert editing.post('/recordings/missing/edit', data=data).status_code == 404
    assert editing.post('/recordings/missing/revert', data=data).status_code == 404
    data['content'] = 'a' * 200001
    assert editing.post(PATH + '/edit', data=data).status_code == 400
    del data['content']
    assert editing.post(PATH + '/edit', data=data).status_code == 400


def test_detail_csrf_coexists_with_other_admin_pages(editing):
    csrf = token(editing)
    for path in ['/hotwords', '/admin', '/assistant']:
        assert editing.get(path).status_code == 200
    assert editing.post(PATH + '/edit', data={'csrf_token': csrf, 'field': 'summary', 'content': NEW}).status_code == 200


def test_diff_preserves_corrections_inside_repetitive_long_transcript():
    old = '甲' * 1000 + '错' + '甲' * 1000
    new = '甲' * 1000 + '对' + '甲' * 1000
    assert diff_suggestions(old, new) == [{'term': '对'}]
