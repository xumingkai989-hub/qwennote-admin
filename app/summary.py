"""Document presentation of source Markdown; no source or export rewriting."""
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup


MARKDOWN = MarkdownIt('commonmark', {'html': False}).enable('table')
CHAPTER = re.compile(r'^(\d{2,}:[0-5]\d(?::[0-5]\d)?)(?=\s|$)')
TASK = re.compile(r'^\[([ xX])\](?:[ \t]+|$)')


def fixed_html(content: str) -> Token:
    token = Token('html_inline', '', 0)
    token.content = content
    return token


def decorate_chapters(children: list[Token]) -> list[Token]:
    result = []
    line_start = True
    chapter = False
    for token in children:
        if token.type in ('softbreak', 'hardbreak'):
            if chapter:
                result.append(fixed_html('</span>'))
            result.append(token)
            line_start, chapter = True, False
            continue
        match = CHAPTER.match(token.content) if line_start and token.type == 'text' else None
        if match:
            chapter = True
            result.append(fixed_html('<span class="summary-chapter"><span class="chapter-time">'))
            clock = Token('text', '', 0)
            clock.content = match[1]
            result.extend([clock, fixed_html('</span>')])
            token.content = token.content[match.end():]
        result.append(token)
        line_start = False
    if chapter:
        result.append(fixed_html('</span>'))
    return result


def render_summary(value: str) -> Markup:
    tokens = MARKDOWN.parse(value)
    first_h1 = True
    todo_level = None
    items = []
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            level = int(token.tag[1:])
            token.attrSet('class', 'summary-' + token.tag)
            if token.tag == 'h1':
                if not first_h1:
                    token.attrJoin('class', 'summary-h1-separated')
                first_h1 = False
            if todo_level is not None and level <= todo_level:
                todo_level = None
            if token.tag == 'h2' and tokens[index + 1].content.strip() == '待办':
                todo_level = level
        elif token.type == 'list_item_open':
            items.append({'token': token, 'first': True})
        elif token.type == 'list_item_close':
            items.pop()
        elif token.type == 'inline' and tokens[index - 1].type == 'paragraph_open':
            children = token.children or []
            if items and items[-1]['first']:
                items[-1]['first'] = False
                match = TASK.match(children[0].content) if children and children[0].type == 'text' else None
                if match or todo_level is not None:
                    checked = bool(match and match[1].lower() == 'x')
                    items[-1]['token'].attrSet('class', 'summary-task')
                    if match:
                        children[0].content = children[0].content[match.end():]
                    children.insert(0, fixed_html(
                        '<input class="summary-checkbox" type="checkbox" disabled'
                        + (' checked' if checked else '')
                        + ' aria-label="' + ('已完成' if checked else '未完成') + '">'))
            token.children = decorate_chapters(children)
        if token.children:
            for child in token.children:
                if child.type == 'link_open':
                    child.attrSet('rel', 'noopener noreferrer')

    # Only renderer output is trusted; injected HTML above is fixed markup.
    return Markup(MARKDOWN.renderer.render(tokens, MARKDOWN.options, {}))


def table_open(renderer, tokens, index, options, env):
    return '<div class="summary-table" role="region" aria-label="表格" tabindex="0">\n' + renderer.renderToken(tokens, index, options, env)


def table_close(renderer, tokens, index, options, env):
    return renderer.renderToken(tokens, index, options, env) + '</div>\n'


MARKDOWN.add_render_rule('table_open', table_open)
MARKDOWN.add_render_rule('table_close', table_close)
