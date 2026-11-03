import logging
import random
import time

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from app.mcp import TokenRefreshError
from app.minutes import MinutesRetryableError, MinutesSource, RecordingDetail, RecordingMeta, SHANGHAI

OVERLAP = timedelta(minutes=10)


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
    skipped: int = 0
    mode: str = 'quick'
    duration_ms: int = 0
    detail_serialized: int = 0

    def result(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != 'id'}


class SyncStore(Protocol):
    def get_recording(self, recording_id: str): ...

    def sync_lock(self) -> AbstractContextManager[None]: ...

    def start_sync(self, started_at: str, mode: str = 'quick') -> int: ...

    def finish_sync(self, run: SyncRun) -> None: ...

    def latest_sync(self) -> SyncRun | None: ...

    def latest_successful_sync(self) -> SyncRun | None: ...

    def upsert_recording(self, meta: RecordingMeta, detail: RecordingDetail, synced_at: str) -> str: ...


def run_sync(source: MinutesSource, store: SyncStore, *, backfill_days: int = 90,
             full: bool = False) -> SyncRun:
    if type(backfill_days) is not int or backfill_days < 1:
        raise ValueError('backfill_days must be a positive integer')
    with store.sync_lock():
        clock_started = time.perf_counter()
        started = datetime.now(SHANGHAI)
        # Full recovery and invalid watermarks scan the bounded backfill window.
        floor = started - timedelta(days=min(backfill_days, started.toordinal() - 1))
        start = floor
        mode = 'full'
        if not full:
            try:
                previous_run = store.latest_successful_sync()
                if previous_run is not None:
                    watermark = datetime.fromisoformat(previous_run.started_at)
                    if watermark.tzinfo is None or watermark > started:
                        raise ValueError('Invalid watermark')
                    start = max(floor, watermark - OVERLAP)
                    mode = 'quick'
            except Exception:
                # A missing/unreadable watermark must never exclude old data.
                start = floor
                mode = 'full'
        run_id = store.start_sync(started.isoformat(), mode=mode)
        fetched = inserted = updated = skipped = 0
        errors = []
        seen = set()
        tag_ids = []
        judge_ids = []
        refresh_before = getattr(source, 'refresh_count', 0)
        detail_serialized = False
        try:
            if hasattr(source, 'sync_range'):
                batches = (source.sync_range(start, started, early_stop=mode == 'quick'),)
            else:
                # Day-only providers cannot stop within a day, but never request
                # earlier days or let overinclusive responses reach detail/storage.
                batches = (source.list_by_date(started.date() - timedelta(days=offset))
                           for offset in range((started.date() - start.date()).days + 1))
            # Providers may return pages or days in any order. Sort the whole
            # window before fetching details so new content reaches storage first.
            recordings = sorted((meta for batch in batches for meta in batch
                                 if start <= meta.created_at <= started),
                                key=lambda meta: meta.created_at, reverse=True)
            pending = []
            previous_by_id = {}
            for meta in recordings:
                if meta.recording_id in seen:
                    continue
                seen.add(meta.recording_id)
                fetched += 1
                try:
                    previous = store.get_recording(meta.recording_id)
                except Exception:
                    errors.append(f'录音 {meta.recording_id}：校验或入库失败，请重试')
                    continue
                if (mode == 'quick' and previous is not None
                        and previous.has_summary and previous.has_transcript):
                    skipped += 1
                    continue
                pending.append(meta)
                previous_by_id[meta.recording_id] = previous

            # Submit only one wave of at most 3. Once throttling is established,
            # there are no queued requests left to race the serial fallback.
            consecutive_limited = 0
            with ThreadPoolExecutor(max_workers=3) as pool:
                for offset in range(0, len(pending), 3):
                    wave = pending[offset:offset + 3]
                    if detail_serialized:
                        results = {m.recording_id: _fetch_detail(source, m) for m in wave}
                    else:
                        futures = {m.recording_id: pool.submit(_fetch_detail, source, m) for m in wave}
                        results = {rid: future.result() for rid, future in futures.items()}
                    for meta in wave:
                        detail, error = results[meta.recording_id]
                        limited = isinstance(error, MinutesRetryableError) and error.status_code == 429
                        consecutive_limited = consecutive_limited + 1 if limited else 0
                        if consecutive_limited >= 3 and not detail_serialized:
                            detail_serialized = True
                            logging.getLogger(__name__).warning('连续 3 条录音限流失败，本轮剩余详情请求降为串行')
                        if isinstance(error, TokenRefreshError):
                            raise error
                        if error is not None:
                            # Never persist exception text: it may contain credentials.
                            errors.append(f'录音 {meta.recording_id}：详情拉取失败，请重试')
                            continue
                        previous = previous_by_id[meta.recording_id]
                        try:
                            outcome = store.upsert_recording(meta, detail, datetime.now(SHANGHAI).isoformat())
                        except Exception:
                            errors.append(f'录音 {meta.recording_id}：校验或入库失败，请重试')
                            continue
                        inserted += outcome == 'inserted'
                        updated += outcome == 'updated'
                        if previous is None or (previous.summary_md, previous.transcript_md,
                                previous.has_summary, previous.has_transcript) != (
                                detail.summary_md or '', detail.transcript_md or '', detail.has_summary, detail.has_transcript):
                            judge_ids.append(meta.recording_id)
                        if previous is None or (previous.title, previous.summary_md, previous.transcript_md) != (
                                meta.title or '', detail.summary_md or '', detail.transcript_md or ''):
                            tag_ids.append(meta.recording_id)
        except TokenRefreshError:
            errors.append('千问 OAuth 续期失败，请检查 token 文件、授权或网络后重试')
        except Exception:
            errors.append('录音列表拉取失败，请检查数据源、凭据和网络配置后重试')
        run = SyncRun(id=run_id, started_at=started.isoformat(),
                      finished_at=datetime.now(SHANGHAI).isoformat(),
                      status='failed' if errors else 'success', mode=mode, fetched=fetched,
                      inserted=inserted, updated=updated, skipped=skipped,
                      error='\n'.join(errors) if errors else None,
                      duration_ms=max(1, int((time.perf_counter() - clock_started) * 1000)),
                      detail_serialized=int(detail_serialized),
                      refreshed_token=int(getattr(source, 'refresh_count', 0) > refresh_before))
        store.finish_sync(run)
        try:
            from app.hotword_judge import run_judgments
            if judge_ids:
                run_judgments(store, recording_ids=judge_ids)
        except Exception:
            logging.getLogger(__name__).warning('热词判断批次失败；未判断内容保持原文')
        # Tags are optional enrichment; never turn a successful sync into failure.
        try:
            from app.tag import run_tags
            if not hasattr(store, 'get_settings') or store.get_settings().tag_enabled:
                run_tags(store, recording_ids=tag_ids, force=True)
        except Exception:
            logging.getLogger(__name__).warning('打标批次失败；录音同步已完成')
        # Keep enrichment at its existing point after the first finish_sync,
        # then include it in the total wall-clock duration returned to callers.
        run = replace(run, finished_at=datetime.now(SHANGHAI).isoformat(),
                      duration_ms=max(1, int((time.perf_counter() - clock_started) * 1000)))
        store.finish_sync(run)
        return run


def _fetch_detail(source: MinutesSource, meta: RecordingMeta):
    for attempt in range(3):
        try:
            return source.get_detail(meta.recording_id, meta=meta), None
        except MinutesRetryableError as error:
            if attempt == 2:
                return None, error
            time.sleep((1, 3)[attempt] + random.uniform(0, 0.25))
        except Exception as error:
            return None, error
