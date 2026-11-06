from datetime import date, datetime
import hashlib
import json
import sqlite3

import httpx
import pytest

from app import board, board_analysis as analysis
from app.models import ModelCredentials, ModelError


@pytest.fixture
def db(store):
    store.migrate()
    return store


def add(db, ident='r1', stamp='2026-11-06T09:30:00+08:00', minutes=30, title='产品例会',
        summary='讨论产品方案。', tags=('产品',)):
    with db._connect() as conn:
        conn.execute('INSERT INTO recordings (recording_id, title, created_at, duration_min, '
                     'summary_md, synced_at) VALUES (?, ?, ?, ?, ?, ?)',
                     (ident, title, stamp, minutes, summary, stamp))
        for position, tag in enumerate(tags, 1):
            conn.execute('INSERT INTO recording_tags (recording_id, tag, position, model, created_at) '
                         'VALUES (?, ?, ?, ?, ?)', (ident, tag, position, 'fixture', stamp))


def reports(db):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM board_reports ORDER BY day, scope')]


GOOD = '时间主要用于产品例会与技术评审。两者围绕同一产品展开。整体属于方案推进工作。'


def response(text=GOOD):
    return httpx.Response(200, json={'content': [{'type': 'text', 'text': text}]})


@pytest.fixture
def model(monkeypatch):
    calls, sleeps = [], []
    client = httpx.Client
    monkeypatch.setattr(analysis, 'resolve_model', lambda store: ModelCredentials(
        'private-test-key', 'https://model.invalid/anthropic', 'test-model'))
    monkeypatch.setattr(analysis.time, 'sleep', sleeps.append)
    def install(handler=lambda request: response()):
        def capture(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(analysis.httpx, 'Client', lambda **kwargs: client(
            transport=httpx.MockTransport(capture), **kwargs))
        return calls, sleeps
    return install


def test_empty_statistics(db):
    assert board.totals(db) == {'count': 0, 'minutes': 0}
    assert board.day_stats(db, '2026-11-06') == {'count': 0, 'minutes': 0, 'items': []}
    assert board.week_stats(db, date(2026, 11, 2), date(2026, 11, 6)) == {
        'count': 0, 'minutes': 0, 'by_day': []}


@pytest.mark.parametrize('first,second', [
    ('2026-11-06T23:59:00+08:00', '2026-11-07T00:01:00+08:00'),
    ('2026-11-06T15:59:00Z', '2026-11-06T16:01:00+00:00'),
    ('2026-11-06T23:59:00', '2026-11-07T00:01:00')])
def test_shanghai_midnight(db, first, second):
    add(db, 'before', first, 11)
    add(db, 'after', second, 22)
    assert board.day_stats(db, '2026-11-06')['minutes'] == 11
    assert board.day_stats(db, '2026-11-07')['minutes'] == 22


def test_week_boundary_and_zero_days(db):
    add(db, 'previous', '2026-11-01T23:59:00+08:00', 90)
    add(db, 'monday', '2026-11-01T16:00:00Z', 20)
    add(db, 'today', '2026-11-06T23:59:00+08:00', 30)
    add(db, 'future', '2026-11-07T00:00:00+08:00', 70)
    stats = board.week_stats(db, '2026-11-02', '2026-11-06')
    assert stats == {'count': 2, 'minutes': 50, 'by_day': [
        {'day': '2026-11-02', 'count': 1, 'minutes': 20},
        {'day': '2026-11-03', 'count': 0, 'minutes': 0},
        {'day': '2026-11-04', 'count': 0, 'minutes': 0},
        {'day': '2026-11-05', 'count': 0, 'minutes': 0},
        {'day': '2026-11-06', 'count': 1, 'minutes': 30}]}


def test_cross_month_week(db):
    add(db, 'october', '2026-10-31T23:59:00+08:00', 10)
    add(db, 'november', '2026-11-01T00:01:00+08:00', 20)
    assert board.week_stats(db, '2026-10-26', '2026-11-01')['minutes'] == 30


@pytest.mark.parametrize('monday,today', [('2026-11-03', '2026-11-06'),
                                         ('2026-11-02', '2026-11-09'),
                                         ('2026-11-02', '2026-11-01')])
def test_week_requires_explicit_valid_monday(db, monday, today):
    with pytest.raises(ValueError):
        board.week_stats(db, monday, today)


def test_unknown_duration_and_details(db):
    add(db, 'later', '2026-11-06T10:30:00+08:00', None, summary='长' * 500,
        tags=('产品', '技术'))
    add(db, 'earlier', '2026-11-06T01:30:00Z', 12)
    stats = board.day_stats(db, '2026-11-06')
    assert board.totals(db) == {'count': 2, 'minutes': 12}
    assert stats['minutes'] == 12
    assert [item['recording_id'] for item in stats['items']] == ['earlier', 'later']
    assert stats['items'][1] == {'recording_id': 'later', 'title': '产品例会', 'minutes': None,
                                  'created_at': '10:30', 'tags': ['产品', '技术'],
                                  'summary_head': '长' * 120}
    assert board.week_stats(db, '2026-11-02', '2026-11-06')['minutes'] == 12


def test_null_summary_and_no_tags(db):
    add(db, summary=None, tags=())
    item = board.day_stats(db, '2026-11-06')['items'][0]
    assert item['summary_head'] == '' and item['tags'] == []


def test_input_bound_longest_first_and_omission(db, model):
    calls, _ = model()
    for index in range(30):
        add(db, f'r{index}', minutes=index, summary='长' * 3000)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    request = calls[0]
    payload = json.loads(request.content)
    prompt = payload['messages'][0]['content']
    assert len(prompt) + len(payload['system']) <= 8000
    included = [json.loads(line) for line in prompt.splitlines()[1:-1]]
    assert 0 < len(included) < 30
    assert [item['minutes'] for item in included] == list(range(29, 29 - len(included), -1))
    assert all(len(item['summary']) == 300 and item['tags'] == ['产品'] for item in included)
    assert prompt.endswith(f'（另有 {30 - len(included)} 场未纳入）')
    assert result['stats']['count'] == 30
    assert request.url == 'https://model.invalid/anthropic/v1/messages'
    assert request.headers['x-api-key'] == 'private-test-key'
    assert request.headers['anthropic-version'] == '2023-06-01'
    assert payload['model'] == 'test-model'


def test_short_input_retains_chronology_and_unknown_duration(db, model):
    calls, _ = model()
    add(db, minutes=None)
    analysis.generate(db, scope='day', day='2026-11-06', model='override-model')
    payload = json.loads(calls[0].content)
    prompt = payload['messages'][0]['content']
    assert '未纳入' not in prompt and '"minutes": null' in prompt
    assert payload['model'] == 'override-model'


def test_oversized_title_tags_still_bounded(db, model):
    calls, _ = model()
    add(db, title='长' * 9000, tags=('标' * 9000,))
    analysis.generate(db, scope='day', day='2026-11-06')
    payload = json.loads(calls[0].content)
    prompt = payload['messages'][0]['content']
    assert len(prompt) + len(analysis.SYSTEM) <= 8000
    assert '另有 1 场未纳入' in prompt


def test_clean_titles_lists_and_five_sentences(db, model):
    model(lambda _: response('# 今天\n\n- 第一点。\n* 第二点。\n1. 第三点。\n2、第四点。\n3) 第五点。第六点。'))
    add(db)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert result['analysis'] == '第一点。 第二点。 第三点。 第四点。 第五点。'
    assert result['error'] is None
    assert reports(db)[0]['analysis_md'] == result['analysis']


def test_decimal_minutes_are_not_sentence_boundaries():
    assert analysis.clean_analysis('会议用了 1.5 分钟。其余为讨论！最后是总结？四。五。六。') == (
        '会议用了 1.5 分钟。其余为讨论！最后是总结？四。五。')
    assert analysis.clean_analysis('1.5 分钟用于讨论。') == '1.5 分钟用于讨论。'


@pytest.mark.parametrize('text', ['只有一句话。', '第一句。第二句。', 'One. Two. Three.'])
def test_noncompliant_output_is_not_archived_as_analysis(db, model, text):
    calls, sleeps = model(lambda _: response(text))
    add(db)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert result['analysis'] is None and result['error'] == 'invalid_output'
    assert len(calls) == 3 and sleeps == [1, 3]


@pytest.mark.parametrize('kind,error', [('http', 'http_error_503'), ('network', 'request_failed'),
                                        ('empty', 'empty_output'), ('invalid', 'invalid_response'),
                                        ('structure', 'invalid_response')])
def test_model_failures_safe_and_bounded(db, model, kind, error, caplog):
    def handler(request):
        if kind == 'http':
            return httpx.Response(503, text='private-test-key https://sensitive.invalid RESPONSE_BODY')
        if kind == 'network':
            raise httpx.ConnectError('private-test-key https://sensitive.invalid RESPONSE_BODY', request=request)
        if kind == 'invalid':
            return httpx.Response(200, text='RESPONSE_BODY')
        if kind == 'structure':
            return httpx.Response(200, json={'content': 1})
        return response('# 今天\n\n')
    calls, sleeps = model(handler)
    add(db)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert result['analysis'] is None and result['error'] == error
    assert len(calls) == 3 and sleeps == [1, 3]
    assert reports(db)[0]['error'] == error
    for secret in ('private-test-key', 'https://sensitive.invalid', 'RESPONSE_BODY'):
        assert secret not in json.dumps(result) + caplog.text


def test_retry_can_recover(db, model):
    responses = iter([httpx.Response(500), response()])
    calls, sleeps = model(lambda _: next(responses))
    add(db)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert result['analysis'] == GOOD and result['error'] is None
    assert len(calls) == 2 and sleeps == [1]


@pytest.mark.parametrize('broken', [False, True])
def test_unavailable_model(db, monkeypatch, broken):
    add(db)
    def resolve(store):
        if broken:
            raise ModelError('sensitive model configuration')
        return ModelCredentials('', 'https://model.invalid', 'model')
    monkeypatch.setattr(analysis, 'resolve_model', resolve)
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert result['analysis'] is None and result['error'] == 'model_unavailable'
    assert result['stats']['minutes'] == 30


def test_migration_idempotence_composite_key_and_scope_check(db, model):
    model()
    add(db, stamp='2026-11-02T10:00:00+08:00')
    first = analysis.generate(db, scope='day', day='2026-11-02')
    db.migrate()
    db.migrate()
    assert len(reports(db)) == 1
    assert json.loads(reports(db)[0]['stats_json']) == first['stats']
    with db._connect() as conn:
        conn.execute('UPDATE recordings SET duration_min = 60')
    analysis.generate(db, scope='day', day='2026-11-02')
    analysis.generate(db, scope='week', day='2026-11-06')
    saved = reports(db)
    assert len(saved) == 2 and {row['scope'] for row in saved} == {'day', 'week'}
    assert {row['day'] for row in saved} == {'2026-11-02'}
    assert all(json.loads(row['stats_json'])['minutes'] == 60 for row in saved)
    with db._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO board_reports VALUES ('2026-11-06', 'month', '{}', NULL, NULL, 'now', NULL)")


def test_failed_regeneration_clears_old_analysis(db, model):
    model()
    add(db)
    analysis.generate(db, scope='day', day='2026-11-06')
    model(lambda _: response(''))
    result = analysis.generate(db, scope='day', day='2026-11-06')
    assert len(reports(db)) == 1
    assert reports(db)[0]['analysis_md'] is None
    assert reports(db)[0]['error'] == result['error'] == 'empty_output'


def test_week_uses_all_meetings_up_to_requested_day(db, model):
    calls, _ = model()
    add(db, 'monday', '2026-11-02T00:00:00+08:00', title='周一会议')
    add(db, 'friday', title='周五会议')
    add(db, 'saturday', '2026-11-07T00:00:00+08:00', title='周六会议')
    result = analysis.generate(db, scope='week', day='2026-11-06')
    prompt = json.loads(calls[0].content)['messages'][0]['content']
    assert '周一会议' in prompt and '周五会议' in prompt and '周六会议' not in prompt
    assert result['day'] == '2026-11-02' and result['stats']['count'] == 2


def test_cli_print_no_writes_even_without_archive_table(db, model, monkeypatch, capsys):
    calls, _ = model()
    add(db)
    with db._connect() as conn:
        conn.execute('DROP TABLE board_reports')
    before = hashlib.sha256(db.db_path.read_bytes()).hexdigest()
    monkeypatch.setattr(type(db), 'migrate', lambda self: pytest.fail('print must not migrate'))
    assert board.main(['--db', str(db.db_path), '--day', '2026-11-06', '--print']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['totals'] == {'count': 1, 'minutes': 30}
    assert result['day']['analysis'] == result['week']['analysis'] == GOOD
    assert len(calls) == 2
    assert hashlib.sha256(db.db_path.read_bytes()).hexdigest() == before


def test_cli_default_shanghai_today_generates_both(db, model, monkeypatch, capsys):
    model()
    monkeypatch.setenv('DB_PATH', str(db.db_path))
    assert board.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['day']['day'] == datetime.now(board.SHANGHAI).date().isoformat()
    assert {row['scope'] for row in reports(db)} == {'day', 'week'}


def test_cli_no_analysis_never_resolves_model(db, monkeypatch, capsys):
    add(db)
    monkeypatch.setattr(analysis, 'resolve_model', lambda store: pytest.fail('must stay offline'))
    assert board.main(['--db', str(db.db_path), '--day', '2026-11-06', '--print', '--no-analysis']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['day']['error'] == output['week']['error'] == 'analysis_skipped'
    assert reports(db) == []


def test_invalid_scope_does_not_write(db):
    with pytest.raises(ValueError):
        analysis.generate(db, scope='month', day='2026-11-06')
    assert reports(db) == []
