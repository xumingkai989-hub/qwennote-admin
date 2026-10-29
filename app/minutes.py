"""Injectable QwenNote readers. No network requests occur during construction.

MCP lifecycle/transport: https://modelcontextprotocol.io/specification/2025-03-26
Tool names and fields follow the connected QwenNote tool inventory.
"""
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

SHANGHAI = ZoneInfo('Asia/Shanghai')


@dataclass(frozen=True)
class RecordingMeta:
    recording_id: str
    title: str
    created_at: datetime
    duration_min: int | None


@dataclass(frozen=True)
class RecordingDetail:
    recording_id: str
    summary_md: str | None
    transcript_md: str | None
    has_transcript: bool
    has_summary: bool


class MinutesSource(Protocol):
    def list_by_date(self, day: date) -> list[RecordingMeta]: ...

    def get_detail(self, recording_id: str) -> RecordingDetail: ...


class MinutesSourceError(Exception):
    """A safe, credential-free source error; provider bodies are never surfaced."""


def _text(value) -> str:
    if not isinstance(value, str):
        raise ValueError('Expected text')
    return value


def _flag(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError('Expected boolean')
    return value


def _optional_text(value) -> str | None:
    return None if value is None else _text(value)


def _items(payload: dict, *keys: str) -> list:
    if not isinstance(payload, dict):
        raise MinutesSourceError('千问返回的数据结构无效')
    for key in keys:
        if key in payload:
            if not isinstance(payload[key], list) or not all(isinstance(item, dict) for item in payload[key]):
                break
            return payload[key]
    raise MinutesSourceError('千问返回的数据结构无效')


class FixtureMinutesSource:
    """Read *.json files, each containing a recordings array (metadata + detail)."""
    def __init__(self, directory: Path | str):
        directory = Path(directory).expanduser()
        if not directory.is_dir() or not list(directory.glob('*.json')):
            raise ValueError('Minutes fixture directory must contain JSON files')
        self.records: list[RecordingMeta] = []
        self.details: dict[str, RecordingDetail] = {}
        try:
            for path in sorted(directory.glob('*.json')):
                for item in _items(json.loads(path.read_text(encoding='utf-8')), 'recordings'):
                    created_at = datetime.fromisoformat(item['created_at'])
                    if created_at.tzinfo is None:
                        raise ValueError('Timezone required')
                    duration = item['duration_min']
                    if duration is not None and (type(duration) is not int or duration < 0):
                        raise ValueError('Invalid duration')
                    recording_id = _text(item['recording_id'])
                    if not recording_id:
                        raise ValueError('Missing recording ID')
                    self.records.append(RecordingMeta(recording_id, _text(item['title']), created_at.astimezone(SHANGHAI), duration))
                    self.details[recording_id] = RecordingDetail(
                        recording_id, _optional_text(item['summary_md']), _optional_text(item['transcript_md']),
                        _flag(item['has_transcript']), _flag(item['has_summary']),
                    )
        except (OSError, ValueError, KeyError, TypeError, MinutesSourceError):
            raise ValueError('Invalid minutes fixture data') from None

    def list_by_date(self, day: date) -> list[RecordingMeta]:
        return [record for record in self.records if record.created_at.date() == day]

    def get_detail(self, recording_id: str) -> RecordingDetail:
        try:
            return self.details[recording_id]
        except KeyError:
            raise MinutesSourceError('Fixture 中没有该录音详情') from None


def _offset(milliseconds: int) -> str:
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError('Invalid transcript offset')
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{ms:03}'


class HttpMinutesSource:
    URL = 'https://minutes.qwennote.cn/mcp'
    VERSION = '2025-03-26'
    TOOLS = {'list_my_minutes', 'get_minutes_ai_summary', 'get_minutes_transcription'}

    def __init__(self, token: str | None = None, *, transport: httpx.BaseTransport | None = None):
        token = os.environ.get('QWENNOTE_MCP_TOKEN', '') if token is None else token
        if not token.strip():
            raise ValueError('QWENNOTE_MCP_TOKEN must be configured for HttpMinutesSource')
        self._token = token.strip()
        self._transport = transport
        self._session_id: str | None = None
        self._initialized = False
        self._request_id = 0

    def _rpc(self, method: str, params: dict | None = None, *, notification: bool = False) -> dict:
        self._request_id += 1
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = self._request_id
        if params is not None:
            message['params'] = params
        headers = {
            'Authorization': f'Bearer {self._token}',
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': self.VERSION,
        }
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id
        try:
            with httpx.Client(timeout=30, transport=self._transport, follow_redirects=False) as client:
                with client.stream('POST', self.URL, headers=headers, json=message) as response:
                    response.raise_for_status()
                    if notification:
                        return {}
                    if response.headers.get('Mcp-Session-Id'):
                        self._session_id = response.headers['Mcp-Session-Id']
                    if response.headers.get('Content-Type', '').split(';')[0] == 'text/event-stream':
                        payload = None
                        data = []
                        for line in response.iter_lines():
                            if line.startswith('data:'):
                                data.append(line[5:].lstrip(' '))
                            elif not line and data:
                                event = json.loads('\n'.join(data))
                                data = []
                                if isinstance(event, dict) and event.get('id') == message['id']:
                                    payload = event
                                    break
                    else:
                        payload = json.loads(response.read())
                    if (not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0'
                            or payload.get('id') != message['id'] or 'error' in payload
                            or not isinstance(payload.get('result'), dict)):
                        raise MinutesSourceError('千问 MCP 请求失败或响应无效')
                    return payload['result']
        except (httpx.HTTPError, ValueError, TypeError):
            raise MinutesSourceError('千问 MCP 请求失败，请检查凭据与网络配置') from None

    def _initialize(self):
        if self._initialized:
            return
        result = self._rpc('initialize', {
            'protocolVersion': self.VERSION, 'capabilities': {},
            'clientInfo': {'name': 'qwennote-admin', 'version': '0.1.0'},
        })
        if result.get('protocolVersion') != self.VERSION:
            raise MinutesSourceError('千问 MCP 协议版本不兼容')
        self._rpc('notifications/initialized', notification=True)
        names = set()
        cursor = None
        seen = set()
        while True:
            result = self._rpc('tools/list', {'cursor': cursor} if cursor else {})
            names.update(_text(tool['name']) for tool in _items(result, 'tools'))
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise MinutesSourceError('千问工具列表分页游标无效')
            seen.add(cursor)
        if not self.TOOLS <= names:
            raise MinutesSourceError('千问 MCP 缺少必需的只读工具')
        self._initialized = True

    def _call(self, name: str, arguments: dict) -> dict:
        result = self._rpc('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise MinutesSourceError('千问工具调用失败')
        payload = result.get('structuredContent')
        if payload is None:
            blocks = result.get('content', [])
            if not isinstance(blocks, list) or not all(isinstance(block, dict) for block in blocks):
                raise MinutesSourceError('千问工具返回的数据无效')
            payload = json.loads('\n'.join(b['text'] for b in blocks if b.get('type') == 'text'))
        if not isinstance(payload, dict) or payload.get('success') is False:
            raise MinutesSourceError('千问工具返回的数据无效')
        if 'data' in payload:
            payload = payload['data']
        if not isinstance(payload, dict):
            raise MinutesSourceError('千问工具返回的数据无效')
        return payload

    def _pages(self, name: str, arguments: dict):
        seen = set()
        while True:
            payload = self._call(name, arguments)
            yield payload
            if not _flag(payload.get('hasMore', False)):
                break
            cursor = payload.get('nextToken')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise MinutesSourceError('千问返回的分页游标无效')
            seen.add(cursor)
            arguments = {**arguments, 'nextToken': cursor}

    def list_by_date(self, day: date) -> list[RecordingMeta]:
        try:
            self._initialize()
            start = datetime.combine(day, time.min, SHANGHAI)
            arguments = {
                # API bounds are exclusive; subtract 1ms to include midnight.
                'createdAfter': int(start.timestamp() * 1000) - 1,
                'createdBefore': int((start + timedelta(days=1)).timestamp() * 1000),
                'pageSize': 20,
            }
            records = []
            for page in self._pages('list_my_minutes', arguments):
                for item in _items(page, 'items', 'minutes'):
                    created_at = datetime.fromtimestamp(item['gmtCreate'] / 1000, SHANGHAI)
                    if created_at.date() != day:
                        continue
                    duration = item.get('durationMillis')
                    if duration is not None and (type(duration) not in (int, float) or duration < 0):
                        raise ValueError('Invalid duration')
                    recording_id = _text(item['uuid'])
                    if not recording_id:
                        raise ValueError('Missing recording ID')
                    records.append(RecordingMeta(recording_id, _text(item['title']), created_at,
                                                 None if duration is None else int((duration + 30000) // 60000)))
            return records
        except (MinutesSourceError, KeyError, ValueError, TypeError, OverflowError, OSError):
            self._initialized = False
            self._session_id = None
            raise MinutesSourceError('千问录音列表拉取失败，请检查凭据、网络或数据格式') from None

    def get_detail(self, recording_id: str) -> RecordingDetail:
        try:
            self._initialize()
            summary = self._call('get_minutes_ai_summary', {'taskUuid': recording_id})
            has_summary = _flag(summary['hasSummary'])
            summary_md = _optional_text(summary.get('fullSummary'))
            paragraphs = []
            has_transcript = False
            for page in self._pages('get_minutes_transcription', {
                'taskUuid': recording_id, 'direction': 0, 'maxResults': 500,
            }):
                items = _items(page, 'paragraphs', 'items')
                has_transcript = has_transcript or _flag(page.get('hasTranscript', bool(items)))
                for paragraph in items:
                    paragraphs.append(
                        f"[{_offset(paragraph['startTime'])}–{_offset(paragraph['endTime'])}] "
                        f"{_text(paragraph['speakerName'])}：{_text(paragraph['content'])}"
                    )
            return RecordingDetail(recording_id, summary_md if has_summary else None,
                                   '\n\n'.join(paragraphs) if has_transcript else None,
                                   has_transcript, has_summary)
        except (MinutesSourceError, KeyError, ValueError, TypeError):
            self._initialized = False
            self._session_id = None
            raise MinutesSourceError('千问录音详情拉取失败，请检查凭据、网络或数据格式') from None
