# Tests

Kiro Crew uses pytest with pytest-asyncio for async tests. Pytest collects both
`test/` and the test suites shipped with builtin apps under
`src/kiro_crew/apps/builtins/`; `setup.cfg` is the source of truth for collection
and default options.

See the [testing conventions](../docs/system-specs/common/testing-conventions.md)
for isolation rules, platform guidance, worker budgeting, and the full command
reference.

## Running Tests

Run commands from the repository root:

```bash
# Related backend and frontend checks for the current diff:
python3 scripts/local-gate.py

# One test file (serial startup is faster for a narrow selection):
python -m pytest test/test_dashboard_chat.py -n0 -q

# One test by keyword:
python -m pytest -k "test_warm_pool" -n0 -q

# Only tests that failed on the previous run:
python -m pytest --lf -n0 -q

# Whole suite with the defaults from setup.cfg:
python -m pytest
```

Coverage is opt-in locally; CI requests it explicitly. For selective runs with
`pytest-testmon`, use the complete `--override-ini` command in the testing
conventions so the xdist safety flags are retained.

## Test Directories

- `test/` — main test directory
- `src/kiro_crew/apps/builtins/*/tests/` — builtin-app suites included by `testpaths`

## Conventions

- Name test files `test_<module>.py`.
- Mark each async test with `@pytest.mark.asyncio`; do not mark synchronous tests.
- Use `tmp_path` for filesystem tests and `monkeypatch` for config overrides.
- Mock external agent processes; tests must not spawn a real `kiro-cli` process.

## Smoke Tests

- `test/smoke_gateway.sh` — end-to-end gateway smoke test
- `test/smoke_sandbox.sh` — sandbox isolation smoke test
