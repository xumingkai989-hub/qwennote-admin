"""Read-only, Bearer-authenticated API and per-request audit boundary."""
import hashlib
import re
from datetime import date

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.store import RecordingCard, StoredRecording
from app.recording_content import apply_recording_hotwords


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(400, 'invalid parameters') from None


def date_bounds(request: Request) -> tuple[date | None, date | None]:
    params = request.query_params
    if 'date' in params:
        if 'from' in params or 'to' in params:
            raise HTTPException(400, 'invalid parameters')
        day = parse_date(params['date'])
        return day, day
    start = parse_date(params['from']) if 'from' in params else None
    end = parse_date(params['to']) if 'to' in params else None
    if start is not None and end is not None and start > end:
        raise HTTPException(400, 'invalid parameters')
    return start, end


def integer_param(request: Request, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.query_params.get(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise HTTPException(400, 'invalid parameters') from None


def recording_item(recording: RecordingCard) -> dict:
    return {
        'recording_id': recording.recording_id, 'title': recording.title,
        'created_at': recording.created_at.isoformat(), 'duration_min': recording.duration_min,
        'status': recording.status[0].replace('-', '_'),
        'has_transcript': recording.has_transcript, 'has_summary': recording.has_summary,
    }


def daily_markdown(day: str, recordings: list[StoredRecording]) -> str:
    total_minutes = sum(item.duration_min or 0 for item in recordings)
    lines = [f'# {day} 工作记录', '', f'共 {len(recordings)} 条录音，合计 {total_minutes} 分钟。', '',
             '时间为笔记创建时间（Asia/Shanghai），非录音开始时间。', '']
    if any(item.duration_min is None for item in recordings):
        lines += ['部分录音时长未知，合计仅包含已知时长。', '']
    lines += ['## 录音清单', '| 时间 | 标题 | 时长 | 状态 |', '|---|---|---|---|']
    for item in recordings:
        title = ' '.join(item.title.splitlines()).replace('\\', '\\\\').replace('|', '\\|')
        duration = f'{item.duration_min} 分钟' if item.duration_min is not None else '时长未知'
        lines.append(f'| {item.created_at:%H:%M} | {title} | {duration} | {item.status[1]} |')
    lines += ['', '## 详细内容', '']
    if not recordings:
        lines += ['当天没有录音。', '']
    for item in recordings:
        title = ' '.join(item.title.splitlines()) or '未命名录音'
        duration = f'{item.duration_min} 分钟' if item.duration_min is not None else '时长未知'
        lines += [f'### {item.created_at:%H:%M} {title}（{duration}）', '', '#### 总结', '',
                  item.summary_md if item.has_summary else '（暂无总结）', '', '#### 转写原文', '',
                  item.transcript_md if item.has_transcript else '（未转录）', '']
    return '\n'.join(lines)


def install_public_api(app: FastAPI) -> None:
    @app.middleware('http')
    async def authenticate_and_audit(request: Request, call_next):
        path = request.url.path
        if path != '/api/v1' and not path.startswith('/api/v1/'):
            return await call_next(request)
        store = request.app.state.store
        headers = request.headers.getlist('authorization')
        match = re.fullmatch(r'Bearer (qn_[A-Za-z0-9]{32})', headers[0], re.IGNORECASE) if len(headers) == 1 else None
        key_id, active = None, False
        if match is not None:
            digest = hashlib.sha256(match[1].encode()).hexdigest()
            key_id, active = await run_in_threadpool(store.authenticate_api_key, digest)
        status_code = 500
        try:
            response = (await call_next(request) if active else
                        JSONResponse({'detail': 'unauthorized'}, status_code=401))
            status_code = response.status_code
            response.headers['Cache-Control'] = 'no-store'
            return response
        finally:
            # Never retain Authorization, query strings, response bodies or
            # exception text. Even rejected credentials get an audit row.
            await run_in_threadpool(store.audit_api_call, key_id, path, status_code)

    router = APIRouter(prefix='/api/v1')

    @router.get('/recordings')
    def recordings(request: Request):
        start, end = date_bounds(request)
        limit = integer_param(request, 'limit', 20, 1, 100)
        offset = integer_param(request, 'offset', 0, 0, 2**63 - 1)
        batch = request.app.state.store.read_recordings(start, end, limit=limit, offset=offset)
        return {'total': batch.total, 'limit': limit, 'offset': offset,
                'items': [recording_item(item) for item in batch.items]}

    @router.get('/recordings/{recording_id}')
    def recording_detail(request: Request, recording_id: str):
        item = request.app.state.store.get_recording(recording_id)
        if item is None:
            raise HTTPException(404, 'recording not found')
        item = apply_recording_hotwords(item, request.app.state.store.apply_hotwords)
        return {**recording_item(item), 'summary_md': item.summary_md, 'transcript_md': item.transcript_md}

    @router.get('/daily/{day}')
    def daily(request: Request, day: str):
        selected = parse_date(day)
        format = request.query_params.get('format', 'json')
        if format not in {'json', 'md'}:
            raise HTTPException(400, 'invalid parameters')
        batch = request.app.state.store.read_recordings(selected, selected, limit=None)
        if format == 'md':
            items = [apply_recording_hotwords(item, request.app.state.store.apply_hotwords)
                     for item in batch.items]
            return Response(daily_markdown(day, items), media_type='text/markdown')
        return {'date': day, 'count': batch.total, 'recordings': [recording_item(item) for item in batch.items]}

    @router.get('/search')
    def search(request: Request):
        query = request.query_params.get('q', '').strip()
        if not query:
            raise HTTPException(400, 'invalid parameters')
        start, end = date_bounds(request)
        limit = integer_param(request, 'limit', 20, 1, 100)
        batch = request.app.state.store.read_recordings(start, end, query=query, limit=limit)
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        return {'q': query, 'total': batch.total, 'items': [
            {'recording_id': item.recording_id, 'title': item.title, 'created_at': item.created_at.isoformat(),
             'matched': [field for field, value in [('title', item.title), ('summary', item.summary_md),
                                                  ('transcript', item.transcript_md)] if pattern.search(value)]}
            for item in batch.items
        ]}

    app.include_router(router)
