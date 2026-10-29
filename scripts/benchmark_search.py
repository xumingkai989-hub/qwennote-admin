"""Offline search benchmark: python -m scripts.benchmark_search (temporary DB only)."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from app.minutes import RecordingDetail, RecordingMeta, SHANGHAI
from app.store import SQLiteStore


def main() -> None:
    with TemporaryDirectory(prefix='qwennote-search-') as directory:
        store = SQLiteStore(Path(directory) / 'benchmark.db')
        store.migrate()
        now = datetime(2026, 10, 29, 12, tzinfo=SHANGHAI)
        for i in range(1000):
            recording_id = f'benchmark-{i:04}'
            keyword = '数据看板' if i % 10 == 0 else '日常会议'
            store.upsert_recording(
                RecordingMeta(recording_id, f'{keyword}讨论 {i}', now - timedelta(minutes=i), 30),
                RecordingDetail(recording_id, '讨论项目进度与后续安排。' * 50,
                                '发言人：本周继续推进各项任务，核对进度并记录会议结论。' * 100 + keyword, True, True),
                now.isoformat(),
            )
        results = []
        for query, path in [('数据看板', 'FTS5 trigram MATCH'), ('看板', 'LIKE search_text scan')]:
            # Warm up once; then measure the real Store operation, including
            # connection, COUNT, sorting and loading the first 20 full results.
            store.search_recordings(query)
            timings = []
            for _ in range(100):
                start = perf_counter()
                page = store.search_recordings(query)
                timings.append((perf_counter() - start) * 1000)
                assert page.total == 100 and len(page.items) == 20
            results.append({'query': query, 'path': path, 'matches': page.total,
                            'median_ms': round(median(timings), 3),
                            'p95_ms': round(sorted(timings)[94], 3)})
        with sqlite3.connect(store.db_path) as conn:
            plans = {
                'FTS': [r[3] for r in conn.execute('EXPLAIN QUERY PLAN SELECT recording_id FROM recordings '
                    'WHERE rowid IN (SELECT rowid FROM recordings_fts WHERE recordings_fts MATCH ?) '
                    'ORDER BY created_at DESC, recording_id DESC LIMIT 20', ('"数据看板"',))],
                'LIKE': [r[3] for r in conn.execute('EXPLAIN QUERY PLAN SELECT recording_id FROM recordings '
                    "WHERE search_text LIKE ? ESCAPE '\\' "
                    'ORDER BY created_at DESC, recording_id DESC LIMIT 20', ('%看板%',))],
            }
        print(json.dumps({'sqlite': sqlite3.sqlite_version, 'recordings': 1000,
                          'runs_per_query': 100, 'page_size': 20, 'results': results,
                          'query_plans': plans}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
