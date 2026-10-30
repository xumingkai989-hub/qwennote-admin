"""Explicit, one-way hotword publishing. Planning is entirely local and read-only."""
import fcntl
import hashlib
import json
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

from app.mcp import McpMinutesSource
from app.minutes import HttpMinutesSource, MinutesSourceError


REASONS = {
    'already_synced': '已同步，标准词未变', 'invalid_chars': '含中英文、数字、空格以外的字符',
    'too_long': '超过千问长度限制', 'disabled': '热词已停用',
    'upstream_failed': '千问调用失败；请先从千问导入核对，勿盲目重试',
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
            self._call('add_personal_hot_word', {'hotWordList': terms})
        except (MinutesSourceError, KeyError, TypeError, ValueError, OSError):
            raise MinutesSourceError(REASONS['upstream_failed']) from None


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
        if reason:
            skipped.append({**row, 'reason': reason, 'message': REASONS[reason]})
        else:
            eligible.append(row)
    count = quota['currentCount'] if quota else None
    maximum = quota['maxCount'] if quota else 200
    after = count + len(eligible) if count is not None else None
    error = 'quota_unknown' if count is None else 'quota_insufficient' if after > maximum else None
    result = dict(to_push=eligible, skipped=skipped, currentCount=count, maxCount=maximum,
                  afterCount=after, quota_checked_at=quota.get('checked_at') if quota else None,
                  error=error, can_confirm=not error and bool(eligible))
    # Bind browser confirmation to the actual reviewed list and quota, never silently add new words.
    result['plan_hash'] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k != 'quota_checked_at'},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def refresh(store, source):
    with sync_lock(store):
        value = source.list_hot_words()
        save_snapshot(store, value)
        return value


def execute(store, source, expected_hash=''):
    with sync_lock(store):
        quota = source.list_hot_words()  # Refresh immediately before the irreversible call.
        result = plan(store, quota)
        if result['error']:
            return result
        if expected_hash and expected_hash != result['plan_hash']:
            return {**result, 'error': 'plan_changed', 'can_confirm': False}
        save_snapshot(store, quota)
        at = now()
        with store._connect() as conn:
            run_id = conn.execute('INSERT INTO hotword_upstream_runs(started_at,status,plan_json) '
                                  "VALUES (?,'running',?)", (at, json.dumps(result, ensure_ascii=False))).lastrowid
        status = 'ok'
        if result['to_push']:
            try:
                source.add_personal_hot_word([r['term'] for r in result['to_push']])
            except MinutesSourceError:
                status = 'failed'
        # All local results are committed together, never a partially recorded batch.
        with store._connect() as conn:
            for row in result['to_push'] + result['skipped']:
                if row.get('reason') == 'already_synced':
                    continue  # Preserve successful deduplication evidence.
                state = 'skipped' if 'reason' in row else status
                conn.execute('INSERT INTO hotword_upstream_sync VALUES (?,?,?,?) '
                             'ON CONFLICT(hotword_id) DO UPDATE SET upstream_value=excluded.upstream_value, '
                             'synced_at=excluded.synced_at,status=excluded.status',
                             (row['hotword_id'], row['term'], at, state))
            conn.execute('UPDATE hotword_upstream_runs SET status=?,finished_at=? WHERE id=?',
                         (status, now(), run_id))
            if status == 'ok':
                quota = {**quota, 'hotWords': quota['hotWords'] + [r['term'] for r in result['to_push']],
                         'currentCount': result['afterCount'], 'checked_at': now()}
                conn.execute('UPDATE hotword_upstream_state SET snapshot_json=? WHERE id=1',
                             (json.dumps(quota, ensure_ascii=False),))
        return {**result, 'status': status, 'run_id': run_id,
                'error': 'upstream_failed' if status == 'failed' else None}


def import_preview(store, source):
    upstream = source.list_hot_words()
    existing = set()
    for word in store.list_hotwords():
        existing.update(normalize(v) for v in [word.term, *re.split('[,，]', word.aliases)] if v.strip())
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
        for row in conn.execute('SELECT term,aliases FROM hotwords'):
            if key in {normalize(v) for v in [row['term'], *re.split('[,，]', row['aliases'])] if v.strip()}:
                return False
        at = now()
        conn.execute('INSERT INTO hotwords(term,created_at,updated_at) VALUES (?,?,?)', (term, at, at))
    return True
