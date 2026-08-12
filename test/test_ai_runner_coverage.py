"""Coverage for the auto-improvement app's run supervisor (``backend/runner.py``).

``RunSupervisor`` is the only thing that starts the autonomous loop, and it owns three
controls the rest of the app trusts: the push-disabled refusal, the credential-confinement
gate that decides whether an agent may run at all, and the fail-closed redaction of every
string entering the activity feed (which ``status()`` serves straight into ``GET /run``).
The module's own suite lives under the app tree, which the reduced-scope CI selector
deselects, so none of that was exercised on a pull request.

These tests drive the supervisor with INJECTED fakes at every blocking boundary: a fake
driver instead of the spine, a fake profile/ruler instead of a repository, and
``clone_setup.checkout_branch`` stubbed out. No agent binary, no provider, no network, no
real ``git``, and an autouse guard that fails the test if anything reaches
``subprocess``. Writes are confined to ``tmp_path`` (``KIROCREW_HOME``,
``AUTO_IMPROVEMENT_SCRATCH`` and ``store.data_dir`` are all redirected there), and the
worker threads the supervisor really does spawn are always joined before a test returns.

Runs everywhere the backend runs — Linux, macOS, Windows, 3.10 through 3.13: nothing here
touches a POSIX-only primitive.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement import profiles as profiles_mod
from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
from kiro_crew.apps.builtins.auto_improvement.backend import store
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as agent_runner_mod
from kiro_crew.apps.builtins.auto_improvement.spine import driver as driver_mod

WAIT_S = 10.0


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeDriver:
    """Duck-types the spine ``Driver``: ``run()`` returns stats, ``request_stop()`` unblocks.

    ``block`` makes ``run`` wait for a stop (or the release event) so a test can observe a
    genuinely in-flight run; ``raises`` makes it fail, which is how ``_run_loop``'s terminal
    error branch is reached. Nothing here spawns anything.
    """

    def __init__(
        self,
        *,
        block: bool = False,
        raises: BaseException | None = None,
        stats: Any = None,
        stop_raises: bool = False,
    ) -> None:
        self.block = block
        self.raises = raises
        self.stop_raises = stop_raises
        self.stats = stats if stats is not None else _stats()
        self.release = threading.Event()
        self.started = threading.Event()
        self.stop_calls = 0

    def run(self) -> Any:
        self.started.set()
        if self.block:
            self.release.wait(WAIT_S)
        if self.raises is not None:
            raise self.raises
        return self.stats

    def request_stop(self) -> None:
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("stop refused")
        self.release.set()


def _stats(**over: Any) -> SimpleNamespace:
    fields = {
        "cycles": 2,
        "discovered": 7,
        "deduped": 1,
        "gated_out": 2,
        "not_kept": 1,
        "kept": 3,
        "filed": 1,
        "errors": 0,
        "cost_usd": 0.5,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


class FakeRuler:
    """The profile's ruler: scripted baseline samples and one canary measurement."""

    def __init__(
        self,
        *,
        samples: list[float] | None = None,
        delta: float = -100.0,
        ok: bool = True,
        direction: str = "minimize",
    ) -> None:
        self.samples = [100.0, 100.0, 101.0] if samples is None else samples
        self.delta = delta
        self.ok = ok
        self.direction = direction
        self.primary_name = "suite_ms"
        self.unit = "ms"
        self.calls: list[str] = []

    def baseline_samples(self, *, base_src: Path, reps: int) -> list[float]:
        self.calls.append(f"baseline:{reps}")
        return list(self.samples)

    def measure_canary(self, *, base_src: Path) -> SimpleNamespace:
        self.calls.append("canary")
        return SimpleNamespace(ok=self.ok, primary_delta=self.delta)


def _profile(clone: Path, *, push_disabled: bool = True, ruler: FakeRuler | None = None) -> Any:
    return SimpleNamespace(
        clone_path=str(clone),
        isolation=SimpleNamespace(push_disabled=lambda: push_disabled),
        ruler=ruler or FakeRuler(),
        calibration=SimpleNamespace(noise_floor=1.0),
    )


class FakeSpineDriver:
    """Records the kwargs :meth:`_build_driver_locked` would construct the spine with."""

    instances: list[FakeSpineDriver] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeSpineDriver.instances.append(self)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the app's data root, the crew home, and the clone scratch into ``tmp_path``.

    ``store.data_dir`` is the seam every other path helper derives from, so patching it
    covers ``config_path``, ``ledger_path``, ``results_dir`` and the per-repo subtree.
    """
    data = tmp_path / "app-data"
    data.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "crew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("AUTO_IMPROVEMENT_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(store, "data_dir", lambda: data)
    return data


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any path under test tries to spawn a real binary."""

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(subprocess, "check_output", _refuse)


@pytest.fixture(autouse=True)
def _no_real_checkout(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """Stub the one git call both entry points make; records what it was asked to do."""
    seen: list[tuple[Path, str]] = []

    def _ok(clone: Path, branch: str, **kwargs: Any) -> tuple[bool, str]:
        seen.append((Path(clone), branch))
        return True, f"on {branch}"

    monkeypatch.setattr(clone_setup, "checkout_branch", _ok)
    return seen


@pytest.fixture()
def sup() -> Any:
    """A fresh supervisor whose worker thread is always joined."""
    made = R.RunSupervisor()
    yield made
    driver = made._driver
    if isinstance(driver, FakeDriver):
        driver.release.set()
    thread = made._thread
    if thread is not None:
        thread.join(timeout=WAIT_S)


def _await_status(sup: Any, wanted: set[str], timeout: float = WAIT_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = sup.status()
        if snapshot.get("status") in wanted:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"supervisor never reached {wanted}; last={snapshot}")


def _await_log(fragment: str, caplog: Any, timeout: float = WAIT_S) -> None:
    """Wait until *fragment* appears in the captured log, or fail with a deadline.

    A terminal status is written BEFORE the work that logs after it, so waiting on
    status and then reading `caplog` is a race the reader can lose. Waiting on the
    record is the same shape as `_await_status`: bounded, and loud if it never
    arrives rather than silently asserting on an empty buffer.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fragment in caplog.text:
            return
        time.sleep(0.01)
    raise AssertionError(f"log never carried {fragment!r}; got: {caplog.text[-400:]!r}")


# ── config coercion ──────────────────────────────────────────────────────────


class TestAwaitLog:
    """The log waiter must stay load-bearing.

    Its own failure mode -- returning unconditionally -- would make every caller
    assert on an empty buffer again, and on a fast host nothing would notice. The
    race it absorbs is only observable on a slow worker (the Windows runners), so
    this pins the two properties that ARE deterministic: it returns once the record
    is there, and it raises when it never arrives.
    """

    def test_it_returns_once_the_record_arrives(self, caplog: Any) -> None:
        with caplog.at_level("ERROR"):
            logging.getLogger("test.awaitlog").error("late but present")
            _await_log("late but present", caplog, timeout=1.0)

    def test_it_raises_when_the_record_never_arrives(self, caplog: Any) -> None:
        with pytest.raises(AssertionError, match="log never carried"):
            _await_log("nothing ever logs this", caplog, timeout=0.05)


class TestCoercion:
    """A bad config value must fall back to a sane budget, never 500 the Start button."""

    @pytest.mark.parametrize(
        "value,expected",
        [(7, 7), ("7", 7), (0, 3), (-1, 3), (None, 3), ("nope", 3), (2.9, 2)],
    )
    def test_pos_int(self, value: Any, expected: int) -> None:
        assert R._pos_int(value, 3) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(1.5, 1.5), ("2.5", 2.5), (0.0, 9.0), (-2.0, 9.0), (None, 9.0), ("x", 9.0)],
    )
    def test_pos_float(self, value: Any, expected: float) -> None:
        assert R._pos_float(value, 9.0) == expected

    @pytest.mark.parametrize(
        "value,expected", [(4, 4), ("4", 4), (0, None), (-3, None), (None, None), ("x", None)]
    )
    def test_opt_int(self, value: Any, expected: int | None) -> None:
        assert R._opt_int(value, None) == expected

    @pytest.mark.parametrize(
        "value,expected", [(4.0, 4.0), ("4", 4.0), (0.0, None), (-1.0, None), ("x", None)]
    )
    def test_opt_float(self, value: Any, expected: float | None) -> None:
        assert R._opt_float(value, None) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, True),
            (True, True),
            (False, False),
            ("yes", True),
            ("ON", True),
            ("1", True),
            ("true", True),
            ("no", False),
            ("", False),
            (1, True),
            (0, False),
        ],
    )
    def test_as_bool(self, value: Any, expected: bool) -> None:
        assert R._as_bool(value, True) is expected

    def test_as_bool_default_false_for_absent(self) -> None:
        assert R._as_bool(None, False) is False

    def test_stats_dict_copies_known_keys_and_zero_fills(self) -> None:
        out = R._stats_dict(SimpleNamespace(kept=3, cost_usd=1.25))
        assert out["kept"] == 3 and out["cost_usd"] == 1.25
        assert out["cycles"] == 0 and out["filed"] == 0
        assert set(out) == {
            "cycles",
            "discovered",
            "deduped",
            "not_kept",
            "gated_out",
            "kept",
            "filed",
            "errors",
            "cost_usd",
        }


# ── the activity-feed redactor (an egress path) ───────────────────────────────


class TestRedactActivity:
    def test_recurses_into_nested_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.security.redact", lambda text: text.replace("s3cret", "***"))
        out = R._redact_activity({"agent": {"detail": ["s3cret", 3, None]}, "n": 1})
        assert out == {"agent": {"detail": ["***", 3, None]}, "n": 1}

    def test_passthrough_for_non_text_scalars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.security.redact", lambda text: text)
        assert R._redact_activity(4.5) == 4.5

    def test_fails_closed_when_the_redactor_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_text: str) -> str:
            raise RuntimeError("scanner down")

        monkeypatch.setattr("kiro_crew.security.redact", _boom)
        assert R._redact_activity("aws_secret_access_key=abc") == R._UNSCANNED
        assert R._redact_activity({"k": "aws_secret_access_key=abc"}) == {"k": R._UNSCANNED}

    def test_fails_closed_for_a_bare_string_when_the_redactor_cannot_be_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "kiro_crew.security", None)
        assert R._redact_activity("some-credential=abc") == R._UNSCANNED

    def test_import_failure_fails_OPEN_for_containers_known_defect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CHARACTERISES A PRODUCT DEFECT — the assertion below is not the desired contract.

        ``_redact_activity``'s docstring says it is fail-closed and that "each unscannable
        STRING becomes a fixed placeholder while the event's structure ... survive". That
        holds when ``redact`` RAISES, but not when it cannot be IMPORTED: the import guard
        returns ``_UNSCANNED if isinstance(value, str) else value``, so a dict or list is
        returned verbatim WITHOUT recursing — no string inside it is ever scanned. Both
        callers pass a dict (``_on_progress`` passes the whole driver event,
        ``_on_agent_activity`` the whole agent event), so on an unimportable redactor every
        agent-authored string in the feed reaches ``GET /run`` unscanned. That is the exact
        fail-open the docstring records as having been closed.

        A fix would recurse first and only substitute at the leaf, e.g. hoist the
        placeholder decision below the dict/list branches. Reported, not fixed.
        """
        monkeypatch.setitem(sys.modules, "kiro_crew.security", None)
        served = R._redact_activity({"agent": {"detail": "some-credential=abc"}})
        assert served == {"agent": {"detail": "some-credential=abc"}}  # defect: unscanned
        assert R._redact_activity(["some-credential=abc"]) == ["some-credential=abc"]


# ── the credential-confinement gate ──────────────────────────────────────────


class TestCredentialConfinement:
    def _config(self, payload: dict[str, Any]) -> None:
        store.write_json_atomic(store.config_path(), payload)

    def test_opt_in_requires_the_literal_true(self) -> None:
        assert R._unsandboxed_agent_accepted() is False
        self._config({"acceptUnsandboxedAgentRisk": "true"})
        assert R._unsandboxed_agent_accepted() is False
        self._config({"acceptUnsandboxedAgentRisk": 1})
        assert R._unsandboxed_agent_accepted() is False
        self._config({"acceptUnsandboxedAgentRisk": True})
        assert R._unsandboxed_agent_accepted() is True

    def _sandbox(self, monkeypatch: pytest.MonkeyPatch, mode: Any) -> None:
        from kiro_crew.config import KiroCrewConfig

        monkeypatch.setattr(
            KiroCrewConfig, "load", staticmethod(lambda: SimpleNamespace(sandbox=mode))
        )

    @pytest.mark.parametrize("mode", ["cc", "strict", "  STRICT  "])
    def test_credential_hiding_modes_are_confined(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        self._sandbox(monkeypatch, mode)
        assert R._credentials_are_unconfined() == ""

    @pytest.mark.parametrize("mode", ["auto", "standard", "off", "", None])
    def test_other_modes_are_refused_with_a_reason(
        self, monkeypatch: pytest.MonkeyPatch, mode: Any
    ) -> None:
        self._sandbox(monkeypatch, mode)
        reason = R._credentials_are_unconfined()
        assert "requires 'cc' or 'strict'" in reason
        assert "acceptUnsandboxedAgentRisk" in reason

    def test_unreadable_config_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config import KiroCrewConfig

        def _boom() -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
        assert R._credentials_are_unconfined() == (
            "the gateway sandbox setting could not be read (OSError)"
        )

    def test_explicit_acknowledgement_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._sandbox(monkeypatch, "auto")
        self._config({"acceptUnsandboxedAgentRisk": True})
        assert R._credentials_are_unconfined() == ""


# ── the progress sinks ───────────────────────────────────────────────────────


class TestProgressSinks:
    def test_each_key_is_applied_independently(self, sup: Any) -> None:
        sup._on_progress({"cycle": "3"})
        sup._on_progress({"stage": "measure"})
        sup._on_progress({"cycle": "not-a-number"})
        sup._on_progress({"stage": None})
        sup._on_progress({"preflight": {"noiseBand": 4}, "budget": {"spent": 1}})
        sup._on_progress({"quiescence": {"streak": 2}, "cr_filed": {"fp": "abc"}})
        sup._on_progress({"cr_filed": "not-a-dict"})
        snapshot = sup.status()
        assert snapshot["cycle"] == 3
        assert snapshot["stage"] == ""
        assert snapshot["preflight"] == {"noiseBand": 4}
        assert snapshot["budget"] == {"spent": 1}
        assert snapshot["quiescence"] == {"streak": 2}
        assert snapshot["drafted"] == 1
        assert len(snapshot["activity"]) == 7
        assert all("t" in entry for entry in snapshot["activity"])

    def test_agent_activity_is_tagged_and_redacted(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.security.redact", lambda text: text.replace("k3y", "***"))
        sup._on_agent_activity({"detail": "read the k3y"})
        entry = sup.status()["activity"][-1]
        assert entry["agent"] == {"detail": "read the ***"}

    def test_activity_ring_is_bounded(self, sup: Any) -> None:
        assert sup._state.activity.maxlen == R.ACTIVITY_MAXLEN
        for index in range(R.ACTIVITY_MAXLEN + 5):
            sup._note(f"n{index}")
        activity = sup.status()["activity"]
        assert len(activity) == R.ACTIVITY_MAXLEN
        assert activity[-1]["note"] == f"n{R.ACTIVITY_MAXLEN + 4}"

    def test_fail_records_a_terminal_redacted_message(self, sup: Any) -> None:
        sup._fail(PermissionError("push is enabled"))
        snapshot = sup.status()
        assert snapshot["status"] == R.STATUS_ERROR
        assert snapshot["error"] == "PermissionError: push is enabled"
        assert snapshot["finished_at"] > 0
        assert snapshot["activity"][-1]["error"] == "PermissionError: push is enabled"

    def test_fail_message_is_fail_closed(self, sup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "kiro_crew.security", None)
        sup._fail(RuntimeError("aws_secret_access_key=abc"))
        assert sup.status()["error"] == f"RuntimeError: {R._UNSCANNED}"


# ── runner selection ─────────────────────────────────────────────────────────


class _FakeSessionRunner:
    """Stands in for ``SessionAgentRunner``: no provider, no session, no spawn."""

    is_available = True
    registers = True
    made: list[_FakeSessionRunner] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeSessionRunner.made.append(self)

    @classmethod
    def available(cls) -> bool:
        return cls.is_available

    def ensure_agent_registered(self) -> bool:
        return type(self).registers


@pytest.fixture()
def fake_session_runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeSessionRunner.made = []
    _FakeSessionRunner.is_available = True
    _FakeSessionRunner.registers = True
    monkeypatch.setattr(agent_runner_mod, "SessionAgentRunner", _FakeSessionRunner)
    return _FakeSessionRunner


class TestBuildRunner:
    def test_offline_when_no_provider_is_available(
        self, sup: Any, fake_session_runner: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_session_runner.is_available = False
        with caplog.at_level("WARNING"):
            assert sup._build_runner(stop_check=lambda: False) is None
        assert "no provider-backed agent runner available" in caplog.text

    def test_offline_when_credentials_are_unconfined(
        self,
        sup: Any,
        fake_session_runner: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "sandbox is 'off'")
        with caplog.at_level("WARNING"):
            assert sup._build_runner(stop_check=lambda: False) is None
        assert "refusing the provider-backed agent runner" in caplog.text
        assert fake_session_runner.made == []

    def test_offline_when_the_restricted_agent_cannot_be_registered(
        self,
        sup: Any,
        fake_session_runner: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "")
        fake_session_runner.registers = False
        with caplog.at_level("WARNING"):
            assert sup._build_runner(stop_check=lambda: False) is None
        assert "running OFFLINE" in caplog.text
        # NOT the subprocess fallback: the provider's permission gate must not be bypassed.
        assert len(fake_session_runner.made) == 1

    def test_returns_the_provider_runner_when_confined(
        self, sup: Any, fake_session_runner: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "")
        stop_check = sup._stop_check
        made = sup._build_runner(stop_check=stop_check)
        assert isinstance(made, _FakeSessionRunner)
        assert made.kwargs["stop_check"] is stop_check
        assert made.kwargs["on_activity"] == sup._on_agent_activity


# ── driver construction ──────────────────────────────────────────────────────


class TestBuildDriver:
    def _config(self, tmp_path: Path, **over: Any) -> dict[str, Any]:
        clone = tmp_path / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"clone": str(clone), "branch": "feat/x"}
        payload.update(over)
        return payload

    def _locked(self, sup: Any, config: dict[str, Any], profile: Any) -> Any:
        FakeSpineDriver.instances = []
        return sup._build_driver_locked(
            config, lambda cfg: profile, driver_mod.BudgetCaps, FakeSpineDriver
        )

    def test_checks_out_the_configured_branch_before_building_the_profile(
        self, sup: Any, tmp_path: Path, _no_real_checkout: list[tuple[Path, str]]
    ) -> None:
        config = self._config(tmp_path)
        seen_branch: list[str] = []

        def _build(cfg: dict[str, Any]) -> Any:
            seen_branch.append(_no_real_checkout[-1][1])
            return _profile(tmp_path / "clone")

        FakeSpineDriver.instances = []
        sup._build_driver_locked(config, _build, driver_mod.BudgetCaps, FakeSpineDriver)
        assert seen_branch == ["feat/x"]

    def test_no_checkout_when_no_clone_is_configured(
        self, sup: Any, tmp_path: Path, _no_real_checkout: list[tuple[Path, str]]
    ) -> None:
        self._locked(sup, {"clone": "  "}, _profile(tmp_path / "clone"))
        assert _no_real_checkout == []

    def test_defaults_to_main(
        self, sup: Any, tmp_path: Path, _no_real_checkout: list[tuple[Path, str]]
    ) -> None:
        self._locked(sup, self._config(tmp_path, branch=""), _profile(tmp_path / "clone"))
        assert _no_real_checkout[-1][1] == "main"
        assert FakeSpineDriver.instances[-1].kwargs["branch"] == "main"

    def test_failed_checkout_refuses_the_run(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            clone_setup, "checkout_branch", lambda clone, branch, **kw: (False, "no such ref")
        )
        with pytest.raises(RuntimeError) as excinfo:
            self._locked(sup, self._config(tmp_path), _profile(tmp_path / "clone"))
        message = str(excinfo.value)
        assert "could not check out feat/x (no such ref)" in message
        assert "wrong revision" in message
        assert "scopeDiffBase" not in message

    def test_failed_checkout_names_the_scope_hazard(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            clone_setup, "checkout_branch", lambda clone, branch, **kw: (False, "no such ref")
        )
        config = self._config(tmp_path, scopeDiffBase="origin/main")
        with pytest.raises(RuntimeError) as excinfo:
            self._locked(sup, config, _profile(tmp_path / "clone"))
        assert "scopeDiffBase would resolve against the wrong HEAD" in str(excinfo.value)

    def test_refuses_when_push_is_not_disabled(self, sup: Any, tmp_path: Path) -> None:
        profile = _profile(tmp_path / "clone", push_disabled=False)
        with pytest.raises(PermissionError) as excinfo:
            self._locked(sup, self._config(tmp_path), profile)
        assert "push is not disabled" in str(excinfo.value)

    def test_caps_come_from_config_with_documented_defaults(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "unset")
        config = self._config(
            tmp_path,
            maxCycles="0",
            maxHours="4.5",
            maxCostUsd="nonsense",
            quiesceAfter=9,
            proposerWide=3,
            proposerDeep=0,
            measureReps="2",
            bandCapMs="12.5",
            canaryAdvisory="yes",
            directCommit=True,
        )
        driver = self._locked(sup, config, _profile(tmp_path / "clone"))
        assert isinstance(driver, FakeSpineDriver)
        caps = driver.kwargs["caps"]
        assert caps.max_cycles == R.DEFAULT_MAX_CYCLES  # 0 is not positive → default
        assert caps.max_hours == 4.5
        assert caps.max_cost_usd == R.DEFAULT_MAX_COST_USD
        assert caps.quiesce_after == 9
        assert caps.proposer_wide == 3
        assert caps.proposer_deep == 1  # 0 is not positive → the documented default
        assert caps.measure_reps == 2
        assert caps.reproduce_reps is None
        assert caps.band_cap_ms == 12.5
        assert driver.kwargs["canary_advisory"] is True
        assert driver.kwargs["direct_commit"] is True

    def test_quiesce_after_falls_back_to_the_spine_default(self, sup: Any, tmp_path: Path) -> None:
        driver = self._locked(sup, self._config(tmp_path), _profile(tmp_path / "clone"))
        assert driver.kwargs["caps"].quiesce_after == driver_mod.BudgetCaps.quiesce_after

    def test_worktrees_live_in_scratch_never_the_data_dir(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "unset")
        driver = self._locked(sup, self._config(tmp_path), _profile(tmp_path / "clone"))
        worktrees = Path(driver.kwargs["worktree_root"])
        assert worktrees == store.scratch_dir() / "worktrees"
        assert store.data_dir() not in worktrees.parents
        assert driver.kwargs["on_progress"] == sup._on_progress
        assert driver.kwargs["agent_runner"] is None  # offline: unconfined credentials

    def test_build_driver_holds_the_clone_lock(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper is what serializes the ``checkout -B`` against the draft route."""
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        held: list[bool] = []
        real_lock = commit_mod.clone_lock()

        def _build(cfg: dict[str, Any]) -> Any:
            held.append(real_lock._is_owned())
            return _profile(tmp_path / "clone")

        monkeypatch.setattr(R, "_credentials_are_unconfined", lambda: "unset")
        monkeypatch.setattr(profiles_mod, "build_profile", _build)
        monkeypatch.setattr(driver_mod, "Driver", FakeSpineDriver)
        FakeSpineDriver.instances = []
        driver = sup._build_driver(self._config(tmp_path))
        assert held == [True]
        assert isinstance(driver, FakeSpineDriver)
        assert not real_lock._is_owned()


# ── start / status / stop ────────────────────────────────────────────────────


class TestStart:
    def test_start_runs_the_driver_and_reports_terminal_stats(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(stats=_stats(kept=3, cycles=2))
        monkeypatch.setattr(sup, "_build_driver", lambda config: driver)
        accepted = sup.start({"clone": "x"})
        assert accepted["status"] == R.STATUS_RUNNING
        assert accepted["run_id"].startswith("run-")
        snapshot = _await_status(sup, {R.STATUS_DONE})
        assert snapshot["kept"] == 3
        assert snapshot["stats"]["cycles"] == 2
        assert snapshot["stage"] == ""
        assert snapshot["finished_at"] >= snapshot["started_at"]
        notes = [entry.get("note") for entry in snapshot["activity"]]
        assert notes[0] == f"run {accepted['run_id']} starting"
        assert notes[-1] == "run finished"

    def test_a_driver_failure_is_state_not_a_crash(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sup, "_build_driver", lambda config: FakeDriver(raises=RuntimeError("suite exploded"))
        )
        sup.start({})
        snapshot = _await_status(sup, {R.STATUS_ERROR})
        assert snapshot["error"] == "RuntimeError: suite exploded"

    def test_second_start_is_refused_while_a_run_is_in_flight(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(block=True)
        monkeypatch.setattr(sup, "_build_driver", lambda config: driver)
        accepted = sup.start({})
        assert driver.started.wait(WAIT_S)
        with pytest.raises(RuntimeError) as excinfo:
            sup.start({})
        assert accepted["run_id"] in str(excinfo.value)
        driver.release.set()
        _await_status(sup, {R.STATUS_DONE})

    def test_a_reserved_but_unstarted_worker_still_counts_as_in_flight(self, sup: Any) -> None:
        with sup._lock:
            sup._reserved = True
        with pytest.raises(RuntimeError):
            sup.start({})
        with pytest.raises(RuntimeError):
            sup.calibrate({})
        with sup._lock:
            sup._reserved = False

    def test_a_race_between_the_two_probes_is_caught_under_the_lock(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_build_driver`` runs OUTSIDE the lock, so both entry points re-check inside it.

        Simulated rather than raced: ``_in_flight`` answers "free" to the first probe and
        "busy" to the second, which is what a concurrent POST landing during the slow
        driver build looks like from here.
        """
        for entry, kwargs in (("start", {}), ("calibrate", {})):
            probes: list[bool] = [False, True]
            monkeypatch.setattr(sup, "_in_flight", lambda: probes.pop(0))
            monkeypatch.setattr(sup, "_build_driver", lambda config: FakeDriver())
            with sup._lock:
                sup._state.run_id = "run-earlier"
            with pytest.raises(RuntimeError, match="run-earlier"):
                getattr(sup, entry)(kwargs)
            assert probes == []
            assert sup._thread is None

    def test_a_refusal_leaves_the_supervisor_untouched(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(config: dict[str, Any]) -> Any:
            raise ValueError("no repository configured")

        monkeypatch.setattr(sup, "_build_driver", _refuse)
        with pytest.raises(ValueError):
            sup.start({})
        assert sup.status()["status"] == R.STATUS_IDLE
        assert sup._thread is None and sup._reserved is False

    def test_a_spawn_failure_does_not_wedge_the_supervisor(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``thread.start()`` raising must clear the reservation, not leave it busy forever."""

        class _UnstartableThread:
            def __init__(self, **kwargs: Any) -> None:
                self.name = kwargs.get("name", "")

            def start(self) -> None:
                raise RuntimeError("can't start new thread")

            def is_alive(self) -> bool:
                return False

        monkeypatch.setattr(
            R, "threading", SimpleNamespace(Thread=_UnstartableThread, Lock=threading.Lock)
        )
        monkeypatch.setattr(sup, "_build_driver", lambda config: FakeDriver())
        with pytest.raises(RuntimeError, match="can't start new thread"):
            sup.start({})
        assert sup._reserved is False
        with pytest.raises(RuntimeError, match="can't start new thread"):
            sup.calibrate({})
        assert sup._reserved is False
        sup._thread = None


class TestStatusAndStop:
    def test_idle_status_shape(self, sup: Any) -> None:
        snapshot = sup.status()
        assert snapshot["status"] == R.STATUS_IDLE
        assert snapshot["run_id"] == ""
        assert snapshot["activity"] == []
        assert set(snapshot) == {
            "status",
            "run_id",
            "cycle",
            "stage",
            "kept",
            "drafted",
            "error",
            "started_at",
            "finished_at",
            "preflight",
            "budget",
            "quiescence",
            "stats",
            "activity",
        }

    @pytest.mark.parametrize(
        "error,expected",
        [("", R.STATUS_DONE), ("BaseException: torn down", R.STATUS_ERROR)],
    )
    def test_a_dead_worker_is_never_reported_running(
        self, sup: Any, error: str, expected: str
    ) -> None:
        with sup._lock:
            sup._state.status = R.STATUS_RUNNING
            sup._state.error = error
        assert sup.status()["status"] == expected

    def test_stop_with_no_active_run(self, sup: Any) -> None:
        assert sup.stop() == {
            "status": R.STATUS_IDLE,
            "run_id": "",
            "stopped": False,
            "note": "no active run",
        }

    def test_stop_requests_a_clean_stop_and_joins(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(block=True)
        monkeypatch.setattr(sup, "_build_driver", lambda config: driver)
        accepted = sup.start({})
        assert driver.started.wait(WAIT_S)
        outcome = sup.stop()
        assert outcome["run_id"] == accepted["run_id"]
        assert outcome["stopped"] is True
        assert driver.stop_calls == 1
        assert sup._stop_check() is True
        notes = [entry.get("note") for entry in sup.status()["activity"]]
        assert "stop requested" in notes

    def test_stop_reports_stopping_when_the_driver_will_not_yield(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(block=True, stop_raises=True)
        monkeypatch.setattr(sup, "_build_driver", lambda config: driver)
        monkeypatch.setattr(R, "STOP_JOIN_TIMEOUT_S", 0.2)
        sup.start({})
        assert driver.started.wait(WAIT_S)
        outcome = sup.stop()
        assert outcome["stopped"] is False
        assert outcome["status"] == R.STATUS_STOPPING
        assert driver.stop_calls == 1  # the raise was swallowed, not propagated
        driver.release.set()
        _await_status(sup, {R.STATUS_DONE})


# ── calibration ──────────────────────────────────────────────────────────────


class TestCalibrate:
    def _config(self, tmp_path: Path, **over: Any) -> dict[str, Any]:
        clone = tmp_path / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "clone": str(clone),
            "branch": "origin/main",
            "target_display": "owner/repo",
            "calibrationReps": 3,
        }
        payload.update(over)
        return payload

    def _ruler_doc(self, config: dict[str, Any]) -> dict[str, Any]:
        path = store.data_dir() / "repos" / store.workspace_key(config) / "ruler" / "ruler.json"
        return store.read_json(path, {}) or {}

    def _arrange(self, monkeypatch: pytest.MonkeyPatch, profile: Any) -> None:
        monkeypatch.setattr(profiles_mod, "build_profile", lambda cfg: profile)

    def test_a_clearing_canary_writes_a_calibrated_ruler(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruler = FakeRuler(samples=[100.0, 100.0, 102.0], delta=-90.0)
        profile = _profile(tmp_path / "clone", ruler=ruler)
        self._arrange(monkeypatch, profile)
        config = self._config(tmp_path)
        # Make the live config agree with the captured one, so the post-write
        # cross-check reads the same workspace the worker wrote to.
        store.write_json_atomic(store.config_path(), config)

        accepted = sup.calibrate(config)
        assert accepted["status"] == R.STATUS_CALIBRATING
        assert accepted["run_id"].startswith("cal-")
        snapshot = _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
        assert snapshot["status"] == R.STATUS_DONE
        assert snapshot["preflight"]["canaryCleared"] is True
        assert snapshot["preflight"]["baselineReps"] == 3
        assert snapshot["preflight"]["canaryDelta"] == -90.0
        assert ruler.calls == ["baseline:3", "canary"]

        doc = self._ruler_doc(config)
        assert doc["status"] == "calibrated"
        assert doc["canary"] == {
            "result": "calibrated",
            "observedDelta": -90.0,
            "clearedBand": True,
        }
        assert doc["battery"] == [100.0, 100.0, 102.0]
        assert doc["measured"] == {"reps": 3, "median": 100.0}
        assert doc["noiseBand"]["method"] == "max(2sigma, floor)"
        assert doc["primary"]["unit"] == "ms"

    def test_a_regression_canary_is_reported_not_calibrated(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direction matters: a +25 delta on a minimize ruler must NOT pass."""
        profile = _profile(
            tmp_path / "clone", ruler=FakeRuler(samples=[100.0, 100.0, 100.0], delta=25.0)
        )
        self._arrange(monkeypatch, profile)
        config = self._config(tmp_path)
        store.write_json_atomic(store.config_path(), config)
        sup.calibrate(config)
        snapshot = _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
        assert snapshot["status"] == R.STATUS_ERROR
        assert "canary did not clear the noise band" in snapshot["error"]
        assert self._ruler_doc(config)["status"] == "canary_failed"

    def test_an_unsuccessful_measurement_does_not_pass(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = _profile(tmp_path / "clone", ruler=FakeRuler(delta=-500.0, ok=False))
        self._arrange(monkeypatch, profile)
        config = self._config(tmp_path)
        sup.calibrate(config)
        snapshot = _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
        assert snapshot["status"] == R.STATUS_ERROR
        assert self._ruler_doc(config)["canary"]["clearedBand"] is False

    def test_no_baseline_samples_is_a_failure(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arrange(monkeypatch, _profile(tmp_path / "clone", ruler=FakeRuler(samples=[])))
        sup.calibrate(self._config(tmp_path))
        snapshot = _await_status(sup, {R.STATUS_ERROR})
        assert snapshot["error"] == "RuntimeError: the ruler returned no baseline samples"

    def test_a_garbled_band_cap_is_ignored_rather_than_fatal(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = _profile(
            tmp_path / "clone", ruler=FakeRuler(samples=[100.0, 130.0, 160.0], delta=-500.0)
        )
        self._arrange(monkeypatch, profile)
        config = self._config(tmp_path, bandCapMs="not-a-number")
        sup.calibrate(config)
        snapshot = _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
        assert snapshot["status"] == R.STATUS_DONE
        assert self._ruler_doc(config)["noiseBand"]["value"] > 1.0  # uncapped 2sigma

    def test_a_band_cap_bounds_the_measured_band(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = _profile(
            tmp_path / "clone", ruler=FakeRuler(samples=[100.0, 130.0, 160.0], delta=-500.0)
        )
        self._arrange(monkeypatch, profile)
        config = self._config(tmp_path, bandCapMs=5)
        sup.calibrate(config)
        _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
        assert self._ruler_doc(config)["noiseBand"]["value"] == 5.0

    def test_a_failed_checkout_refuses_to_calibrate(
        self, sup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            clone_setup, "checkout_branch", lambda clone, branch, **kw: (False, "gone")
        )
        self._arrange(monkeypatch, _profile(tmp_path / "clone"))
        sup.calibrate(self._config(tmp_path))
        snapshot = _await_status(sup, {R.STATUS_ERROR})
        assert "refusing to calibrate: could not check out origin/main (gone)" in snapshot["error"]
        assert "wrong revision" in snapshot["error"]

    def test_a_ruler_that_disagrees_with_the_result_is_logged(
        self,
        sup: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The captured config pins the write path; a retargeted live config must not
        silently redirect it — and the post-write cross-check is what notices."""
        self._arrange(monkeypatch, _profile(tmp_path / "clone", ruler=FakeRuler(delta=-90.0)))
        config = self._config(tmp_path)
        # No config.json on disk → `workspace_key()` resolves a DIFFERENT workspace than
        # `workspace_key(config)`, which is exactly the disagreement the check exists for.
        with caplog.at_level("ERROR"):
            sup.calibrate(config)
            snapshot = _await_status(sup, {R.STATUS_DONE, R.STATUS_ERROR})
            # The worker writes the terminal STATUS before it runs the post-write
            # cross-check that logs, so `_await_status` returning is not proof the
            # record exists yet — a fixed assertion here read an empty `caplog`
            # whenever the worker thread lost that gap, which Windows runners did
            # consistently. Wait for the record itself, with a real deadline.
            _await_log("ruler.json disagrees with the calibration result", caplog)
        assert snapshot["status"] == R.STATUS_DONE
        assert self._ruler_doc(config)["status"] == "calibrated"
        assert "ruler.json disagrees with the calibration result" in caplog.text

    def test_calibrate_is_refused_while_a_run_is_in_flight(
        self, sup: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(block=True)
        monkeypatch.setattr(sup, "_build_driver", lambda config: driver)
        accepted = sup.start({})
        assert driver.started.wait(WAIT_S)
        with pytest.raises(RuntimeError) as excinfo:
            sup.calibrate({})
        assert accepted["run_id"] in str(excinfo.value)
        driver.release.set()
        _await_status(sup, {R.STATUS_DONE})


# ── the process-wide singleton ───────────────────────────────────────────────


class TestGetSupervisor:
    def test_one_supervisor_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(R, "_SUPERVISOR", None)
        first = R.get_supervisor()
        assert first is R.get_supervisor()
        assert isinstance(first, R.RunSupervisor)
