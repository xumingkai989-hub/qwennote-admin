"""Knowledge indexes use isolated fixtures, never production data or models."""
from datetime import datetime
import json
from pathlib import Path

import pytest

from app import kb


STAMP = '2026-11-13T10:04:00+08:00'


@pytest.fixture
def db(store):
    store.migrate()
    with store._connect() as conn:
        conn.execute('DELETE FROM tags')
    return store


def add_tag(db, name, category='产品', status='active'):
    with db._connect() as conn:
        return conn.execute('INSERT INTO tags '
                            '(name, category, status, source, created_at, updated_at) '
                            "VALUES (?, ?, ?, 'manual', ?, ?)",
                            (name, category, status, STAMP, STAMP)).lastrowid


def add_meeting(db, ident, tags=(), minutes=30, stamp=STAMP):
    with db._connect() as conn:
        conn.execute('INSERT INTO recordings (recording_id, created_at, duration_min, synced_at) '
                     'VALUES (?, ?, ?, ?)', (ident, stamp, minutes, stamp))
        conn.executemany('INSERT INTO recording_tags '
                         '(recording_id, tag, position, model, created_at) VALUES (?, ?, ?, ?, ?)',
                         [(ident, name, position, 'fixture', stamp)
                          for position, name in enumerate(tags, 1)])


def named(rows, name):
    return next(row for row in rows if row['name'] == name)


@pytest.fixture
def sample(db):
    for i in range(22):
        add_tag(db, f'标签{i:02}', '产品' if i < 11 else '技术')
    for i in range(52):
        add_meeting(db, f'r{i:02}', (f'标签{i % 22:02}',), i + 1)
    return db


def test_scan_52_recordings_and_22_tags(sample):
    result = kb.scan(sample)
    assert result['source'] == dict(recordings=52, tags=22, notes=0)
    assert len(result['tags']) == 22
    assert len(result['categories']) == 7
    for i, tag in enumerate(result['tags']):
        ids = list(range(i, 52, 22))
        assert tag['sessions'] == len(ids)
        assert tag['minutes'] == sum(j + 1 for j in ids)
        assert tag['notes'] == 0
        assert tag['last_session_at'] == STAMP
    assert sum(t['sessions'] for t in result['tags']) == 52
    assert sum(c['minutes'] for c in result['categories']) == sum(range(1, 53))
    assert named(result['categories'], '产品')['sessions'] == 30
    assert named(result['categories'], '技术')['sessions'] == 22


def test_scan_does_not_write_files_or_database(sample, tmp_path, monkeypatch):
    with sample._connect() as conn:
        before = '\n'.join(conn.iterdump())
    monkeypatch.chdir(tmp_path)
    files = set(tmp_path.rglob('*'))
    kb.scan(sample)
    assert set(tmp_path.rglob('*')) == files
    with sample._connect() as conn:
        assert '\n'.join(conn.iterdump()) == before


def test_untagged_meetings_have_virtual_tag_and_category(db):
    add_meeting(db, 'one', minutes=20)
    add_meeting(db, 'two', minutes=None)
    result = kb.scan(db)
    tag = named(result['tags'], '未分类')
    assert (tag['id'], tag['sessions'], tag['minutes'], tag['category']) == (None, 2, 20, '未分类')
    assert result['source'] == dict(recordings=2, tags=0, notes=0)
    assert named(result['categories'], '未分类') == dict(
        name='未分类', path='categories/未分类.md', tags=1, sessions=2, minutes=20)


@pytest.mark.parametrize('minutes,expected', [(None, 0), (0, 0), (21, 21)])
def test_null_duration_counts_session(db, minutes, expected):
    add_tag(db, 'AI Tutor')
    add_meeting(db, 'one', ('AI Tutor',), minutes)
    result = kb.scan(db)
    for row in [result['tags'][0], named(result['categories'], '产品')]:
        assert row['sessions'] == 1
        assert row['minutes'] == expected


def test_categories_deduplicate_meetings_but_each_tag_counts_them(db):
    for name, category in [('A', '产品'), ('B', '产品'), ('C', '技术')]:
        add_tag(db, name, category)
    add_meeting(db, 'one', ('A', 'B', 'C'), 40)
    add_meeting(db, 'two', ('B',), 10)
    result = kb.scan(db)
    assert [(t['sessions'], t['minutes']) for t in result['tags']] == [(1, 40), (2, 50), (1, 40)]
    assert named(result['categories'], '产品')['sessions'] == 2
    assert named(result['categories'], '产品')['minutes'] == 50
    assert named(result['categories'], '技术')['minutes'] == 40


def test_latest_meeting_compares_instants_and_updates_use_source_time(db):
    add_tag(db, 'A')
    add_meeting(db, 'one', ('A',), stamp='2026-11-14T10:00:00+08:00')
    add_meeting(db, 'two', ('A',), stamp='2026-11-14T03:00:00+00:00')
    tag = kb.scan(db)['tags'][0]
    assert tag['last_session_at'] == '2026-11-14T03:00:00+00:00'
    assert tag['updated_at'] == '2026-11-14T03:00:00+00:00'


def test_unused_deactivated_and_legacy_tags_are_not_lost(db):
    add_tag(db, '空标签')
    add_tag(db, '停用标签', status='deactivated')
    add_meeting(db, 'one', ('停用标签', '旧标签'))
    result = kb.scan(db)
    assert result['source']['tags'] == 3
    assert named(result['tags'], '空标签')['last_session_at'] is None
    assert named(result['tags'], '空标签')['sessions'] == 0
    assert named(result['tags'], '停用标签')['sessions'] == 1
    assert named(result['tags'], '旧标签')['sessions'] == 1
    assert named(result['tags'], '旧标签')['id'] is None


def test_merged_tag_and_alias_resolve_without_double_counting(db):
    target = add_tag(db, '新标签')
    old = add_tag(db, '旧标签')
    with db._connect() as conn:
        conn.execute("UPDATE tags SET status='merged', merged_into=? WHERE id=?", (target, old))
        conn.execute('INSERT INTO tag_aliases VALUES (?, ?)', ('别名', target))
    add_meeting(db, 'one', ('旧标签', '新标签', '别名'))
    result = kb.scan(db)
    assert result['source']['tags'] == 1
    assert len(result['tags']) == 1
    assert result['tags'][0]['sessions'] == 1
    assert result['tags'][0]['minutes'] == 30


@pytest.mark.parametrize('name,expected', [
    (' /\\:*?"<>|中文 ', '中文'), (' AI   Tutor\t 开发\n进度 ', 'AI Tutor 开发 进度'),
    ('导师中心', '导师中心'), ('AI Tutor', 'AI Tutor'), ('', '未分类'),
    (' /:*? ', '未分类'), ('.', '未分类'), ('..', '未分类'), ('a\x00b', 'ab'),
])
def test_slug(name, expected):
    assert kb.slug(name) == expected
    assert kb.slug(name) == expected  # Pure function: no global collision state.


def test_paths_handle_case_sanitization_and_suffix_collisions(db):
    for name in ['Foo', 'foo', 'FOO', 'foo-2', '导/师', '导师']:
        add_tag(db, name, category='类/别' if '/' in name else '类别')
    result = kb.scan(db)
    assert [t['path'] for t in result['tags']] == [
        'tags/Foo.md', 'tags/foo-2.md', 'tags/FOO-3.md', 'tags/foo-2-2.md',
        'tags/导师.md', 'tags/导师-2.md']
    paths = [c['path'].casefold() for c in result['categories']]
    assert len(paths) == len(set(paths))


def test_real_unclassified_tag_does_not_absorb_untagged_meetings(db):
    add_tag(db, '未分类')
    add_meeting(db, 'tagged', ('未分类',))
    add_meeting(db, 'untagged')
    result = kb.scan(db)
    assert [t['sessions'] for t in result['tags']] == [1, 1]
    assert [t['path'] for t in result['tags']] == ['tags/未分类.md', 'tags/未分类-2.md']
    assert named(result['categories'], '未分类')['sessions'] == 1


def test_build_creates_indexes_directories_and_exact_schema(sample, tmp_path):
    root = tmp_path / 'nested' / 'knowledge'
    result = kb.build_index(sample, root)
    assert json.loads((root / 'index.json').read_text()) == result
    assert set(result) == {'generated_at', 'source', 'categories', 'tags'}
    assert datetime.fromisoformat(result['generated_at']).utcoffset().total_seconds() == 8 * 3600
    for row in result['categories']:
        assert set(row) == {'name', 'path', 'tags', 'sessions', 'minutes'}
    for row in result['tags']:
        assert set(row) == {'id', 'name', 'category', 'path', 'sessions', 'minutes', 'notes',
                            'last_session_at', 'updated_at', 'content_hash'}
    for row in result['tags'] + result['categories']:
        assert (root / row['path']).parent.is_dir()
        assert (root / row['path']).resolve().is_relative_to(root.resolve())
    assert sorted(p.name for p in root.iterdir()) == ['categories', 'index.json', 'index.md', 'tags']
    assert not list((root / 'tags').iterdir())
    assert not list((root / 'categories').iterdir())
    markdown = (root / 'index.md').read_text()
    assert '# 知识库索引\n\n更新于 ' in markdown
    assert '52 场会议 · 22 个标签' in markdown
    assert '| 标签 | 会议 | 时长 | 最近 |\n|---|---:|---:|---|' in markdown
    assert '| 标签00 | 3 | 69 分钟 | 11-13 |' in markdown


def test_repeated_build_changes_only_generated_at(sample, tmp_path, monkeypatch):
    class Clock:
        value = datetime.fromisoformat('2026-11-14T15:30:00+08:00')
        fromisoformat = staticmethod(datetime.fromisoformat)

        @classmethod
        def now(cls, tz):
            return cls.value.astimezone(tz)

    monkeypatch.setattr(kb, 'datetime', Clock)
    first = kb.build(sample, tmp_path)
    Clock.value = datetime.fromisoformat('2026-11-15T15:30:00+08:00')
    second = kb.build(sample, tmp_path, tags=['标签00'])
    assert first.pop('generated_at') != second.pop('generated_at')
    assert first == second


def test_default_root_is_repository_knowledge():
    expected = Path(kb.__file__).resolve().parents[1] / 'knowledge'
    assert kb.DEFAULT_ROOT == expected
    assert kb.build.__defaults__[0] == expected
    assert kb.build_index.__defaults__[0] == expected


def test_empty_database_and_safe_markdown(db, tmp_path):
    empty = kb.build(db, tmp_path)
    assert empty['source'] == dict(recordings=0, tags=0, notes=0)
    assert empty['tags'] == []
    add_tag(db, '<b>A|B\nC</b>', category='<script>')
    kb.build(db, tmp_path)
    markdown = (tmp_path / 'index.md').read_text()
    assert '&lt;script&gt;' in markdown
    assert '| &lt;b&gt;A&#124;B C&lt;/b&gt; | 0 | 0 分钟 | — |' in markdown
