import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import deploy


@pytest.mark.parametrize('script', ['deploy.py', 'install_release.sh'])
@pytest.mark.parametrize('domain', [None, '', '<domain>', 'notes.example.com/private-value'])
def test_deployment_rejects_missing_or_invalid_domain_before_side_effects(script, domain):
    env = {**os.environ, 'QWENNOTE_SWAS_INSTANCE_ID': 'test-instance'}
    env.pop('QWENNOTE_DOMAIN', None)
    if domain is not None:
        env['QWENNOTE_DOMAIN'] = domain
    interpreter = sys.executable if script.endswith('.py') else 'bash'
    result = subprocess.run([interpreter, str(deploy.ROOT / 'scripts' / script)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'QWENNOTE_DOMAIN' in result.stderr
    assert result.stdout == ''
    if domain:
        assert domain not in result.stderr


def test_app_only_deploy_uses_existing_release_flow_without_certificate_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, 'INSTANCE', 'test-instance')
    monkeypatch.setenv('QWENNOTE_DOMAIN', 'notes.example.com')
    monkeypatch.setenv('SWAS_INSTANCE_ID', 'previous-instance')
    commands_path = tmp_path / 'commands.jsonl'
    helper = tmp_path / 'swas.py'
    helper.write_text(
        'import json, re\n'
        'from pathlib import Path\n'
        'def run(command, **kwargs):\n'
        '    assert kwargs["instance"] == "test-instance"\n'
        f'    with Path({str(commands_path)!r}).open("a") as output:\n'
        '        output.write(json.dumps(command) + "\\n")\n'
        '    return re.findall(r"QWENNOTE_STEP_OK_[\\w-]+", command)[-1]\n'
    )

    def archive(args, **kwargs):
        assert args[:2] == ['tar', 'czf']
        assert '--exclude=.scratch' in args
        assert '--exclude=knowledge' in args
        Path(args[2]).write_bytes(b'test archive')

    monkeypatch.setattr(deploy.subprocess, 'run', archive)
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['deploy.py', '--app-only', '--helper', str(helper),
                                     '--log-dir', str(tmp_path / 'logs')])
    deploy.main()
    assert os.environ['SWAS_INSTANCE_ID'] == 'test-instance'
    commands = [json.loads(line) for line in commands_path.read_text().splitlines()]
    phases = [command for command in commands if 'scripts/install_release.sh' in command]
    assert len(phases) == 2
    assert ' prepare ' in phases[0]
    assert ' activate ' in phases[1]
    assert all('QWENNOTE_APP_ONLY=1' in command for command in phases)
    assert all('QWENNOTE_DOMAIN=notes.example.com' in command for command in phases)
    assert not any(' certificate ' in command for command in commands)
    bootstrap = next(command for command in commands if 'KB_INITIAL_BUILD_STARTED' in command)
    assert commands.index(bootstrap) > commands.index(phases[1])
    assert any('sha256sum -c' in command for command in commands)
    assert 'systemctl is-active qwennote.service' in commands[-1]
    assert 'nginx -t' not in commands[-1]


@pytest.mark.parametrize('app_only', [True, False])
def test_installer_activation_limits_app_only_changes(tmp_path, app_only):
    root = tmp_path / 'qwennote'
    release = root / 'releases/20261030T000000Z'
    old = root / 'releases/20261029T000000Z'
    release.mkdir(parents=True)
    (release / 'deploy/nginx').mkdir(parents=True)
    for template in (Path(deploy.ROOT) / 'deploy/nginx').glob('qwennote*.conf'):
        (release / 'deploy/nginx' / template.name).write_text(template.read_text())
    (old / '.venv').mkdir(parents=True)
    certificate = tmp_path / 'certificate.pem'
    certificate.write_text('test certificate')
    commands = tmp_path / 'commands.log'
    rendered = tmp_path / 'nginx.conf'
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    stub = (
        '#!/bin/bash\n'
        'name=${0##*/}\n'
        f'printf "%s\\n" "$name $*" >> {shlex.quote(str(commands))}\n'
        'case "$name" in\n'
        'id) echo 0;;\n'
        f'readlink) echo {shlex.quote(str(old))};;\n'
        'curl) if [[ "$*" == *https://* ]]; then printf "302 0"; else printf 302; fi;;\n'
        f'install) if [[ "$*" == */etc/nginx/conf.d/qwennote.conf ]]; then cat "$3" > {shlex.quote(str(rendered))}; fi;;\n'
        'esac\n'
    )
    for name in ('id', 'flock', 'readlink', 'ln', 'mv', 'systemctl', 'curl', 'install',
                 'systemd-analyze', 'nginx'):
        path = stubs / name
        path.write_text(stub)
        path.chmod(0o755)
    source = (Path(deploy.__file__).parent / 'install_release.sh').read_text()
    source = source.replace('/opt/qwennote', str(root)).replace(
        '/run/lock/qwennote-deploy.lock', str(tmp_path / 'deploy.lock')).replace(
        '/etc/letsencrypt/live/${QWENNOTE_DOMAIN}/fullchain.pem', str(certificate))
    installer = tmp_path / 'installer.sh'
    installer.write_text(source)
    subprocess.run(['bash', str(installer), 'activate', str(release)], check=True,
                   env={**os.environ, 'PATH': str(stubs) + os.pathsep + os.environ['PATH'],
                        'QWENNOTE_DOMAIN': 'notes.example.com',
                        'QWENNOTE_APP_ONLY': '1' if app_only else '0'}, capture_output=True)
    calls = commands.read_text().splitlines()
    assert f'ln -s {release} {root}/.current-deploy-' in '\n'.join(calls)
    assert 'systemctl restart qwennote.service' in calls
    infrastructure = [line for line in calls if line.startswith(
        ('install ', 'nginx ', 'systemd-analyze ', 'systemctl enable ',
         'systemctl daemon-reload', 'systemctl reload '))]
    if app_only:
        assert infrastructure == []
        assert not rendered.exists()
    else:
        assert any('/etc/nginx/conf.d/qwennote.conf' in line for line in infrastructure)
        assert 'systemctl reload nginx' in infrastructure
        config = rendered.read_text()
        assert '<domain>' not in config
        assert 'server_name notes.example.com;' in config
        assert '/etc/letsencrypt/live/notes.example.com/fullchain.pem' in config
        assert '$request_uri' in config and '$proxy_add_x_forwarded_for' in config


@pytest.mark.parametrize('state', ['missing_directory', 'missing_index', 'existing_index'])
@pytest.mark.parametrize('build_exit', [0, 2])
def test_initial_knowledge_build_only_when_index_missing(tmp_path, state, build_exit):
    root = tmp_path / 'knowledge'
    if state != 'missing_directory':
        root.mkdir()
    if state == 'existing_index':
        (root / 'index.json').write_text('{}')
    calls = tmp_path / 'build-call'
    script = tmp_path / 'kb.sh'
    script.write_text(f'printf "%s\\n" "$*" > {shlex.quote(str(calls))}\nexit {build_exit}\n')

    def run(label, command, timeout):
        assert label == 'knowledge' and timeout == 900
        command = command.replace('/opt/qwennote/data/knowledge', str(root)).replace(
            '/opt/qwennote/current/scripts/kb.sh', str(script))
        result = subprocess.run(['bash', '-eu', '-c', command], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        if state == 'existing_index':
            assert 'initial build skipped' in result.stdout
        elif build_exit:
            assert 'KB_INITIAL_BUILD_FAILED' in result.stderr
        else:
            assert 'KB_INITIAL_BUILD_OK' in result.stdout

    remote = Mock(side_effect=run)
    deploy.initialize_knowledge(remote)
    remote.assert_called_once()
    if state == 'existing_index':
        assert not calls.exists()
    else:
        assert calls.read_text() == 'build --full\n'


def test_initial_knowledge_remote_failure_does_not_block_deployment(capsys):
    deploy.initialize_knowledge(Mock(side_effect=RuntimeError('knowledge failed; inspect logs')))
    assert 'WARNING: knowledge initialization failed' in capsys.readouterr().out


@pytest.mark.parametrize('unit', ['qwennote', 'qwennote-tick', 'qwennote-sync', 'qwennote-tag-maintenance'])
def test_knowledge_units_use_persistent_root(unit):
    text = (deploy.ROOT / 'deploy/systemd' / (unit + '.service')).read_text()
    assert 'Environment=KB_ROOT=/opt/qwennote/data/knowledge' in text
    assert 'EnvironmentFile=/etc/qwennote/qwennote.env' in text
    assert 'ReadWritePaths=/opt/qwennote/data' in text
