"""Persistent vocabulary, deterministic normalization and transactional merges."""
from datetime import datetime, timezone
import json
import unicodedata

SEED_TAGS = (
    'AI Tutor', '模考系统', '导师中心', '师资匹配', '中台与数据看板',
    'AI Passport', '市场与投放', '组织与培训', '述职', '周会/例会', '客户沟通',
)


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_tag(name):
    # Only width folding, not compatibility folding (e.g. ① must not become 1).
    name = ''.join(chr(ord(c) - 0xFEE0) if '\uff01' <= c <= '\uff5e' else c for c in name)
    name = ''.join(c for c in name.lower() if not c.isspace() and not unicodedata.category(c).startswith('P'))
    while name.endswith(('相关', '类', '项目')):
        for suffix in ('相关', '类', '项目'):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
    return name


def log_change(conn, action, **detail):
    conn.execute('INSERT INTO tag_change_log (at, action, detail) VALUES (?, ?, ?)',
                 (now(), action, json.dumps(detail, ensure_ascii=False)))


def migrate(conn):
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tags' AND type = 'table'").fetchone() is None
    statements = [
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('active', 'merged', 'deactivated')),
            merged_into INTEGER REFERENCES tags(id),
            source TEXT NOT NULL CHECK (source IN ('seed', 'model', 'manual')),
            usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK ((status IN ('active', 'deactivated') AND merged_into IS NULL) OR
                   (status = 'merged' AND merged_into IS NOT NULL AND merged_into != id)))""",
        """CREATE TABLE IF NOT EXISTS tag_aliases (
            alias TEXT PRIMARY KEY NOT NULL, tag_id INTEGER NOT NULL REFERENCES tags(id))""",
        """CREATE TABLE IF NOT EXISTS assistant_config (
            id INTEGER PRIMARY KEY CHECK (id = 1), instructions TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS tag_change_log (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS tag_merge_proposals (
            id INTEGER PRIMARY KEY, from_tag TEXT NOT NULL, to_tag TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN ('model', 'normalized')),
            status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')), created_at TEXT NOT NULL,
            UNIQUE (from_tag, to_tag))""",
        # Successful null answers also need durable deduplication across daily runs.
        """CREATE TABLE IF NOT EXISTS tag_semantic_checks (
            tag_id INTEGER PRIMARY KEY REFERENCES tags(id), checked_at TEXT NOT NULL)""",
    ]
    # Work order 08's CHECK constraint predates deactivation. Rebuild only the
    # vocabulary and its two FK dependants inside the caller's write transaction.
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tags'").fetchone()
    if schema and "'deactivated'" not in schema[0]:
        aliases = list(conn.execute('SELECT alias, tag_id FROM tag_aliases'))
        checks = list(conn.execute('SELECT tag_id, checked_at FROM tag_semantic_checks'))
        conn.execute('DROP TABLE tag_aliases')
        conn.execute('DROP TABLE tag_semantic_checks')
        conn.execute('ALTER TABLE tags RENAME TO tags_before_deactivation')
        conn.execute(statements[0])
        conn.execute('INSERT INTO tags SELECT * FROM tags_before_deactivation')
        conn.execute('DROP TABLE tags_before_deactivation')
        conn.execute(statements[1])
        conn.execute(statements[-1])
        conn.executemany('INSERT INTO tag_aliases VALUES (?, ?)', aliases)
        conn.executemany('INSERT INTO tag_semantic_checks VALUES (?, ?)', checks)
    for statement in statements:
        conn.execute(statement)
    stamp = now()
    conn.execute("INSERT OR IGNORE INTO assistant_config (id, instructions, updated_at) VALUES (1, '', ?)", (stamp,))
    if fresh:
        for name in SEED_TAGS:
            conn.execute("INSERT INTO tags (name, status, source, created_at, updated_at) VALUES (?, 'active', 'seed', ?, ?)",
                         (name, stamp, stamp))
            log_change(conn, 'add', **{'from': None, 'to': name, 'reason': 'seed'})


def refresh_usage(conn):
    conn.execute("""UPDATE tags SET usage_count = (
        SELECT count(*) FROM recording_tags r WHERE r.tag = tags.name), updated_at = ?""", (now(),))


def rewrite_recordings(conn, old, new):
    # INSERT with conflict handling preserves the earliest position if both spellings
    # occur on the same recording, and prevents counting that recording twice.
    conn.execute("""INSERT INTO recording_tags (recording_id, tag, position, model, created_at)
        SELECT recording_id, ?, position, model, created_at FROM recording_tags WHERE tag = ?
        ON CONFLICT(recording_id, tag) DO UPDATE SET position = min(position, excluded.position)""", (new, old))
    conn.execute('DELETE FROM recording_tags WHERE tag = ?', (old,))


def resolve(conn, name):
    row = conn.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
    if row is None:
        row = conn.execute('SELECT t.* FROM tag_aliases a JOIN tags t ON t.id = a.tag_id WHERE a.alias = ?',
                           (normalize_tag(name),)).fetchone()
    seen = set()
    while row is not None and row['status'] == 'merged':
        if row['id'] in seen:
            raise ValueError('Invalid tag merge cycle')
        seen.add(row['id'])
        row = conn.execute('SELECT * FROM tags WHERE id = ?', (row['merged_into'],)).fetchone()
    return row


def merge_tags(conn, source, target, *, reason, proposal_id=None):
    if source['id'] == target['id']:
        return False
    if source['status'] != 'active' or target['status'] != 'active':
        raise ValueError('Merge endpoints must be active')
    rewrite_recordings(conn, source['name'], target['name'])
    conn.execute("UPDATE tags SET status = 'merged', merged_into = ?, usage_count = 0, updated_at = ? WHERE id = ?",
                 (target['id'], now(), source['id']))
    conn.execute('UPDATE tags SET merged_into = ? WHERE merged_into = ?', (target['id'], source['id']))
    conn.execute('UPDATE tag_aliases SET tag_id = ? WHERE tag_id = ?', (target['id'], source['id']))
    conn.execute('INSERT INTO tag_aliases (alias, tag_id) VALUES (?, ?) ON CONFLICT(alias) DO UPDATE SET tag_id = excluded.tag_id',
                 (normalize_tag(source['name']), target['id']))
    log_change(conn, 'merge', **{'from': source['name'], 'to': target['name'], 'reason': reason, 'proposal_id': proposal_id})
    return True


class TagVocabulary:
    def __init__(self, store):
        self.store = store

    def accept_proposal(self, proposal_id):
        with self.store.tag_lock(), self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            proposal = conn.execute('SELECT * FROM tag_merge_proposals WHERE id = ?', (proposal_id,)).fetchone()
            if proposal is None:
                raise ValueError('Unknown merge proposal')
            if proposal['status'] != 'pending':
                return False
            source, target = resolve(conn, proposal['from_tag']), resolve(conn, proposal['to_tag'])
            if source is None or target is None:
                raise ValueError('Merge endpoint unavailable')
            merge_tags(conn, source, target, reason='accepted_proposal', proposal_id=proposal_id)
            conn.execute("UPDATE tag_merge_proposals SET status = 'accepted' WHERE id = ?", (proposal_id,))
            refresh_usage(conn)
            return True

    def reject_proposal(self, proposal_id):
        with self.store.tag_lock(), self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM tag_merge_proposals WHERE id = ?', (proposal_id,)).fetchone() is None:
                raise ValueError('Unknown merge proposal')
            return conn.execute("UPDATE tag_merge_proposals SET status = 'rejected' WHERE id = ? AND status = 'pending'",
                                (proposal_id,)).rowcount == 1
