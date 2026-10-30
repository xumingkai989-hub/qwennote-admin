"""Safe presentation and a single Markdown document shared by copy and download."""
import json
import re
from urllib.parse import quote

from markdown_it import MarkdownIt
from markupsafe import Markup

from app.store import StoredRecording

# Explicitly disable raw HTML. CommonMark also rejects javascript/data HTML links.
MARKDOWN = MarkdownIt('commonmark', {'html': False})
SEGMENT = re.compile(
    r'^\[(\d{2,}:\d{2}(?::\d{2})?\.\d+[–—-]\d{2,}:\d{2}(?::\d{2})?\.\d+)\][ \t]*(.*)$',
    re.MULTILINE,
)


def render_summary(value: str) -> Markup:
    # This is the only trust boundary: only the HTML-disabled renderer output
    # becomes markup; user titles, transcript and export text stay autoescaped.
    return Markup(MARKDOWN.render(value))


def paragraphs(value: str) -> list[str]:
    return [part.strip() for part in re.split(r'\n\s*\n', value.strip()) if part.strip()]


def transcript_segments(value: str) -> list[dict]:
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    matches = list(SEGMENT.finditer(value))
    segments = []
    prefix = value[:matches[0].start()] if matches else value
    if prefix.strip():
        segments.append({'timestamp': '', 'speaker': '', 'paragraphs': paragraphs(prefix)})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        segments.append({'timestamp': '[' + match[1] + ']', 'speaker': match[2],
                         'paragraphs': paragraphs(value[match.end():end])})
    return segments


def export_markdown(recording: StoredRecording) -> str:
    # JSON quoted scalars are also YAML double quoted scalars, including newlines,
    # quotes, colon/hash and string values such as "true" or numeric-looking IDs.
    fields = {
        'recording_id': recording.recording_id,
        'title': recording.title,
        'created_at': recording.created_at.strftime('%Y-%m-%d %H:%M'),
        'duration_min': recording.duration_min,
        'has_transcript': recording.has_transcript,
        'has_summary': recording.has_summary,
    }
    def yaml_scalar(value):
        if value is None:
            return ''
        text = json.dumps(value, ensure_ascii=False)
        # YAML treats these Unicode characters as line separators even inside
        # quotes; escape them so the scalar round trips without folding.
        for char in ('\x85', '\u2028', '\u2029'):
            text = text.replace(char, f'\\u{ord(char):04x}')
        return text

    header = '\n'.join(f'{key}: {yaml_scalar(value)}' for key, value in fields.items())
    summary = recording.summary_md if recording.has_summary else '（无总结）'
    transcript = recording.transcript_md if recording.has_transcript else '（未转录）'
    return (f'---\n{header}\n---\n\n# {recording.title}\n\n'
            f'## 总结\n{summary}\n\n## 转写原文\n{transcript}\n')


def download_disposition(title: str) -> str:
    slug = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', '', title).strip().strip('.') or '录音'
    # UTF-8 filename* preserves Chinese in browsers; the quoted fallback is
    # percent encoded so the HTTP header remains valid ASCII (no CR/LF).
    filename = quote(slug + '.md', safe='')
    return f'attachment; filename="{filename}"; filename*=UTF-8\'\'{filename}'
