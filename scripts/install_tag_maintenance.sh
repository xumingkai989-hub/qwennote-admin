#!/usr/bin/env bash
# Compatibility entry point; never re-enable the retired daily timer.
set -euo pipefail
test "$(id -u)" = 0
release=$(readlink -e /opt/qwennote/current)
[[ "$release" =~ ^/opt/qwennote/releases/[0-9]{8}T[0-9]{6}Z$ ]]
exec bash "$release/scripts/install_scheduler.sh"
