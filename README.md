# SysEdge

**Requirements traceability for AI coding agent teams.**

When multiple AI coding sessions work on the same codebase simultaneously, they burn tokens re-reading source files, duplicate each other's work, and leave coverage gaps no one notices. SysEdge gives every session a shared, live graph of what exists, what's tested, and what's pending — updated by the sessions themselves as they work.

Verified on two production open-source codebases, cloned cold:
- **Formbricks** (survey platform): 71% fewer orientation tokens — and SysEdge surfaced the PII spec gap that caused the export defect, before the code shipped
- **Documenso** (e-signature platform): 5 findings in 15 minutes — including Inngest job handlers with no tests and expiration exception paths never specified

---

## Supported AI coding assistants

SysEdge works with any of these — no API key required when running inside the tool:

| Tool | Session instructions | AI calls |
|---|---|---|
| **Claude Code** | `/plugin install sysedge@sysedge` | `claude` CLI (session tokens) |
| **Gemini CLI** | `GEMINI.md` auto-loaded | `gemini` CLI (session tokens, free) |
| **Qwen Code** | `AGENTS.md` auto-loaded | `qwen` CLI (session tokens) |
| **OpenAI Codex** | `AGENTS.md` auto-loaded | `codex exec` (session tokens) |

For environments without a coding assistant CLI, set any of: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `DASHSCOPE_API_KEY` in your `.env` file.

---

## What it looks like

```
$ python3 cli/sys_graph.py briefing --instance api

  MOD-auth           Auth & Session       12/12 ✓
                       ✓cmp 12/12  ✓int 12/12  ~uc  8/12  ✗e2e 0/12
  MOD-orders         Orders API           9/9   ✓
                       ✓cmp  9/9   ✓int  9/9   ✓uc  9/9   ✗e2e 0/9

  ENHANCEMENTS (2) — PROPOSED
    ENH-14  [Must]   Add rate limiting to /auth/login  → F-AUTH-003
    ENH-15  [Should] Pagination on /orders endpoint    → F-ORD-002

$ python3 cli/sys_graph.py start-enhancement --id ENH-14 --instance api
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

**5. Connect your AI coding assistant**

**Claude Code** (plugin marketplace):
```
/plugin marketplace add org-edge/sysedge
/plugin install sysedge@sysedge
```

**Gemini CLI** — copy `GEMINI.md` to your project root. Gemini auto-loads it:
```bash
cp /path/to/sysedge/GEMINI.md .
gemini   # GEMINI.md loaded automatically
```
Install Gemini CLI: `npm install -g @google/generative-ai-cli` then `gemini auth login` (free, Google account).

**Qwen Code / OpenAI Codex** — copy `AGENTS.md` to your project root. Both auto-load it:
```bash
cp /path/to/sysedge/AGENTS.md .
qwen     # or: codex
```
Install Qwen Code: `npm install -g @qwen/qwen-code` then set `DASHSCOPE_API_KEY` (Alibaba Cloud free tier), or point to a local Ollama model.

---

## Bootstrap your project config

The **Bootstrap Kit** includes `/init-sysedge` — a skill that scans your working directory, detects Go/TypeScript/Python/Java/C# structure, and generates a complete seed JSON automatically. No manual JSON writing required.

[Get the Bootstrap Kit →](https://www.org-edge.com/sysedge.html)

---

## Key commands

| Command | What it does |
|---|---|
| `briefing --instance X` | Coverage by module, open enhancements, defects (30 seconds) |
| `worklog --instance X` | Prioritised work queue for this session |
| `test-gaps --instance X` | Missing test tiers per feature |
| `quality-review --entity UC-X` | Automated QUS + TRC standards check (free, no AI key) |
| `quality-review --entity UC-X --ai` | + Cockburn UC guidance via AI (uses session tokens) |
| `start-enhancement --id ENH-X --instance X` | Mark in-progress — visible to all sessions |
| `close-enhancement --id ENH-X --instance X` | Mark done — smart checklist |
| `link-defect --feature F-X --title "..." --severity high --instance X` | File a defect before fixing it |
| `link-endpoint --feature F-X --method GET --path /api/...` | Link endpoint to feature |
| `audit-status --instance X` | Last audit-test + coverage-review timestamps — flags stale |
| `feedback-submit --category gap --body "..." --instance X` | Send feedback to sysedge-feedback@org-edge.com |
| `backup` | Export full graph to JSON |

---

## Instance topology

Every project needs three permanent roles: `architect` (what to build), `master` (what's shipped), `graph` (graph health). Add feature instances per domain — not per technology layer.

```
architect   — US design, ADRs, architecture standards
master      — US maintenance, E2E tests, shared patterns
graph       — SysEdge health, backups, seed operations
content     — Website, README, i18n, help content, skill files (doc staleness tracked)
deploy      — Docker, CI/CD, runbooks, install docs  (doc staleness tracked)

api         — REST handlers, service layer     (MOD-orders, MOD-customers…)
auth        — Authentication, permissions      (MOD-auth, MOD-sessions…)
ui          — Frontend components, routing     (MOD-dashboard, MOD-admin…)
```

`content` and `deploy` sessions use `doc-gaps` to find documentation that has drifted from recent code changes:

```bash
python3 cli/sys_graph.py doc-gaps --instance content   # website, README, skill files
python3 cli/sys_graph.py doc-gaps --instance deploy    # runbooks, install docs, Docker configs
python3 cli/sys_graph.py mark-doc-reviewed --id DOC-website-main  # stamp after reviewing
```

See [INSTANCES.md](INSTANCES.md) for the full guide.

---

## V-model test coverage

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
- `reset` is a shell script requiring manual confirmation — coding assistant sessions cannot run it
- Every `seed` auto-backs up before running
- Sessions only write nodes in their declared instance scope

---

## Pricing

**Free (this repo):** CLI, GEMINI.md, AGENTS.md, Docker Compose setup — everything in this README.

**$149/repository** — [Bootstrap Kit](https://www.org-edge.com/sysedge.html): web visualiser, `/init-sysedge` auto-seed skill, architecture standards catalogue (53 standards), `audit-test` (7 AS-TEST dimensions), `coverage-review --uc/--us` (7 AS-REQ dimensions), graph analysis, export commands, session templates, 12 months updates and email support.

---

MIT + Commons Clause · Built at [OrgEdge](https://www.org-edge.com) · [sysedge@org-edge.com](mailto:sysedge@org-edge.com)

Free to use for your own projects (including commercial software development).
You may not sell the CLI itself as a product or service. See `LICENSE` for full terms.
