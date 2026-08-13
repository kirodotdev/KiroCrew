# Tests

KiroCrew uses pytest with pytest-asyncio for async tests. ~170 test files, 3000+ tests.

## Running Tests

```bash
# Full cycle (format + build + test):
brazil-build format && brazil-build clean && brazil-build && brazil-build test

# Fast iteration — only tests affected by changes:
python -m pytest --testmon --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q

# Specific test file:
python -m pytest test/test_dashboard_chat.py --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q

# Specific test by keyword:
python -m pytest -k "test_warm_pool" --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q

# Only previously failed:
python -m pytest --lf --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q
```

The `--override-ini` flag skips coverage measurement (configured in `setup.cfg`) for faster iteration.

## Test Directories

- `test/` — main test directory (170+ files)
- `tests/` — additional tests (6 files)

## Conventions

- Test files: `test/test_<module>.py`
- Use `pytest-asyncio` with `mode=strict` — every async test needs `@pytest.mark.asyncio`
- Use `tmp_path` fixture for filesystem tests
- Use `monkeypatch` for config overrides
- Mock external processes (kiro-cli) — never spawn real processes in tests
- Group related tests in classes: `class TestFeatureName:`

## Smoke Tests

- `test/smoke_gateway.sh` — end-to-end gateway smoke test
- `test/smoke_sandbox.sh` — sandbox isolation smoke test
