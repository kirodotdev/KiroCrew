"""Fakes, builders and fixtures shared by the auto-improvement spine driver tests.

``test_ai_spine_driver_coverage.py`` and ``test_ai_spine_driver_coverage_cycle.py``
import these by name: the :class:`_Git` recorder that stands in for both git
surfaces, the hand-rolled profile / proposer / gate / measurer / keeper / CR-pipeline
fakes, the measurement and proposal builders, the ``_make`` driver builder, the
preflight stub, the autouse ``_isolate_env`` fixture and the ``git`` fixture. An
autouse fixture applies only in a module that imports it, so both test modules import
``_isolate_env``. The ``git`` fixture is declared ``name="git"`` on ``git_fixture`` so
no test parameter shadows the import.
"""

from __future__ import annotations

import logging
import subprocess
import types
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_FILED,
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
        self.configs: list[tuple[str, tuple[str, ...]]] = []
        self._script: dict[str, list[tuple[int, str, str]]] = {}
        # PRECONDITIONS OF THE RETRY PATH, answered by default. Before replaying, it reads the
        # authorized object's own committer identity to pin the replay to it; afterwards it
        # compares the replayed commit's author, committer and message against that object's.
        # Both run on every retry regardless of what a test is about, and an unscripted key
        # returns empty stdout -- which those checks correctly read as "cannot tell" and
        # refuse, failing every test in the class for a reason unrelated to its subject. One
        # answer for both revisions, so the comparison is equal unless a test overrides it.
        self._script["log -1 --format=%cn%x00%ce"] = [
            (0, "pipeline\x00pipeline@example.invalid\n", "")
        ]
        self._script["log -1 --format=%an"] = [
            (
                0,
                "author\x00author@example.invalid\x00pipeline"
                "\x00pipeline@example.invalid\x00the authorized change\n",
                "",
            )
        ]

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

    def git(self, args, cwd, **kwargs):
        toks = [str(a) for a in args]
        clean: list[str] = []
        index = 0
        while index < len(toks):
            if toks[index] == "-c":
                index += 2
                continue
            clean.append(toks[index])
            index += 1
        joined = " ".join(clean)
        self.calls.append(joined)
        # ``config`` arrives by KEYWORD, so it is absent from *args* and invisible to the
        # scripting keys. Recorded separately, keyed by the same joined string, so a test can
        # assert what a call was pinned to without the pins disturbing how results are matched.
        self.configs.append((joined, tuple(str(c) for c in kwargs.get("config", ()))))
        rc, out, err = self._take(joined)
        return subprocess.CompletedProcess(args=list(args), returncode=rc, stdout=out, stderr=err)

    def config_for(self, prefix: str) -> tuple[str, ...]:
        """The ``-c`` pins the first call matching *prefix* was given."""
        for joined, config in self.configs:
            if joined.startswith(prefix):
                return config
        return ()

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

#: A full 40-char lowercase-hex commit id. The finalizers hand back a validated forty-hex
#: object id and `_direct_push` validates its `sha` the same way, so the id a test passes in
#: and the id the clone resolves to must both be real object ids, never an abbreviation.
COMMITTED_HEAD = "c0ffee" + "0" * 34


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in a module that imports this fixture calls monkeypatch.undo()
def _isolate_env(tmp_path, monkeypatch):
    """No real home, no inherited fan-out overrides, no leaked log handlers."""
    from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    for var in ("AUTO_IMPROVEMENT_WIDE", "AUTO_IMPROVEMENT_DEEP"):
        monkeypatch.delenv(var, raising=False)
    named = logging.getLogger("auto_improvement.driver")
    before = list(named.handlers)
    yield
    named.handlers[:] = before


@pytest.fixture(name="git")
def git_fixture(monkeypatch):
    g = _Git()
    monkeypatch.setattr(drv, "_git", g.git)
    monkeypatch.setattr(drv, "subprocess", types.SimpleNamespace(run=g.run))
    monkeypatch.setattr(drv, "require_pinned", lambda cwd: None)
    # `_direct_push`'s HEAD-identity gate resolves the clone's HEAD with `rev-list -1`
    # and refuses when it differs from the full object id it was handed. Scripted here
    # rather than per test because every direct-push test needs the SAME answer, and the
    # default for an unscripted key is rc 0 with EMPTY stdout, which that gate correctly
    # reads as "cannot resolve" and fails closed on. A test that wants to exercise the
    # refusal overrides this with its own longer-prefix script.
    g.script("rev-list -1", (0, f"{COMMITTED_HEAD}\n", ""))
    # And the retry proves the rebase replayed exactly ONE commit; the longer
    # prefix wins over `rev-list -1`, so the two questions stay separable.
    g.script("rev-list --count", (0, "1\n", ""))
    # The retry captures the fetched tip's object id from `git fetch --porcelain` STDOUT rather
    # than from the mutable `FETCH_HEAD` ref, so the fetch has to report one; an
    # unscripted fetch yields empty stdout, which the capture correctly reads as a refusal.
    g.script("fetch --porcelain", (0, f"* {'0' * 40} {'ba5e' + '0' * 36} FETCH_HEAD\n", ""))
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
