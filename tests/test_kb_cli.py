"""T5 CLI, exact file mtimes, sync isolation and independent daily claims."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from app import kb
from app.automation import AutomationSettings
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.scheduler_tick import tick, next_runs, read_state
from app.sync import run_sync
from fakes import FakeMinutesSource
from test_kb import db, sample, add_tag, add_meeting, meeting_content, named
from test_login import login
from test_settings import settings_form


@pytest.mark.parametrize('value', [None, '', '/tmp/persistent-knowledge'])
def test_default_root_respects_environment_on_import(value):
    env = dict(os.environ)
    env.pop('KB_ROOT', None)
    if value is not None:
        env['KB_ROOT'] = value
    result = subprocess.run([sys.executable, '-c',
                             'from app.kb import DEFAULT_ROOT; print(DEFAULT_ROOT)'],
                            env=env, check=True, capture_output=True, text=True)
    expected = Path(value) if value else Path(kb.__file__).resolve().parents[1] / 'knowledge'
    assert result.stdout.strip() == str(expected)


def mtimes(root):
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob('*') if p.is_file() and 'db' not in p.relative_to(root).parts}


@pytest.fixture
def cli(db, tmp_path, monkeypatch, capsys):
    root = tmp_path / 'knowledge'
    monkeypatch.setenv('DB_PATH', str(db.db_path))
    monkeypatch.setenv('KB_ROOT', str(root))
    def call(*args):
        code = kb.main(list(args))
        output = capsys.readouterr()
        return code, output.out, output.err
    call.root = root
    return call


def test_full_build_count_hash_and_idempotence(sample, tmp_path):
    first = kb.build(sample, tmp_path)
    assert len(first['rebuilt']) == 22 and not first['skipped'] and not first['errors']
    index = json.loads((tmp_path / 'index.json').read_text())
    for tag in index['tags']:
        assert hashlib.sha256((tmp_path / tag['path']).read_bytes()).hexdigest() == tag['content_hash']
    before = mtimes(tmp_path)
    second = kb.build(sample, tmp_path)
    assert second == dict(rebuilt=[], skipped=first['rebuilt'], errors=[])
    assert mtimes(tmp_path) == before


def test_incremental_only_selected_category_and_indexes(sample, tmp_path):
    kb.build(sample, tmp_path)
    before = mtimes(tmp_path)
    with sample._connect() as conn:
        conn.execute("UPDATE recordings SET duration_min=999 WHERE recording_id='r00'")
    result = kb.build(sample, tmp_path, tags=['标签00'])
    assert result == dict(rebuilt=['标签00'], skipped=[], errors=[])
    after = mtimes(tmp_path)
    assert {p for p in before if after[p] != before[p]} == {
        'tags/标签00.md', 'categories/产品.md', 'index.json', 'index.md'}


def test_partial_empty_root_does_not_build_unselected(sample, tmp_path):
    result = kb.build(sample, tmp_path, tags=['标签00'])
    assert result['rebuilt'] == ['标签00']
    assert {p.name for p in (tmp_path / 'tags').glob('*.md')} == {'标签00.md'}
    assert {p.name for p in (tmp_path / 'categories').glob('*.md')} == {'产品.md'}


def test_unselected_dirty_metadata_is_not_marked_built(db, tmp_path):
    add_tag(db, 'A'); add_tag(db, 'B')
    kb.build(db, tmp_path)
    before = json.loads((tmp_path / 'index.json').read_text())
    kb.add_note(db, 'B', '尚未构建')
    kb.build(db, tmp_path, tags=['A'])
    after = json.loads((tmp_path / 'index.json').read_text())
    assert named(before['tags'], 'B') == named(after['tags'], 'B')
    assert kb.build(db, tmp_path)['rebuilt'] == ['B']


def test_render_failure_isolated_and_retryable(db, tmp_path, monkeypatch):
    add_tag(db, 'A'); add_tag(db, 'B')
    render = kb.render_tag_file
    def fail(store, tag, **kw):
        if tag['name'] == 'A':
            raise RuntimeError('private credential')
        return render(store, tag, **kw)
    monkeypatch.setattr(kb, 'render_tag_file', fail)
    result = kb.build(db, tmp_path)
    assert result == dict(rebuilt=['B'], skipped=[], errors=[dict(tag='A', error='tag_build_failed')])
    assert json.loads((tmp_path / 'index.json').read_text())['tags'][0]['error'] == 'tag_build_failed'
    monkeypatch.setattr(kb, 'render_tag_file', render)
    assert kb.build(db, tmp_path) == dict(rebuilt=['A'], skipped=['B'], errors=[])


@pytest.mark.parametrize('change', ['title', 'summary', 'primary'])
def test_rendered_content_changes_invalidate_hash(db, tmp_path, change):
    add_tag(db, 'A'); add_meeting(db, 'one', ['A'])
    kb.build(db, tmp_path)
    meeting_content(db, 'one', title='更正标题' if change == 'title' else '',
                    summary='更正总结。' if change == 'summary' else '',
                    primary='A' if change == 'primary' else None)
    assert kb.build(db, tmp_path)['rebuilt'] == ['A']


def test_deleted_file_and_edited_file_are_repaired(db, tmp_path):
    add_tag(db, 'A'); kb.build(db, tmp_path)
    path = tmp_path / 'tags/A.md'
    path.unlink()
    assert kb.build(db, tmp_path)['rebuilt'] == ['A']
    path.write_text('broken')
    assert kb.build(db, tmp_path)['rebuilt'] == ['A']
    assert path.read_text().startswith('# A\n')


def test_seven_commands_and_json(db, cli):
    add_tag(db, 'A'); add_meeting(db, 'one', ['A'])
    code, out, err = cli('build', '--full')
    assert code == 0 and json.loads(out)['rebuilt'] == ['A'] and not err
    code, out, err = cli('list', '--category', '产品', '--json')
    assert code == 0 and json.loads(out)[0]['name'] == 'A' and not err
    code, out, err = cli('show', 'A', '--json')
    payload = json.loads(out)
    assert code == 0 and set(payload) == {'tag', 'path', 'sessions', 'notes', 'content'}
    assert cli('show', 'A')[1] == payload['content']
    code, out, err = cli('search', 'a', '--json')
    assert code == 0 and json.loads(out)[0]['name'] == 'A'
    code, out, err = cli('note', 'add', 'A', '--text', 'CLI 验收记录', '--source', 'codex')
    ident = int(out)
    assert code == 0 and not err and 'CLI 验收记录' in (cli.root / 'tags/A.md').read_text()
    code, out, err = cli('note', 'list', 'A', '--json')
    assert code == 0 and json.loads(out)[0]['id'] == ident
    assert cli('note', 'remove', str(ident))[0] == 0
    assert 'CLI 验收记录' not in (cli.root / 'tags/A.md').read_text()


@pytest.mark.parametrize('args,empty', [
    (['list', '--json'], []), (['show', '不存在', '--json'], None),
    (['search', '不存在', '--json'], []), (['note', 'list', '不存在', '--json'], []),
])
def test_empty_queries_exit_one_valid_json(cli, args, empty):
    code, out, err = cli(*args)
    assert code == 1 and json.loads(out) == empty and not err


@pytest.mark.parametrize('args', [
    ['list'], ['show', '不存在'], ['search', '不存在'], ['note', 'list', '不存在'],
    ['note', 'remove', '999'], ['build', '--tag', '不存在'],
])
def test_plain_empty_queries(cli, args):
    code, out, err = cli(*args)
    assert code == 1 and not err


@pytest.mark.parametrize('args', [
    [], ['unknown'], ['build', '--full', '--tag', 'A'], ['search', 'x', '--limit', '0'],
    ['search', 'x', '--limit', 'bad'], ['note', 'remove', '-1'], ['note', 'add', 'A'],
])
def test_invalid_arguments_stderr_only(cli, capsys, args):
    with pytest.raises(SystemExit) as error:
        cli(*args)
    output = capsys.readouterr()
    assert error.value.code == 2 and output.err and not output.out


@pytest.mark.parametrize('text', ['', 'x' * 2001])
def test_invalid_note_does_not_write(db, cli, text):
    code, out, err = cli('note', 'add', 'A', '--text', text)
    assert code == 2 and not out and err and not kb.list_notes(db, 'A')


def test_notes_no_build_and_last_orphan_removal(db, cli):
    code, out, _ = cli('note', 'add', '新主题', '--text', '测试', '--no-build')
    assert code == 0 and not cli.root.exists()
    ident = out.strip()
    assert cli('build', '--tag', '新主题')[0] == 0
    assert cli('note', 'remove', ident, '--no-build')[0] == 0
    assert '测试' in (cli.root / 'tags/新主题.md').read_text()
    assert cli('build', '--tag', '新主题')[0] == 0
    assert not (cli.root / 'tags/新主题.md').exists()
    assert cli('show', '新主题')[0] == 1


def test_search_all_four_fields_beyond_render_limits(db, cli, monkeypatch):
    for name in ['标签匹配', '标题', '记录', '综述']:
        add_tag(db, name)
    for i in range(21):
        add_meeting(db, f'r{i:02}', ['标题'])
        meeting_content(db, f'r{i:02}', title='深处标题' if i == 0 else '其他')
        kb.add_note(db, '记录', '深处正文' if i == 0 else '其他')
    monkeypatch.setattr(kb, 'generate_summary', lambda store, tag: '模型综述词' if tag['name'] == '综述' else None)
    cli('build')
    for term, expected in [('标签匹配', '标签匹配'), ('深处标题', '标题'), ('深处正文', '记录'), ('模型综述词', '综述')]:
        code, out, err = cli('search', term, '--json')
        assert code == 0 and [t['name'] for t in json.loads(out)] == [expected]
    assert len(json.loads(cli('search', '其他', '--limit', '1', '--json')[1])) == 1


def test_runtime_error_stderr_and_durable_note_id(db, cli, monkeypatch):
    monkeypatch.setattr(kb, 'build', Mock(side_effect=OSError('secret')))
    code, out, err = cli('note', 'add', 'A', '--text', '保留')
    assert code == 2 and int(out) == kb.list_notes(db, 'A')[0]['id']
    assert err and 'secret' not in err
    code, out, err = cli('build')
    assert code == 2 and not out and err


def test_subprocess_module_entrypoint_and_root(db, tmp_path):
    add_tag(db, 'A')
    root = tmp_path / 'custom'
    env = dict(os.environ, DB_PATH=str(db.db_path), ANTHROPIC_AUTH_TOKEN='')
    result = subprocess.run([sys.executable, '-m', 'app.kb', 'build', '--full', '--root', str(root)],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0 and json.loads(result.stdout)['rebuilt'] == ['A']
    result = subprocess.run([sys.executable, '-m', 'app.kb', '--root', str(root), 'show', 'A', '--json'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0 and json.loads(result.stdout)['tag'] == 'A'


@pytest.mark.parametrize('failure', ['raise', 'partial', 'ok'])
def test_sync_finished_before_build_and_failure_isolation(db, monkeypatch, caplog, failure):
    now = datetime.now(SHANGHAI) - timedelta(minutes=1)
    source = FakeMinutesSource([RecordingMeta('new', '新会议', now, 20)],
                              {'new': RecordingDetail('new', '总结', '正文', True, True)})
    calls = []
    def build(store, root, tags):
        calls.append(tags)
        assert store.latest_sync().status == 'success'
        with store.sync_lock():
            pass
        if failure == 'raise':
            raise RuntimeError('private credential')
        return dict(rebuilt=[], skipped=[], errors=['failure'] if failure == 'partial' else [])
    monkeypatch.setattr(kb, 'build', build)
    result = run_sync(source, db)
    assert calls == [['未分类']]
    assert result.status == 'success' and result.error is None and db.latest_sync() == result
    assert ('知识库' in caplog.text) == (failure != 'ok')
    assert 'credential' not in caplog.text


def test_sync_updates_canonical_tags_and_ignores_unchanged(db, monkeypatch, detail_clock):
    add_tag(db, 'A')
    now = datetime.now(SHANGHAI) - timedelta(minutes=1)
    meta = RecordingMeta('existing', '原题', now, 20)
    detail = RecordingDetail('existing', '总结', '正文', True, True)
    db.upsert_recording(meta, detail, now.isoformat())
    with db._connect() as conn:
        conn.execute("INSERT INTO tag_aliases SELECT '别名', id FROM tags WHERE name='A'")
        conn.execute("INSERT INTO recording_tags VALUES ('existing', '别名', 1, 'fixture', ?)", (now.isoformat(),))
    source = FakeMinutesSource([replace(meta, title='更正')], {'existing': detail})
    build = Mock(return_value=dict(errors=[]))
    monkeypatch.setattr(kb, 'build', build)
    assert run_sync(source, db, full=True).updated == 1
    assert build.call_args.kwargs['tags'] == ['A', '未分类']
    build.reset_mock(); detail_clock[0] += 60
    assert run_sync(source, db, full=True).updated == 0
    build.assert_not_called()
    db.set_setting('kb_auto_build', False)
    detail_clock[0] += 60; source.recordings[0] = replace(meta, title='再更正')
    assert run_sync(source, db, full=True).updated == 1
    build.assert_not_called()


@pytest.mark.parametrize('failed', ['board', 'kb_full_build', None])
def test_daily_order_failure_isolation_and_claim(db, monkeypatch, failed):
    db.save_settings(replace(AutomationSettings(), sync_enabled=False, full_sync_enabled=False, tag_enabled=False))
    monkeypatch.setattr('app.enrichment.has_work', lambda s: True)
    calls = []
    def callback(name):
        def call(*args):
            calls.append(name)
            assert name in read_state(db)
            if name == failed:
                raise RuntimeError('secret')
        return call
    jobs = {f'{name}_job': callback(name) for name in ('board', 'kb_full_build', 'enrichment')}
    now = datetime(2026, 11, 14, 22, 0, tzinfo=SHANGHAI)
    result = tick(db, now=now, **jobs)
    assert calls == ['board', 'kb_full_build', 'enrichment']
    assert result['failed'] == ([failed] if failed else [])
    assert tick(db, now=now, **jobs)['executed'] == []
    due = next_runs(db, db.get_settings(), now)
    assert due['kb_full_build'] == due['board'] == now + timedelta(days=1)


def test_kb_switch_persists_and_daily_full_remains_enabled(client, store):
    login(client)
    form = settings_form(client)
    del form['kb_auto_build']
    assert client.post('/settings', data=form).status_code == 303
    assert store.get_settings().kb_auto_build is False
    assert 'id="kb_auto_build"' in client.get('/settings').text
    assert next_runs(store, store.get_settings(), datetime.now(SHANGHAI))['kb_full_build'] is not None


def test_partial_build_retains_unselected_orphan_until_full(db, tmp_path):
    ident = kb.add_note(db, 'B', '即将删除')
    add_tag(db, 'A'); kb.build(db, tmp_path)
    kb.remove_note(db, ident)
    before = (tmp_path / 'tags/B.md').stat().st_mtime
    kb.build(db, tmp_path, tags=['A'])
    assert (tmp_path / 'tags/B.md').stat().st_mtime == before
    kb.build(db, tmp_path)
    assert not (tmp_path / 'tags/B.md').exists()


def test_search_default_five_and_partial_index(db, cli):
    for i in range(7):
        add_tag(db, f'共同{i}')
    cli('build', '--tag', '共同0')
    code, out, err = cli('search', '共同', '--json')
    assert code == 0 and len(json.loads(out)) == 5 and not err


@pytest.mark.parametrize('path', ['', '/absent/no-database.sqlite3'])
def test_missing_database_is_runtime_error(cli, monkeypatch, path):
    monkeypatch.setenv('DB_PATH', path)
    code, out, err = cli('build')
    assert code == 2 and not out and 'DB_PATH' in err


def test_category_failure_does_not_abort_other_tags(db, tmp_path, monkeypatch):
    add_tag(db, 'A', '产品'); add_tag(db, 'B', '技术')
    original = kb.render_category_file
    def render(store, category, tags):
        if category['name'] == '产品':
            raise OSError('secret')
        return original(store, category, tags)
    monkeypatch.setattr(kb, 'render_category_file', render)
    result = kb.build(db, tmp_path)
    assert result['rebuilt'] == ['A', 'B']
    assert result['errors'] == [dict(category='产品', error='category_build_failed')]
    assert (tmp_path / 'categories/技术.md').exists()


def test_build_serializes_parallel_writers(db, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    add_tag(db, 'A')
    entered, release = Event(), Event()
    original = kb.render_tag_file
    calls = []
    def render(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(kb, 'render_tag_file', render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(kb.build, db, tmp_path)
        assert entered.wait(5)
        second = pool.submit(kb.build, db, tmp_path)
        assert not second.done()
        release.set()
        assert first.result()['rebuilt'] == ['A']
        assert second.result()['skipped'] == ['A']
