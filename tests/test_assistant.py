"""Assistant administration, using real SQLite transactions and session-bound CSRF."""
import json
import re
import sqlite3
from html import unescape
from urllib.parse import urlencode

import pytest

from app.feishu import FeishuUser
from app.tag_vocabulary import now, refresh_usage
from app.tagging import load_tagging_context
from test_admin import replace_session
from test_login import login
from test_tag_maintenance import add_recording, rows

PATHS = ['/assistant/instructions', '/assistant/tags/merge', '/assistant/tags/deactivate',
         '/assistant/proposals/1/accept', '/assistant/proposals/1/reject']


def csrf(client):
    page = client.get('/assistant')
    assert page.status_code == 200
    return unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1])


def post(client, path, **fields):
    return client.post(path, data={'csrf_token': csrf(client), **fields})


def proposal(store):
    with store._connect() as conn:
        return conn.execute("INSERT INTO tag_merge_proposals (from_tag,to_tag,reason,status,created_at) VALUES ('模考系统','AI Tutor','model','pending',?)", (now(),)).lastrowid


@pytest.mark.parametrize('path', ['/assistant', *PATHS])
@pytest.mark.parametrize('role', ['anonymous', 'member', 'inactive-admin'])
def test_assistant_requires_admin(client, store, fake_auth, settings, path, role):
    if role == 'member':
        fake_auth.user = FeishuUser('ou_member', '管理员', '')
        store.add_user(fake_auth.user)
        login(client)
        assert 'href="/assistant"' not in client.get('/').text
    if role == 'inactive-admin':
        login(client)
        store.remove_user(settings.admin_open_id)
    response = client.get(path) if path == '/assistant' else client.post(path)
    assert response.status_code == 403


def test_admin_page_sections_and_navigation(client, store):
    login(client)
    proposal(store)
    response = client.get('/assistant')
    assert response.status_code == 200
    for title in ['反馈指令', '标签词表', '待确认合并', '最近变更']:
        assert re.search(r'<h2[^>]*>' + title + '</h2>', response.text)
    assert 'rows="8"' in response.text
    assert 'href="/assistant" aria-label="AI 助手配置" data-label="AI 助手配置" aria-current="page"' in response.text
    assert 'id="navigation-toggle"' in response.text
    assert 'aria-expanded="true"' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'Path=/assistant' in response.headers['set-cookie']


@pytest.mark.parametrize('value', ['', '根据业务归类\n保留细分产品', '中' * 2000, '📝' * 2000, '<script>alert(1)</script>'])
def test_save_instructions_roundtrip(client, store, value):
    login(client)
    response = post(client, PATHS[0], instructions=value)
    assert response.status_code == 303
    assert rows(store, 'assistant_config')[0]['instructions'] == value
    page = client.get(response.headers['location'])
    assert re.search(r'已保存 · \d{2}:\d{2}', page.text)
    assert unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>', page.text, re.S)[1]) == value
    assert rows(store, 'tag_change_log')[-1]['action'] == 'instructions'
    if value:
        assert value in load_tagging_context(store)[1]
    assert '<script>alert(1)</script>' not in page.text


def test_over_limit_preserves_database_and_draft(client, store):
    login(client)
    post(client, PATHS[0], instructions='原指令')
    response = post(client, PATHS[0], instructions='字' * 2001)
    assert response.status_code == 400
    assert '反馈指令最多 2000 字' in response.text
    assert '字' * 2001 in response.text
    assert rows(store, 'assistant_config')[0]['instructions'] == '原指令'


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('kind', ['missing', 'tampered', 'other-session', 'no-cookie', 'duplicate', 'json'])
def test_all_mutations_require_existing_csrf(client, store, settings, path, kind):
    login(client)
    token = csrf(client)
    before = {table: rows(store, table) for table in ['tags', 'assistant_config', 'tag_change_log', 'tag_merge_proposals']}
    if kind == 'other-session':
        replace_session(client, settings)
    if kind == 'no-cookie':
        client.cookies.delete('admin_csrf')
    data = {'csrf_token': '' if kind == 'missing' else 'wrong' if kind == 'tampered' else token, 'instructions': 'bad'}
    if kind == 'json':
        response = client.post(path, json=data)
    elif kind == 'duplicate':
        response = client.post(path, content=urlencode(data) + '&csrf_token=' + token,
                               headers={'content-type': 'application/x-www-form-urlencoded'})
    else:
        response = client.post(path, data=data)
    assert response.status_code == 403
    assert before == {table: rows(store, table) for table in before}


def test_merge_rewrites_deduplicates_and_refreshes_usage(client, store):
    login(client)
    add_recording(store, 'source', ['模考系统'])
    add_recording(store, 'target', ['AI Tutor'])
    add_recording(store, 'both', ['模考系统', 'AI Tutor'])
    assert post(client, PATHS[1], **{'from': 2, 'to': 1}).status_code == 303
    tags = rows(store, 'tags')
    assert tags[1]['status'] == 'merged' and tags[1]['merged_into'] == 1
    assert tags[0]['usage_count'] == 3 and tags[1]['usage_count'] == 0
    assert all(store.get_recording(key).tags == ('AI Tutor',) for key in ['source', 'target', 'both'])
    assert rows(store, 'tag_change_log')[-1]['action'] == 'merge'


@pytest.mark.parametrize('source,target', [(1, 1), (999, 1), (1, 999), ('invalid', 1)])
def test_bad_merge_is_rejected_without_writes(client, store, source, target):
    login(client)
    before = rows(store, 'tags')
    assert post(client, PATHS[1], **{'from': source, 'to': target}).status_code == 400
    assert rows(store, 'tags') == before


def test_deactivate_preserves_history_and_excludes_prompt(client, store):
    login(client)
    add_recording(store, 'history', ['AI Tutor'])
    assert post(client, PATHS[2], tag_id=1).status_code == 303
    assert rows(store, 'tags')[0]['status'] == 'deactivated'
    assert store.get_recording('history').tags == ('AI Tutor',)
    vocabulary, prompt = load_tagging_context(store)
    assert 'AI Tutor' not in vocabulary and 'AI Tutor' not in prompt
    assert 'AI Tutor</th>' not in client.get('/assistant').text
    assert post(client, PATHS[1], **{'from': 2, 'to': 1}).status_code == 400
    store.migrate()
    assert rows(store, 'tags')[0]['status'] == 'deactivated'


@pytest.mark.parametrize('action,status', [('accept', 'accepted'), ('reject', 'rejected')])
def test_proposals_change_state_and_disappear(client, store, action, status):
    login(client)
    key = proposal(store)
    add_recording(store, 'history', ['模考系统'])
    assert '待确认合并</h2>' in client.get('/assistant').text
    path = f'/assistant/proposals/{key}/{action}'
    assert post(client, path).status_code == 303
    assert rows(store, 'tag_merge_proposals')[0]['status'] == status
    assert '待确认合并</h2>' not in client.get('/assistant').text
    assert store.get_recording('history').tags == (('AI Tutor',) if action == 'accept' else ('模考系统',))
    assert post(client, path).status_code == 303


def test_stale_proposal_does_not_partially_accept(client, store):
    login(client)
    key = proposal(store)
    post(client, PATHS[2], tag_id=1)
    assert post(client, f'/assistant/proposals/{key}/accept').status_code == 400
    assert rows(store, 'tag_merge_proposals')[0]['status'] == 'pending'
    assert rows(store, 'tags')[1]['status'] == 'active'


def test_empty_vocabulary_and_latest_twenty_changes(client, store):
    login(client)
    with store._connect() as conn:
        conn.execute("UPDATE tags SET status = 'deactivated'")
        conn.execute('DELETE FROM tag_change_log')
        for n in range(25):
            conn.execute('INSERT INTO tag_change_log (at,action,detail) VALUES (?, ?, ?)',
                         ('2026-10-30T06:22:00+00:00', 'add', json.dumps({'to': f'标签-{n:02}'})))
    page = client.get('/assistant').text
    assert '词表为空，下次打标会全部落到「其他」' in page
    assert '待确认合并</h2>' not in page
    assert page.count('10-30 14:22 · 新增') == 20
    assert '标签-04' not in page
    assert page.index('标签-24') < page.index('标签-05')


def test_active_tags_sorted_and_escaped(client, store):
    login(client)
    add_recording(store, 'one', ['模考系统'])
    with store._connect() as conn:
        refresh_usage(conn)
        conn.execute("UPDATE tags SET name = '<script>bad</script>' WHERE id = 3")
    page = client.get('/assistant').text
    assert page.index('<th scope="row">模考系统') < page.index('<th scope="row">AI Tutor')
    assert '<script>bad</script>' not in page
    assert '&lt;script&gt;bad&lt;/script&gt;' in page


def test_08_schema_migration_preserves_ids_merges_aliases_and_checks(store):
    # Build the exact previous CHECK constraints, including a self-reference and
    # both dependant tables. Migration must retain IDs and remain idempotent.
    store.migrate()
    with store._connect() as conn:
        conn.execute("UPDATE tags SET status = 'merged', merged_into = 1 WHERE id = 2")
        conn.execute("INSERT INTO tag_aliases VALUES ('mock', 1)")
        conn.execute("INSERT INTO tag_semantic_checks VALUES (1, '2026-10-30')")
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tags'").fetchone()[0]
    # Fixture-only downgrade creates the previous schema without modifying its data.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute('CREATE TABLE old_tags AS SELECT * FROM tags')
        conn.execute('DROP TABLE tags')
        conn.execute(schema.replace("'active', 'merged', 'deactivated'", "'active', 'merged'").replace("status IN ('active', 'deactivated')", "status = 'active'"))
        conn.execute('INSERT INTO tags SELECT * FROM old_tags')
        conn.execute('DROP TABLE old_tags')
    before = {t: rows(store, t) for t in ['tags', 'tag_aliases', 'tag_semantic_checks', 'assistant_config', 'tag_change_log']}
    store.migrate()
    store.migrate()
    assert before == {t: rows(store, t) for t in before}
    with store._connect() as conn:
        assert not list(conn.execute('PRAGMA foreign_key_check'))
        conn.execute("UPDATE tags SET status = 'deactivated' WHERE id = 3")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tags SET status = 'invented' WHERE id = 4")
