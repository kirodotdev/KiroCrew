"""A crashed isolation probe must surface as a sandbox failure, never as
"push is not disabled".

Issue #8151: on a host whose LSM kills the namespace-sandbox launcher (AppArmor
unprivileged-userns restriction), the ``git remote``/``config`` probe exits
nonzero with the launcher's traceback on stderr. ``_push_disabled`` fail-closed
that exit into ``False`` and the run-start route reported the 409
"the clone's push is not disabled — re-run repository setup" for a clone whose
remotes were never read. These tests pin the distinction:

* an unambiguous launcher signature on stderr + nonzero exit →
  :class:`IsolationProbeError` naming the sandbox failure (still refuses to
  start — the surfaced REASON is what changes);
* every other nonzero exit keeps its existing fail-closed meaning — git's own
  exit 1 for an absent config key, a launcher WARNING that coexists with a
  genuine git exit code, an ambiguous fatal, and (the Opus finding on this
  branch) a git fatal that merely ECHOES a clone path containing the launcher
  filename substring, which a repository named ``kirocrew_sandbox_x`` can put
  there legally;
* tuple-returning entry points (``setup_safe_clone``, ``list_clone_branches``,
  ``checkout_branch``) convert the raise into their error string instead of
  letting it escape a worker thread as a 500.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
    RepoIsolation,
)

#: The reported crash verbatim (issue #8151): a real Python traceback naming the
#: launcher's own script file — the frame line plus the banner are what the
#: structural matcher requires.
_LAUNCHER_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/home/user/.config/kirocrew/run/kirocrew_sandbox_ab12cd.py", '
    "line 293, in main\n"
    "    import platform as _plat\n"
    "ModuleNotFoundError: No module named 'platform'\n"
)

_PUSH_ISOLATION_MESSAGE = "push is not disabled"


def _proc(returncode: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def probe_result(monkeypatch: pytest.MonkeyPatch):
    """Route ``clone_setup``'s git probe to a canned result."""

    holder: dict = {"proc": _proc(0, stdout="DISABLED_NO_PUSH\n")}

    def _fake_run(*_args, **_kwargs):
        return holder["proc"]

    monkeypatch.setattr(clone_setup.subprocess, "run", _fake_run)
    return holder


class TestProbeCrashShape:
    def test_launcher_traceback_raises_probe_error(self, probe_result: dict) -> None:
        probe_result["proc"] = _proc(1, stderr=_LAUNCHER_TRACEBACK)
        with pytest.raises(clone_setup.IsolationProbeError) as excinfo:
            clone_setup._push_disabled(Path("/nonexistent/clone"))
        message = str(excinfo.value)
        assert "sandbox launcher" in message
        assert "ModuleNotFoundError: No module named 'platform'" in message
        # The whole point: the surfaced error must NOT be the push-isolation shape.
        assert _PUSH_ISOLATION_MESSAGE not in message

    def test_launcher_blocked_message_raises_probe_error(self, probe_result: dict) -> None:
        probe_result["proc"] = _proc(
            1, stderr="sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned -1)"
        )
        with pytest.raises(clone_setup.IsolationProbeError):
            clone_setup._push_disabled(Path("/nonexistent/clone"))

    def test_absent_key_is_still_not_a_crash(self, probe_result: dict) -> None:
        """git's own exit 1 (config key absent, empty stderr) keeps meaning []."""
        probe_result["proc"] = _proc(1)
        assert clone_setup._origin_urls(Path("/nonexistent/clone"), push=True) == []
        assert clone_setup._push_disabled(Path("/nonexistent/clone")) is False

    def test_launcher_warning_does_not_reclassify_git_exit(self, probe_result: dict) -> None:
        """The launcher warns and still runs the command, so a WARNING line can
        coexist with git's own exit code and must keep git's meaning."""
        probe_result["proc"] = _proc(
            1, stderr="sandbox: WARNING — cannot read /home/user/.aws/config (denied)"
        )
        assert clone_setup._origin_urls(Path("/nonexistent/clone"), push=True) == []

    def test_ambiguous_git_error_stays_fail_closed(self, probe_result: dict) -> None:
        probe_result["proc"] = _proc(128, stderr="fatal: not a git repository")
        assert clone_setup._origin_urls(Path("/nonexistent/clone"), push=True) is None
        assert clone_setup._push_disabled(Path("/nonexistent/clone")) is False

    def test_a_repo_named_like_the_launcher_cannot_spoof_the_signature(
        self, probe_result: dict
    ) -> None:
        """Repository-influenced text must not satisfy the launcher markers.

        A repo may legally be NAMED ``kirocrew_sandbox_x`` (the owner/repo
        charset admits ``_``), which puts the launcher-filename substring into
        the clone PATH that git echoes on path-printing fatals. That must stay
        an AMBIGUOUS error (fail closed, ``None``), never a launcher crash —
        otherwise a repository name could suppress clone retirement on the
        driver path. Raised by the Opus review of this branch.
        """
        probe_result["proc"] = _proc(
            128,
            stderr=(
                "fatal: bad config line 1 in file " "/scratch/owner--kirocrew_sandbox_x/.git/config"
            ),
        )
        assert clone_setup._origin_urls(Path("/nonexistent/clone"), push=True) is None
        assert clone_setup._push_disabled(Path("/nonexistent/clone")) is False

    def test_a_mid_line_prefix_does_not_reclassify(self, probe_result: dict) -> None:
        """The refusal prefixes only count at line START — text merely
        containing them (an echoed value, a wrapped message) stays git's own."""
        probe_result["proc"] = _proc(
            128, stderr="fatal: unexpected value 'sandbox: BLOCKED' in config"
        )
        assert clone_setup._origin_urls(Path("/nonexistent/clone"), push=True) is None

    def test_repo_isolation_propagates_probe_error(
        self, probe_result: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``RepoIsolation.push_disabled`` (the run-start gate) lets the typed
        error reach the route handler, which maps it to the distinct
        ``sandbox_launcher_failed`` code — that is what replaces the misleading
        409 text."""
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _repo: True)
        probe_result["proc"] = _proc(1, stderr=_LAUNCHER_TRACEBACK)
        isolation = RepoIsolation(clone_path=Path("/nonexistent/clone"))
        with pytest.raises(clone_setup.IsolationProbeError):
            isolation.push_disabled()
        assert issubclass(clone_setup.IsolationProbeError, RuntimeError)


class TestTupleEntryPointsStaySoft:
    """(result, err) / (ok, note) surfaces must not leak the raise."""

    def test_setup_safe_clone_returns_error_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args, **_kwargs):
            raise clone_setup.IsolationProbeError("ModuleNotFoundError: No module named 'platform'")

        monkeypatch.setattr(clone_setup, "_setup_safe_clone", _boom)
        result, err = clone_setup.setup_safe_clone(
            "https://github.com/o/r", Path("/nonexistent/scratch")
        )
        assert result == {}
        assert "sandbox launcher" in err
        assert _PUSH_ISOLATION_MESSAGE not in err

    def test_list_clone_branches_returns_error_string(
        self, probe_result: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _repo: True)
        probe_result["proc"] = _proc(1, stderr=_LAUNCHER_TRACEBACK)
        branches, err = clone_setup.list_clone_branches(tmp_path)
        assert branches == []
        assert "sandbox launcher" in err

    def test_checkout_branch_returns_error_string(
        self, probe_result: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _repo: True)
        probe_result["proc"] = _proc(1, stderr=_LAUNCHER_TRACEBACK)
        ok, note = clone_setup.checkout_branch(tmp_path, "main")
        assert ok is False
        assert "sandbox launcher" in note
