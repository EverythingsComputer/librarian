#!/usr/bin/env bash
# Librarian installer — Linux (tested on Ubuntu 24.04/26.04, x86_64).
# Usage:  ./install.sh [install-dir]     (default: ~/librarian-app)
set -euo pipefail

DEST="${1:-$HOME/librarian-app}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== librarian install -> $DEST"

# 1. system packages: poppler (PDF text extraction) + python3-venv
if command -v apt-get >/dev/null; then
  echo "-- installing poppler-utils (needs sudo)"
  sudo apt-get update -qq
  sudo apt-get install -y poppler-utils python3-venv
fi
command -v pdftotext >/dev/null || { echo "ERROR: pdftotext missing. Install poppler-utils."; exit 1; }

# 2. python: prefer uv (fast, can fetch its own python), fall back to system python
if command -v uv >/dev/null 2>&1; then
  echo "-- using uv"
  uv venv "$DEST/.venv" --python 3.12
  uv pip install --python "$DEST/.venv/bin/python" -e "$DEST"
else
  if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "-- using system python3: $(python3 --version)"
    python3 -m venv "$DEST/.venv"
    "$DEST/.venv/bin/pip" install --upgrade pip
    "$DEST/.venv/bin/pip" install -e "$DEST"
  else
    echo "Need python3 >= 3.10 (or install uv: curl -LsSf https://astral.sh/uv/install.sh | sh)"
    exit 1
  fi
fi

# 3. copy sources if we were run from a different directory
if [ "$HERE" != "$DEST" ]; then
  mkdir -p "$DEST"
  cp -a "$HERE"/librarian "$HERE"/pyproject.toml "$DEST"/
fi

# 4. first run: create config (data dir ~/librarian)
BIN="$DEST/.venv/bin/librarian"
if [ ! -f "$HOME/librarian/config.toml" ]; then
  "$BIN" init
  echo
  echo ">>> EDIT ~/librarian/config.toml: set your library folder path(s),"
  echo ">>> then run:  $BIN build"
fi

ln -sf "$BIN" "$HOME/.local/bin/librarian" 2>/dev/null || true
echo
echo "== installed. Try:"
echo "   librarian status"
echo "   librarian search 'your question'"
echo
echo "MCP (for Hermes/LMStudio): command = $DEST/.venv/bin/python"
echo "                            args    = [-m, librarian.mcp_server]"
