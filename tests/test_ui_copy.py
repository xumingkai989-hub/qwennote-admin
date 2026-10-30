"""UI copy contract: remove redundant explanations, preserve useful guidance."""
import re

import pytest

from app.feishu import FeishuUser
from test_detail_page import detail_client
from test_login import login


@pytest.mark.parametrize('url', ['/', '/?date=2026-10-29', '/search?q=会议'])
def test_home_removes_redundant_help_and_moves_time_note(client, url):
    login(client)
    html = client.get(url).text
    assert '只填起始日期可查看单日' not in html
    assert 'date-help' not in html
    assert 'selected-period' not in html
    assert '时间均为笔记创建时间' not in html
    assert html.count('时间为笔记创建时间') == 1
    assert html.index('时间为笔记创建时间') > html.index('class="pagination"')
    css = client.get('/static/app.css').text
    note = re.search(r'\.time-note \{([^}]+)\}', css)[1]
    assert 'font-size: 12px' in note and 'color: var(--secondary)' in note


@pytest.mark.parametrize('url', ['/admin', '/admin?q=张三', '/admin?q=不存在'])
def test_admin_removes_limit_but_keeps_section_names(client, fake_auth, url):
    login(client)
    fake_auth.search_results = [FeishuUser('ou_contact', '张三', '')]
    html = client.get(url).text
    assert '最多显示 20 人' not in html
    assert '当前白名单' in html
    if url == '/admin?q=张三':
        assert '联系人搜索结果' in html
    if url == '/admin?q=不存在':
        assert '未找到联系人' in html
        assert '联系人搜索结果' not in html


@pytest.mark.parametrize('view', ['summary', 'transcript'])
def test_detail_copy_labels_are_short(detail_client, view):
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert '可全选复制' not in html
    assert '<label for="markdown-source">Markdown 原文</label>' in html
    if view == 'transcript':
        assert '<label for="transcript-source">转写原文</label>' in html


def test_detail_preserves_ai_disclaimer(detail_client):
    assert '智能纪要由 AI 生成，可能不准确，请谨慎甄别后使用' in detail_client.get('/recordings/detail').text


@pytest.mark.parametrize('view,message', [
    ('summary', '这条录音还没有 AI 总结'),
    ('transcript', '这条录音还没有转写内容'),
])
def test_detail_preserves_each_empty_message_without_empty_toolbar(detail_client, view, message):
    detail_client.add(summary='', transcript='')
    html = detail_client.get('/recordings/detail?view=' + view).text
    assert message in html
    assert 'transcript-toolbar' not in html
    assert 'summary-metadata' not in html
    assert '<hr' not in html


@pytest.mark.parametrize('message', [
    '你的 open_id：', '（把上面这串交给管理员加入白名单即可）',
])
def test_error_preserves_identity_and_next_step(client, fake_auth, message):
    fake_auth.user = FeishuUser('ou_denied', '访客', '')
    response = login(client)
    assert response.status_code == 403
    assert message in response.text
    assert '<code>ou_denied</code>' in response.text


def test_short_whitelist_subject_has_no_stray_ellipsis(client, fake_auth, store):
    login(client)
    store.add_user(FeishuUser('short', '短标识', ''))
    html = client.get('/admin').text
    assert '<td data-label="subject" title="short">short</td>' in html


def test_unsynced_state_is_not_repeated(client):
    login(client)
    html = client.get('/').text
    assert html.count('尚未同步') == 1
    assert '拉取 0 · 新增 0 · 更新 0' not in html


def test_empty_whitelist_has_no_empty_table(client, store, monkeypatch):
    login(client)
    monkeypatch.setattr(store, 'list_users', lambda: [])
    html = client.get('/admin').text
    assert '当前白名单' in html
    assert '白名单为空' in html
    assert '<table' not in html
