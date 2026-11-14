"""Read-only knowledge scan and index generation (no tag bodies or summaries)."""
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re

from app.minutes import SHANGHAI
from app.tag_vocabulary import resolve


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'knowledge'
UNCLASSIFIED = '未分类'


def slug(name: str) -> str:
    """Clean one filename stem; build-local allocation handles collisions."""
    value = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '', ' '.join(name.split()))
    value = ' '.join(value.split())
    # Empty and dot-only names must still produce visible, safe filenames.
    return value if value and value not in {'.', '..'} else UNCLASSIFIED


def _paths(names, directory):
    used = set()
    for name in names:
        stem = slug(name)
        candidate, suffix = stem, 2
        while candidate.casefold() in used:
            candidate = f'{stem}-{suffix}'
            suffix += 1
        used.add(candidate.casefold())
        yield f'{directory}/{candidate}.md'


def _instant(value):
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp


def _latest(values):
    return max((v for v in values if v), key=lambda v: (_instant(v), v), default=None)


def scan(store) -> dict:
    """Read one SQLite snapshot; do not migrate, mutate sources or write files.

    Categories count the union of their tags' recordings. Non-merged vocabulary
    entries (including unused/deactivated tags) are retained. Legacy names missing
    from the vocabulary get id=None. Untagged recordings get a virtual tag/category
    named 未分类 with id=None; this virtual tag is excluded from source.tags.
    """
    with store._connect() as conn:
        conn.execute('PRAGMA query_only = ON')
        conn.execute('BEGIN')
        recordings = {row['recording_id']: dict(row) for row in conn.execute(
            'SELECT recording_id, created_at, duration_min, synced_at FROM recordings '
            'ORDER BY recording_id')}
        vocabulary = [dict(row) for row in conn.execute(
            "SELECT id, name, category, updated_at FROM tags WHERE status != 'merged' ORDER BY id")]
        category_names = [row['name'] for row in conn.execute(
            'SELECT name FROM tag_categories ORDER BY position, id')]
        relations = list(conn.execute(
            'SELECT recording_id, tag, created_at FROM recording_tags ORDER BY tag, recording_id'))
        by_name = {tag['name']: tag for tag in vocabulary}
        resolved = {}
        for name in sorted({row['tag'] for row in relations}):
            target = resolve(conn, name)
            canonical = target['name'] if target is not None else name
            resolved[name] = canonical
            if canonical not in by_name:
                tag = dict(id=None, name=canonical, category='其他', updated_at=None)
                vocabulary.append(tag)
                by_name[canonical] = tag

    members = {tag['name']: set() for tag in vocabulary}
    relation_times = {tag['name']: [] for tag in vocabulary}
    tagged = set()
    for relation in relations:
        ident = relation['recording_id']
        if ident not in recordings:
            continue
        name = resolved[relation['tag']]
        members[name].add(ident)
        relation_times[name].append(relation['created_at'])
        tagged.add(ident)

    source = dict(recordings=len(recordings), tags=len(vocabulary), notes=0)
    entries = [(tag, members[tag['name']], relation_times[tag['name']]) for tag in vocabulary]
    untagged = set(recordings) - tagged
    if untagged:
        entries.append((dict(id=None, name=UNCLASSIFIED, category=UNCLASSIFIED, updated_at=None),
                        untagged, []))

    tags = []
    category_members = {name: set() for name in category_names}
    category_counts = {name: 0 for name in category_names}
    paths = _paths((tag['name'] for tag, _, _ in entries), 'tags')
    for (tag, ids, times), path in zip(entries, paths):
        category = tag['category'] or UNCLASSIFIED
        if category not in category_members:
            category_names.append(category)
            category_members[category] = set()
            category_counts[category] = 0
        category_members[category].update(ids)
        category_counts[category] += 1
        rows = [recordings[ident] for ident in sorted(ids)]
        stats = dict(sessions=len(rows), minutes=sum(row['duration_min'] or 0 for row in rows),
                     last_session_at=_latest(row['created_at'] for row in rows))
        updated_at = _latest([tag['updated_at'], stats['last_session_at'], *times,
                              *(row['synced_at'] for row in rows)])
        content_hash = hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()
        tags.append(dict(id=tag['id'], name=tag['name'], category=category, path=path,
                         **stats, notes=0, updated_at=updated_at, content_hash=content_hash))

    categories = [dict(name=name, path=path, tags=category_counts[name],
                       sessions=len(category_members[name]),
                       minutes=sum(recordings[ident]['duration_min'] or 0
                                   for ident in category_members[name]))
                  for name, path in zip(category_names, _paths(category_names, 'categories'))]
    return dict(generated_at=datetime.now(SHANGHAI).isoformat(), source=source,
                categories=categories, tags=tags)


def _markdown_text(value):
    return html.escape(' '.join(value.split()), quote=False).replace('|', '&#124;').replace(
        '\\', '&#92;').replace('*', '&#42;').replace('_', '&#95;').replace('`', '&#96;').replace(
        '[', '&#91;').replace(']', '&#93;')


def _markdown(index):
    stamp = _instant(index['generated_at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')
    source = index['source']
    lines = ['# 知识库索引', '',
             f"更新于 {stamp} · {source['recordings']} 场会议 · {source['tags']} 个标签", '']
    for category in index['categories']:
        lines += [f"## {_markdown_text(category['name'])}（{category['tags']} 个标签 · "
                  f"{category['sessions']} 场 · {category['minutes']} 分钟）", '',
                  '| 标签 | 会议 | 时长 | 最近 |', '|---|---:|---:|---|']
        for tag in index['tags']:
            if tag['category'] != category['name']:
                continue
            recent = (_instant(tag['last_session_at']).astimezone(SHANGHAI).strftime('%m-%d')
                      if tag['last_session_at'] else '—')
            lines.append(f"| {_markdown_text(tag['name'])} | {tag['sessions']} | "
                         f"{tag['minutes']} 分钟 | {recent} |")
        lines.append('')
    return '\n'.join(lines)


def build_index(store, root: Path = DEFAULT_ROOT) -> dict:
    """Create directories and both indexes, leaving tag/category bodies for T2."""
    index = scan(store)
    root = Path(root)
    (root / 'categories').mkdir(parents=True, exist_ok=True)
    (root / 'tags').mkdir(parents=True, exist_ok=True)
    (root / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (root / 'index.md').write_text(_markdown(index), encoding='utf-8')
    return index


def build(store, root: Path = DEFAULT_ROOT, tags: list[str] | None = None) -> dict:
    """T1 always rebuilds the full index; tags is reserved for incremental bodies."""
    return build_index(store, root)
