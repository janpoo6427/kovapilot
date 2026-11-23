#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
CONNECTOR="${KOVA_COMMA_CONNECTOR:-$WORKSPACE_ROOT/comma-readonly.sh}"
SOURCE="$REPO_ROOT/tools/kovapilot/dm_shadow_source.py"
OBSERVER="$REPO_ROOT/tools/kovapilot/dm_shadow_observer.py"
DURATION=60
STOP_AFTER_TRIGGER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      [[ $# -ge 2 ]] || { echo "missing value for --duration" >&2; exit 2; }
      DURATION="$2"
      shift 2
      ;;
    --stop-after-trigger)
      STOP_AFTER_TRIGGER=1
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--duration SECONDS] [--stop-after-trigger]"
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
done

[[ "$DURATION" =~ ^[0-9]+$ ]] && (( DURATION >= 1 && DURATION <= 600 )) || {
  echo "--duration must be an integer from 1 to 600" >&2
  exit 2
}

[[ -x "$CONNECTOR" ]] || { echo "read-only comma connector is unavailable" >&2; exit 1; }
[[ -f "$SOURCE" && -f "$OBSERVER" ]] || { echo "shadow scripts are unavailable" >&2; exit 1; }

REMOTE_COMMAND="for d in /data/openpilot /data/openpilot_source; do if [ -d \"\$d/openpilot/cereal\" ]; then cd \"\$d\"; for p in \"\$d/.venv/bin/python\" \"\$d/.venv/bin/python3\" \"\$d/venv/bin/python\" \"\$d/venv/bin/python3\" /usr/local/venv/bin/python /usr/local/venv/bin/python3 /usr/local/pyenv/bin/python /usr/local/pyenv/bin/python3; do if [ -x \"\$p\" ]; then exec env PYTHONDONTWRITEBYTECODE=1 DM_SHADOW_DURATION=$DURATION \"\$p\" -; fi; done; if [ -f ./launch_env.sh ]; then . ./launch_env.sh >/dev/null 2>&1; fi; exec env PYTHONDONTWRITEBYTECODE=1 DM_SHADOW_DURATION=$DURATION python3 -; fi; done; echo shadow_source_environment_unavailable >&2; exit 1"

run_observer() {
  if (( STOP_AFTER_TRIGGER )); then
    PYTHONDONTWRITEBYTECODE=1 "$REPO_ROOT/.venv/bin/python" "$OBSERVER" --stop-after-trigger
  else
    PYTHONDONTWRITEBYTECODE=1 "$REPO_ROOT/.venv/bin/python" "$OBSERVER"
  fi
}

"$CONNECTOR" -- "$REMOTE_COMMAND" <"$SOURCE" | run_observer
