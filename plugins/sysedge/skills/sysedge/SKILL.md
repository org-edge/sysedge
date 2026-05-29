---
name: sysedge
description: "Use at the start of every session. Loads the SysEdge knowledge graph layer for 30-second orientation via shared requirements graph: briefing, worklog, test-gaps, and full traceability from user stories to tests."
---

# SysEdge — Session Skill

## What this does

SysEdge gives every Claude Code session instant orientation from a shared Neo4j graph instead of re-reading source files. Three startup commands replace 10–30 minutes of file reading.

---

## ⛔ Hard Rules

| Prohibited | Why |
|---|---|
| Raw `DETACH DELETE` / `DELETE` Cypher | Removes shared data permanently — use `close-*` commands |
| `sys_graph_reset.sh` without user confirmation | Destroys the entire graph |
| Fixing a defect before logging it | Run `link-defect` first, then fix |
| Working outside your instance scope | Each instance owns its modules only |
| Debugging a graph DB connection failure yourself | Tell the user and wait |

**If the graph is unavailable** (startup commands print `⚠ Graph database unavailable`): tell the user "Graph database is unavailable — please start Docker and let me know when it's up." Do not investigate. Wait.

---

## Session Start

Run all three after reading your instance CLAUDE.md:

```bash
python3 cli/sys_graph.py briefing  --instance <your-instance>
python3 cli/sys_graph.py worklog   --instance <your-instance>
python3 cli/sys_graph.py test-gaps --instance <your-instance>
```

The worklog shows (in order): **active notes → open proposals → open defects → enhancements**. Read your notes before starting work — they are persistent memory from previous sessions.

---

## Complete list of free commands

### Read / orient
```bash
python3 cli/sys_graph.py briefing   --instance <name>          # coverage + defects + enhancements
python3 cli/sys_graph.py briefing   --instance <name> --compact
python3 cli/sys_graph.py worklog    --instance <name>           # what to work on
python3 cli/sys_graph.py test-gaps  --instance <name>           # missing tests by tier
python3 cli/sys_graph.py status                                 # whole-system dashboard
python3 cli/sys_graph.py features   --module MOD-xxx
python3 cli/sys_graph.py stories
python3 cli/sys_graph.py stories    --gap                       # stories with no use case
python3 cli/sys_graph.py show-enhancement --id ENH-024
python3 cli/sys_graph.py show-defect      --id DEF-042
python3 cli/sys_graph.py show-proposal    --id PROP-001
python3 cli/sys_graph.py show-notes       --instance <name>
python3 cli/sys_graph.py show-feature-tests --feature F-xxx
python3 cli/sys_graph.py test-status
```

### Update descriptions
```bash
python3 cli/sys_graph.py update-feature  --id F-xxx  --description "..."
python3 cli/sys_graph.py update-usecase  --id UC-xxx --description "..." --main-flow "..."
python3 cli/sys_graph.py update-story    --id US-xxx --goal "..." --acceptance-criteria "..."
python3 cli/sys_graph.py update-enhancement --id ENH-xxx --description "..."
```

### Enhancements
```bash
python3 cli/sys_graph.py create-enhancement \
  --title "..." --instance <name> --priority Must|Should|Could --description "..."
python3 cli/sys_graph.py start-enhancement  --id ENH-xxx --instance <name>   # do BEFORE starting work
python3 cli/sys_graph.py close-enhancement  --id ENH-xxx --instance <name>   # prints pre-close checklist
python3 cli/sys_graph.py link-enhancement   --id ENH-xxx --feature F-xxx
python3 cli/sys_graph.py link-blocks        --id ENH-xxx --blocked-by ENH-yyy
```

**Always run `start-enhancement` before starting work** — other sessions see it immediately.

**Before `close-enhancement` — confirm each applies or explicitly doesn't:**
1. `link-endpoint` run for every new API endpoint
2. `link-symbol` run for every new code symbol
3. UC REQUIRES→Feature edges wired (`link-usecase`)
4. Tests linked to features (`link-feature`)
5. User stories still accurately describe what was built

### Defects

**Rule: `link-defect` runs BEFORE any code fix begins.**

```bash
python3 cli/sys_graph.py link-defect \
  --id DEF-042 --feature F-xxx \
  --title "..." --severity high|medium|low --instance <name>
python3 cli/sys_graph.py close-defect --id DEF-042
```

### Proposals (design work before it becomes an enhancement)
```bash
python3 cli/sys_graph.py create-proposal \
  --title "..." --instance <name> --priority Should --description "..."
python3 cli/sys_graph.py update-proposal  --id PROP-001 --status accepted
python3 cli/sys_graph.py close-proposal   --id PROP-001 --outcome filed --filed-as ENH-042
python3 cli/sys_graph.py close-proposal   --id PROP-001 --outcome rejected
```

### Notes (instance memory)
```bash
python3 cli/sys_graph.py add-note \
  --instance <name> \
  --body "Decided X because Y — revisit if Z" \
  --expires 2026-07-01   # optional
python3 cli/sys_graph.py expire-note --id NOTE-001
```

**Notes vs Proposals vs Feedback:**
- `SysNote` — cross-session memory, design rationale, reminders
- `SysProposal` — design decisions awaiting sign-off or filing
- `SysFeedback` — observations about the SysEdge tooling itself

### User stories and use cases
```bash
python3 cli/sys_graph.py create-story \
  --id US-016 --title "..." --actor "Manager" --goal "..." --priority Must

python3 cli/sys_graph.py create-usecase \
  --id UC-xxx --instance <name> --title "..." --description "..." --story US-016

python3 cli/sys_graph.py link-usecase  --id UC-xxx --feature F-xxx --story US-016
python3 cli/sys_graph.py link-story    --story US-016 --features F-xxx,F-yyy
```

### Link tests and code to features
```bash
python3 cli/sys_graph.py link-feature \
  --feature F-xxx \
  --tests "test_auth.py::TestLogin::test_valid_credentials"

python3 cli/sys_graph.py link-endpoint \
  --feature F-xxx --method GET --path /api/auth/me \
  --binary <your-binary> --permission read:org

python3 cli/sys_graph.py link-symbol \
  --feature F-xxx \
  --file backend/internal/auth/handlers.go --symbol handleGetMe
```

### Scan (additive — safe to re-run)
```bash
python3 cli/sys_graph.py scan-tests     --module MOD-xxx
python3 cli/sys_graph.py scan-go-tests
python3 cli/sys_graph.py scan-code      --module MOD-xxx
```

### Features
```bash
python3 cli/sys_graph.py create-feature \
  --id F-xxx --module MOD-xxx --name "..." --description "..."
python3 cli/sys_graph.py retire-feature --id F-xxx --reason "..."
```

### Coverage report (V-model gap analysis — free)
```bash
python3 cli/sys_graph.py coverage-report --instance <name>
```

### Data
```bash
python3 cli/sys_graph.py backup
python3 cli/sys_graph.py seed <backup.json> --instance <name>   # per-instance restore only
python3 cli/sys_graph.py record-run \
  --package "tests/test_auth.py" --passed 42 --failed 0 --duration 18.4
```

### Feedback (about the SysEdge tooling)
```bash
python3 cli/sys_graph.py feedback \
  --instance <name> --category general|usability|gap|workflow|positive \
  --body "..."
```

---

## V-model test tiers

| Spec artefact | Test tier | Technology | Owned by |
|---|---|---|---|
| User Story | `e2e` | Playwright — full journey | master/coordinator session |
| Use Case | `usecase` | Playwright — single UC flow | owning session |
| Feature / Module | `integration` | pytest / Jest API tests | owning session |
| Symbol / Routine | `component` | Go test, vitest, unit | owning session |

---

## Premium features (Bootstrap Kit)

The following require the **SysEdge Bootstrap Kit** ($149/repo):

- `export` — structured Markdown/JSON document export
- `analyse` — merge candidates, split candidates, orphan detection
- `traceability-review` — AI semantic sufficiency check (PASS/PARTIAL/GAP/INSUFFICIENT)
- `create-adr` — architecture decision records
- `preview-import` / `commit-import` — requirements import pipeline
- `/init-sysedge` — auto-seed from working directory
- `sys_graph_viz.py` — web visualiser with drill-down navigation
- Architecture standards YAML (53 standards, 5 domains)
- Docker Compose one-command setup

[Get the Bootstrap Kit →](https://www.org-edge.com/sysedge.html)
