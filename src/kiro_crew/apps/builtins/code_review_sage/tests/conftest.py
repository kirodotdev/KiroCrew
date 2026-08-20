"""Collection-time platform gate for the Code Review Sage suite.

Two independent reasons, both still true:

* the app refuses to run on Windows — `sage_lib/discovery.py` raises because its
  review worker invokes `python3`, which is not an interpreter there; and
* these tests assert POSIX behaviour throughout anyway (`0600` file modes,
  forward-slash path suffixes, shell-script `gh` stubs the Windows runner cannot
  execute).

Note what changed and what did not. The provider-CLI trust gate Sage shares with
Issue Radar is no longer POSIX-only: `github_runner.validate_provider_executable`
now answers from the Windows ACL. Sage's own refusal is narrower than it was — it
names the interpreter, not the trust check — but it is still a refusal, so
running this suite on Windows would still exercise a configuration the app
rejects.

This lives next to the suite it gates, so the reason travels with the tests
rather than sitting in a CI workflow that would hide it.
"""

import os

import pytest

collect_ignore_glob = ["*"] if os.name == "nt" else []

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Code Review Sage does not run on Windows yet (see sage_lib/discovery.py)",
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
