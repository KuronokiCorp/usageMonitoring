#!/usr/bin/env bash
# Launch the iTerm2 web admin with MiniMax credentials loaded from .env.
# Usage: ./start.sh [--port 8765] [--open]
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a           # export everything sourced
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec python3 iterm_web.py "$@"
