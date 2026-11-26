"""Per-test resets of process-global ``kiro_crew.dashboard.token_auth`` family state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

from collections import Counter

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_would_refuse_session_key_log(_floor_monkeypatch):
    """Start every test with no would-refuse line throttled and nothing counted.

    Any internal-secret request over TCP in a middleware test can fill
    ``token_auth_callers``' throttle map and counter, which would silence the line
    or shift the count for a later test on the same worker.
    """
    from kiro_crew.dashboard import token_auth_callers

    _floor_monkeypatch.setattr(token_auth_callers, "_would_refuse_log_last", {})
    _floor_monkeypatch.setattr(token_auth_callers, "_WOULD_REFUSE_COUNTS", Counter())
