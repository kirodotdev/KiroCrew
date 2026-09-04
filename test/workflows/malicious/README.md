# Adversarial workflow-script corpus (GATE B9)

Each `*.py` here is a **hostile workflow script** that `kiro_crew.workflows.validate`
MUST statically reject. They are **data/fixtures**, not pytest modules — the
package only collects tests under `test/` (`testpaths = test`), so nothing here
runs. `test/test_workflows_malicious.py` loads every file in this directory and
asserts `validate(source).ok is False`.

**Flywheel:** when a new escape idea is found, drop it in as a new `*.py` file —
the loader test picks it up automatically. 100% must be rejected (B9).
