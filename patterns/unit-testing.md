# Pattern: Unit Testing

## Context

Each project owns its own unit tests. Unit tests run fast (no network, no database) and verify individual modules in isolation. They complement integration tests (hosted in P6) which test live backends and cross-project flows.

## Test Categories

| Category | Location | Framework | Runs Against | Speed |
|----------|----------|-----------|-------------|-------|
| **Unit (backend)** | `projects/pX/backend/tests/` | pytest + FastAPI TestClient | Mocked deps | < 1s per test |
| **Unit (frontend)** | `projects/pX/frontend/tests/` | vitest + @testing-library/react | jsdom | < 1s per test |
| **Integration (API)** | `projects/p6/tests/test_pX_api.py` | pytest + httpx | Live backends | 1-5s per test |
| **Integration (scenario)** | `projects/p6/tests/test_pX_scenarios.py` | pytest + httpx + Neo4j | Live backends + DB | 5-30s per test |
| **Integration (UI)** | `projects/p6/tests/test_pX_frontend.py` | pytest + Playwright | Live frontends | 5-15s per test |
| **Cross-project (E2E)** | `projects/p6/tests/test_e2e_*.py` | pytest + httpx + Playwright | Multiple live services | 10-60s per test |

## Backend Unit Tests

### Directory Structure

```
projects/pX-name/backend/
├── tests/
│   ├── conftest.py           # Shared fixtures (TestClient, mocked deps)
│   ├── test_router_entities.py    # Router endpoint tests
│   ├── test_router_search.py      # Another router
│   ├── test_neo4j_client.py       # Data layer tests
│   ├── test_models.py             # Pydantic model tests
│   └── test_ontology_client.py    # Service client tests
├── pytest.ini
└── app/
    └── ...
```

### conftest.py Template

```python
"""Shared test fixtures for <project> backend unit tests."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from app.main import app
from shared.auth.backend.dependencies import get_current_user


# --- Auth mock ---
def _mock_admin():
    return {
        "sub": "test|admin",
        "email": "admin@test.io",
        "roles": ["admin"],
        "permissions": ["read", "write", "delete"],
        "orgId": "test-org",
    }

@pytest.fixture(scope="module")
def client():
    """FastAPI TestClient with auth overridden to admin."""
    app.dependency_overrides[get_current_user] = _mock_admin
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- Neo4j mock ---
@pytest.fixture
def mock_neo4j(monkeypatch):
    """Mock Neo4j driver — returns empty results by default."""
    mock_session = MagicMock()
    mock_session.run.return_value = []
    mock_driver = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    # Patch wherever your app imports the driver
    monkeypatch.setattr("app.neo4j_client.get_driver", lambda: mock_driver)
    return mock_session
```

### Naming Conventions

- **File names:** `test_<module>.py` — mirrors the source module being tested
- **Class names:** `TestClassName` — group related tests
- **Function names:** `test_<action>_<scenario>` — e.g., `test_create_entity_missing_name`, `test_get_entity_not_found`

### What to Test

| Layer | What to test | What to mock |
|-------|-------------|-------------|
| **Routers** | HTTP status codes, response shapes, validation errors, auth enforcement | Neo4j client, external services |
| **Neo4j client** | Cypher query construction, parameter handling, result mapping | Neo4j driver (return canned records) |
| **Models** | Validation rules, defaults, enum constraints, serialization | Nothing |
| **Service clients** | Request construction, response parsing, error handling, caching | httpx (use `respx` or `monkeypatch`) |

### pytest.ini

```ini
[pytest]
testpaths = tests
```

### Running

```bash
cd projects/pX-name/backend
python -m pytest tests/ -v
```

## Frontend Unit Tests

### Directory Structure

```
projects/pX-name/frontend/
├── tests/
│   ├── setup.ts              # Test environment setup
│   ├── store/
│   │   └── myStore.test.ts   # Zustand store tests
│   ├── components/
│   │   └── MyComponent.test.tsx  # Component rendering tests
│   ├── hooks/
│   │   └── useMyHook.test.ts     # Custom hook tests
│   └── utils/
│       └── helpers.test.ts       # Utility function tests
├── vitest.config.ts
└── src/
    └── ...
```

### setup.ts Template

```typescript
import '@testing-library/jest-dom';

// Mock window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
```

### Component Test Example

```tsx
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { MyComponent } from '../../src/components/MyComponent';

describe('MyComponent', () => {
  it('renders title from props', () => {
    render(<MyComponent title="Test" />);
    expect(screen.getByText('Test')).toBeInTheDocument();
  });

  it('calls onSave when form submitted', async () => {
    const onSave = vi.fn();
    render(<MyComponent onSave={onSave} />);
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(onSave).toHaveBeenCalledOnce();
  });
});
```

### Store Test Example

```typescript
import { describe, it, expect, beforeEach } from 'vitest';
import { useMyStore } from '../../src/store/myStore';

describe('myStore', () => {
  beforeEach(() => {
    useMyStore.setState(useMyStore.getInitialState());
  });

  it('updates selected entity', () => {
    useMyStore.getState().selectEntity('entity-1');
    expect(useMyStore.getState().selectedEntityId).toBe('entity-1');
  });
});
```

### vitest.config.ts

```typescript
import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
  },
  resolve: {
    alias: {
      '@shared': path.resolve(__dirname, '../../shared'),
    },
  },
});
```

### Running

```bash
cd projects/pX-name/frontend
npx vitest run           # Single run
npx vitest --watch       # Watch mode
```

## Adding Tests to a Project

When implementing tests for a project that doesn't have them yet:

1. **Create the directory structure** — `backend/tests/` and/or `frontend/tests/`
2. **Create conftest.py / setup.ts** — Copy from P1 (backend) or P4 (frontend) and adapt
3. **Create pytest.ini / vitest.config.ts** — Use templates above
4. **Start with router tests** — They cover the most surface area with least effort
5. **Add to project CLAUDE.md** — Note that unit tests exist and how to run them
6. **Update CODE-MAP.md / TESTCODE-MAP.md** — List new test files

### Priority Order for New Tests

1. **Router/endpoint tests** — Verify API contracts (status codes, response shapes)
2. **Neo4j client tests** — Verify Cypher query construction and result mapping
3. **Component tests** — Verify UI rendering and user interactions
4. **Store tests** — Verify state management logic
5. **Model/utility tests** — Verify validation and transformation logic

## Used By

- P1: `backend/tests/` (84 tests — parser, ontology, BPs, relationships, enums)
- P4: `frontend/tests/` (56 tests — display names, entity refs, CASL, editor store)
- P9: `client/backend/tests/` (100 tests — middleware, validation, heartbeat, cache)
- Shared: `shared/assessment/test_*.py`, `shared/minio_utils/test_resolve.py`

## Change Log

- 2026-03-07: Initial pattern
