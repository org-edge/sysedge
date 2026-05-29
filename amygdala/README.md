# Amygdala — Session Guardrail System

The amygdala is an optional Claude Code hook that enforces SysEdge's safety rules
in real time. It intercepts tool calls before they execute and blocks operations
that could corrupt the shared graph.

## What it blocks

- `DETACH DELETE` and `DELETE` Cypher — prevents accidental node deletion
- `sys_graph_reset.sh` — the nuclear option; must be run manually by the user
- Raw Python scripts that bypass `sys_graph.py` — all graph writes must go through the CLI
- `docker restart` on the Neo4j container — disrupts all parallel sessions

## What it does NOT do

- It does not phone home or transmit data
- It does not modify files outside the Neo4j graph
- It does not run automatically — it must be wired as a Claude Code PreToolUse hook

## Setup (optional)

```bash
# Wire as a PreToolUse hook in your .claude/settings.json:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{"type": "command", "command": "python3 /path/to/amygdala/guard.py"}]
    }]
  }
}
```

Without this hook, SysEdge works normally — the guardrail is a safety net for teams
running many parallel sessions where an accidental graph wipe would be disruptive.
