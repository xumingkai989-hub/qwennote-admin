#!/usr/bin/env bash
set -euo pipefail
root=${QWENNOTE_ROOT:-/opt/qwennote}
backup="$root/backups/qwennote-$(TZ=Asia/Shanghai date +%Y%m%d).db"
test -f "$root/data/qwennote.db"
umask 077
exec 9>"$root/backups/.backup.lock"
flock -n 9
trap 'rm -f -- "$backup.tmp"' EXIT
sqlite3 "$root/data/qwennote.db" ".timeout 10000" ".backup '$backup.tmp'"
test "$(sqlite3 "$backup.tmp" 'PRAGMA quick_check;')" = ok
mv -f -- "$backup.tmp" "$backup"
mapfile -t backups < <(find "$root/backups" -maxdepth 1 -type f -regextype posix-extended \
    -regex '.*/qwennote-[0-9]{8}\.db' -printf '%f\n' | sort -r)
for ((index=7; index<${#backups[@]}; index++)); do
    rm -- "$root/backups/${backups[index]}"
done
printf 'BACKUP=%s\n' "$backup"
