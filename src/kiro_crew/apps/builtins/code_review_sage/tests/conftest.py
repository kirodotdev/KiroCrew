"""Shared fixtures for the Code Review Sage suite.

The suite collects on every platform. Tests that pin inherently POSIX
behaviour carry their own ``skipUnless`` guards (owner-only mode bits,
unprivileged symlinks), so this module carries no platform gate -- only the
fixtures every platform needs.
"""

import pytest


@pytest.fixture(autouse=True)
def _mute_shared_runner_audit(monkeypatch):
    """No test in this suite may write the operator's real SEL log.

    ``discovery.run_gh_json`` / ``current_login`` / ``pipeline.list_open_prs``
    now route through ``github_runner.run_gh``, which emits a real SEL event
    per spawn. ``KIROCREW_HOME`` is pinned by the rootdir ``conftest.py``, but
    ``config_dir()`` caches the resolved home at its FIRST call in the process, so a suite that
    imported something touching it before the isolation fixture ran would
    write through the cached REAL data dir. The audit is not under test here
    (``test/test_github_runner.py`` covers it against a mocked SEL), so mute
    the emitter itself — deterministic regardless of cache state.
    """
    try:
        from kiro_crew import github_runner
    except ImportError:  # pragma: no cover - standalone checkout
        yield
        return
    monkeypatch.setattr(github_runner, "_audit_run", lambda *a, **k: None)
    yield


#: The rootdir ``conftest.py`` pins ``$KIROCREW_HOME`` for every testpath, which is what
#: keeps this suite off the real data home: ``store.app_root()`` derives from ``crew_home()``.
