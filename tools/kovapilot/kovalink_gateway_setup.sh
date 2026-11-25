#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ ! -f /AGNOS || ! -x /usr/local/venv/bin/python3 ]]; then
  echo "Run KovaLink setup on a comma with the AGNOS Python environment." >&2
  exit 1
fi

# Use the same Python environment as manager in an SSH shell too.
export PATH="/usr/local/venv/bin:$PATH"
export PYTHONPATH="$root"
exec /usr/local/venv/bin/python3 "$root/tools/kovapilot/kovalink_gateway_setup.py"
