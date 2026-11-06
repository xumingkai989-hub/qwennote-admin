import logging
import re
import sqlite3
import time

from collections import Counter

import httpx

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from app.mcp import TokenRefreshError
from app.minutes import MinutesRetryableError, MinutesSource, RecordingDetail, RecordingMeta, SHANGHAI

OVERLAP = timedelta(minutes=10)
BACKFILL_LIMIT = 3
DETAIL_WORKERS = 2


def _storage_error(error: Exception) -> str:
    if isinstance(error, sqlite3.Error):
        diagnostic = re.search(r'no such (?:tokenizer(?::\s*trigram\b)?|module:\s*fts5\b)',
                               str(error), re.IGNORECASE)
        if diagnostic:
            return f'校验或入库失败（{diagnostic.group()}）'
    return '校验或入库失败，请重试'


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
    enrichment_run_id: int | None = None
    note: str | None = None
    http_status: str | None = None

    def result(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != 'id'}


class SyncStore(Protocol):
    def get_recording(self, recording_id: str): ...

    def complete_recording_ids(self) -> set[str]: ...

    def backfill_candidates(self, now: datetime, limit: int) -> list[RecordingMeta]: ...

    def finish_backfill(self, recording_id: str, now: datetime, *, status: str,
                        http_status: int | None = None) -> None: ...

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
        http_failures = Counter()
        backfilled = 0
        seen = set()
        tag_ids = []
        judge_ids = []
        refresh_before = getattr(source, 'refresh_count', 0)
        detail_serialized = False
        complete_ids = store.complete_recording_ids()
        candidates = store.backfill_candidates(started, BACKFILL_LIMIT)
        backfill_ids = {meta.recording_id for meta in candidates}
        recordings = []
        fatal_auth = False
        try:
            if hasattr(source, 'sync_range'):
                batches = (source.sync_range(start, started, early_stop=mode == 'quick',
                                             complete_ids=complete_ids),)
            else:
                batches = (source.list_by_date(started.date() - timedelta(days=offset))
                           for offset in range((started.date() - start.date()).days + 1))
            # Old missing/incomplete entries explicitly survive the quick window.
            recordings = sorted((meta for batch in batches for meta in batch
                                 if meta.created_at <= started and (meta.created_at >= start or
                                     (mode == 'quick' and meta.recording_id not in complete_ids))),
                                key=lambda meta: meta.created_at, reverse=True)
        except TokenRefreshError:
            errors.append('千问 OAuth 续期失败，请检查 token 文件、授权或网络后重试')
            fatal_auth = True
        except Exception as error:
            _count_http_failure(http_failures, error)
            errors.append('录音列表拉取失败，请检查数据源、凭据和网络配置后重试')

        # This independent stage also runs when listing fails. Existing incomplete
        # rows only enter through the bounded, due queue, even during full sync.
        pending = []
        previous_by_id = {}
        for meta in ([] if fatal_auth else [*recordings, *candidates]):
            if meta.recording_id in seen:
                continue
            seen.add(meta.recording_id)
            fetched += 1
            try:
                previous = store.get_recording(meta.recording_id)
            except Exception as error:
                errors.append(f'录音 {meta.recording_id}：{_storage_error(error)}')
                continue
            complete = previous is not None and previous.has_transcript
            if ((mode == 'quick' and complete) or
                    (previous is not None and not complete and meta.recording_id not in backfill_ids)):
                skipped += 1
                continue
            pending.append(meta)
            previous_by_id[meta.recording_id] = previous

        # Only one wave is submitted at a time; no queued requests race fallback.
        consecutive_limited = 0
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            for offset in range(0, len(pending), DETAIL_WORKERS):
                wave = pending[offset:offset + DETAIL_WORKERS]
                if detail_serialized:
                    results = {m.recording_id: _fetch_detail(source, m) for m in wave}
                else:
                    futures = {m.recording_id: pool.submit(_fetch_detail, source, m) for m in wave}
                    results = {rid: future.result() for rid, future in futures.items()}
                for meta in wave:
                    detail, error = results[meta.recording_id]
                    code = _http_status(error)
                    consecutive_limited = consecutive_limited + 1 if code == 429 else 0
                    if consecutive_limited >= 2 and not detail_serialized:
                        detail_serialized = True
                        logging.getLogger(__name__).warning('连续 2 条录音限流失败，本轮剩余详情请求降为串行')
                    if isinstance(error, TokenRefreshError):
                        fatal_auth = True
                        errors.append('千问 OAuth 续期失败，请检查 token 文件、授权或网络后重试')
                    if error is not None:
                        _count_http_failure(http_failures, error)
                        if not isinstance(error, TokenRefreshError):
                            errors.append(f'录音 {meta.recording_id}：详情拉取失败，请重试')
                        if meta.recording_id in backfill_ids:
                            store.finish_backfill(meta.recording_id, datetime.now(SHANGHAI),
                                status='rate_limited' if code == 429 else (
                                    'upstream_error' if code is not None or isinstance(error,
                                        (MinutesRetryableError, httpx.TimeoutException, TimeoutError)) else 'bad_response'),
                                http_status=code)
                        continue
                    previous = previous_by_id[meta.recording_id]
                    try:
                        outcome = store.upsert_recording(meta, detail, datetime.now(SHANGHAI).isoformat())
                    except Exception as error:
                        errors.append(f'录音 {meta.recording_id}：{_storage_error(error)}')
                        if meta.recording_id in backfill_ids:
                            store.finish_backfill(meta.recording_id, datetime.now(SHANGHAI), status='bad_response')
                        continue
                    complete = detail.has_transcript
                    checked_at = datetime.now(SHANGHAI)
                    backfill_status = ('ok' if complete else 'pending_upstream'
                                       if checked_at - meta.created_at < timedelta(hours=24)
                                       else 'no_upstream_content')
                    store.finish_backfill(meta.recording_id, checked_at, status=backfill_status)
                    backfilled += meta.recording_id in backfill_ids and complete
                    inserted += outcome == 'inserted'
                    updated += outcome == 'updated'
                    if previous is None or (previous.summary_md, previous.transcript_md,
                            previous.has_summary, previous.has_transcript) != (
                            detail.summary_md or '', detail.transcript_md or '', detail.has_summary, detail.has_transcript):
                        judge_ids.append(meta.recording_id)
                    if previous is None or (previous.title, previous.summary_md, previous.transcript_md) != (
                            meta.title or '', detail.summary_md or '', detail.transcript_md or ''):
                        tag_ids.append(meta.recording_id)
                if fatal_auth:
                    break
        run = SyncRun(id=run_id, started_at=started.isoformat(),
                      finished_at=datetime.now(SHANGHAI).isoformat(),
                      status='failed' if errors else 'success', mode=mode, fetched=fetched,
                      inserted=inserted, updated=updated, skipped=skipped,
                      error='\n'.join(errors) if errors else None,
                      note=f'backfilled={backfilled}',
                      http_status=','.join(f'{code}×{count}' for code, count in sorted(http_failures.items())) or None,
                      duration_ms=max(1, int((time.perf_counter() - clock_started) * 1000)),
                      detail_serialized=int(detail_serialized),
                      refreshed_token=int(getattr(source, 'refresh_count', 0) > refresh_before))
        store.finish_sync(run)
    # The producer lock is released before tasks become visible to any worker.
    if run.status == 'success':
        from app.enrichment import enqueue
        enrichment_run_id = enqueue(store, [*tag_ids, *judge_ids], sync_run_id=run_id,
                                    tag_ids=tag_ids, hotword_ids=judge_ids)
        run = replace(run, enrichment_run_id=enrichment_run_id)
    run = replace(run, finished_at=datetime.now(SHANGHAI).isoformat(),
                  duration_ms=max(1, int((time.perf_counter() - clock_started) * 1000)))
    store.finish_sync(run)
    return run


def _http_status(error):
    code = (error.response.status_code if isinstance(error, httpx.HTTPStatusError)
            else getattr(error, 'status_code', None))
    return code if type(code) is int and 100 <= code <= 599 else None


def _count_http_failure(counts, error):
    code = _http_status(error)
    if code is not None:
        counts[code] += 1


def _fetch_detail(source: MinutesSource, meta: RecordingMeta):
    for attempt in range(3):
        try:
            return source.get_detail(meta.recording_id, meta=meta), None
        except Exception as error:
            code = _http_status(error)
            retryable = (code == 429 or (code is not None and 500 <= code < 600)
                         or isinstance(error, (httpx.TimeoutException, TimeoutError))
                         or (isinstance(error, MinutesRetryableError) and code is None))
            if attempt == 2 or not retryable:
                return None, error
            time.sleep(((30, 60) if code == 429 else (2, 6))[attempt])
