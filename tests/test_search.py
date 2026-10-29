import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.minutes import RecordingDetail, RecordingMeta
from test_list_page import Page
from test_login import login


@pytest.fixture
def search_client(client):
    assert login(client).status_code == 302
    return client


@pytest.fixture
def add_search_recording(store):
    def add(recording_id, *, title='例会', summary='', transcript='',
            created='2026-10-29T09:30:00+08:00'):
        store.upsert_recording(
            RecordingMeta(recording_id, title, datetime.fromisoformat(created), 18),
            RecordingDetail(recording_id, summary, transcript, bool(transcript), bool(summary)),
            '2026-10-29T12:00:00+08:00',
        )
    return add


@pytest.mark.parametrize('query', ['数据看板', '看板', '看', 'ROADMAP', '12345', '12', 'R'])
def test_literal_search_finds_chinese_english_numbers_across_all_dates(
        search_client, add_search_recording, query):
    add_search_recording('older', transcript='数据看板 Roadmap 12345', created='2026-10-01T12:00:00+08:00')
    add_search_recording('newer', summary='数据看板 Roadmap 12345')
    add_search_recording('miss', title='其他事项')
    response = search_client.get('/search', params={'q': query})
    assert response.status_code == 200
    assert Page(response.text).cards == ['newer', 'older']
    assert f'搜索「{query}」，共 2 条' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert '<mark>' in response.text


@pytest.mark.parametrize('fields,labels', [
    ({'title': '数据看板'}, ['标题命中']),
    ({'summary': '数据看板'}, ['总结命中']),
    ({'transcript': '数据看板'}, ['转写命中']),
    ({'title': '数据看板', 'summary': '数据看板', 'transcript': '数据看板'},
     ['标题命中', '总结命中', '转写命中']),
])
def test_search_labels_only_the_matching_fields(search_client, add_search_recording, fields, labels):
    add_search_recording('match', **fields)
    html = search_client.get('/search', params={'q': '数据看板'}).text
    assert Page(html).cards == ['match']  # Multiple matching fields never duplicate a card.
    for label in ['标题命中', '总结命中', '转写命中']:
        assert (label in html) == (label in labels)


@pytest.mark.parametrize('query', ['看板', 'script', '<script>', '" OR ', '%', '_', '\\', '"', "' OR 1=1 --"])
def test_query_is_literal_and_highlight_escapes_source_and_query(search_client, add_search_recording, query):
    source = '<script>x</script> 看板 " OR  % _ \\ \' OR 1=1 --'
    add_search_recording('match', transcript=source)
    add_search_recording('miss', transcript='安全的无关内容')
    html = search_client.get('/search', params={'q': query}).text
    assert Page(html).cards == ['match']
    assert '<script>x</script>' not in html
    assert '<mark><script>' not in html
    assert '<mark>' in html
    if query == '看板':
        assert '&lt;script&gt;' in html and '&lt;/script&gt; <mark>看板</mark>' in html
    if query == '<script>':
        assert '<mark>&lt;script&gt;</mark>' in html


def test_snippet_has_twenty_characters_of_context_and_is_bounded(search_client, add_search_recording):
    add_search_recording('match', summary='前' * 1000 + '数据看板' + '后' * 1000)
    html = search_client.get('/search', params={'q': '数据看板'}).text
    assert '前' * 20 + '<mark>数据看板</mark>' + '后' * 20 in html
    assert '前' * 21 not in html and '后' * 21 not in html


@pytest.mark.parametrize('query', [None, '', '   '])
def test_empty_search_redirects_to_list(search_client, query):
    response = search_client.get('/search', params={} if query is None else {'q': query})
    assert response.status_code == 302 and response.headers['location'] == '/'


def test_no_results_message_and_query_remains_in_input(search_client):
    html = search_client.get('/search', params={'q': '不存在的关键词'}).text
    assert '没有找到包含「不存在的关键词」的录音' in html
    assert '换个关键词' in html
    assert Page(html).cards == []
    assert any(i.get('name') == 'q' and i.get('value') == '不存在的关键词' for i in Page(html).inputs)


@pytest.mark.parametrize('query', ['看板', '数据看板'])
def test_pagination_date_range_and_detail_return_preserve_search(search_client, add_search_recording, query):
    for i in range(25):
        add_search_recording(f'match-{i:02}', title='数据看板', created='2026-10-28T23:59:59.999999+08:00')
    add_search_recording('outside', title='数据看板', created='2026-10-29T00:00:00+08:00')
    add_search_recording('start', title='数据看板', created='2026-10-27T00:00:00+08:00')
    params = {'q': query, 'from': '2026-10-27', 'to': '2026-10-28'}
    first = search_client.get('/search', params=params)
    page1 = Page(first.text)
    assert page1.cards == [f'match-{i:02}' for i in range(24, 4, -1)]
    next_link, = [a for a in page1.links if a.get('rel') == 'next']
    assert parse_qs(urlsplit(next_link['href']).query) == {**{k: [v] for k, v in params.items()}, 'page': ['2']}
    second = search_client.get(next_link['href'])
    page2 = Page(second.text)
    assert page2.cards == [f'match-{i:02}' for i in range(4, -1, -1)] + ['start']
    assert '共 26 条' in second.text
    previous, = [a for a in page2.links if a.get('rel') == 'prev']
    assert search_client.get(previous['href']).text == first.text
    detail_link = next(a['href'] for a in page2.links if a.get('href', '').startswith('/recordings/'))
    detail_html = search_client.get(detail_link).text
    back = next(a['href'] for a in Page(detail_html).links if a.get('href', '').startswith('/search?'))
    assert search_client.get(back).text == second.text
    assert Page(search_client.get('/search', params={**params, 'page': '999'}).text).cards == page2.cards


@pytest.mark.parametrize('params', [
    {'from': '2026-10-29'}, {'to': '2026-10-29'},
    {'from': '2026-10-29', 'to': '2026-10-28'}, {'page': '0'}, {'page': 'bad'},
])
def test_search_reuses_filter_validation(search_client, params):
    response = search_client.get('/search', params={'q': '看板', **params})
    assert response.status_code == 400 and '筛选条件无效' in response.text


def test_search_requires_active_whitelist(search_client, settings):
    search_client.cookies.clear()
    assert search_client.get('/search?q=看板').status_code == 401
    login(search_client)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('UPDATE users SET is_active = 0')
    assert search_client.get('/search?q=看板').status_code == 403


def test_insert_update_delete_and_rebuild_keep_search_consistent(search_client, add_search_recording, settings):
    add_search_recording('match', title='数据看板', summary='模考')
    for q in ['数据看板', '模考']:
        assert Page(search_client.get('/search', params={'q': q}).text).cards == ['match']
    add_search_recording('match', title='师资讨论', transcript='数字12345')
    for q in ['数据看板', '模考']:
        assert Page(search_client.get('/search', params={'q': q}).text).cards == []
    before = [search_client.get('/search', params={'q': q}).text for q in ['师资', '12345']]
    with sqlite3.connect(settings.db_path) as conn:
        # Rebuild must recover from stale/empty derived text, using source fields.
        conn.execute("UPDATE recordings SET search_text = ''")
    assert Page(search_client.get('/search?q=12345').text).cards == []
    for _ in range(2):
        result = subprocess.run([sys.executable, '-m', 'app.rebuild_search'],
                                env={**os.environ, 'DB_PATH': str(settings.db_path)},
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert [search_client.get('/search', params={'q': q}).text for q in ['师资', '12345']] == before
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM recordings WHERE recording_id = 'match'")
        assert conn.execute("SELECT rowid FROM recordings_fts WHERE recordings_fts MATCH '12345'").fetchall() == []
    assert Page(search_client.get('/search?q=12345').text).cards == []


def test_startup_migrates_existing_work_order_04_database(settings, fake_auth):
    settings.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('''CREATE TABLE recordings (
            recording_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            duration_min INTEGER, summary_md TEXT, transcript_md TEXT,
            has_transcript INTEGER NOT NULL DEFAULT 0, has_summary INTEGER NOT NULL DEFAULT 0,
            owner_open_id TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL)''')
        conn.execute("INSERT INTO recordings VALUES ('legacy', '数据看板', '2026-10-01T00:00:00+08:00', NULL, NULL, '模考', 1, 0, 'owner', 'old')")
    for _ in range(2):
        with TestClient(create_app(settings=settings, feishu_auth=fake_auth), follow_redirects=False) as client:
            login(client)
            for query in ['数据看板', '模考']:
                assert Page(client.get('/search', params={'q': query}).text).cards == ['legacy']
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute('SELECT search_text, owner_open_id FROM recordings').fetchone() == ('数据看板\n\n模考', 'owner')
    # Losing only the virtual table is repaired on the next startup too.
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('DROP TABLE recordings_fts')
    with TestClient(create_app(settings=settings, feishu_auth=fake_auth), follow_redirects=False) as client:
        login(client)
        assert Page(client.get('/search?q=数据看板').text).cards == ['legacy']


def test_rebuild_command_requires_explicit_database(monkeypatch):
    monkeypatch.delenv('DB_PATH', raising=False)
    result = subprocess.run([sys.executable, '-m', 'app.rebuild_search'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'DB_PATH is required' in result.stderr
