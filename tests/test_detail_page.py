import json
import re
import sqlite3
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from test_login import login


TRANSCRIPT = ('[00:00:00.360–00:00:26.760] 发言人 1\n\n'
              '第一段正文。\n\n[00:00:26.760–00:00:40.000] 发言人 2\n\n第二段正文。\n\n补充一段。')
SUMMARY = '# 会议总结\n\n- **行动事项**\n\n> 引用结论\n\n```python\nprint("ok")\n```'


@pytest.fixture
def detail_client(client, store):
    assert login(client).status_code == 302
    def add(*, title='项目例会', summary=SUMMARY, transcript=TRANSCRIPT, duration=12, recording_id='detail'):
        store.upsert_recording(
            RecordingMeta(recording_id, title, datetime(2026, 10, 29, 9, 30, tzinfo=SHANGHAI), duration),
            RecordingDetail(recording_id, summary, transcript, bool(transcript), bool(summary)),
            '2026-10-29T12:00:00+08:00',
        )
    client.add = add
    add()
    return client


def test_detail_markdown_and_metadata(detail_client):
    response = detail_client.get('/recordings/detail')
    assert response.status_code == 200
    html = response.text
    for text in ['项目例会', '2026-10-29 09:30', '12 分钟', '已转录', '时间为笔记创建时间',
                 '<h1>会议总结</h1>', '<ul>', '<strong>行动事项</strong>', '<blockquote>', '<pre><code',
                 '第一段正文。', '第二段正文。', '复制 Markdown', '下载 .md']:
        assert text in html
    assert response.headers['cache-control'] == 'no-store'
    assert '/recordings/detail.md' in html


def test_transcript_preserves_segments_and_paragraphs(detail_client):
    html = detail_client.get('/recordings/detail').text
    assert re.search(r'<[^>]*class="timestamp"[^>]*>\[00:00:00.360–00:00:26.760\]</', html)
    assert re.search(r'<strong[^>]*>发言人 1</strong>', html)
    paragraphs = re.findall(r'<p class="transcript-paragraph">(.*?)</p>', html, re.S)
    assert paragraphs == ['第一段正文。', '第二段正文。', '补充一段。']
    assert html.count('class="transcript-segment"') == 2


@pytest.mark.parametrize('payload', [
    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
    '</textarea><script>alert(2)</script>',
])
def test_user_html_is_escaped_everywhere(detail_client, payload):
    detail_client.add(title=payload, summary=payload, transcript=TRANSCRIPT + '\n\n' + payload)
    html = detail_client.get('/recordings/detail').text
    assert payload not in html
    assert '&lt;script&gt;' in html or '&lt;img' in html
    assert unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>', html, re.S)[1]).endswith(payload + '\n')


def test_dangerous_markdown_links_are_not_executable(detail_client):
    detail_client.add(summary='[点击](javascript:alert%281%29)\n\n![图片](data:text/html;base64,abc)')
    html = detail_client.get('/recordings/detail').text
    assert 'href="javascript:' not in html and 'src="data:' not in html


@pytest.mark.parametrize('summary,transcript,message,absent', [
    (SUMMARY, '', '该录音未转录', 'class="transcript-segment"'),
    ('', TRANSCRIPT, '该录音暂无总结', 'id="summary-content"'),
    ('', '', '该录音既无转写也无总结', 'class="transcript-segment"'),
])
def test_empty_content_branches(detail_client, summary, transcript, message, absent):
    detail_client.add(summary=summary, transcript=transcript)
    html = detail_client.get('/recordings/detail').text
    assert message in html and absent not in html


@pytest.mark.parametrize('suffix', ['', '.md'])
def test_unknown_id_has_html_404(detail_client, suffix):
    response = detail_client.get('/recordings/missing' + suffix)
    assert response.status_code == 404
    assert 'text/html' in response.headers['content-type']
    assert '录音不存在' in response.text and '<html' in response.text


@pytest.mark.parametrize('suffix', ['', '.md'])
def test_anonymous_cannot_read_recording(client, suffix):
    response = client.get('/recordings/detail' + suffix)
    assert response.status_code in (302, 401)
    assert '第一段正文' not in response.text


@pytest.mark.parametrize('suffix', ['', '.md'])
def test_inactive_user_cannot_read_recording(detail_client, store, settings, suffix):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('UPDATE users SET is_active=0 WHERE open_id=?', (settings.admin_open_id,))
    assert detail_client.get('/recordings/detail' + suffix).status_code in (302, 401)


def frontmatter(text):
    assert text.startswith('---\n')
    header, body = text[4:].split('\n---\n', 1)
    fields = {}
    for line in header.splitlines():
        key, value = line.split(':', 1)
        fields[key] = json.loads(value.strip()) if value.strip() else None
    return fields, body


def test_markdown_export(detail_client):
    response = detail_client.get('/recordings/detail.md')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'text/markdown; charset=utf-8'
    assert response.headers['cache-control'] == 'no-store'
    fields, body = frontmatter(response.text)
    assert fields == dict(recording_id='detail', title='项目例会', created_at='2026-10-29 09:30',
                          duration_min=12, has_transcript=True, has_summary=True)
    assert body == '\n# 项目例会\n\n## 总结\n' + SUMMARY + '\n\n## 转写原文\n' + TRANSCRIPT + '\n'
    assert unquote(response.headers['content-disposition'].split('filename="')[1].split('"')[0]) == '项目例会.md'


@pytest.mark.parametrize('title', ['标题/\\:*?"<>|', '冒号: #注释\n下一行 "引号"', 'true', '', '\r\n恶意标题', 'Unicode\x85换行\u2028分隔\u2029'])
def test_export_quotes_yaml_and_cleans_filename(detail_client, title):
    detail_client.add(title=title, duration=None)
    response = detail_client.get('/recordings/detail.md')
    assert response.status_code == 200
    fields, _ = frontmatter(response.text)
    assert fields['title'] == title and fields['duration_min'] is None
    disposition = response.headers['content-disposition']
    filename = unquote(disposition.split('filename="')[1].split('"')[0])
    assert not re.search(r'[/\\:*?"<>|\x00-\x1f\x7f]', filename)
    assert filename.endswith('.md') and len(filename) > 3
    if title.startswith('标题'):
        assert filename == '标题.md'


def test_export_empty_sections(detail_client):
    detail_client.add(summary='', transcript='', duration=0)
    fields, body = frontmatter(detail_client.get('/recordings/detail.md').text)
    assert fields['has_summary'] is False and fields['has_transcript'] is False
    assert fields['duration_min'] == 0
    assert '## 总结\n（无总结）' in body and '## 转写原文\n（未转录）' in body


def test_copy_has_clipboard_and_selection_fallback(detail_client):
    html = detail_client.get('/recordings/detail').text
    textarea = re.search(r'<textarea[^>]*>(.*?)</textarea>', html, re.S)
    assert textarea and unescape(textarea[1]) == detail_client.get('/recordings/detail.md').text
    js = detail_client.get('/static/detail.js').text
    assert 'navigator.clipboard.writeText' in js and '.select()' in js


def test_list_detail_return_preserves_filter(detail_client):
    response = detail_client.get('/?date=2026-10-29&page=1')
    href = unescape(re.search(r'href="(/recordings/detail[^" ]*)"', response.text)[1])
    detail = detail_client.get(href)
    back = unescape(re.search(r'<a[^>]*id="back-to-list"[^>]*href="([^"]+)"', detail.text)[1])
    assert parse_qs(urlsplit(back).query) == {'date': ['2026-10-29'], 'page': ['1']}


def test_invalid_return_filter_falls_back(detail_client):
    html = detail_client.get('/recordings/detail?date=bad').text
    assert re.search(r'id="back-to-list"[^>]*href="/"', html)


def test_transcript_without_timestamp_keeps_paragraphs(detail_client):
    detail_client.add(transcript='普通转写第一段。\n\n普通转写第二段。')
    html = detail_client.get('/recordings/detail').text
    assert re.findall(r'<p class="transcript-paragraph">(.*?)</p>', html, re.S) == [
        '普通转写第一段。', '普通转写第二段。',
    ]
