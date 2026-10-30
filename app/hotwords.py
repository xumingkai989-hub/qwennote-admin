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


def validate_hotword(source: str, target: str, note: str = '') -> None:
    if (not source.strip() or not target.strip()
            or any(len(value) > limit for value, limit in [(source, 200), (target, 500), (note, 1000)])
            or any(ord(char) < 32 or ord(char) == 127 for value in (source, target, note) for char in value)):
        raise ValueError('Hotwords must be nonempty, bounded single-line text')


class HotwordApplier:
    def __init__(self, rules):
        # Stable ID order breaks equal-length ties. Regex alternatives are tried
        # left to right at each source position, so no replacement is rescanned.
        self.rules = sorted(rules, key=lambda row: (-len(row.source), row.id))
        self.pattern = (re.compile('|'.join(f'(?P<r{row.id}>{re.escape(row.source)})'
                                           for row in self.rules), re.IGNORECASE)
                        if self.rules else None)
        self.by_id = {row.id: row for row in self.rules}
        self.hits = Counter()

    def _replace(self, text: str, markdown: bool) -> str:
        if self.pattern is None:
            return text
        def replacement(match):
            hotword_id = int(match.lastgroup[1:])
            self.hits[hotword_id] += 1
            target = self.by_id[hotword_id].target
            return MARKDOWN_SPECIAL.sub(r'\\\1', target) if markdown else target
        return self.pattern.sub(replacement, text)

    def apply_hotwords(self, text: str, *, markdown: bool = True) -> str:
        output = []
        fence_char = ''
        fence_width = 0
        for line in text.splitlines(keepends=True):
            marker = FENCE.match(line)
            if fence_char:
                output.append(line)
                if (marker and marker[1][0] == fence_char and len(marker[1]) >= fence_width
                        and not line[marker.end():].strip()):
                    fence_char = ''
                continue
            if marker:
                fence_char, fence_width = marker[1][0], len(marker[1])
                output.append(line)
                continue
            if line.startswith('    ') or line.startswith('\t') or STRUCTURE_LINE.match(line.rstrip('\r\n')):
                output.append(line)
                continue
            prefix = STRUCTURE_PREFIX.match(line)
            head = line[:prefix.end()] if prefix else ''
            body = line[len(head):]
            parts = []
            start = 0
            for protected in PROTECTED_INLINE.finditer(body):
                parts.append(self._replace(body[start:protected.start()], markdown))
                parts.append(protected.group())
                start = protected.end()
            parts.append(self._replace(body[start:], markdown))
            output.append(head + ''.join(parts))
        return ''.join(output)
