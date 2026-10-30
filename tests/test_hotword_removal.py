"""Ticket 20 migration destroys obsolete values without changing original content."""
import json
import sqlite3

import httpx
import pytest

from app.hotword_judge import request_decisions
from test_hotword_judge import SETTINGS, response


@pytest.mark.parametrize('review', [False, True])
def test_alias_column_and_content_removed_everywhere_and_sync_preserved(store, review):
    store.db_path.parent.mkdir(parents=True)
    sentinel = 'DISCARD_OBSOLETE_7d830f'
    with store._connect() as conn:
        conn.execute("CREATE TABLE hotwords(id INTEGER PRIMARY KEY,term TEXT UNIQUE,aliases TEXT,note TEXT,enabled INTEGER,hits INTEGER,created_at TEXT,updated_at TEXT" + (',needs_review INTEGER' if review else '') + ')')
        conn.execute('INSERT INTO hotwords VALUES (7,?,?,?,?,?,?,?' + (',?' if review else '') + ')',
                     ('标准词',sentinel,'备注',1,19,'created','updated', *([1] if review else [])))
        conn.execute('CREATE TABLE hotword_upstream_sync(hotword_id INTEGER PRIMARY KEY, upstream_value TEXT,synced_at TEXT,status TEXT)')
        conn.execute("INSERT INTO hotword_upstream_sync VALUES (7,'标准词','yesterday','ok')")
        conn.execute('CREATE TABLE recording_hotword_legacy_decisions(id INTEGER PRIMARY KEY, source TEXT, target TEXT)')
        conn.execute('INSERT INTO recording_hotword_legacy_decisions VALUES (1,?,?)',(sentinel,'标准词'))
    store.migrate()
    with store._connect() as conn:
        assert {r['name'] for r in conn.execute('PRAGMA table_info(hotwords)')} == {
            'id','term','note','enabled','hits','created_at','updated_at','needs_review'}
        assert tuple(conn.execute('SELECT * FROM hotwords').fetchone()) == (7,'标准词','备注',1,19,'created','updated',int(review))
        assert tuple(conn.execute('SELECT * FROM hotword_upstream_sync').fetchone()) == (7,'标准词','yesterday','ok')
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            name = row['name'].replace('"','""')
            assert sentinel not in str([tuple(r) for r in conn.execute(f'SELECT * FROM "{name}"')])
    store.migrate()
    assert not hasattr(store.list_hotwords()[0],'aliases')


def test_alias_rebuild_failure_rolls_back_original_table_and_values(store, monkeypatch):
    store.db_path.parent.mkdir(parents=True)
    with store._connect() as conn:
        conn.execute('CREATE TABLE hotwords(id INTEGER PRIMARY KEY,term TEXT UNIQUE,aliases TEXT,note TEXT,enabled INTEGER,hits INTEGER,created_at TEXT,updated_at TEXT)')
        conn.execute("INSERT INTO hotwords VALUES(7,'标准词','rollback-value','备注',1,17,'old','old')")
    import app.hotword_migration as migration
    original = migration.migrate_hotwords
    def fail(conn):
        original(conn)
        assert 'aliases' not in migration.columns(conn,'hotwords')
        raise sqlite3.OperationalError('injected failure')
    monkeypatch.setattr(migration,'migrate_hotwords',fail)
    with pytest.raises(sqlite3.OperationalError): store.migrate()
    with store._connect() as conn:
        assert conn.execute('SELECT aliases FROM hotwords').fetchone()[0] == 'rollback-value'
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='hotwords_clean'").fetchone()


@pytest.mark.parametrize('term,text', [('小鹿直播','产品使用小鹅直播'),('AI Tutor','这款助教用于课堂')])
def test_actual_assembled_prompt_only_term_and_context(term,text):
    import asyncio
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return response([])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await request_decisions(client, SETTINGS, {'term':term,'hotword_id':1,'field':'summary',
                'text':text,'contexts':[{'start':0,'text':text}]})
    asyncio.run(run())
    payload, = sent
    prompt = payload['messages'][0]['content']
    assert term in prompt and text in prompt
    assert '可能的其他写法' not in prompt and 'aliases' not in prompt and '别名' not in payload['system']
    assert '自行识别' in payload['system']
