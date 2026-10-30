#!/usr/bin/env bash
# Install only this application's tag maintenance units after an app-only deploy.
set -euo pipefail
test "$(id -u)" = 0
release=$(readlink -e /opt/qwennote/current)
[[ "$release" =~ ^/opt/qwennote/releases/[0-9]{8}T[0-9]{6}Z$ ]]
for kind in service timer; do
    install -m 644 "$release/deploy/systemd/qwennote-tag-maintenance.$kind" \
        "/etc/systemd/system/qwennote-tag-maintenance.$kind"
done
systemd-analyze verify /etc/systemd/system/qwennote-tag-maintenance.service \
    /etc/systemd/system/qwennote-tag-maintenance.timer
systemctl daemon-reload
systemctl enable --now qwennote-tag-maintenance.timer
