#!/usr/bin/env bash
set -euo pipefail
cd /opt/qwennote/current
set -a; . /etc/qwennote/qwennote.env; set +a
export LD_LIBRARY_PATH=/opt/qwennote/current/.sqlite/lib
exec sudo -u qwennote env \
  LD_LIBRARY_PATH="$LD_LIBRARY_PATH" DB_PATH=/opt/qwennote/data/qwennote.db \
  QWENNOTE_MCP_URL="$QWENNOTE_MCP_URL" QWENNOTE_TOKEN_FILE=/opt/qwennote/data/mcp_token.json \
  .venv/bin/python -m app.kb "$@"
