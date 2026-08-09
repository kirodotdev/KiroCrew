"""Collection-time platform gate for the Code Review Sage suite.

The app itself refuses to run off POSIX — `sage_lib/discovery.py` raises
"Code Review Sage requires a POSIX platform (macOS/Linux); run the Kiro Crew
gateway under WSL on Windows" — so these tests assert POSIX behaviour throughout:
`0600` file modes, forward-slash path suffixes, and a `gh` binary the Windows
runner never reaches because the platform guard fires first.

Running them on Windows therefore tests a configuration the app does not support,
and the failures say nothing about the code. Skip the suite there rather than
teaching nine assertions to be platform-agnostic for a platform the app rejects.

This lives here, next to the suite it gates, so the reason travels with the tests
rather than sitting in a CI workflow that would hide it.
"""
import os

import pytest

collect_ignore_glob = ["*"] if os.name == "nt" else []

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Code Review Sage requires a POSIX platform (see sage_lib/discovery.py)",
)


@pytest.fixture(autouse=True)
def _mute_shared_runner_audit(monkeypatch):
    """No test in this suite may write the operator's real SEL log.

    ``discovery.run_gh_json`` / ``current_login`` / ``pipeline.list_open_prs``
    now route through ``github_runner.run_gh``, which emits a real SEL event
    per spawn. ``KIROCREW_HOME`` is isolated below, but ``config_dir()`` caches
    the resolved home at its FIRST call in the process, so a suite that
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


@pytest.fixture(autouse=True)
def _isolate_app_home(monkeypatch, tmp_path):
    """Point ``$KIROCREW_HOME`` at a tmp dir so the suite never touches the real one.

    ``store.app_root()`` derives from ``crew_home()``, so without this every test
    that reaches ``load_config()`` reads the DEVELOPER'S live app config -- and
    seeds one into their real data directory when it is absent, because
    ``load_config`` calls ``ensure_layout`` on a miss. Both are wrong: a machine
    with ``review.max_concurrent`` configured fails assertions that CI passes
    (CI has no config, so it sees the seeded default), and a machine without one
    gets files written outside the test's tmp dir.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
