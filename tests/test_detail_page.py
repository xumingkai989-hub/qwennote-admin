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
    for text in ['项目例会', '2026-10-29 09:30', '12 分钟', '已转录',
                 '<h1 class="summary-h1">会议总结</h1>', '<ul>', '<strong>行动事项</strong>', '<blockquote>', '<pre><code',
                 '复制 Markdown', '导出总结', '导出转写原文', '导出全部']:
        assert text in html
    assert response.headers['cache-control'] == 'no-store'
    assert '/recordings/detail.md' in html


def test_transcript_preserves_segments_and_paragraphs(detail_client):
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert re.search(r'<[^>]*class="timestamp"[^>]*>00:00</', html)
    assert '<span class="speaker-avatar" aria-hidden="true">发</span>' in html
    assert '<span class="speaker-name">发言人 1</span>' in html
    paragraphs = re.findall(r'<p class="transcript-paragraph">(.*?)</p>', html, re.S)
    assert paragraphs == ['第一段正文。', '第二段正文。', '补充一段。']
    assert html.count('class="transcript-segment"') == 2


@pytest.mark.parametrize('payload', [
    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
    '</textarea><script>alert(2)</script>',
])
@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_user_html_is_escaped_everywhere(detail_client, payload, view):
    detail_client.add(title=payload, summary=payload, transcript=TRANSCRIPT + '\n\n' + payload)
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert payload not in html
    assert '&lt;script&gt;' in html or '&lt;img' in html
    assert '<textarea' not in html
    assert '<pre id="markdown-source"' in html
    assert detail_client.get('/recordings/detail.md').text.endswith(payload + '\n')


def test_dangerous_markdown_links_are_not_executable(detail_client):
    detail_client.add(summary='[点击](javascript:alert%281%29)\n\n![图片](data:text/html;base64,abc)')
    html = detail_client.get('/recordings/detail').text
    assert 'href="javascript:' not in html and 'src="data:' not in html


@pytest.mark.parametrize('summary,transcript,view,message,absent', [
    (SUMMARY, '', 'transcript', '这条录音还没有转写内容', 'class="transcript-segment"'),
    ('', TRANSCRIPT, 'summary', '这条录音还没有 AI 总结', 'id="summary-content"'),
    ('', '', 'summary', '这条录音还没有 AI 总结', 'id="summary-content"'),
    ('', '', 'transcript', '这条录音还没有转写内容', 'class="transcript-segment"'),
])
def test_empty_content_branches(detail_client, summary, transcript, view, message, absent):
    detail_client.add(summary=summary, transcript=transcript)
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert message in html
    if view == "summary":
        assert re.search(r'<div id="summary-content"[^>]*></div>', html)
    else:
        assert absent not in html
    assert re.search(r'class="view-option is-empty" aria-current="page"', html)
    assert 'aria-disabled=' not in html
    assert 'id="transcript-toolbar"' not in html


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
    assert detail_client.get('/recordings/detail' + suffix).status_code == 403


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


@pytest.mark.parametrize('part,suffix', [('summary', '-总结'), ('transcript', '-转写'), ('all', '')])
def test_export_parts(detail_client, part, suffix):
    response = detail_client.get('/recordings/detail.md?part=' + part)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'text/markdown; charset=utf-8'
    assert response.headers['cache-control'] == 'no-store'
    fields, body = frontmatter(response.text)
    assert fields == dict(recording_id='detail', title='项目例会', created_at='2026-10-29 09:30',
                          duration_min=12, has_transcript=True, has_summary=True)
    assert body.startswith('\n# 项目例会\n\n')
    assert ('## 总结\n' in body) == (part != 'transcript')
    assert (SUMMARY in body) == (part != 'transcript')
    assert ('## 转写原文\n' in body) == (part != 'summary')
    assert (TRANSCRIPT in body) == (part != 'summary')
    filename = '项目例会' + suffix + '.md'
    disposition = response.headers['content-disposition']
    assert unquote(disposition.split('filename="')[1].split('"')[0]) == filename
    assert unquote(disposition.split("filename*=UTF-8''")[1]) == filename
    if part == 'all':
        assert response.content == detail_client.get('/recordings/detail.md').content


@pytest.mark.parametrize('part', ['', 'invalid', 'SUMMARY', 'summary/transcript'])
def test_invalid_export_part_falls_back_to_all(detail_client, part):
    response = detail_client.get('/recordings/detail.md', params={'part': part})
    default = detail_client.get('/recordings/detail.md')
    assert response.status_code == 200
    assert response.content == default.content
    assert response.headers['content-disposition'] == default.headers['content-disposition']


@pytest.mark.parametrize('part', ['summary', 'transcript', 'all'])
def test_export_part_only_applies_hotwords_to_exported_content(detail_client, store, part):
    detail_client.add(summary='总结小{origin:1}红直播', transcript='转写小{origin:2}红直播')
    original = store.get_recording('detail')
    store.add_hotword('小红直播', '小鹿直播')
    store.add_hotword('origin', '不应先替换')
    from test_hotwords import approve_rules
    approve_rules(store, 'detail')
    response = detail_client.get('/recordings/detail.md?part=' + part)
    assert 'origin:' not in response.text and '不应先替换' not in response.text
    assert '小红直播' not in response.text
    assert ('总结小鹿直播' in response.text) == (part != 'transcript')
    assert ('转写小鹿直播' in response.text) == (part != 'summary')
    assert {word.source: word.hits for word in store.list_hotwords()} == {
        '小红直播': 2 if part == 'all' else 1, 'origin': 0}
    assert store.get_recording('detail') == original


@pytest.mark.parametrize('part,heading,placeholder', [
    ('summary', '总结', '（无总结）'), ('transcript', '转写原文', '（未转录）')])
def test_empty_export_part(detail_client, part, heading, placeholder):
    detail_client.add(summary='', transcript='', duration=None)
    fields, body = frontmatter(detail_client.get('/recordings/detail.md?part=' + part).text)
    assert fields['duration_min'] is None
    assert body == f'\n# 项目例会\n\n## {heading}\n{placeholder}\n'


@pytest.mark.parametrize('title', ['标题/\\:*?"<>|', '冒号: #注释\n下一行 "引号"', 'true', '', '\r\n恶意标题', 'Unicode\x85换行\u2028分隔\u2029'])
@pytest.mark.parametrize('part,suffix', [('summary', '-总结'), ('transcript', '-转写'), ('all', '')])
def test_export_quotes_yaml_and_cleans_filename(detail_client, title, part, suffix):
    detail_client.add(title=title, duration=None)
    response = detail_client.get('/recordings/detail.md?part=' + part)
    assert response.status_code == 200
    fields, _ = frontmatter(response.text)
    assert fields['title'] == title and fields['duration_min'] is None
    disposition = response.headers['content-disposition']
    filename = unquote(disposition.split('filename="')[1].split('"')[0])
    assert not re.search(r'[/\\:*?"<>|\x00-\x1f\x7f]', filename)
    assert filename.endswith('.md') and len(filename) > 3
    if title.startswith('标题'):
        assert filename == '标题' + suffix + '.md'


def test_export_empty_sections(detail_client):
    detail_client.add(summary='', transcript='', duration=0)
    fields, body = frontmatter(detail_client.get('/recordings/detail.md').text)
    assert fields['has_summary'] is False and fields['has_transcript'] is False
    assert fields['duration_min'] == 0
    assert '## 总结\n（无总结）' in body and '## 转写原文\n（未转录）' in body


def test_copy_has_clipboard_and_selection_fallback(detail_client):
    html = detail_client.get('/recordings/detail').text
    assert '<textarea' not in html
    assert re.search(r'<pre id="markdown-source"[^>]*></pre>', html)
    assert 'data-markdown-url="http://testserver/recordings/detail.md"' in html
    js = detail_client.get('/static/detail.js').text
    assert 'navigator.clipboard.writeText' in js and 'selectNodeContents(source)' in js


def test_list_detail_return_drops_ignored_dates(detail_client):
    response = detail_client.get('/?date=2026-10-29&page=1')
    href = unescape(re.search(r'href="(/recordings/detail[^" ]*)"', response.text)[1])
    detail = detail_client.get(href)
    back = unescape(re.search(r'<a[^>]*id="back-to-list"[^>]*href="([^"]+)"', detail.text)[1])
    assert parse_qs(urlsplit(back).query) == {'page': ['1']}


def test_invalid_return_filter_falls_back(detail_client):
    html = detail_client.get('/recordings/detail?date=bad').text
    assert re.search(r'id="back-to-list"[^>]*href="/"', html)


def test_transcript_without_timestamp_keeps_paragraphs(detail_client):
    detail_client.add(transcript='普通转写第一段。\n\n普通转写第二段。')
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert re.findall(r'<p class="transcript-paragraph">(.*?)</p>', html, re.S) == [
        '普通转写第一段。', '普通转写第二段。',
    ]


@pytest.mark.parametrize('summary,transcript,query,expected', [
    (SUMMARY, TRANSCRIPT, '', 'summary'),
    (SUMMARY, TRANSCRIPT, '?view=summary', 'summary'),
    (SUMMARY, TRANSCRIPT, '?view=transcript', 'transcript'),
    ('', TRANSCRIPT, '', 'transcript'),
    ('', '', '', 'summary'),
    (SUMMARY, '', '', 'summary'),
    (SUMMARY, TRANSCRIPT, '?view=xxx', 'summary'),
    ('', TRANSCRIPT, '?view=xxx', 'transcript'),
    (SUMMARY, TRANSCRIPT, '?view=', 'summary'),
])
def test_only_selected_view_is_rendered(detail_client, summary, transcript, query, expected):
    detail_client.add(summary=summary, transcript=transcript)
    response = detail_client.get('/recordings/detail' + query)
    assert response.status_code == 200
    html = response.text
    assert f'id="{expected}-heading"' in html
    if expected == 'summary':
        assert '第一段正文。' not in html and 'transcript-heading' not in html
    else:
        assert '行动事项' not in html and 'summary-heading' not in html
    assert '复制 Markdown' in html and '导出全部' in html


@pytest.mark.parametrize('view,unused', [('summary', 'transcript_segments'), ('transcript', 'render_summary')])
def test_inactive_content_and_export_are_not_processed(detail_client, monkeypatch, view, unused):
    def forbidden(*args, **kwargs):
        raise AssertionError('Inactive content must not be processed')
    monkeypatch.setattr('app.main.' + unused, forbidden)
    monkeypatch.setattr('app.main.export_markdown', forbidden)
    assert detail_client.get('/recordings/detail?view=' + view).status_code == 200


@pytest.mark.parametrize('filters', ['date=2026-10-29&page=2', 'q=项目&from=2026-10-01&to=2026-10-30&page=3'])
def test_switch_links_preserve_origin_and_replace_view(detail_client, filters):
    html = detail_client.get('/recordings/detail?' + filters + '&view=summary').text
    links = re.findall(r'<a href="([^"]+)" class="view-option[^"]*"', html)
    assert len(links) == 2
    for href, view in zip(links, ['summary', 'transcript']):
        url = unescape(href)
        assert parse_qs(urlsplit(url).query) == {**parse_qs(filters), 'view': [view]}
        switched = detail_client.get(url).text
        back = unescape(re.search(r'id="back-to-list"[^>]*href="([^"]+)"', switched)[1])
        assert parse_qs(urlsplit(back).query) == parse_qs(filters)
        assert urlsplit(back).path == ('/search' if 'q=' in filters else '/')


def test_short_timestamp_format(detail_client):
    detail_client.add(transcript='[00:00.360–00:26.760] 发言人 1\n\n正文')
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert '[00:00.360–00:26.760]' in html
    assert '<span class="speaker-name">发言人 1</span>' in html
    assert re.search(r'class="timestamp"[^>]*>00:00</span>', html)


def test_consecutive_speaker_has_no_repeated_avatar(detail_client):
    detail_client.add(transcript=(
        '[00:00.000–00:26.000] alice\n\n第一段\n\n'
        '[00:26.000–01:20.000] alice\n\n第二段\n\n'
        '[01:20.000–01:40.000] 王小明\n\n第三段\n\n'
        '[01:40.000–02:00.000] alice\n\n第四段'))
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert html.count('class="transcript-segment"') == 4
    assert html.count('class="speaker-avatar"') == 3
    assert html.count('class="speaker-name">alice</span>') == 2
    assert '<span class="speaker-avatar" aria-hidden="true">A</span>' in html
    assert '<span class="speaker-avatar" aria-hidden="true">王</span>' in html
    second = html.split('class="transcript-segment"')[2].split('class="transcript-segment"')[0]
    assert 'speaker-avatar' not in second and 'speaker-name' not in second
    assert '>00:26</span>' in second and '>第二段</p>' in second


@pytest.mark.parametrize('source,expected', [
    ('00:01:20.000', '01:20'), ('01:20.999', '01:20'),
    ('01:00:00.000', '60:00'), ('00:00.360', '00:00'),
])
def test_transcript_short_offset(detail_client, source, expected):
    detail_client.add(transcript=f'[{source}–01:00:26.760] 王小明\n\n正文')
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert re.search(r'class="timestamp"[^>]*>' + expected + '</span>', html)


def test_millisecond_offset():
    from app.recording_content import format_offset
    assert format_offset(80000) == '01:20'


def test_transcript_speaker_is_escaped(detail_client):
    name = '<script>alert("speaker")</script>'
    detail_client.add(transcript=f'[00:00.000–00:26.000] {name}\n\n正文<script>alert(1)</script>')
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert '<script>alert' not in html
    assert '&lt;script&gt;alert(&#34;speaker&#34;)&lt;/script&gt;' in html
    assert '正文&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert 'class="speaker-avatar" aria-hidden="true">&lt;</span>' in html


def test_transcript_toolbar_and_focusable_container(detail_client):
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert '<span class="transcript-count">2 段</span>' in html
    assert '转录全文' not in html
    assert 'id="transcript-search" type="search"' in html
    assert 'id="transcript-segments"' in html
    assert html.count('class="transcript-segment" tabindex="0" role="button"') == 2
    assert 'id="copy-transcript"' in html and 'id="transcript-toast"' in html


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_compact_header_has_single_export_menu(detail_client, view):
    html = detail_client.get('/recordings/detail?view=' + view).text
    header = re.search(r'<header class="detail-header">(.*?)</header>', html, re.S)[1]
    assert re.search(r'id="back-to-list"[^>]*aria-label="返回列表"', header)
    assert '<h1 title="项目例会">项目例会</h1>' in header
    assert 'class="detail-meta"' in header and 'class="badge badge-complete"' in header
    assert re.search(r'id="export-button"[^>]*aria-haspopup="menu"[^>]*aria-expanded="false"', header)
    menu = re.search(r'<div id="export-menu"[^>]*role="menu"[^>]*hidden>(.*?)</div>', header, re.S)[1]
    assert menu.count('role="menuitem"') == 4
    for label in ['复制 Markdown', '导出总结', '导出转写原文', '导出全部']:
        assert menu.count(label) == 1 and html.count(label) == 1
    links = re.findall(r'<a id="download-[^"]+"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', menu)
    assert links == [(f'http://testserver/recordings/detail.md?part={part}', label)
                     for part, label in [('summary', '导出总结'), ('transcript', '导出转写原文'), ('all', '导出全部')]]
    download = re.search(r'<a id="download-markdown"[^>]*>', menu)[0]
    assert 'href="http://testserver/recordings/detail.md?part=all"' in download
    # Preserve the existing link attributes and server Content-Disposition filename.
    assert ' download' not in download
    assert '时间为笔记创建时间' not in html and '非录音开始时间' not in html


def test_detail_return_preserves_date_range(detail_client):
    html = detail_client.get('/recordings/detail?from=2026-10-01&to=2026-10-30').text
    back = unescape(re.search(r'id="back-to-list"[^>]*href="([^"]+)"', html)[1])
    assert urlsplit(back).path == '/'
    assert parse_qs(urlsplit(back).query) == {
        'from': ['2026-10-01'], 'to': ['2026-10-30'], 'page': ['1'],
    }


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_no_segment_count_without_transcript(detail_client, view):
    detail_client.add(transcript='')
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert 'transcript-count' not in html
    assert not re.search(r'\d+ 段', html)


def test_header_full_title_attribute_is_escaped(detail_client):
    title = '很长的录音标题' * 20 + '"><img src=x onerror=alert(1)>'
    detail_client.add(title=title)
    html = detail_client.get('/recordings/detail').text
    heading = re.search(r'<header.*?<h1 title="([^"]*)">(.*?)</h1>', html, re.S)
    assert unescape(heading[1]) == title and unescape(heading[2]) == title
    assert '<img src=x' not in html


def test_speaker_palette_is_stable_and_accessible():
    from app.recording_content import SPEAKER_PALETTE, transcript_presentation

    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(a * b for a, b in zip(channels, (0.2126, 0.7152, 0.0722)))

    def contrast(first, second):
        low, high = sorted((luminance(first), luminance(second)))
        return (high + 0.05) / (low + 0.05)

    assert len(SPEAKER_PALETTE) == 8
    for background, foreground, night in SPEAKER_PALETTE:
        assert contrast(background, foreground) >= 4.5
        assert contrast('#FFFFFF', foreground) >= 4.5
        assert contrast('#1C1C1E', night) >= 4.5
    segments = [{'timestamp': '', 'speaker': name, 'paragraphs': ['正文']} for name in ('alice', '王小明')]
    first = transcript_presentation(segments)
    reversed_result = transcript_presentation(list(reversed(segments)))
    assert first[0]['colors'] == reversed_result[1]['colors']
    assert first[1]['colors'] == reversed_result[0]['colors']


def test_summary_document_metadata_and_disclaimer(detail_client):
    html = detail_client.get('/recordings/detail?view=summary').text
    assert 'summary-metadata' not in html
    assert '<h1 title="项目例会">项目例会</h1>' in html
    metadata = re.search(r'<div class="detail-meta">(.*?)</div>', html, re.S)[1]
    assert '2026-10-29 09:30' in metadata
    assert '12 分钟' in metadata
    assert 'title="GMT+08"' in metadata
    assert '智能纪要由 AI 生成，可能不准确，请谨慎甄别后使用' in html
    assert 'class="detail-section summary-document"' in html


@pytest.mark.parametrize('duration,expected', [(None, '时长未知'), (0, '0 分钟')])
def test_summary_metadata_missing_or_zero_duration(detail_client, duration, expected):
    detail_client.add(duration=duration)
    html = detail_client.get('/recordings/detail').text
    metadata = re.search(r'<div class="detail-meta">(.*?)</div>', html, re.S)[1]
    assert '<span>' + expected + '</span>' in metadata


def test_summary_headings_and_first_h1_separator(detail_client):
    detail_client.add(summary='前言\n\n# 总结\n\n## 讨论\n\n### 细节\n\n# 智能章节')
    html = detail_client.get('/recordings/detail').text
    assert '<h1 class="summary-h1">总结</h1>' in html
    assert '<h2 class="summary-h2">讨论</h2>' in html
    assert '<h3 class="summary-h3">细节</h3>' in html
    assert '<h1 class="summary-h1 summary-h1-separated">智能章节</h1>' in html
    assert html.count('summary-h1-separated') == 1


def test_summary_nested_lists_and_quote(detail_client):
    detail_client.add(summary='- 一级\n  - 二级\n    - 三级\n\n1. 有序\n   1. 子项\n\n> 引用')
    html = detail_client.get('/recordings/detail').text
    assert re.search(r'<ul>\s*<li>一级\s*<ul>\s*<li>二级\s*<ul>\s*<li>三级</li>', html)
    assert re.search(r'<ol>\s*<li>有序\s*<ol>\s*<li>子项</li>', html)
    assert '<blockquote>\n<p>引用</p>\n</blockquote>' in html


def test_summary_tasks_are_read_only_and_export_unchanged(detail_client):
    source = '- [ ] 未完成 **任务**\n- [x] 已完成\n- [X] 也完成'
    detail_client.add(summary=source)
    html = detail_client.get('/recordings/detail').text
    boxes = re.findall(r'<input class="summary-checkbox"[^>]*>', html)
    assert len(boxes) == 3
    assert all('type="checkbox" disabled' in box for box in boxes)
    assert ' checked' not in boxes[0]
    assert all(' checked' in box for box in boxes[1:])
    assert '<strong>任务</strong>' in html
    assert source in detail_client.get('/recordings/detail.md').text


def test_summary_todo_section_stops_at_next_peer_heading(detail_client):
    detail_client.add(summary='## 待办\n\n- 跟进\n  - 确认\n\n### 明天\n\n1. 回访\n\n## 讨论\n\n- 普通列表')
    html = detail_client.get('/recordings/detail').text
    assert html.count('class="summary-checkbox"') == 3
    assert '<li>普通列表</li>' in html


def test_summary_task_continuations_and_code_are_not_rewritten(detail_client):
    detail_client.add(summary='- [ ] 第一段\n\n  [ ] 后续段落\n\n`[ ] 行内代码`\n\n```\n- [ ] 代码块\n00:00 代码时间\n```')
    html = detail_client.get('/recordings/detail').text
    assert html.count('class="summary-checkbox"') == 1
    assert '<p>[ ] 后续段落</p>' in html
    assert '<code>[ ] 行内代码</code>' in html
    assert '- [ ] 代码块\n00:00 代码时间' in html
    assert 'class="chapter-time"' not in html


def test_summary_chapter_lines_preserve_inline_format_and_quote(detail_client):
    detail_client.add(summary='00:00 **开场**\n01:20 讨论\n\n> 章节摘要\n\n01:02:03 收尾')
    html = detail_client.get('/recordings/detail').text
    assert '<span class="chapter-time">00:00</span> <strong>开场</strong></span>' in html
    assert '<span class="chapter-time">01:20</span> 讨论</span>' in html
    assert '<span class="chapter-time">01:02:03</span> 收尾</span>' in html
    assert '<blockquote>\n<p>章节摘要</p>\n</blockquote>' in html


def test_summary_chapter_task_and_table_xss_remains_escaped(detail_client):
    detail_client.add(summary='00:00 <script>alert(1)</script>\n\n- [ ] <img src=x onerror=alert(1)>\n\n| 名称 |\n| --- |\n| <script>bad()</script> |')
    html = detail_client.get('/recordings/detail').text
    assert '<script>alert' not in html and '<script>bad' not in html
    assert '<img src=x' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html


def test_summary_tables_and_links(detail_client):
    detail_client.add(summary='| 项目 | 进度 |\n| --- | --- |\n| A | 完成 |\n\n[链接](https://example.com)\n\n---')
    html = detail_client.get('/recordings/detail').text
    assert 'class="summary-table" role="region" aria-label="表格" tabindex="0"' in html
    assert '<table>' in html and '<thead>' in html and '<th>项目</th>' in html and '<td>完成</td>' in html
    assert '<a href="https://example.com" rel="noopener noreferrer">链接</a>' in html
    assert '<hr />' in html


def test_summary_document_is_absent_from_transcript(detail_client):
    html = detail_client.get('/recordings/detail?view=transcript').text
    assert 'summary-document' not in html and 'summary-metadata' not in html
    assert '智能纪要由 AI 生成' not in html
    assert html.count('class="transcript-segment"') == 2


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_detail_views_share_one_layout_container(detail_client, view):
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert html.count('<main class="detail-layout">') == 1
    main = re.search(r'<main class="detail-layout">(.*?)</main>', html, re.S)[1]
    assert '<header class="detail-header">' in main
    assert '<nav class="detail-view-switch"' in main
    assert 'class="detail-meta"' in main
    assert main.count('<section class="detail-section ') == 1
    assert f'aria-labelledby="{view}-heading"' in main
    assert 'class="detail-layout"' not in detail_client.get('/').text


def test_detail_layout_width_and_gutter_contract(detail_client):
    css = detail_client.get('/static/app.css').text
    frame = re.search(r'\.detail-layout \{([^}]+)\}', css)[1]
    assert '--content-max: 1120px;' in css
    assert '--gutter: 16px;' in css
    assert 'max-width: var(--content-max);' in frame
    assert 'padding: 0 var(--gutter) 48px;' in frame
    assert 'margin-inline: auto;' in frame
    assert css.count('--gutter:') == 1
    assert '.detail-view-switch { margin-top: 16px;' in css
    assert '.transcript-segments { width: 100%; }' in css
    summary = re.search(r'\.detail-section\.summary-document \{([^}]+)\}', css)[1]
    assert 'max-width' not in summary and 'padding' not in summary


def test_detail_long_text_readability(detail_client):
    css = detail_client.get('/static/app.css').text
    for selector in ['.transcript-body', '.detail-section.summary-document',
                     '.summary-document .markdown-body p']:
        rule = re.search(re.escape(selector) + r' \{([^}]+)\}', css)[1]
        assert 'line-height: 1.75;' in rule
    for selector in ['.transcript-segment + .transcript-segment',
                     '.transcript-paragraph + .transcript-paragraph']:
        assert selector + ' { margin-top: 24px; }' in css


def test_detail_section_has_nonzero_horizontal_padding(detail_client):
    css = detail_client.get('/static/app.css').text
    rules = re.findall(r'\.detail-section \{([^}]+)\}', css)
    paddings = [re.search(r'padding:\s*([^;]+);', rule)[1].split() for rule in rules]
    assert paddings == [['28px', '32px', '40px'], ['20px', '16px', '28px']]
    assert all(float(padding[1].removesuffix('px')) > 0 for padding in paddings)
    assert re.search(r'@media \(max-width: 719\.98px\)\s*\{\s*\.detail-section \{ padding: 20px 16px 28px;', css)
