# SysEdge

**Requirements traceability for Claude Code multi-agent teams.**

When ten Claude Code sessions work on the same codebase simultaneously, they burn tokens re-reading source files, duplicate each other's work, and leave coverage gaps no one notices. SysEdge gives every session a shared, live graph of what exists, what's tested, and what's pending — updated by the sessions themselves as they work.

---

## What it looks like

```
$ python3 sys_graph.py briefing --instance api

  MOD-auth           Auth & Session       12/12 ✓
                       ✓cmp 12/12  ✓int 12/12  ~uc  8/12  ✗e2e 0/12
  MOD-orders         Orders API           9/9   ✓
                       ✓cmp  9/9   ✓int  9/9   ✓uc  9/9   ✗e2e 0/9

  ENHANCEMENTS (2) — PROPOSED
    ENH-14  [Must]   Add rate limiting to /auth/login  → F-AUTH-003
    ENH-15  [Should] Pagination on /orders endpoint    → F-ORD-002

$ python3 sys_graph.py start-enhancement --id ENH-14 --instance api
✓ ENH-14 in-progress — other sessions can see this is being built
```

---

## Install

**1. Start Neo4j**
```bash
cd setup/
docker compose up -d
```

**2. Python dependencies**
```bash
pip install neo4j python-dotenv pyyaml
```

**3. Configure**
```bash
cp setup/.env.template .env
# Edit NEO4J_PASSWORD if changed from default
```

**4. Initialise and seed**
```bash
python3 cli/sys_graph.py init
cp examples/seed-example.json data/sys-init.json
# Edit data/sys-init.json to define your modules and features, then:
python3 cli/sys_graph.py seed data/sys-init.json
```

**5. Install the Claude Code plugin**

Via the plugin marketplace (recommended — requires Claude Code v2.1.128+):
```
/plugin marketplace add org-edge/sysedge
/plugin install sysedge@sysedge
```

Then invoke with `/sysedge:sysedge` at the start of any session.

Or install manually:
```bash
mkdir -p .claude/skills/sysedge
curl -o .claude/skills/sysedge/SKILL.md \
  https://raw.githubusercontent.com/org-edge/sysedge/main/plugins/sysedge/skills/sysedge/SKILL.md
```

---

## Bootstrap your project config

The **Bootstrap Kit** includes `/init-sysedge` — a Claude Code skill that scans your working directory, detects Go/TypeScript/Python/Java/C# structure, and generates a complete seed JSON automatically. No manual JSON writing required.

[Get the Bootstrap Kit →](https://www.org-edge.com/sysedge.html)

---

## Key commands

| Command | What it does |
|---|---|
| `briefing --instance X` | Coverage by module, open enhancements, defects (30 seconds) |
| `worklog --instance X` | Prioritised work queue for this session |
| `test-gaps --instance X` | Missing test tiers per feature |
| `start-enhancement --id ENH-X --instance X` | Mark in-progress — visible to all sessions |
| `close-enhancement --id ENH-X --instance X` | Mark done, prints CONTEXT.md reminder |
| `show-enhancement --id ENH-X` | Full description + linked features |
| `create-enhancement --title "..." --instance X --priority Must` | File new work item |
| `link-endpoint --feature F-X --method GET --path /api/...` | Link endpoint to feature |
| `backup` | Export full graph to JSON |
| `seed backup.json --instance X` | Restore only your instance's nodes (safe) |

---

## Instance topology

Every project needs three permanent roles: `architect` (what to build), `master` (what's shipped), `graph` (graph health). Add feature instances per domain — not per technology layer.

```
architect   — US design, ADRs, architecture standards
master      — US maintenance, E2E tests, shared patterns
graph       — SysEdge health, backups, seed operations

api         — REST handlers, service layer     (MOD-orders, MOD-customers…)
auth        — Authentication, permissions      (MOD-auth, MOD-sessions…)
ui          — Frontend components, routing     (MOD-dashboard, MOD-admin…)
deploy      — Docker, CI/CD, runbooks          (MOD-infra…)
```

See [INSTANCES.md](INSTANCES.md) for the full guide: naming conventions, scope templates, sizing by codebase size, and anti-patterns.

---

## V-model test coverage

SysEdge enforces the V-model. Each spec artefact has a required test artefact:

| Spec | Test tier | Technology |
|---|---|---|
| User Story | `e2e` | Playwright — cross-tool journey |
| Use Case | `usecase` | Playwright — single UC flow |
| Feature / Module | `integration` | pytest / Jest API tests |
| Symbol / Routine | `component` | Go test, vitest, pytest unit |

---

## Language support

| Language | Graph features | Code scan | Test scan |
|---|---|---|---|
| Go | ✓ Full | ✓ Auto (AST) | ✓ Auto (`*_test.go`) |
| TypeScript | ✓ Full | ✓ Auto (regex) | ✓ Auto (`*.spec.ts`) |
| Python | ✓ Full | ✓ Auto (AST) | ✓ Auto (`test_*.py`) |
| Java | ✓ Full | ✓ Auto (regex) | Manual `link-feature` |
| C# | ✓ Full | ✓ Auto (regex) | Manual `link-feature` |
| Rust / other | ✓ Full | Manual `link-symbol` | Manual `link-feature` |

---

## Safety model

- `seed` without `--instance` is **blocked** — prevents accidental full overwrites
- `reset` is a shell script requiring you to type `DELETE ALL SYS NODES` — Claude sessions cannot run it
- Every `seed` auto-backs up before running
- Sessions only write nodes in their declared instance scope

---

## Pricing

**Free (this repo):** CLI, skill file, Docker Compose setup, patterns — everything in this README.

**$149/repository** — [Bootstrap Kit](https://www.org-edge.com/sysedge.html): web visualiser, `/init-sysedge` auto-seed skill, architecture standards catalogue (53 standards), AI traceability review, export and analysis commands, session templates, 12 months updates and email support.

---

MIT + Commons Clause · Built at [OrgEdge](https://www.org-edge.com) · [sysedge@org-edge.com](mailto:sysedge@org-edge.com)

Free to use for your own projects (including commercial software development).
You may not sell the CLI itself as a product or service. See `LICENSE` for full terms.
