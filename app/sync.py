import logging

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.mcp import TokenRefreshError
from app.minutes import MinutesSource, RecordingDetail, RecordingMeta, SHANGHAI


class SyncInProgress(Exception):
    """Another worker is already syncing this database."""


@dataclass(frozen=True)
class SyncRun:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    error: str | None = None
    refreshed_token: int = 0

    def result(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != 'id'}


class SyncStore(Protocol):
    def get_recording(self, recording_id: str): ...

    def sync_lock(self) -> AbstractContextManager[None]: ...

    def start_sync(self, started_at: str) -> int: ...

    def finish_sync(self, run: SyncRun) -> None: ...

    def latest_sync(self) -> SyncRun | None: ...

    def upsert_recording(self, meta: RecordingMeta, detail: RecordingDetail, synced_at: str) -> str: ...


def run_sync(source: MinutesSource, store: SyncStore, *, backfill_days: int = 90) -> SyncRun:
    if type(backfill_days) is not int or backfill_days < 1:
        raise ValueError('backfill_days must be a positive integer')
    with store.sync_lock():
        started = datetime.now(SHANGHAI)
        run_id = store.start_sync(started.isoformat())
        fetched = inserted = updated = 0
        errors = []
        seen = set()
        tag_ids = []
        refresh_before = getattr(source, 'refresh_count', 0)
        try:
            if hasattr(source, 'sync_window'):
                batches = (source.sync_window(backfill_days),)
            else:
                batches = (source.list_by_date(started.date() - timedelta(days=offset))
                           for offset in range(backfill_days))
            for recordings in batches:
                for meta in recordings:
                    if meta.recording_id in seen:
                        continue
                    seen.add(meta.recording_id)
                    fetched += 1
                    try:
                        detail = source.get_detail(meta.recording_id)
                    except TokenRefreshError:
                        raise
                    except Exception:
                        # Never persist provider exception text: it may include credentials.
                        errors.append(f'录音 {meta.recording_id}：详情拉取失败，请重试')
                        continue
                    try:
                        previous = store.get_recording(meta.recording_id)
                        outcome = store.upsert_recording(meta, detail, datetime.now(SHANGHAI).isoformat())
                    except Exception:
                        errors.append(f'录音 {meta.recording_id}：校验或入库失败，请重试')
                        continue
                    inserted += outcome == 'inserted'
                    updated += outcome == 'updated'
                    if previous is None or (previous.title, previous.summary_md, previous.transcript_md) != (
                            meta.title or '', detail.summary_md or '', detail.transcript_md or ''):
                        tag_ids.append(meta.recording_id)
        except TokenRefreshError:
            errors.append('千问 OAuth 续期失败，请检查 token 文件、授权或网络后重试')
        except Exception:
            errors.append('录音列表拉取失败，请检查数据源、凭据和网络配置后重试')
        run = SyncRun(run_id, started.isoformat(), datetime.now(SHANGHAI).isoformat(),
                      'failed' if errors else 'success', fetched, inserted, updated,
                      '\n'.join(errors) if errors else None,
                      int(getattr(source, 'refresh_count', 0) > refresh_before))
        store.finish_sync(run)
        # Tags are optional enrichment; never turn a successful sync into failure.
        try:
            from app.tag import run_tags
            run_tags(store, recording_ids=tag_ids, force=True)
        except Exception:
            logging.getLogger(__name__).warning('打标批次失败；录音同步已完成')
        return run
