from datetime import datetime
from html.parser import HTMLParser
import re
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
            if 'recording-row' in attrs.get('class', '').split():
                self.row_fields[self.current_card] = set()
        classes = attrs.get('class', '').split()
        if self.current_card in self.row_fields:
            self.row_fields[self.current_card].update(set(classes) & {
                'recording-time', 'recording-title', 'recording-duration', 'recording-status',
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
    assert all(field.get('value') == '' for field in page.inputs if field.get('type') == 'date')
    assert not page.selected
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


def test_list_time_always_includes_month_day_and_hour_minute(list_client, add_recording):
    add_recording('today-early', '2026-10-29T00:01:00+08:00')
    add_recording('today-late', '2026-10-29T10:14:00+08:00')
    add_recording('yesterday', '2026-10-28T23:59:00+08:00')
    html = list_client.get('/?from=2026-10-28&to=2026-10-29').text
    times = re.findall(r'<time class="recording-time"[^>]*>([^<]+)</time>', html)
    assert len(times) == 3
    assert all(re.fullmatch(r'\d{2}-\d{2} \d{2}:\d{2}', value) for value in times)
    assert times == ['10-29 10:14', '10-29 00:01', '10-28 23:59']


def test_apple_grouped_list_and_dark_mode_markers(list_client):
    html = list_client.get('/').text
    assert 'class="recording-list inset-grouped"' in html
    assert 'segmented-control' not in html
    assert not {'今天', '昨天'} & {''.join(parts).strip() for parts in Page(html).controls}
    assert 'aria-label="清除筛选"' in html
    css = list_client.get('/static/app.css').text
    assert '@media (prefers-color-scheme: dark)' in css
    assert '.inset-grouped' in css
    assert '"SF Pro Text"' in css
    assert 'outline: 2px solid var(--accent)' in css


@pytest.mark.parametrize('query,expected', [
    ({'date': '2026-10-28'}, ['day-28']),
    ({'from': '2026-10-27', 'to': '2026-10-28'}, ['day-28', 'day-27']),
    ({'from': '2026-10-28', 'to': '2026-10-28'}, ['day-28']),
    ({'yesterday': '1'}, ['day-28']),
])
def test_date_filters_include_whole_days(list_client, add_recording, query, expected):
    for day in range(26, 30):
        add_recording(f'day-{day}', f'2026-10-{day}T23:59:59.999999+08:00')
    response = list_client.get('/', params=query)
    assert response.status_code == 200
    assert Page(response.text).cards == expected
    assert Page(response.text).selected
    for key, value in query.items():
        if key == 'date':
            assert any(field.get('name') == 'from' and field.get('value') == value
                       for field in Page(response.text).inputs)
        elif key != 'yesterday':
            assert any(field.get('name') == key and field.get('value') == value
                       for field in Page(response.text).inputs)


def test_row_metadata_four_states_and_no_recording_bodies(list_client, add_recording):
    for recording_id, transcript, summary in [
        ('complete', True, True), ('transcript-only', True, False),
        ('summary-only', False, True), ('empty', False, False),
    ]:
        add_recording(recording_id, transcript=transcript, summary=summary,
                      duration=None if recording_id == 'empty' else 18)
    html = list_client.get('/').text
    assert Page(html).badges == {
        'complete': '已转录', 'transcript-only': '仅转写', 'summary-only': '仅总结', 'empty': '未转录',
    }
    assert '>10-29 09:30</time>' in html
    assert '18 分</span>' in html and '—' in html
    assert '时间为笔记创建时间' in html
    assert 'SUMMARY_BODY_PRIVATE' not in html and 'TRANSCRIPT_BODY_PRIVATE' not in html
    assert len(html) < 20000
    assert {link['href'] for link in Page(html).links if link.get('href', '').startswith('/recordings/')} == {
        '/recordings/complete', '/recordings/transcript-only', '/recordings/summary-only', '/recordings/empty',
    }


@pytest.mark.parametrize('query', [
    {}, {'date': '2026-10-29'}, {'yesterday': '1'},
    {'from': '2026-10-28', 'to': '2026-10-29'},
])
def test_pagination_20_then_5_no_overlap_and_preserves_filter(list_client, add_recording, query):
    day = '2026-10-28' if 'yesterday' in query else '2026-10-29'
    for i in range(25):
        # Identical timestamps also require a stable tie breaker.
        add_recording(f'item-{i:02}', f'{day}T12:00:00+08:00')
    if query:
        # More than a page of newer records outside the selected period would
        # replace page 2 if pagination accidentally dropped the date filter.
        for i in range(25):
            add_recording(f'outside-{i:02}', '2026-10-30T12:00:00+08:00')
    first = list_client.get('/', params=query)
    page1 = Page(first.text)
    next_link, = [link for link in page1.links if link.get('rel') == 'next']
    assert parse_qs(urlsplit(next_link['href']).query) == {**{k: [v] for k, v in query.items()}, 'page': ['2']}
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
def test_invalid_filters_have_readable_error(list_client, query):
    response = list_client.get('/', params=query)
    assert response.status_code == 400
    assert '筛选条件无效' in response.text
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
    assert '0 分</span>' in html


def test_desktop_rows_each_have_four_fields_and_detail_link(list_client, add_recording):
    for i in range(23):
        add_recording(f'row-{i:02}')
    page = Page(list_client.get('/').text)
    assert len(page.row_fields) == len(page.cards) == 20
    for recording_id, fields in page.row_fields.items():
        assert fields == {'recording-time', 'recording-title', 'recording-duration', 'recording-status'}
        assert any(link.get('class') == 'recording-row-link' and
                   link.get('href') == f'/recordings/{recording_id}' for link in page.links)


def test_long_title_has_complete_escaped_title_attribute(list_client, add_recording):
    title = '会议："导师中心"与教务分身 <进展> & 后续安排' * 12
    add_recording('long-title', title=title)
    page = Page(list_client.get('/').text)
    assert page.titles == {'long-title': title}


def test_mobile_two_line_structure_and_desktop_header(list_client, add_recording):
    add_recording('mobile')
    html = list_client.get('/').text
    for class_name in ['recording-list-header', 'recording-row-link', 'recording-time',
                       'recording-title', 'recording-details', 'recording-meta-separator',
                       'recording-duration', 'recording-status']:
        assert f'class="{class_name}"' in html


def test_header_search_is_enabled_and_css_is_served(list_client):
    html = list_client.get('/').text
    assert 'QwenNote' in html and '测试用户' in html and '退出' in html
    search, = [field for field in Page(html).inputs if field.get('type') == 'search']
    assert 'disabled' not in search
    assert search.get('name') == 'q'
    assert 'action="/search"' in html
    assert '更新' in html
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
    assert f'共 {total} 场' in response.text
    assert all(field.get('value') == '' for field in Page(response.text).inputs if field.get('type') == 'date')


@pytest.mark.parametrize('query,label', [
    ({'date': '2026-10-29'}, ['2026-10-29', '']),
    ({'from': '2026-10-29', 'to': '2026-10-30'}, ['2026-10-29', '2026-10-30']),
])
def test_filter_visible_and_clear_returns_all(list_client, add_recording, query, label):
    add_recording('outside', '2026-10-28T12:00:00+08:00')
    add_recording('inside')
    response = list_client.get('/', params=query)
    assert [field['value'] for field in Page(response.text).inputs if field.get('type') == 'date'] == label
    assert 'aria-current="true"' in response.text
    assert Page(response.text).cards == ['inside']
    clear, = [link for link in Page(response.text).links if link.get('aria-label') == '清除筛选']
    assert clear['href'] == '/'
    cleared = list_client.get(clear['href'])
    assert Page(cleared.text).cards == ['inside', 'outside']
    assert all(field.get('value') == '' for field in Page(cleared.text).inputs if field.get('type') == 'date')


@pytest.mark.parametrize('query', [
    {'date': '2026-10-29'}, {'from': '2026-10-29', 'to': '2026-10-30'},
])
def test_filtered_empty_state(list_client, add_recording, query):
    add_recording('outside', '2026-10-28T12:00:00+08:00')
    response = list_client.get('/', params=query)
    assert response.status_code == 200
    assert response.context['recordings'].total == 0
    assert '这个时间范围内没有录音' in response.text
    assert '还没有同步任何录音' not in response.text


@pytest.mark.parametrize('path,query,target', [
    ('/', {'from': '2026-10-29', 'to': ''}, '/?date=2026-10-29'),
    ('/', {'from': '', 'to': ''}, '/'),
    ('/search', {'q': '会议', 'from': '2026-10-29', 'to': ''}, '/search?q=%E4%BC%9A%E8%AE%AE&date=2026-10-29'),
    ('/search', {'q': '会议', 'from': '', 'to': ''}, '/search?q=%E4%BC%9A%E8%AE%AE'),
])
def test_one_row_form_canonicalizes_single_day_and_cleared_dates(list_client, path, query, target):
    response = list_client.get(path, params=query)
    assert response.status_code == 303
    assert response.headers['location'] == target
    assert list_client.get(target).status_code == 200


def test_form_end_without_start_is_invalid(list_client):
    assert list_client.get('/?from=&to=2026-10-29').status_code == 400
