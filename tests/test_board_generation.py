"""T4 contracts: generation ordering, persisted idempotence and safe presentation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json

import pytest

from app import board, board_analysis, board_generation as generation, primary_tag, tag_time
from test_board import add, GOOD
from test_board_allocation import block
from test_board_page import board_client, token
from test_tag_time import meeting

DAY = date(2026, 11, 6)
MONDAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def migrated(store):
    store.migrate()


@pytest.fixture
def provider(monkeypatch):
    calls = []
    monkeypatch.setattr(board_analysis, '_analyze', lambda *a: (GOOD, 'fixture', None))
    def request(prompt):
        data = json.loads(prompt)
        calls.append(data)
        return json.dumps({'comparison': list(data['comparisons'])}, ensure_ascii=False)
    monkeypatch.setattr(generation, '_requester', lambda *args: request)
    return calls


def run(store):
    return generation.generate(store, scope='week', day=DAY)


def test_ensure_failure_continues_in_order(store, monkeypatch, provider):
    meeting(store)
    events = []
    def fail(*a, **kw):
        events.append('ensure')
        raise RuntimeError('private-provider-key')
    original = tag_time.week_allocation
    def allocate(*args):
        events.append('allocation')
        return original(*args)
    monkeypatch.setattr(primary_tag, 'ensure_week', fail)
    monkeypatch.setattr(tag_time, 'week_allocation', allocate)
    report = run(store)
    assert events[:2] == ['ensure', 'allocation']
    assert report['allocation']['total_minutes'] == 30
    assert '主标签判定部分失败' in report['error']
    assert report['allocation_analysis'] and len(provider) == 1
    assert 'private' not in json.dumps(report)


def test_repeat_and_restart_no_model_or_duplicate_rows(store, monkeypatch, provider):
    meeting(store)
    calls = []
    monkeypatch.setattr(board_analysis, '_analyze', lambda *a: (calls.append(1) or GOOD, 'fixture', None))
    first = run(store)
    from app.store import SQLiteStore
    again = run(SQLiteStore(store.db_path))
    assert not first['cached'] and again['cached']
    assert len(provider) == len(calls) == 1
    assert again['judgment']['judged'] == 0 and again['judgment']['skipped'] == 1
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM board_reports').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM board_generation_state').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM recording_primary_tag').fetchone()[0] == 1
    assert first['analysis'] == again['analysis'] == GOOD + generation.SECTION + first['allocation_analysis']


def test_concurrent_generation_calls_model_once(store, provider):
    meeting(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(store), range(2)))
    assert sum(r['cached'] for r in results) == 1
    assert len(provider) == 1


@pytest.mark.parametrize('change', ['duration', 'title', 'category', 'tag', 'override_model'])
def test_input_change_invalidates(store, provider, change):
    meeting(store)
    run(store)
    with store._connect() as conn:
        if change == 'duration':
            conn.execute('UPDATE recordings SET duration_min=51')
        elif change == 'title':
            conn.execute("UPDATE recordings SET title='新标题'")
        elif change == 'category':
            conn.execute("UPDATE tags SET category='市场'")
        elif change == 'tag':
            conn.execute("INSERT INTO recording_tags VALUES ('r1', '新增', 2, 'fixture', 'now')")
    result = generation.generate(store, scope='week', day=DAY, model='other' if change == 'override_model' else None)
    assert not result['cached'] and len(provider) == 2


def sample():
    return {'total_minutes': 10000, 'unknown_duration_count': 0, 'by_category': [
        {'name': '产品', 'minutes': 3333, 'share': .3333},
        {'name': '技术', 'minutes': 6667, 'share': .6667}], 'by_tag': []}


def test_text_uses_exact_allocation_percentages_and_no_previous():
    text = board_analysis.week_allocation_text(sample(), model=lambda p: '{"comparison":[]}')
    assert text == '本周时间主要投入技术（66.67%）、产品（33.33%）。上周无对比数据。'
    assert not any(s in text for s in ('上升', '下降', '9 个点'))


def test_comparison_numbers_are_computed_before_model():
    previous = sample()
    previous['by_category'] = [{'name': '产品', 'minutes': 5000, 'share': .5},
                               {'name': '技术', 'minutes': 5000, 'share': .5}]
    text = board_analysis.week_allocation_text(sample(), previous,
                                               model=lambda p: '{"comparison":["技术","产品"]}')
    assert text.endswith('与上周相比，技术上升 16.67 个百分点、产品下降 16.67 个百分点。')


@pytest.mark.parametrize('output', ['本周上升9个点，建议多开会。', '{}', 'null',
    '{"comparison":["财务"]}', '{"comparison":["产品"]}', '{"comparison":[{}]}'])
def test_untrusted_model_cannot_invent_categories_numbers_or_advice(output):
    assert board_analysis.week_allocation_text(sample(), model=lambda p: output) is None


def test_no_model_and_empty_week_do_not_generate(provider):
    assert board_analysis.week_allocation_text(sample()) is None
    assert board_analysis.week_allocation_text(generation.EMPTY, model=lambda p: pytest.fail()) is None


def test_model_failure_preserves_numeric_page_and_old_week_review(board_client, store, monkeypatch, provider):
    meeting(store)
    run(store)
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET duration_min=60')
    def fail(_):
        raise RuntimeError('private-token')
    monkeypatch.setattr(generation, '_requester', lambda *args: fail)
    result = run(store)
    assert result['allocation_analysis'] is None and result['allocation']['total_minutes'] == 60
    assert result['analysis'] == GOOD
    html = block(board_client.get('/board'))
    assert '解读未生成' in html and '<svg' in html and 'private-token' not in html


@pytest.mark.parametrize('existing', [True, False])
def test_conservation_failure_no_bad_pie(board_client, store, monkeypatch, provider, existing):
    meeting(store, minutes=31)
    if existing:
        run(store)
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET duration_min=999')
    def fail(*args):
        raise ValueError('by_category minutes conservation failed')
    monkeypatch.setattr(tag_time, 'week_allocation', fail)
    result = run(store)
    assert result['allocation_error'] == '时间分配计算失败'
    response = board_client.get('/board')
    assert response.context['allocation']['total_minutes'] == (31 if existing else 0)
    html = block(response)
    assert ('<svg' in html) is existing
    assert '时间分配计算失败' in html and '解读未生成' in html
    assert '999' not in html


def test_read_failure_and_corrupt_fallback_render_no_pie(board_client, store, monkeypatch, provider):
    meeting(store)
    run(store)
    with store._connect() as conn:
        conn.execute("UPDATE board_generation_state SET allocation_json='{}'")
    monkeypatch.setattr(tag_time, 'week_allocation', lambda *a: (_ for _ in ()).throw(ValueError()))
    assert '<svg' not in block(board_client.get('/board'))


def test_page_text_current_week_and_stale_text_hidden(board_client, store, provider):
    meeting(store)
    report = run(store)
    assert report['allocation_analysis'] in block(board_client.get('/board?day=2026-10-30'))
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET duration_min=41')
    assert '解读未生成' in block(board_client.get('/board'))


def test_manual_and_scheduler_use_same_pipeline(board_client, store, monkeypatch, provider):
    from app.scheduler_tick import _board
    meeting(store)
    response = board_client.post('/board/regenerate', data={'csrf_token': token(board_client)})
    assert response.status_code == 302
    assert len(provider) == 1
    _board(store, DAY)
    assert len(provider) == 1
    assert '### 时间分配' in board.load_report(store, MONDAY, 'week')['analysis_md']


def test_actual_judgment_and_analysis_models_called_only_once(store, monkeypatch, provider):
    meeting(store)
    with store._connect() as conn:
        conn.execute('DELETE FROM recording_primary_tag')
    from app.models import ModelCredentials
    calls = []
    monkeypatch.setattr(board_analysis, 'resolve_model', lambda s: ModelCredentials('fixture', 'https://fixture.invalid', 'test'))
    monkeypatch.setattr(board_analysis, 'request_text', lambda *a, **kw: calls.append(1) or 'AI Tutor')
    first, second = run(store), run(store)
    assert first['judgment']['judged'] == 1 and first['judgment']['model'] == 1
    assert second['judgment']['skipped'] == 1
    assert len(calls) == len(provider) == 1


def test_daily_repeat_does_not_recall_model(store, monkeypatch):
    add(store)
    calls = []
    monkeypatch.setattr(board_analysis, '_analyze', lambda *a: (calls.append(1) or GOOD, 'test', None))
    for _ in range(2):
        result = generation.generate(store, scope='day', day=DAY)
    assert calls == [1] and result['analysis'] == GOOD and result['cached']


def test_fresh_daily_archive_cannot_skip_missing_week(store, monkeypatch, provider):
    from datetime import datetime
    from app.minutes import SHANGHAI
    from app.scheduler_tick import tick
    from test_board_automation import board_only
    meeting(store)
    board_only(store)
    with store._connect() as conn:
        conn.execute('INSERT INTO board_reports VALUES (?, ?, ?, ?, ?, ?, ?)',
                     ('2026-11-06', 'day', '{}', GOOD, 'fixture', '2026-11-06T21:00:00+08:00', None))
    assert tick(store, now=datetime(2026, 11, 6, 22, 1, tzinfo=SHANGHAI))['executed'] == ['board']
    assert len(provider) == 1 and board.load_report(store, MONDAY, 'week')


def test_failed_allocation_stays_failed_until_regenerated(board_client, store, monkeypatch, provider):
    meeting(store, minutes=30)
    run(store)
    with store._connect() as conn:
        conn.execute('UPDATE recordings SET duration_min=40')
    with monkeypatch.context() as m:
        m.setattr(tag_time, 'week_allocation', lambda *a: (_ for _ in ()).throw(ValueError()))
        run(store)
    response = board_client.get('/board')
    assert response.context['allocation']['total_minutes'] == 30
    assert '最近一次成功结果' in block(response)
    assert not run(store)['allocation_error']
    assert board_client.get('/board').context['allocation']['total_minutes'] == 40
