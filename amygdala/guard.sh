#!/usr/bin/env bash
# SysEdge Amygdala Guard — hook entry point.
# Spawns guard.py directly (no daemon required, no heavy dependencies).
#
# Wired into .claude/settings.json by setup.py. Do not edit the path below.
#
# Usage (called by Claude Code hooks automatically):
#   echo '{"prompt":"..."}' | guard.sh stimulus-check
#   echo '{"tool_name":"Bash","tool_input":{...}}' | guard.sh action-enforce
#   echo '{"tool_name":"Write","tool_input":{...}}' | guard.sh post-validate

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVENT_TYPE="${1:-}"

if [[ -z "$EVENT_TYPE" ]]; then
  exit 0
fi

PAYLOAD_FILE=$(mktemp)
trap 'rm -f "$PAYLOAD_FILE"' EXIT
cat > "$PAYLOAD_FILE"

if [[ ! -s "$PAYLOAD_FILE" ]]; then
  exit 0
fi

PYTHON="${SYSGRAPH_PYTHON:-python3}"

# Exit 2 from guard.py means BLOCK — propagate it
"$PYTHON" "$SCRIPT_DIR/guard.py" "$EVENT_TYPE" < "$PAYLOAD_FILE"
