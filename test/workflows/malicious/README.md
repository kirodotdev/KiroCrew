# Adversarial workflow-script corpus (GATE B9)

Each `*.py` here is a **hostile workflow script** that `kiro_crew.workflows.validate`
MUST statically reject. They are **data/fixtures**, not pytest modules: `test/` is
one of the configured test paths, but these filenames do not match pytest's test
module pattern, so nothing here is collected. `test/test_workflows_malicious.py`
loads every file in this directory and asserts `validate(source).ok is False`.

**Flywheel:** when a new escape idea is found, drop it in as a new `*.py` file —
the loader test picks it up automatically. 100% must be rejected (B9).
