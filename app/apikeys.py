"""Local API key administration. Plaintext is emitted only by create."""
import argparse
import hashlib
import os
import secrets
from pathlib import Path

from app.store import SQLiteStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--name', required=True)
    revoke = commands.add_parser('revoke')
    revoke.add_argument('id', type=int)
    commands.add_parser('list')
    args = parser.parse_args()
    db_path = os.environ.get('DB_PATH', '').strip()
    if not db_path:
        parser.error('DB_PATH is required')
    if args.command == 'create' and (not args.name.strip() or any(ord(c) < 32 for c in args.name)):
        parser.error('name must be nonempty and contain no control characters')
    store = SQLiteStore(Path(db_path).expanduser())
    store.migrate()
    if args.command == 'create':
        key = 'qn_' + secrets.token_hex(16)
        store.create_api_key(args.name.strip(), hashlib.sha256(key.encode()).hexdigest())
        print(key)
    elif args.command == 'revoke':
        if not store.revoke_api_key(args.id):
            parser.error('key id not found')
        print(f'revoked {args.id}')
    else:
        print('id\tname\tcreated_at\tlast_used_at\tstatus')
        for key in store.list_api_keys():
            print(f'{key.id}\t{key.name}\t{key.created_at}\t{key.last_used_at or "—"}\t'
                  f'{"revoked" if key.revoked_at else "active"}')


if __name__ == '__main__':
    main()
