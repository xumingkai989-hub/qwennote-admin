"""Plain-text snippets; all content is escaped by Jinja before adding mark tags."""
import re
from dataclasses import dataclass

from app.store import StoredRecording


@dataclass(frozen=True)
class SnippetPart:
    text: str
    highlighted: bool = False


@dataclass(frozen=True)
class SearchSnippet:
    label: str
    parts: list[SnippetPart]


def search_snippets(recording: StoredRecording, query: str) -> list[SearchSnippet]:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    snippets = []
    for label, text in [('标题命中', recording.title), ('总结命中', recording.summary_md),
                        ('转写命中', recording.transcript_md)]:
        first = pattern.search(text)
        if first is None:
            continue
        start, end = max(0, first.start() - 20), min(len(text), first.end() + 20)
        context = text[start:end]
        parts = [SnippetPart('…')] if start else []
        offset = 0
        for match in pattern.finditer(context):
            parts.append(SnippetPart(context[offset:match.start()]))
            parts.append(SnippetPart(match.group(), True))
            offset = match.end()
        parts.append(SnippetPart(context[offset:]))
        if end < len(text):
            parts.append(SnippetPart('…'))
        snippets.append(SearchSnippet(label, parts))
    return snippets
