from datetime import datetime
from html.parser import HTMLParser
import re
from xml.etree import ElementTree
from urllib.parse import parse_qs, urlsplit

import pytest

import app.main as main
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from test_login import login


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.controls = []
        self.current_control = None
        self.links = []
        self.cards = []
        self.selected = []
        self.inputs = []
        self.badges = {}
        self.row_fields = {}
        self.titles = {}
        self.current_card = None
        self.in_badge = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('a', 'button'):
            self.current_control = []
            self.controls.append(self.current_control)
        if tag == 'a':
            self.links.append(attrs)
        if tag == 'article':
            self.cards.append(attrs['data-recording-id'])
            self.current_card = attrs['data-recording-id']
            if 'timeline-card' in attrs.get('class', '').split():
                self.row_fields[self.current_card] = set()
        classes = attrs.get('class', '').split()
        if self.current_card in self.row_fields:
            self.row_fields[self.current_card].update(set(classes) & {
                'recording-time', 'recording-title', 'duration-pill', 'card-summary',
            })
        if 'recording-title' in classes:
            self.titles[self.current_card] = attrs.get('title')
        if tag == 'span' and 'badge' in attrs.get('class', '').split():
            self.in_badge = True
        if attrs.get('aria-current'):
            self.selected.append(attrs)
        if tag == 'input':
            self.inputs.append(attrs)

    def handle_data(self, data):
        if self.current_control is not None:
            self.current_control.append(data)
        if self.in_badge and self.current_card:
            self.badges[self.current_card] = data.strip()

    def handle_endtag(self, tag):
        if tag in ('a', 'button'):
            self.current_control = None
        if tag == 'span':
            self.in_badge = False
        if tag == 'article':
            self.current_card = None


@pytest.fixture
def list_client(client, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # UTC is still October 28; Shanghai is already October 29.
            return datetime(2026, 10, 29, 0, 30, tzinfo=SHANGHAI).astimezone(tz)

    monkeypatch.setattr(main, 'datetime', FrozenDatetime, raising=False)
    assert login(client).status_code == 302
    return client


@pytest.fixture
def add_recording(store):
    def add(recording_id, created='2026-10-29T09:30:00+08:00', *,
            title=None, duration=18, transcript=True, summary=True):
        store.upsert_recording(
            RecordingMeta(recording_id, title or recording_id, datetime.fromisoformat(created), duration),
            RecordingDetail(recording_id, 'SUMMARY_BODY_PRIVATE' * 1000,
                            'TRANSCRIPT_BODY_PRIVATE' * 1000, transcript, summary),
            '2026-10-29T12:00:00+08:00',
        )
    return add


def test_default_is_all_dates_sorted_by_created_time(list_client, add_recording):
    add_recording('early', '2026-10-29T00:00:00+08:00')
    add_recording('late', '2026-10-29T23:59:59.999999+08:00')
    add_recording('yesterday', '2026-10-28T23:59:59+08:00')
    add_recording('tomorrow', '2026-10-30T00:00:00+08:00')
    response = list_client.get('/')
    assert response.status_code == 200
    page = Page(response.text)
    assert page.cards == ['tomorrow', 'late', 'early', 'yesterday']
    assert response.context['filters']['start'] is None
    assert response.context['filters']['end'] is None
    assert 'type="date"' not in response.text
    assert not [item for item in page.selected if 'navigation-link' not in item.get('class', '').split()]
    assert response.headers['cache-control'] == 'no-store'


def test_list_newest_created_at_first_with_descending_id_tiebreaker(list_client, add_recording):
    # Deliberately insert in an order that differs from both sort keys.
    add_recording('z-older', '2026-10-28T23:59:00+08:00')
    add_recording('a-newest', '2026-10-30T00:01:00+08:00')
    add_recording('a-middle', '2026-10-29T10:14:00+08:00')
    add_recording('z-newest', '2026-10-30T00:01:00+08:00')
    add_recording('z-middle', '2026-10-29T10:14:00+08:00')
    response = list_client.get('/?from=2026-10-28&to=2026-10-30')
    assert response.status_code == 200
    assert Page(response.text).cards == [
        'z-newest', 'a-newest', 'z-middle', 'a-middle', 'z-older',
    ]


def test_list_time_shows_hour_minute_below_date_heading(list_client, add_recording):
    add_recording('today-early', '2026-10-29T00:01:00+08:00')
    add_recording('today-late', '2026-10-29T10:14:00+08:00')
    add_recording('yesterday', '2026-10-28T23:59:00+08:00')
    html = list_client.get('/?from=2026-10-28&to=2026-10-29').text
    times = re.findall(r'<time class="recording-time"[^>]*>([^<]+)</time>', html)
    assert len(times) == 3
    assert all(re.fullmatch(r'\d{2}:\d{2} 创建', value) for value in times)
    assert times == ['10:14 创建', '00:01 创建', '23:59 创建']
    assert '10月29日 周四' in html and '10月28日 周三' in html


def test_apple_grouped_list_and_dark_mode_markers(list_client):
    html = list_client.get('/').text
    assert 'class="recording-list timeline"' in html
    assert 'segmented-control' not in html
    assert not {'今天', '昨天'} & {''.join(parts).strip() for parts in Page(html).controls}
    assert 'type="date"' not in html
    assert not {'筛选', '清除'} & {''.join(parts).strip() for parts in Page(html).controls}
    css = list_client.get('/static/app.css').text
    assert '@media (prefers-color-scheme: dark)' in css
    assert '.date-group' in css
    assert '"SF Pro Text"' in css
    assert 'outline: 2px solid var(--accent)' in css


@pytest.mark.parametrize('query', [
    {'date': '2026-10-28'},
    {'from': '2026-10-01', 'to': '2026-10-31'},
    {'from': '2026-10-28', 'to': '2026-10-28'},
    {'yesterday': '1'},
])
def test_date_parameters_are_ignored(list_client, add_recording, query):
    for day in range(26, 30):
        add_recording(f'day-{day}', f'2026-10-{day}T23:59:59.999999+08:00')
    add_recording('outside-month', '2026-09-30T12:00:00+08:00')
    baseline = list_client.get('/')
    response = list_client.get('/', params=query)
    assert response.status_code == 200
    assert response.context['recordings'].total == baseline.context['recordings'].total == 5
    assert response.text == baseline.text
    assert Page(response.text).cards == ['day-29', 'day-28', 'day-27', 'day-26', 'outside-month']


def test_card_badges_follow_transcript_state_and_only_excerpt_is_rendered(list_client, add_recording):
    for recording_id, transcript, summary in [
        ('complete', True, True), ('transcript-only', True, False),
        ('summary-only', False, True), ('empty', False, False),
    ]:
        add_recording(recording_id, transcript=transcript, summary=summary,
                      duration=None if recording_id == 'empty' else 18)
    html = list_client.get('/').text
    assert Page(html).badges == {
        'complete': '已转录', 'transcript-only': '仅转写',
        'summary-only': '仅总结', 'empty': '未转录',
    }
    assert '>09:30 创建</time>' in html
    assert '录音 · 18分</span>' in html
    assert html.count('class="duration-pill"') == 3
    assert '时间为笔记创建时间' in html
    assert 'SUMMARY_BODY_PRIVATE' in html and 'TRANSCRIPT_BODY_PRIVATE' not in html
    assert 'SUMMARY_BODY_PRIVATE' * 7 not in html
    assert len(html) < 20000
    assert {link['href'] for link in Page(html).links if link.get('href', '').startswith('/recordings/')} == {
        '/recordings/complete', '/recordings/transcript-only', '/recordings/summary-only', '/recordings/empty',
    }


@pytest.mark.parametrize('query', [
    {}, {'date': '2026-10-29'}, {'yesterday': '1'},
    {'from': '2026-10-28', 'to': '2026-10-29'},
])
def test_pagination_20_then_5_no_overlap_and_drops_dates(list_client, add_recording, query):
    day = '2026-10-28' if 'yesterday' in query else '2026-10-29'
    for i in range(25):
        # Identical timestamps also require a stable tie breaker.
        add_recording(f'item-{i:02}', f'{day}T12:00:00+08:00')
    first = list_client.get('/', params=query)
    page1 = Page(first.text)
    next_link, = [link for link in page1.links if link.get('rel') == 'next']
    assert parse_qs(urlsplit(next_link['href']).query) == {'page': ['2']}
    second = list_client.get(next_link['href'])
    page2 = Page(second.text)
    assert len(page1.cards) == 20 and len(page2.cards) == 5
    assert set(page1.cards).isdisjoint(page2.cards)
    assert set(page1.cards + page2.cards) == {f'item-{i:02}' for i in range(25)}
    assert '第 1 / 2 页' in first.text and '第 2 / 2 页' in second.text
    previous, = [link for link in page2.links if link.get('rel') == 'prev']
    assert parse_qs(urlsplit(previous['href']).query)['page'] == ['1']
    assert list_client.get(previous['href']).text == first.text
    assert not any(link.get('rel') == 'prev' for link in page1.links)
    assert not any(link.get('rel') == 'next' for link in page2.links)


def test_empty_state_and_out_of_range_page(list_client):
    response = list_client.get('/?page=99999999999999999999')
    assert response.status_code == 200
    assert '还没有同步任何录音' in response.text
    assert '这个时间范围内没有录音' not in response.text
    assert '第 1 / 1 页' in response.text
    assert Page(response.text).cards == []


def test_out_of_range_page_clamps_to_last_page(list_client, add_recording):
    for i in range(25):
        add_recording(f'item-{i:02}')
    response = list_client.get('/?page=999')
    assert len(Page(response.text).cards) == 5
    assert '第 2 / 2 页' in response.text


@pytest.mark.parametrize('query', [
    {'date': '2026-02-30'}, {'date': '20261029'}, {'date': ''},
    {'from': '2026-10-29'}, {'to': '2026-10-29'},
    {'from': 'bad', 'to': '2026-10-29'},
    {'from': '2026-10-29', 'to': '2026-10-28'},
    {'date': '2026-10-29', 'yesterday': '1'}, {'yesterday': '0'},
    {'page': '0'}, {'page': '-1'}, {'page': 'bad'}, {'page': '1.5'},
])
def test_invalid_dates_are_ignored_but_invalid_pages_have_readable_error(list_client, add_recording, query):
    add_recording('visible')
    response = list_client.get('/', params=query)
    if 'page' in query:
        assert response.status_code == 400
        assert '页码无效' in response.text
    else:
        assert response.status_code == 200
        assert response.text == list_client.get('/').text
        assert response.context['recordings'].total == 1
    assert 'Traceback' not in response.text


def test_unauthenticated_filtered_list_redirects_before_validation(client):
    response = client.get('/?date=bad&page=0')
    assert response.status_code == 302
    assert urlsplit(response.headers['location']).netloc == 'accounts.feishu.cn'


def test_title_is_escaped_and_zero_duration_is_visible(list_client, add_recording):
    add_recording('escaped', title='<script>alert(1)</script>', duration=0)
    html = list_client.get('/').text
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '录音 · 0分</span>' in html


def test_cards_each_have_four_fields_and_detail_link(list_client, add_recording):
    for i in range(23):
        add_recording(f'row-{i:02}')
    page = Page(list_client.get('/').text)
    assert len(page.row_fields) == len(page.cards) == 20
    for recording_id, fields in page.row_fields.items():
        assert fields == {'recording-time', 'recording-title', 'duration-pill', 'card-summary'}
        assert any(link.get('class') == 'timeline-card-link' and
                   link.get('href') == f'/recordings/{recording_id}' for link in page.links)


def test_long_title_has_complete_escaped_title_attribute(list_client, add_recording):
    title = '会议："导师中心"与教务分身 <进展> & 后续安排' * 12
    add_recording('long-title', title=title)
    page = Page(list_client.get('/').text)
    assert page.titles == {'long-title': title}


def test_mobile_two_line_structure_and_desktop_header(list_client, add_recording):
    add_recording('mobile')
    html = list_client.get('/').text
    for class_name in ['date-heading', 'timeline-card-link', 'recording-time',
                       'recording-title', 'card-summary', 'duration-pill', 'card-footer']:
        assert f'class="{class_name}"' in html
    assert 'recording-list-header' not in html
    css = list_client.get('/static/app.css').text
    assert '-webkit-line-clamp: 2' in css
    assert '.date-group { position: relative; padding-left: 20px; }' in css
    assert '.date-group:not(:last-child)::before' in css


def test_header_search_is_enabled_and_css_is_served(list_client):
    html = list_client.get('/').text
    assert 'QwenNote' in html and '测试用户' in html and '退出' in html
    search, = [field for field in Page(html).inputs if field.get('type') == 'search']
    assert 'disabled' not in search
    assert search.get('name') == 'q'
    assert 'action="/search"' in html
    assert 'aria-label="快速同步"' in html
    assert '/static/app.css' in html
    css = list_client.get('/static/app.css')
    assert css.status_code == 200 and 'text/css' in css.headers['content-type']
    assert '@media' in css.text
    assert 'style=' not in html


def test_logout_clears_session_and_returns_to_login(list_client):
    response = list_client.post('/auth/logout')
    assert response.status_code == 302 and response.headers['location'] == '/'
    assert not list_client.cookies.get('session')
    assert list_client.get('/').status_code == 302


def test_logout_rejects_foreign_origin(list_client):
    assert list_client.post('/auth/logout', headers={'Origin': 'https://other.example'}).status_code == 403
    assert list_client.get('/').status_code == 200


@pytest.mark.parametrize('url', ['/', '/?page=1'])
def test_all_dates_total_matches_database_and_first_page_has_20(list_client, add_recording, store, url):
    for i in range(27):
        add_recording(f'day-{i + 1:02}', f'2026-10-{i + 1:02}T12:00:00+08:00')
    with store._connect() as conn:
        total = conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        newest = conn.execute('SELECT recording_id FROM recordings ORDER BY created_at DESC LIMIT 1').fetchone()[0]
    response = list_client.get(url)
    assert response.context['recordings'].total == total == 27
    assert len(Page(response.text).cards) == 20
    assert Page(response.text).cards[0] == newest
    assert f'全部录音 · {total} 场' in response.text
    assert 'type="date"' not in response.text


@pytest.mark.parametrize('query', [
    {'date': '2026-10-29'},
    {'from': '2026-10-29', 'to': '2026-10-30'},
])
def test_legacy_dates_do_not_show_controls_or_current_filter(list_client, add_recording, query):
    add_recording('outside', '2026-10-28T12:00:00+08:00')
    add_recording('inside')
    response = list_client.get('/', params=query)
    assert 'type="date"' not in response.text
    assert '筛选' not in response.text and '清除' not in response.text
    assert 'aria-current="true"' not in response.text
    assert Page(response.text).cards == ['inside', 'outside']
    assert response.text == list_client.get('/').text


@pytest.mark.parametrize('query', [
    {'date': '2026-10-29'}, {'from': '2026-10-29', 'to': '2026-10-30'},
])
def test_empty_state_with_ignored_date_parameters(list_client, query):
    response = list_client.get('/', params=query)
    assert response.status_code == 200
    assert response.context['recordings'].total == 0
    assert '这个时间范围内没有录音' not in response.text
    assert '还没有同步任何录音' in response.text
    assert '第 1 / 1 页' in response.text


@pytest.mark.parametrize('path,query', [
    ('/', {'from': '2026-10-29', 'to': ''}),
    ('/', {'from': '', 'to': ''}),
    ('/search', {'q': '会议', 'from': '2026-10-29', 'to': ''}),
    ('/search', {'q': '会议', 'from': '', 'to': ''}),
])
def test_legacy_form_parameters_are_ignored_without_redirect(list_client, path, query):
    response = list_client.get(path, params=query)
    assert response.status_code == 200
    assert 'location' not in response.headers
    assert response.text == list_client.get(path, params={'q': query['q']} if 'q' in query else {}).text


def test_form_end_without_start_is_ignored(list_client):
    response = list_client.get('/?from=&to=2026-10-29')
    assert response.status_code == 200
    assert response.text == list_client.get('/').text


def test_three_date_groups_descending_with_cards_sorted_inside(list_client, add_recording):
    for key, day, hour in [('old', 28, 18), ('new-early', 30, 1), ('middle', 29, 23), ('new-late', 30, 22)]:
        add_recording(key, f'2026-10-{day}T{hour:02}:00:00+08:00')
    html = list_client.get('/').text
    headings = re.findall(r'<h2 class="date-heading"[^>]*><time[^>]*>([^<]+)</time>', html)
    assert headings == ['10月30日 周五', '10月29日 周四', '10月28日 周三']
    assert Page(html).cards == ['new-late', 'new-early', 'middle', 'old']


@pytest.mark.parametrize('value,expected', [
    ('# 标题\n\n**重点**与[链接](https://example.com)\n\n- 项目\n- `代码`', '标题 重点与链接 项目 代码'),
    ('> 引用\n\n~~删除线~~\n\n- [x] 完成\n- [ ] 待办', '引用 删除线 完成 待办'),
    ('\n  \n---\n', ''),
    ('一' * 140, '一' * 120),
])
def test_summary_preview_strips_markdown_and_limits_length(value, expected):
    from app.home_presentation import summary_excerpt
    assert summary_excerpt(value) == expected


@pytest.mark.parametrize('summary,has_summary', [('', True), ('   \n', True), ('---', True), ('stale summary', False)])
def test_no_summary_has_no_empty_paragraph(list_client, store, summary, has_summary):
    store.upsert_recording(
        RecordingMeta('empty-preview', '无摘要', datetime(2026, 10, 30, 10, 59, tzinfo=SHANGHAI), None),
        RecordingDetail('empty-preview', summary, '', False, has_summary), '2026-10-30T12:00:00+08:00',
    )
    html = list_client.get('/').text
    assert 'class="card-summary"' not in html
    assert 'class="duration-pill"' not in html
    assert '10:59 创建' in html
    assert Page(html).badges['empty-preview'] == ('仅总结' if has_summary else '未转录')


@pytest.mark.parametrize('minutes,expected', [(None, ''), (0, '0分'), (18, '18分'), (60, '1小时'), (90, '1小时30分')])
def test_duration_uses_available_minute_precision(minutes, expected):
    from app.home_presentation import duration_label
    assert duration_label(minutes) == expected


def test_group_key_uses_shanghai_date_not_source_offset():
    from app.home_presentation import date_groups
    from app.store import RecordingCard
    rows = [RecordingCard(key, key, datetime.fromisoformat(timestamp), None, False, False)
            for key, timestamp in [('utc', '2026-10-29T17:00:00+00:00'), ('local', '2026-10-29T23:00:00+08:00')]]
    groups = date_groups(rows, None)
    assert [group['label'] for group in groups] == ['10月30日 周五', '10月29日 周四']
    assert groups[0]['items'][0]['recording'].created_at.strftime('%H:%M') == '01:00'


def test_total_and_card_links_preserve_page_but_drop_dates(list_client, add_recording):
    for index in range(25):
        add_recording(f'card-{index:02}')
    add_recording('outside', '2026-10-28T23:00:00+08:00')
    html = list_client.get('/?date=2026-10-29&page=2').text
    assert '全部录音 · 26 场' in html
    links = [link['href'] for link in Page(html).links if link.get('class') == 'timeline-card-link']
    assert len(links) == 6
    assert all(parse_qs(urlsplit(link).query) == {'page': ['2']} for link in links)


def test_summary_escape_and_no_player_or_tags(list_client, store):
    store.upsert_recording(
        RecordingMeta('safe', '四要素', datetime(2026, 10, 30, 10, 59, tzinfo=SHANGHAI), 90),
        RecordingDetail('safe', '**摘要** & <img src=x onerror=alert(1)> 正文', '', False, True),
        '2026-10-30T12:00:00+08:00',
    )
    html = list_client.get('/').text
    assert '四要素' in html
    assert '<p class="card-summary">摘要 &amp; 正文</p>' in html
    assert '录音 · 1小时30分' in html and '10:59 创建' in html
    assert all(value not in html for value in ('onerror', '<audio', '播放', 'tag-chip', '上次同步时间'))


@pytest.mark.parametrize('transcript,summary,status,label', [
    (True, True, 'complete', '已转录'),
    (True, False, 'transcript-only', '仅转写'),
    (False, True, 'summary-only', '仅总结'),
    (False, False, 'empty', '未转录'),
])
def test_home_status_badge_and_creation_time_share_footer(
    list_client, add_recording, transcript, summary, status, label,
):
    add_recording('status-card', transcript=transcript, summary=summary)
    html = list_client.get('/').text
    card = ElementTree.fromstring(re.search(r'<article\b.*?</article>', html, re.S)[0])
    link = card.find("./a[@class='timeline-card-link']")
    footer = link.find("./div[@class='card-footer']")
    assert footer is not None and list(link)[-1] is footer
    badge = footer.find(f"./span[@class='badge badge-{status}']")
    assert badge is not None and badge.text == label
    assert list(footer)[0] is badge
    created = footer.find("./span[@class='card-created']")
    assert created is not None and list(footer)[-1] is created
    assert created.find("./time[@class='recording-time']").text == '09:30 创建'
    assert len(card.findall(".//span[@class='badge badge-" + status + "']")) == 1
    title_row = link.find("./div[@class='card-title-row']")
    assert title_row.find('./span') is None


@pytest.mark.parametrize('status,light_bg,light_text,dark_bg,dark_text', [
    ('complete', '#E7F7EC', '#1B7F3B', '#1C3A26', '#85D9A0'),
    ('transcript-only', '#E8F0FF', '#2551A8', '#16233F', '#9BBCFF'),
    ('summary-only', '#FFF4E5', '#9A5B00', '#3A2A12', '#F5C77E'),
    ('empty', '#F2F2F7', '#6B6B70', '#2C2C2E', '#AEAEB2'),
])
def test_home_status_palette_and_dark_contrast(
    list_client, status, light_bg, light_text, dark_bg, dark_text,
):
    css = list_client.get('/static/app.css').text
    light = re.search(r'(?m)^\.timeline \{([^}]+)\}', css)[1]
    dark = re.search(
        r'@media \(prefers-color-scheme: dark\) \{\s*\.timeline \{([^}]+)\}', css,
    )[1]
    for block, bg, text in [(light, light_bg, light_text), (dark, dark_bg, dark_text)]:
        assert f'--status-{status}-bg: {bg};' in block
        assert f'--status-{status}-text: {text};' in block
    rule = re.search(r'\.timeline \.badge-' + status + r' \{([^}]+)\}', css)[1]
    assert f'background: var(--status-{status}-bg);' in rule
    assert f'color: var(--status-{status}-text);' in rule

    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))

    assert (luminance(dark_text) + .05) / (luminance(dark_bg) + .05) >= 4.5
