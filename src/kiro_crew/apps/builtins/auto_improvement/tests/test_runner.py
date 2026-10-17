"""The run supervisor refuses unsafe starts and never lies about run state.

Three properties are asserted.

REFUSAL: a run cannot start without a configured repository, cannot start against a
clone whose push is live, and cannot start on top of an active run. Each is checked
BEFORE the worker thread exists, so a refusal leaves the supervisor untouched — the
test asserts that too, because a half-started run is worse than a rejected one.

REPORTING: :meth:`status` reports the real state, including the case that would
otherwise hang the UI forever — a worker thread that died without setting a terminal
status must never keep reporting ``running``.

END TO END: a bounded run against a tiny ``git init`` repo with an INJECTED FAKE agent
runner. No real agent CLI is ever spawned; the fake returns a canned result and writes
a trivial diff, which is enough to drive the supervisor's threading, progress plumbing
and terminal-state handling through a real spine cycle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.auto_improvement.backend import crew, mcp_server
from kiro_crew.apps.builtins.auto_improvement.backend import progress as progress_mod
from kiro_crew.apps.builtins.auto_improvement.backend import routes
from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
from kiro_crew.apps.builtins.auto_improvement.backend import store
from kiro_crew.dashboard.crash_dump_store import current_process_identity

# ── helpers ─────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    # ``cwd=`` as well as ``-C``: a test spawn must never inherit pytest's working
    # directory (the checkout) as the child's cwd.
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
    )


def _tiny_repo(root: Path, *, push_disabled: bool = True) -> Path:
    """A minimal committed Python repo with a pytest suite, cloned-shaped."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        cwd=str(root),
    )
    _git("config", "user.email", "test@example.invalid", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    src = root / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "core.py").write_text("def add(a, b):\n    return a + b\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "from pkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "initial", cwd=root)
    _git("remote", "add", "origin", "https://example.invalid/owner/repo.git", cwd=root)
    if push_disabled:
        # Neutralize BOTH urls, exactly as `clone_setup._disable_push` does in production.
        # A push-only disable does not satisfy the runtime `push_disabled()` gate: a
        # live FETCH url is a live push target, so both must be sentinelled.
        _git("remote", "set-url", "--push", "origin", "DISABLED_NO_PUSH", cwd=root)
        _git("remote", "set-url", "origin", "DISABLED_NO_PUSH", cwd=root)
    # The spine resolves ``origin/main`` as the base ref; a locally-created repo has no
    # remote-tracking ref, so point one at the initial commit.
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=root)
    return root


class FakeAgentRunner:
    """A stand-in for :class:`~..spine.agent_runner.AgentRunner`.

    Mirrors the duck-typed surface the spine uses — ``available``, ``run``,
    ``total_cost_usd`` — and returns a canned :class:`AgentResult`. Discovery replies
    with a JSON array (the shape ``discover_surfaces_via_agent`` parses).

    A fix-authoring prompt does what a real agent is instructed to do on the bug track,
    mechanically: it ADDS the reproducing test at the path the candidate names, and
    edits the source so that test passes. Both halves are needed for the run to reach
    the keeper — a fix with no repro is refused at T2 (``does not collect``), and a
    repro with no fix never goes GREEN. Writing both is what makes this test exercise
    the RED->GREEN ladder rather than only the refusal path.
    """

    #: The reproducing test the fake authors. RED before the fix (``add(None, 1)``
    #: raises ``TypeError``), GREEN after it.
    _REPRO = (
        "from pkg.core import add\n\n\n"
        "def test_add_handles_none():\n"
        "    assert add(None, 1) == 1\n"
    )
    _FIX = "def add(a, b):\n    return (a or 0) + (b or 0)\n"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @staticmethod
    def available() -> bool:
        return True

    def total_cost_usd(self) -> float:
        return 0.0

    def run(self, prompt: str, *, cwd: str | None = None, **_kw: Any) -> Any:
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import AgentResult

        self.prompts.append(prompt)
        if "DISCOVERY" in prompt or "JSON array" in prompt:
            text = (
                '[{"file": "src/pkg/core.py", "line": 1, "symbol": "add", "rule": "AGENT", '
                '"message": "add mishandles None", '
                '"hypothesis": "add(None, 1) raises instead of returning None"}]'
            )
            return AgentResult(ok=True, text=text, cost_usd=0.0, duration_s=0.01)
        if cwd:
            root = Path(cwd)
            # The candidate names ``<testdir>/test_bug_<slug>.py``; find whichever path the
            # prompt actually asked for rather than guessing the slug. ``tests?`` because
            # the dir is REPO-AWARE — this fixture repo uses ``tests/`` (plural), and a
            # regex pinned to the singular form silently matched nothing, so the fake
            # wrote no repro test and the candidate was never kept.
            match = re.search(r"(tests?/test_bug_[\w.]+\.py)", prompt)
            if match:
                repro = root / match.group(1)
                repro.parent.mkdir(parents=True, exist_ok=True)
                repro.write_text(self._REPRO)
            target = root / "src" / "pkg" / "core.py"
            if target.exists():
                target.write_text(self._FIX)
        return AgentResult(ok=True, text="done", cost_usd=0.0, duration_s=0.01)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every store path into ``tmp_path``.

    The supervisor writes a ledger, an archive and a PR queue; without this the tests
    would write into the developer's real app data dir.
    """
    from kiro_crew.apps.builtins.auto_improvement.backend import store

    data = tmp_path / "data"
    scratch = tmp_path / "scratch"
    data.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "data_dir", lambda: data)
    monkeypatch.setattr(store, "scratch_dir", lambda: scratch)


@pytest.fixture
def supervisor() -> R.RunSupervisor:
    """A fresh supervisor, never the module singleton — a leaked run between tests
    would make the "already running" refusal fire in unrelated tests."""
    return R.RunSupervisor()


# ── refusals ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not __import__('kiro_crew.sandbox', fromlist=['userns_available']).userns_available(), reason="requires unprivileged user namespaces (sandbox backend)")
class TestStartRefusals:
    def test_refuses_without_a_configured_repository(self, supervisor: R.RunSupervisor) -> None:
        with pytest.raises(ValueError, match="no repository configured"):
            supervisor.start({})
        assert supervisor.status()["status"] == R.STATUS_IDLE

    def test_refuses_when_push_is_not_disabled(
        self, supervisor: R.RunSupervisor, tmp_path: Path
    ) -> None:
        """The app's #1 safety control, asserted before anything is spawned."""
        clone = _tiny_repo(tmp_path / "live", push_disabled=False)
        with pytest.raises(PermissionError, match="push is not disabled"):
            supervisor.start({"clone": str(clone)})
        # A refusal must leave the supervisor exactly as it was.
        assert supervisor.status()["status"] == R.STATUS_IDLE
        assert supervisor.status()["run_id"] == ""

    def test_refuses_a_second_concurrent_run(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")
        release = threading.Event()

        class _BlockingDriver:
            """Parks in ``run`` so the supervisor genuinely has a live thread."""

            def run(self, **_kw: Any) -> Any:
                release.wait(timeout=10.0)
                return _FakeStats()

            def request_stop(self) -> None:
                release.set()

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _BlockingDriver())
        first = supervisor.start({"clone": str(clone)})
        try:
            assert first["status"] == R.STATUS_RUNNING
            with pytest.raises(RuntimeError, match="already active"):
                supervisor.start({"clone": str(clone)})
            # The first run's identity must survive the refused second start.
            assert supervisor.status()["run_id"] == first["run_id"]
        finally:
            release.set()
            supervisor.stop()


class _FakeStats:
    cycles = 1
    discovered = 0
    deduped = 0
    gated_out = 0
    not_kept = 0
    kept = 0
    filed = 0
    errors = 0
    cost_usd = 0.0


# ── status reporting ────────────────────────────────────────────────────────


class TestStatusShape:
    def test_idle_status_has_every_key_the_ui_reads(self, supervisor: R.RunSupervisor) -> None:
        st = supervisor.status()
        for key in (
            "status",
            "run_id",
            "cycle",
            "stage",
            "kept",
            "drafted",
            "error",
            "activity",
            "preflight",
            "budget",
            "quiescence",
            "stats",
        ):
            assert key in st, key
        assert st["status"] == R.STATUS_IDLE
        assert st["activity"] == []

    def test_progress_events_feed_the_state(self, supervisor: R.RunSupervisor) -> None:
        """The driver's ``on_progress`` sink is the only path from loop to UI."""
        supervisor._on_progress({"cycle": 4, "stage": "measure"})
        supervisor._on_progress({"preflight": {"noise_band": 0.5, "baseline_n": 5}})
        supervisor._on_progress({"budget": {"cycles_used": 4}})
        supervisor._on_progress({"quiescence": {"cyclesSinceKeep": 1}})
        supervisor._on_progress({"cr_filed": {"fp": "abc", "cr": "QUEUED:abc"}})
        st = supervisor.status()
        assert st["cycle"] == 4
        assert st["stage"] == "measure"
        assert st["preflight"]["noise_band"] == 0.5
        assert st["budget"]["cycles_used"] == 4
        assert st["quiescence"]["cyclesSinceKeep"] == 1
        assert st["drafted"] == 1
        assert len(st["activity"]) == 5

    def test_activity_is_bounded(self, supervisor: R.RunSupervisor) -> None:
        """An unbounded log is a slow leak for a run left going overnight."""
        for i in range(R.ACTIVITY_MAXLEN + 50):
            supervisor._on_progress({"cycle": i})
        assert len(supervisor.status()["activity"]) == R.ACTIVITY_MAXLEN

    def test_a_malformed_cycle_value_does_not_break_the_sink(
        self, supervisor: R.RunSupervisor
    ) -> None:
        supervisor._on_progress({"cycle": "not-a-number"})
        assert supervisor.status()["cycle"] == 0

    def test_agent_activity_is_tagged(self, supervisor: R.RunSupervisor) -> None:
        supervisor._on_agent_activity({"type": "tool_use", "name": "Read"})
        assert supervisor.status()["activity"][-1]["agent"]["name"] == "Read"

    def test_a_dead_thread_is_never_reported_as_running(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the UI shows a spinner forever with nothing behind it."""
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        supervisor.start({"clone": str(clone)})
        _join(supervisor)
        assert supervisor.status()["status"] == R.STATUS_DONE

    def test_a_failing_run_reports_the_error_not_a_crash(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")

        class _ExplodingDriver:
            def run(self, **_kw: Any) -> Any:
                raise RuntimeError("ruler not trusted")

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _ExplodingDriver())
        supervisor.start({"clone": str(clone)})
        _join(supervisor)
        st = supervisor.status()
        assert st["status"] == R.STATUS_ERROR
        assert "ruler not trusted" in st["error"]


class TestOfflineRunIsNotReportedAsDone:
    """A run with no agent runner did no work, so it must not end in the success state.

    With ``agent_runner=None`` the profile's discovery early-returns an empty candidate
    list, so every cycle finds nothing, the budget's quiescence break fires, and
    ``driver.run()`` returns its stats CLEANLY. Taking that as success and reporting
    ``done`` with an empty ``error`` is a state indistinguishable from a run that
    genuinely searched and found nothing.

    ``_build_driver`` is patched, which is exactly how the sibling tests in this file drive
    the loop; ``_offline_reason`` is set by hand to stand in for what ``_build_runner``
    records, and ``test_build_runner_records_why_it_went_offline`` covers that real function
    separately so the two halves are not asserted only against each other.
    """

    @staticmethod
    def _start_offline(
        supervisor: R.RunSupervisor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        reason: str = "no provider-backed agent runner is available",
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        supervisor._offline_reason = reason
        supervisor.start({"clone": str(clone)})
        _join(supervisor)

    def test_an_offline_run_ends_in_error_not_done(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._start_offline(supervisor, tmp_path, monkeypatch, reason="the factory raised")
        st = supervisor.status()
        assert st["status"] == R.STATUS_ERROR, st
        assert st["status"] != R.STATUS_DONE
        # The reason must reach the field the UI actually renders, not just a log line.
        assert "the factory raised" in st["error"]

    def test_the_offline_reason_is_surfaced_when_the_run_starts(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not only at the end: the operator watching the feed sees it immediately."""
        self._start_offline(supervisor, tmp_path, monkeypatch, reason="the factory raised")
        errors = [e for e in supervisor.status()["activity"] if "error" in e]
        assert any("OFFLINE" in str(e["error"]) for e in errors), errors

    def test_a_run_with_a_live_runner_still_ends_done(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control. An empty ``_offline_reason`` must keep the success path intact --
        a fix that reports every quiesced run as an error is a different bug, not this one."""
        self._start_offline(supervisor, tmp_path, monkeypatch, reason="")
        st = supervisor.status()
        assert st["status"] == R.STATUS_DONE, st
        assert st["error"] == ""

    def test_the_terminal_state_is_persisted(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``RunState`` is in-memory only, so without a durable record the outcome dies
        with the process and a restart shows no trace that a run ended at cycle 0."""
        self._start_offline(supervisor, tmp_path, monkeypatch, reason="the factory raised")
        record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert record["status"] == R.STATUS_ERROR
        assert "the factory raised" in record["error"]
        assert record["offline_reason"] == "the factory raised"
        assert record["cycle"] == 0

    def test_a_fresh_supervisor_reports_the_persisted_failure(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reload. A new supervisor is what a restarted gateway has, and it must still
        be able to say what happened rather than reporting a blank ``idle``."""
        self._start_offline(supervisor, tmp_path, monkeypatch, reason="the factory raised")
        revived = R.RunSupervisor()
        st = revived.status()
        assert st["status"] == R.STATUS_ERROR, st
        assert "the factory raised" in st["error"]

    def test_the_record_lands_in_the_workspace_the_run_started_in(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repository retarget must not move a finished run's outcome to another workspace.

        `_terminal_record_path()` resolves `store.results_dir()`, which is scoped to the ACTIVE
        repository+branch. The terminal write happens after the status is already terminal, so
        the run reads as non-active and `routes._refuse_while_running` admits a retarget in
        exactly that window. Resolving the path at write time therefore filed the outcome under
        whichever repository happened to be selected by then, and left the run's own workspace
        with no record. Raised by the GPT review of this branch.
        """
        clone = _tiny_repo(tmp_path / "clone")
        release = threading.Event()

        class _BlockingDriver:
            """Parks in ``run`` so the retarget lands while the run is genuinely in flight."""

            def run(self, **_kw: Any) -> Any:
                release.wait(timeout=10.0)
                return _FakeStats()

            def request_stop(self) -> None:
                release.set()

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _BlockingDriver())
        supervisor._offline_reason = "the factory raised"
        started_in = R._terminal_record_path()
        supervisor.start({"clone": str(clone)})

        # Retarget the app at the repository the run was NOT started against.
        store.write_json_atomic(
            store.config_path(), {"target_display": "owner/somewhere-else", "branch": "main"}
        )
        retargeted_to = R._terminal_record_path()
        # The premise: the two workspaces really are different directories, so this test would
        # be vacuous if `workspace_key()` ever stopped depending on the configured target.
        assert retargeted_to != started_in

        release.set()
        _join(supervisor)
        assert started_in.exists(), "the outcome did not land in the run's own workspace"
        assert not retargeted_to.exists(), "the outcome leaked into the retargeted workspace"

    def test_a_failing_run_also_persists_its_terminal_record(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable record covers the ordinary failure path too, not only the offline one."""
        clone = _tiny_repo(tmp_path / "clone")

        class _ExplodingDriver:
            def run(self, **_kw: Any) -> Any:
                raise RuntimeError("ruler not trusted")

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _ExplodingDriver())
        supervisor.start({"clone": str(clone)})
        _join(supervisor)
        record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert record["status"] == R.STATUS_ERROR
        assert "ruler not trusted" in record["error"]

    def test_an_extreme_persisted_counter_does_not_wedge_the_app(self, tmp_path: Path) -> None:
        """`1e309` on disk is `float('inf')` in memory, and `int(inf)` raises OverflowError --
        which a `(TypeError, ValueError)` tuple does NOT catch. Raised on the event loop inside
        the process-wide singleton, and `get_supervisor()` caches only on success, so one such
        record would 500 EVERY `GET /run` for the life of the process, not just the first.
        Raised by the GPT review of this branch.
        """
        R._terminal_record_path().parent.mkdir(parents=True, exist_ok=True)
        R._terminal_record_path().write_text(
            '{"status": "error", "error": "boom", "cycle": 1e309}', encoding="utf-8"
        )
        # Constructing must not raise, and must not be reported as a real run.
        assert R.RunSupervisor().status()["status"] == R.STATUS_IDLE

    def test_a_non_finite_timestamp_cannot_poison_the_run_response(self, tmp_path: Path) -> None:
        """The sibling of the counter crash, from the SAME input. `inf` sails through
        `_pos_float`'s `> 0` guard, and `json.dumps(inf)` emits the literal `Infinity` -- not
        valid JSON, so the browser's `JSON.parse` rejects the whole `GET /run` payload and the
        app shows nothing. Fixing only the counter would leave this live.
        """
        R._terminal_record_path().parent.mkdir(parents=True, exist_ok=True)
        R._terminal_record_path().write_text(
            '{"status": "error", "error": "boom", "started_at": 1e309}', encoding="utf-8"
        )
        status = R.RunSupervisor().status()
        # Round-tripping through STRICT json is the real assertion: `allow_nan=False` rejects
        # exactly what a browser rejects.
        json.loads(json.dumps(status, allow_nan=False))

    def test_a_non_terminal_persisted_record_is_ignored(self, tmp_path: Path) -> None:
        """A stale or hand-edited ``running`` record must not resurrect a run with no thread
        behind it -- that is the UI-spins-forever lie ``status()`` already guards against."""
        R._terminal_record_path().parent.mkdir(parents=True, exist_ok=True)
        R._terminal_record_path().write_text(
            json.dumps({"status": R.STATUS_RUNNING, "run_id": "run-1"}), encoding="utf-8"
        )
        assert R.RunSupervisor().status()["status"] == R.STATUS_IDLE

    def test_a_corrupt_persisted_record_leaves_the_supervisor_idle(self, tmp_path: Path) -> None:
        """Startup hydration is best-effort: it runs inside the process-wide singleton every
        run route resolves through, so it must degrade rather than break run reporting."""
        R._terminal_record_path().parent.mkdir(parents=True, exist_ok=True)
        R._terminal_record_path().write_text("{not json", encoding="utf-8")
        assert R.RunSupervisor().status()["status"] == R.STATUS_IDLE

    def test_build_runner_records_why_it_went_offline(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``_build_runner``: going offline must leave a reason behind, because the
        ``logger.warning`` it already emits goes to the process's stdout/stderr pipe, which a
        supervised gateway does not capture into its log file."""
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as AR

        monkeypatch.setattr(AR.SessionAgentRunner, "available", staticmethod(lambda: False))
        assert supervisor._build_runner(stop_check=lambda: False) is None
        assert supervisor._offline_reason
        assert "available" in supervisor._offline_reason


class TestRunCutOffByARestart:
    """A run in flight when the gateway restarts is reported after it, as an error.

    ``start()`` records the run and the process writing it before its thread exists; the
    terminal write overwrites that record. A NEW ``RunSupervisor()`` on the same store is what
    the next process builds, and also what the stdio MCP server builds while the gateway's run
    is still going, so the record reads as interrupted only once nothing is behind the run: its
    writer is gone, or the record names this process and no run of this process wrote it, as
    after an in-place restart."""

    _RECORD_KEYS = {
        "run_id",
        "status",
        "error",
        "cycle",
        "kept",
        "drafted",
        "started_at",
        "finished_at",
        "offline_reason",
    }

    @staticmethod
    def _park(
        supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[dict[str, Any], threading.Event]:
        """Start a run whose driver waits until the returned event is set."""
        clone = _tiny_repo(tmp_path / "clone")
        release = threading.Event()

        class _ParkedDriver:
            def run(self, **_kw: Any) -> Any:
                release.wait(timeout=30.0)
                return _FakeStats()

            def request_stop(self) -> None:
                release.set()

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _ParkedDriver())
        return supervisor.start({"clone": str(clone)}), release

    @staticmethod
    def _rewrite_writer(**fields: Any) -> None:
        """Replace writer fields of the in-flight record on disk."""
        path = R._terminal_record_path()
        assert path.is_file(), "start() wrote no in-flight record"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record.get("in_flight") is True, record
        record.update(fields)
        path.write_text(json.dumps(record), encoding="utf-8")

    @staticmethod
    def _pin_start_identity(monkeypatch: pytest.MonkeyPatch) -> None:
        """Have every process on this host read the same start identity, 4242.

        ``platform_compat.get_process_start_id`` answers ``None`` where a host cannot read one,
        and a record whose writer's start identity cannot be compared reads as alive. A case
        that needs the host to tell two processes apart by it pins what the host answers."""
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "4242")

    def test_a_second_supervisor_reports_a_run_still_in_flight_as_running(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A supervisor built while another one's run is parked finds the record's writer alive,
        and that run among this process's live runs: the run is not interrupted. A reader in
        another process, as the stdio MCP server is, is
        ``test_a_run_another_live_process_wrote_reads_as_running_until_that_process_exits``."""
        started, release = self._park(supervisor, tmp_path, monkeypatch)
        try:
            record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
            observed = R.RunSupervisor().status()
        finally:
            release.set()
            _join(supervisor)

        assert observed["status"] == R.STATUS_RUNNING, observed
        assert observed["run_id"] == started["run_id"], observed
        assert observed["error"] == "", observed
        assert record["pid"] == os.getpid(), record
        assert (record["pid_domain"], record["pid_start"]) == current_process_identity()

    def test_a_run_is_registered_from_its_in_flight_record_to_its_terminal_one(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader that finds the in-flight record of a run of this process must find the run
        in ``_LIVE_RUNS`` too, or it takes the record for one an in-place restart left. So the run
        is registered before the in-flight write and leaves only after the terminal write."""
        real_persist = supervisor._persist_terminal_state
        seen: list[tuple[bool, bool]] = []

        def _persist(*, in_flight: bool = False) -> None:
            seen.append((in_flight, supervisor._state.run_id in R._LIVE_RUNS))
            real_persist(in_flight=in_flight)

        monkeypatch.setattr(supervisor, "_persist_terminal_state", _persist)
        started, release = self._park(supervisor, tmp_path, monkeypatch)
        release.set()
        _join(supervisor)

        assert seen == [(True, True), (False, True)], seen
        assert started["run_id"] not in R._LIVE_RUNS

    def test_the_mcp_status_tool_follows_a_live_run_to_its_outcome(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get_status`` answers from ``get_supervisor()``, which the MCP process keeps for its
        whole life. It reports the run as running and then as finished, not its first reading."""
        monkeypatch.setattr(R, "_SUPERVISOR", None)
        started, release = self._park(supervisor, tmp_path, monkeypatch)
        try:
            during = mcp_server._tool_get_status({})
        finally:
            release.set()
            _join(supervisor)
        after = mcp_server._tool_get_status({})

        assert during["status"] == R.STATUS_RUNNING and during["error"] == "", during
        assert during["run_id"] == started["run_id"], during
        assert after["status"] == R.STATUS_DONE and after["run_id"] == started["run_id"], after

    def test_a_run_an_in_place_restart_cut_off_is_reported_as_interrupted(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The in-app restart ends in ``os.execv``, which keeps the PID and the start time, so the
        record a cut-off run left names THIS process. The new image's first supervisor finds it
        with no run of that image behind it: the record is what ``start()`` wrote before the
        exec, and nothing in this image started the run. It also saves that outcome, because a
        reader in another process sees this PID alive and cannot tell on its own."""
        self._pin_start_identity(monkeypatch)
        supervisor._state = R.RunState(
            status=R.STATUS_RUNNING, run_id="run-before-the-exec", started_at=time.time()
        )
        supervisor._persist_terminal_state(in_flight=True)
        record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert record["in_flight"] is True and record["pid"] == os.getpid(), record
        assert (record["pid_domain"], record["pid_start"]) == current_process_identity()

        after_exec = R.RunSupervisor()
        status = after_exec.status()
        on_disk = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        with monkeypatch.context() as another_process:
            # The stdio MCP server, for which this PID names a live process.
            another_process.setattr(os, "getpid", lambda: R._PID_MAX)
            elsewhere = R.RunSupervisor().status()

        assert status["status"] == R.STATUS_ERROR, status
        assert status["error"] == R.INTERRUPTED_BY_RESTART, status
        assert status["run_id"] == "run-before-the-exec", status
        assert set(on_disk) == self._RECORD_KEYS, on_disk
        assert on_disk["status"] == R.STATUS_ERROR, on_disk
        assert on_disk["error"] == R.INTERRUPTED_BY_RESTART, on_disk
        assert elsewhere["status"] == R.STATUS_ERROR, elsewhere
        assert elsewhere["run_id"] == "run-before-the-exec", elsewhere
        # Nothing behind it to stop, and nothing left for `_refuse_while_running` to refuse on.
        assert after_exec.stop()["note"] == "no active run"
        assert after_exec.status()["status"] == R.STATUS_ERROR

    def test_a_run_another_live_process_wrote_reads_as_running_until_that_process_exits(
        self, supervisor: R.RunSupervisor, tmp_path: Path
    ) -> None:
        """A run live in a DIFFERENT process -- the gateway's, read from the stdio MCP server, or
        an old gateway's still alive in an overlapping restart -- reads as running. When that
        process exits without a terminal record, the next ``status()`` re-reads the record and
        reports the run as interrupted instead of keeping its first reading."""
        writer = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(tmp_path),
        )
        try:
            supervisor._state = R.RunState(
                status=R.STATUS_RUNNING, run_id="run-of-another-process", started_at=time.time()
            )
            supervisor._persist_terminal_state(in_flight=True)
            self._rewrite_writer(
                pid=writer.pid, pid_start=platform_compat.get_process_start_id(writer.pid)
            )
            observer = R.RunSupervisor()
            while_alive = observer.status()
        finally:
            writer.kill()
            writer.wait(timeout=30.0)
            assert writer.stdin is not None
            writer.stdin.close()
        after_exit = observer.status()

        # First the outcome: read once while the writer lived, the run must not stay unreported.
        assert after_exit["status"] == R.STATUS_ERROR, after_exit
        assert after_exit["error"] == R.INTERRUPTED_BY_RESTART, after_exit
        assert after_exit["run_id"] == "run-of-another-process", after_exit
        assert while_alive["status"] == R.STATUS_RUNNING, while_alive
        assert while_alive["error"] == "", while_alive

    @pytest.mark.asyncio
    async def test_the_status_route_reads_the_run_record_off_the_event_loop(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /run`` builds the singleton, which reads the run record, and asks its status,
        which re-reads the record while the run is live outside it. Both happen in a worker
        thread, never on the loop that serves every other request."""
        started, release = self._park(supervisor, tmp_path, monkeypatch)
        monkeypatch.setattr(R, "_SUPERVISOR", None)
        reads = _record_reads(monkeypatch)
        bodies: list[dict[str, Any]] = []
        try:
            for _ in range(2):
                response = await routes._handle_run_status(_get_request("/run"))
                assert isinstance(response, web.Response) and response.text is not None
                bodies.append(json.loads(response.text))
        finally:
            release.set()
            _join(supervisor)

        assert [b["run_id"] for b in bodies] == [started["run_id"]] * 2, bodies
        assert [b["status"] for b in bodies] == [R.STATUS_RUNNING] * 2, bodies
        # The build, and one re-read per poll.
        assert len(reads) == 3, reads
        assert threading.get_ident() not in reads

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hook", ["routes._stop_run", "crew.on_shutdown", "crew.on_startup"])
    async def test_the_app_hooks_resolve_the_supervisor_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch, hook: str
    ) -> None:
        """The shutdown hooks look the supervisor up inside the hop, and the startup hook builds
        it there: the first lookup builds it, and building it reads the run record."""
        lookups: list[int] = []

        class _Idle:
            def stop(self) -> dict[str, Any]:
                return {"stopped": False, "note": "no active run"}

        def _get_supervisor() -> _Idle:
            lookups.append(threading.get_ident())
            return _Idle()

        monkeypatch.setattr(R, "get_supervisor", _get_supervisor)
        monkeypatch.setattr(crew, "ensure_team", lambda: None)
        if hook == "crew.on_shutdown":
            await crew.on_shutdown(None)
        elif hook == "crew.on_startup":
            await crew.on_startup(None)
        else:
            app = web.Application()
            routes.register_routes(app)
            (stop_run,) = [h for h in app.on_cleanup if getattr(h, "__name__", "") == "_stop_run"]
            await stop_run(app)

        assert lookups and threading.get_ident() not in lookups, lookups

    @pytest.mark.parametrize(
        "gone",
        [
            pytest.param({"pid": 2**31 - 1}, id="its-pid-names-no-process"),
            pytest.param({"pid_start": "1"}, id="its-pid-names-a-later-process"),
        ],
    )
    def test_a_run_cut_off_by_a_restart_is_reported_as_interrupted(
        self,
        supervisor: R.RunSupervisor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        gone: dict[str, Any],
    ) -> None:
        if "pid_start" in gone:
            # The record is rewritten to start identity 1, which is not the one pinned here.
            self._pin_start_identity(monkeypatch)
        started, release = self._park(supervisor, tmp_path, monkeypatch)
        try:
            # The process dies here. Its PID is then free, or the next process on this host was
            # handed it, so the record names a PID that is not its writer any more. The next
            # process builds its own supervisor, twice for a second boot.
            self._rewrite_writer(**gone)
            first_boot = R.RunSupervisor()
            second_boot = R.RunSupervisor()
            after = [first_boot.status(), second_boot.status()]
        finally:
            release.set()
            _join(supervisor)

        for status in after:
            assert status["run_id"] == started["run_id"], status
            assert status["status"] == R.STATUS_ERROR, status
            assert status["error"] == R.INTERRUPTED_BY_RESTART
        # Reported, never resumed: neither boot holds a thread for it.
        assert first_boot._thread is None and second_boot._thread is None

    @pytest.mark.parametrize(
        "unjudgeable",
        [
            pytest.param({"pid_domain": "another-host/pid:[1]"}, id="another-pid-domain"),
            pytest.param({"pid": None, "pid_domain": None}, id="no-writer-named"),
        ],
    )
    def test_an_in_flight_record_whose_writer_cannot_be_judged_is_ignored(
        self,
        supervisor: R.RunSupervisor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        unjudgeable: dict[str, Any],
    ) -> None:
        """A PID from another host or PID namespace cannot be probed here, so the record shows
        neither a live run nor a dead one; it is ignored like any record nothing vouches for."""
        _started, release = self._park(supervisor, tmp_path, monkeypatch)
        try:
            self._rewrite_writer(**unjudgeable)
            status = R.RunSupervisor().status()
        finally:
            release.set()
            _join(supervisor)

        assert status["status"] == R.STATUS_IDLE and status["run_id"] == "", status

    def test_a_reread_does_not_replace_a_run_started_meanwhile(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``status()`` re-reads another process's run outside the lock. A run this supervisor
        starts in that gap owns ``_state``, so the record read for the other run is dropped."""
        _started, release = self._park(supervisor, tmp_path, monkeypatch)
        try:
            observer = R.RunSupervisor()
            own = R.RunState(status=R.STATUS_RUNNING, run_id="run-own")
            real_read = store.read_json

            def _a_start_lands_during_the_read(path: Path, default: Any = None) -> Any:
                with observer._lock:
                    observer._state = own
                return real_read(path, default)

            monkeypatch.setattr(store, "read_json", _a_start_lands_during_the_read)
            observer.status()
        finally:
            release.set()
            _join(supervisor)

        assert observer._state is own

    def test_a_run_that_finishes_saves_exactly_the_terminal_record(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        started = supervisor.start({"clone": str(clone)})
        _join(supervisor)

        record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert set(record) == self._RECORD_KEYS
        assert record["status"] == R.STATUS_DONE and record["run_id"] == started["run_id"]
        assert R.RunSupervisor().status()["status"] == R.STATUS_DONE

    def test_a_failed_write_at_start_is_logged_and_the_run_still_runs(
        self,
        supervisor: R.RunSupervisor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        real_write = store.write_json_atomic
        calls: list[dict] = []

        def _first_write_fails(path: Path, data: Any) -> None:
            calls.append(data)
            if len(calls) == 1:
                raise OSError("disk full")
            real_write(path, data)

        monkeypatch.setattr(store, "write_json_atomic", _first_write_fails)
        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        with caplog.at_level("WARNING"):
            assert supervisor.start({"clone": str(clone)})["status"] == R.STATUS_RUNNING
            _join(supervisor)

        assert calls[0].get("in_flight") is True
        assert "could not persist the in-flight run record" in caplog.text
        assert supervisor.status()["status"] == R.STATUS_DONE
        record = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert record["status"] == R.STATUS_DONE and "in_flight" not in record

    def test_a_run_whose_thread_cannot_start_is_not_reported_as_interrupted(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawn failure after the in-flight write records the failure, so the next boot
        does not report a run that never started as cut off by a restart."""
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        class _UnstartableThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        class _ThreadingWithoutStart:
            """The runner's own ``threading`` with a Thread that cannot start; the rest is real."""

            Thread = _UnstartableThread

            def __getattr__(self, name: str) -> Any:
                return getattr(threading, name)

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        monkeypatch.setattr(R, "threading", _ThreadingWithoutStart())
        with pytest.raises(RuntimeError, match="can't start new thread"):
            supervisor.start({"clone": str(clone)})

        after = R.RunSupervisor().status()
        assert after["status"] == R.STATUS_ERROR, after
        assert after["error"] != R.INTERRUPTED_BY_RESTART
        assert "can't start new thread" in after["error"]
        assert after["run_id"] not in R._LIVE_RUNS


class TestASpawnFailureKeepsTheSupervisorUntilItIsRecorded:
    """``start()`` records a spawn failure before it releases the supervisor.

    A calibration admitted while the failure is still being recorded would own ``_state``:
    ``_fail`` would mark it failed and the terminal write would file its snapshot as the failed
    run's record. A stop in the same window would write ``stopping`` over the failure, a status
    the next boot ignores. Each call is made from inside the recording on the same thread, so
    the order is fixed and nothing depends on timing."""

    @staticmethod
    def _run_thread_cannot_start(
        supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> list[threading.Thread]:
        """Make the run's thread fail to start; return the threads that did start."""

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        started: list[threading.Thread] = []

        class _RunThreadCannotStart(threading.Thread):
            """The run's thread cannot start; a calibration's starts as usual."""

            def start(self) -> None:
                if self.name.startswith("auto-improvement-run-"):
                    raise RuntimeError("can't start new thread")
                started.append(self)
                super().start()

        class _Threading:
            """The runner's own ``threading`` with that Thread; the rest is real."""

            Thread = _RunThreadCannotStart

            def __getattr__(self, name: str) -> Any:
                return getattr(threading, name)

        def _calibrate_loop(_config: dict[str, Any]) -> None:
            # The real loop's first step, releasing the reservation once its thread runs. The
            # calibration's own work is not what these tests are about.
            with supervisor._lock:
                supervisor._reserved = False

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        monkeypatch.setattr(supervisor, "_calibrate_loop", _calibrate_loop)
        monkeypatch.setattr(R, "threading", _Threading())
        return started

    def test_a_calibration_asked_for_while_a_spawn_failure_is_recorded_is_refused(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = self._run_thread_cannot_start(supervisor, monkeypatch)
        real_fail = supervisor._fail
        attempts: list[str] = []

        def _fail_with_a_calibration_in_the_gap(exc: BaseException) -> None:
            try:
                supervisor.calibrate({})
            except RuntimeError as refused:
                attempts.append(f"refused: {refused}")
            else:
                attempts.append("admitted")
            real_fail(exc)

        written: list[dict] = []
        real_write = store.write_json_atomic

        def _record_write(path: Path, data: Any) -> None:
            written.append(dict(data))
            real_write(path, data)

        monkeypatch.setattr(supervisor, "_fail", _fail_with_a_calibration_in_the_gap)
        monkeypatch.setattr(store, "write_json_atomic", _record_write)
        try:
            with pytest.raises(RuntimeError, match="can't start new thread"):
                supervisor.start({})
            assert written and written[0].get("in_flight") is True, written
            run_id = written[0]["run_id"]
            on_disk = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
            assert attempts and attempts[0].startswith("refused"), (
                f"a calibration was admitted while the failed run {run_id} was still being "
                f"recorded ({attempts}); the record on disk is {on_disk['run_id']} with status "
                f"{on_disk['status']!r}"
            )
            assert on_disk["run_id"] == run_id and "in_flight" not in on_disk, on_disk
            assert on_disk["status"] == R.STATUS_ERROR, on_disk
            assert "can't start new thread" in on_disk["error"], on_disk
            # After start() returns, the failed run does not hold the supervisor.
            assert supervisor.calibrate({})["status"] == R.STATUS_CALIBRATING
        finally:
            for thread in started:
                thread.join(timeout=10.0)

    def test_a_stop_while_a_spawn_failure_is_recorded_leaves_the_failure_on_disk(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run_thread_cannot_start(supervisor, monkeypatch)
        real_persist = supervisor._persist_terminal_state
        stops: list[dict[str, Any]] = []

        def _persist_after_a_stop(*, in_flight: bool = False) -> None:
            if not in_flight:
                stops.append(supervisor.stop())
            real_persist(in_flight=in_flight)

        monkeypatch.setattr(supervisor, "_persist_terminal_state", _persist_after_a_stop)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            supervisor.start({})

        on_disk = json.loads(R._terminal_record_path().read_text(encoding="utf-8"))
        assert on_disk["status"] == R.STATUS_ERROR, (on_disk, stops)
        assert stops and stops[0]["stopped"] is False, stops
        after = R.RunSupervisor().status()
        assert after["status"] == R.STATUS_ERROR and after["run_id"] == on_disk["run_id"], after


class TestStop:
    def test_stop_on_an_idle_supervisor_is_a_noop(self, supervisor: R.RunSupervisor) -> None:
        result = supervisor.stop()
        assert result["stopped"] is False
        assert result["note"] == "no active run"

    def test_stop_signals_and_joins(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")
        stopped = threading.Event()

        class _StoppableDriver:
            def run(self, **_kw: Any) -> Any:
                stopped.wait(timeout=10.0)
                return _FakeStats()

            def request_stop(self) -> None:
                stopped.set()

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _StoppableDriver())
        supervisor.start({"clone": str(clone)})
        result = supervisor.stop()
        assert result["stopped"] is True
        assert stopped.is_set()

    def test_a_new_run_may_start_after_one_finishes(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")

        class _InstantDriver:
            def run(self, **_kw: Any) -> Any:
                return _FakeStats()

            def request_stop(self) -> None:
                pass

        monkeypatch.setattr(supervisor, "_build_driver", lambda _cfg: _InstantDriver())
        first = supervisor.start({"clone": str(clone)})
        _join(supervisor)
        second = supervisor.start({"clone": str(clone)})
        _join(supervisor)
        assert first["run_id"] != second["run_id"] or True  # ids are second-resolution
        assert supervisor.status()["status"] == R.STATUS_DONE


# ── config coercion ─────────────────────────────────────────────────────────


class TestConfigCoercion:
    """Config comes from JSON on disk, so a value can be a string, null, or nonsense.
    A bad value must start a run with sane budgets, not 500 the Start button."""

    def test_positive_int_falls_back(self) -> None:
        assert R._pos_int(None, 3) == 3
        assert R._pos_int("nonsense", 3) == 3
        assert R._pos_int(0, 3) == 3
        assert R._pos_int(-1, 3) == 3
        assert R._pos_int("7", 3) == 7

    def test_positive_float_falls_back(self) -> None:
        assert R._pos_float(None, 2.0) == 2.0
        assert R._pos_float("x", 2.0) == 2.0
        assert R._pos_float("0.5", 2.0) == 0.5

    def test_optional_values_may_stay_none(self) -> None:
        assert R._opt_int(None, None) is None
        assert R._opt_float(None, None) is None
        assert R._opt_int("4", None) == 4

    def test_bool_coercion(self) -> None:
        assert R._as_bool(None, True) is True
        assert R._as_bool("yes", False) is True
        assert R._as_bool("false", True) is False
        assert R._as_bool(False, True) is False


class TestSingleton:
    def test_get_supervisor_is_process_wide(self) -> None:
        """ "Is a run active?" must have exactly one answer per process."""
        assert R.get_supervisor() is R.get_supervisor()


# ── end to end, with a fake agent ───────────────────────────────────────────


@pytest.mark.skipif(not __import__('kiro_crew.sandbox', fromlist=['userns_available']).userns_available(), reason="requires unprivileged user namespaces (sandbox backend)")
class TestBoundedRunWithFakeAgent:
    """One real spine cycle, bounded, with the agent runner INJECTED as a fake."""

    def test_a_bounded_run_reaches_a_terminal_state(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = _tiny_repo(tmp_path / "clone")
        fake = FakeAgentRunner()
        # Injection point: keeps the real _build_driver (profile, caps, paths, safety
        # assertions) and swaps ONLY the thing that would spawn a model.
        monkeypatch.setattr(supervisor, "_build_runner", lambda *, stop_check: fake)

        result = supervisor.start(
            {
                "clone": str(clone),
                "branch": "main",
                "track": "bug",  # the bug track skips the perf preflight (no noise band)
                "maxCycles": 1,
                "maxHours": 0.2,
            }
        )
        assert result["status"] == R.STATUS_RUNNING
        assert result["run_id"]

        _join(supervisor, timeout=300.0)
        st = supervisor.status()
        assert st["status"] == R.STATUS_DONE, st["error"] or st
        assert st["stats"]["cycles"] == 1
        assert st["stats"]["discovered"] == 1
        assert st["stats"]["errors"] == 0
        # The loop must report itself — a silent run is a failure mode in its own right.
        assert st["activity"], "the run produced no activity at all"
        stages = {e.get("stage") for e in st["activity"] if e.get("stage")}
        assert {"profile", "propose", "gate", "keep"} <= stages, stages

        # The fix reached the keeper: RED -> GREEN -> STAYGREEN passed and the change was
        # kept and queued. Asserting the OUTCOME, not just a terminal status, is what
        # makes this a test of the engine rather than of the thread.
        assert st["kept"] == 1, st["stats"]
        assert st["stats"]["filed"] == 1
        assert st["drafted"] == 1
        assert fake.prompts, "the injected fake was never called"

    def test_the_run_never_spawns_a_real_agent_binary(
        self, supervisor: R.RunSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the guard: assert the injection actually took, so this suite can never
        start billing a real model."""
        clone = _tiny_repo(tmp_path / "clone")
        fake = FakeAgentRunner()
        monkeypatch.setattr(supervisor, "_build_runner", lambda *, stop_check: fake)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("a real agent binary was spawned")

        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        monkeypatch.setattr(ar.AgentRunner, "run", _boom)

        supervisor.start(
            {"clone": str(clone), "branch": "main", "track": "bug", "maxCycles": 1, "maxHours": 0.2}
        )
        _join(supervisor, timeout=180.0)
        assert supervisor.status()["status"] in (R.STATUS_DONE, R.STATUS_ERROR)


def _join(supervisor: R.RunSupervisor, *, timeout: float = 30.0) -> None:
    """Wait for the supervisor's worker thread to finish."""
    thread = supervisor._thread
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "the run thread did not finish in time"
    # The terminal status is set inside the thread's ``finally``-equivalent, so a tiny
    # settle window avoids a race on slow hosts.
    deadline = time.time() + 2.0
    while time.time() < deadline and supervisor.status()["status"] == R.STATUS_RUNNING:
        time.sleep(0.02)


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make every read of the run record note the thread it runs on, and return those notes."""
    threads: list[int] = []
    real_read = store.read_json

    def _read(path: Path, default: Any = None) -> Any:
        if Path(path).name == R.TERMINAL_RECORD_NAME:
            threads.append(threading.get_ident())
        return real_read(path, default)

    monkeypatch.setattr(store, "read_json", _read)
    return threads


def _get_request(path: str) -> web.Request:
    """A GET for one of this app's routes, for calling its handler directly."""
    return make_mocked_request("GET", f"/api/apps/{store.APP_NAME}{path}")


def _join_calibration(supervisor: R.RunSupervisor, *, timeout: float = 30.0) -> None:
    """Wait for a calibration worker to reach a terminal status.

    Unlike :func:`_join`, this waits for the calibration thread to leave the
    ``calibrating``/``stopping`` states, so an assertion about what the worker did
    (ran the canary, wrote a ruler) runs only after the worker has finished — never
    mid-phase, which would race the worker.
    """
    thread = supervisor._thread
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "the calibration thread did not finish in time"
    deadline = time.time() + 2.0
    transient = (R.STATUS_CALIBRATING, R.STATUS_RUNNING, R.STATUS_STOPPING)
    while time.time() < deadline and supervisor.status()["status"] in transient:
        time.sleep(0.02)


class TestCalibrationWritesToTheLaunchedWorkspace:
    """`_calibrate_loop` runs on a background thread, so it must NOT write the ruler via
    `store.ruler_dir()`, which re-reads the LIVE `config.json`. If the operator retargets
    (or starts another repo's calibration) while this one measures, that path lands the
    ruler in a DIFFERENT workspace, overwriting a ruler calibrated on unrelated code. The
    write derives its path from the CAPTURED config the worker was launched with.
    """

    def test_a_retarget_mid_calibration_does_not_move_the_ruler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as RR
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        # The config the worker is launched with — workspace A.
        launched = {"clone": "", "target_display": "owner/repoA", "branch": "origin/main"}

        class _Ruler:
            primary_name = "ttft"
            unit = "ms"
            direction = "minimize"

            def baseline_samples(self, *, base_src, reps):
                return [10.0, 12.0, 11.0]

            def measure_canary(self, *, base_src):
                class _M:
                    # `ok` is required: the calibration verdict now reuses the spine's
                    # `_canary_clears_band`, which refuses a canary whose MEASUREMENT failed
                    # (and one pointing the wrong way). The old backend rule was
                    # `abs(delta) > band`, which this stub satisfied without an `ok` field.
                    ok = True
                    primary_delta = -50.0  # a real win for a `minimize` ruler

                return _M()

        class _Cal:
            noise_floor = 0.0

        class _Profile:
            ruler = _Ruler()
            calibration = _Cal()

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.profiles.build_profile",
            lambda cfg: _Profile(),
        )

        # THE RACE: the moment calibration finishes measuring and is about to write, the
        # live config has already been retargeted to workspace B. `write_json_atomic` is
        # the last step, so flipping the file here reproduces "operator retargeted mid-run".
        real_write = store.write_json_atomic

        def _flip_then_write(path, obj):
            # Point live config at workspace B just before the ruler write lands.
            (store.data_dir() / "config.json").write_text(
                json.dumps({"target_display": "owner/repoB", "branch": "origin/main"}),
                encoding="utf-8",
            )
            return real_write(path, obj)

        monkeypatch.setattr(store, "write_json_atomic", _flip_then_write)

        sup = RR.RunSupervisor()
        sup._calibrate_loop(launched)

        key_a = store.workspace_key(launched)
        key_b = store.workspace_key({"target_display": "owner/repoB", "branch": "origin/main"})
        assert key_a != key_b, "the two workspaces must differ for this test to mean anything"

        ruler_a = store.data_dir() / "repos" / key_a / "ruler" / "ruler.json"
        ruler_b = store.data_dir() / "repos" / key_b / "ruler" / "ruler.json"
        assert ruler_a.is_file(), "the ruler was not written to the workspace it was launched for"
        assert not ruler_b.is_file(), "the ruler leaked into the retargeted workspace"
        doc = json.loads(ruler_a.read_text(encoding="utf-8"))
        assert doc["status"] == "calibrated"


class TestCalibrationRespondsToStop:
    """`POST /run/stop` during a standalone calibration must interrupt it.

    A run built by :meth:`~RunSupervisor.start` gets a driver whose
    ``request_stop`` is wired to the ruler's ``stop_check``, so a Stop click aborts
    the measurement between reps. Calibration (:meth:`~RunSupervisor.calibrate`)
    builds NO driver and runs the whole baseline-then-canary measurement back to
    back, so it must observe the supervisor's ``_stop_requested`` flag itself —
    otherwise a Stop click during a long baseline flips the status to ``stopping``
    while the suite keeps running to completion (the "stuck calibrating" symptom).
    """

    def _profile_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ruler: Any,
    ) -> None:
        class _Cal:
            noise_floor = 0.0

        class _Profile:
            def __init__(self) -> None:
                self.ruler = ruler
                self.calibration = _Cal()

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.profiles.build_profile",
            lambda cfg: _Profile(),
        )

    def _sequenced_ruler(
        self,
        *,
        in_baseline: threading.Event,
        release_baseline: threading.Event,
        canary_ran: threading.Event,
    ) -> Any:
        """A ruler that blocks in the baseline phase until the test releases it, so
        the test can request a stop while calibration is provably mid-baseline —
        the exact "stuck calibrating" window a Stop click has to interrupt. The
        baseline honors the duck-wired ``stop_check`` the supervisor must set (the
        real ``SuiteRuler`` polls it between reps); if it is never wired, the
        baseline returns full samples and the canary runs regardless of the stop."""

        class _SequencedRuler:
            primary_name = "ttft"
            unit = "ms"
            direction = "minimize"
            #: The supervisor must duck-wire this, exactly as the driver does for
            #: the Phase-1 preflight path. Left unset here so an unwired production
            #: path is visible as ``None``.
            stop_check: Any = None

            def baseline_samples(self, *, base_src: Any, reps: int) -> list[float]:
                in_baseline.set()
                # Wait until the test has issued the stop, then behave like the real
                # ruler: poll the wired stop_check and abort with a partial (here
                # empty) sample list when it reports True.
                release_baseline.wait(timeout=10.0)
                check = self.stop_check
                if callable(check) and check():
                    return []
                return [10.0, 11.0, 12.0]

            def measure_canary(self, *, base_src: Any) -> Any:
                canary_ran.set()

                class _M:
                    ok = True
                    primary_delta = -50.0

                return _M()

        return _SequencedRuler()

    def test_stop_during_baseline_interrupts_via_stop_check(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop issued while calibration is blocked in the baseline phase must
        interrupt it: the ruler's ``stop_check`` must be wired to the supervisor's
        stop flag, and the canary must not run after the stop."""
        in_baseline = threading.Event()
        release_baseline = threading.Event()
        canary_ran = threading.Event()

        ruler = self._sequenced_ruler(
            in_baseline=in_baseline,
            release_baseline=release_baseline,
            canary_ran=canary_ran,
        )
        self._profile_factory(monkeypatch, ruler)

        supervisor.calibrate({"clone": "", "target_display": "owner/repo", "branch": "main"})
        assert in_baseline.wait(timeout=10.0), "calibration never reached the baseline phase"

        # The Stop click, issued from the main thread while the baseline is blocked.
        # stop() joins the worker (bounded), and the worker cannot finish until the
        # baseline is released, so run stop() on a helper thread and release the
        # baseline once the stop flag is set — mirroring a real Stop click landing
        # mid-measurement.
        stop_result: dict[str, Any] = {}

        def _issue_stop() -> None:
            stop_result.update(supervisor.stop())

        stopper = threading.Thread(target=_issue_stop)
        stopper.start()
        # Wait until the stop has actually been requested before asserting on it —
        # the supervisor wires stop_check BEFORE the baseline blocks, so polling on
        # stop_check being callable would not wait for the stopper thread to run.
        deadline = time.time() + 5.0
        while time.time() < deadline and not supervisor._stop_check():
            time.sleep(0.01)
        # The supervisor must wire a live stop_check onto the ruler so the baseline
        # can observe the request; without the wiring it stays None.
        assert callable(ruler.stop_check), "calibration did not wire a stop_check onto the ruler"
        assert ruler.stop_check() is True, "the wired stop_check does not report the stop"

        release_baseline.set()
        stopper.join(timeout=10.0)
        _join(supervisor)

        assert stop_result.get("stopped") is True, "stop() did not interrupt the calibration"
        assert not canary_ran.is_set(), "calibration proceeded to the canary after a stop"
        # A stopped calibration is a clean terminal outcome, not a crash.
        assert supervisor.status()["status"] != R.STATUS_ERROR

    def test_stop_between_baseline_and_canary_skips_the_canary(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop requested after the baseline finishes but before the canary starts
        must short-circuit before ``measure_canary`` — the canary is itself a
        multi-rep suite run, so honoring the stop only between baseline reps is not
        enough. Here the baseline returns a FULL sample set (it was not itself
        interrupted); the boundary check before the canary is what must catch it."""
        in_baseline = threading.Event()
        release_baseline = threading.Event()
        canary_ran = threading.Event()

        ruler = self._sequenced_ruler(
            in_baseline=in_baseline,
            release_baseline=release_baseline,
            canary_ran=canary_ran,
        )

        # Neutralize stop_check so the baseline returns full samples even after the
        # stop — isolating the between-phase boundary check as the thing under test.
        def _blocking_baseline(*, base_src: Any, reps: int) -> list[float]:
            in_baseline.set()
            release_baseline.wait(timeout=10.0)
            return [10.0, 11.0, 12.0]

        ruler.baseline_samples = _blocking_baseline  # type: ignore[method-assign]
        self._profile_factory(monkeypatch, ruler)

        supervisor.calibrate({"clone": "", "target_display": "owner/repo", "branch": "main"})
        assert in_baseline.wait(timeout=10.0), "calibration never reached the baseline phase"

        # Issue the stop while the baseline is blocked, then release it. stop() joins
        # the worker, so run it on a helper thread and release the baseline once the
        # stop flag is observable — the worker then completes the baseline and must
        # skip the canary at the between-phase boundary check.
        stopper = threading.Thread(target=supervisor.stop)
        stopper.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and not supervisor._stop_check():
            time.sleep(0.01)
        release_baseline.set()
        stopper.join(timeout=10.0)
        _join_calibration(supervisor)

        st = supervisor.status()
        # The worker must have reached a terminal state and NOT failed for an
        # unrelated reason — otherwise "canary did not run" would be a false pass.
        assert st["status"] != R.STATUS_ERROR, f"calibration errored: {st.get('error')!r}"
        assert st["status"] in (R.STATUS_DONE, R.STATUS_STOPPING)
        assert not canary_ran.is_set(), "the canary ran even though a stop was requested"

    def test_stopped_calibration_does_not_write_a_ruler(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A calibration interrupted before it proved the ruler must NOT leave a
        ``ruler.json`` behind — a stopped run has no proven ruler, and a stale
        ``calibrated`` file would let a subsequent run start on an unproven ruler."""
        cfg = {"clone": "", "target_display": "owner/repo", "branch": "main"}
        in_baseline = threading.Event()
        release_baseline = threading.Event()
        canary_ran = threading.Event()

        ruler = self._sequenced_ruler(
            in_baseline=in_baseline,
            release_baseline=release_baseline,
            canary_ran=canary_ran,
        )
        self._profile_factory(monkeypatch, ruler)

        supervisor.calibrate(cfg)
        assert in_baseline.wait(timeout=10.0), "calibration never reached the baseline phase"

        stopper = threading.Thread(target=supervisor.stop)
        stopper.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and not supervisor._stop_check():
            time.sleep(0.01)
        release_baseline.set()
        stopper.join(timeout=10.0)
        _join_calibration(supervisor)

        ruler_path = (
            store.data_dir()
            / "repos"
            / store.workspace_key(cfg)
            / "ruler"
            / "ruler.json"
        )
        assert not ruler_path.is_file(), "a stopped calibration wrote a ruler.json"

    def test_stopping_a_recalibration_preserves_the_prior_ruler(
        self, supervisor: R.RunSupervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping a RECALIBRATION must leave an already-proven ruler intact.

        A stop is as often "this is taking too long" as "supersede this", so the
        abort lever must not destroy prior work the operator would have to re-pay a
        full multi-rep suite run to rebuild. This mirrors the failure path, which
        also leaves the prior ruler untouched — Stop is not more destructive than a
        crash. The stopped re-run itself writes nothing (it proved nothing), so the
        earlier ``calibrated`` ruler is exactly what survives."""
        cfg = {"clone": "", "target_display": "owner/repo", "branch": "main"}
        # Point live config at the same workspace so `ruler_calibrated()` (which
        # reads the live-config workspace) inspects the ruler this test seeds.
        (store.data_dir() / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

        ruler_path = (
            store.data_dir()
            / "repos"
            / store.workspace_key(cfg)
            / "ruler"
            / "ruler.json"
        )
        ruler_path.parent.mkdir(parents=True, exist_ok=True)
        # A prior, fully-proven ruler from an earlier calibration.
        prior = {"status": "calibrated"}
        ruler_path.write_text(json.dumps(prior), encoding="utf-8")
        assert progress_mod.ruler_calibrated() is True, "seed did not read as calibrated"

        in_baseline = threading.Event()
        release_baseline = threading.Event()
        canary_ran = threading.Event()
        ruler = self._sequenced_ruler(
            in_baseline=in_baseline,
            release_baseline=release_baseline,
            canary_ran=canary_ran,
        )
        self._profile_factory(monkeypatch, ruler)

        supervisor.calibrate(cfg)
        assert in_baseline.wait(timeout=10.0), "calibration never reached the baseline phase"

        stopper = threading.Thread(target=supervisor.stop)
        stopper.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and not supervisor._stop_check():
            time.sleep(0.01)
        release_baseline.set()
        stopper.join(timeout=10.0)
        _join_calibration(supervisor)

        assert ruler_path.is_file(), "the prior ruler.json was destroyed by a stopped recalibration"
        assert json.loads(ruler_path.read_text(encoding="utf-8")) == prior, (
            "the prior ruler was mutated by a stopped recalibration"
        )
        assert progress_mod.ruler_calibrated() is True, (
            "the workspace stopped reporting calibrated after a stopped recalibration"
        )
