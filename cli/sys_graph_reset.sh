#!/usr/bin/env bash
# sys_graph_reset.sh — Wipe all Sys* nodes from the system knowledge graph.
#
# This script is intentionally NOT part of sys_graph.py so Claude sessions
# cannot invoke it. Only a human running a shell can execute this.
#
# Usage:  ./scripts/sys_graph_reset.sh
#
# The script will:
#   1. Take an automatic backup before touching anything
#   2. Show current node counts
#   3. Require you to type the full confirmation phrase
#   4. Delete all Sys* nodes

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  sys_graph_reset — WIPE ALL Sys* NODES                      ║"
echo "║                                                              ║"
echo "║  This deletes the shared system knowledge graph used by      ║"
echo "║  ALL parallel Claude sessions. They will lose their context  ║"
echo "║  until the graph is reseeded.                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Show current counts
echo "Current graph state:"
docker exec sysgraph-neo4j bash -c "cypher-shell -u neo4j -p password \
  'MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH \"Sys\") \
   RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY label'" 2>/dev/null \
  | grep -v "^label" | grep -v "^$" | awk '{printf "  %-30s %s\n", $1, $2}' || true

echo ""

# Auto-backup before anything else
TS=$(date +%Y-%m-%dT%H-%M-%S)
BACKUP_PATH="$ROOT/data/sys-prereset-${TS}.json"
echo "Taking pre-reset backup → $BACKUP_PATH"
python3 "$ROOT/scripts/sys_graph.py" backup --output "$BACKUP_PATH" 2>/dev/null \
  && echo "  ✓ Backup complete" \
  || { echo "  ✗ Backup FAILED — aborting reset"; exit 1; }

echo ""
echo "⚠  To proceed, type exactly:  DELETE ALL SYS NODES"
echo "   (anything else aborts)"
echo ""
read -r CONFIRM

if [[ "$CONFIRM" != "DELETE ALL SYS NODES" ]]; then
    echo "Aborted — graph unchanged."
    echo "Pre-reset backup was written to $BACKUP_PATH (safe to delete if not needed)."
    exit 0
fi

echo ""
echo "Deleting Sys* nodes..."

python3 - << PYEOF
import os, sys
from pathlib import Path
try:
    from dotenv import load_dotenv; load_dotenv(Path("$ROOT/.env"), override=False)
except ImportError:
    pass
from neo4j import GraphDatabase

drv = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "password")),
)
labels = [
    "SysSystem","SysDomain","SysModule","SysFeature","SysUserStory",
    "SysArchStd","SysArchDecision","SysTest","SysEndpoint","SysSymbol",
    "SysScenario","SysScenarioChapter","SysTrainingProgram","SysTrainingModule",
    "SysTestPackage","SysTestRun","SysFeedback","SysEnhancement","SysDefect",
    "SysUseCase","SysUser","SysApplication","SysDirCategory","SysArchStd",
]
total = 0
with drv.session() as s:
    for label in labels:
        result = s.run(f"MATCH (n:{label}) DETACH DELETE n RETURN count(n) AS cnt").single()
        cnt = result["cnt"] if result else 0
        if cnt:
            print(f"  deleted {cnt:4d} :{label}")
            total += cnt
drv.close()
print(f"\n✓ Reset complete — {total} nodes deleted")
PYEOF

echo ""
echo "Graph reset. To restore, run:"
echo "  python3 scripts/sys_graph.py seed $BACKUP_PATH"
echo ""
echo "Or to restore from a named backup:"
echo "  python3 scripts/sys_graph.py seed data/sys-backup-YYYY-MM-DDTHH-MM-SS.json"
