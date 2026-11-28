"""Per-test reset of the ``kiro_crew.executors`` path-resolver pool size.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

from conftest_guards import per_test_guard


@per_test_guard
def _path_resolver_pool_keeps_its_import_size(_floor_monkeypatch):
    """Start every test with the pool size a fresh non-gateway import reads.

    A test that runs ``cli.main()`` as ``gateway`` raises it to the gateway's size,
    which would otherwise size the pool of every later test on the same worker.
    """
    from kiro_crew import executors

    _floor_monkeypatch.setattr(
        executors, "_MAX_PATH_RESOLVE_WORKERS", executors._path_resolve_worker_count()
    )
