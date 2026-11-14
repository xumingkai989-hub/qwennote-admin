#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_path="$root/scripts/qwennote-kb"
target=/usr/local/bin/qwennote-kb
# Preserve a stable current symlink on release-based installations.
if [[ "$root" = /opt/qwennote/releases/* && -d /opt/qwennote/current ]]; then
    source_path=/opt/qwennote/current/scripts/qwennote-kb
fi
if [[ -e "$target" && ! -L "$target" ]]; then
    echo "拒绝覆盖已有普通文件：$target" >&2
    exit 2
fi
if [[ ! -d /usr/local/bin || ! -w /usr/local/bin ]]; then
    echo '安装到 /usr/local/bin 需要 sudo 权限。' >&2
    sudo mkdir -p /usr/local/bin
    sudo ln -sfn "$source_path" "$target"
else
    ln -sfn "$source_path" "$target"
fi
printf '已安装：%s -> %s\n' "$target" "$source_path"
