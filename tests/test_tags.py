import asyncio
from dataclasses import replace
from datetime import date, datetime
import json
import sqlite3

import httpx
import pytest

from app import tag
from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.sync import run_sync
from fakes import FakeMinutesSource
from test_login import login


@pytest.fixture
def tagged_store(store):
    store.migrate()
    for index in range(3):
        key = f'tag-{index}'
        store.upsert_recording(RecordingMeta(key, f'会议{index}', datetime.now(SHANGHAI), 30),
                               RecordingDetail(key, '总结', '转写', True, True), tag.now())
    return store


def response(tags=('AI Tutor', '模考系统', '导师中心'), *, text=None):
    return httpx.Response(200, json={'content': [{'type': 'text', 'text': text if text is not None else
                                                json.dumps({'tags': tags}, ensure_ascii=False)}],
                                    'usage': {'input_tokens': 40, 'output_tokens': 10,
                                              'cache_read_input_tokens': 20}})


def run(store, handler=None, **kwargs):
    return tag.run_tags(store, settings=tag.TagSettings('private-test-key'),
                        transport=httpx.MockTransport(handler or (lambda request: response())), **kwargs)


def rows(store, table='recording_tags'):
    with store._connect() as conn:
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]


def test_three_tags_order_model_and_usage(tagged_store):
    requests = []
    def handler(request):
        requests.append(request)
        return response()
    result = run(tagged_store, handler, recording_ids=['tag-0'])
    labels = rows(tagged_store)
    assert [row['tag'] for row in labels] == ['AI Tutor', '模考系统', '导师中心']
    assert [row['position'] for row in labels] == [1, 2, 3]
    assert all(row['model'] == 'deepseek-flash' and row['created_at'] for row in labels)
    assert result['attempted'] == result['succeeded'] == 1 and result['failed'] == 0
    assert result['input_tokens'] == 40 and result['output_tokens'] == 10
    assert result['cache_read_input_tokens'] == 20
    assert rows(tagged_store, 'tag_runs')[0] == result
    assert requests[0].url == 'https://api.deepseek.com/anthropic/v1/messages'
    body = json.loads(requests[0].content)
    assert body['model'] == 'deepseek-flash' and body['thinking']['type'] == 'disabled'
    assert requests[0].headers['x-api-key'] == 'private-test-key'


def test_seven_tags_truncated_and_duplicates_removed(tagged_store):
    result = run(tagged_store, lambda _: response(list(tag.VOCABULARY[:2]) + list(tag.VOCABULARY[:7])),
                 recording_ids=['tag-0'])
    assert [row['tag'] for row in rows(tagged_store)] == list(tag.VOCABULARY[:5])
    assert result['succeeded'] == 1


@pytest.mark.parametrize('labels,warn', [(['其他:产品探索'], False), (['词表外'], True),
                                        (['其他:一二三四五六七'], True),
                                        (['其他:产品', '其他:工程'], True)])
def test_unknown_accepted_with_safe_warning(tagged_store, caplog, labels, warn):
    result = run(tagged_store, lambda _: response(labels), recording_ids=['tag-0'])
    assert [row['tag'] for row in rows(tagged_store)] == labels
    assert result['succeeded'] == 1
    assert bool(caplog.records) is warn
    assert all(labels[0] not in record.message for record in caplog.records)


@pytest.mark.parametrize('invalid', ['not json', '```json\n{"tags":["AI Tutor"]}\n```',
                                    '{"tags":[]}', '{"tags":[1]}', '{"tags":[" "]}',
                                    '{"tags":["AI Tutor"],"extra":true}'])
def test_invalid_json_retries_once_then_empty(tagged_store, caplog, invalid):
    calls = []
    result = run(tagged_store, lambda request: calls.append(request) or response(text=invalid),
                 recording_ids=['tag-0'])
    assert len(calls) == 2
    assert result['failed'] == 1 and result['error'] == 'invalid_json_after_retry'
    assert rows(tagged_store) == []
    assert result['input_tokens'] == 80  # Count both paid responses.
    assert 'private-test-key' not in caplog.text


def test_json_retry_can_recover(tagged_store):
    responses = iter([response(text='bad'), response()])
    assert run(tagged_store, lambda _: next(responses), recording_ids=['tag-0'])['succeeded'] == 1


def test_all_skip_force_rebuild_and_failed_force_clear(tagged_store, monkeypatch, capsys):
    run(tagged_store)
    original_run = tag.run_tags
    monkeypatch.setenv('DB_PATH', str(tagged_store.db_path))
    def fake(store, **kwargs):
        return original_run(store, settings=tag.TagSettings('private-test-key'),
                            transport=httpx.MockTransport(lambda _: response(['周会/例会'])), **kwargs)
    monkeypatch.setattr(tag, 'run_tags', fake)
    assert tag.main(['--all']) == 0
    assert json.loads(capsys.readouterr().out)['attempted'] == 0
    assert len(rows(tagged_store)) == 9
    assert tag.main(['--all', '--force']) == 0
    assert json.loads(capsys.readouterr().out)['succeeded'] == 3
    assert len(rows(tagged_store)) == 3
    assert {row['tag'] for row in rows(tagged_store)} == {'周会/例会'}
    monkeypatch.setattr(tag, 'run_tags', original_run)
    result = run(tagged_store, lambda _: response(text='bad'), force=True)
    assert result['failed'] == 3 and rows(tagged_store) == []


def test_missing_key_sync_succeeds_one_warning(tagged_store, monkeypatch, caplog):
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)
    source = FakeMinutesSource([RecordingMeta('new', '新会议', datetime.now(SHANGHAI), None)],
                               {'new': RecordingDetail('new', '', '', False, False)})
    result = run_sync(source, tagged_store, backfill_days=1)
    assert result.status == 'success' and result.inserted == 1
    assert len(caplog.records) == 1 and '跳过' in caplog.text
    assert rows(tagged_store) == []
    assert rows(tagged_store, 'tag_runs')[0]['attempted'] == 0


def test_input_truncation_and_env(monkeypatch, tagged_store):
    recording = replace(tagged_store.get_recording('tag-0'), summary_md='总'*1501, transcript_md='转'*2001)
    content = json.loads(tag.model_input(recording))
    assert content == {'标题': '会议0', '总结': '总'*1500+'\n[已截断]', '转写': '转'*2000+'\n[已截断]'}
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'private-test-key')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://example.invalid/anthropic')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'custom-model')
    settings = tag.TagSettings.from_env()
    assert settings.auth_token == 'private-test-key' and settings.model == 'custom-model'
    assert settings.base_url == 'https://example.invalid/anthropic'
    assert 'private-test-key' not in repr(settings)


@pytest.mark.parametrize('recover', [True, False])
def test_429_two_backoff_retries(tagged_store, monkeypatch, recover):
    delays, calls = [], []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(tag.asyncio, 'sleep', sleep)
    def handler(request):
        calls.append(request)
        return response() if recover and len(calls) == 3 else httpx.Response(429)
    result = run(tagged_store, handler, recording_ids=['tag-0'])
    assert len(calls) == 3 and delays == [1, 2]
    assert result['succeeded'] == int(recover) and result['failed'] == int(not recover)


def test_concurrency_two_and_single_timeout_does_not_stop_rest(tagged_store, monkeypatch):
    active = peak = 0
    monkeypatch.setattr(tag, 'ITEM_TIMEOUT', .05)
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            content = json.loads(json.loads(request.content)['messages'][0]['content'])
            await asyncio.sleep(.2 if content['标题'] == '会议0' else .01)
            return response()
        finally:
            active -= 1
    result = run(tagged_store, handler)
    assert peak == 2 and active == 0
    assert (result['attempted'], result['succeeded'], result['failed']) == (3, 2, 1)
    assert result['error'] == 'timeout_30s'


def test_provider_errors_do_not_leak_and_single_failure_isolated(tagged_store, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError('private-test-key provider secrets')
        return response()
    result = run(tagged_store, handler)
    assert result['failed'] == 1 and result['succeeded'] == 2
    assert 'private-test-key' not in str(result) + caplog.text


def test_sync_only_new_or_model_input_changed(tagged_store, monkeypatch):
    original_run = tag.run_tags
    results = []
    def fake(store, **kwargs):
        result = original_run(store, settings=tag.TagSettings('private-test-key'),
                              transport=httpx.MockTransport(lambda _: response()), **kwargs)
        results.append(result)
        return result
    monkeypatch.setattr(tag, 'run_tags', fake)
    meta = RecordingMeta('new', '标题', datetime.now(SHANGHAI), 30)
    detail = RecordingDetail('new', '总结', '转写', True, True)
    source = FakeMinutesSource([meta], {'new': detail})
    for field, value in [(None, None), (None, None), ('duration_min', 40), ('title', '更正')]:
        if field:
            source.recordings[0] = replace(source.recordings[0], **{field: value})
        assert run_sync(source, tagged_store, backfill_days=1).status == 'success'
    for field in ['summary_md', 'transcript_md']:
        source.details['new'] = replace(source.details['new'], **{field: '更正内容'})
        run_sync(source, tagged_store, backfill_days=1)
    assert [result['attempted'] for result in results] == [1, 0, 0, 1, 1, 1]


def test_cascade_migration_and_stale_response_guard(tagged_store):
    run(tagged_store)
    tagged_store.migrate()
    assert len(rows(tagged_store)) == 9
    recording = tagged_store.get_recording('tag-0')
    with tagged_store._connect() as conn:
        conn.execute("DELETE FROM recordings WHERE recording_id = 'tag-0'")
    assert len(rows(tagged_store)) == 6
    with pytest.raises(ValueError):
        tagged_store.replace_tags(recording, ['述职'], 'deepseek-flash', tag.now())
    assert tagged_store.list_recordings(date.min, date.max).items[0].tags


def test_home_tags_after_status_overflow_and_escaping(client, store):
    store.upsert_recording(RecordingMeta('page', '测试', datetime.now(SHANGHAI), 30),
                           RecordingDetail('page', '总结', '转写', True, True), tag.now())
    assert login(client).status_code == 302
    assert 'class="recording-tags"' not in client.get('/').text
    run(store, lambda _: response(['AI Tutor', '模考系统', '<script>x</script>', '导师中心', '述职']))
    html = client.get('/').text
    assert html.index('已转录</span>') < html.index('class="recording-tags"') < html.index('class="card-created"')
    assert html.count('class="tag-pill"') == 4 and '>+2</span>' in html
    assert 'title="AI Tutor · 模考系统 · &lt;script&gt;x&lt;/script&gt; · 导师中心 · 述职"' in html
    assert '<script>x</script>' not in html


def test_total_timeout_includes_rate_limit_backoff(tagged_store, monkeypatch):
    monkeypatch.setattr(tag, 'ITEM_TIMEOUT', .01)
    calls = []
    result = run(tagged_store, lambda request: calls.append(request) or httpx.Response(429),
                 recording_ids=['tag-0'])
    assert len(calls) == 1 and result['error'] == 'timeout_30s'


def test_changed_content_clears_old_tags_and_rejects_inflight_result(tagged_store):
    run(tagged_store, recording_ids=['tag-0'])
    before = tagged_store.get_recording('tag-0')
    tagged_store.upsert_recording(RecordingMeta('tag-0', before.title, before.created_at, 30),
                                  RecordingDetail('tag-0', '内容改变', '转写', True, True), tag.now())
    assert tagged_store.get_recording('tag-0').tags == ()
    with pytest.raises(ValueError):
        tagged_store.replace_tags(before, ['述职'], 'deepseek-flash', tag.now())
    assert rows(tagged_store) == []


def test_http_error_and_malformed_envelope_are_safe(tagged_store, caplog):
    result = run(tagged_store, lambda _: httpx.Response(401, text='private-test-key'),
                 recording_ids=['tag-0'])
    assert result['error'] == 'http_401' and 'private-test-key' not in caplog.text
    calls = []
    result = run(tagged_store, lambda request: calls.append(request) or httpx.Response(200, json={'content': None}),
                 recording_ids=['tag-0'])
    assert len(calls) == 2 and result['error'] == 'invalid_json_after_retry'


def test_concurrent_batches_are_idempotent(tagged_store):
    from concurrent.futures import ThreadPoolExecutor
    async def handler(request):
        await asyncio.sleep(.01)
        return response()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, tagged_store, handler) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(result['attempted'] for result in results) == [0, 3]
    assert len(rows(tagged_store)) == 9 and len(rows(tagged_store, 'tag_runs')) == 2


def test_systemd_loads_optional_tag_environment():
    from pathlib import Path
    for name in ['qwennote.service', 'qwennote-sync.service']:
        assert 'EnvironmentFile=-/etc/claude/deepseek.qwennote.env' in (
            Path(__file__).parents[1] / 'deploy/systemd' / name).read_text()
