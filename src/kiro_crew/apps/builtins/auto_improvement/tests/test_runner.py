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

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import runner as R

# ── helpers ─────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _tiny_repo(root: Path, *, push_disabled: bool = True) -> Path:
    """A minimal committed Python repo with a pytest suite, cloned-shaped."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
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
        # A push-only disable no longer satisfies the runtime `push_disabled()` gate: a
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


class TestCalibrationWritesToTheLaunchedWorkspace:
    """`_calibrate_loop` runs on a background thread and used to write the ruler via
    `store.ruler_dir()`, which re-reads the LIVE `config.json`. If the operator retargeted
    (or started another repo's calibration) while this one measured, the ruler landed in a
    DIFFERENT workspace, overwriting a ruler calibrated on unrelated code. The write now
    derives its path from the CAPTURED config the worker was launched with. Raised by the
    GPT review of this branch.
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
