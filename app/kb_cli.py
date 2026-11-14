"""Standalone, stdlib-only command adapter for the knowledge business functions."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from app import kb


class CLIError(Exception):
    def __init__(self, kind, message, code=2, **details):
        super().__init__(message)
        self.kind, self.code, self.details = kind, code, details


class Parser(argparse.ArgumentParser):
    def format_help(self):
        formatter = self._get_formatter()
        formatter.add_text(self.description)
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups, prefix='Usage:\n  ')
        order = {'Commands': 0, 'Arguments': 1, 'Flags': 2, 'Global flags': 3}
        for group in sorted(self._action_groups, key=lambda g: order.get(g.title, 2)):
            formatter.start_section(group.title)
            formatter.add_text(group.description)
            formatter.add_arguments(group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()

    def error(self, message):
        raise CLIError('invalid_argument', message)


# One parser definition drives both help and machine-readable schema.
DESCRIPTIONS = {
    'list': ('列出标签及其会议数、时长、补充记录数', 'read',
             '需要先知道有哪些标签、各自规模时\n  • 不确定标签准确名称，准备调用 show 之前',
             '全部标签\n      qwennote-kb list\n  • 按分类过滤\n      qwennote-kb list --category 产品'),
    'show': ('查看某个标签的完整内容（四段：当前状态/补充记录/会议/时间线）', 'read',
             '需要阅读某个主题的完整证据时', '读取原文\n      qwennote-kb show "<标签>"\n  • JSON 全文\n      qwennote-kb show "<标签>" --format json'),
    'search': ('跨标签检索：标签名、会议标题、补充记录、综述', 'read',
               '不知道相关内容属于哪个标签时', '检索关键词\n      qwennote-kb search 状态机 --limit 5'),
    'note': ('补充记录的增删查', 'write (add/remove); read (list)',
             '保存结论、读取或删除已有补充记录时', '先查看写入参数\n      qwennote-kb note add --help'),
    'note add': ('给标签追加一条补充记录，默认重建该标签', 'write',
                 '已有明确结论，需要保留给后续 agent 时', '预览写入\n      qwennote-kb note add "<标签>" --text "结论" --source codex --dry-run'),
    'note list': ('查看某个标签的补充记录', 'read',
                  '需要核验已写入记录或查找删除 ID 时', '最近记录\n      qwennote-kb note list "<标签>" --limit 20'),
    'note remove': ('按 ID 删除补充记录，默认重建所属标签', 'write',
                    '已确认某条补充记录应删除时', '预览删除\n      qwennote-kb note remove 123 --dry-run'),
    'build': ('重建知识库文件', 'write',
              '首次生成知识库，或需要更新派生文件时', '全量重建\n      qwennote-kb build --full\n  • 预览单标签重建\n      qwennote-kb build --tag "<标签>" --dry-run'),
    'prompt': ('输出可直接粘贴给 agent 的提示词', 'read',
               '准备把知识库交给 agent 使用时', '指定任务\n      qwennote-kb prompt --task "总结本周进展"'),
    'schema': ('查看某个命令的参数定义', 'read',
               'agent 需要机器可读的参数、默认值和风险定义时', '查看参数\n      qwennote-kb schema list\n  • 查看嵌套命令\n      qwennote-kb schema note add'),
}
QUICKSTART = '''qwennote-kb — QwenNote 知识库命令行工具。

AGENT QUICKSTART（让 agent 驱动时从这里开始）：
    先看全貌：  qwennote-kb list
    读一个主题：qwennote-kb show "<标签>"
    检索：      qwennote-kb search <关键词>
    写入：      qwennote-kb note add "<标签>" --text "…" --source codex
    接入说明：  qwennote-kb prompt

    所有命令支持 --format json|md|table，--jq 过滤 JSON 输出。
    写操作支持 --dry-run 预览，不落库不写文件。
    show / prompt 默认输出 Markdown；其余命令默认 JSON。
'''


def _globals(parser, *, preview=False):
    group = parser.add_argument_group('Global flags')
    # Suppression preserves flags supplied before a subcommand.
    group.add_argument('--format', choices=('json', 'md', 'table'), default=argparse.SUPPRESS,
                       metavar='string', help='输出格式：json | md | table（默认 json；show/prompt 默认 md）')
    group.add_argument('--jq', default=argparse.SUPPRESS, metavar='string',
                       help='过滤 JSON：.data[0].name、.data[].name、.data | length（字符串原样输出）')
    group.add_argument('--root', default=argparse.SUPPRESS, type=Path, metavar='string',
                       help='知识库根目录（KB_ROOT 或 <repo>/knowledge）')
    group.add_argument('--json', dest='format', action='store_const', const='json',
                       default=argparse.SUPPRESS, help='--format json 的兼容别名')
    if preview:
        group.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS,
                           help='写操作预览，不写库不写文件（仅 note add/remove、build）')
    group.add_argument('-h', '--help', action='help', help='查看帮助')


def make_parser():
    parser = Parser(prog='qwennote-kb', description=QUICKSTART, add_help=False,
                    usage='qwennote-kb <command> [flags]\n  qwennote-kb note <add|list|remove> [flags]',
                    formatter_class=argparse.RawTextHelpFormatter)
    _globals(parser, preview=True)
    commands = parser.add_subparsers(dest='command', required=True, title='Commands', metavar='')
    parsers = {}

    def command(parent, name, key, usage):
        desc, risk, when, examples = DESCRIPTIONS[key]
        sub = parent.add_parser(name, help=desc, add_help=False, usage=f'qwennote-kb {usage}',
                                description=f'{desc}\n\nRisk: {risk}\n\nWhen to use:\n  • {when}\n\nExamples:\n  • {examples}',
                                formatter_class=argparse.RawTextHelpFormatter)
        sub._positionals.title = 'Arguments'
        sub._optionals.title = 'Flags'
        _globals(sub, preview=key == 'note')
        parsers[key] = sub
        return sub

    listing = command(commands, 'list', 'list', 'list [flags]')
    listing.add_argument('--category', metavar='string', help='只显示该一级分类下的标签')
    listing.add_argument('--sort', choices=('sessions', 'updated'), default='sessions', metavar='string',
                         help='sessions | updated（默认 sessions，降序）')
    show = command(commands, 'show', 'show', 'show <标签> [flags]')
    show.add_argument('tag', help='准确标签名')
    search = command(commands, 'search', 'search', 'search <关键词> [flags]')
    search.add_argument('keyword', help='非空检索关键词')
    search.add_argument('--limit', type=kb._positive, default=5, metavar='N', help='最多返回的标签数（默认 5）')
    note = command(commands, 'note', 'note', 'note <add|list|remove> [flags]')
    notes = note.add_subparsers(dest='note_command', required=True, title='Commands', metavar='')
    add = command(notes, 'add', 'note add', 'note add <标签> --text "…" [flags]')
    add.add_argument('tag', help='标签名（最多 60 字）')
    add.add_argument('--text', required=True, help='补充记录正文（1–2000 字）')
    add.add_argument('--source', default='cli', choices=('codex', 'hermes', 'cli'), help='记录来源（默认 cli）')
    listing_notes = command(notes, 'list', 'note list', 'note list <标签> [flags]')
    listing_notes.add_argument('tag', help='准确标签名')
    listing_notes.add_argument('--limit', type=kb._positive, default=20, metavar='N', help='最多返回的记录数（默认 20）')
    remove = command(notes, 'remove', 'note remove', 'note remove <id> [flags]')
    remove.add_argument('id', type=kb._positive, help='记录 ID（正整数）')
    for sub in (add, remove):
        sub.add_argument('--no-build', action='store_true', help='只修改数据库，暂不重建文件')
        sub.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS, help='只预览动作，不写库不写文件')
    build = command(commands, 'build', 'build', 'build [flags]')
    mode = build.add_mutually_exclusive_group()
    mode.add_argument('--tag', action='append', help='只重建指定标签（可重复）')
    mode.add_argument('--full', action='store_true', help='全量重建（默认）')
    build.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS, help='只预览动作，不写库不写文件')
    prompt = command(commands, 'prompt', 'prompt', 'prompt [flags]')
    prompt.add_argument('--task', help='替换提示词末行的任务占位符')
    schema = command(commands, 'schema', 'schema', 'schema <command> [flags]')
    schema.add_argument('target', nargs='+', help='命令名，例如 list 或 note add')
    return parser, parsers


def command_schema(name, parsers):
    if name not in parsers:
        raise CLIError('invalid_argument', '未知命令：' + name)
    parameters = []
    for action in parsers[name]._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        kind = 'boolean' if isinstance(action, (argparse._StoreTrueAction, argparse._HelpAction)) else (
            'integer' if action.type is kb._positive else 'string')
        parameters.append(dict(name=action.dest, flags=action.option_strings, type=kind,
                               required=action.required, default=None if action.default == argparse.SUPPRESS else action.default,
                               choices=action.choices, multiple=action.nargs == '+' or isinstance(action, argparse._AppendAction),
                               description=action.help))
    return dict(command=name, description=DESCRIPTIONS[name][0], risk=DESCRIPTIONS[name][1],
                default_format='md' if name in ('show', 'prompt') else 'json', parameters=parameters)


def compile_filter(expression):
    """Parse a deliberately small jq subset; never eval or execute shell code."""
    stages = []
    for part in expression.split('|'):
        part = part.strip()
        if part in ('length', 'keys'):
            stages.append(part)
            continue
        if not part.startswith('.'):
            raise CLIError('invalid_argument', '--jq 支持路径、数组展开及管道 length / keys')
        tokens, pos = [], 1
        if pos < len(part) and part[pos] not in '.[':
            match = re.match(r'[\w-]+', part[pos:])
            if not match:
                raise CLIError('invalid_argument', '--jq 路径无效')
            tokens.append(match[0]); pos += len(match[0])
        while pos < len(part):
            match = re.match(r'\.([\w-]+)|\[(-?\d*)\]|\[("(?:[^"\\]|\\.)*")\]', part[pos:])
            if not match:
                raise CLIError('invalid_argument', '--jq 路径无效')
            key, index, quoted = match.groups()
            tokens.append(key if key is not None else json.loads(quoted) if quoted else
                          int(index) if index else None)
            pos += len(match[0])
        stages.append(tokens)
    return stages


def filter_values(value, stages):
    values = [value]
    try:
        for stage in stages:
            if stage == 'length':
                values = [len(v) if v is not None else 0 for v in values]
            elif stage == 'keys':
                values = [sorted(v) if isinstance(v, dict) else list(range(len(v))) for v in values]
            else:
                for key in stage:
                    found = []
                    for item in values:
                        if key is None:
                            found.extend(item.values() if isinstance(item, dict) else item)
                        elif item is None:
                            found.append(None)
                        elif isinstance(item, dict) and isinstance(key, str):
                            found.append(item.get(key))
                        elif isinstance(item, list) and isinstance(key, int):
                            found.append(item[key] if -len(item) <= key < len(item) else None)
                        else:
                            raise TypeError()
                    values = found
        return values
    except (TypeError, ValueError):
        raise CLIError('invalid_argument', '--jq 表达式与输出数据类型不匹配') from None


def _store():
    from app.store import SQLiteStore
    db = os.environ.get('DB_PATH', '').strip()
    if not db or not Path(db).expanduser().is_file():
        raise CLIError('io_error', 'DB_PATH 必须指向已有数据库')
    store = SQLiteStore(Path(db).expanduser())
    store.migrate()
    return store


def execute(args, root, parsers):
    if args.command == 'schema':
        return command_schema(' '.join(args.target), parsers), 0
    if args.command == 'prompt':
        from app.knowledge_page import build_agent_prompt
        prompt = build_agent_prompt('qwennote-kb')
        if args.task is not None:
            prompt = prompt.rsplit('\n', 1)[0] + '\n' + args.task
        return dict(content=prompt), 0
    if args.command == 'note':
        if args.note_command in ('add', 'list'):
            if not args.tag.strip() or len(args.tag.strip()) > 60:
                raise CLIError('invalid_argument', '标签名必须为 1–60 字')
        if args.note_command == 'add' and not 1 <= len(args.text.strip()) <= 2000:
            raise CLIError('invalid_argument', '补充记录必须为 1–2000 字')
    if args.command == 'search' and not args.keyword.strip():
        raise CLIError('invalid_argument', '检索关键词不能为空')
    if args.command == 'build' and args.tag and any(not tag.strip() for tag in args.tag):
        raise CLIError('invalid_argument', '标签名不能为空')
    # Preview is deliberately independent of DB initialization, migrations and model calls.
    if getattr(args, 'dry_run', False):
        action = dict(action=args.command, dry_run=True, root=str(root))
        if args.command == 'build':
            action.update(tags=args.tag, full=not bool(args.tag))
        else:
            action.update(action='note ' + args.note_command, rebuild=not args.no_build)
            if args.note_command == 'add':
                action.update(tag=args.tag.strip(), text=args.text.strip(), source=args.source)
            else:
                action.update(id=args.id)
        return action, 0
    if args.command == 'list':
        result = [tag for tag in kb._read_index(root)['tags']
                  if args.category is None or tag['category'] == args.category]
        result.sort(key=lambda tag: tag['sessions'] if args.sort == 'sessions' else tag.get('updated_at') or '', reverse=True)
    elif args.command == 'show':
        tag = next((t for t in kb._read_index(root)['tags'] if t['name'] == args.tag), None)
        if tag is None:
            raise CLIError('not_found', '标签不存在：' + args.tag, 1)
        return kb._show(root, tag), 0
    else:
        store = _store()
        if args.command == 'search':
            result = kb._search(store, root, args.keyword, args.limit)
        elif args.command == 'build':
            result = kb.build(store, root, tags=args.tag)
            if result['errors']:
                raise CLIError('io_error', '知识库部分文件重建失败', result=result)
            return result, 0 if result['rebuilt'] or result['skipped'] else 1
        elif args.note_command == 'list':
            result = kb.list_notes(store, args.tag, args.limit)
        else:
            if args.note_command == 'add':
                ident = kb.add_note(store, args.tag, args.text, args.source)
                name = args.tag.strip()
            else:
                with store._connect() as conn:
                    row = conn.execute('SELECT tag_name FROM knowledge_notes WHERE id=?', (args.id,)).fetchone()
                if row is None or not kb.remove_note(store, args.id):
                    raise CLIError('not_found', f'补充记录不存在：{args.id}', 1)
                ident, name = args.id, row['tag_name']
            result = dict(id=ident, tag=name, action=args.note_command)
            if not args.no_build:
                try:
                    result['build'] = kb.build(store, root, tags=[name])
                    if result['build']['errors']:
                        raise OSError()
                except Exception:
                    raise CLIError('io_error', '记录已提交，但文件重建失败；请按 ID 核验后重建，勿重复写入',
                                   committed=True, id=ident, tag=name) from None
            return result, 0
    return result, 0 if result else 1


def render(data, fmt):
    if fmt == 'md' and isinstance(data, dict) and 'content' in data:
        return data['content']
    rows = data if isinstance(data, list) else [data]
    if not rows:
        return ''
    if not all(isinstance(row, dict) for row in rows):
        return '\n'.join(str(row) for row in rows) + '\n'
    columns = list(dict.fromkeys(key for row in rows for key in row))
    def cell(value):
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value if value is not None else '')
        return text.replace('\n', '\\n').replace('\r', '\\r').replace('\t', ' ').replace('|', '\\|')
    matrix = [columns, *[[cell(row.get(key)) for key in columns] for row in rows]]
    if fmt == 'md':
        matrix.insert(1, ['---'] * len(columns))
        return '\n'.join('| ' + ' | '.join(row) + ' |' for row in matrix) + '\n'
    widths = [max(len(row[i]) for row in matrix) for i in range(len(columns))]
    return '\n'.join('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in matrix) + '\n'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    started = time.perf_counter()
    root = kb.configured_root().expanduser().resolve()
    args = None
    def meta(count=0):
        return dict(count=count, root=str(root), took_ms=round((time.perf_counter() - started) * 1000, 3))
    try:
        parser, parsers = make_parser()
        args = parser.parse_args(argv)
        root = getattr(args, 'root', root).expanduser().resolve()
        if getattr(args, 'dry_run', False) and not (args.command == 'build' or
                args.command == 'note' and args.note_command in ('add', 'remove')):
            raise CLIError('invalid_argument', '--dry-run 仅支持 note add/remove 和 build')
        expression = getattr(args, 'jq', None)
        stages = compile_filter(expression) if expression is not None else None
        fmt = getattr(args, 'format', 'md' if args.command in ('show', 'prompt') else 'json')
        if stages is not None and getattr(args, 'format', 'json') != 'json':
            raise CLIError('invalid_argument', '--jq 只能与 JSON 输出一起使用')
        data, code = execute(args, root, parsers)
        count = len(data) if isinstance(data, list) else (len(data['rebuilt']) + len(data['skipped'])
                                                       if args.command == 'build' and not getattr(args, 'dry_run', False) else 1)
        envelope = dict(ok=True, data=data, meta=meta(count))
        if stages is not None:
            values = filter_values(envelope, stages)
            for value in values:
                print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        elif fmt == 'json':
            print(json.dumps(envelope, ensure_ascii=False))
        else:
            output = render(data, fmt)
            print(output, end='')
        return code
    except SystemExit as exc:
        return exc.code
    except Exception as exc:
        if not isinstance(exc, CLIError):
            if isinstance(exc, (OSError, sqlite3.Error, ValueError, KeyError, TypeError)):
                exc = CLIError('io_error', '知识库操作失败：请检查数据库、知识库文件及目录权限')
            else:
                exc = CLIError('model_error', '知识库执行失败：请检查模型与运行配置')
        print(str(exc), file=sys.stderr)
        # Even a missing default-Markdown show emits a structured error for agents.
        error_format = getattr(args, 'format', None)
        if error_format is None:
            error_format = 'json'
            for i, arg in enumerate(argv):
                if arg == '--format' and i + 1 < len(argv):
                    error_format = argv[i + 1]
                elif arg.startswith('--format='):
                    error_format = arg.split('=', 1)[1]
                elif arg == '--json':
                    error_format = 'json'
        if error_format == 'json':
            print(json.dumps(dict(ok=False, error=dict(type=exc.kind, message=str(exc), **exc.details), meta=meta()), ensure_ascii=False))
        return exc.code


if __name__ == '__main__':
    raise SystemExit(main())
