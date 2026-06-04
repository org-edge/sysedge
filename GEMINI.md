# SysEdge — Session Instructions for Gemini CLI

This file is automatically loaded by Gemini CLI when you run `gemini` in this directory.
It gives the agent the same knowledge graph orientation that Claude Code users get via the SysEdge plugin.

## What SysEdge does

SysEdge connects to a Neo4j graph that models requirements traceability (user stories → use cases → features → code → tests) and architecture standards. Three startup commands replace 10–30 minutes of file reading.

## Session Start

Run all three at the start of every session:

```bash
python3 cli/sys_graph.py briefing  --instance <your-instance>
python3 cli/sys_graph.py worklog   --instance <your-instance>
python3 cli/sys_graph.py test-gaps --instance <your-instance>
```

The worklog shows (in order): active notes → open defects → enhancements. Read your notes first.

**If the graph is unavailable** (`⚠ Graph database unavailable`): tell the user to start Docker and wait. Do not investigate the connection yourself.

## Hard Rules

- Never run `DETACH DELETE` or `DELETE` Cypher — use `close-*` commands only
- Never run `sys_graph_reset.sh` without explicit user confirmation
- Always run `link-defect` BEFORE fixing a defect — log first, fix second
- Only write to graph nodes within your declared instance scope
- Do not debug graph DB connection failures — tell the user and wait

## Key Commands

### Orientation
```bash
python3 cli/sys_graph.py briefing  --instance <name>           # coverage + defects + enhancements
python3 cli/sys_graph.py worklog   --instance <name>           # prioritised work queue
python3 cli/sys_graph.py test-gaps --instance <name>           # V-model coverage gaps
python3 cli/sys_graph.py show-enhancement --id ENH-xxx
python3 cli/sys_graph.py show-defect      --id DEF-xxx
```

### Track work
```bash
python3 cli/sys_graph.py create-enhancement \
  --title "..." --instance <name> --priority Must|Should|Could --description "..."
python3 cli/sys_graph.py start-enhancement  --id ENH-xxx --instance <name>  # before starting
python3 cli/sys_graph.py close-enhancement  --id ENH-xxx --instance <name>  # when done

python3 cli/sys_graph.py link-defect \
  --feature F-xxx --title "..." --severity high --instance <name>            # before fixing
python3 cli/sys_graph.py close-defect --id DEF-xxx
```

### Link code to features
```bash
python3 cli/sys_graph.py link-feature --feature F-xxx \
  --tests "test_auth.py::TestLogin::test_valid"
python3 cli/sys_graph.py link-endpoint \
  --feature F-xxx --method GET --path /api/auth/me --binary <name>
python3 cli/sys_graph.py scan-tests --module MOD-xxx
```

### AI-powered quality audit (Bootstrap Kit)
```bash
python3 cli/sys_graph.py coverage-review --uc UC-xxx          # spec adequacy (AS-REQ)
python3 cli/sys_graph.py audit-test --uc UC-xxx --file <test> # test quality (AS-TEST-UC)
python3 cli/sys_graph.py quality-review --entity UC-xxx --ai  # all standards + Cockburn guidance
```

### Data
```bash
python3 cli/sys_graph.py backup
python3 cli/sys_graph.py record-run --package "tests/..." --passed 42 --failed 0 --duration 18
```

## AI calls within SysEdge

When `coverage-review`, `audit-test`, or `quality-review --ai` need to call an AI model,
SysEdge first tries the `gemini` CLI (this session's tokens — no API key needed), then falls
back to `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` if set.

## V-model test tiers

| Spec | Tier | Technology |
|---|---|---|
| User Story | `e2e` | Playwright — full cross-tool journey |
| Use Case | `usecase` | Playwright — single UC flow |
| Feature | `integration` | pytest / Jest API tests |
| Symbol | `component` | unit tests |

## More information

- Free CLI: https://github.com/org-edge/sysedge
- Bootstrap Kit ($149/repo): https://www.org-edge.com/sysedge.html
