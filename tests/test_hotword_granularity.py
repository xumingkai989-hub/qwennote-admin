"""Word-sized suggestions and ingestion, legacy flags and offline judge retry."""
import json

import httpx
import pytest

from app.hotword_judge import run_judgments
from app.hotwords import validate_hotword
from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.recording_edit import diff_suggestions
from app.store import SQLiteStore
from datetime import datetime
from fastapi.testclient import TestClient
from test_hotword_judge import SETTINGS, response
from test_hotwords import post, csrf
from test_login import login
from test_recording_edit import editing, token


@pytest.mark.parametrize('old,new,expected', [
    ('本次会议中星河直播的转化率', '本次会议中星火直播的转化率', [{'term': '星火直播'}]),
    ('讨论：星河直播，今天上线', '讨论：星火直播，今天上线', [{'term': '星火直播'}]),
    ('原文', '原文新增文字', []),
    ('AI Tutor', 'ai tutor', []),
    ('AI Tutor', 'AI  Tutor', []),
    ('星 河直播', '星河直播', []),
    ('甲' * 13, '乙' * 13, []),
    ('A' * 25, 'B' * 25, []),
    ('「甲词」', '「乙词」', [{'term': '乙词'}]),
    ('原词\n丢弃', '新词\n', [{'term': '新词'}]),
])
def test_word_diff(old, new, expected):
    assert diff_suggestions(old, new) == expected


@pytest.mark.parametrize('term', ['甲，乙', '甲。乙', '甲！乙', '甲？乙', '甲；乙', '甲：乙', '甲、乙',
                                  '甲\n乙', '甲  乙', '<标签>', '甲' * 21, ''])
def test_term_rejected_at_server(client, store, term):
    login(client)
    result = post(client, term=term)
    assert result.status_code == 400
    assert 'hotword-field-error' in result.text and 'aria-invalid="true"' in result.text
    assert store.list_hotwords() == []


@pytest.mark.parametrize('term', ['甲' * 20, 'A' * 20, '词', 'AI Tutor'])
def test_term_boundaries_pass(client, store, term):
    login(client)
    assert post(client, term=term).status_code == 303
    assert store.list_hotwords()[0].term == term


@pytest.mark.parametrize('alias', ['甲。乙', '甲\n乙', '甲  乙', '<标签>', '字' * 21])
def test_obsolete_field_is_ignored_without_persisting(client, store, alias):
    login(client)
    result = post(client, term='标准词', aliases='好词,' + alias + ',另一个词')
    assert result.status_code == 303
    assert not hasattr(store.list_hotwords()[0], 'aliases')


def test_bulk_rejects_invalid_term_atomically(editing, store):
    result = editing.post('/hotwords/bulk', data={'csrf_token': token(editing), 'items': json.dumps([
        {'term': '标准词'}, {'term': '整句，词'}])})
    assert result.status_code == 400 and '标点' in result.json()['detail']
    assert store.list_hotwords() == []


def test_bulk_stores_only_standard_term(editing, store):
    result = editing.post('/hotwords/bulk', data={'csrf_token': token(editing), 'items': json.dumps([
        {'term': '标准词'}])})
    assert result.status_code == 200
    assert result.json()['added'] == 1
    assert not hasattr(store.list_hotwords()[0], 'aliases')


@pytest.mark.parametrize('term', ['整句话，包含标点。', '甲' * 21])
def test_startup_flags_legacy_without_rewriting(settings, fake_auth, store, term):
    # Ticket 17 schema, before needs_review exists; startup must add/scan it.
    settings.db_path.parent.mkdir(parents=True)
    import sqlite3
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('CREATE TABLE hotwords(id INTEGER PRIMARY KEY,term TEXT UNIQUE,aliases TEXT,note TEXT,enabled INTEGER,hits INTEGER,created_at TEXT,updated_at TEXT)')
        conn.execute("INSERT INTO hotwords VALUES (7,?,'旧别名','原备注',1,17,'old','old')", (term,))
    with TestClient(create_app(settings=settings, store=store, feishu_auth=fake_auth), follow_redirects=False) as client:
        word, = store.list_hotwords()
        assert word.needs_review == 1 and (word.term, word.note, word.hits) == (term, '原备注', 17)
        login(client)
        page = client.get('/hotwords').text
        assert '这条像是整句，建议改成词语本身' in page and '/hotwords/7/update' in page
        store.migrate()
        assert store.list_hotwords() == [word]
        assert post(client, '/hotwords/7/update', term='短词', aliases='旧别名', note='原备注').status_code == 303
        assert store.list_hotwords()[0].needs_review == 0


@pytest.mark.parametrize('bad_span', ['小鹅，直播', '小鹅。直播', '长' * 13, '小鹅(直播)'])
@pytest.mark.parametrize('retry_valid', [False, True])
def test_judge_invalid_span_retry_once(store, bad_span, retry_valid):
    store.migrate()
    store.add_hotword('标准词')
    text = bad_span + ' 星河直播'
    store.upsert_recording(RecordingMeta('word', '词粒度', datetime.now(SHANGHAI), 1),
                          RecordingDetail('word', text, '', False, True), 'now')
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        span = '星河直播' if retry_valid and len(requests) == 2 else bad_span
        return response([{'span': span, 'reason': '产品'}])
    before = store.get_recording('word')
    result, = run_judgments(store, settings=SETTINGS, transport=httpx.MockTransport(handler))
    assert len(requests) == 2 and '上次结果不合法' in requests[1]['system']
    rows = store.hotword_decisions('word')
    if retry_valid:
        assert len(rows) == 1 and rows[0]['span'] == '星河直播' and not result['judge_failed']
    else:
        assert rows == [] and result['judge_failed'] == 1
        assert any(c['reason'] == 'judge_failed' for c in store.hotword_checks('word'))
    assert store.get_recording('word') == before
