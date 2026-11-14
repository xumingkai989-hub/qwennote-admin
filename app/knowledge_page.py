"""File-backed knowledge pages; only explicit rebuilds read meeting data."""
import json
import math
import secrets
from datetime import datetime
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import kb
from app.minutes import SHANGHAI
from app.recording_content import render_summary
from app.session import SESSION_MAX_AGE

COMMANDS = (
    'qwennote-kb list --format json',
    'qwennote-kb show "AI Tutor" --format json',
    'qwennote-kb search 状态机 --limit 5 --format json',
    'qwennote-kb note add "AI Tutor" --text "…" --source codex',
)


def build_agent_prompt(kb_cli='qwennote-kb'):
    return f'''你是我的会议知识助手。QwenNote 已经把会议按标签整理成知识库，你直接读它，不要让我贴会议内容。

## 先看全貌
执行：{kb_cli} list --format json
返回所有标签及其会议数、总时长、补充记录数、最近会议日期。

## 再读具体主题
执行：{kb_cli} show "<标签>" --format json
返回的 data.content 字段是完整的 Markdown，固定四段：
- 当前状态：3-5 句综述，说明在推进什么、进展到哪、卡在哪
- 补充记录：我和之前的 agent 写进去的结论，按时间倒序，优先级高于会议原文
- 会议：最多 20 场，每场带日期、时长、一句话摘要和原文链接
- 时间线：会议与补充记录的合并视图

## 搜索
执行：{kb_cli} search <关键词> --limit 5 --format json

## 写入
执行：{kb_cli} note add "<标签>" --text "<你的结论>" --source codex
- 只在你有明确结论时写，不要复述会议内容
- 单条不超过 2000 字
- 写完立刻 show 读回，确认记录真的进了文件

## 规则
- 先 list 再 show，不要盲目遍历所有文件
- 只需要概览时，读「当前状态」一段就够，不必读完整篇
- 标签名含中文和空格，命令里必须加引号
- 引用会议时带上日期和标题，不要只说「之前讨论过」
- 知识库是派生产物，不要直接编辑 knowledge/ 下的文件，写内容只能走 note add

## 我现在要你做的事
<在这里写你的具体任务>'''


def display_date(value, pattern='%Y-%m-%d %H:%M'):
    if value is None:
        return '—'
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI)
    return stamp.astimezone(SHANGHAI).strftime(pattern)


def read_index(root):
    """Treat malformed JSON/schema as unavailable, retaining generated ordering."""
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid count')

    try:
        index = json.loads((root / 'index.json').read_text(encoding='utf-8'))
        if not isinstance(index['generated_at'], str):
            raise ValueError('invalid generation date')
        index['updated'] = display_date(index['generated_at'])
        number(index['source']['recordings'])
        if not isinstance(index['tags'], list) or not isinstance(index['categories'], list):
            raise ValueError('invalid entries')
        categories = {}
        for category in index['categories']:
            if not isinstance(category['name'], str) or category['name'] in categories:
                raise ValueError('invalid category')
            category['items'] = []
            categories[category['name']] = category
        for tag in index['tags']:
            if not isinstance(tag['name'], str) or not isinstance(tag['path'], str):
                raise ValueError('invalid tag')
            for key in ('sessions', 'minutes', 'notes'):
                number(tag[key])
            tag['recent'] = display_date(tag['last_session_at'], '%m-%d')
            tag['url'] = '/knowledge/' + quote(tag['name'], safe='')
            categories[tag['category']]['items'].append(tag)
        index['groups'] = [category for category in categories.values() if category['items']]
        return index
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def install_knowledge(app, *, settings, templates, sessions, authorize, valid_csrf,
                      csrf, session_binding, parse_form, error_page):
    cookie = 'knowledge_csrf'

    def access(request):
        subject = sessions.decode(request.cookies.get('session'))
        user = app.state.store.get_user(subject) if subject else None
        return user if user and user.is_active else None

    def denied(request):
        return error_page(request, 403, '无访问权限', '请使用白名单账户登录后查看知识库。')

    def listing(request, user, error=None):
        admin = bool(set(settings.admin_open_ids).intersection((user.subject, user.union_id, user.open_id)))
        token = ''
        if admin:
            token = request.cookies.get(cookie, '')
            if not valid_csrf(request, token, cookie_name=cookie):
                token = csrf.encode(session_binding(request) + ':' + secrets.token_urlsafe(32))
        kb_cli = 'qwennote-kb'
        response = templates.TemplateResponse(request=request, name='knowledge.html', context={
            'title': '知识库', 'index': read_index(kb.configured_root()),
            'commands': COMMANDS,
            'kb_prompt': build_agent_prompt(kb_cli), 'kb_cli': kb_cli,
            'csrf_token': token, 'error': error,
        }, headers={'Cache-Control': 'no-store'})
        if admin:
            response.set_cookie(cookie, token, max_age=SESSION_MAX_AGE, httponly=True,
                                secure=settings.secure_cookies, samesite='lax', path='/knowledge')
        return response

    @app.get('/knowledge')
    def knowledge(request: Request):
        user = access(request)
        return listing(request, user) if user else denied(request)

    @app.post('/knowledge/rebuild')
    async def rebuild(request: Request):
        user = access(request)
        if user is None:
            return denied(request)
        error = authorize(request, message='仅管理员可以重新生成知识库。')
        if error is not None:
            return error
        fields = await parse_form(request, cookie_name=cookie)
        if not isinstance(fields, dict):
            return fields
        try:
            result = await run_in_threadpool(kb.build, app.state.store, kb.configured_root(), None)
            if result['errors']:
                return listing(request, user, '知识库部分内容生成失败，请稍后重试。')
        except Exception:
            # Never expose provider errors, filesystem paths or credentials in HTML.
            return listing(request, user, '知识库重新生成失败，请稍后重试。')
        return RedirectResponse('/knowledge', status_code=302, headers={'Cache-Control': 'no-store'})

    @app.get('/knowledge/{tag:path}')
    def knowledge_tag(request: Request, tag: str):
        if access(request) is None:
            return denied(request)
        root = kb.configured_root()
        index = read_index(root)
        entry = next((item for item in index['tags'] if item['name'] == tag), None) if index else None
        if entry is not None:
            try:
                content = kb._safe_path(root, entry['path'], 'tags').read_text(encoding='utf-8')
            except (OSError, ValueError):
                entry = None
        if entry is None:
            return error_page(request, 404, '标签不存在', '找不到该标签，请返回知识库。')
        return templates.TemplateResponse(request=request, name='knowledge_detail.html', context={
            'title': entry['name'] + ' · 知识库', 'tag': entry,
            'updated': index['updated'], 'content_html': render_summary(content),
        }, headers={'Cache-Control': 'no-store'})
