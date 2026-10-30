"""Explicit, one-way hotword publishing. Each append contains exactly one standard term."""
import fcntl
import json
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

from app.mcp import McpMinutesSource
from app.minutes import HttpMinutesSource, MinutesSourceError


REASONS = {
    'already_synced': '已推送，标准词未变', 'invalid_chars': '含中英文、数字、空格以外的字符',
    'too_long': '超过千问长度限制', 'disabled': '热词已停用',
    'upstream_failed': '千问调用失败，请检查连接后重试',
    'quota_full': '千问热词配额已满', 'not_found': '热词不存在',
}
HAN = '\u3400-\u4dbf\u4e00-\u9fff'


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return ''.join(unicodedata.normalize('NFKC', value).casefold().split())


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS hotword_upstream_sync (
        hotword_id INTEGER PRIMARY KEY, upstream_value TEXT NOT NULL, synced_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ok','skipped','failed')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hotword_upstream_errors (
        hotword_id INTEGER PRIMARY KEY, message TEXT NOT NULL)""")
    # Keep an append-only attempt history: the per-word table alone loses earlier results.
    conn.execute("""CREATE TABLE IF NOT EXISTS hotword_upstream_runs (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL, plan_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hotword_upstream_state (
        id INTEGER PRIMARY KEY CHECK(id=1), snapshot_json TEXT NOT NULL)""")


@contextmanager
def sync_lock(store):
    with open(str(store.db_path) + '.hotword-upstream.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class HotwordTools:
    TOOLS = {'list_hot_words', 'add_personal_hot_word'}

    def _rpc_error_message(self, error):
        message = error.get('message') if isinstance(error, dict) else None
        return error_summary(message if isinstance(message, str) else REASONS['upstream_failed'], self)

    def list_hot_words(self):
        try:
            self._initialize()
            value = self._call('list_hot_words', {})
            words, count, maximum = value['hotWords'], value['currentCount'], value['maxCount']
            if (not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words)
                    or type(count) is not int or type(maximum) is not int
                    or not 0 <= count <= maximum <= 200 or len(words) != count):
                raise ValueError()
            return dict(hotWords=words, currentCount=count, maxCount=maximum, checked_at=now())
        except (MinutesSourceError, KeyError, TypeError, ValueError, OSError):
            raise MinutesSourceError('千问热词读取失败，请检查授权、网络或数据格式。') from None

    def add_personal_hot_word(self, terms):
        try:
            self._initialize()
            # Use the existing transport (including its non-retry rule), but retain
            # the tool's actual error message for this explicit administrator action.
            result = self._rpc('tools/call', {'name': 'add_personal_hot_word',
                                             'arguments': {'hotWordList': terms}})
            blocks = result.get('content', [])
            text = '\n'.join(b['text'] for b in blocks if isinstance(b, dict)
                             and b.get('type') == 'text' and isinstance(b.get('text'), str))
            payload = result.get('structuredContent')
            if result.get('isError'):
                raise MinutesSourceError(error_summary(text or str(payload or REASONS['upstream_failed']), self))
            if payload is None:
                payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError()
            if payload.get('success') is False:
                raise MinutesSourceError(error_summary(str(payload.get('message') or payload.get('error')
                                                         or REASONS['upstream_failed']), self))
        except MinutesSourceError as exc:
            raise MinutesSourceError(error_summary(str(exc), self)) from None
        except (KeyError, TypeError, ValueError, OSError):
            raise MinutesSourceError(REASONS['upstream_failed']) from None


def error_summary(message, source=None):
    # Redact actual transport credentials and common credential-shaped fields
    # before persistence or display; ordinary tool errors remain visible.
    if source is not None:
        values = [getattr(source, '_token', '')]
        if getattr(source, 'tokens', None):
            try:
                values += [v for k, v in source.tokens._read().items()
                           if k in ('access_token', 'refresh_token', 'client_id')]
            except (OSError, MinutesSourceError):
                pass
        for value in values:
            if isinstance(value, str) and value:
                message = message.replace(value, '[已隐藏]')
    message = re.sub(r'(?i)(bearer\s+)[^\s,;]+', r'\1[已隐藏]', message)
    message = re.sub(r'(?i)((?:access_token|refresh_token|api_key|password|secret)\s*[=:]\s*)[^\s,;]+',
                     r'\1[已隐藏]', message)
    return ' '.join(message.split())[:200] or REASONS['upstream_failed']


class HttpHotwordSource(HotwordTools, HttpMinutesSource):
    pass


class McpHotwordSource(HotwordTools, McpMinutesSource):
    pass


def source_for(settings):
    if settings.minutes_source == 'mcp':
        return McpHotwordSource()
    if settings.minutes_source == 'http':
        return HttpHotwordSource()
    raise MinutesSourceError('当前数据源未连接千问热词。')


def snapshot(store):
    with store._connect() as conn:
        row = conn.execute('SELECT snapshot_json FROM hotword_upstream_state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def save_snapshot(store, value):
    with store._connect() as conn:
        conn.execute('INSERT INTO hotword_upstream_state VALUES (1,?) '
                     'ON CONFLICT(id) DO UPDATE SET snapshot_json=excluded.snapshot_json',
                     (json.dumps(value, ensure_ascii=False),))


def sync_rows(store):
    with store._connect() as conn:
        return {r['hotword_id']: dict(r) for r in conn.execute('SELECT * FROM hotword_upstream_sync')}


def skip_reason(word, previous):
    if previous and previous['status'] == 'ok' and previous['upstream_value'] == word.term:
        return 'already_synced'
    if not re.fullmatch(f'[{HAN}A-Za-z0-9 ]+', word.term) or not word.term.strip():
        return 'invalid_chars'
    # All non-Han characters (including digits and spaces) count toward the ASCII limit.
    chinese = len(re.findall(f'[{HAN}]', word.term))
    if chinese > 20 or len(word.term) - chinese > 40:
        return 'too_long'
    if not word.enabled:
        return 'disabled'
    return None


def plan(store, quota=None):
    quota = snapshot(store) if quota is None else quota
    previous = sync_rows(store)
    eligible, skipped = [], []
    for word in store.list_hotwords():
        reason = skip_reason(word, previous.get(word.id))
        row = {'hotword_id': word.id, 'term': word.term}
        (skipped if reason else eligible).append(
            {**row, 'reason': reason, 'message': REASONS[reason]} if reason else row)
    return dict(to_push=eligible, skipped=skipped,
                currentCount=quota['currentCount'] if quota else None,
                maxCount=quota['maxCount'] if quota else 200)


def refresh(store, source):
    with sync_lock(store):
        value = source.list_hot_words()
        save_snapshot(store, value)
        return value


def error_rows(store):
    with store._connect() as conn:
        return {r['hotword_id']: r['message'] for r in conn.execute('SELECT * FROM hotword_upstream_errors')}


def _push_one(store, source, identifier, force=False):
    word = next((w for w in store.list_hotwords() if w.id == identifier), None)
    if word is None:
        return dict(hotword_id=identifier, status='failed', error='not_found', message=REASONS['not_found'])
    reason = skip_reason(word, None if force else sync_rows(store).get(identifier))
    if reason == 'already_synced':
        return dict(hotword_id=identifier, term=word.term, status='skipped', error=None,
                    message=REASONS[reason])
    message = REASONS.get(reason, '')
    quota = None
    if not reason:
        try:
            quota = source.list_hot_words()
            save_snapshot(store, quota)
            if quota['currentCount'] >= quota['maxCount']:
                reason, message = 'quota_full', REASONS['quota_full']
            else:
                source.add_personal_hot_word([word.term])
        except (MinutesSourceError, ValueError, OSError) as exc:
            reason, message = 'upstream_failed', error_summary(str(exc), source)
    status = 'failed' if reason else 'ok'
    at = now()
    result = dict(hotword_id=identifier, term=word.term, status=status, error=reason,
                  message=message or '已推送')
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO hotword_upstream_sync VALUES (?,?,?,?) '
                     'ON CONFLICT(hotword_id) DO UPDATE SET upstream_value=excluded.upstream_value, '
                     'synced_at=excluded.synced_at,status=excluded.status', (identifier, word.term, at, status))
        conn.execute('DELETE FROM hotword_upstream_errors WHERE hotword_id=?', (identifier,))
        if reason:
            conn.execute('INSERT INTO hotword_upstream_errors VALUES (?,?)', (identifier, message))
        conn.execute('INSERT INTO hotword_upstream_runs(started_at,finished_at,status,plan_json) VALUES (?,?,?,?)',
                     (at, now(), status, json.dumps(result, ensure_ascii=False)))
        if status == 'ok':
            words = quota['hotWords'] if word.term in quota['hotWords'] else quota['hotWords'] + [word.term]
            quota = {**quota, 'hotWords': words, 'currentCount': len(words), 'checked_at': now()}
            conn.execute('UPDATE hotword_upstream_state SET snapshot_json=? WHERE id=1',
                         (json.dumps(quota, ensure_ascii=False),))
    if quota is not None:
        result.update(currentCount=quota['currentCount'], maxCount=quota['maxCount'])
    return result


def execute_one(store, source, hotword_id, force=False):
    with sync_lock(store):
        return _push_one(store, source, hotword_id, force)


def execute(store, source):
    with sync_lock(store):
        results = [_push_one(store, source, w.id) for w in store.list_hotwords()]
    successful = sum(r['status'] == 'ok' for r in results)
    failed = sum(r['status'] == 'failed' for r in results)
    return dict(results=results, success=successful, failed=failed,
                skipped=sum(r['status'] == 'skipped' for r in results),
                status='failed' if failed else 'ok', error='upstream_failed' if failed else None,
                message=f'成功 {successful} / 失败 {failed}')


def import_preview(store, source):
    upstream = source.list_hot_words()
    existing = set()
    for word in store.list_hotwords():
        existing.add(normalize(word.term))
    candidates = []
    for term in upstream['hotWords']:
        key = normalize(term)
        if key not in existing:
            candidates.append(term)
            existing.add(key)
    return {'candidates': candidates, 'currentCount': upstream['currentCount'], 'maxCount': upstream['maxCount']}


def import_selected(store, term):
    """One explicitly selected term, with normalized deduplication inside a write lock."""
    from app.hotwords import validate_hotword
    validate_hotword(term)
    with store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        key = normalize(term)
        for row in conn.execute('SELECT term FROM hotwords'):
            if key == normalize(row['term']):
                return False
        at = now()
        conn.execute('INSERT INTO hotwords(term,created_at,updated_at) VALUES (?,?,?)', (term, at, at))
    return True
