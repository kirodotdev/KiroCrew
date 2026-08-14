"""Coverage for the auto-improvement spine :mod:`driver` — the durable while-loop.

The driver is the outer layer of the two-layer spine: it owns git + archive + ledger
state and drives one per-cycle workflow (discover -> propose -> gate -> measure ->
keep/revert) until a budget cap or quiescence stops it. Almost every branch in it is a
REFUSAL path (push not disabled, review gate blocked, credential scan hit, rebase
conflict, provisional commit rolled back), and those are exactly the ones a happy-path
test never reaches.

Every collaborator is injected as a fake and both git surfaces are replaced:

  * ``driver._git`` (the module-level helper) and ``driver.subprocess`` are routed to one
    :class:`_Git` recorder that answers scripted ``(returncode, stdout, stderr)`` triples
    by argv prefix, so no real git process ever runs;
  * ``driver.require_pinned`` is stubbed — the attributes pin needs a real gitdir and is
    covered by its own suite;
  * profile / proposer / gate / measurer / keeper / CR-pipeline are hand-rolled fakes.

Nothing writes outside ``tmp_path`` and nothing touches the network.
"""

from __future__ import annotations

import logging
import subprocess
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L
from kiro_crew.apps.builtins.auto_improvement.spine import push_policy as PP
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_FAILED_BUILD,
    BUG_FILED,
    BUG_NOT_GREEN,
    TRACK_BUG,
    TRACK_PERF,
    BugGateResult,
    Candidate,
    DiscoveryResult,
    GateResult,
    Measurement,
    Proposal,
    StageBreakdown,
    Verdict,
)
from kiro_crew.apps.builtins.auto_improvement.spine.keeper import DISCARD_NOISE, KEPT
from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrOutcome
from kiro_crew.apps.builtins.auto_improvement.spine.preflight import PreflightResult

LOG = logging.getLogger("test.ai_spine_driver")


# ─────────────────────────── fakes ───────────────────────────


class _Git:
    """Recorder standing in for BOTH ``driver._git`` and ``driver.subprocess.run``.

    Results are scripted by argv PREFIX (longest match wins) so a caller can pin
    ``"rev-parse --verify"`` separately from ``"rev-parse HEAD"``. Passing several results
    for one key makes it a queue (each call pops one, the last one repeats).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inputs: list[str] = []
        self._script: dict[str, list[tuple[int, str, str]]] = {}

    def script(self, key: str, *results) -> None:
        norm: list[tuple[int, str, str]] = []
        for r in results:
            norm.append(r if isinstance(r, tuple) else (int(r), "", ""))
        self._script[key] = norm

    def _take(self, joined: str) -> tuple[int, str, str]:
        best: str | None = None
        for k in self._script:
            if joined.startswith(k) and (best is None or len(k) > len(best)):
                best = k
        if best is None:
            return (0, "", "")
        seq = self._script[best]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def git(self, args, cwd):
        joined = " ".join(str(a) for a in args)
        self.calls.append(joined)
        rc, out, err = self._take(joined)
        return subprocess.CompletedProcess(args=list(args), returncode=rc, stdout=out, stderr=err)

    def run(self, argv, **kwargs):
        toks = [str(a) for a in argv]
        if toks[:1] == ["git"]:
            toks = toks[1:]
        if toks[:1] == ["-C"]:
            toks = toks[2:]
        clean: list[str] = []
        i = 0
        while i < len(toks):
            if toks[i] == "-c":
                i += 2
                continue
            clean.append(toks[i])
            i += 1
        joined = " ".join(clean)
        self.calls.append(joined)
        self.inputs.append(str(kwargs.get("input", "")))
        rc, out, err = self._take(joined)
        return subprocess.CompletedProcess(args=list(argv), returncode=rc, stdout=out, stderr=err)

    def seen(self, prefix: str) -> list[str]:
        return [c for c in self.calls if c.startswith(prefix)]


class _Ruler:
    primary_name = "latency"
    unit = "ms"

    def __init__(self, *, direction="minimize", tolerances=None, baselines=None, boom=False):
        self.direction = direction
        self._tol = tolerances
        self._base = baselines
        self._boom = boom

    def guardrail_tolerances(self):
        if self._boom:
            raise RuntimeError("tolerance source unavailable")
        return self._tol

    def guardrail_baselines(self):
        return self._base


class _SlottedRuler:
    """A ruler that refuses ``stop_check`` — the frozen/slotted branch in ``preflight``."""

    __slots__ = ()
    primary_name = "latency"
    unit = "ms"
    direction = "minimize"


class _Isolation:
    base_ref = "origin/feature"

    def __init__(self, *, disabled=True, boot="absent"):
        self._disabled = disabled
        self._boot = boot

    def push_disabled(self) -> bool:
        return self._disabled

    def __getattr__(self, name):
        # ``measurement_boot`` only exists when the recipe was built with one, so an
        # older recipe (the fallback branch) genuinely lacks the attribute.
        if name == "measurement_boot" and self._boot != "absent":
            return lambda: self._boot
        raise AttributeError(name)


class _Calib:
    def __init__(self, noise_band=0.0):
        self.noise_band = noise_band
        self.canary_id = "canary-1"


class _SlottedCalib:
    """Adopting the calibrated band into THIS refuses both setattr paths."""

    __slots__ = ()
    noise_band = 0.0
    canary_id = "canary-1"


class _BuildGate:
    def __init__(self, *, passed=True, detail="", boom=False):
        self._passed = passed
        self._detail = detail
        self._boom = boom
        self.calls = 0

    def build_and_test(self, *, worktree, src):
        self.calls += 1
        if self._boom:
            raise RuntimeError("gate exploded")
        return types.SimpleNamespace(passed=self._passed, detail=self._detail)


class _BugRunner:
    def __init__(self, *, green=True, failing=(), boom=False):
        self._green = green
        self._failing = list(failing)
        self._boom = boom

    def run_suite(self, *, src):
        if self._boom:
            raise RuntimeError("suite exploded")
        return self._green, list(self._failing)


class _Profile:
    id = "fake-profile"

    def __init__(
        self,
        *,
        track=TRACK_PERF,
        ruler=None,
        isolation=None,
        calibration=None,
        build_gate=None,
        bug_runner=None,
        fetch_url="",
        discovery=None,
        capture=None,
    ):
        self.track = track
        self.ruler = ruler if ruler is not None else _Ruler()
        self.isolation = isolation if isolation is not None else _Isolation()
        self.calibration = calibration if calibration is not None else _Calib()
        self.build_gate = build_gate if build_gate is not None else _BuildGate()
        self.bug_runner = bug_runner if bug_runner is not None else _BugRunner()
        self.pr_recipe = types.SimpleNamespace(fetch_url=fetch_url)
        self._discovery = discovery if discovery is not None else DiscoveryResult()
        self.discover_kwargs: dict = {}
        if capture is not None:
            self.capture_profile = capture

    def discover(self, *, base_sha, top_k, known_loci, agent_runner=None):
        self.discover_kwargs = {
            "base_sha": base_sha,
            "top_k": top_k,
            "known_loci": known_loci,
            "agent_runner": agent_runner,
        }
        return self._discovery


class _Proposer:
    def __init__(self, proposals=()):
        self._proposals = list(proposals)
        self.torn_down: list[str] = []
        self.fan_out_kwargs: dict = {}

    def fan_out(self, *, profile, candidates, base_sha, cycle, stop_check):
        self.fan_out_kwargs = {"candidates": list(candidates), "cycle": cycle}
        return list(self._proposals)

    def teardown(self, proposal) -> None:
        self.torn_down.append(proposal.cand_id)


class _Gate:
    def __init__(self, *, result=None, bug_result=None):
        self._result = result or GateResult(passed=True, commit_sha="gatedsha")
        self._bug = bug_result or BugGateResult(passed=True, reason=BUG_FILED)

    def run(self, *, profile, proposal, base_sha):
        return self._result

    def run_bug(self, *, profile, proposal, base_sha):
        return self._bug


class _Measurer:
    reps = 4

    def __init__(self, measurement=None):
        self._m = measurement or _measurement()

    def measure(self, *, profile, proposal, gated_commit_sha):
        return self._m


class _Keeper:
    def __init__(self, verdict=None, archived=()):
        self._verdict = verdict or Verdict(keep=False, status="no_keep", reason="none")
        self._archived = list(archived)
        self.direction: str | None = None

    def decide(self, *, survivors, guardrail_tolerances=None, direction="minimize"):
        self.direction = direction
        return self._verdict, list(self._archived)


class _Pipeline:
    """Stand-in for :class:`CrPipeline` — the driver only reads the outcome."""

    def __init__(self, outcome=None):
        self.ruler_proven = False
        self._outcome = outcome or CrOutcome(fp="fp-1", status="filed", cr="CR-1", filed=True)
        self.perf_kwargs: dict = {}
        self.bug_kwargs: dict = {}

    def emit_perf(self, **kwargs):
        self.perf_kwargs = kwargs
        return self._outcome

    def emit_bug(self, **kwargs):
        self.bug_kwargs = kwargs
        return self._outcome


class _AgentRunner:
    def __init__(self, text="", boom=False, cost=None):
        self._text = text
        self._boom = boom
        self.prompts: list[str] = []
        if cost is not None:
            self.total_cost_usd = cost

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self._boom:
            raise RuntimeError("runner exploded")
        return types.SimpleNamespace(text=self._text)


class _Clock:
    def __init__(self, steps=(0.0,)):
        self._steps = list(steps)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]

    def sleep(self, seconds) -> None:
        self.slept.append(seconds)


# ─────────────────────────── builders ───────────────────────────


def _measurement(delta=-5.0, **kw):
    base = {
        "ok": True,
        "primary_delta": delta,
        "primary_base": 100.0,
        "primary_cand": 95.0,
        "noise_band": 2.0,
        "stages": StageBreakdown(stages={"boot": 1.5}),
        "guardrails": {"rss": -1.0},
        "secondary": {"cpu": 2.0},
        "note": "measured",
    }
    base.update(kw)
    return Measurement(**base)


def _proposal(cand_id="c1", *, kind=TRACK_PERF, target="mod.py::sym", diff="", skipped=False, **kw):
    return Proposal(
        cand_id=cand_id,
        candidate=Candidate(kind=kind, target=target, signature="sig"),
        worktree=Path("."),
        branch=f"cand/{cand_id}",
        description="a candidate",
        diff=diff,
        skipped=skipped,
        **kw,
    )


DIFF = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """No real home, no inherited fan-out overrides, no leaked log handlers."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    for var in ("AUTO_IMPROVEMENT_WIDE", "AUTO_IMPROVEMENT_DEEP"):
        monkeypatch.delenv(var, raising=False)
    named = logging.getLogger("auto_improvement.driver")
    before = list(named.handlers)
    yield
    named.handlers[:] = before


@pytest.fixture
def git(monkeypatch):
    g = _Git()
    monkeypatch.setattr(drv, "_git", g.git)
    monkeypatch.setattr(drv, "subprocess", types.SimpleNamespace(run=g.run))
    monkeypatch.setattr(drv, "require_pinned", lambda cwd: None)
    return g


def _make(tmp_path, *, profile=None, caps=None, **kw):
    clone = tmp_path / "clone"
    clone.mkdir(parents=True, exist_ok=True)
    d = drv.Driver(
        profile=profile if profile is not None else _Profile(),
        clone=clone,
        branch="auto_improvement/feature",
        archive_root=tmp_path / "results",
        ledger_path=tmp_path / "state" / "ledger.jsonl",
        pr_queue_dir=tmp_path / "queue",
        worktree_root=tmp_path / "worktrees",
        caps=caps,
        logger=LOG,
        **kw,
    )
    d.stats = drv.Stats()
    return d


# ─────────────────────────── construction ───────────────────────────


def test_default_cost_meter_is_a_zero_that_never_trips_the_cap(tmp_path):
    d = _make(tmp_path)
    assert d.cost_meter() == 0.0
    assert d.direct_commit is False
    assert d.prepush_review is False


def test_cost_meter_defaults_to_the_agent_runners_accumulated_spend(tmp_path):
    runner = _AgentRunner(cost=lambda: 12.5)
    d = _make(tmp_path, agent_runner=runner)
    assert d.cost_meter() == 12.5


def test_explicit_cost_meter_wins_over_the_agent_runner(tmp_path):
    d = _make(tmp_path, agent_runner=_AgentRunner(cost=lambda: 1.0), cost_meter=lambda: 9.0)
    assert d.cost_meter() == 9.0


def test_caps_fan_out_overrides_beat_the_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_IMPROVEMENT_WIDE", "6")
    monkeypatch.setenv("AUTO_IMPROVEMENT_DEEP", "3")
    d = _make(tmp_path, caps=drv.BudgetCaps(proposer_wide=1, proposer_deep=2))
    assert (d.proposer.wide, d.proposer.deep) == (1, 2)


def test_env_supplies_the_fan_out_shape_when_caps_are_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_IMPROVEMENT_WIDE", "4")
    monkeypatch.setenv("AUTO_IMPROVEMENT_DEEP", "2")
    d = _make(tmp_path)
    assert (d.proposer.wide, d.proposer.deep) == (4, 2)


def test_measure_rep_overrides_are_clamped_to_a_floor_of_two(tmp_path):
    d = _make(tmp_path, caps=drv.BudgetCaps(measure_reps=1, reproduce_reps=1))
    assert d.measurer.reps == 2
    assert d.measurer.reproduce_reps == 2


def test_retry_cooldown_is_threaded_into_the_ledger(tmp_path):
    d = _make(tmp_path, retry_cooldown_s=7.0)
    assert d.ledger.retry_cooldown_s == 7.0


# ─────────────────────────── boot-time safety ───────────────────────────


def test_push_disabled_clone_starts(tmp_path):
    _make(tmp_path).assert_push_disabled()


def test_live_push_without_direct_commit_refuses_to_start(tmp_path):
    d = _make(tmp_path, profile=_Profile(isolation=_Isolation(disabled=False)))
    with pytest.raises(drv.PushEnabledError, match="refusing to start"):
        d.assert_push_disabled()


def test_live_push_is_tolerated_under_an_authorized_direct_commit(tmp_path):
    d = _make(
        tmp_path,
        profile=_Profile(isolation=_Isolation(disabled=False)),
        direct_commit=True,
    )
    d.assert_push_disabled()  # scoped push exception


def test_live_push_on_a_protected_branch_still_refuses(tmp_path):
    d = _make(tmp_path, profile=_Profile(isolation=_Isolation(disabled=False)), direct_commit=True)
    d.branch = "main"
    with pytest.raises(drv.PushEnabledError):
        d.assert_push_disabled()


def test_head_sha_reads_the_branch_tip(tmp_path, git):
    git.script("rev-parse HEAD", (0, "  abc123\n", ""))
    assert _make(tmp_path).head_sha() == "abc123"


def test_git_helper_pins_then_delegates(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(drv, "require_pinned", lambda cwd: seen.append(Path(cwd)))
    monkeypatch.setattr(
        drv,
        "subprocess",
        types.SimpleNamespace(
            run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "out", "")
        ),
    )
    res = drv._git(["status"], tmp_path)
    assert res.stdout == "out"
    assert seen == [tmp_path]


# ─────────────────────────── preflight ───────────────────────────


def _stub_preflight(monkeypatch, result=None, capture=None):
    res = result or PreflightResult(
        ok=True,
        noise_band=7.5,
        baseline_n=5,
        canary_delta=-30.0,
        canary_cleared=True,
        note="proven",
    )

    def _fake(profile, *, base_src, boot, logger=None, canary_advisory=False, band_cap_ms=None):
        if capture is not None:
            capture.update(
                {
                    "base_src": base_src,
                    "boot": boot,
                    "canary_advisory": canary_advisory,
                    "band_cap_ms": band_cap_ms,
                }
            )
        return res

    monkeypatch.setattr(drv.PF, "calibrate_and_prove", _fake)
    return res


def test_preflight_adopts_the_calibrated_band_and_derived_tolerances(tmp_path, monkeypatch):
    seen: dict = {}
    _stub_preflight(monkeypatch, capture=seen)
    profile = _Profile(ruler=_Ruler(tolerances={"rss": 4.0, "boot": 9.0}))
    d = _make(tmp_path, profile=profile, caps=drv.BudgetCaps(band_cap_ms=25.0))
    d.guardrail_tolerances["rss"] = 1.0  # an explicit caller value must survive

    res = d.preflight()

    assert res.noise_band == 7.5
    assert d.preflight_result is res
    assert d.pr_pipeline.ruler_proven is True
    assert profile.calibration.noise_band == 7.5
    assert d.guardrail_tolerances == {"rss": 1.0, "boot": 9.0}
    assert seen["band_cap_ms"] == 25.0
    assert callable(profile.ruler.stop_check)
    assert profile.ruler.stop_check() is False


def test_preflight_survives_a_ruler_that_refuses_a_stop_check_and_a_frozen_band(
    tmp_path, monkeypatch
):
    _stub_preflight(monkeypatch)
    profile = _Profile(ruler=_SlottedRuler(), calibration=_SlottedCalib())
    d = _make(tmp_path, profile=profile)
    assert d.preflight().noise_band == 7.5
    assert _SlottedCalib.noise_band == 0.0  # nothing was mutated


def test_preflight_tolerates_a_raising_tolerance_source(tmp_path, monkeypatch):
    _stub_preflight(monkeypatch)
    d = _make(tmp_path, profile=_Profile(ruler=_Ruler(boom=True)))
    d.preflight()
    assert d.guardrail_tolerances == {}


def test_preflight_uses_an_explicitly_injected_boot_verbatim(tmp_path, monkeypatch):
    seen: dict = {}
    _stub_preflight(monkeypatch, capture=seen)
    sentinel = object()
    boot = lambda: sentinel  # noqa: E731 — a one-expression fake boot
    d = _make(tmp_path, profile=_Profile(isolation=_Isolation(boot=lambda: None)), boot_callable=boot)
    d.preflight()
    assert seen["boot"] is boot


def test_measurement_boot_comes_from_the_isolation_recipe_when_not_injected(tmp_path):
    real_boot = lambda: None  # noqa: E731
    d = _make(tmp_path, profile=_Profile(isolation=_Isolation(boot=real_boot)))
    assert d._resolve_measurement_boot() is real_boot


def test_measurement_boot_falls_back_when_the_recipe_yields_no_callable(tmp_path):
    d = _make(tmp_path, profile=_Profile(isolation=_Isolation(boot=None)))
    assert d._resolve_measurement_boot() is d.boot_callable


def test_measurement_boot_falls_back_when_the_recipe_has_no_seam(tmp_path):
    d = _make(tmp_path)
    assert d._resolve_measurement_boot() is d.boot_callable


# ─────────────────────────── small pure helpers ───────────────────────────


def test_progress_sink_failure_never_breaks_the_loop(tmp_path):
    def _boom(_event):
        raise RuntimeError("sink down")

    d = _make(tmp_path, on_progress=_boom)
    d._progress(stage="propose")  # swallowed


def test_a_non_callable_progress_sink_degrades_to_a_no_op(tmp_path):
    d = _make(tmp_path, on_progress="not callable")
    d._progress(stage="gate")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("maximize", "maximize"),
        ("  MAXIMIZE  ", "maximize"),
        ("minimize", "minimize"),
        ("", "minimize"),
        ("sideways", "minimize"),
    ],
)
def test_metric_direction_normalizes_to_the_two_keeper_values(tmp_path, raw, expected):
    d = _make(tmp_path, profile=_Profile(ruler=_Ruler(direction=raw)))
    assert d._metric_direction() == expected


def test_metric_direction_defaults_when_the_profile_has_no_ruler(tmp_path):
    d = _make(tmp_path)
    d.profile = types.SimpleNamespace()
    assert d._metric_direction() == "minimize"


def test_record_truncates_a_long_note_before_the_ledger(tmp_path):
    d = _make(tmp_path)
    d._record(_proposal(), L.STATUS_ERROR, "x" * 500)
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    entry = d.ledger._seen[fp]
    assert entry.status == L.STATUS_ERROR
    assert len(entry.note) == 200


def test_metric_blob_forwards_exactly_what_the_ruler_measured(tmp_path):
    blob = drv.Driver._metric_blob(_measurement())
    assert blob["primary_delta"] == -5.0
    assert blob["stages"] == {"boot": 1.5}
    assert blob["guardrails"] == {"rss": -1.0}
    assert blob["secondary"] == {"cpu": 2.0}
    assert blob["rh_capability_ok"] is True


def test_redact_commit_message_scrubs_and_returns_a_string(tmp_path):
    out = drv.Driver._redact_commit_message("perf: shave 5ms off boot")
    assert "shave 5ms" in out


def test_redact_commit_message_fails_closed_to_a_fixed_subject(monkeypatch):
    import kiro_crew.security as sec

    monkeypatch.setattr(sec, "redact", lambda text: (_ for _ in ()).throw(RuntimeError("no")))
    assert drv.Driver._redact_commit_message("anything") == (
        "auto-improvement: apply verified change"
    )


def test_capture_profile_is_optional(tmp_path):
    _make(tmp_path)._capture_profile(_proposal())  # no hook at all


def test_capture_profile_records_a_hook_result(tmp_path):
    seen: dict = {}

    def _hook(*, fp, worktree):
        seen.update({"fp": fp, "worktree": worktree})
        return "profile.json"

    d = _make(tmp_path, profile=_Profile(capture=_hook))
    d._capture_profile(_proposal())
    assert seen["fp"] == L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")


def test_capture_profile_tolerates_a_hook_that_captured_nothing(tmp_path):
    d = _make(tmp_path, profile=_Profile(capture=lambda *, fp, worktree: None))
    d._capture_profile(_proposal())


def test_capture_profile_failure_never_loses_a_candidate(tmp_path):
    def _hook(*, fp, worktree):
        raise RuntimeError("profiler died")

    _make(tmp_path, profile=_Profile(capture=_hook))._capture_profile(_proposal())


# ─────────────────────────── re-verify + push with rebase ───────────────────────────


def test_reverify_head_passes_a_green_rebased_tree(tmp_path, git):
    gate = _BuildGate(passed=True)
    d = _make(tmp_path, profile=_Profile(build_gate=gate))
    assert d._reverify_head() is True
    assert gate.calls == 1


def test_reverify_head_refuses_a_red_rebased_tree(tmp_path, git):
    d = _make(tmp_path, profile=_Profile(build_gate=_BuildGate(passed=False, detail="2 failing")))
    assert d._reverify_head() is False


def test_reverify_head_refuses_an_unverifiable_tree(tmp_path, git):
    d = _make(tmp_path, profile=_Profile(build_gate=_BuildGate(boom=True)))
    assert d._reverify_head() is False


def test_push_succeeds_on_the_first_attempt(tmp_path, git):
    git.script("push", 0)
    d = _make(tmp_path)
    assert d._push_with_rebase("https://example.invalid/r.git", "feature", "mod.py::sym").returncode == 0
    assert git.seen("fetch") == []


def test_a_non_race_push_failure_is_returned_untouched(tmp_path, git):
    git.script("push", (1, "", "fatal: authentication failed"))
    d = _make(tmp_path)
    res = d._push_with_rebase("https://example.invalid/r.git", "feature", "mod.py::sym")
    assert res.returncode == 1
    assert git.seen("fetch") == []  # no retry masks a real error


def test_a_lost_race_with_a_failing_fetch_returns_the_rejection(tmp_path, git):
    git.script("push", (1, "", "! [rejected] non-fast-forward"))
    git.script("fetch", 1)
    d = _make(tmp_path)
    assert d._push_with_rebase("https://example.invalid/r.git", "feature", "t").returncode == 1
    assert git.seen("rebase") == []


def test_a_conflicting_rebase_aborts_and_does_not_push(tmp_path, git):
    git.script("push", (1, "", "fetch first"))
    git.script("fetch", 0)
    git.script("rebase FETCH_HEAD", 1)
    git.script("rebase --abort", 0)
    d = _make(tmp_path)
    assert d._push_with_rebase("https://example.invalid/r.git", "feature", "t").returncode == 1
    assert git.seen("rebase --abort")


def test_an_unverifiable_rebased_tree_is_not_published(tmp_path, git):
    git.script("push", (1, "", "non-fast-forward"))
    git.script("fetch", 0)
    git.script("rebase FETCH_HEAD", 0)
    d = _make(tmp_path, profile=_Profile(build_gate=_BuildGate(passed=False)))
    assert d._push_with_rebase("https://example.invalid/r.git", "feature", "t").returncode == 1
    assert len(git.seen("push")) == 1  # never pushed a second time


def test_a_reverified_rebase_retries_the_push_once(tmp_path, git):
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("fetch", 0)
    git.script("rebase FETCH_HEAD", 0)
    d = _make(tmp_path)
    assert d._push_with_rebase("https://example.invalid/r.git", "feature", "t").returncode == 0
    assert len(git.seen("push")) == 2


# ─────────────────────────── pre-push review gate ───────────────────────────


def test_review_gate_off_authorizes_without_running(tmp_path):
    d = _make(tmp_path)
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is True
    assert "disabled" in note


def test_review_gate_without_an_agent_runner_blocks(tmp_path):
    d = _make(tmp_path, prepush_review=True)
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is False
    assert "no agent runner" in note


def test_review_gate_accepts_the_last_clean_verdict(tmp_path):
    runner = _AgentRunner(text="REVIEW: 1 open\nfixed it\nREVIEW: clean")
    d = _make(tmp_path, prepush_review=True, agent_runner=runner)
    clean, note = d._prepush_review_clean(target="t", base_ref="origin/feature")
    assert (clean, note) == (True, "prepush_review clean")
    assert "PRE-PUSH review gate" in runner.prompts[0]


def test_review_gate_blocks_on_concrete_open_findings(tmp_path):
    d = _make(tmp_path, prepush_review=True, agent_runner=_AgentRunner(text="REVIEW: 3 open"))
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is False
    assert "open findings" in note


def test_an_unavailable_review_is_rescued_by_a_green_suite(tmp_path):
    d = _make(
        tmp_path,
        profile=_Profile(bug_runner=_BugRunner(green=True)),
        prepush_review=True,
        agent_runner=_AgentRunner(text="REVIEW: unavailable"),
    )
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is True
    assert "full suite green" in note


def test_an_unparseable_verdict_with_a_red_suite_blocks(tmp_path):
    d = _make(
        tmp_path,
        profile=_Profile(bug_runner=_BugRunner(green=False, failing=["a", "b", "c", "d"])),
        prepush_review=True,
        agent_runner=_AgentRunner(text="I had a look and it seems fine"),
    )
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is False
    assert "no clear verdict" in note
    assert "blocking push" in note


def test_a_raising_review_gate_blocks(tmp_path):
    d = _make(tmp_path, prepush_review=True, agent_runner=_AgentRunner(boom=True))
    clean, note = d._prepush_review_clean(target="t", base_ref="b")
    assert clean is False
    assert "gate error" in note


def test_build_test_fallback_needs_a_suite_primitive(tmp_path):
    d = _make(tmp_path, profile=_Profile(bug_runner=object()))
    assert d._build_test_pre_push_clean(target="t") == (False, "no build/test gate available")


def test_build_test_fallback_fails_closed_on_a_gate_error(tmp_path):
    d = _make(tmp_path, profile=_Profile(bug_runner=_BugRunner(boom=True)))
    clean, note = d._build_test_pre_push_clean(target="t")
    assert clean is False
    assert "gate error" in note


def test_build_test_fallback_reports_the_first_failing_tests(tmp_path):
    d = _make(
        tmp_path,
        profile=_Profile(bug_runner=_BugRunner(green=False, failing=["t1", "t2", "t3", "t4"])),
    )
    clean, note = d._build_test_pre_push_clean(target="t")
    assert clean is False
    assert note == "4 failing test(s): t1, t2, t3"


def test_build_test_fallback_prefers_the_clones_src_tree(tmp_path):
    seen: dict = {}

    class _Runner:
        def run_suite(self, *, src):
            seen["src"] = src
            return True, []

    d = _make(tmp_path, profile=_Profile(bug_runner=_Runner()))
    (d.clone / "src").mkdir(parents=True, exist_ok=True)
    assert d._build_test_pre_push_clean(target="t")[0] is True
    import os

    assert os.path.realpath(seen["src"]) == os.path.realpath(d.clone / "src")


# ─────────────────────────── the F10 direct push ───────────────────────────


def _direct_push_driver(tmp_path, **kw):
    profile = kw.pop("profile", None) or _Profile(fetch_url="https://example.invalid/repo.git")
    return _make(tmp_path, profile=profile, direct_commit=True, **kw)


def test_direct_push_refuses_a_protected_branch(tmp_path, git):
    d = _direct_push_driver(tmp_path)
    d.branch = "main"
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is False
    assert d.ledger._seen["fp"].note.startswith("direct-push refused:")
    assert git.seen("push") == []


def test_direct_push_refuses_when_direct_commit_is_off(tmp_path, git):
    d = _make(tmp_path)
    assert d._direct_push(fp="fp", kind="bug", target="t", sha="abc") is False
    assert "direct-commit mode is off" in d.ledger._seen["fp"].note


def test_direct_push_is_blocked_by_the_review_gate(tmp_path, git):
    d = _direct_push_driver(tmp_path, prepush_review=True)
    assert d._direct_push(fp="fp", kind="bug", target="t", sha="abc") is False
    assert d.ledger._seen["fp"].note.startswith("pre-push review gate blocked:")


@pytest.mark.parametrize("sha", ["", "-"])
def test_direct_push_refuses_a_missing_commit_sha(tmp_path, git, sha):
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=sha) is False
    assert d.ledger._seen["fp"].note == "direct-push: winner diff did not apply"


def test_direct_push_refuses_a_disabled_remote_url(tmp_path, git):
    git.script("remote get-url", (0, "DISABLED_NO_PUSH\n", ""))
    d = _direct_push_driver(tmp_path, profile=_Profile(fetch_url=""))
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is False
    assert d.ledger._seen["fp"].note == "direct-push: no usable remote url"


def test_direct_push_refuses_an_unreadable_pushable_diff(tmp_path, git):
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (128, "", "fatal"))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is False
    assert d.ledger._seen["fp"].note == (
        "direct-push refused: could not read the pushable diff"
    )


def test_direct_push_scans_a_root_commit_with_show(tmp_path, git, monkeypatch):
    git.script("rev-parse --verify", 1)  # no parent → root commit
    git.script("show --format=", (0, DIFF, ""))
    git.script("rev-parse HEAD", (0, "landedsha\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is True
    assert git.seen("show --format=")


def test_direct_push_refuses_content_the_scanner_flags(tmp_path, git, monkeypatch):
    monkeypatch.setattr(PP, "scan_content_for_secrets", lambda text: (False, PP.SCAN_HIT))
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (0, DIFF, ""))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is False
    assert d.ledger._seen["fp"].note == (
        "direct-push refused: content scan found credential/exfiltration finding(s)"
    )
    assert git.seen("push") == []


def test_direct_push_records_a_failed_push(tmp_path, git):
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (0, DIFF, ""))
    git.script("rev-parse HEAD", (0, "headsha\n", ""))
    git.script("push", (1, "", "remote rejected"))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is False
    assert "direct-push failed" in d.ledger._seen["fp"].note
    assert d.pushed_sha == "headsha"


def test_direct_push_reports_the_sha_that_actually_landed(tmp_path, git):
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (0, DIFF, ""))
    git.script("rev-parse HEAD", (0, "rebasedsha\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="presha") is True
    assert d.pushed_sha == "rebasedsha"


def test_direct_push_falls_back_to_the_snapshot_sha_when_rev_parse_blanks(tmp_path, git):
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (0, DIFF, ""))
    git.script("rev-parse HEAD", (0, "   \n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="snapshot") is True
    assert d.pushed_sha == "snapshot"


def test_direct_push_reads_the_fetch_url_off_the_clone_when_the_profile_has_none(tmp_path, git):
    git.script("remote get-url", (0, "https://example.invalid/from-clone.git\n", ""))
    git.script("rev-parse --verify", 0)
    git.script("diff HEAD~1..HEAD", (0, DIFF, ""))
    git.script("rev-parse HEAD", (0, "sha\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path, profile=_Profile(fetch_url=""))
    assert d._direct_push(fp="fp", kind="perf", target="t", sha="abc") is True
    pushes = git.seen("push")
    assert pushes == [
        "push https://example.invalid/from-clone.git HEAD:refs/heads/auto_improvement/feature"
    ]


# ─────────────────────────── staging / committing / rollback ───────────────────────────


def test_discard_staged_removes_files_the_patch_created(tmp_path, git):
    git.script("diff --cached", (0, "new.py\n\n", ""))
    d = _make(tmp_path)
    created = d.clone / "new.py"
    created.write_text("x\n", newline="\n")
    d._discard_staged("a failed provisional commit")
    assert not created.exists()
    assert git.seen("reset --hard HEAD")


def test_discard_staged_logs_a_failed_reset(tmp_path, git, caplog):
    git.script("diff --cached", (0, "", ""))
    git.script("reset --hard", (1, "", "index locked"))
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        _make(tmp_path)._discard_staged("a failed commit")
    assert "could not discard the staged diff" in caplog.text


def test_discard_staged_tolerates_an_unremovable_path(tmp_path, git):
    git.script("diff --cached", (0, "subdir\n", ""))
    d = _make(tmp_path)
    (d.clone / "subdir").mkdir()
    (d.clone / "subdir" / "keep.txt").write_text("x\n", newline="\n")
    d._discard_staged("a failed commit")
    assert (d.clone / "subdir" / "keep.txt").exists()


def test_stage_winner_short_circuits_on_an_empty_diff(tmp_path, git):
    d = _make(tmp_path)
    assert d._stage_winner(_proposal(diff="   ")) is True
    assert git.seen("apply") == []
    assert git.seen("checkout auto_improvement/feature")


def test_stage_winner_reports_a_diff_that_will_not_apply(tmp_path, git):
    git.script("apply", (1, "", "error: patch does not apply"))
    d = _make(tmp_path)
    assert d._stage_winner(_proposal(diff=DIFF)) is False
    assert git.seen("add -A") == []


def test_stage_winner_stages_an_applied_diff(tmp_path, git):
    d = _make(tmp_path)
    assert d._stage_winner(_proposal(diff=DIFF)) is True
    assert git.seen("add -A")
    assert DIFF in git.inputs


def test_provisional_commit_is_skipped_for_an_empty_diff(tmp_path, git):
    d = _make(tmp_path)
    assert d._commit_winner_provisional(_proposal(diff="")) is True
    assert git.seen("commit") == []


def test_provisional_commit_fails_closed_when_it_cannot_apply(tmp_path, git):
    git.script("apply", 1)
    d = _make(tmp_path)
    assert d._commit_winner_provisional(_proposal(diff=DIFF)) is False


def test_a_rejected_provisional_commit_discards_the_staged_diff(tmp_path, git):
    git.script("commit -q -m", (1, "", "pre-commit hook rejected"))
    git.script("diff --cached", (0, "", ""))
    d = _make(tmp_path)
    assert d._commit_winner_provisional(_proposal(diff=DIFF)) is False
    assert git.seen("reset --hard HEAD")


def test_a_provisional_commit_never_names_the_candidate(tmp_path, git):
    d = _make(tmp_path)
    assert d._commit_winner_provisional(_proposal("c1_AKIAIOSFODNN7EXAMPLE", diff=DIFF)) is True
    commits = git.seen("commit -q -m")
    assert commits == ["commit -q -m wip(auto-improvement): staging a verified candidate"]


@pytest.mark.parametrize("pre_sha", ["", "abc123"])
def test_reset_provisional_is_a_no_op_when_nothing_advanced(tmp_path, git, pre_sha):
    git.script("rev-parse HEAD", (0, "abc123\n", ""))
    d = _make(tmp_path)
    d._reset_provisional(pre_sha)
    assert git.seen("reset --hard abc123") == []


def test_reset_provisional_rolls_the_branch_back(tmp_path, git):
    git.script("rev-parse HEAD", (0, "newsha\n", ""))
    d = _make(tmp_path)
    d._reset_provisional("oldsha")
    assert git.seen("reset --hard oldsha")


def test_reset_provisional_logs_a_failed_rollback(tmp_path, git, caplog):
    git.script("rev-parse HEAD", (0, "newsha\n", ""))
    git.script("reset --hard", (1, "", "cannot reset"))
    d = _make(tmp_path)
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        d._reset_provisional("oldsha")
    assert "could not roll back the provisional commit" in caplog.text


def test_finalize_winner_commit_skips_the_amend_for_an_empty_diff(tmp_path, git):
    git.script("rev-parse --short HEAD", (0, "shorty\n", ""))
    d = _make(tmp_path)
    got = d._finalize_winner_commit(_proposal(diff=""), verify=_measurement(), cycle=1, diff_ref="d")
    assert got == "shorty"
    assert git.seen("commit -q --amend") == []


def test_finalize_winner_commit_amends_with_the_reproduce_numbers(tmp_path, git, monkeypatch):
    seen: dict = {}

    def _msg(**kwargs):
        seen.update(kwargs)
        return "perf: real numbers"

    monkeypatch.setattr(drv.D, "perf_commit_message", _msg)
    git.script("rev-parse --short HEAD", (0, "amended\n", ""))
    d = _make(tmp_path)
    reproduce = _measurement(delta=-4.0)
    got = d._finalize_winner_commit(
        _proposal(diff=DIFF), verify=_measurement(), reproduce=reproduce, cycle=3, diff_ref="d.diff"
    )
    assert got == "amended"
    assert seen["reproduce"] is reproduce
    assert git.seen("commit -q --amend")


def test_finalize_winner_commit_falls_back_to_verify_without_a_reproduce(tmp_path, git, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(drv.D, "perf_commit_message", lambda **kw: seen.update(kw) or "m")
    git.script("rev-parse --short HEAD", (0, "x\n", ""))
    verify = _measurement()
    _make(tmp_path)._finalize_winner_commit(
        _proposal(diff=DIFF), verify=verify, cycle=1, diff_ref="d"
    )
    assert seen["reproduce"] is verify


def test_bug_stage_retries_with_a_three_way_merge(tmp_path, git):
    git.script("apply", (1, "", "uv.lock: already exists"))
    git.script("apply --3way", 0)
    d = _make(tmp_path)
    assert d._stage_bug_winner(_proposal(kind=TRACK_BUG, diff=DIFF)) is True
    assert git.seen("apply --3way")


def test_bug_stage_gives_up_when_even_three_way_fails(tmp_path, git):
    git.script("apply", (1, "", "no"))
    git.script("apply --3way", (1, "", "still no"))
    d = _make(tmp_path)
    assert d._stage_bug_winner(_proposal(kind=TRACK_BUG, diff=DIFF)) is False


def test_bug_stage_short_circuits_on_an_empty_diff(tmp_path, git):
    d = _make(tmp_path)
    assert d._stage_bug_winner(_proposal(kind=TRACK_BUG, diff="")) is True
    assert git.seen("apply") == []


def test_a_rejected_provisional_bug_commit_discards_the_staged_diff(tmp_path, git):
    git.script("commit -q -m", (1, "", "hook rejected"))
    git.script("diff --cached", (0, "", ""))
    d = _make(tmp_path)
    assert d._commit_bug_winner_provisional(_proposal(kind=TRACK_BUG, diff=DIFF)) is False
    assert git.seen("reset --hard HEAD")


def test_provisional_bug_commit_short_circuits_on_an_empty_diff(tmp_path, git):
    d = _make(tmp_path)
    assert d._commit_bug_winner_provisional(_proposal(kind=TRACK_BUG, diff="")) is True


def test_provisional_bug_commit_reports_a_diff_that_will_not_apply(tmp_path, git):
    git.script("apply", 1)
    git.script("apply --3way", 1)
    d = _make(tmp_path)
    assert d._commit_bug_winner_provisional(_proposal(kind=TRACK_BUG, diff=DIFF)) is False


def test_a_provisional_bug_commit_never_names_the_candidate(tmp_path, git):
    d = _make(tmp_path)
    prop = _proposal("b1_AKIAIOSFODNN7EXAMPLE", kind=TRACK_BUG, diff=DIFF)
    assert d._commit_bug_winner_provisional(prop) is True
    assert git.seen("commit -q -m") == [
        "commit -q -m wip(auto-improvement): staging a verified candidate"
    ]


def test_finalize_bug_commit_amends_with_the_red_green_narrative(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv.D, "bug_commit_message", lambda **kw: "fix: red to green")
    git.script("rev-parse --short HEAD", (0, "bugsha\n", ""))
    d = _make(tmp_path)
    got = d._finalize_bug_winner_commit(
        _proposal(kind=TRACK_BUG, diff=DIFF),
        bug_res=BugGateResult(passed=True, reason=BUG_FILED),
        cycle=2,
        diff_ref="d.diff",
    )
    assert got == "bugsha"
    assert git.seen("commit -q --amend")


def test_finalize_bug_commit_skips_the_amend_for_an_empty_diff(tmp_path, git):
    git.script("rev-parse --short HEAD", (0, "same\n", ""))
    d = _make(tmp_path)
    got = d._finalize_bug_winner_commit(
        _proposal(kind=TRACK_BUG, diff=""),
        bug_res=BugGateResult(passed=True, reason=BUG_FILED),
        cycle=1,
        diff_ref="d",
    )
    assert got == "same"
    assert git.seen("commit -q --amend") == []


# ─────────────────────────── one proposal through its track ───────────────────────────


def test_a_skipped_proposal_records_its_own_terminal_status(tmp_path, git):
    d = _make(tmp_path)
    prop = _proposal(skipped=True, skip_status=L.STATUS_NO_DEFECT, skip_reason="nothing found")
    d._work_one_proposal(
        prop, base_sha="b", cycle=1, proposals=[prop], perf_survivors=[], bug_winners=[], gated_sha={}
    )
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].status == L.STATUS_NO_DEFECT
    assert d.stats.errors == 0


def test_a_skipped_proposal_from_a_real_error_counts_as_one(tmp_path, git):
    d = _make(tmp_path)
    prop = _proposal(skipped=True, skip_status=L.STATUS_ERROR, skip_reason="")
    d._work_one_proposal(
        prop, base_sha="b", cycle=1, proposals=[prop], perf_survivors=[], bug_winners=[], gated_sha={}
    )
    assert d.stats.errors == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].note == "no diff produced"


def test_an_accepted_bug_fix_becomes_a_winner(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(bug_result=BugGateResult(passed=True, reason=BUG_FILED))
    winners: list = []
    prop = _proposal(kind=TRACK_BUG)
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=[],
        bug_winners=winners,
        gated_sha={},
    )
    assert [p.cand_id for p, _ in winners] == ["c1"]


@pytest.mark.parametrize(
    "reason,expected_status,counter",
    [
        (BUG_FAILED_BUILD, L.STATUS_FAILED_GATE, "gated_out"),
        (BUG_NOT_GREEN, L.STATUS_FAILED_VERIFY, "not_kept"),
    ],
)
def test_a_rejected_bug_fix_maps_onto_the_shared_ledger_vocabulary(
    tmp_path, git, reason, expected_status, counter
):
    d = _make(tmp_path)
    d.gate = _Gate(bug_result=BugGateResult(passed=False, reason=reason, detail="why"))
    prop = _proposal(kind=TRACK_BUG)
    d._work_one_proposal(
        prop, base_sha="b", cycle=1, proposals=[prop], perf_survivors=[], bug_winners=[], gated_sha={}
    )
    fp = L.fingerprint(kind=TRACK_BUG, target="mod.py::sym")
    assert d.ledger._seen[fp].status == expected_status
    assert getattr(d.stats, counter) == 1


def test_a_perf_candidate_failing_the_gate_never_measures(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(result=GateResult(passed=False, detail="tests red"))
    d.measurer = _Measurer()
    survivors: list = []
    prop = _proposal()
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=survivors,
        bug_winners=[],
        gated_sha={},
    )
    assert survivors == []
    assert d.stats.gated_out == 1


def test_a_gated_perf_candidate_is_measured_and_pinned_to_its_sha(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(result=GateResult(passed=True, commit_sha="gated-1"))
    d.measurer = _Measurer()
    survivors: list = []
    gated: dict = {}
    prop = _proposal()
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=survivors,
        bug_winners=[],
        gated_sha=gated,
    )
    assert gated == {"c1": "gated-1"}
    assert len(survivors) == 1


# ─────────────────────────── the perf verdict ───────────────────────────


def test_no_keep_archives_every_survivor_with_its_real_discard_reason(tmp_path, git):
    d = _make(tmp_path)
    d.measurer = _Measurer()
    prop = _proposal()
    meas = _measurement(delta=-0.5)
    verdict = Verdict(keep=False, status="no_keep", reason="inside the band")
    assert d._apply_verdict(1, "base", verdict, [(prop, DISCARD_NOISE, meas)], 2, {}) == 2
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].status == L.STATUS_DISCARDED_NOISE
    assert d.stats.not_kept == 1
    assert (tmp_path / "results" / "candidates" / "c1.diff").exists()


def test_a_keep_whose_diff_will_not_apply_is_recorded_as_an_error(tmp_path, git):
    git.script("apply", 1)
    d = _make(tmp_path)
    d.measurer = _Measurer()
    prop = _proposal(diff=DIFF)
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")
    assert d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {"c1": "g"}) == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym", signature="sig")
    assert d.ledger._seen[fp].note == "winner diff did not apply to the working branch"


def test_a_filed_perf_win_advances_the_branch_and_announces_the_cr(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(
        CrOutcome(fp="fp-perf", status="filed", cr="CR-9", filed=True, reproduce=_measurement(-4.0))
    )
    git.script("rev-parse --short HEAD", (0, "kept1\n", ""))
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    assert d._apply_verdict(4, "basesha", verdict, [(prop, KEPT, meas)], 1, {"c1": "gated"}) == 1

    assert d.stats.kept == 1
    assert d.stats.filed == 1
    assert d.pr_pipeline.perf_kwargs["gated_commit_sha"] == "gated"
    assert d.pr_pipeline.perf_kwargs["base_anchor"] == "auto_improvement/feature @ basesha"
    filed = [e for e in events if "cr_filed" in e]
    assert filed[0]["cr_filed"]["cr"] == "CR-9"
    assert filed[0]["cr_filed"]["base_ref"] == "origin/feature"


def test_an_unreproduced_perf_keep_rolls_the_branch_back(tmp_path, git):
    d = _make(tmp_path)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp", status="failed_verify", filed=False))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")
    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})
    assert d.stats.kept == 0  # the keep did not become a reproduced win
    assert git.seen("reset --hard presha")


def test_a_direct_committed_perf_win_records_the_landed_sha(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(
        CrOutcome(fp="fp-c", status="committed", committed_ready=True, reproduce=_measurement())
    )
    git.script("rev-parse --short HEAD", (0, "shortsha\n", ""))
    d._direct_push = lambda **kwargs: True
    d.pushed_sha = "landed123"
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})

    entry = d.ledger._seen["fp-c"]
    assert entry.status == L.STATUS_COMMITTED
    assert entry.cr == "landed123"
    assert d.stats.filed == 1


def test_a_refused_direct_push_rolls_the_commit_back_and_unwinds_the_keep(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-r", status="error", committed_ready=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "short\n", ""))
    d._direct_push = lambda **kwargs: False
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})

    assert d.stats.kept == 0
    assert d.stats.filed == 0
    assert git.seen("reset --hard presha")


# ─────────────────────────── the bug verdict ───────────────────────────


def test_a_bug_fix_that_will_not_apply_is_recorded_as_an_error(tmp_path, git):
    git.script("apply", 1)
    git.script("apply --3way", 1)
    d = _make(tmp_path)
    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=DIFF), BugGateResult(passed=True, reason=BUG_FILED)
    )
    fp = L.fingerprint(kind=TRACK_BUG, target="mod.py::sym", signature="sig")
    assert d.ledger._seen[fp].note == "bug fix diff did not apply to the working branch"


def test_a_filed_bug_fix_files_then_returns_head_to_where_it_started(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-bug", status="filed", cr="CR-3", filed=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "bugshort\n", ""))

    d._apply_bug_winner(
        2, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    assert d.stats.kept == 1
    assert d.stats.filed == 1
    assert d.pr_pipeline.bug_kwargs["base_anchor"] == "auto_improvement/feature @ presha"
    assert [e for e in events if "cr_filed" in e][0]["cr_filed"]["kind"] == "bug"
    assert git.seen("reset --hard presha")


def test_a_direct_committed_bug_fix_records_committed_once(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-bc", status="committed", committed_ready=True))
    git.script("rev-parse --short HEAD", (0, "bshort\n", ""))
    d._direct_push = lambda **kwargs: True
    d.pushed_sha = ""

    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    entry = d.ledger._seen["fp-bc"]
    assert entry.status == L.STATUS_COMMITTED
    assert entry.cr == "bshort"  # fell back to the pre-push short sha
    assert (d.stats.kept, d.stats.filed) == (1, 1)


def test_a_refused_bug_push_rolls_back_without_touching_the_keep_counter(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-br", status="error", committed_ready=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "short\n", ""))
    d._direct_push = lambda **kwargs: False

    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    assert d.stats.kept == 0  # never incremented, so never decremented
    assert git.seen("reset --hard presha")


def test_an_unfiled_bug_fix_leaves_head_where_it_was(tmp_path, git):
    d = _make(tmp_path)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp", status="duplicate", filed=False))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )
    assert d.stats.filed == 0
    assert git.seen("reset --hard presha")


# ─────────────────────────── the per-cycle workflow ───────────────────────────


def test_a_cycle_with_no_candidates_returns_zero_fresh(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.proposer = _Proposer()
    assert d.run_cycle(1) == 0
    assert d.profile.discover_kwargs["base_sha"] == ""
    assert events[-1]["fresh"] == 0


def test_an_already_terminal_locus_is_deduped_before_any_expensive_work(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer()
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::hot")
    d.ledger.record(
        L.LedgerEntry(fp=fp, kind=TRACK_PERF, target="mod.py::hot", status=L.STATUS_FILED)
    )
    assert d.run_cycle(1) == 0
    assert d.stats.deduped == 1
    assert d.proposer.fan_out_kwargs == {}


def test_the_skip_list_is_a_cost_optimization_and_never_fatal(tmp_path, git):
    d = _make(tmp_path)
    d.proposer = _Proposer()
    d.ledger.terminal_targets = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no"))
    assert d.run_cycle(1) == 0


def test_the_discovery_rotation_and_skip_list_reach_the_profile(tmp_path, git):
    d = _make(tmp_path)
    d.proposer = _Proposer()
    d.run_cycle(7)
    assert d.profile._discovery_rotate == 7
    assert d.profile._skip_targets == []


def test_one_bad_candidate_never_kills_the_cycle(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    prop = _proposal("cX", target="mod.py::hot")
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer([prop])
    d.keeper = _Keeper()
    d.measurer = _Measurer()

    def _boom(*args, **kwargs):
        raise ValueError("gate exploded")

    d._work_one_proposal = _boom
    assert d.run_cycle(1) == 1
    assert d.stats.errors == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::hot")
    assert d.ledger._seen[fp].status == L.STATUS_ERROR
    assert d.ledger._seen[fp].note.startswith("ValueError:")
    assert d.proposer.torn_down == ["cX"]


def test_a_stop_request_aborts_the_proposal_loop(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    props = [_proposal("c1", target="mod.py::hot"), _proposal("c2", target="mod.py::hot")]
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer(props)
    d.keeper = _Keeper()
    d.measurer = _Measurer()
    worked: list = []
    d._work_one_proposal = lambda prop, **kwargs: worked.append(prop.cand_id)
    d.request_stop()
    d.run_cycle(1)
    assert worked == []
    assert sorted(d.proposer.torn_down) == ["c1", "c2"]


def test_two_bug_winners_on_one_locus_file_once(tmp_path, git):
    cand = Candidate(kind=TRACK_BUG, target="mod.py::bug")
    props = [
        _proposal("b1", kind=TRACK_BUG, target="mod.py::bug"),
        _proposal("b2", kind=TRACK_BUG, target="mod.py::bug"),
    ]
    d = _make(
        tmp_path,
        profile=_Profile(track=TRACK_BUG, discovery=DiscoveryResult(candidates=[cand])),
    )
    d.proposer = _Proposer(props)
    d.gate = _Gate(bug_result=BugGateResult(passed=True, reason=BUG_FILED))
    d.keeper = _Keeper()
    d.measurer = _Measurer()
    applied: list = []
    d._apply_bug_winner = lambda cycle, prop, bug_res: applied.append(prop.cand_id)

    d.run_cycle(1)

    assert applied == ["b1"]
    assert d.stats.deduped == 1


def test_the_keeper_is_told_the_rulers_improving_direction(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    d = _make(
        tmp_path,
        profile=_Profile(
            ruler=_Ruler(direction="maximize"), discovery=DiscoveryResult(candidates=[cand])
        ),
    )
    d.proposer = _Proposer([_proposal("c1", target="mod.py::hot")])
    d.gate = _Gate()
    d.measurer = _Measurer()
    d.keeper = _Keeper()
    d.run_cycle(1)
    assert d.keeper.direction == "maximize"


# ─────────────────────────── the durable loop ───────────────────────────


def _loop_driver(tmp_path, git, *, caps=None, profile=None, keeps=0, **kw):
    d = _make(tmp_path, caps=caps, profile=profile, **kw)
    d.run_cycle = lambda cycle: keeps
    return d


def test_a_dry_run_exercises_exactly_one_cycle(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    cycles: list = []
    d.run_cycle = lambda cycle: cycles.append(cycle) or 0
    stats = d.run(dry_run=True)
    assert stats.cycles == 1
    assert cycles == [1]
    meta = (tmp_path / "results" / "run.meta.json").read_text()
    assert '"profile_id": "fake-profile"' in meta


def test_the_run_meta_reads_head_when_the_clone_is_a_real_repo(tmp_path, git):
    git.script("rev-parse HEAD", (0, "basesha\n", ""))
    d = _loop_driver(tmp_path, git)
    (d.clone / ".git").mkdir()
    d.run(dry_run=True)
    assert '"base_sha": "basesha"' in (tmp_path / "results" / "run.meta.json").read_text()


def test_a_stop_request_before_the_loop_runs_no_cycle(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    d.request_stop()
    assert d.run(dry_run=True).cycles == 0


def test_the_time_budget_ends_the_run_cleanly(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0, 40000.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_hours=1.0))
    assert d.run(dry_run=False, preflight=False).cycles == 0


def test_the_cost_budget_ends_the_run_cleanly(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cost_usd=5.0), cost_meter=lambda: 99.0)
    stats = d.run(dry_run=False, preflight=False)
    assert stats.cycles == 0
    assert stats.cost_usd == 99.0


def test_quiescence_stops_a_mined_out_run(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    events: list = []
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=9, quiesce_after=2),
        on_progress=events.append,
    )
    stats = d.run(dry_run=False, preflight=False)
    assert stats.cycles == 2
    quiesce = [e for e in events if "quiescence" in e]
    assert quiesce[-1]["quiescence"] == {"cyclesSinceKeep": 2, "stopAt": 2}
    assert quiesce[-1]["budget"]["cycles_used"] == 2


def test_a_non_positive_quiesce_after_never_quiesces(tmp_path, git, monkeypatch):
    clock = _Clock([0.0])
    monkeypatch.setattr(drv, "time", clock)
    d = _loop_driver(
        tmp_path, git, caps=drv.BudgetCaps(max_cycles=3, quiesce_after=0, cycle_gap_s=0.25)
    )
    assert d.run(dry_run=False, preflight=False).cycles == 3
    assert clock.slept == [0.25, 0.25, 0.25]


def test_a_keep_resets_the_no_keep_streak(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cycles=2, quiesce_after=1))

    def _cycle(cycle):
        d.stats.kept += 1
        return 1

    d.run_cycle = _cycle
    assert d.run(dry_run=False, preflight=False).cycles == 2


def test_a_real_run_proves_the_ruler_before_the_loop(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    _stub_preflight(monkeypatch)
    events: list = []
    profile = _Profile(ruler=_Ruler(baselines={"boot": 120.0}))
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=profile,
        on_progress=events.append,
    )
    d.run(dry_run=False)
    pf = [e for e in events if "preflight" in e][0]["preflight"]
    assert pf == {
        "noise_band": 7.5,
        "baseline_n": 5,
        "canary_delta": -30.0,
        "guardrail_baselines": {"boot": 120.0},
    }


def test_a_ruler_without_baselines_still_reports_the_band(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    _stub_preflight(monkeypatch)
    events: list = []
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=_Profile(ruler=_SlottedRuler()),
        on_progress=events.append,
    )
    d.run(dry_run=False)
    pf = [e for e in events if "preflight" in e][0]["preflight"]
    assert pf["guardrail_baselines"] == {}


def test_the_bug_track_skips_the_ruler_preflight(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))

    def _never(*args, **kwargs):
        raise AssertionError("the bug track must not calibrate a noise band")

    monkeypatch.setattr(drv.PF, "calibrate_and_prove", _never)
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=_Profile(track=TRACK_BUG),
    )
    assert d.run(dry_run=False, preflight=True).cycles == 1


def test_preflight_can_be_forced_off_on_a_real_run(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    monkeypatch.setattr(
        drv.PF,
        "calibrate_and_prove",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cycles=1))
    assert d.run(dry_run=False, preflight=False).cycles == 1


def test_the_cycle_index_resumes_from_the_archive(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    d.archive.append_row({"cycle": 11, "cand_id": "old", "status": "kept"})
    seen: list = []
    d.run_cycle = lambda cycle: seen.append(cycle) or 0
    d.run(dry_run=True)
    assert seen == [12]


# ─────────────────────────── the CLI ───────────────────────────


def test_the_bare_cli_prints_a_dry_plan(capsys):
    assert drv.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY PLAN" in out
    assert "DRAFT (unpublished) CR" in out


def test_go_without_a_profile_explains_itself(capsys):
    assert drv.main(["--go"]) == 0
    assert "requires a configured Target Profile" in capsys.readouterr().out


def test_the_cli_forwards_budget_flags_into_the_dry_run(monkeypatch):
    seen: dict = {}

    def _dry(args, caps, log):
        seen.update({"caps": caps, "clone": args.clone})
        return 7

    monkeypatch.setattr(drv, "_run_dry", _dry)
    rc = drv.main(["--dry-run", "--max-cycles", "3", "--max-hours", "0.5", "--quiesce", "1"])
    assert rc == 7
    assert seen["caps"].max_cycles == 3
    assert seen["caps"].max_hours == 0.5
    assert seen["caps"].quiesce_after == 1


def test_the_cli_reads_sys_argv_when_given_no_list(monkeypatch):
    monkeypatch.setattr(drv.sys, "argv", ["driver", "--dry-run"])
    monkeypatch.setattr(drv, "_run_dry", lambda args, caps, log: 3)
    assert drv.main(None) == 3


def test_build_logger_is_idempotent():
    first = drv._build_logger()
    assert len(first.handlers) == 1
    assert drv._build_logger() is first
    assert len(first.handlers) == 1


def test_run_dry_builds_a_throwaway_clone_and_honors_data_dir(tmp_path, git, monkeypatch, capsys):
    root = tmp_path / "ephemeral"
    root.mkdir()
    monkeypatch.setattr(drv, "tempfile", types.SimpleNamespace(mkdtemp=lambda prefix="": str(root)))
    monkeypatch.setattr(drv.Driver, "run", lambda self, **kwargs: drv.Stats(cycles=1))
    args = types.SimpleNamespace(data_dir=tmp_path / "data")

    assert drv._run_dry(args, drv.BudgetCaps(max_cycles=1), LOG) == 0

    assert (root / "clone" / "src" / "mesh_pkg" / "__init__.py").exists()
    assert git.seen("init -q -b auto_improvement/trunk-base")
    assert git.seen("remote add origin DISABLED_NO_PUSH")
    out = capsys.readouterr().out
    assert str(tmp_path / "data" / "results") in out


def test_run_dry_falls_back_to_an_ephemeral_data_dir(tmp_path, git, monkeypatch, capsys):
    root = tmp_path / "ephemeral2"
    root.mkdir()
    monkeypatch.setattr(drv, "tempfile", types.SimpleNamespace(mkdtemp=lambda prefix="": str(root)))
    monkeypatch.setattr(drv.Driver, "run", lambda self, **kwargs: drv.Stats())
    args = types.SimpleNamespace(data_dir=None)

    assert drv._run_dry(args, drv.BudgetCaps(), LOG) == 0
    assert str(root / "data" / "results") in capsys.readouterr().out
