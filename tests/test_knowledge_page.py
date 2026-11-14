import json
import re
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest

from app import kb, knowledge_page
from app.feishu import FeishuUser
from app.session import SESSION_MAX_AGE, SignedCookie
from test_login import login


@pytest.fixture
def knowledge_files():
    root = kb.configured_root()
    (root / 'tags').mkdir(parents=True)
    tags = [dict(name=name, category=category, path=f'tags/{i}.md', sessions=sessions,
                 minutes=minutes, notes=notes, last_session_at=last)
            for i, (name, category, sessions, minutes, notes, last) in enumerate([
                ('AI Tutor', '产品', 17, 607, 2, '2026-11-13T15:30:00+08:00'),
                ('导师 中心', '产品', 5, 210, 0, '2026-11-12T18:30:00Z'),
                ('状态机', '中台', 0, 0, 1, None)])]
    index = dict(generated_at='2026-11-14T08:50:00Z', source={'recordings': 18},
                 categories=[{'name': name} for name in ['中台', '空分类', '产品']], tags=tags)
    (root / 'index.json').write_text(json.dumps(index, ensure_ascii=False))
    for tag in tags:
        (root / tag['path']).write_text('# ' + tag['name'] + '\n\n## 当前状态\n\n正在推进。\n\n'
                                       '## 会议\n\n[试点会议](/recordings/meeting-1)\n')
    return root, index


@pytest.fixture
def knowledge_client(client, knowledge_files):
    assert login(client).status_code == 302
    return client


def test_group_order_stats_links_and_navigation(knowledge_client, knowledge_files, monkeypatch):
    # Authentication still reads users; the knowledge presentation must not query business data.
    monkeypatch.setattr(kb, 'scan', lambda *a: pytest.fail('GET queried knowledge database'))
    response = knowledge_client.get('/knowledge')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    html = response.text
    assert '3 个标签 · 2 个分类 · 18 场会议 · 更新于 2026-11-14 16:50' in html
    assert [g['name'] for g in response.context['index']['groups']] == ['中台', '产品']
    assert '空分类' not in html
    assert '17 场 · 607 分钟 · 2 条补充' in html
    assert '5 场 · 210 分钟 · 0 条补充' in html
    assert '0 场 · 0 分钟 · 1 条补充' in html
    assert html.count('>11-13</time>') == 2
    assert 'href="/knowledge/AI%20Tutor"' in html
    assert 'href="/knowledge/' + quote('导师 中心', safe='') + '"' in html
    assert re.findall(r'class="navigation-link" href="([^"]+)"', html)[:4] == ['/', '/board', '/knowledge', '/hotwords']
    assert 'href="/knowledge" aria-label="知识库" data-label="知识库" aria-current="page"' in html
    assert html.count('aria-current="page"') == 1
    assert html.count('class="secondary knowledge-copy"') == 4
    for command in knowledge_page.COMMANDS:
        from html import unescape
        assert command in unescape(html)


@pytest.mark.parametrize('root, cli', [
    ('/var/lib/qwennote/knowledge', '/opt/qwennote/current/scripts/kb.sh'),
    (None, 'python -m app.kb'),
    ('', 'python -m app.kb'),
])
def test_agent_prompt_and_commands_use_runtime_cli(knowledge_client, knowledge_files, monkeypatch, root, cli):
    from html import unescape

    # Keep fixture files isolated while testing the deployment environment branch.
    monkeypatch.setattr(kb, 'configured_root', lambda: knowledge_files[0])
    if root is None:
        monkeypatch.delenv('KB_ROOT', raising=False)
    else:
        monkeypatch.setenv('KB_ROOT', root)
    response = knowledge_client.get('/knowledge')
    assert response.status_code == 200
    assert response.context['kb_cli'] == cli
    prompt = response.context['kb_prompt']
    assert prompt == knowledge_page.build_agent_prompt(cli)
    assert prompt.splitlines()[-1] == '<在这里写你的具体任务>'
    assert ('在项目根目录执行' in prompt) == (cli == 'python -m app.kb')
    for command in ('list --json', 'show "<标签>" --json', 'search <关键词> --limit 5 --json',
                    'note add "<标签>" --text "<你的结论>" --source codex'):
        assert f'执行：{cli} {command}' in prompt
    assert '- 写完立刻 show 读回，确认记录真的进了文件' in prompt
    assert '- 知识库是派生产物，不要直接编辑 knowledge/ 下的文件，写内容只能走 note add' in prompt
    html = response.text
    assert 'data-copy="knowledge-agent-prompt" data-copy-label="提示词">复制提示词</button>' in html
    rendered = re.search(r'<pre id="knowledge-agent-prompt"[^>]*>(.*?)</pre>', html, re.S)[1]
    assert unescape(rendered) == prompt
    assert '&lt;在这里写你的具体任务&gt;' in rendered
    assert '<details class="knowledge-commands">' in html
    assert '<summary>只用命令的话</summary>' in html
    assert len(response.context['commands']) == 4
    assert all(command.startswith(cli + ' ') for command in response.context['commands'])


@pytest.mark.parametrize('bad', [None, '{broken', '[]', '{}', 'null', '{"tags": []}', '\xff',
                                '{"generated_at": 5}', '{"generated_at": "not-a-date"}'])
def test_missing_or_corrupt_index_empty_state(client, bad):
    assert login(client).status_code == 302
    if bad is not None:
        kb.configured_root().mkdir()
        (kb.configured_root() / 'index.json').write_bytes(bad.encode('latin1'))
    response = client.get('/knowledge')
    assert response.status_code == 200
    assert '知识库尚未生成' in response.text
    assert 'python -m app.kb build --full' in response.text
    assert 'scripts/kb.sh build --full' in response.text
    assert '给 Agent 接入' in response.text


@pytest.mark.parametrize('mutation', ['tags', 'categories', 'count', 'name', 'category', 'date', 'path'])
def test_corrupt_index_schema_is_empty(knowledge_client, knowledge_files, mutation):
    root, index = knowledge_files
    if mutation in ('tags', 'categories'):
        index[mutation] = [None]
    else:
        key, value = {'count': ('sessions', '17'), 'name': ('name', []), 'category': ('category', 'unknown'),
                      'date': ('last_session_at', 'invalid'), 'path': ('path', None)}[mutation]
        index['tags'][0][key] = value
    (root / 'index.json').write_text(json.dumps(index))
    assert '知识库尚未生成' in knowledge_client.get('/knowledge').text


@pytest.mark.parametrize('tag', ['AI Tutor', '导师 中心'])
def test_detail_markdown_links_encoded_name(knowledge_client, tag):
    response = knowledge_client.get('/knowledge/' + quote(tag, safe=''))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert '<h2 class="summary-h2">当前状态</h2>' in response.text and '<h2 class="summary-h2">会议</h2>' in response.text
    assert 'href="/recordings/meeting-1"' in response.text
    assert 'href="/knowledge">← 知识库' in response.text
    assert '更新于 2026-11-14 16:50' in response.text
    assert 'href="/knowledge" aria-label="知识库" data-label="知识库" aria-current="page"' in response.text
    assert response.text.count('aria-current="page"') == 1


@pytest.mark.parametrize('mode', ['unknown', 'missing', 'escape', 'symlink', 'invalid-utf8'])
def test_missing_and_unsafe_files_are_404(knowledge_client, knowledge_files, mode):
    root, index = knowledge_files
    tag = index['tags'][0]
    target = root / tag['path']
    if mode == 'unknown':
        tag['name'] = '不存在'
    elif mode == 'missing':
        target.unlink()
    elif mode == 'escape':
        index['tags'][0]['path'] = '../private.md'
        (root / 'index.json').write_text(json.dumps(index))
    elif mode == 'symlink':
        target.unlink()
        outside = root.parent / 'private.md'
        outside.write_text('secret content')
        target.symlink_to(outside)
    else:
        target.write_bytes(b'\xff')
    response = knowledge_client.get('/knowledge/' + quote(tag['name'], safe=''))
    assert response.status_code == 404
    assert '标签不存在' in response.text and 'secret content' not in response.text


@pytest.mark.parametrize('mode', ['anonymous', 'outside', 'inactive'])
def test_access_denied(client, store, settings, mode):
    if mode != 'anonymous':
        subject = uuid4().hex
        if mode == 'inactive':
            with store._connect() as conn:
                conn.execute('INSERT INTO users (subject,name,avatar_url,is_active,added_at) VALUES (?,?,?,0,?)',
                             (subject, '停用', '', 'now'))
        client.cookies.set('session', SignedCookie(settings.session_secret, salt='web-session',
                                                  max_age=SESSION_MAX_AGE).encode(subject))
    for url in ['/knowledge', '/knowledge/AI%20Tutor']:
        assert client.get(url).status_code == 403
    assert client.post('/knowledge/rebuild').status_code == 403


def test_regular_whitelist_read_only(client, fake_auth, store, knowledge_files):
    subject = uuid4().hex
    with store._connect() as conn:
        conn.execute('INSERT INTO users (subject,name,avatar_url,is_active,added_at) VALUES (?,?,?,1,?)',
                     (subject, '白名单', '', 'now'))
    fake_auth.user = FeishuUser(subject, '白名单', '')
    assert login(client).status_code == 302
    response = client.get('/knowledge')
    assert response.status_code == 200 and '重新生成' not in response.text
    assert 'href="/knowledge" aria-label="知识库"' in response.text
    assert client.get('/knowledge/AI%20Tutor').status_code == 200
    assert client.post('/knowledge/rebuild').status_code == 403
    (knowledge_files[0] / 'index.json').unlink()
    html = client.get('/knowledge').text
    assert '知识库尚未生成' in html and 'python -m app.kb build --full' not in html
    assert 'scripts/kb.sh build --full' not in html


def test_empty_generated_index_offers_admin_build_hint(knowledge_client, knowledge_files):
    root, index = knowledge_files
    index['tags'] = []
    (root / 'index.json').write_text(json.dumps(index))
    html = knowledge_client.get('/knowledge').text
    assert '知识库暂无内容' in html
    assert 'scripts/kb.sh build --full' in html


def test_admin_rebuild(knowledge_client, store, knowledge_files, monkeypatch):
    calls = []
    monkeypatch.setattr(kb, 'build', lambda *args: calls.append(args) or {'errors': []})
    page = knowledge_client.get('/knowledge')
    assert '重新生成' in page.text
    response = knowledge_client.post('/knowledge/rebuild', data={'csrf_token': page.context['csrf_token']})
    assert response.status_code == 302 and response.headers['location'] == '/knowledge'
    assert calls == [(store, knowledge_files[0], None)]


@pytest.mark.parametrize('mode', ['missing', 'wrong', 'duplicate', 'new-session'])
def test_rebuild_csrf(knowledge_client, monkeypatch, mode):
    monkeypatch.setattr(kb, 'build', lambda *a: pytest.fail('CSRF allowed build'))
    token = knowledge_client.get('/knowledge').context['csrf_token']
    fields = {'csrf_token': token}
    if mode == 'missing': fields = {}
    if mode == 'wrong': fields['csrf_token'] = 'wrong'
    if mode == 'duplicate': fields['csrf_token'] = [token, token]
    if mode == 'new-session': monkeypatch.setattr('app.admin.hashlib.sha256', lambda *a: type('Digest', (), {'hexdigest': lambda self: 'new-session'})())
    assert knowledge_client.post('/knowledge/rebuild', data=fields).status_code == 403


@pytest.mark.parametrize('mode', ['exception', 'partial'])
def test_rebuild_failure_is_visible_without_leaking(knowledge_client, monkeypatch, mode):
    def fail(*args):
        if mode == 'exception': raise RuntimeError('private token value')
        return {'errors': [{'error': 'private token value'}]}
    monkeypatch.setattr(kb, 'build', fail)
    token = knowledge_client.get('/knowledge').context['csrf_token']
    response = knowledge_client.post('/knowledge/rebuild', data={'csrf_token': token})
    assert response.status_code == 200 and 'role="alert"' in response.text
    assert '生成失败' in response.text and 'private token value' not in response.text


def test_escaped_markdown_and_tag(knowledge_client, knowledge_files):
    root, index = knowledge_files
    index['tags'][0]['name'] = '<script>alert(1)</script>'
    (root / 'index.json').write_text(json.dumps(index))
    (root / 'tags/0.md').write_text('<script>alert(2)</script>\n\n[unsafe](javascript:alert(3))')
    url = '/knowledge/' + quote(index['tags'][0]['name'], safe='')
    for html in (knowledge_client.get('/knowledge').text, knowledge_client.get(url).text):
        assert '<script>alert' not in html and 'href="javascript:' not in html
        assert '&lt;script&gt;' in html


def test_wrapper_executable():
    import os
    import subprocess
    script = Path(__file__).resolve().parents[1] / 'scripts/kb.sh'
    assert script.stat().st_mode & 0o777 == 0o755
    assert os.access(script, os.X_OK)
    assert subprocess.run(['bash', '-n', str(script)]).returncode == 0


def test_real_build_round_trip_and_category_position(client, store):
    from test_kb import add_meeting, add_tag
    with store._connect() as conn:
        conn.execute('DELETE FROM tags')
        conn.execute("UPDATE tag_categories SET position=0 WHERE name='技术'")
        conn.execute("UPDATE tag_categories SET position=99 WHERE name='产品'")
    add_tag(store, 'AI Tutor', '产品')
    add_tag(store, '状态机', '技术')
    add_meeting(store, 'roundtrip', tags=('AI Tutor', '状态机'), minutes=42)
    kb.add_note(store, 'AI Tutor', '补充进展', 'codex')
    assert login(client).status_code == 302
    token = client.get('/knowledge').context['csrf_token']
    response = client.post('/knowledge/rebuild', data={'csrf_token': token})
    assert response.status_code == 302
    response = client.get('/knowledge')
    assert '2 个标签 · 2 个分类 · 1 场会议' in response.text
    assert [g['name'] for g in response.context['index']['groups']] == ['技术', '产品']
    assert '1 场 · 42 分钟 · 1 条补充' in response.text
    detail = client.get('/knowledge/AI%20Tutor')
    assert detail.status_code == 200 and '补充进展' in detail.text
    assert 'href="/recordings/roundtrip"' in detail.text


def test_get_reads_only_user_database_for_auth(knowledge_client, store, monkeypatch):
    original = store._connect
    statements = []
    from contextlib import contextmanager
    @contextmanager
    def tracked():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, '_connect', tracked)
    assert knowledge_client.get('/knowledge').status_code == 200
    assert knowledge_client.get('/knowledge/AI%20Tutor').status_code == 200
    reads = [sql.lower() for sql in statements if sql.lower().startswith('select')]
    assert reads and all('from users' in sql for sql in reads)
