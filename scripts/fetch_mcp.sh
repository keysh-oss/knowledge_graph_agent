#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/fetch_mcp.sh <git-repo-url> [target-dir]
# Example: ./scripts/fetch_mcp.sh https://github.com/yourorg/mcp-server.git vendor/mcp-server

REPO=${1:-}
TARGET=${2:-vendor/mcp-server}

if [ -z "$REPO" ]; then
  echo "Usage: $0 <git-repo-url> [target-dir]"
  exit 2
fi

mkdir -p "$(dirname "$TARGET")"

if [ -d "$TARGET/.git" ]; then
  echo "Target $TARGET already exists as a git repo — pulling latest"
  (cd "$TARGET" && git fetch --all && git pull)
  exit 0
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # If this repo is a git repo, add as a submodule if not present
  if git submodule status -- "$TARGET" >/dev/null 2>&1; then
    echo "Submodule already registered. Updating..."
    git submodule update --init --recursive "$TARGET"
    exit 0
  fi
  echo "Adding $REPO as a git submodule at $TARGET"
  git submodule add "$REPO" "$TARGET"
  git submodule update --init --recursive "$TARGET"
  echo "Submodule added. To commit the change: git add .gitmodules $TARGET && git commit -m 'Add MCP server submodule'"
else
  # Not a git repo (or submodule not desired) — clone directly
  echo "Cloning $REPO into $TARGET"
  git clone --depth 1 "$REPO" "$TARGET"
fi

echo "Fetched MCP server into $TARGET"
