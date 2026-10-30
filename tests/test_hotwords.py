"""Offline hotword rules, administrator boundary, and all presentation exits."""
import hashlib
import re
import secrets
import sqlite3
from datetime import datetime
from html import unescape
from urllib.parse import urlencode

import pytest
from markdown_it import MarkdownIt

from app.feishu import FeishuUser
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from test_admin import replace_session
from test_login import login


MUTATIONS = ['/hotwords', '/hotwords/1/update', '/hotwords/1/delete', '/hotwords/1/toggle']
BODY = 'AI Tutor 和 AI，ai tutor'
EXPECTED = 'AI导师 和 人工智能，AI导师'


def csrf(client):
    page = client.get('/hotwords')
    assert page.status_code == 200
    return unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1])


def post(client, path='/hotwords', **fields):
    return client.post(path, data={'csrf_token': csrf(client), **fields})


def recording_rows(store):
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute('SELECT * FROM recordings')]


@pytest.fixture
def populated(client, store):
    store.upsert_recording(
        RecordingMeta('hotword', 'AI 例会', datetime(2026, 10, 29, 9, 30, tzinfo=SHANGHAI), 12),
        RecordingDetail('hotword', BODY, BODY, True, True), '2026-10-29T12:00:00+08:00')
    # The longer rule deliberately comes second.
    store.add_hotword('AI', '人工智能')
    store.add_hotword('AI Tutor', 'AI导师')
    key = 'qn_' + secrets.token_hex(16)
    store.create_api_key('offline-hotwords', hashlib.sha256(key.encode()).hexdigest())
    client.headers['Authorization'] = 'Bearer ' + key
    assert login(client).status_code == 302
    return client


@pytest.mark.parametrize('text,expected', [
    ('AI Tutor 和 AI', 'AI导师 和 人工智能'),
    ('ai tutor / Ai TuToR / ai / aI', 'AI导师 / AI导师 / 人工智能 / 人工智能'),
    ('中文 AI Tutor 中文', '中文 AI导师 中文'),
    ('', ''), ('没有命中', '没有命中'),
])
def test_longest_first_and_case(store, text, expected):
    store.migrate()
    store.add_hotword('AI', '人工智能')
    store.add_hotword('AI Tutor', 'AI导师')
    assert store.apply_hotwords(text) == expected


def test_one_pass_does_not_rescan_targets_or_cycle(store):
    store.migrate()
    for source, target in [('A', 'B'), ('B', 'C'), ('C', 'A')]:
        store.add_hotword(source, target)
    assert store.apply_hotwords('A B C A') == 'B C A B'
    assert {row.source: row.hits for row in store.list_hotwords()} == {'A': 2, 'B': 1, 'C': 1}


def test_chinese_exact_and_regex_metacharacters_are_literal(store):
    store.migrate()
    store.add_hotword('千问', '通义千问')
    store.add_hotword('C++', 'Cpp')
    store.add_hotword('a.b', 'dot')
    assert store.apply_hotwords('千问 C++ a.b axb') == '通义千问 Cpp dot axb'


def test_disabled_rules_do_not_match_or_count(store):
    store.migrate()
    store.add_hotword('AI', '人工智能')
    key = store.list_hotwords()[0].id
    assert store.toggle_hotword(key)
    assert store.apply_hotwords('AI ai') == 'AI ai'
    assert store.list_hotwords()[0].hits == 0
    assert store.toggle_hotword(key)
    assert store.apply_hotwords('AI ai') == '人工智能 人工智能'
    assert store.list_hotwords()[0].hits == 2


@pytest.mark.parametrize('text,expected,hits', [
    ('# AI\n\n- **AI**\n\n> AI\n', '# 正确\n\n- **正确**\n\n> 正确\n', 3),
    ('[AI](https://AI/path(foo))', '[正确](https://AI/path(foo))', 1),
    ('![AI](https://AI/img "AI")', '![正确](https://AI/img "AI")', 1),
    ('[AI][ref]\n\n[ref]: https://AI "AI"\n', '[正确][ref]\n\n[ref]: https://AI "AI"\n', 1),
    ('`AI` ``AI ` AI`` AI', '`AI` ``AI ` AI`` 正确', 1),
    ('```text\nAI\n```\nAI\n', '```text\nAI\n```\n正确\n', 1),
    ('~~~~\nAI\n~~~\nAI\n~~~~\nAI', '~~~~\nAI\n~~~\nAI\n~~~~\n正确', 1),
    ('    AI\n\tAI\nAI', '    AI\n\tAI\n正确', 1),
    ('<https://AI> AI', '<https://AI> 正确', 1),
    ('> - **AI**\n\nAI\n---\n', '> - **正确**\n\n正确\n---\n', 2),
    ('| AI | AI |\n|---|---|\n| AI | AI |', '| 正确 | 正确 |\n|---|---|\n| 正确 | 正确 |', 4),
])
def test_markdown_syntax_code_and_link_destinations_are_protected(store, text, expected, hits):
    store.migrate()
    store.add_hotword('AI', '正确')
    assert store.apply_hotwords(text) == expected
    assert store.list_hotwords()[0].hits == hits


@pytest.mark.parametrize('target', ['**加粗**', '# 标题', '[点我](https://example.test)',
                                  '<https://example.test>', '&copy;', '`代码`', 'A|B', r'反\斜杠'])
def test_target_is_plain_text_and_does_not_create_markdown(store, target):
    store.migrate()
    store.add_hotword('AI', target)
    markdown = store.apply_hotwords('AI')
    parser = MarkdownIt('commonmark', {'html': False})
    children = parser.parse(markdown)[1].children
    assert [(token.type, token.content) for token in children] == [('text', target)]
    assert store.apply_hotwords('AI', markdown=False) == target


@pytest.mark.parametrize('path,fields', [
    ('/recordings/hotword?view=summary', 1),
    ('/recordings/hotword?view=transcript', 1),
    ('/recordings/hotword.md', 2),
    ('/api/v1/recordings/hotword', 2),
    ('/api/v1/daily/2026-10-29?format=md', 2),
])
def test_five_exits_share_the_function_and_count_exact_hits(populated, store, monkeypatch, path, fields):
    before = recording_rows(store)
    calls = []
    apply = store.apply_hotwords

    def spy(text, **kwargs):
        calls.append(text)
        return apply(text, **kwargs)

    monkeypatch.setattr(store, 'apply_hotwords', spy)
    response = populated.get(path)
    assert response.status_code == 200
    assert calls == [BODY] * fields
    if path == '/api/v1/recordings/hotword':
        assert response.json()['summary_md'] == response.json()['transcript_md'] == EXPECTED
    else:
        assert response.text.count(EXPECTED) == fields
    assert BODY not in response.text
    assert {row.source: row.hits for row in store.list_hotwords()} == {'AI Tutor': fields * 2, 'AI': fields}
    assert recording_rows(store) == before


def test_counts_accumulate_only_requested_bodies_and_can_be_revoked(populated, store):
    before = recording_rows(store)
    for path in ['/recordings/hotword?view=summary', '/recordings/hotword?view=transcript',
                 '/recordings/hotword.md', '/api/v1/recordings/hotword',
                 '/api/v1/daily/2026-10-29?format=md']:
        assert populated.get(path).status_code == 200
    assert {row.source: row.hits for row in store.list_hotwords()} == {'AI Tutor': 16, 'AI': 8}
    for path in ['/', '/hotwords', '/api/v1/recordings', '/api/v1/daily/2026-10-29?format=json']:
        assert populated.get(path).status_code == 200
    assert {row.source: row.hits for row in store.list_hotwords()} == {'AI Tutor': 16, 'AI': 8}
    for word in store.list_hotwords():
        store.toggle_hotword(word.id)
    result = populated.get('/api/v1/recordings/hotword').json()
    assert result['summary_md'] == result['transcript_md'] == BODY
    assert recording_rows(store) == before


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_special_target_displays_literally_in_both_views(populated, store, view):
    for word in store.list_hotwords():
        store.delete_hotword(word.id)
    store.add_hotword('AI Tutor', '**词** [名称] <标签> &copy;')
    result = populated.get('/recordings/hotword?view=' + view)
    assert '**词** [名称] &lt;标签&gt; &amp;copy;' in result.text
    assert '<strong>词</strong>' not in result.text
    assert r'\*' not in result.text


@pytest.mark.parametrize('role', ['anonymous', 'member', 'inactive-admin'])
@pytest.mark.parametrize('method,path', [('get', '/hotwords'), *[('post', path) for path in MUTATIONS]])
def test_management_requires_active_admin(client, store, fake_auth, settings, method, path, role):
    store.add_hotword('AI', '正确')
    if role == 'member':
        fake_auth.user = FeishuUser('ou_hotword_member', '管理员', '')
        store.add_user(fake_auth.user)
        login(client)
        assert 'href="/hotwords"' not in client.get('/').text
    elif role == 'inactive-admin':
        login(client)
        store.remove_user(settings.admin_open_id)
    before = store.list_hotwords()
    result = getattr(client, method)(path)
    assert result.status_code == 403
    assert store.list_hotwords() == before


@pytest.mark.parametrize('path', MUTATIONS)
@pytest.mark.parametrize('kind', ['missing', 'tampered', 'other-session', 'no-cookie', 'duplicate', 'json'])
def test_mutations_require_csrf(client, store, settings, path, kind):
    login(client)
    store.add_hotword('AI', '正确')
    token = csrf(client)
    before = store.list_hotwords()
    if kind == 'other-session':
        replace_session(client, settings)
    elif kind == 'no-cookie':
        client.cookies.delete('admin_csrf')
    data = {'csrf_token': '' if kind == 'missing' else 'wrong' if kind == 'tampered' else token,
            'source': '改了', 'target': '变了'}
    if kind == 'json':
        result = client.post(path, json=data)
    elif kind == 'duplicate':
        result = client.post(path, content=urlencode(data) + '&csrf_token=' + token,
                             headers={'content-type': 'application/x-www-form-urlencoded'})
    else:
        result = client.post(path, data=data)
    assert result.status_code == 403
    assert store.list_hotwords() == before


def test_administration_crud_navigation_and_cookie(client, store):
    login(client)
    page = client.get('/hotwords')
    assert page.status_code == 200 and '暂无热词' in page.text
    assert 'href="/hotwords" aria-label="热词中心" data-label="热词中心" aria-current="page"' in page.text
    assert page.headers['cache-control'] == 'no-store'
    assert 'Path=/hotwords' in page.headers['set-cookie'] and 'HttpOnly' in page.headers['set-cookie']
    assert post(client, source=' AI ', target=' 正确 ', note='备注').status_code == 303
    word, = store.list_hotwords()
    assert (word.source, word.target, word.note, word.enabled, word.hits) == ('AI', '正确', '备注', True, 0)
    assert word.created_at and word.updated_at
    assert store.apply_hotwords('AI AI') == '正确 正确'
    assert post(client, f'/hotwords/{word.id}/update', source='Tutor', target='导师', note='新备注').status_code == 303
    updated, = store.list_hotwords()
    assert (updated.source, updated.target, updated.note, updated.hits) == ('Tutor', '导师', '新备注', 2)
    assert updated.created_at == word.created_at and updated.updated_at >= word.updated_at
    assert post(client, f'/hotwords/{word.id}/toggle').status_code == 303
    assert not store.list_hotwords()[0].enabled
    assert '停用</span>' in client.get('/hotwords').text
    assert post(client, f'/hotwords/{word.id}/delete').status_code == 303
    assert store.list_hotwords() == []


@pytest.mark.parametrize('fields', [
    {'source': '', 'target': '好'}, {'source': '坏', 'target': ' '},
    {'source': 'x' * 201, 'target': '好'}, {'source': '坏', 'target': 'x' * 501},
    {'source': '坏', 'target': '好', 'note': 'x' * 1001},
    {'source': '坏\n词', 'target': '好'}, {'source': '坏', 'target': '# 标题\n新增段落'},
])
@pytest.mark.parametrize('path', ['/hotwords', '/hotwords/1/update'])
def test_invalid_fields_never_write(client, store, path, fields):
    login(client)
    store.add_hotword('AI', '正确')
    before = store.list_hotwords()
    assert post(client, path, **fields).status_code == 400
    assert store.list_hotwords() == before


@pytest.mark.parametrize('path', ['/hotwords', '/hotwords/2/update'])
def test_duplicate_source_never_partially_updates(client, store, path):
    login(client)
    store.add_hotword('AI', '正确')
    store.add_hotword('Tutor', '导师')
    before = store.list_hotwords()
    assert post(client, path, source='AI', target='改了', note='不该保存').status_code == 409
    assert store.list_hotwords() == before


@pytest.mark.parametrize('action', ['update', 'delete', 'toggle'])
def test_missing_hotword_is_404(client, action):
    login(client)
    assert post(client, f'/hotwords/999/{action}', source='AI', target='正确').status_code == 404


def test_management_escapes_untrusted_fields(client, store):
    login(client)
    payload = '<script>alert(1)</script>'
    assert post(client, source=payload, target=payload, note=payload).status_code == 303
    page = client.get('/hotwords')
    assert payload not in page.text and '&lt;script&gt;' in page.text


def test_migration_is_idempotent_preserves_rules_and_override_constraint(store):
    store.migrate()
    store.add_hotword('AI', '正确')
    store.apply_hotwords('AI')
    before = store.list_hotwords()
    with store._connect() as conn:
        conn.execute("INSERT INTO recording_overrides VALUES ('future', 'summary', '# 未来', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO recording_overrides VALUES ('future', 'title', '不支持', 'now')")
    store.migrate()
    assert store.list_hotwords() == before
    with store._connect() as conn:
        assert tuple(conn.execute('SELECT * FROM recording_overrides').fetchone()) == ('future', 'summary', '# 未来', 'now')
