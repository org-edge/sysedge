# SysEdge — Session Instructions for OpenAI Codex / Qwen Code

This file is automatically loaded by OpenAI Codex CLI and Qwen Code when working in this directory.
It gives the agent the same knowledge graph orientation that Claude Code users get via the SysEdge plugin.

## What SysEdge does

SysEdge connects to a Neo4j graph that models requirements traceability (user stories → use cases → features → code → tests) and architecture standards. Three startup commands replace 10–30 minutes of file reading. Measured: 71% fewer orientation tokens on a real open-source codebase (Formbricks, actual API counts).

## Session Start

Run all three at the start of every session:

```bash
python3 cli/sys_graph.py briefing  --instance <your-instance>
python3 cli/sys_graph.py worklog   --instance <your-instance>
python3 cli/sys_graph.py test-gaps --instance <your-instance>
```

The worklog shows (in order): active notes → open defects → enhancements. Read your notes first.

**If the graph is unavailable** (`⚠ Graph database unavailable`): tell the user to start Docker and wait. Do not investigate yourself.

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
python3 cli/sys_graph.py test-gaps --instance <name>           # V-model coverage gaps by tier
python3 cli/sys_graph.py show-enhancement --id ENH-xxx
python3 cli/sys_graph.py show-defect      --id DEF-xxx
python3 cli/sys_graph.py show-usecase     --id UC-xxx
```

### Track work
```bash
# Always start-enhancement BEFORE starting work
python3 cli/sys_graph.py create-enhancement \
  --title "..." --instance <name> --priority Must|Should|Could --description "..."
python3 cli/sys_graph.py start-enhancement  --id ENH-xxx --instance <name>
python3 cli/sys_graph.py close-enhancement  --id ENH-xxx --instance <name>

# Always link-defect BEFORE fixing
python3 cli/sys_graph.py link-defect \
  --feature F-xxx --title "..." --severity high --instance <name>
python3 cli/sys_graph.py close-defect --id DEF-xxx
```

### Update spec content
```bash
python3 cli/sys_graph.py update-feature  --id F-xxx  --description "..."
python3 cli/sys_graph.py update-usecase  --id UC-xxx \
  --description "..." --preconditions "..." --authorized-roles "..." --failure-scenarios "..."
python3 cli/sys_graph.py update-story    --id US-xxx \
  --goal "..." --acceptance-criteria "..." --out-of-scope "..."
```

### Link code to features
```bash
python3 cli/sys_graph.py link-feature --feature F-xxx \
  --tests "test_auth.py::TestLogin::test_valid"
python3 cli/sys_graph.py link-endpoint \
  --feature F-xxx --method GET --path /api/auth/me --binary <name>
python3 cli/sys_graph.py link-usecase  --id UC-xxx --feature F-xxx --tests "test.py::Class::fn"
python3 cli/sys_graph.py scan-tests    --module MOD-xxx
```

### Quality review (AI-powered — Bootstrap Kit)
```bash
python3 cli/sys_graph.py coverage-review --uc UC-xxx    # AS-REQ 7-dimension spec audit
python3 cli/sys_graph.py audit-test --uc UC-xxx \
  --file path/to/test.ts                               # AS-TEST-UC 7-dimension test audit
python3 cli/sys_graph.py quality-review --entity UC-xxx --ai  # all standards
```

### Data
```bash
python3 cli/sys_graph.py backup
python3 cli/sys_graph.py record-run \
  --package "tests/..." --passed 42 --failed 0 --duration 18 --skip-reason missing-fixture
```

## AI calls within SysEdge

When `coverage-review`, `audit-test`, or `quality-review --ai` need to call an AI model,
SysEdge tries providers in this order:
1. `claude` CLI (Claude Code session)
2. `gemini` CLI (Gemini session)
3. `qwen` CLI (Qwen Code session — this session)
4. `codex exec` (OpenAI Codex session — this session)
5. API keys: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`

No configuration needed when running inside a supported coding assistant.

## V-model test tiers

| Spec | Tier | Technology |
|---|---|---|
| User Story | `e2e` | Playwright — full cross-tool journey |
| Use Case | `usecase` | Playwright — single UC flow |
| Feature | `integration` | pytest / Jest API tests |
| Symbol | `component` | unit tests |

Gaps at any tier surface in `test-gaps --instance <name>` with the exact file to create.

## More information

- Free CLI: https://github.com/org-edge/sysedge
- Case studies: https://www.org-edge.com/sysedge.html
- Bootstrap Kit ($149/repo): https://www.org-edge.com/sysedge.html#get-sysgraph
