from datetime import datetime
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from app import tag, tag_maintenance as maintenance
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.tag_vocabulary import SEED_TAGS, normalize_tag
from app.tagging import DEFAULT_INSTRUCTIONS, build_system_prompt, load_tagging_context


@pytest.fixture
def vocabulary(store):
    store.migrate()
    return store


def rows(store, table):
    with store._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


def add_recording(store, key, labels):
    store.upsert_recording(RecordingMeta(key, '会议', datetime.now(SHANGHAI), 1),
                           RecordingDetail(key, '', '', False, False), tag.now())
    store.replace_tags(store.get_recording(key), labels, 'test', tag.now())


def run(store, handler=None):
    return maintenance.run_maintenance(store, settings=tag.TagSettings('test-key' if handler else ''),
                                       transport=httpx.MockTransport(handler) if handler else None)


def answer(value):
    return httpx.Response(200, json={'content': [{'type': 'text', 'text': json.dumps({'duplicate_of': value})}]})


@pytest.mark.parametrize('name', ['AI Tutor', 'ai tutor', 'AI-Tutor', 'AI Tutor项目',
                                  '　ＡＩ　Ｔｕｔｏｒ　', 'AI·Tutor', 'AI_Tutor/',
                                  '（AI）(Tutor)!，。', '\tAI\nTutor相关', 'AI Tutor类', 'AI Tutor项目相关'])
def test_normalized_equivalents(name):
    assert normalize_tag(name) == 'aitutor'
    assert normalize_tag(normalize_tag(name)) == 'aitutor'


@pytest.mark.parametrize('name', ['AI教学', 'AI Tutors', '①AI Tutor', '项目AI Tutor', 'AI+Tutor'])
def test_not_semantic_or_compatibility_normalization(name):
    assert normalize_tag(name) != normalize_tag('AI Tutor')


def test_seed_migration_is_once_and_preserves_instructions(vocabulary):
    assert [row['name'] for row in rows(vocabulary, 'tags')] == list(SEED_TAGS)
    assert all(row['source'] == 'seed' and row['status'] == 'active' for row in rows(vocabulary, 'tags'))
    with vocabulary._connect() as conn:
        conn.execute("UPDATE assistant_config SET instructions = '反馈原文' WHERE id = 1")
    vocabulary.migrate()
    assert len(rows(vocabulary, 'tag_change_log')) == 11
    assert rows(vocabulary, 'assistant_config')[0]['instructions'] == '反馈原文'


def test_empty_vocabulary_stays_empty_on_restart(vocabulary):
    with vocabulary._connect() as conn:
        conn.execute('DELETE FROM tags')
    vocabulary.migrate()
    assert rows(vocabulary, 'tags') == []


def test_normalized_merge_rewrites_deduplicates_and_is_idempotent(vocabulary):
    add_recording(vocabulary, 'a', ['AI-Tutor', 'AI Tutor', 'ai tutor', 'AI Tutor项目'])
    first = run(vocabulary)
    assert (first['added'], first['merged']) == (0, 3)
    assert vocabulary.get_recording('a').tags == ('AI Tutor',)
    tags = {row['name']: row for row in rows(vocabulary, 'tags')}
    assert tags['AI Tutor']['usage_count'] == 1
    assert all(row['merged_into'] == tags['AI Tutor']['id'] and row['usage_count'] == 0
               for row in tags.values() if row['status'] == 'merged')
    assert rows(vocabulary, 'tag_aliases') == [{'alias': 'aitutor', 'tag_id': tags['AI Tutor']['id']}]
    second = run(vocabulary)
    assert (second['added'], second['merged'], second['proposed']) == (0, 0, 0)
    logs = rows(vocabulary, 'tag_change_log')
    assert len([row for row in logs if row['action'] == 'merge']) == 3
    assert json.loads(logs[-1]['detail'])['status'] == 'success'
    assert logs[-1]['action'] == 'run'


def test_most_used_spelling_wins_even_over_seed(vocabulary):
    add_recording(vocabulary, 'a', ['AI Tutor', 'AI-Tutor'])
    add_recording(vocabulary, 'b', ['AI-Tutor'])
    result = run(vocabulary)
    assert result['merged'] == 1
    assert vocabulary.get_recording('a').tags == ('AI-Tutor',)
    tags = {row['name']: row for row in rows(vocabulary, 'tags')}
    assert tags['AI-Tutor']['usage_count'] == 2
    assert tags['AI Tutor']['merged_into'] == tags['AI-Tutor']['id']
    assert tags['AI Tutor']['status'] == 'merged'
    vocabulary.migrate()
    assert next(row for row in rows(vocabulary, 'tags') if row['name'] == 'AI Tutor')['status'] == 'merged'


def test_existing_vocabulary_duplicates_merge_tie_uses_id(vocabulary):
    with vocabulary._connect() as conn:
        conn.execute("INSERT INTO tags (name, status, source, usage_count, created_at, updated_at) VALUES ('ai tutor', 'active', 'manual', 99, ?, ?)", (tag.now(), tag.now()))
    result = run(vocabulary)
    assert (result['added'], result['merged']) == (0, 1)
    assert rows(vocabulary, 'tags')[-1]['status'] == 'merged'  # Actual counts are both 0.


def test_new_tag_counts_and_audit(vocabulary):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['全新业务'])
    result = run(vocabulary)
    assert result['added'] == 1 and result['merged'] == 0
    row = rows(vocabulary, 'tags')[-1]
    assert row['name'] == '全新业务' and row['source'] == 'model' and row['usage_count'] == 2
    adds = [json.loads(row['detail']) for row in rows(vocabulary, 'tag_change_log') if row['action'] == 'add']
    assert adds[-1] == {'from': None, 'to': '全新业务', 'reason': 'model'}
    assert run(vocabulary)['added'] == 0


def test_semantic_only_proposes_and_one_request_per_candidate(vocabulary):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['AI教学'])
    calls = []
    def handler(request):
        calls.append(request)
        data = json.loads(request.content)
        assert data['model'] == 'deepseek-flash'
        assert json.loads(data['messages'][0]['content'])['tag'] == 'AI教学'
        return answer('AI Tutor')
    result = run(vocabulary, handler)
    assert (result['added'], result['merged'], result['proposed'], result['checked']) == (1, 0, 1, 1)
    assert all(row['status'] == 'active' for row in rows(vocabulary, 'tags'))
    assert vocabulary.get_recording('a').tags == ('AI教学',)
    proposal = rows(vocabulary, 'tag_merge_proposals')[0]
    assert (proposal['from_tag'], proposal['to_tag'], proposal['reason'], proposal['status']) == ('AI教学', 'AI Tutor', 'model', 'pending')
    assert run(vocabulary, handler)['proposed'] == 0 and len(calls) == 1


def test_candidate_becomes_eligible_after_second_use_and_null_checked_once(vocabulary):
    add_recording(vocabulary, 'a', ['新业务'])
    calls = []
    handler = lambda request: calls.append(request) or answer(None)
    assert run(vocabulary, handler)['checked'] == 0
    add_recording(vocabulary, 'b', ['新业务'])
    assert run(vocabulary, handler)['checked'] == 1
    assert run(vocabulary, handler)['checked'] == 0 and len(calls) == 1
    assert rows(vocabulary, 'tag_merge_proposals') == []


def test_distant_length_does_not_call_model(vocabulary):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['这是一条字数特别特别多且完全无关的标签'])
    def forbidden(request):
        pytest.fail('No similar length candidate')
    assert run(vocabulary, forbidden)['checked'] == 0


@pytest.mark.parametrize('mode', ['http', 'json', 'target', 'type', 'exception'])
def test_model_failure_is_safe_and_retryable(vocabulary, caplog, mode):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['AI教学'])
    def handler(request):
        if mode == 'http':
            return httpx.Response(429, text='test-key')
        if mode == 'json':
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'test-key'}]})
        if mode == 'target':
            return answer('invented target')
        if mode == 'type':
            return answer(['AI Tutor'])
        raise RuntimeError('test-key')
    result = run(vocabulary, handler)
    assert result['failed'] == 1 and result['added'] == 1
    assert rows(vocabulary, 'tag_merge_proposals') == rows(vocabulary, 'tag_semantic_checks') == []
    assert 'test-key' not in caplog.text + str(result)
    assert json.loads(next(row for row in reversed(rows(vocabulary, 'tag_change_log')) if row['action'] == 'run')['detail'])['status'] == 'partial'
    assert run(vocabulary, lambda _: answer('AI Tutor'))['proposed'] == 1


def test_missing_auth_is_deferred_then_recovered(vocabulary):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['AI教学'])
    assert run(vocabulary)['deferred'] == 1
    assert run(vocabulary, lambda _: answer('AI Tutor'))['proposed'] == 1


def proposal(store, source='AI教学', target='AI Tutor'):
    with store._connect() as conn:
        return conn.execute("INSERT INTO tag_merge_proposals (from_tag, to_tag, reason, status, created_at) VALUES (?, ?, 'model', 'pending', ?)",
                            (source, target, tag.now())).lastrowid


def test_accept_merge_and_alias_reuse(vocabulary):
    add_recording(vocabulary, 'a', ['AI教学', 'AI Tutor'])
    run(vocabulary)
    key = proposal(vocabulary)
    assert maintenance.accept_proposal(key, store=vocabulary)
    assert not maintenance.accept_proposal(key, store=vocabulary)
    assert not maintenance.reject_proposal(key, store=vocabulary)
    assert vocabulary.get_recording('a').tags == ('AI Tutor',)
    assert rows(vocabulary, 'tag_merge_proposals')[0]['status'] == 'accepted'
    add_recording(vocabulary, 'b', ['AI教学', 'ＡＩ教学相关'])
    assert run(vocabulary)['merged'] == 1
    assert vocabulary.get_recording('b').tags == ('AI Tutor',)
    assert next(row for row in rows(vocabulary, 'tags') if row['name'] == 'AI Tutor')['usage_count'] == 2
    assert run(vocabulary)['merged'] == 0


def test_reject_does_not_merge_or_repropose(vocabulary):
    for key in ['a', 'b']:
        add_recording(vocabulary, key, ['AI教学'])
    run(vocabulary, lambda _: answer('AI Tutor'))
    key = rows(vocabulary, 'tag_merge_proposals')[0]['id']
    assert maintenance.reject_proposal(key, store=vocabulary)
    assert not maintenance.reject_proposal(key, store=vocabulary)
    assert not maintenance.accept_proposal(key, store=vocabulary)
    assert vocabulary.get_recording('a').tags == ('AI教学',)
    assert run(vocabulary, lambda _: answer('AI Tutor'))['proposed'] == 0
    assert len(rows(vocabulary, 'tag_merge_proposals')) == 1


def test_merge_chain_and_stale_proposal_no_cycle(vocabulary):
    add_recording(vocabulary, 'a', ['AI教学', 'AI Tutor', '模考系统'])
    run(vocabulary)
    first = proposal(vocabulary)
    stale = proposal(vocabulary, 'AI Tutor', 'AI教学')
    second = proposal(vocabulary, 'AI Tutor', '模考系统')
    maintenance.accept_proposal(first, store=vocabulary)
    maintenance.accept_proposal(second, store=vocabulary)
    maintenance.accept_proposal(stale, store=vocabulary)
    assert vocabulary.get_recording('a').tags == ('模考系统',)
    tags = rows(vocabulary, 'tags')
    target = next(row for row in tags if row['name'] == '模考系统')['id']
    assert all(row['merged_into'] == target for row in tags if row['status'] == 'merged')
    assert all(row['tag_id'] == target for row in rows(vocabulary, 'tag_aliases'))


def test_invalid_proposal_rolls_back_and_missing_id(vocabulary):
    key = proposal(vocabulary, 'missing')
    with pytest.raises(ValueError):
        maintenance.accept_proposal(key, store=vocabulary)
    assert rows(vocabulary, 'tag_merge_proposals')[0]['status'] == 'pending'
    for action in [maintenance.accept_proposal, maintenance.reject_proposal]:
        with pytest.raises(ValueError):
            action(999, store=vocabulary)


def test_dynamic_prompt_and_feedback_sent_verbatim(vocabulary):
    instructions = '  优先按业务线。\nAI Tutor 内部培训仍归 AI Tutor。  '
    with vocabulary._connect() as conn:
        conn.execute('UPDATE assistant_config SET instructions = ? WHERE id = 1', (instructions,))
    add_recording(vocabulary, 'a', ['新业务'])
    run(vocabulary)
    names, prompt = load_tagging_context(vocabulary)
    assert instructions in prompt and '新业务' in names
    assert '归类规则：' + instructions in prompt
    assert DEFAULT_INSTRUCTIONS not in prompt
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"tags":["新业务"]}'}]})
    result = tag.run_tags(vocabulary, force=True, settings=tag.TagSettings('test-key'), transport=httpx.MockTransport(handler))
    assert result['succeeded'] == 1
    assert calls[0]['system'] == prompt
    assert vocabulary.get_recording('a').tags == ('新业务',)


def test_default_and_empty_vocabulary_fallback(vocabulary):
    assert DEFAULT_INSTRUCTIONS in build_system_prompt(SEED_TAGS)
    with vocabulary._connect() as conn:
        conn.execute('DELETE FROM tags')
    add_recording(vocabulary, 'a', [])
    def handler(request):
        assert '词表为空' in json.loads(request.content)['system']
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"tags":["自由业务标签很长", "另一个"]}'}]})
    result = tag.run_tags(vocabulary, settings=tag.TagSettings('test-key'), transport=httpx.MockTransport(handler))
    assert result['succeeded'] == 1
    assert vocabulary.get_recording('a').tags == ('其他:自由业务标签',)


def test_cli_and_run_log_on_no_change(vocabulary, monkeypatch, capsys):
    monkeypatch.setenv('DB_PATH', str(vocabulary.db_path))
    assert maintenance.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['added'] == result['merged'] == result['proposed'] == 0
    assert rows(vocabulary, 'tag_change_log')[-1]['action'] == 'run'
    monkeypatch.delenv('DB_PATH')
    with pytest.raises(SystemExit):
        maintenance.main([])


def test_deterministic_transaction_rollback(vocabulary, monkeypatch):
    add_recording(vocabulary, 'a', ['AI-Tutor'])
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('simulated write failure')
    monkeypatch.setattr(maintenance, 'merge_tags', fail)
    with pytest.raises(sqlite3.OperationalError):
        run(vocabulary)
    assert len(rows(vocabulary, 'tags')) == 11
    assert vocabulary.get_recording('a').tags == ('AI-Tutor',)
    assert json.loads(rows(vocabulary, 'tag_change_log')[-1]['detail'])['status'] == 'failed'


def test_usage_drops_after_recording_deleted(vocabulary):
    add_recording(vocabulary, 'a', ['AI Tutor'])
    run(vocabulary)
    assert rows(vocabulary, 'tags')[0]['usage_count'] == 1
    with vocabulary._connect() as conn:
        conn.execute('DELETE FROM recordings')
    run(vocabulary)
    assert all(row['usage_count'] == 0 for row in rows(vocabulary, 'tags'))


def test_timer_service_contract():
    root = Path(__file__).resolve().parents[1] / 'deploy/systemd'
    timer = (root / 'qwennote-tag-maintenance.timer').read_text()
    service = (root / 'qwennote-tag-maintenance.service').read_text()
    assert 'OnCalendar=*-*-* 05:00:00 Asia/Shanghai' in timer
    assert 'Persistent=true' in timer
    assert 'User=qwennote' in service and ' -m app.tag_maintenance' in service
    assert 'EnvironmentFile=-/etc/claude/deepseek.qwennote.env' in service
    assert 'ReadWritePaths=/opt/qwennote/data' in service
