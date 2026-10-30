from app.mcp import McpMinutesSource
"""systemd entry point using the application's existing synchronization pipeline."""
import json

from app.minutes import FixtureMinutesSource, HttpMinutesSource
from app.store import SQLiteStore
from app.sync import run_sync
from scripts.deployment_runtime import deployment_settings


def main():
    settings = deployment_settings()
    store = SQLiteStore(settings.db_path)
    store.migrate()
    source = (FixtureMinutesSource(settings.minutes_fixture_dir)
              if settings.minutes_source == 'fixture' else
              McpMinutesSource() if settings.minutes_source == 'mcp' else HttpMinutesSource())
    result = run_sync(source, store, backfill_days=settings.backfill_days)
    print(json.dumps(result.result(), ensure_ascii=False))
    return 0 if result.status == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
