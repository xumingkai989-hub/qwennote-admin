"""One-pass, longest-first presentation substitutions for Markdown bodies."""
import re
from collections import Counter


MARKDOWN_SPECIAL = re.compile(r'([\\`*_{}\[\]()#+.!|<>~&-])')
# Preserve existing Markdown syntax, code, escapes, link destinations and IDs.
# A label remains body text; URLs and reference identifiers do not.
PROTECTED_INLINE = re.compile(
    r'(`+)(?!`)(.*?)(?<!`)(\1)(?!`)|\\.|\]\((?:[^()\n]|\([^()\n]*\))*\)'
    r'|\]\[[^\]\n]*\]|<[^\n>]+>|[\[\]*_`|]|!(?=\[)|[ \t]+#+(?=[ \t]*(?:\n|$))')
STRUCTURE_PREFIX = re.compile(r'^( {0,3}(?:(?:>[ \t]?|[-+*][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+)\s*)+)')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
STRUCTURE_LINE = re.compile(r'^ {0,3}(?:([-*_])(?:[ \t]*\1){2,}[ \t]*|[=-]+[ \t]*|\[[^\]]+\]:.*|\|?[ :|-]+\|?[ \t]*)$')


def validate_hotword(term: str, aliases: str = '', note: str = '') -> None:
    if (not term.strip()
            or any(len(value) > limit for value, limit in [(term, 500), (aliases, 1000), (note, 1000)])
            or any(ord(char) < 32 or ord(char) == 127 for value in (term, aliases, note) for char in value)):
        raise ValueError('Hotwords must have a standard term and bounded single-line hints')


def validate_decision(row, text, term):
    start, end, span = row['start_offset'], row['end_offset'], row['span']
    if (row['field'] not in ('summary', 'transcript') or type(start) is not int or type(end) is not int
            or not isinstance(span, str) or not 1 <= len(span) <= 20 or start < 0
            or end != start + len(span) or text[start:end] != span or row['replacement'] != term):
        raise ValueError('Decision must address an exact original occurrence')


def body_ranges(text):
    """Absolute ranges of editable Markdown body text; structure/code/URLs stay protected."""
    offset, fence_char, fence_width = 0, '', 0
    for line in text.splitlines(keepends=True):
        marker = FENCE.match(line)
        if fence_char:
            if (marker and marker[1][0] == fence_char and len(marker[1]) >= fence_width
                    and not line[marker.end():].strip()):
                fence_char = ''
        elif marker:
            fence_char, fence_width = marker[1][0], len(marker[1])
        elif not (line.startswith('    ') or line.startswith('\t') or STRUCTURE_LINE.match(line.rstrip('\r\n'))):
            prefix = STRUCTURE_PREFIX.match(line)
            head = prefix.end() if prefix else 0
            start = head
            for protected in PROTECTED_INLINE.finditer(line, head):
                if protected.start() > start:
                    yield offset + start, offset + protected.start()
                start = protected.end()
            if start < len(line):
                yield offset + start, offset + len(line)
        offset += len(line)


class HotwordApplier:
    def __init__(self, decisions):
        self.decisions = sorted(decisions, key=lambda d: (-len(d['span']), d['id']))
        self.hits = Counter()
        self.applied_ids = set()

    def apply_hotwords(self, text: str, *, markdown: bool = True) -> str:
        # Validate against original coordinates; select longest spans before touching text.
        # Replacements are never rescanned, and equal text elsewhere is left alone.
        ranges = list(body_ranges(text))
        selected = []
        for d in self.decisions:
            start, end = d['start_offset'], d['end_offset']
            if (text[start:end] != d['span'] or end - start != len(d['span'])
                    or not any(left <= start and end <= right for left, right in ranges)
                    or any(start < b and end > a for a, b, _ in selected)):
                continue
            target = MARKDOWN_SPECIAL.sub(r'\\\1', d['replacement']) if markdown else d['replacement']
            selected.append((start, end, target))
            self.applied_ids.add(d['id'])
            self.hits[d['hotword_id']] += 1
        for start, end, target in sorted(selected, reverse=True):
            text = text[:start] + target + text[end:]
        return text
