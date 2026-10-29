"""Recompute search text and rebuild the trigram index from existing recordings."""
import os
from pathlib import Path

from app.store import SQLiteStore


def main() -> None:
    db_path = os.environ.get('DB_PATH', '').strip()
    if not db_path:
        raise SystemExit('DB_PATH is required')
    SQLiteStore(Path(db_path).expanduser()).rebuild_search()
    print('搜索文本与 FTS5 trigram 索引已重建')


if __name__ == '__main__':
    main()
