"""Atomic standard-term migration; caller holds BEGIN IMMEDIATE."""

HOTWORDS = """CREATE TABLE {table} (
    id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE,
    aliases TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, hits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
DECISIONS = """CREATE TABLE recording_hotword_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, recording_id TEXT NOT NULL, hotword_id INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(field IN ('summary','transcript')),
    span TEXT NOT NULL CHECK(length(span) BETWEEN 1 AND 20), replacement TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
    start_offset INTEGER NOT NULL CHECK(start_offset>=0), end_offset INTEGER NOT NULL,
    CHECK(end_offset=start_offset+length(span)),
    UNIQUE(recording_id,hotword_id,field,span,start_offset))"""


def columns(conn, table):
    return {r['name'] for r in conn.execute('PRAGMA table_info(' + table + ')')}


def migrate_hotwords(conn):
    old = columns(conn, 'hotwords')
    old_decisions = columns(conn, 'recording_hotword_decisions')
    if old_decisions and 'span' not in old_decisions:
        # A live old release may have judged more records since preflight. Archive
        # all legacy rows (including unexpected apply) before merging rule IDs.
        conn.execute("""CREATE TABLE IF NOT EXISTS recording_hotword_legacy_decisions (
            id INTEGER PRIMARY KEY, recording_id TEXT NOT NULL, hotword_id INTEGER NOT NULL,
            field TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, decided_at TEXT NOT NULL,
            source TEXT NOT NULL, target TEXT NOT NULL, archived_at TEXT NOT NULL)""")
        source, target = ('h.source', 'h.target') if 'source' in old else ('h.aliases', 'h.term')
        conn.execute(f"""INSERT INTO recording_hotword_legacy_decisions
            (recording_id,hotword_id,field,decision,reason,decided_at,source,target,archived_at)
            SELECT d.recording_id,d.hotword_id,d.field,d.decision,d.reason,d.decided_at,
            coalesce({source},''),coalesce({target},''),strftime('%Y-%m-%dT%H:%M:%fZ','now')
            FROM recording_hotword_decisions d LEFT JOIN hotwords h ON h.id=d.hotword_id""")
    if 'source' in old:
        conn.execute(HOTWORDS.format(table='hotwords_standard'))
        merged = {}
        for row in conn.execute('SELECT * FROM hotwords ORDER BY id'):
            row = dict(row)
            term = row['target']
            if term not in merged:
                merged[term] = dict(id=row['id'], term=term, aliases=[], note=[], enabled=0,
                                    hits=0, created_at=row['created_at'], updated_at=row['updated_at'])
            item = merged[term]
            # Preserve every distinct original spelling and note, including commas in legacy sources.
            if row['source'] not in item['aliases']:
                item['aliases'].append(row['source'])
            if row['note'] and row['note'] not in item['note']:
                item['note'].append(row['note'])
            item['enabled'] = max(item['enabled'], row['enabled'])
            item['hits'] += row['hits']
            item['created_at'] = min(item['created_at'], row['created_at'])
            item['updated_at'] = max(item['updated_at'], row['updated_at'])
        for item in merged.values():
            conn.execute('INSERT INTO hotwords_standard VALUES (?,?,?,?,?,?,?,?)',
                         (item['id'], item['term'], ','.join(item['aliases']), '；'.join(item['note']),
                          item['enabled'], item['hits'], item['created_at'], item['updated_at']))
        conn.execute('DROP TABLE hotwords')
        conn.execute('ALTER TABLE hotwords_standard RENAME TO hotwords')
    elif not old:
        conn.execute(HOTWORDS.format(table='hotwords'))
    if 'needs_review' not in columns(conn, 'hotwords'):
        conn.execute('ALTER TABLE hotwords ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0,1))')
    # Only flag legacy data. Never normalize or overwrite a user's term/aliases.
    from app.hotwords import SENTENCE_PUNCTUATION
    for row in conn.execute('SELECT id,term FROM hotwords'):
        if len(row['term']) > 20 or any(c in SENTENCE_PUNCTUATION for c in row['term']):
            conn.execute('UPDATE hotwords SET needs_review=1 WHERE id=?', (row['id'],))
    if old_decisions and 'span' not in old_decisions:
        conn.execute('DROP TABLE recording_hotword_decisions')
        old_decisions = set()
    if not old_decisions:
        conn.execute(DECISIONS)
    conn.execute('CREATE INDEX IF NOT EXISTS hotword_decision_counts ON recording_hotword_decisions(hotword_id,revoked,recording_id)')
    conn.execute("""CREATE TABLE IF NOT EXISTS recording_hotword_checks (
        recording_id TEXT NOT NULL, hotword_id INTEGER NOT NULL, field TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('apply','skip')), reason TEXT NOT NULL,
        decided_at TEXT NOT NULL, PRIMARY KEY(recording_id,hotword_id,field))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS recording_hotword_decision_history (
        history_id INTEGER PRIMARY KEY, id INTEGER NOT NULL, recording_id TEXT NOT NULL,
        hotword_id INTEGER NOT NULL, field TEXT NOT NULL, span TEXT NOT NULL, replacement TEXT NOT NULL,
        reason TEXT NOT NULL, decided_at TEXT NOT NULL, revoked INTEGER NOT NULL,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
        archived_at TEXT NOT NULL, operation TEXT NOT NULL)""")
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS archive_hotword_{action.lower()}
            BEFORE {action} ON recording_hotword_decisions BEGIN
            INSERT INTO recording_hotword_decision_history
            (id,recording_id,hotword_id,field,span,replacement,reason,decided_at,revoked,start_offset,end_offset,archived_at,operation)
            VALUES (OLD.id,OLD.recording_id,OLD.hotword_id,OLD.field,OLD.span,OLD.replacement,OLD.reason,
            OLD.decided_at,OLD.revoked,OLD.start_offset,OLD.end_offset,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{action}'); END""")

    conn.execute("""CREATE TABLE IF NOT EXISTS recording_hotword_check_history (
        history_id INTEGER PRIMARY KEY, recording_id TEXT NOT NULL, hotword_id INTEGER NOT NULL,
        field TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, decided_at TEXT NOT NULL,
        archived_at TEXT NOT NULL, operation TEXT NOT NULL)""")
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS archive_hotword_check_{action.lower()}
            BEFORE {action} ON recording_hotword_checks BEGIN
            INSERT INTO recording_hotword_check_history
            (recording_id,hotword_id,field,status,reason,decided_at,archived_at,operation)
            VALUES (OLD.recording_id,OLD.hotword_id,OLD.field,OLD.status,OLD.reason,OLD.decided_at,
            strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{action}'); END""")
