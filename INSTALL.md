# SysEdge — Installation Guide

## Prerequisites

- Docker
- Python 3.10+
- Claude Code CLI

---

## 1. Start Neo4j

```bash
cd setup/
docker compose up -d
```

Neo4j starts on `bolt://localhost:7688` (port 7688 avoids collision with any application Neo4j you already run). Browser UI (optional) at `http://localhost:7475`.

Default credentials: `neo4j / sysedge`

---

## 2. Configure connection

```bash
cp setup/.env.template .env
# Edit .env if you changed the password
```

Place `.env` in your **project root** — the same directory you run `sys_graph.py` from.

```
NEO4J_URI=bolt://localhost:7688
NEO4J_USER=neo4j
NEO4J_PASSWORD=sysedge
SYSGRAPH_NEO4J_URI=bolt://localhost:7688
SYSGRAPH_NEO4J_USER=neo4j
SYSGRAPH_NEO4J_PASSWORD=sysedge
```

---

## 3. Install Python dependencies

```bash
pip install neo4j python-dotenv pyyaml
```

---

## 4. Initialise the graph

```bash
python3 cli/sys_graph.py init
```

Creates Neo4j constraints. Safe to re-run.

---

## 5. Seed your project

```bash
cp examples/seed-example.json data/sys-init.json
# Edit to define your modules and features, then:
python3 cli/sys_graph.py seed data/sys-init.json
```

See `examples/seed-example.json` for the format and `INSTANCES.md` for how to design your instance topology.

> **Bootstrap Kit users:** use `/init-sysedge` in a Claude Code session to auto-generate the seed from your working directory. [Get the kit →](https://www.org-edge.com/sysgraph.html)

---

## 6. Install the Claude Code plugin

```
/plugin marketplace add org-edge/sysedge
/plugin install sysedge@sysedge
```

Or install the skill manually:

```bash
mkdir -p .claude/skills/sysedge
cp plugins/sysedge/skills/sysedge/SKILL.md .claude/skills/sysedge/
```

---

## 7. Verify

```bash
python3 cli/sys_graph.py briefing --instance <your-first-instance>
```

---

## Session start protocol

Add this to each Claude Code session's `CLAUDE.md`:

```markdown
## Session Start
  python3 cli/sys_graph.py briefing  --instance <your-instance>
  python3 cli/sys_graph.py worklog   --instance <your-instance>
  python3 cli/sys_graph.py test-gaps --instance <your-instance>
```

---

## Language support

| Language | Symbol scan | Test scan |
|---|---|---|
| Go | ✓ Auto | ✓ Auto (`*_test.go`) |
| TypeScript | ✓ Auto | ✓ Auto (`*.spec.ts`) |
| Python | ✓ Auto | ✓ Auto (`test_*.py`) |
| Java / C# / Rust | Manual `link-symbol` | Manual `link-feature` |

---

## Backup and recovery

```bash
python3 cli/sys_graph.py backup
python3 cli/sys_graph.py seed data/sys-backup-LATEST.json --instance <name>
```
