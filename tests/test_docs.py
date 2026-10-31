"""文档契约检查；仅使用标准库，不读取运行配置或连接外部服务。"""
import ast
from collections import Counter
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / 'README.md'
ARCHITECTURE = ROOT / 'docs/ARCHITECTURE.md'
DOCUMENTS = [README, *sorted((ROOT / 'docs').rglob('*.md'))]


def section(text, heading):
    """取一个二级章节，避免其他代码块/说明中的名称掩盖表格遗漏。"""
    match = re.search(r'^## ' + re.escape(heading) + r'\n(.*?)(?=^## |\Z)',
                      text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f'缺少章节：{heading}')
    return match[1]


def first_column_names(text, pattern):
    return re.findall(r'^\| `(' + pattern + r')` \|', text, re.MULTILINE)


class DocumentationTests(unittest.TestCase):
    def assert_repository_excludes(self, forbidden):
        paths = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
            cwd=ROOT).split(b'\0')
        for name in filter(None, paths):
            path = ROOT / name.decode('utf-8')
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn(forbidden, path.read_bytes(),
                                 '仓库含生产环境信息，须改用环境变量或占位符')

    def test_repository_does_not_contain_production_domain(self):
        # 拆分构造，检测器本身也必须通过全仓库扫描。
        self.assert_repository_excludes(b'mingkai' + b'.xin')

    def test_repository_does_not_contain_unrelated_service(self):
        self.assert_repository_excludes(b'dashboard' + b'-shell')

    def test_repository_has_mit_license(self):
        license_path = ROOT / 'LICENSE'
        self.assertTrue(license_path.is_file())
        self.assertIn('MIT License', license_path.read_text())

    def test_repository_does_not_contain_fixed_instance_id(self):
        # 拆分构造，避免检测器自身保留完整实例标识。
        instance_id = ''.join(('74ee', '3f09', '792c', '4864',
                               '9cf8', 'aad3', '1870', '2dd5')).encode('ascii')
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
        for name in filter(None, paths):
            path = ROOT / name.decode('utf-8')
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertFalse(instance_id in path.read_bytes(),
                                 '仓库含固定实例标识，须改用环境变量或占位符')

    def test_readme_environment_table_matches_example(self):
        expected = set(re.findall(r'^([A-Z][A-Z0-9_]*)=',
                                  (ROOT / '.env.example').read_text(), re.MULTILINE))
        names = first_column_names(section(README.read_text(), '配置'), r'[A-Z][A-Z0-9_]*')
        self.assertEqual(set(names), expected)
        self.assertEqual(len(names), len(set(names)), '环境变量表存在重复行')

    def test_architecture_module_table_matches_app(self):
        expected = {path.name for path in (ROOT / 'app').glob('*.py')}
        names = first_column_names(section(ARCHITECTURE.read_text(), '1. 模块职责'),
                                   r'[a-z_]+\.py')
        self.assertEqual(set(names), expected)
        self.assertEqual(len(names), len(set(names)), '模块表存在重复行')

    def test_documents_do_not_contain_real_ipv4(self):
        pattern = re.compile(r'\d+\.\d+\.\d+\.\d+')
        for path in DOCUMENTS:
            with self.subTest(document=str(path.relative_to(ROOT))):
                matches = set(pattern.findall(path.read_text()))
                self.assertFalse(matches - {'127.0.0.1'}, '文档含非回环 IPv4，须改用占位符')

    def test_documents_do_not_contain_credentials(self):
        patterns = (
            r'\bsk-[A-Za-z0-9_-]+',
            r'\bqn_[A-Za-z0-9]{32}\b',
            r'\bgh[pousr]_[A-Za-z0-9]{20,}',
            r'\bgithub_pat_[A-Za-z0-9_]{20,}',
            r'\bAKIA[A-Z0-9]{16}\b',
            r'\bLTAI[A-Za-z0-9]{12,}',
            r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----',
            r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}',
            r'''(?im)\b(?:SESSION_SECRET|FEISHU_APP_SECRET|ANTHROPIC_AUTH_TOKEN|QWENNOTE_MCP_TOKEN|access_token|refresh_token)\s*[=:]\s*["']?[A-Za-z0-9_+/=-]{20,}''',
        )
        for path in DOCUMENTS:
            content = path.read_text()
            with self.subTest(document=str(path.relative_to(ROOT))):
                # 不把匹配到的值放入失败消息，避免检测器二次泄露。
                self.assertFalse(any(re.search(pattern, content) for pattern in patterns),
                                 '发现疑似密钥，须脱敏后提交')

    def test_readme_length_and_section_order(self):
        text = README.read_text()
        # 21–30 的开发记录与同步策略说明增加后，仍保持入口文档简短。
        self.assertTrue(150 <= len(text.splitlines()) <= 220)
        self.assertEqual(re.findall(r'^## (.+)$', text, re.MULTILINE), [
            '它解决什么问题', '功能', '技术栈与取舍', '快速开始', '配置',
            '部署', '测试', '目录结构', '开发记录', '许可证',
        ])
        self.assertIn('把千问笔记里的会议录音同步到自建服务，可搜索、可编辑、可导出，并按热词自动校正。',
                      text.split('## ', 1)[0])

    def test_readme_development_records_cover_existing_notes(self):
        new_guides = {'PRD.md', 'ARCHITECTURE.md', 'OPERATIONS.md'}
        expected = {p.name for p in (ROOT / 'docs').glob('*.md') if p.name not in new_guides}
        records = section(README.read_text(), '开发记录')
        listed = re.findall(r'\]\(docs/([^/)]+\.md)\)', records)
        self.assertEqual(set(listed), expected)
        self.assertTrue(all(count == 1 for count in Counter(listed).values()))
        self.assertIn('过程记录，非面向使用者的说明', records)

    def test_prd_has_sixteen_unique_requirement_rows(self):
        requirements = section((ROOT / 'docs/PRD.md').read_text(), '4. 功能需求')
        rows = re.findall(r'^\| (F\d+) \| (.+) \| (.+) \| (P[012]) \|$',
                          requirements, re.MULTILINE)
        self.assertEqual([row[0] for row in rows], [f'F{i}' for i in range(1, 17)])

    def test_readme_documents_sync_modes_and_watermark(self):
        text = README.read_text()
        for term in ('快速同步', '全盘同步', '水位'):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_prd_documents_sync_modes_and_watermark(self):
        text = (ROOT / 'docs/PRD.md').read_text()
        for term in ('快速同步', '全盘同步', '水位'):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_sync_mode_configuration_is_documented(self):
        # 只解析源码，不导入应用或读取运行环境。
        tree = ast.parse((ROOT / 'app/automation.py').read_text())
        settings = next(node for node in tree.body
                        if isinstance(node, ast.ClassDef) and node.name == 'AutomationSettings')
        names = {node.target.id for node in settings.body
                 if isinstance(node, ast.AnnAssign)
                 and (node.target.id.startswith('full_sync_')
                      or node.target.id == 'sync_mode_default')}
        self.assertTrue(names, '未找到同步模式配置项')
        for path in (README, ROOT / 'docs/PRD.md'):
            text = path.read_text()
            for name in sorted(names):
                with self.subTest(document=path.name, setting=name):
                    self.assertIn(name, text)

    def test_new_guides_have_no_broken_local_markdown_links(self):
        paths = [README, *(ROOT / 'docs' / name for name in
                           ('PRD.md', 'ARCHITECTURE.md', 'OPERATIONS.md'))]
        for path in paths:
            for target in re.findall(r'\]\(([^\s)]+)\)', path.read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                with self.subTest(document=path.name, link=target):
                    self.assertTrue((path.parent / target.split('#', 1)[0]).is_file())


if __name__ == '__main__':
    unittest.main()
