"""Date groups and compact previews for the home view; storage stays unchanged."""
import re
from dataclasses import replace

from markdown_it import MarkdownIt

from app.minutes import SHANGHAI
from app.store import RecordingCard, Store


PREVIEW_MARKDOWN = MarkdownIt('commonmark', {'html': True}).enable(['table', 'strikethrough'])


def summary_excerpt(value: str) -> str:
    def text(tokens):
        for token in tokens:
            if token.children:
                yield from text(token.children)
            elif token.type in ('text', 'code_inline', 'code_block', 'fence'):
                yield token.content
            elif token.type in ('softbreak', 'hardbreak') or token.block:
                yield ' '

    plain = ''.join(text(PREVIEW_MARKDOWN.parse(value)))
    plain = re.sub(r'(^|\s)\[[ xX]\]\s+', r'\1', plain)
    return ' '.join(plain.split())[:120]


def duration_label(minutes: int | None) -> str:
    # The existing source stores rounded minutes, not seconds. Do not invent
    # second-level precision or change the source/sync contract for a UI task.
    if minutes is None:
        return ''
    hours, remainder = divmod(minutes, 60)
    return (f'{hours}小时' if hours else '') + (f'{remainder}分' if remainder or not hours else '')


def date_groups(items: list[RecordingCard], store: Store) -> list[dict]:
    groups = []
    localized = [replace(item, created_at=item.created_at.astimezone(SHANGHAI)) for item in items]
    for item in sorted(localized, key=lambda row: (row.created_at, row.recording_id), reverse=True):
        day = item.created_at.date()
        if not groups or groups[-1]['date'] != day:
            groups.append({'date': day, 'label': f'{day.month}月{day.day}日 周' + '一二三四五六日'[day.weekday()], 'items': []})
        # Fetch only the current page's summaries through the existing API.
        # Full source text and transcripts never enter the template context.
        detail = store.get_recording(item.recording_id) if item.has_summary else None
        groups[-1]['items'].append({
            'recording': item,
            'summary': summary_excerpt(detail.summary_md) if detail and detail.has_summary else '',
            'duration': duration_label(item.duration_min),
        })
    return groups
