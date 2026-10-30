"""Presentation-only provenance cleanup, shared exits and inline editing contract."""
import hashlib
import secrets
from datetime import datetime

import pytest

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.recording_content import strip_origin_markers
from test_login import login


@pytest.mark.parametrize('text,expected', [
    ('正文{origin:1.60,45.33}', '正文'),
    ('前{origin:1,2}后', '前后'),
    ('甲{origin:1}乙{origin:2}丙', '甲乙丙'),
    ('甲{origin:1}{origin:2}乙', '甲乙'),
    ('甲  {origin:1}   乙   ', '甲 乙'),
    ('甲\n\n{origin:1}\n\n乙', '甲\n\n乙'),
    ('甲\r\n\r\n{origin:1}\r\n\r\n乙', '甲\n\n乙'),
    ('{origin:1}{origin:2}', ''),
    ('甲\n\n  {origin:1} \n {origin:2} \n\n乙', '甲\n\n乙'),
    ('甲{origin:跨\n行}乙', '甲乙'),
    ('甲{origin:跨\n\n段}乙', '甲乙'),
    ('甲{origin:未闭合', '甲{origin:未闭合'),
    ('正文 {other:保留} {origin:1}  结尾', '正文 {other:保留} 结尾'),
    ('    代码{origin:1}\n\n- 项目{origin:2}', '    代码\n\n- 项目'),
    ('普通  正文\n\n下一段', '普通  正文\n\n下一段'),
    ('', ''),
])
def test_strip_origin_markers(text, expected):
    assert strip_origin_markers(text) == expected
    assert strip_origin_markers(expected) == expected


@pytest.fixture
def provenance(client, store):
    body = '小{origin:1}红直播  {origin:2}  内容\n\n{origin:3}\n\n最后一句{origin:4}   '
    meta = RecordingMeta('origin-test', '展示测试', datetime(2026, 10, 30, 10, tzinfo=SHANGHAI), 5)
    store.upsert_recording(meta, RecordingDetail('origin-test', body, body, True, True), 'original')
    store.add_hotword('小鹿直播', '小红直播')
    store.add_hotword('标记不能先变热词', 'origin')
    from test_hotwords import approve_rules
    approve_rules(store, 'origin-test')
    login(client)
    key = 'qn_' + secrets.token_hex(16)
    store.create_api_key('origin-test', hashlib.sha256(key.encode()).hexdigest())
    client.headers['Authorization'] = 'Bearer ' + key
    return client, body


@pytest.mark.parametrize('path', [
    '/recordings/origin-test?view=summary', '/recordings/origin-test?view=transcript',
    '/recordings/origin-test.md',
    '/recordings/origin-test.md?part=summary', '/recordings/origin-test.md?part=transcript',
    '/recordings/origin-test.md?part=all', '/api/v1/recordings/origin-test',
    '/api/v1/daily/2026-10-30?format=md',
])
@pytest.mark.parametrize('override', [False, True])
def test_five_exits_strip_before_hotwords_without_polluting_source(provenance, store, path, override):
    client, original = provenance
    before = store.get_recording('origin-test')
    if override:
        for field in ('summary', 'transcript'):
            store.save_override('origin-test', field, '人工{origin:7}正文')
    response = client.get(path)
    assert response.status_code == 200
    assert 'origin:' not in response.text and '标记不能先变热词' not in response.text
    assert ('人工正文' if override else '小鹿直播') in response.text
    assert store.get_recording('origin-test') == before
    assert before.summary_md == before.transcript_md == original
    assert 'origin:' in before.summary_md
    if override:
        assert all('origin:' in item['content_md'] for item in store.get_overrides('origin-test').values())


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_inline_editor_and_navigation(provenance, view):
    client, _ = provenance
    page = client.get('/recordings/origin-test?view=' + view).text
    assert 'id="edit-button"' in page and 'aria-label="编辑"' in page
    assert 'contenteditable="false"' in page and '<textarea' not in page
    sidebar = page.split('<nav id="sidebar-links">')[1].split('</nav>')[0]
    assert 'href="/search"' not in sidebar
    assert all(label in sidebar for label in ['会议', '热词中心', 'AI 助手配置'])
    assert client.get('/search?q=直播').status_code == 200
    assert 'action="/search"' in client.get('/').text
