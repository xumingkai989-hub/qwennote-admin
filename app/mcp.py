"""QwenNote MCP reader and file-backed, rotating OAuth credentials.

Provider responses and credentials must never appear in exceptions or logs.
"""
import fcntl
import json
import os
import tempfile
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx

from app.minutes import (HttpMinutesSource, MinutesSourceError, RecordingDetail,
                         RecordingMeta, SHANGHAI, _flag, _items, _optional_text, _text)


class TokenRefreshError(MinutesSourceError):
    """Fatal authentication failure, distinct from an unavailable recording."""


class OAuthTokenManager:
    URL = 'https://minutes.qwennote.cn/token'

    def __init__(self, path: Path | str, *, transport=None, now=None):
        self.path = Path(path).expanduser()
        self._transport = transport
        self._now = now or (lambda: datetime.now(SHANGHAI))
        self.refresh_count = 0
        self._read()  # Fail startup for a missing/malformed credential file.

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            if not all(isinstance(data.get(k), str) and data[k] for k in
                       ('client_id', 'access_token', 'refresh_token')):
                raise ValueError()
            for key in ('obtained_at', 'expires_in'):
                if type(data.get(key)) not in (int, float) or data[key] < 0:
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise TokenRefreshError('千问 OAuth token 文件缺失或格式无效') from None

    def _expired(self, data):
        return self._now().timestamp() >= data['obtained_at'] + data['expires_in'] - 120

    def access_token(self, *, rejected_token: str | None = None):
        data = self._read()
        if not self._expired(data) and (rejected_token is None or data['access_token'] != rejected_token):
            return data['access_token']
        try:
            # Lock a stable sidecar inode: the credential file itself is atomically replaced.
            fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = self._read()
                if not self._expired(data) and (rejected_token is None or data['access_token'] != rejected_token):
                    return data['access_token']
                with httpx.Client(timeout=30, transport=self._transport, follow_redirects=False) as client:
                    response = client.post(self.URL, data={
                        'grant_type': 'refresh_token', 'client_id': data['client_id'],
                        'refresh_token': data['refresh_token'],
                    })
                    response.raise_for_status()
                    fresh = response.json()
                if not isinstance(fresh.get('access_token'), str) or not fresh['access_token']:
                    raise ValueError()
                if type(fresh.get('expires_in')) not in (int, float) or fresh['expires_in'] <= 120:
                    raise ValueError()
                if 'refresh_token' in fresh and (not isinstance(fresh['refresh_token'], str) or not fresh['refresh_token']):
                    raise ValueError()
                data.update(access_token=fresh['access_token'], expires_in=fresh['expires_in'],
                            obtained_at=self._now().timestamp())
                if 'refresh_token' in fresh:
                    data['refresh_token'] = fresh['refresh_token']
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as output:
                        temporary = output.name
                        os.fchmod(output.fileno(), 0o600)
                        json.dump(data, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, self.path)
                    temporary = None
                finally:
                    if temporary:
                        os.unlink(temporary)
                self.refresh_count += 1
                return data['access_token']
        except TokenRefreshError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
            raise TokenRefreshError('千问 OAuth 续期失败，请检查 token 文件、授权或网络后重试') from None


class McpMinutesSource(HttpMinutesSource):
    TOOLS = HttpMinutesSource.TOOLS | {'get_minutes_basic_info'}

    def __init__(self, *, token_file=None, url=None, transport=None, now=None, sleep=None):
        self.URL = url or os.environ.get('QWENNOTE_MCP_URL', self.URL)
        self._transport = transport
        self._now = now or (lambda: datetime.now(SHANGHAI))
        self._sleep = sleep or clock.sleep
        self.tokens = OAuthTokenManager(token_file or os.environ.get(
            'QWENNOTE_TOKEN_FILE', '/opt/qwennote/data/mcp_token.json'), transport=transport, now=self._now)
        self._session_id = None
        self._initialized = False
        self._request_id = 0

    @property
    def refresh_count(self):
        return self.tokens.refresh_count

    def _rpc(self, method, params=None, *, notification=False):
        self._request_id += 1
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = self._request_id
        if params is not None:
            message['params'] = params
        retries = 0
        refreshed = False
        while True:
            token = self.tokens.access_token()
            headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': self.VERSION}
            if self._session_id:
                headers['Mcp-Session-Id'] = self._session_id
            try:
                with httpx.Client(timeout=30, transport=self._transport, follow_redirects=False) as client:
                    response = client.post(self.URL, headers=headers, json=message)
                if response.status_code == 401 and not refreshed:
                    self.tokens.access_token(rejected_token=token)
                    refreshed = True
                    continue
                response.raise_for_status()
                if response.headers.get('Mcp-Session-Id'):
                    self._session_id = response.headers['Mcp-Session-Id']
                if notification:
                    return {}
                if response.headers.get('content-type', '').split(';')[0] == 'text/event-stream':
                    payload = None
                    # Qwen emits one JSON envelope per data line, sometimes with no blank terminator.
                    for line in response.text.splitlines():
                        if not line.startswith('data:'):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == '[DONE]':
                            continue
                        event = json.loads(raw)
                        if isinstance(event, dict) and event.get('id') == message['id']:
                            payload = event
                            break
                else:
                    payload = response.json()
                if isinstance(payload, dict) and 'error' in payload:
                    raise MinutesSourceError(self._rpc_error_message(payload['error']))
                if (not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0'
                        or payload.get('id') != message['id'] or 'error' in payload
                        or not isinstance(payload.get('result'), dict)):
                    raise MinutesSourceError('千问 MCP 请求失败或响应无效')
                return payload['result']
            except TokenRefreshError:
                raise
            except httpx.HTTPError as exc:
                retryable = (not isinstance(exc, httpx.HTTPStatusError)
                             or exc.response.status_code in (408, 429)
                             or exc.response.status_code >= 500)
                # An append-only write may have succeeded before the connection failed.
                # It has no idempotency key, so never retry it automatically.
                append_hotwords = (method == 'tools/call' and params
                                   and params.get('name') == 'add_personal_hot_word')
                if retryable and not append_hotwords and retries < 2:
                    self._sleep(2 ** retries)
                    retries += 1
                    continue
                raise MinutesSourceError('千问 MCP 请求失败，请检查授权与网络') from None
            except (ValueError, TypeError):
                raise MinutesSourceError('千问 MCP 响应格式无效') from None

    def _pages(self, name, arguments):
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
            arguments = {**arguments, 'cursor' if name == 'list_my_minutes' else 'nextToken': cursor}

    def _range(self, start, end):
        try:
            self._initialize()
            records = []
            seen = set()
            for page in self._pages('list_my_minutes', {
                    'startTime': int(start.timestamp()*1000), 'endTime': int(end.timestamp()*1000),
                    'pageSize': 100}):
                for item in _items(page, 'items'):
                    created = datetime.fromtimestamp(item['gmtCreate']/1000, SHANGHAI)
                    if not start <= created <= end:
                        continue
                    uuid = _text(item['uuid'])
                    duration = item.get('durationMillis')
                    if not uuid or (duration is not None and (type(duration) not in (int, float) or duration < 0)):
                        raise ValueError()
                    if uuid not in seen:
                        records.append(RecordingMeta(uuid, _text(item['title']), created,
                            None if duration is None else int((duration+30000)//60000),
                            _optional_text(item.get('mediaType'))))
                        seen.add(uuid)
            return records
        except TokenRefreshError:
            raise
        except (MinutesSourceError, KeyError, ValueError, TypeError, OverflowError, OSError):
            raise MinutesSourceError('千问录音列表拉取失败，请检查授权、网络或数据格式') from None

    def sync_window(self, days: int):
        if type(days) is not int or days < 1:
            raise ValueError('days must be a positive integer')
        now = self._now()
        return self._range(datetime.combine(now.date()-timedelta(days=days), time.min, SHANGHAI), now)

    def list_by_date(self, day: date):
        start = datetime.combine(day, time.min, SHANGHAI)
        return [m for m in self._range(start, start+timedelta(days=1)) if m.created_at.date() == day]

    def get_detail(self, recording_id):
        try:
            self._initialize()
            basic = self._call('get_minutes_basic_info', {'taskUuid': recording_id})
            summary = self._call('get_minutes_ai_summary', {'taskUuid': recording_id})
            has_summary = _flag(summary['hasSummary'])
            paragraphs = []
            filtered = False
            for page in self._pages('get_minutes_transcription', {
                    'taskUuid': recording_id, 'maxResults': 500, 'direction': 0}):
                filtered = filtered or _flag(page.get('filtered', False))
                paragraphs.extend(_items(page, 'paragraphs'))
            has_transcript = bool(paragraphs) and not filtered
            transcript = ''.join(
                f"[{_relative(p['startTime'])}–{_relative(p['endTime'])}] {_text(p['speakerName'])}\n\n"
                f"{_text(p['content'])}\n\n" for p in paragraphs) if has_transcript else None
            return RecordingDetail(recording_id,
                _optional_text(summary.get('fullSummary')) if has_summary else None,
                transcript, has_transcript, has_summary, _optional_text(basic.get('mediaType')))
        except TokenRefreshError:
            raise
        except (MinutesSourceError, KeyError, ValueError, TypeError):
            raise MinutesSourceError('千问录音详情拉取失败，请检查授权、网络或数据格式') from None

    def get_media_url(self, recording_id):
        """Optional metadata lookup; an unavailable private recording is a normal result."""
        self._initialize()
        return self._call('get_minutes_media_url', {'taskUuid': recording_id})


def _relative(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError('Invalid transcript offset')
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes:02}:{seconds:02}.{ms:03}'
