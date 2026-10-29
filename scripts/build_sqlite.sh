#!/usr/bin/env bash
# Isolated SQLite for FTS5 trigram; never replaces a system library or interpreter.
set -euo pipefail
release=${1:?absolute release directory}
prefix="$release/.sqlite"
command -v gcc >/dev/null
work=$(mktemp -d /tmp/qwennote-sqlite.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
downloaded=false
for host in www.sqlite.org www2.sqlite.org www3.sqlite.org; do
    if curl --fail --silent --show-error --connect-timeout 8 --max-time 60 \
        "https://$host/2026/sqlite-amalgamation-3540100.zip" -o "$work/source.zip"; then
        downloaded=true
        break
    fi
done
test "$downloaded" = true
/usr/bin/python3.11 - "$work" <<'PY'
import hashlib
from pathlib import Path
import sys
import zipfile
work = Path(sys.argv[1])
archive = work / 'source.zip'
expected = '92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30'
if hashlib.sha3_256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit('Official SQLite source SHA3-256 mismatch; not extracting')
with zipfile.ZipFile(archive) as source:
    # Extract only the three pinned source files, without trusting archive paths.
    for name in ('sqlite3.c', 'sqlite3.h', 'shell.c'):
        (work / name).write_bytes(source.read('sqlite-amalgamation-3540100/' + name))
print('SQLITE_SOURCE_SHA3_256=OK')
PY
install -d -m 755 "$prefix/lib" "$prefix/bin"
cd "$work"
gcc -O2 -fPIC -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_COLUMN_METADATA \
    -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_DBSTAT_VTAB -c sqlite3.c -o sqlite3.o
gcc -shared -Wl,-soname,libsqlite3.so.0 -o "$prefix/lib/libsqlite3.so.0" sqlite3.o -lpthread -ldl -lm
ln -sf libsqlite3.so.0 "$prefix/lib/libsqlite3.so"
gcc -O2 -I. shell.c -L"$prefix/lib" -Wl,-rpath,'$ORIGIN/../lib' \
    -lsqlite3 -lpthread -ldl -lm -o "$prefix/bin/sqlite3"
"$prefix/bin/sqlite3" :memory: "CREATE VIRTUAL TABLE verify USING fts5(content, tokenize='trigram'); SELECT sqlite_version();"
LD_LIBRARY_PATH="$prefix/lib" "$release/.venv/bin/python" - <<'PY'
import sqlite3
with sqlite3.connect(':memory:') as db:
    db.execute("CREATE VIRTUAL TABLE verify USING fts5(content, tokenize='trigram')")
print('PYTHON_SQLITE=' + sqlite3.sqlite_version)
PY
