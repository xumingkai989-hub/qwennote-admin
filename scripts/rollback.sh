#!/usr/bin/env bash
set -euo pipefail
root=${QWENNOTE_ROOT:-/opt/qwennote}
exec 9>/run/lock/qwennote-deploy.lock
flock -n 9
current=$(readlink -f "$root/current")
previous=$(readlink -f "$root/previous" || true)
if [[ ! -d "$previous/.venv" || "$previous" != "$root/releases/"* || "$current" == "$previous" ]]; then
    echo 'No previous valid release; current unchanged.' >&2
    exit 1
fi
ln -s "$previous" "$root/.current-rollback-$$"
mv -Tf "$root/.current-rollback-$$" "$root/current"
healthy() {
    for attempt in {1..20}; do
        if systemctl is-active --quiet qwennote && \
            [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/ || true)" = 302 ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}
if ! systemctl restart qwennote || ! healthy; then
    ln -s "$current" "$root/.current-rollback-$$"
    mv -Tf "$root/.current-rollback-$$" "$root/current"
    systemctl restart qwennote
    echo 'Rollback failed; restored original release.' >&2
    exit 1
fi
ln -s "$current" "$root/.previous-rollback-$$"
mv -Tf "$root/.previous-rollback-$$" "$root/previous"
printf 'ROLLED_BACK_TO=%s\n' "$(readlink -f "$root/current")"
