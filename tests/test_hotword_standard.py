"""Work order 17: real migration, occurrence isolation and alias-independent discovery."""
import json
import re
import sqlite3

import httpx
import pytest

from app.hotword_judge import parse_decisions, run_judgments
from app.hotwords import HotwordApplier
from test_hotword_judge import SETTINGS, response, clear, EXITS
from test_hotwords import populated, post, recording_rows
from test_login import login


def legacy(store, *, apply=False):
    store.db_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as c:
        c.execute("""CREATE TABLE hotwords (id INTEGER PRIMARY KEY, source TEXT NOT NULL UNIQUE,
          target TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
          hits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        c.executemany('INSERT INTO hotwords VALUES (?,?,?,?,?,?,?,?)', [
            (7, '小鹅直播', '小鹿直播', '首条', 0, 12, '2026-01', '2026-02'),
            (9, '小路直播', '小鹿直播', '次条', 1, 5, '2026-02', '2026-03'),
            (12, 'AI', '人工智能', '', 1, 3, '2026-01', '2026-01')])
        c.execute("""CREATE TABLE recording_hotword_decisions (recording_id TEXT NOT NULL,
            hotword_id INTEGER NOT NULL, field TEXT NOT NULL, decision TEXT NOT NULL,
            reason TEXT NOT NULL, decided_at TEXT NOT NULL,
            PRIMARY KEY(recording_id,hotword_id,field))""")
        c.execute('INSERT INTO recording_hotword_decisions VALUES (?,?,?,?,?,?)',
                  ('legacy', 7, 'summary', 'apply' if apply else 'skip', '旧判断', '2026-01'))


def test_legacy_migration_merges_targets_accumulates_hits_and_removes_columns(store):
    legacy(store)
    store.migrate()
    rows = {r.term: r for r in store.list_hotwords()}
    assert set(rows) == {'小鹿直播', '人工智能'}
    word = rows['小鹿直播']
    assert (word.id, word.aliases, word.hits, word.enabled) == (7, '小鹅直播,小路直播', 17, True)
    assert word.note == '首条；次条' and (word.created_at, word.updated_at) == ('2026-01', '2026-03')
    with store._connect() as c:
        assert {r['name'] for r in c.execute('PRAGMA table_info(hotwords)')} == {
            'id', 'term', 'aliases', 'note', 'enabled', 'hits', 'created_at', 'updated_at', 'needs_review'}
    assert store.hotword_decisions('legacy') == []
    store.migrate()
    assert {r.term: r for r in store.list_hotwords()} == rows


@pytest.mark.parametrize('failure', ['unexpected-apply', 'injected-ddl'])
def test_failed_migration_rolls_back_all_hotword_changes(store, monkeypatch, failure):
    legacy(store, apply=failure == 'unexpected-apply')
    import app.hotword_migration as migration
    original = migration.migrate_hotwords
    def broken(c):
        original(c)
        raise sqlite3.OperationalError('simulated failure after archive and table rebuild')
    monkeypatch.setattr(migration, 'migrate_hotwords', broken)
    with pytest.raises((ValueError, sqlite3.OperationalError)): store.migrate()
    with store._connect() as c:
        assert 'source' in {r['name'] for r in c.execute('PRAGMA table_info(hotwords)')}
        assert c.execute('SELECT count(*) FROM hotwords').fetchone()[0] == 3
        assert c.execute('SELECT count(*) FROM recording_hotword_decisions').fetchone()[0] == 1
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='hotwords_standard'").fetchone()


def isolated(store, text='产品用小鹅直播；人名叫小鹅直播。'):
    clear(store)
    for w in store.list_hotwords(): store.delete_hotword(w.id)
    store.add_hotword('小鹿直播', '小鹅直播')
    with store._connect() as c:
        c.execute("UPDATE recordings SET summary_md=?,transcript_md=? WHERE recording_id='hotword'", (text, text))
    rule, = store.list_hotwords()
    recording = store.get_recording('hotword')
    if '小鹅直播' not in text:
        return rule
    start = text.index('小鹅直播')
    rows = [{'hotword_id': rule.id, 'field': f, 'span': '小鹅直播', 'replacement': rule.term,
             'reason': '此处明确指产品', 'start_offset': start, 'end_offset': start+4} for f in ('summary','transcript')]
    checks = [{'hotword_id': rule.id, 'field': f, 'status': 'apply', 'reason': 'variants_identified'} for f in ('summary','transcript')]
    assert store.save_hotword_decisions(recording, [rule], rows, [], checks)
    return rule


@pytest.mark.parametrize('path', EXITS)
def test_five_exits_replace_only_identified_occurrence(populated, store, path):
    isolated(store)
    original = recording_rows(store)
    result = populated.get(path)
    assert result.status_code == 200
    if path == '/api/v1/recordings/hotword':
        assert result.json()['summary_md'] == result.json()['transcript_md'] == '产品用小鹿直播；人名叫小鹅直播。'
    else:
        assert '产品用小鹿直播；人名叫小鹅直播。' in result.text
    assert recording_rows(store) == original


def test_per_occurrence_undo_restores_original_and_preserves_history(populated, store):
    rule = isolated(store)
    row = store.hotword_decisions('hotword')[0]
    before = recording_rows(store)
    assert store.revoke_hotword('hotword', rule.id, row['field'], row['id'])
    assert store.apply_hotwords('产品用小鹅直播；人名叫小鹅直播。', recording_id='hotword', field=row['field']) == '产品用小鹅直播；人名叫小鹅直播。'
    assert next(d for d in store.hotword_decisions('hotword') if d['id'] == row['id'])['reason'] == '此处明确指产品'
    assert store.hotword_presentation('hotword')['count'] == 1
    with store._connect() as c:
        assert c.execute('SELECT count(*) FROM recording_hotword_decision_history WHERE id=?', (row['id'],)).fetchone()[0] == 1
    assert recording_rows(store) == before


def test_identical_spans_can_have_independent_decisions_and_revocations(populated, store):
    rule = isolated(store)
    rows = store.hotword_decisions('hotword')
    original = store.get_recording('hotword')
    text = original.summary_md
    start = text.rindex('小鹅直播')
    extra = {'hotword_id': rule.id, 'field': 'summary', 'span': '小鹅直播', 'replacement': rule.term,
             'reason': '第二处单独判定', 'start_offset': start, 'end_offset': start+4}
    assert store.save_hotword_decisions(original, [rule], [extra], rows)
    assert store.apply_hotwords(text, recording_id='hotword', field='summary') == '产品用小鹿直播；人名叫小鹿直播。'
    second = next(d for d in store.hotword_decisions('hotword') if d['field'] == 'summary' and d['start_offset'] == start)
    assert store.revoke_hotword('hotword', rule.id, 'summary', second['id'])
    assert store.apply_hotwords(text, recording_id='hotword', field='summary') == '产品用小鹿直播；人名叫小鹅直播。'


@pytest.mark.parametrize('kind', ['unlisted', 'missing', 'too-long', 'ambiguous', 'empty'])
def test_span_validation_and_unlisted_alias_discovery(populated, store, kind):
    rule = isolated(store, text='产品用小路直播。') if kind == 'unlisted' else isolated(store)
    clear(store)
    if kind == 'unlisted':
        store.update_hotword(rule.id, rule.term, '', '')  # Discover an unlisted spelling without any alias.
    original = recording_rows(store)
    def handler(request):
        if kind == 'unlisted':
            assert '小路直播' in json.loads(request.content)['messages'][0]['content']
            return response([{'span': '小路直播', 'reason': '上下文指该产品'}])
        if kind == 'empty': return response([])
        return response([{'span': '不存在' if kind == 'missing' else '字'*21 if kind == 'too-long' else '小鹅直播小鹅直播', 'reason': '测试'}])
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(handler))
    if kind == 'unlisted':
        assert result['apply'] == 2 and not result['request_failed']
        assert store.apply_hotwords('产品用小路直播。', recording_id='hotword', field='summary') == '产品用小鹿直播。'
    elif kind == 'empty':
        assert result['apply'] == 0 and result['skip'] == 2 and not result['request_failed']
    else:
        assert result['apply'] == 0 and result['judge_failed'] == 2
    assert recording_rows(store) == original


def test_same_spelling_in_mixed_context_is_judged_separately(populated, store):
    isolated(store)
    clear(store)
    def handler(request):
        text = json.loads(request.content)['messages'][0]['content'].split('---\n')[1]
        if '产品用' in text:
            return response([{'span': '小鹅直播', 'reason': '产品上下文'}])
        return response([])
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(handler))
    assert result['apply'] == 2 and not result['request_failed']
    assert store.apply_hotwords('产品用小鹅直播；人名叫小鹅直播。', recording_id='hotword', field='summary') == '产品用小鹿直播；人名叫小鹅直播。'


def test_ambiguous_continuous_span_is_discarded_without_guessing():
    item = dict(hotword_id=1, field='summary', term='小鹿直播', text='小鹅直播与小鹅直播')
    assert parse_decisions('{"replacements":[{"span":"小鹅直播","reason":"产品"}]}', item, {'start': 0, 'text': item['text']}) == ([], True)


@pytest.mark.parametrize('reverse', [False, True])
def test_multiple_words_and_spans_use_longest_first_without_rescanning(reverse):
    rows = [dict(id=1, hotword_id=1, span='小鹅', replacement='短词', start_offset=0, end_offset=2),
            dict(id=2, hotword_id=2, span='小鹅直播', replacement='小鹿直播', start_offset=0, end_offset=4),
            dict(id=3, hotword_id=3, span='AI', replacement='人工智能', start_offset=5, end_offset=7),
            dict(id=4, hotword_id=2, span='小鹅直播', replacement='小鹿直播', start_offset=8, end_offset=12)]
    applier = HotwordApplier(rows[::-1] if reverse else rows)
    assert applier.apply_hotwords('小鹅直播 AI 小鹅直播') == '小鹿直播 人工智能 小鹿直播'
    assert applier.applied_ids == {2, 3, 4}


def test_only_standard_term_is_required_in_form(client, store):
    login(client)
    page = client.get('/hotwords').text
    assert '替换为' not in page and 'name="source"' not in page and 'name="target"' not in page
    assert '还没有热词。添加后，AI 会在新内容里识别可能的其它写法并替换成你的标准词' in page
    form = re.search(r'<form method="post" action="/hotwords".*?</form>', page, re.S)[0]
    assert re.findall(r'<label>(.*?)<input', form) == ['标准词（必填）', '别名提示（选填，逗号分隔）', '备注（选填）']
    assert post(client, term='小鹿直播').status_code == 303
    word, = store.list_hotwords()
    assert word.term == '小鹿直播' and word.aliases == word.note == ''


def test_missing_decision_id_cannot_revoke_entire_field(populated, store):
    rule = isolated(store)
    from test_hotword_judge import token
    before = store.hotword_decisions('hotword')
    result = populated.post(f'/recordings/hotword/hotwords/{rule.id}/revoke', data={'csrf_token': token(populated), 'field': 'summary'})
    assert result.status_code == 400 and store.hotword_decisions('hotword') == before


def test_invalid_spans_are_discarded_individually_while_valid_span_is_preserved():
    item = dict(hotword_id=1, field='summary', term='小鹿直播', text='用小鹅直播')
    payload = {'replacements': [{'span': '小鹅直播', 'reason': '明确指该产品'},
                                {'span': '不存在', 'reason': '不应接受'},
                                {'span': '长'*21, 'reason': '不应接受'}]}
    rows, failed = parse_decisions(json.dumps(payload), item, {'start': 0, 'text': item['text']})
    assert failed and len(rows) == 1
    assert (rows[0]['span'], rows[0]['start_offset'], rows[0]['replacement']) == ('小鹅直播', 1, '小鹿直播')


def test_legacy_apply_appearing_during_deployment_is_archived_before_rule_merge(store):
    legacy(store, apply=True)
    store.migrate()
    with store._connect() as c:
        row = dict(c.execute('SELECT * FROM recording_hotword_legacy_decisions').fetchone())
        assert (row['hotword_id'], row['source'], row['target'], row['decision'], row['reason']) == (7, '小鹅直播', '小鹿直播', 'apply', '旧判断')
        assert row['archived_at'] and row['decided_at'] == '2026-01'
        assert c.execute('SELECT count(*) FROM recording_hotword_decisions').fetchone()[0] == 0
    store.migrate()
    with store._connect() as c:
        assert c.execute('SELECT count(*) FROM recording_hotword_legacy_decisions').fetchone()[0] == 1
