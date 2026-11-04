"""Category migration, model parsing, manual management, and unchanged public labels."""
import json
import sqlite3

import httpx
import pytest

from app import tag
from app.tag_categories import run_backfill
from app.tagging import load_tagging_context
from app.tag_vocabulary import merge_tags, now, resolve
from test_assistant import post
from test_login import login
from test_tag_maintenance import add_recording, rows, run as maintain
from test_tags import response, run


@pytest.fixture(autouse=True)
def migrated(store):
    store.migrate()


def category_map(store):
    return {row['name']: row['category'] for row in rows(store, 'tags')}


def test_legacy_database_migration_is_additive_and_idempotent(store):
    with store._connect() as conn:
        conn.execute('DROP TABLE tag_category_checks')
        conn.execute('DROP TABLE tag_categories')
        conn.execute('ALTER TABLE tags DROP COLUMN category')
        before = [dict(row) for row in conn.execute('SELECT * FROM tags')]
    store.migrate()
    categories = rows(store, 'tag_categories')
    assert [(c['name'], c['position']) for c in categories] == [
        ('产品', 1), ('公司管理', 2), ('中台', 3), ('师资', 4), ('市场', 5), ('技术', 6), ('其他', 99)]
    assert all(c['enabled'] == 1 and c['created_at'] for c in categories)
    after = rows(store, 'tags')
    assert all(row['category'] == '其他' for row in after)
    assert [{k: v for k, v in row.items() if k != 'category'} for row in after] == before
    with store._connect() as conn:
        conn.execute("UPDATE tag_categories SET enabled = 0, name = '产品业务' WHERE id = 1")
        conn.execute("UPDATE tags SET category = '产品业务' WHERE id = 1")
    store.migrate()
    store.migrate()
    assert category_map(store)['AI Tutor'] == '产品业务'
    assert rows(store, 'tag_categories')[0]['enabled'] == 0
    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE tags SET category = NULL WHERE id = 1')


@pytest.mark.parametrize('entry,expected', [({'name': '新产品', 'category': '产品'}, '产品'),
                                           ({'name': '新产品'}, '其他'), ('新产品', '其他')])
def test_tagging_saves_new_tag_and_category_immediately(store, entry, expected):
    add_recording(store, 'new', [])
    result = run(store, lambda _: response([entry]))
    assert result['succeeded'] == 1
    assert category_map(store)['新产品'] == expected
    assert store.get_recording('new').tags == ('新产品',)
    assert rows(store, 'tags')[-1]['usage_count'] == 1
    maintain(store)
    assert category_map(store)['新产品'] == expected


@pytest.mark.parametrize('semantic_ok,expected', [(True, '公司管理'), (False, '其他')])
def test_unknown_category_is_mapped_without_creating_category(store, semantic_ok, expected):
    add_recording(store, 'new', [])
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return response([{'name': '员工激励', 'category': '人力资源'}])
        assert json.loads(payload['messages'][0]['content'])['suggested_category'] == '人力资源'
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': json.dumps(
            {'category': '公司管理' if semantic_ok else '非法新增'})}]})
    result = run(store, handler)
    assert result['succeeded'] == 1 and len(calls) == 2
    assert len(rows(store, 'tag_categories')) == 7
    assert category_map(store)['员工激励'] == expected


def test_unknown_category_lexical_fallback(store):
    add_recording(store, 'new', [])
    def handler(request):
        if 'suggested_category' in json.loads(request.content)['messages'][0]['content']:
            return httpx.Response(503)
        return response([{'name': '投放规划', 'category': '市场营销'}])
    assert run(store, handler)['succeeded'] == 1
    assert category_map(store)['投放规划'] == '市场'


@pytest.mark.parametrize('entry', [None, {'category': '产品'}, {'name': ''},
                                    {'name': 'new', 'category': 1}, {'name': 'new', 'extra': 1}])
def test_invalid_category_entry_retries_safely(store, entry):
    add_recording(store, 'new', [])
    result = run(store, lambda _: response([entry]))
    assert result['failed'] == 1
    assert store.get_recording('new').tags == ()


def test_manual_category_survives_model_responses_backfill_and_restart(client, store):
    login(client)
    assert post(client, '/assistant/tags/category', tag_id=1, category_id=6).status_code == 303
    add_recording(store, 'new', [])
    run(store, lambda _: response([{'name': 'AI Tutor', 'category': '产品'}]))
    settings = tag.TagSettings('fake')
    calls = []
    def handler(request):
        name = json.loads(json.loads(request.content)['messages'][0]['content'])['tag']
        calls.append(name)
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"category":"产品"}'}]})
    result = run_backfill(store, settings=settings, transport=httpx.MockTransport(handler))
    assert result['succeeded'] == 10 and 'AI Tutor' not in calls
    store.migrate()
    assert run_backfill(store, settings=settings, transport=httpx.MockTransport(handler))['attempted'] == 0
    assert category_map(store)['AI Tutor'] == '技术'
    assert store.get_recording('new').tags == ('AI Tutor',)


def test_backfill_failure_is_safe_retryable_and_does_not_hold_sqlite_transaction(store):
    missing = run_backfill(store, settings=tag.TagSettings(''))
    assert missing['deferred'] == 11 and missing['attempted'] == 0
    failed = run_backfill(store, settings=tag.TagSettings('fake'), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert failed['failed'] == 11 and not rows(store, 'tag_category_checks')
    def handler(request):
        # Other writers remain possible while the model is working.
        with store._connect() as conn:
            conn.execute("UPDATE assistant_config SET instructions = 'feedback' WHERE id = 1")
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"category":"产品"}'}]})
    result = run_backfill(store, settings=tag.TagSettings('fake'), transport=httpx.MockTransport(handler))
    assert result['succeeded'] == 11 and result['failed'] == 0
    assert all(value == '产品' for value in category_map(store).values())


def test_grouped_page_immediate_save_and_dynamic_prompt(client, store):
    login(client)
    token = __import__('test_assistant').csrf(client)
    result = client.post('/assistant/tags/category', data={'csrf_token': token, 'tag_id': 1, 'category_id': 1},
                         headers={'accept': 'application/json'})
    assert result.json() == {'saved': True}
    page = client.get('/assistant').text
    product = page.split('id="category-body-1"')[1].split('</section>')[0]
    assert '<th scope="row">AI Tutor</th>' in product
    other = page.split('id="category-body-7"')[1].split('</section>')[0]
    assert '<th scope="row">AI Tutor</th>' not in other
    assert page.count('class="category-toggle secondary" aria-expanded="true"') == 7
    assert '产品: AI Tutor' in load_tagging_context(store)[1]
    assert '"category":"一级分类"' in load_tagging_context(store)[1]


def test_admin_add_rename_toggle_and_tagging_with_disabled_category(client, store):
    login(client)
    assert post(client, '/assistant/categories/add', name='企业服务').status_code == 303
    new_id = rows(store, 'tag_categories')[-1]['id']
    assert post(client, '/assistant/tags/category', tag_id=1, category_id=new_id).status_code == 303
    assert post(client, '/assistant/categories/rename', category_id=new_id, name='业务服务').status_code == 303
    assert post(client, '/assistant/categories/toggle', category_id=new_id).status_code == 303
    assert category_map(store)['AI Tutor'] == '业务服务'
    names, prompt = load_tagging_context(store)
    assert 'AI Tutor' in names and '业务服务: AI Tutor' in prompt
    add_recording(store, 'new', [])
    assert run(store, lambda _: response([{'name': '新业务', 'category': '业务服务'}]))['succeeded'] == 1
    assert category_map(store)['新业务'] == '业务服务'
    assert post(client, '/assistant/categories/toggle', category_id=new_id).status_code == 303
    assert rows(store, 'tag_categories')[-1]['enabled'] == 1


@pytest.mark.parametrize('path,fields', [('/assistant/categories/add', {'name': '产品'}),
    ('/assistant/categories/add', {'name': ' '}), ('/assistant/categories/add', {'name': '中' * 41}),
    ('/assistant/categories/rename', {'category_id': 1, 'name': '其他'}),
    ('/assistant/tags/category', {'tag_id': 1, 'category_id': 999}),
    ('/assistant/tags/category', {'tag_id': 999, 'category_id': 1}),
    ('/assistant/categories/toggle', {'category_id': 999})])
def test_invalid_admin_changes_are_atomic(client, store, path, fields):
    login(client)
    before = {table: rows(store, table) for table in ('tags', 'tag_categories', 'tag_change_log')}
    assert post(client, path, **fields).status_code == 400
    assert before == {table: rows(store, table) for table in before}


def test_renamed_fallback_and_admin_html_escape(client, store):
    login(client)
    assert post(client, '/assistant/categories/rename', category_id=7, name='未分类').status_code == 303
    add_recording(store, 'new', [])
    run(store, lambda _: response([{'name': '新标签'}]))
    assert category_map(store)['新标签'] == '未分类'
    assert post(client, '/assistant/categories/add', name='<script>分类</script>').status_code == 303
    page = client.get('/assistant').text
    assert '<script>分类</script>' not in page and '&lt;script&gt;分类&lt;/script&gt;' in page


def test_merge_chain_and_normalization_keep_target_category(store):
    add_recording(store, 'new', [{'name': 'AI-Tutor', 'category': '技术'}])
    with store._connect() as conn:
        conn.execute("UPDATE tags SET category = '产品' WHERE name = 'AI Tutor'")
    maintain(store)
    assert category_map(store)['AI-Tutor'] == '技术'  # Most-used spelling is the target.
    assert category_map(store)['AI Tutor'] == '技术'
    with store._connect() as conn:
        conn.execute("UPDATE tags SET category = '师资' WHERE name = '模考系统'")
        merge_tags(conn, resolve(conn, 'AI Tutor'), resolve(conn, '模考系统'), reason='manual')
    assert all(category_map(store)[name] == '师资' for name in ['AI Tutor', 'AI-Tutor', '模考系统'])
    assert store.get_recording('new').tags == ('模考系统',)


def test_list_detail_and_search_only_render_second_level_names(client, store):
    login(client)
    assert post(client, '/assistant/categories/add', name='只应出现在配置的分类').status_code == 303
    cid = rows(store, 'tag_categories')[-1]['id']
    assert post(client, '/assistant/tags/category', tag_id=1, category_id=cid).status_code == 303
    add_recording(store, 'new', ['AI Tutor'])
    for path in ['/', '/recordings/new', '/?q=会议']:
        html = client.get(path).text
        assert '只应出现在配置的分类' not in html
        if path != '/recordings/new':
            assert 'AI Tutor' in html
