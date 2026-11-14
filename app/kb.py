"""Read-only knowledge scanning and Markdown generation (no AI summaries)."""
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import quote

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
    """Create directories and both indexes without rendering tag/category bodies."""
    index = scan(store)
    root = Path(root)
    (root / 'categories').mkdir(parents=True, exist_ok=True)
    (root / 'tags').mkdir(parents=True, exist_ok=True)
    (root / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (root / 'index.md').write_text(_markdown(index), encoding='utf-8')
    return index


def _is_unclassified(tag):
    return tag['id'] is None and tag['name'] == UNCLASSIFIED and tag['category'] == UNCLASSIFIED


def _tag_recordings(store, tag):
    """Use the same alias/merge membership as scan, retaining displayed tag order."""
    with store._connect() as conn:
        conn.execute('PRAGMA query_only = ON')
        conn.execute('BEGIN')
        relations = list(conn.execute(
            'SELECT recording_id, tag FROM recording_tags ORDER BY recording_id, position, tag'))
        canonical = {}
        for name in sorted({row['tag'] for row in relations}):
            target = resolve(conn, name)
            canonical[name] = target['name'] if target is not None else name
        labels = {}
        for row in relations:
            labels.setdefault(row['recording_id'], []).append(row['tag'])
        recordings = []
        for row in conn.execute(
                'SELECT r.recording_id, r.title, r.created_at, r.duration_min, r.summary_md, '
                'p.tag_name AS primary_tag FROM recordings r LEFT JOIN recording_primary_tag p '
                'ON p.recording_id = r.recording_id'):
            names = labels.get(row['recording_id'], [])
            belongs = (not names if _is_unclassified(tag) else
                       any(canonical[name] == tag['name'] for name in names))
            if belongs:
                recordings.append(dict(row, tags=names))
    return sorted(recordings, key=lambda row: (_instant(row['created_at']), row['recording_id']),
                  reverse=True)


def _first_sentence(summary):
    text = (summary or '').strip()
    if not text:
        return '（无总结）'
    end = re.search(r'[。.\r\n]', text)
    if end:
        text = text[:end.end()].strip()
    else:
        text = text[:60]
    return text[:80] + '…' if len(text) > 80 else text


def _date(value, pattern):
    return _instant(value).astimezone(SHANGHAI).strftime(pattern) if value else '—'


def render_tag_file(store, tag: dict, notes: list[dict] | None = None) -> str:
    """Render a scanned tag; notes are reserved for T3 and empty in this ticket."""
    recordings = _tag_recordings(store, tag)
    lines = [f"# {_markdown_text(tag['name'])}", '',
             f"> {_markdown_text(tag['category'])} · {tag['sessions']} 场会议 · "
             f"{tag['minutes']} 分钟 · 更新于 {_date(tag['updated_at'], '%Y-%m-%d %H:%M')}", '']
    if not _is_unclassified(tag):
        lines += ['## 当前状态', '', '综述未生成。', '',
                  '## 补充记录', '', '暂无补充记录。', '']
    lines += ['## 会议', '']
    for row in recordings[:20]:
        duration = '时长未知' if row['duration_min'] is None else f"{row['duration_min']} 分钟"
        labels = '、'.join(row['tags']) or '—'
        lines += [f"### {_date(row['created_at'], '%Y-%m-%d')} · {_markdown_text(row['title'])}",
                  f"- {duration} · 主议题：{_markdown_text(row['primary_tag'] or '—')} · "
                  f"标签：{_markdown_text(labels)}",
                  f"- {_markdown_text(_first_sentence(row['summary_md']))}",
                  f"- [查看原文](/recordings/{quote(row['recording_id'], safe='')})", '']
    if len(recordings) > 20:
        lines += [f'另有 {len(recordings) - 20} 场更早的会议未列出。', '']
    lines += ['## 时间线', '', '| 日期 | 类型 | 内容 |', '|---|---|---|']
    for row in recordings[:30]:
        lines.append(f"| {_date(row['created_at'], '%m-%d')} | 会议 | "
                     f"{_markdown_text(row['title'])} |")
    return '\n'.join(lines) + '\n'


def render_category_file(store, category: dict, tags: list[dict]) -> str:
    """Render category union statistics and links using T1's allocated paths."""
    lines = [f"# {_markdown_text(category['name'])}", '',
             f"> {category['tags']} 个标签 · {category['sessions']} 场会议 · "
             f"{category['minutes']} 分钟", '',
             '| 标签 | 会议 | 时长 | 最近 |', '|---|---:|---:|---|']
    for tag in tags:
        if tag['category'] != category['name']:
            continue
        # Angle destinations keep spaces/parentheses valid without changing the slug.
        path = tag['path'].replace('%', '%25').replace('#', '%23')
        destination = f'<{path}>' if re.search(r'[\s()]', path) else path
        lines.append(f"| [{_markdown_text(tag['name'])}]({destination}) | {tag['sessions']} | "
                     f"{tag['minutes']} 分钟 | {_date(tag['last_session_at'], '%m-%d')} |")
    return '\n'.join(lines) + '\n'


def build(store, root: Path = DEFAULT_ROOT, tags: list[str] | None = None) -> dict:
    """Render all bodies, then build the unchanged T1 indexes; tags is reserved."""
    index = scan(store)
    root = Path(root)
    for tag in index['tags']:
        path = root / tag['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_tag_file(store, tag, notes=[]), encoding='utf-8')
    for category in index['categories']:
        if not category['tags']:
            continue
        path = root / category['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_category_file(store, category, index['tags']), encoding='utf-8')
    return build_index(store, root)
