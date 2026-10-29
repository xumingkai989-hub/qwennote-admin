import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.minutes import FixtureMinutesSource, HttpMinutesSource, MinutesSourceError

DAY = date(2026, 10, 29)
FIXTURES = Path(__file__).parent / 'fixtures/minutes'


def test_missing_token_raises_at_construction(monkeypatch):
    monkeypatch.delenv('QWENNOTE_MCP_TOKEN', raising=False)
    with pytest.raises(ValueError, match='QWENNOTE_MCP_TOKEN'):
        HttpMinutesSource()


def test_blank_token_is_rejected():
    with pytest.raises(ValueError, match='QWENNOTE_MCP_TOKEN'):
        HttpMinutesSource('  ')


def test_fixture_reads_metadata_detail_and_empty_day():
    source = FixtureMinutesSource(FIXTURES)
    records = source.list_by_date(DAY)
    assert len(records) == 2
    assert records[0].created_at.tzinfo == ZoneInfo('Asia/Shanghai')
    assert records[0].duration_min == 18
    assert source.get_detail(records[0].recording_id).has_transcript
    assert source.list_by_date(date(2000, 1, 1)) == []
    with pytest.raises(MinutesSourceError):
        source.get_detail('missing')


def test_fixture_missing_directory_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match='fixture'):
        FixtureMinutesSource(tmp_path / 'missing')


@pytest.mark.parametrize('sse', [False, True])
def test_mcp_handshake_pagination_timezone_and_full_markdown(sse):
    calls = []
    start = int(datetime(2026, 10, 29, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)

    def respond(request):
        assert str(request.url) == 'https://minutes.qwennote.cn/mcp'
        assert request.headers['Authorization'] == 'Bearer unit-test-only'
        assert 'text/event-stream' in request.headers['Accept']
        rpc = json.loads(request.content)
        calls.append(rpc)
        method = rpc['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}}
        elif method == 'notifications/initialized':
            return httpx.Response(202)
        elif method == 'tools/list':
            result = {'tools': [{'name': n} for n in ('list_my_minutes', 'get_minutes_ai_summary', 'get_minutes_transcription')]}
        else:
            assert request.headers['Mcp-Session-Id'] == 'test-session'
            args = rpc['params']['arguments']
            name = rpc['params']['name']
            if name == 'list_my_minutes':
                assert args['createdAfter'] == start - 1
                assert args['createdBefore'] == start + 86400000
                result = {'items': [{'uuid': 'r1', 'title': '标题', 'gmtCreate': start, 'durationMillis': 1080000}], 'hasMore': 'nextToken' not in args, 'nextToken': 'list-page-2'}
                if 'nextToken' in args:
                    assert args['nextToken'] == 'list-page-2'
                    result['items'] = []
            elif name == 'get_minutes_ai_summary':
                assert args == {'taskUuid': 'r1'}
                result = {'hasSummary': True, 'fullSummary': '## 完整总结\n\n原样保留'}
            else:
                assert args['taskUuid'] == 'r1' and args['direction'] == 0
                result = {'hasTranscript': True, 'paragraphs': [{'speakerName': '发言人 1', 'startTime': 26600, 'endTime': 30000, 'content': '第一段' if 'nextToken' not in args else '第二段'}], 'hasMore': 'nextToken' not in args, 'nextToken': 'transcript-page-2'}
                if 'nextToken' in args:
                    assert args['nextToken'] == 'transcript-page-2'
            result = {'content': [{'type': 'text', 'text': json.dumps(result)}]}
        envelope = {'jsonrpc': '2.0', 'id': rpc['id'], 'result': result}
        if sse:
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream', 'Mcp-Session-Id': 'test-session'}, text='event: message\ndata: '+json.dumps(envelope)+'\n\n')
        return httpx.Response(200, headers={'Mcp-Session-Id': 'test-session'}, json=envelope)

    source = HttpMinutesSource('unit-test-only', transport=httpx.MockTransport(respond))
    records = source.list_by_date(DAY)
    assert len(records) == 1
    assert records[0].created_at.isoformat() == '2026-10-29T00:00:00+08:00'
    assert records[0].duration_min == 18
    detail = source.get_detail('r1')
    assert detail.summary_md == '## 完整总结\n\n原样保留'
    assert detail.has_transcript and detail.has_summary
    assert '[00:00:26.600–00:00:30.000] 发言人 1：第一段' in detail.transcript_md
    assert '第二段' in detail.transcript_md
    assert [r['method'] for r in calls[:3]] == ['initialize', 'notifications/initialized', 'tools/list']


@pytest.mark.parametrize('failure', ['http', 'rpc', 'malformed'])
def test_mcp_errors_are_explicit_and_do_not_expose_provider_body(failure):
    def respond(request):
        rpc = json.loads(request.content)
        if failure == 'http':
            return httpx.Response(401, text='private-provider-credential')
        if failure == 'rpc':
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': rpc['id'], 'error': {'message': 'private-provider-credential'}})
        return httpx.Response(200, text='private-provider-credential')
    source = HttpMinutesSource('unit-test-only', transport=httpx.MockTransport(respond))
    with pytest.raises(MinutesSourceError) as error:
        source.list_by_date(DAY)
    assert 'private-provider' not in str(error.value)


@pytest.mark.parametrize('tool_result', [
    {'isError': True, 'content': [{'type': 'text', 'text': 'private-provider-credential'}]},
    {'structuredContent': {'items': [], 'hasMore': True}},
    {'structuredContent': {'items': [], 'hasMore': True, 'nextToken': 'repeat'}},
    {'structuredContent': {'items': 'invalid'}},
    {'structuredContent': {'success': False}},
    {'content': [{'type': 'text', 'text': 'invalid-json'}]},
    {'content': [None]},
])
def test_invalid_tool_payload_fails_instead_of_becoming_an_empty_list(tool_result):
    def respond(request):
        rpc = json.loads(request.content)
        if rpc['method'] == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif rpc['method'] == 'notifications/initialized':
            return httpx.Response(202)
        elif rpc['method'] == 'tools/list':
            result = {'tools': [{'name': n} for n in HttpMinutesSource.TOOLS]}
        else:
            result = tool_result
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': rpc['id'], 'result': result})
    source = HttpMinutesSource('unit-test-only', transport=httpx.MockTransport(respond))
    with pytest.raises(MinutesSourceError):
        source.list_by_date(DAY)


def test_malformed_fixture_is_explicit(tmp_path):
    (tmp_path / 'bad.json').write_text('{invalid')
    with pytest.raises(ValueError, match='fixture'):
        FixtureMinutesSource(tmp_path)
