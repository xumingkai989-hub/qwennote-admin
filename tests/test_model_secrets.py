"""Deployment key creation is idempotent and fails closed."""
import os
import stat

from cryptography.fernet import Fernet
import pytest

from scripts.prepare_model_secrets import provision


def test_generation_mode_and_deployment_preserves_key(tmp_path):
    path = tmp_path / 'secret.key'
    provision(path, os.getuid(), os.getgid())
    original = path.read_bytes()
    Fernet(original)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    provision(path, os.getuid(), os.getgid())
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_existing_key_never_overwritten(tmp_path):
    path = tmp_path / 'secret.key'
    path.write_bytes(b'invalid-existing-key')
    with pytest.raises(RuntimeError, match='invalid or unreadable'):
        provision(path, os.getuid(), os.getgid())
    assert path.read_bytes() == b'invalid-existing-key'


def test_symlink_never_follows_or_overwrites(tmp_path):
    target = tmp_path / 'other'
    original = Fernet.generate_key()
    target.write_bytes(original)
    path = tmp_path / 'secret.key'
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match='regular file'):
        provision(path, os.getuid(), os.getgid())
    assert target.read_bytes() == original
