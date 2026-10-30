"""Offline standard-term identification, exact locations, conservative failures and undo."""
import json
import re
from dataclasses import replace

import httpx
import pytest

from app.hotword_judge import contexts, candidates, parse_decisions, run_judgments, main, SYSTEM
from app.tag import TagSettings
from app.minutes import RecordingDetail, RecordingMeta
from app.sync import run_sync
from test_hotwords import populated, approve_rules, BODY, recording_rows

SETTINGS = TagSettings('synthetic-offline-test', 'https://model.invalid/anthropic', 'test')
EXITS = ['/recordings/hotword?view=summary', '/recordings/hotword?view=transcript',
         '/recordings/hotword.md', '/api/v1/recordings/hotword', '/api/v1/daily/2026-10-29?format=md']


def response(rows):
    return httpx.Response(200, json={'content': [{'type': 'text', 'text': json.dumps({'replacements': rows})}]})


def answer(request, decision='apply', reason='确指该产品'):
    prompt = json.loads(request.content)['messages'][0]['content']
    hint = prompt.split('不完整）：', 1)[1].split('\n', 1)[0]
    fragment = prompt.split('---\n', 1)[1]
    matches = list(re.finditer(re.escape(hint), fragment, re.I)) if hint != '无' else []
    return response([{'span': m.group(), 'reason': reason} for m in matches] if decision == 'apply' else [])


def clear(store):
    with store._connect() as c:
        c.execute('DELETE FROM recording_hotword_decisions')
        c.execute('DELETE FROM recording_hotword_checks')


def test_context_limit_and_literal_case():
    text = '左' * 60 + 'aI' + '右' * 60
    assert contexts(text, 'AI')[0] == {'start': 0, 'text': text}
    chunks = contexts('a.b a.b a.b a.b axb', 'a.b')
    assert all(c['text'].count('a.b') <= 1 for c in chunks)
    assert len(contexts('文本' * 1000, '标准词')) == 5
    assert '不确定' in SYSTEM and '没有别名也要识别' in SYSTEM


@pytest.mark.parametrize('path', EXITS)
def test_apply_skip_and_missing_compare_same_raw_text(populated, store, path):
    before = recording_rows(store)
    assert 'AI导师' in populated.get(path).text
    with store._connect() as c:
        c.execute('UPDATE recording_hotword_decisions SET revoked=1')
    assert BODY in populated.get(path).text
    clear(store)
    result = populated.get(path)
    assert BODY in result.text
    if path.startswith('/recordings/') and '.md' not in path:
        assert '本条的用词尚未经过热词判断' in result.text
    assert recording_rows(store) == before and store.apply_hotwords(BODY) == BODY


def test_one_request_packs_fields_rules_contexts_and_disabled_excluded(populated, store):
    clear(store)
    store.add_hotword('别的词', '不存在')
    store.add_hotword('不要发送', '禁用词')
    disabled = next(r for r in store.list_hotwords() if r.term == '不要发送')
    store.toggle_hotword(disabled.id)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return answer(request)
    before = recording_rows(store)
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(handler))
    assert result['apply'] == 10 and result['skip'] == 2 and not result['request_failed']
    assert all(r['system'] == SYSTEM for r in requests)
    assert all('不要发送' not in r['messages'][0]['content'] for r in requests)
    assert len(store.hotword_checks('hotword')) == 6
    assert not any(d['hotword_id'] == disabled.id for d in store.hotword_decisions('hotword'))
    assert recording_rows(store) == before


@pytest.mark.parametrize('failure', ['invalid-json', 'timeout', 'http', 'missing', 'bad-reason', 'unconfigured'])
def test_failures_persist_skip_and_page_survives(populated, store, failure):
    clear(store)
    def handler(request):
        if failure == 'timeout': raise httpx.ReadTimeout('do not persist provider text')
        if failure == 'http': return httpx.Response(500, text='do not persist provider text')
        if failure == 'missing': return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{}'}]})
        if failure == 'bad-reason': return answer(request, reason='长' * 101)
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'not json'}]})
    result, = run_judgments(store, settings=TagSettings('') if failure == 'unconfigured' else SETTINGS,
                            transport=httpx.MockTransport(handler))
    assert result['skip'] == result['judge_failed'] == 4
    assert result['apply'] == 0 and result['saved'] and store.hotword_decisions('hotword') == []
    assert BODY in populated.get('/recordings/hotword').text
    assert 'do not persist provider text' not in str(store.hotword_checks('hotword'))


ITEM = {'hotword_id': 1, 'field': 'summary', 'term': '小鹿直播', 'text': '小鹅直播'}
CONTEXT = {'start': 0, 'text': ITEM['text']}


@pytest.mark.parametrize('text', ['null', '[]', '{}', '{"replacements":null}',
    '{"replacements":[],"extra":1}', '{"replacements":[],"replacements":[]}',
    '{"replacements":[{"span":true,"reason":"ok"}]}', '```json\n{"replacements":[]}\n```'])
def test_strict_json_rejects_invalid_shape(text):
    with pytest.raises(ValueError): parse_decisions(text, ITEM, CONTEXT)


@pytest.mark.parametrize('mutate', ['duplicate', 'unknown', 'long', 'extra', 'bad-field', 'bad-decision', 'control'])
def test_strict_json_rejects_invalid_items(mutate):
    row = {'span': '小鹅直播', 'reason': '确指产品'}
    if mutate == 'unknown': row['span'] = 2
    if mutate == 'long': row['reason'] = '字' * 101
    if mutate == 'extra': row['extra'] = True
    if mutate == 'bad-field': row['field'] = 'title'
    if mutate == 'bad-decision': row['reason'] = True
    if mutate == 'control': row['reason'] = '\n'
    with pytest.raises(ValueError):
        parse_decisions(json.dumps({'replacements': [row] * (2 if mutate == 'duplicate' else 1)}), ITEM, CONTEXT)


def token(client):
    page = client.get('/recordings/hotword')
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.text)[1]


def revoke(store, hotword_id, field='summary'):
    row = next(d for d in store.hotword_decisions('hotword') if d['hotword_id'] == hotword_id and d['field'] == field)
    return store.revoke_hotword('hotword', hotword_id, field, row['id'])


def test_revoke_immediate_restore_and_counts_and_idempotent_post(populated, store):
    before = recording_rows(store)
    word = next(r for r in store.list_hotwords() if r.term == 'AI导师')
    assert word.applied_records == 1
    row = next(d for d in store.hotword_decisions('hotword') if d['hotword_id'] == word.id and d['field'] == 'summary')
    path = f'/recordings/hotword/hotwords/{word.id}/revoke'
    data = {'csrf_token': token(populated), 'field': 'summary', 'view': 'summary', 'decision_id': row['id']}
    assert populated.post(path, data=data).status_code == 303
    assert next(d for d in store.hotword_decisions('hotword') if d['id'] == row['id'])['revoked'] == 1
    assert populated.post(path, data=data).status_code == 303
    assert len(store.hotword_decisions('hotword')) == 10
    assert 'AI导师' in populated.get('/recordings/hotword?view=transcript').text
    assert recording_rows(store) == before


@pytest.mark.parametrize('kind', ['anonymous', 'member', 'csrf', 'field'])
def test_revoke_authorization_and_csrf(populated, store, settings, kind):
    word = store.list_hotwords()[0]
    row = store.hotword_decisions('hotword')[0]
    data = {'csrf_token': token(populated), 'field': 'summary', 'decision_id': row['id']}
    if kind == 'anonymous': populated.cookies.delete('session')
    if kind == 'member': store.remove_user(settings.admin_open_id)
    if kind == 'csrf': data['csrf_token'] = 'invalid'
    if kind == 'field': data['field'] = 'title'
    before = store.hotword_decisions('hotword')
    assert populated.post(f'/recordings/hotword/hotwords/{word.id}/revoke', data=data).status_code == (400 if kind == 'field' else 403)
    assert store.hotword_decisions('hotword') == before


def test_repeated_judgment_upserts_and_all_preserves_manual_revoke(populated, store):
    transport = httpx.MockTransport(lambda r: answer(r, decision='skip'))
    run_judgments(store, settings=SETTINGS, transport=transport, force=True)
    run_judgments(store, settings=SETTINGS, transport=transport, force=True)
    assert store.hotword_decisions('hotword') == []
    assert {d['status'] for d in store.hotword_checks('hotword')} == {'skip'}
    approve_rules(store)
    word = store.list_hotwords()[0]
    revoke(store, word.id)
    before = store.hotword_decisions('hotword')
    run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(lambda r: pytest.fail('should not call')))
    assert store.hotword_decisions('hotword') == before
    run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(answer), force=True)
    assert any(d['revoked'] for d in store.hotword_decisions('hotword'))
    with store._connect() as c:
        assert c.execute('SELECT count(*) FROM recording_hotword_decision_history').fetchone()[0] >= 10
        assert c.execute('SELECT count(*) FROM recording_hotword_check_history').fetchone()[0] >= 4


@pytest.mark.parametrize('change', ['content', 'rule', 'revoke'])
def test_in_flight_stale_judgment_does_not_overwrite_content_rules_or_revoke(populated, store, change):
    changed = False
    def handler(request):
        nonlocal changed
        if not changed:
            changed = True
            if change == 'content':
                r = store.get_recording('hotword')
                store.upsert_recording(RecordingMeta(r.recording_id, r.title, r.created_at, r.duration_min), RecordingDetail('hotword', '新文本', BODY, True, True), 'later')
            elif change == 'rule':
                word = store.list_hotwords()[0]
                store.update_hotword(word.id, '不同事物', word.aliases, '')
            else: revoke(store, store.list_hotwords()[0].id)
        return answer(request)
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(handler), force=True)
    assert not result['saved']
    if change == 'content': assert store.hotword_decisions('hotword') == []
    if change == 'revoke': assert any(d['revoked'] for d in store.hotword_decisions('hotword'))


def test_content_change_invalidates_but_unchanged_sync_preserves(populated, store):
    r = store.get_recording('hotword')
    before = store.hotword_decisions('hotword')
    detail = RecordingDetail('hotword', BODY, BODY, True, True)
    assert store.upsert_recording(RecordingMeta(r.recording_id, r.title, r.created_at, r.duration_min), detail, 'later') == 'unchanged'
    assert store.hotword_decisions('hotword') == before
    store.upsert_recording(RecordingMeta(r.recording_id, r.title, r.created_at, r.duration_min), replace(detail, summary_md='新内容'), 'later')
    assert store.hotword_decisions('hotword') == [] and store.hotword_checks('hotword') == []
    assert '本条的用词尚未经过热词判断' in populated.get('/recordings/hotword').text


def test_sync_judges_only_new_or_changed_content(populated, store, monkeypatch):
    r = store.get_recording('hotword')
    class Source:
        body = BODY
        def list_by_date(self, day): return [RecordingMeta(r.recording_id, r.title, r.created_at, r.duration_min)]
        def get_detail(self, rid): return RecordingDetail(rid, self.body, BODY, True, True)
    calls = []
    monkeypatch.setattr('app.hotword_judge.run_judgments', lambda store, **kw: calls.append(kw))
    source = Source()
    assert run_sync(source, store, backfill_days=1).status == 'success' and calls == []
    source.body = '内容变化'
    assert run_sync(source, store, backfill_days=1).status == 'success'
    assert calls == [{'recording_ids': ['hotword']}] and store.hotword_decisions('hotword') == []


def test_cli_requires_database_and_all_flag(monkeypatch):
    monkeypatch.delenv('DB_PATH', raising=False)
    with pytest.raises(SystemExit): main(['--all'])
    with pytest.raises(SystemExit): main([])


def test_no_occurrence_avoids_model_and_migration_preserves_decisions(populated, store):
    clear(store)
    for w in store.list_hotwords(): store.delete_hotword(w.id)
    store.add_hotword('从未出现')  # No alias is still judged, not gated on literal occurrence.
    calls = []
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(lambda r: calls.append(r) or response([])))
    assert result['skip'] == 2 and not result['request_failed'] and calls
    before = store.hotword_checks('hotword')
    store.migrate()
    assert store.hotword_checks('hotword') == before
    assert {d['reason'] for d in before} == {'no_replacement'}


def test_deadline_timeout_is_skip(populated, store, monkeypatch):
    import asyncio
    clear(store)
    monkeypatch.setattr('app.hotword_judge.ITEM_TIMEOUT', 0.001)
    async def delayed(*args):
        await asyncio.sleep(0.05)
        pytest.fail('deadline did not cancel request')
    monkeypatch.setattr('app.hotword_judge.request_decisions', delayed)
    result, = run_judgments(store, settings=SETTINGS)
    assert result['judge_failed'] == 4 and result['apply'] == 0


def test_cli_all_outputs_distribution(populated, store, settings, monkeypatch, capsys):
    monkeypatch.setenv('DB_PATH', str(settings.db_path))
    assert main(['--all']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['records'][0]['apply'] == 10 and payload['records'][0]['skip'] == 0


def test_trace_counts_actual_replacements_and_escapes_reason(populated, store):
    state = store.hotword_presentation('hotword')
    assert state['count'] == 6 and not state['pending']
    with store._connect() as c: c.execute("UPDATE recording_hotword_decisions SET reason='<script>bad</script>'")
    page = populated.get('/recordings/hotword')
    assert '<script>bad</script>' not in page.text and '&lt;script&gt;bad&lt;/script&gt;' in page.text
    store.save_override('hotword', 'summary', '手动正文')
    assert store.hotword_presentation('hotword')['count'] == 3


def test_rule_change_invalidates_approval_and_delete_cleans_rows(populated, store):
    word = store.list_hotwords()[0]
    assert store.update_hotword(word.id, '新指代', word.aliases, '')
    assert all(d['hotword_id'] != word.id for d in store.hotword_decisions('hotword'))
    assert '新指代' not in populated.get('/recordings/hotword').text
    approve_rules(store)
    store.delete_hotword(word.id)
    assert all(d['hotword_id'] != word.id for d in store.hotword_decisions('hotword'))
    with store._connect() as c:
        assert c.execute('SELECT count(*) FROM recording_hotword_decision_history WHERE hotword_id=?', (word.id,)).fetchone()[0]


def test_unknown_missing_and_extra_batch_decisions_fail_all():
    assert parse_decisions('{"replacements":[]}', ITEM, CONTEXT) == ([], False)
    rows, failed = parse_decisions('{"replacements":[{"span":"原文没有","reason":"猜测"}]}', ITEM, CONTEXT)
    assert rows == [] and failed
    with pytest.raises(ValueError): parse_decisions('{"replacements":[],"hotword_id":2}', ITEM, CONTEXT)
