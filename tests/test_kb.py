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


def meeting_content(db, ident, title='功能同步', summary='新版范围已定。上线时间另议。', primary=None):
    with db._connect() as conn:
        conn.execute('UPDATE recordings SET title=?, summary_md=? WHERE recording_id=?',
                     (title, summary, ident))
        if primary is not None:
            conn.execute('INSERT INTO recording_primary_tag '
                         '(recording_id, tag_name, source, tag_fingerprint, generated_at) '
                         "VALUES (?, ?, 'rule', 'fixture', ?)", (ident, primary, STAMP))


def test_render_tag_three_meetings(db):
    add_tag(db, 'AI Tutor')
    for day in (11, 13, 12):
        ident = f'r{day}'
        add_meeting(db, ident, ('AI Tutor', '直播', '产品设计'), 21,
                    f'2026-11-{day}T10:04:00+08:00')
        meeting_content(db, ident, primary='AI Tutor')
    tag = named(kb.scan(db)['tags'], 'AI Tutor')
    body = kb.render_tag_file(db, tag, [])
    assert body.startswith('# AI Tutor\n\n> 产品 · 3 场会议 · 63 分钟 · 更新于 2026-11-13 10:04\n')
    assert '## 当前状态\n\n综述未生成。\n' in body
    assert '## 补充记录\n\n暂无补充记录。\n' in body
    assert body.count('### 2026-11-') == 3
    assert body.index('### 2026-11-13') < body.index('### 2026-11-12') < body.index('### 2026-11-11')
    assert '- 21 分钟 · 主议题：AI Tutor · 标签：AI Tutor、直播、产品设计\n' in body
    assert '- 新版范围已定。\n- [查看原文](/recordings/r13)' in body
    assert '上线时间另议' not in body
    assert '## 时间线\n\n| 日期 | 类型 | 内容 |\n|---|---|---|\n| 11-13 | 会议 | 功能同步 |' in body


@pytest.mark.parametrize('count', [20, 25, 35])
def test_render_limits_do_not_change_totals(db, count):
    add_tag(db, 'A')
    for i in range(count):
        add_meeting(db, f'r{i:02}', ('A',), 2, f'2026-11-13T10:{i:02}:00+08:00')
        meeting_content(db, f'r{i:02}', title=f'会议{i:02}')
    body = kb.render_tag_file(db, kb.scan(db)['tags'][0])
    assert f'{count} 场会议 · {count * 2} 分钟' in body
    assert body.count('### ') == 20
    assert body.count('| 会议 |') == min(count, 30)
    assert f'(/recordings/r{count - 1:02})' in body
    assert f'(/recordings/r{count - 20:02})' in body
    if count > 20:
        assert f'另有 {count - 20} 场更早的会议未列出。' in body
        assert f'(/recordings/r{count - 21:02})' not in body
    else:
        assert '更早的会议未列出' not in body


@pytest.mark.parametrize('summary,expected', [
    ('第一句。第二句。', '第一句。'), ('First sentence. Second sentence.', 'First sentence.'),
    ('第一行\n第二行。', '第一行'), ('第一行\r\n第二行', '第一行'),
    ('没有句号', '没有句号'), ('长' * 100, '长' * 60),
    ('长' * 100 + '。后句', '长' * 80 + '…'),
    ('长' * 100 + '\n后句', '长' * 80 + '…'),
    ('长' * 79 + '。', '长' * 79 + '。'),
    ('', '（无总结）'), (None, '（无总结）'), (' \n ', '（无总结）'),
])
def test_render_summary_first_sentence(db, summary, expected):
    add_tag(db, 'A')
    add_meeting(db, 'one', ('A',))
    meeting_content(db, 'one', summary=summary)
    assert f'\n- {expected}\n' in kb.render_tag_file(db, kb.scan(db)['tags'][0])


@pytest.mark.parametrize('minutes,expected', [(None, '时长未知'), (0, '0 分钟')])
def test_render_unknown_duration_and_missing_primary(db, minutes, expected):
    add_tag(db, 'A')
    add_meeting(db, 'one', ('A',), minutes=minutes)
    body = kb.render_tag_file(db, kb.scan(db)['tags'][0])
    assert f'- {expected} · 主议题：— · 标签：A\n' in body


def test_build_unclassified_keeps_real_named_tag_separate(db, tmp_path):
    add_tag(db, '未分类')
    add_meeting(db, 'tagged', ('未分类',))
    add_meeting(db, 'untagged')
    kb.build(db, tmp_path)
    real = (tmp_path / 'tags/未分类.md').read_text()
    virtual = (tmp_path / 'tags/未分类-2.md').read_text()
    assert '/recordings/tagged)' in real and '/recordings/untagged)' not in real
    assert '/recordings/untagged)' in virtual and '/recordings/tagged)' not in virtual
    assert '## 当前状态' in real and '## 补充记录' in real
    assert '## 当前状态' not in virtual and '## 补充记录' not in virtual
    assert '## 会议' in virtual and '## 时间线' in virtual


def test_build_untagged_meeting_file(db, tmp_path):
    add_meeting(db, 'untagged')
    kb.build(db, tmp_path)
    body = (tmp_path / 'tags/未分类.md').read_text()
    assert body.startswith('# 未分类\n')
    assert '/recordings/untagged)' in body
    assert '## 当前状态' not in body and '## 补充记录' not in body


def test_render_merged_alias_and_legacy_membership(db):
    target = add_tag(db, '新标签')
    old = add_tag(db, '旧标签')
    with db._connect() as conn:
        conn.execute("UPDATE tags SET status='merged', merged_into=? WHERE id=?", (target, old))
        conn.execute('INSERT INTO tag_aliases VALUES (?, ?)', ('别名', target))
    add_meeting(db, 'one', ('旧标签', '别名', '新标签'))
    add_meeting(db, 'legacy', ('历史标签',))
    index = kb.scan(db)
    body = kb.render_tag_file(db, named(index['tags'], '新标签'))
    assert body.count('### ') == 1 and '标签：旧标签、别名、新标签' in body
    assert '/recordings/legacy)' not in body
    assert '/recordings/legacy)' in kb.render_tag_file(db, named(index['tags'], '历史标签'))


def test_render_orders_instants_and_equal_times_deterministically(db):
    add_tag(db, 'A')
    for ident, stamp in [('a', '2026-11-14T10:00:00+08:00'),
                         ('b', '2026-11-14T03:00:00+00:00'),
                         ('c', '2026-11-14T11:00:00')]:
        add_meeting(db, ident, ('A',), stamp=stamp)
    body = kb.render_tag_file(db, kb.scan(db)['tags'][0])
    assert body.index('/recordings/c)') < body.index('/recordings/b)') < body.index('/recordings/a)')


def test_category_paths_statistics_and_empty_categories(db, tmp_path):
    from markdown_it import MarkdownIt

    for name in ['ai-tutor', 'AI Tutor', '导/师', '导师', 'a#b%', '名(称)']:
        add_tag(db, name)
    add_tag(db, '技术标签', '技术')
    add_meeting(db, 'one', ('ai-tutor', 'AI Tutor'), 21)
    index = kb.build(db, tmp_path)
    body = (tmp_path / 'categories/产品.md').read_text()
    assert body.startswith('# 产品\n\n> 6 个标签 · 1 场会议 · 21 分钟\n')
    assert '| [ai-tutor](tags/ai-tutor.md) | 1 | 21 分钟 | 11-13 |' in body
    assert '| [AI Tutor](<tags/AI Tutor.md>) | 1 | 21 分钟 | 11-13 |' in body
    assert '[导师](tags/导师-2.md)' in body
    assert '[a#b%](tags/a%23b%25.md)' in body
    assert '[名(称)](<tags/名(称).md>)' in body
    assert '技术标签' not in body
    from urllib.parse import unquote
    links = [child.attrGet('href') for token in MarkdownIt().enable('table').parse(body)
             for child in (token.children or []) if child.type == 'link_open']
    assert len(links) == 6
    assert all((tmp_path / unquote(path)).is_file() for path in links)
    for category in index['categories']:
        assert (tmp_path / category['path']).exists() == bool(category['tags'])


def test_build_bodies_stable_and_database_unchanged(sample, tmp_path, monkeypatch):
    class Clock:
        value = datetime.fromisoformat('2026-11-14T15:30:00+08:00')
        fromisoformat = staticmethod(datetime.fromisoformat)

        @classmethod
        def now(cls, tz):
            return cls.value.astimezone(tz)

    monkeypatch.setattr(kb, 'datetime', Clock)
    with sample._connect() as conn:
        before = '\n'.join(conn.iterdump())
    first = kb.build(sample, tmp_path)
    bodies = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.glob('*/*.md')}
    Clock.value = datetime.fromisoformat('2026-11-15T15:30:00+08:00')
    second = kb.build(sample, tmp_path, tags=['标签00'])
    assert first.pop('generated_at') != second.pop('generated_at')
    assert first == second
    assert bodies == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.glob('*/*.md')}
    assert len(bodies) == 24
    with sample._connect() as conn:
        assert before == '\n'.join(conn.iterdump())


def test_render_safe_markdown(db, tmp_path):
    add_tag(db, '<b>A|B</b>')
    add_meeting(db, 'one/#)escape', ('<b>A|B</b>',))
    meeting_content(db, 'one/#)escape', title='标题|换\n行<script>', summary='<script>危险</script>。')
    index = kb.build(db, tmp_path)
    body = (tmp_path / index['tags'][0]['path']).read_text()
    assert '<script>' not in body and '<b>' not in body
    assert '| 11-13 | 会议 | 标题&#124;换 行&lt;script&gt; |' in body
    assert '(/recordings/one%2F%23%29escape)' in body
