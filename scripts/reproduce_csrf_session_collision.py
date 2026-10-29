"""Offline, deterministic reproduction: python -m scripts.reproduce_csrf_session_collision."""
from unittest.mock import patch

from itsdangerous import TimestampSigner

from app.session import SESSION_MAX_AGE, SignedCookie


def main() -> None:
    cookie = SignedCookie('offline-reproduction-secret-' * 2,
                          salt='web-session', max_age=SESSION_MAX_AGE)
    issued_at = 1_800_000_000
    now = issued_at
    with patch.object(TimestampSigner, 'get_timestamp', lambda self: now):
        old = cookie.encode('admin')
        original = TimestampSigner.get_timestamp
        for elapsed in (0, 1, 2):
            now = issued_at + elapsed
            # This is the original test's time simulation.
            with patch.object(TimestampSigner, 'get_timestamp', lambda self: original(self) - 1):
                new = cookie.encode('admin')
            same = new == old
            valid = cookie.decode(new) == 'admin'
            print(f'elapsed={elapsed}s same_session={same} session_valid={valid}')
            assert same == (elapsed == 1)
            assert valid


if __name__ == '__main__':
    main()
