"""Safe presentation and a single Markdown document shared by copy and download."""
import hashlib
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

# Soft avatar backgrounds, dark avatar/light-mode text, dark-mode name text.
# Avatar foreground/background pairs all exceed WCAG's 4.5:1 contrast ratio.
SPEAKER_PALETTE = (
    ('#E8EFFF', '#3553A3', '#AFC4FF'),
    ('#E5F4EF', '#24664F', '#91D5BB'),
    ('#F3EAFE', '#71469B', '#D3B2F2'),
    ('#FCE9EF', '#9B3D60', '#F3ADC6'),
    ('#FFF0DF', '#85551E', '#E9C393'),
    ('#E3F3F8', '#226477', '#93CCDD'),
    ('#F0EDE6', '#665B43', '#D0C5AC'),
    ('#EEEAFE', '#57469B', '#BEB3EF'),
)


def format_offset(milliseconds: int) -> str:
    minutes, seconds = divmod(milliseconds // 1000, 60)
    return f'{minutes:02d}:{seconds:02d}'


def transcript_presentation(segments: list[dict]) -> list[dict]:
    """Decorate parsed text for the view without changing source/export content."""
    result = []
    previous_speaker = None
    for segment in segments:
        speaker = segment['speaker'].strip() or '未知发言人'
        color_index = hashlib.sha256(speaker.encode('utf-8')).digest()[0] % len(SPEAKER_PALETTE)
        timestamp = segment['timestamp']
        offset_ms = None
        if timestamp:
            start = re.split('[–—-]', timestamp[1:-1])[0]
            clock, fraction = start.split('.')
            seconds = 0
            for part in clock.split(':'):
                seconds = seconds * 60 + int(part)
            offset_ms = seconds * 1000 + int((fraction + '000')[:3])
        result.append({
            **segment, 'speaker': speaker, 'initial': speaker[0].upper(),
            'colors': SPEAKER_PALETTE[color_index],
            'short_timestamp': format_offset(offset_ms) if offset_ms is not None else '',
            'continuation': speaker == previous_speaker,
        })
        previous_speaker = speaker
    return result


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
