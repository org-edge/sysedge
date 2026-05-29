# Pattern: Integration Testing

## Context

P6 hosts integration tests that run against live backend APIs. Test failures need to flow into the same DEFECTS.md workflow as backend runtime errors so they're visible during `/check-health` and `/daily` routines.

## Approach

### Test Harness Structure

Tests live in `projects/p6-sample-data-loader/tests/` and are organised by project and test type:

| File Pattern | Type | Example |
|-------------|------|---------|
| `test_<project>_api.py` | API endpoint tests | `test_p4_api.py` |
| `test_<project>_scenarios.py` | Multi-step workflow tests | `test_p4_scenarios.py` |
| `test_<project>_frontend.py` | Playwright UI tests | `test_p4_frontend.py` |
| `test_<project>_ui_scenarios.py` | UI + Neo4j verification | `test_p4_ui_scenarios.py` |

### Shared Fixtures (conftest.py)

Key fixtures provided by `conftest.py`:

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `client` | session | `httpx.Client` with auth headers |
| `auth_headers` | session | `{"Authorization": "Bearer <token>"}` |
| `p4_url`, `p8_url`, etc. | session | Base URLs for each backend |
| `p8_delegation_url`, etc. | session | Per-tool P8 URLs |
| `clean_org` | session | Fresh test org (integration-test) |
| `loaded_org` | session | StartupX data loaded via P6 loader |
| `scenario_org` | function | Per-test isolated org with cleanup |
| `neo4j_session` | function | Direct Neo4j session for DB verification |
| `verify_db` | session | `verify_neo4j()` function wrapper |

### Test File to Project Mapping

When test failures are extracted to DEFECTS.md, the test must be mapped to the correct project:

**Standard projects:** Extracted from filename: `test_p4_api.py` -> `p4`

**P8 sub-tools:** Each P8 tool is a separate microservice with its own DEFECTS.md. Since all P8 tests live in `test_p8_api.py` and `test_p8_scenarios.py`, the **class name prefix** determines the sub-tool:

| Class Prefix | Project ID | Example Class |
|-------------|-----------|---------------|
| `Delegation` | `p8-delegation` | `TestDelegationHealth`, `TestDelegationCRUD` |
| `Positions` | `p8-positions` | `TestPositionsHealth`, `TestPositionsCRUD` |
| `Skills` | `p8-skills` | `TestSkillsHealth` |
| `Succession` | `p8-succession` | `TestSuccessionHealth` |
| `Development` | `p8-development` | `TestDevelopmentHealth` |
| `Docgen` | `p8-docgen` | `TestDocgenHealth` |
| `Decision` | `p8-decision` | `TestDecisionHealth` |
| `PartnerContract` | `p8-partner-contract` | `TestPartnerContractHealth` |

**Convention:** P8 test classes MUST start with `Test<ToolPrefix>` where `<ToolPrefix>` matches an entry in `P8_CLASS_PREFIX_MAP` in `scripts/extract_defects.py`.

### Test Failure Pipeline

```
pytest hooks (conftest.py)        extract_defects.py         DEFECTS.md
┌───────────────────────┐    ┌──────────────────────┐    ┌──────────────┐
│ pytest_runtest_        │    │ load_test_failures()  │    │ ## Defect N  │
│   makereport()        │───>│ _resolve_test_project()│───>│ [TEST FAILURE]│
│ pytest_sessionfinish() │    │ merge into entries    │    │ - Test: ...  │
└───────────────────────┘    └──────────────────────┘    └──────────────┘
        │                            │
        v                            v
  logs/test-failures.json    <project>/DEFECTS.md
```

1. **pytest hooks** collect failures during test run, write `logs/test-failures.json`
2. **extract_defects.py** reads the JSON, resolves each failure to a project, merges with log-based defects
3. **DEFECTS.md** shows `[TEST FAILURE]` entries with the pytest nodeid and traceback
4. When all tests pass, `logs/test-failures.json` is deleted (clean state)

## Implementation

### Adding tests for a new project

1. Create `tests/test_<project>_api.py` with test classes
2. Add URL fixture to `conftest.py` if needed
3. Ensure the project ID exists in `PROJECT_DIRS` in both `scripts/extract_defects.py` and `scripts/archive_defect.py`

### Adding tests for a new P8 sub-tool

1. Add test classes to `tests/test_p8_api.py` (API) or `tests/test_p8_scenarios.py` (scenarios)
2. Name classes with the tool prefix: `class Test<ToolPrefix>...`
3. Add the prefix to `P8_CLASS_PREFIX_MAP` in `scripts/extract_defects.py`
4. Add the project directory to `PROJECT_DIRS` in both `extract_defects.py` and `archive_defect.py`
5. Add URL fixture to `conftest.py` (e.g., `p8_<tool>_url`)

### Expected-error tests

Tests that intentionally trigger backend errors (e.g., testing 404 handling, invalid input validation) should be marked with `@pytest.mark.expected_error`. This prevents the backend errors they trigger from appearing as defects.

```python
@pytest.mark.expected_error
def test_load_nonexistent_file(self, client, p3_url):
    resp = client.post(f"{p3_url}/api/import/load", json={"file": "nonexistent.yaml"})
    assert resp.status_code == 404
```

How it works: conftest.py records the time window (start/end) when each `expected_error` test runs and writes it to `logs/test-expected-error-windows.json`. When `extract_defects.py` processes backend logs, it skips ERROR entries whose timestamps fall within any expected-error window (with a ±2 second buffer).

### Running and extracting

```bash
# Run tests (failures go to logs/test-failures.json)
cd projects/p6-sample-data-loader
pytest tests/ -v

# Extract all defects (logs + test failures)
cd ../..
python scripts/extract_defects.py

# Archive a fixed test defect
python scripts/archive_defect.py p8-delegation 1
```

### Cross-Project E2E Tests

End-to-end tests verify multi-project workflows that a user would perform in sequence. These test the full stack including the Caddy gateway, multiple backends, Neo4j, and optionally the browser UI.

**File naming:** `test_e2e_<workflow>.py`

**Examples of E2E scenarios:**
- Create org (P4) → load sample data (P6) → visualize (P2) → assess (P2)
- Load document (P3) → view extracted entities (P4) → edit entity (P4) → verify in graph (P2)
- Create function (P4) → plan function (P8) → generate doc (P8) → view in org (P2)

**Structure:**

```python
"""E2E: Organization lifecycle — create, populate, visualize, assess."""
import pytest

@pytest.mark.e2e
class TestOrgLifecycle:
    """Multi-project org lifecycle scenario."""

    def test_create_org_in_p4(self, client, p4_url, scenario_org):
        """Step 1: Create organization via P4."""
        resp = client.post(f"{p4_url}/api/entities/Organization", json={...})
        assert resp.status_code == 201

    def test_load_data_via_p6(self, scenario_org):
        """Step 2: Load sample data into the org."""
        # Run P6 loader script
        ...

    def test_visualize_in_p2(self, client, p2_url, scenario_org):
        """Step 3: Verify org appears in P2 graph."""
        resp = client.get(f"{p2_url}/api/graph/{scenario_org}")
        assert resp.status_code == 200
        assert len(resp.json()["nodes"]) > 0
```

**Running E2E tests:**

```bash
pytest tests/ -v -m e2e          # E2E tests only
pytest tests/ -v -m "not e2e"    # Skip E2E tests
```

**When to write E2E vs integration tests:**
- **Integration (single project):** Testing one project's API endpoints or UI in isolation
- **E2E (cross-project):** Testing a workflow that spans 2+ projects and verifies data flows correctly between them

## Used By

- P6: `projects/p6-sample-data-loader/tests/conftest.py` (hooks)
- Scripts: `scripts/extract_defects.py` (test failure loading)
- Scripts: `scripts/archive_defect.py` (archiving)
- All projects: `<project>/DEFECTS.md` (output)

## Change Log

- 2026-03-07: Added cross-project E2E test section, linked to unit-testing.md
- 2026-02-16: Initial pattern
