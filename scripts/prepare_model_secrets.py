"""Provision the model encryption key once. Never print or replace key material."""
import os
from pathlib import Path
import pwd
import stat

from cryptography.fernet import Fernet


def provision(path, uid, gid):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('secret.key must be a regular file')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(path.stat().st_mode):
            raise RuntimeError('secret.key must be a regular file') from None
        # Fail closed on an invalid existing key; do not rotate it at deployment.
        try:
            Fernet(path.read_bytes().strip())
        except (OSError, ValueError):
            raise RuntimeError('Existing secret.key is invalid or unreadable') from None
    else:
        with os.fdopen(fd, 'wb') as output:
            output.write(Fernet.generate_key())
            output.flush()
            os.fsync(output.fileno())
    os.chown(path, uid, gid)
    os.chmod(path, 0o600)


if __name__ == '__main__':
    account = pwd.getpwnam('qwennote')
    provision('/etc/qwennote/secret.key', account.pw_uid, account.pw_gid)
    print('Model encryption key ready; mode 600; existing key preserved.')
