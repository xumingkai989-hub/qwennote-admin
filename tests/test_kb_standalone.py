"""T11 public CLI contracts and side-effect boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from app import kb, kb_cli, knowledge_page
from test_kb import db, add_tag, add_meeting


@pytest.fixture
def run_cli(db, tmp_path, monkeypatch, capsys):
    root = tmp_path / 'knowledge'
    monkeypatch.setenv('KB_ROOT', str(root))
    monkeypatch.setenv('DB_PATH', str(db.db_path))
    def call(*args):
        code = kb_cli.main(list(args))
        output = capsys.readouterr()
        return code, output.out, output.err
    call.root = root
    return call


def seed(db, root):
    add_tag(db, '测试主题', '产品')
    add_tag(db, '其他主题', '技术')
    add_meeting(db, 'm1', ['测试主题'])
    kb.build(db, root)


def test_envelope_sort_filter_and_global_positions(db, run_cli):
    seed(db, run_cli.root)
    code, out, err = run_cli('--format', 'json', 'list', '--root', str(run_cli.root), '--category', '产品')
    data = json.loads(out)
    assert code == 0 and not err and data['ok'] is True
    assert data['data'][0]['name'] == '测试主题'
    assert data['meta']['count'] == 1 and data['meta']['root'] == str(run_cli.root)
    assert data['meta']['took_ms'] >= 0
    path = run_cli.root / 'index.json'
    index = json.loads(path.read_text())
    for tag in index['tags']:
        tag['updated_at'] = '2099-01-01' if tag['name'] == '其他主题' else '2000-01-01'
    path.write_text(json.dumps(index))
    assert json.loads(run_cli('list')[1])['data'][0]['name'] == '测试主题'
    assert json.loads(run_cli('list', '--sort', 'updated')[1])['data'][0]['name'] == '其他主题'


def test_show_preserves_entire_markdown(db, run_cli):
    seed(db, run_cli.root)
    content = (run_cli.root / 'tags/测试主题.md').read_text()
    assert run_cli('show', '测试主题') == (0, content, '')
    for option in (['--format', 'json'], ['--json']):
        code, out, err = run_cli('show', '测试主题', *option)
        assert code == 0 and not err and json.loads(out)['data']['content'] == content


@pytest.mark.parametrize('args', [('show', '不存在'), ('note', 'remove', '987654')])
def test_not_found_structured_and_human(run_cli, args):
    code, out, err = run_cli(*args)
    assert code == 1 and err and json.loads(out)['error']['type'] == 'not_found'
    code, out, err = run_cli(*args, '--format', 'md')
    assert code == 1 and not out and len(err.splitlines()) == 1


@pytest.mark.parametrize('args', [('list',), ('search', '不存在'), ('note', 'list', '不存在')])
def test_empty_collections(run_cli, args):
    code, out, err = run_cli(*args)
    payload = json.loads(out)
    assert code == 1 and not err and payload['ok'] and payload['data'] == [] and payload['meta']['count'] == 0


@pytest.mark.parametrize('args', [
    ('note', 'add', '新主题', '--text', '待写入', '--source', 'codex'),
    ('note', 'remove', '123'), ('build', '--full'), ('build', '--tag', '主题'),
])
def test_dry_run_never_opens_database_or_writes_files(db, run_cli, monkeypatch, args):
    before = db.db_path.read_bytes()
    store, add, remove, build = (Mock(side_effect=AssertionError('side effect')) for _ in range(4))
    monkeypatch.setattr(kb_cli, '_store', store)
    monkeypatch.setattr(kb, 'add_note', add)
    monkeypatch.setattr(kb, 'remove_note', remove)
    monkeypatch.setattr(kb, 'build', build)
    code, out, err = run_cli(*args, '--dry-run')
    assert code == 0 and not err and json.loads(out)['data']['dry_run'] is True
    assert not run_cli.root.exists() and db.db_path.read_bytes() == before
    for mock in (store, add, remove, build):
        mock.assert_not_called()


@pytest.mark.parametrize('expression,expected', [
    ('.data[0].name', '测试主题'), ('.data | length', '1'),
    ('.data[].name', '测试主题'), ('.data[-1]["name"]', '测试主题'),
    ('.data[10].name', 'null'), ('.meta | keys', '["count", "root", "took_ms"]'),
])
def test_jq_filters(db, run_cli, expression, expected):
    seed(db, run_cli.root)
    assert run_cli('list', '--category', '产品', '--jq', expression) == (0, expected + '\n', '')


@pytest.mark.parametrize('expression', ['', 'import os', '.data;system("id")', '.data | map(.name)', '.data[bad]'])
def test_invalid_jq_rejected_before_write(run_cli, monkeypatch, expression):
    store = Mock(side_effect=AssertionError('must not open DB'))
    monkeypatch.setattr(kb_cli, '_store', store)
    code, out, err = run_cli('note', 'add', '主题', '--text', '结论', '--jq', expression)
    assert code == 2 and err and json.loads(out)['error']['type'] == 'invalid_argument'
    store.assert_not_called()


def test_jq_type_error_and_format_conflict(run_cli):
    assert run_cli('list', '--jq', '.data.name')[0] == 2
    assert run_cli('list', '--jq', '.data', '--format', 'table')[0] == 2


@pytest.mark.parametrize('args', [(), ('unknown',), ('search', 'x', '--limit', '0'),
    ('search', 'x', '--limit', 'bad'), ('search', ' '), ('build', '--tag', 'x', '--full'),
    ('note', 'add', 'A'), ('note', 'add', 'A', '--text', ''),
    ('note', 'add', '', '--text', 'x'), ('note', 'add', 'A', '--text', 'x' * 2001),
    ('note', 'add', 'A', '--text', 'x', '--source', 'bad'), ('note', 'remove', '-1'),
    ('schema', 'unknown'), ('list', '--dry-run')])
def test_invalid_arguments(run_cli, args):
    code, out, err = run_cli(*args)
    assert code == 2 and err and json.loads(out)['error']['type'] == 'invalid_argument'


def test_prompt_uses_page_function_and_task(run_cli):
    assert run_cli('prompt') == (0, knowledge_page.build_agent_prompt(), '')
    task = '总结最近一次会议并标明证据'
    assert run_cli('prompt', '--task', task)[1].splitlines()[-1] == task
    payload = json.loads(run_cli('prompt', '--format', 'json', '--task', task)[1])
    assert payload['data']['content'].splitlines()[-1] == task


def test_schema_and_nested_global_flags(run_cli):
    payload = json.loads(run_cli('schema', 'list')[1])['data']
    assert payload['command'] == 'list' and payload['risk'] == 'read'
    sort = next(p for p in payload['parameters'] if p['name'] == 'sort')
    assert sort['default'] == 'sessions' and sort['choices'] == ['sessions', 'updated']
    add = json.loads(run_cli('schema', 'note', 'add')[1])['data']
    assert next(p for p in add['parameters'] if p['name'] == 'text')['required']
    code, out, err = run_cli('--format', 'json', 'note', '--root', str(run_cli.root), 'list', 'A')
    assert code == 1 and not err and json.loads(out)['meta']['root'] == str(run_cli.root)


@pytest.mark.parametrize('command', list(kb_cli.DESCRIPTIONS))
def test_layered_help(run_cli, command):
    code, out, err = run_cli(*command.split(), '--help')
    assert code == 0 and not err
    assert all(section in out for section in ('Risk:', 'When to use:', 'Examples:', 'Usage:', 'Global flags:'))


@pytest.mark.parametrize('fmt', ['md', 'table'])
def test_human_output(db, run_cli, fmt):
    seed(db, run_cli.root)
    code, out, err = run_cli('list', '--format', fmt)
    assert code == 0 and not err and '测试主题' in out and 'sessions' in out
    assert ('| ---' in out) == (fmt == 'md')


def test_note_crud_and_build(db, run_cli):
    code, out, err = run_cli('note', 'add', '主题', '--text', '结论', '--no-build')
    ident = json.loads(out)['data']['id']
    assert code == 0 and not err and not run_cli.root.exists()
    assert json.loads(run_cli('note', 'list', '主题')[1])['data'][0]['id'] == ident
    assert run_cli('build', '--tag', '主题')[0] == 0
    assert '结论' in run_cli('show', '主题')[1]
    assert run_cli('note', 'remove', str(ident))[0] == 0
    assert not kb.list_notes(db, '主题')


@pytest.mark.parametrize('failure', [OSError('private credential'), RuntimeError('private credential'), {'errors': ['failed']}])
def test_committed_note_failure_preserves_id(db, run_cli, monkeypatch, failure):
    monkeypatch.setattr(kb, 'build', Mock(**({'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure})))
    code, out, err = run_cli('note', 'add', '主题', '--text', '已提交')
    payload = json.loads(out)
    assert code == 2 and payload['error']['committed'] is True
    assert payload['error']['id'] == kb.list_notes(db, '主题')[0]['id']
    assert 'private credential' not in out + err


@pytest.mark.parametrize('failure,kind', [(OSError('secret'), 'io_error'), (RuntimeError('secret'), 'model_error')])
def test_error_categories_and_no_secrets(run_cli, monkeypatch, failure, kind):
    monkeypatch.setattr(kb, 'build', Mock(side_effect=failure))
    code, out, err = run_cli('build')
    assert code == 2 and json.loads(out)['error']['type'] == kind and 'secret' not in out + err


def test_missing_db_and_corrupt_index(run_cli, monkeypatch):
    monkeypatch.delenv('DB_PATH')
    code, out, err = run_cli('search', 'A')
    assert code == 2 and json.loads(out)['error']['type'] == 'io_error'
    run_cli.root.mkdir()
    (run_cli.root / 'index.json').write_text('broken')
    assert json.loads(run_cli('list')[1])['error']['type'] == 'io_error'


def test_legacy_module_and_wrapper_symlink_from_other_cwd(db, run_cli, tmp_path):
    seed(db, run_cli.root)
    repo = Path(__file__).resolve().parents[1]
    wrapper = repo / 'scripts/qwennote-kb'
    assert wrapper.stat().st_mode & 0o111 == 0o111
    link = tmp_path / 'bin/qwennote-kb'
    link.parent.mkdir()
    link.symlink_to(os.path.relpath(wrapper, link.parent))
    commands = [([sys.executable, '-m', module], repo) for module in ('app.kb', 'app.kb_cli')]
    commands.append(([str(link)], tmp_path))
    for command, cwd in commands:
        result = subprocess.run([*command, 'list', '--json'], cwd=cwd, capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)['data'][0]['name'] == '测试主题' and not result.stderr


@pytest.mark.parametrize('args', [('--dry-run', 'build'), ('--dry-run', 'note', 'remove', '1'),
                                 ('note', '--dry-run', 'add', '主题', '--text', '结论')])
def test_dry_run_global_positions(run_cli, monkeypatch, args):
    monkeypatch.setattr(kb_cli, '_store', Mock(side_effect=AssertionError('must not open DB')))
    code, out, err = run_cli(*args)
    assert code == 0 and not err and json.loads(out)['data']['dry_run'] is True


def test_dry_run_global_rejects_read_commands(run_cli):
    code, out, err = run_cli('--dry-run', 'list')
    assert code == 2 and err and json.loads(out)['error']['type'] == 'invalid_argument'
