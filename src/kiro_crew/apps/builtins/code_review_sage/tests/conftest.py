"""Collection-time platform gate for the Code Review Sage suite.

The app itself now runs on Windows: the provider-CLI trust gate it shares with
Issue Radar answers from the Windows ACL, and the review worker is handed the
absolute interpreter the app resolves rather than the bare `python3` that is not
an interpreter there.

What still gates the suite is the suite, not the app. These tests assert POSIX
behaviour throughout — `0600` file modes as `st_mode` bits (Windows expresses
owner-only as a DACL and always reports `0o666`), forward-slash path suffixes,
and shell-script `gh` stubs the Windows runner cannot execute — so running them
there would fail on the harness rather than on anything under test. Making them
Windows-native is separate work from making the app run.

This lives next to the suite it gates, so the reason travels with the tests
rather than sitting in a CI workflow that would hide it.
"""

import os

import pytest

collect_ignore_glob = ["*"] if os.name == "nt" else []

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Code Review Sage's test harness is POSIX-only (see this conftest's docstring)",
)


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
