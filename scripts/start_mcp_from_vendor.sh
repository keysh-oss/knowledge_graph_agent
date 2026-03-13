#!/usr/bin/env bash
set -euo pipefail

# Start an MCP server that was fetched into vendor/mcp-server
# Usage: ./scripts/start_mcp_from_vendor.sh [vendor-path]

VENDOR_PATH=${1:-vendor/mcp-server}

if [ ! -d "$VENDOR_PATH" ]; then
  echo "Vendor path $VENDOR_PATH does not exist. Run scripts/fetch_mcp.sh first." >&2
  exit 2
fi

# Activate venv if present
if [ -f .venv/bin/activate ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

echo "Starting MCP from $VENDOR_PATH"

cd "$VENDOR_PATH"

# If the vendor project has requirements.txt, install them into the venv
if [ -f requirements.txt ] && [ -n "${VIRTUAL_ENV:-}" ]; then
  echo "Installing vendor requirements into venv..."
  pip install -r requirements.txt
fi

# Start the MCP server using uvicorn. This assumes the vendor exposes src/mcp_server:app
echo "Launching uvicorn (works if the vendor has src/mcp_server.py exposing 'app')"
nohup .venv/bin/uvicorn src.mcp_server:app --host 127.0.0.1 --port 9000 > ../../mcp.log 2>&1 & echo $! > ../../mcp.pid
echo "MCP started, PID=$(cat ../../mcp.pid), logs: $(pwd)/../../mcp.log"
