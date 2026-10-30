#!/usr/bin/env bash
# Install the tick and retire only this application's two superseded timers.
set -euo pipefail
test "$(id -u)" = 0
release=$(readlink -e /opt/qwennote/current)
[[ "$release" =~ ^/opt/qwennote/releases/[0-9]{8}T[0-9]{6}Z$ ]]
exec 9>/run/lock/qwennote-scheduler-install.lock
flock -n 9
for kind in service timer; do
    install -m 644 "$release/deploy/systemd/qwennote-tick.$kind" \
        "/etc/systemd/system/qwennote-tick.$kind"
done
systemd-analyze verify /etc/systemd/system/qwennote-tick.service \
    /etc/systemd/system/qwennote-tick.timer
systemctl daemon-reload
systemctl disable --now qwennote-sync.timer qwennote-tag-maintenance.timer
# Drain any old work already running before the unified timer can start.
# Do not interrupt an in-flight sync or vocabulary transaction.
for unit in qwennote-sync.service qwennote-tag-maintenance.service; do
    for attempt in {1..180}; do
        state=$(systemctl show "$unit" -p ActiveState --value)
        [[ "$state" != active && "$state" != activating ]] && break
        sleep 1
    done
    state=$(systemctl show "$unit" -p ActiveState --value)
    [[ "$state" != active && "$state" != activating ]]
done
systemctl enable --now qwennote-tick.timer
