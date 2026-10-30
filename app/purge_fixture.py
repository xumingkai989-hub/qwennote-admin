"""Delete only fixture-prefixed recordings from the configured database."""
import os
from pathlib import Path

from app.store import SQLiteStore


def main():
    path = os.environ.get('DB_PATH', '').strip()
    if not path:
        print('DB_PATH is required')
        return 1
    store = SQLiteStore(Path(path).expanduser())
    store.migrate()
    with store._connect() as conn:
        cursor = conn.execute("DELETE FROM recordings WHERE substr(recording_id, 1, 8) = 'fixture-'")
        print(f'已删除 {cursor.rowcount} 条 fixture 记录')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
