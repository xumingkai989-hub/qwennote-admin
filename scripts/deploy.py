#!/usr/bin/env python3
"""SWAS deployment: 48,000-character parts, SHA gate, isolated UTC releases."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import os
import re
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = os.environ.get('QWENNOTE_SWAS_INSTANCE_ID', '')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', type=Path, default=Path.home() / '.hermes/digital-agent/tools/swas_run.py')
    parser.add_argument('--log-dir', type=Path, default=Path('/tmp/qwennote-deploy-logs'))
    parser.add_argument('--app-only', action='store_true',
                        help='Update an existing application without changing nginx, certificates or units')
    args = parser.parse_args()
    if not INSTANCE:
        parser.exit(1, '缺少 QWENNOTE_SWAS_INSTANCE_ID；请设置目标实例 ID 后再执行部署。\n')
    domain = os.environ.get('QWENNOTE_DOMAIN', '')
    if not domain:
        parser.exit(1, '缺少 QWENNOTE_DOMAIN；请显式设置站点域名后再执行部署。\n')
    if len(domain) > 253 or not re.fullmatch(
            r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
            r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+', domain):
        parser.exit(1, 'QWENNOTE_DOMAIN 格式无效；请提供不含协议、端口或路径的域名。\n')
    os.environ['SWAS_INSTANCE_ID'] = INSTANCE
    spec = importlib.util.spec_from_file_location('swas_run', args.helper)
    swas = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(swas)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    release = '/opt/qwennote/releases/' + stamp
    logs = args.log_dir / stamp
    logs.mkdir(parents=True, mode=0o700)
    os.chmod(logs, 0o700)

    def remote(label, command, timeout=120):
        sentinel = 'QWENNOTE_STEP_OK_' + label
        output = swas.run('set -eu\n' + command + '\nprintf "\\n' + sentinel + '\\n"',
                          timeout=timeout, instance=INSTANCE)
        (logs / (label + '.log')).write_text(output)
        print(output, flush=True)
        if sentinel not in output.splitlines():
            raise RuntimeError(label + ' failed; inspect ' + str(logs))
        return output

    print('RELEASE=' + release, flush=True)
    nginx_filter = '' if args.app_only else ' ! -name qwennote.conf'
    remote('preflight', '''
test -x /usr/bin/python3.11
if ss -lntp | grep -E ':8090[[:space:]]'; then
    systemctl is-active --quiet qwennote.service
    test "$(ss -lntp '( sport = :8090 )' | tail -n +2 | wc -l)" -eq 1
    ss -lntp '( sport = :8090 )' | grep '127.0.0.1:8090'
else echo PORT_8090_FREE; fi
install -d -m 755 /opt/qwennote/releases
install -d -m 700 /opt/qwennote/deploy-audit
find /etc/nginx/conf.d -maxdepth 1 -type f -name '*.conf' ''' + nginx_filter + ''' -exec sha256sum {} + > /opt/qwennote/deploy-audit/''' + stamp + '''.nginx-before
cat /opt/qwennote/deploy-audit/''' + stamp + '''.nginx-before
''')
    archive = logs / 'qwennote.tar.gz'
    # Exclude credentials, local data, caches, scratch and macOS metadata as well.
    subprocess.run(['tar', 'czf', str(archive), '--exclude=.git', '--exclude=.venv',
                    '--exclude=data', '--exclude=.scratch', '--exclude=.env', '--exclude=.env.*',
                    '--no-xattrs', '--no-acls', '--no-fflags',
                    '--exclude=__pycache__', '--exclude=.pytest_cache', '--exclude=.DS_Store',
                    '-C', str(ROOT), '.'], check=True,
                   env={**os.environ, 'COPYFILE_DISABLE': '1'})
    raw = archive.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode('ascii')
    chunks = [encoded[index:index + 48000] for index in range(0, len(encoded), 48000)]
    remote('parts-init', 'rm -f /tmp/qwennote.b64.part*')
    for index, chunk in enumerate(chunks):
        # This instance rejected a full 48k command (CmdContent.ExceedLimit).
        # Keep 48k logical parts, append each using commands comfortably below 16k.
        for offset in range(0, len(chunk), 12000):
            remote(f'part-{index:06d}-{offset // 12000}',
                   "printf '%s' " + shlex.quote(chunk[offset:offset + 12000]) +
                   f' >> /tmp/qwennote.b64.part{index:06d}')
    remote('sha-and-extract',
           'cat /tmp/qwennote.b64.part* | base64 -d > /tmp/qwennote.tar.gz\n' +
           "printf '%s\\n' " + shlex.quote(digest + '  /tmp/qwennote.tar.gz') + ' | sha256sum -c -\n' +
           'sha256sum /tmp/qwennote.tar.gz\nmkdir ' + release + '\n' +
           'tar xzf /tmp/qwennote.tar.gz -C ' + release)
    (logs / 'archive.sha256').write_text(digest + '\n')
    phases = ('prepare', 'activate') if args.app_only else ('prepare', 'certificate', 'activate')
    mode = 'QWENNOTE_DOMAIN=' + shlex.quote(domain) + ' '
    if args.app_only:
        mode += 'QWENNOTE_APP_ONLY=1 '
    for phase in phases:
        print('PHASE=' + phase, flush=True)
        remote(phase, f'{mode}bash {release}/scripts/install_release.sh {phase} {release}', timeout=900)
    remote('regression', 'sha256sum -c /opt/qwennote/deploy-audit/' + stamp + '.nginx-before\n' +
           'systemctl is-active qwennote.service')
    print('DEPLOYMENT_LOGS=' + str(logs), flush=True)


if __name__ == '__main__':
    main()
