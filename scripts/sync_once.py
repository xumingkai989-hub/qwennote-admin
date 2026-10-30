from app.mcp import McpMinutesSource
"""systemd entry point using the application's existing synchronization pipeline."""
import argparse
import json

from app.minutes import FixtureMinutesSource, HttpMinutesSource
from app.store import SQLiteStore
from app.sync import run_sync
from scripts.deployment_runtime import deployment_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('quick', 'full'), default=None,
                        help='同步模式；省略时读取 sync_mode_default（默认 quick）')
    args = parser.parse_args(argv)
    settings = deployment_settings()
    store = SQLiteStore(settings.db_path)
    store.migrate()
    source = (FixtureMinutesSource(settings.minutes_fixture_dir)
              if settings.minutes_source == 'fixture' else
              McpMinutesSource() if settings.minutes_source == 'mcp' else HttpMinutesSource())
    preferences = store.get_settings(backfill_days=settings.backfill_days)
    mode = args.mode or preferences.sync_mode_default
    result = run_sync(source, store, backfill_days=preferences.backfill_days, full=mode == 'full')
    print(json.dumps(result.result(), ensure_ascii=False))
    return 0 if result.status == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
