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

The fakes, builders and fixtures live in ``ai_spine_driver_coverage_helpers.py``; one
proposal's track, the verdicts, the per-cycle workflow, the loop and the CLI are in
``test_ai_spine_driver_coverage_cycle.py``.
"""

from __future__ import annotations

import logging
import subprocess
import types
from pathlib import Path

import pytest
from ai_spine_driver_coverage_helpers import _isolate_env  # noqa: F401
from ai_spine_driver_coverage_helpers import git_fixture  # noqa: F401
from ai_spine_driver_coverage_helpers import (
    COMMITTED_HEAD,
    DIFF,
    LOG,
    _AgentRunner,
    _BugRunner,
    _BuildGate,
    _Isolation,
    _make,
    _measurement,
    _Profile,
    _proposal,
    _Ruler,
    _SlottedCalib,
    _SlottedRuler,
    _stub_preflight,
)

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L
from kiro_crew.apps.builtins.auto_improvement.spine import push_policy as PP
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_FILED,
    TRACK_BUG,
    TRACK_PERF,
    BugGateResult,
)
from kiro_crew.apps.builtins.auto_improvement.spine.preflight import PreflightResult

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


def test_direct_commit_never_bypasses_unsafe_repository(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: False)
    d = _make(
        tmp_path,
        profile=_Profile(isolation=_Isolation(disabled=False)),
        direct_commit=True,
    )
    with pytest.raises(drv.PushEnabledError, match="repository metadata"):
        d.assert_push_disabled()


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
    d = _make(
        tmp_path, profile=_Profile(isolation=_Isolation(boot=lambda: None)), boot_callable=boot
    )
    d.preflight()
    assert seen["boot"] is boot


@pytest.mark.parametrize("raises", [False, True])
def test_preflight_retires_before_interpreting_success_or_failure(tmp_path, monkeypatch, raises):
    d = _make(tmp_path)
    result = PreflightResult(ok=True, noise_band=1.0)

    def _preflight():
        if raises:
            raise RuntimeError("preflight failed")
        return result

    retired: list[str] = []
    d.preflight = _preflight
    d._retire_if_unsafe = lambda stage: retired.append(stage) or True

    assert d._preflight_checked() is None
    assert retired == ["perf preflight"]


def test_post_execution_live_urls_retire_the_clone(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

    d = _make(tmp_path)
    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: False)
    retained = tmp_path / ".clone.unsafe" / "clone"
    monkeypatch.setattr(clone_setup, "_retire_unsafe_clone", lambda _clone: retained)

    assert d._retire_if_unsafe("agent") is True
    assert d._repository_retired is True and d._stop is True


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
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "mod.py::sym", COMMITTED_HEAD
        ).returncode
        == 0
    )
    assert git.seen("fetch") == []


def test_a_non_race_push_failure_is_returned_untouched(tmp_path, git):
    git.script("push", (1, "", "fatal: authentication failed"))
    d = _make(tmp_path)
    res = d._push_with_rebase(
        "https://example.invalid/r.git", "feature", "mod.py::sym", COMMITTED_HEAD
    )
    assert res.returncode == 1
    assert git.seen("fetch") == []  # no retry masks a real error


def test_a_lost_race_with_a_failing_fetch_returns_the_rejection(tmp_path, git):
    git.script("push", (1, "", "! [rejected] non-fast-forward"))
    git.script("fetch --porcelain", 1)
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert git.seen("rebase") == []


def test_a_conflicting_rebase_aborts_and_does_not_push(tmp_path, git):
    git.script("push", (1, "", "fetch first"))
    git.script("rebase", 1)
    git.script("rebase --abort", 0)
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert git.seen("rebase --abort")


def test_an_unverifiable_rebased_tree_is_not_published(tmp_path, git):
    git.script("push", (1, "", "non-fast-forward"))
    git.script("rebase", 0)
    d = _make(tmp_path, profile=_Profile(build_gate=_BuildGate(passed=False)))
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert len(git.seen("push")) == 1  # never pushed a second time


def test_a_reverified_rebase_retries_the_push_once(tmp_path, git):
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    _script_matching_replay(git)
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 0
    )
    assert len(git.seen("push")) == 2


def _script_matching_replay(git, *, tree: str = "a" * 40) -> None:
    """Make the replay's tree equal the tree a replay of the source produces.

    ``merge-tree --write-tree`` states the expected tree from the two immutable ids, and
    ``log -1 --format=%T`` reads the tree the captured commit actually carries; equal means
    the published content is the authorized content.
    """
    git.script("merge-tree --write-tree", (0, f"{tree}\n", ""))
    git.script("log -1 --format=%T", (0, f"{tree}\n", ""))


def test_a_replay_carrying_the_authorized_tree_is_published(tmp_path, git):
    """The gate is a binding, not a blanket refusal: a replay whose tree matches passes."""
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    _script_matching_replay(git)
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 0
    )
    assert git.seen("merge-tree --write-tree"), "the expected replay tree was never computed"


def test_a_replay_whose_attribution_differs_is_not_published(tmp_path, git):
    """A replay writes a NEW commit, and git takes its committer from configuration.

    Author and message carry over, but the committer does not, so a process able to write the
    repository's config -- the reviewing agent's shell, whose git denylist covers only `push`
    and `remote set-url` -- chooses who the published commit says committed it. The retry pins
    the replay's committer to the identity the authorized object already holds, and this is the
    check behind that pin: the ``GIT_COMMITTER_*`` environment outranks a ``-c`` override, so
    the published object's attribution is compared against the authorized one rather than
    assumed. Content passing every other gate is exactly the case this catches -- the tree
    matches here, so nothing else in the path objects.
    """
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    _script_matching_replay(git)
    # Same key, two answers: the authorized object's, then the replay's. A queue pops in call
    # order, and the retry reads `src` first.
    git.script(
        "log -1 --format=%an",
        (0, "a\x00a@example.invalid\x00pipeline\x00pipeline@example.invalid\x00m\n", ""),
        (0, "a\x00a@example.invalid\x00intern\x00intern@example.invalid\x00m\n", ""),
    )
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert len(git.seen("push")) == 1, "a falsely attributed commit was published"


def test_the_replay_pins_its_committer_to_the_authorized_object(tmp_path, git):
    """The pin is what keeps an honest retry working rather than merely refusing.

    Refusing on a mismatch alone would hand the agent a denial of service: writing one config
    value would make every retry decline, and the retry exists because a moving branch lost
    gate survivors. Reading the identity off `src` -- an immutable object the pipeline
    committed before any agent ran -- needs no config to be trustworthy at replay time.
    """
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    _script_matching_replay(git)
    d = _make(tmp_path)
    d._push_with_rebase("https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD)
    rebases = [c for c in git.calls if c.startswith("rebase")]
    assert rebases, "the replay never ran"
    pins = git.config_for("rebase")
    assert "user.name=pipeline" in pins, pins
    assert "user.email=pipeline@example.invalid" in pins, pins


def test_a_replay_carrying_a_different_tree_is_not_published(tmp_path, git):
    """A commit substituted into HEAD before the capture is caught by the tree comparison.

    Every later check binds to the captured id, so they all agree ABOUT THE SUBSTITUTE --
    including the HEAD-equality check, which compares HEAD against that same id. Only
    reaching back to what a replay of the authorized `src` must produce refuses it.
    """
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    git.script("merge-tree --write-tree", (0, "a" * 40 + "\n", ""))
    git.script("log -1 --format=%T", (0, "b" * 40 + "\n", ""))
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert len(git.seen("push")) == 1, "a substituted commit was published"


def test_the_same_lines_in_a_different_place_are_a_different_tree(tmp_path, git):
    """The reason this is a tree and not a patch identity.

    A patch identity ignores hunk positions, so a commit whose added and removed lines match
    the authorized change while sitting elsewhere in the file carries the SAME identity. Its
    tree differs, so the tree comparison refuses it where a patch identity would not.
    """
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    # Same change, relocated: the merge result and the captured commit disagree on content.
    git.script("merge-tree --write-tree", (0, "c" * 40 + "\n", ""))
    git.script("log -1 --format=%T", (0, "d" * 40 + "\n", ""))
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert len(git.seen("push")) == 1


def test_an_uncomputable_expected_tree_refuses_the_publish(tmp_path, git):
    """Fail-closed: a conflicted or unsupported merge prints no usable tree, and two empty
    answers would compare EQUAL, so emptiness must refuse."""
    git.script("push", (1, "", "non-fast-forward"), (0, "", ""))
    git.script("rebase", 0)
    git.script("merge-tree --write-tree", (1, "", "CONFLICT"))
    git.script("log -1 --format=%T", (0, "a" * 40 + "\n", ""))
    d = _make(tmp_path)
    assert (
        d._push_with_rebase(
            "https://example.invalid/r.git", "feature", "t", COMMITTED_HEAD
        ).returncode
        == 1
    )
    assert len(git.seen("push")) == 1


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
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert d.ledger._seen["fp"].note == "direct-push: no usable remote url"


def test_direct_push_refuses_when_the_pushable_blobs_cannot_be_listed(tmp_path, git):
    # A git READ that fails must refuse rather than be read as nothing to scan. The scan
    # lists the revision's own changed blobs and reads each one, so both reads are load-
    # bearing: a failing `diff-tree` cannot enumerate what to scan, and a failing
    # `cat-file blob` leaves a real blob unread. The `_Git` double matches by argv prefix
    # (longest wins), so `diff-tree` and `cat-file blob` are scripted independently.
    # `COMMITTED_HEAD` is what the shared `git` fixture scripts `rev-list -1` to return.
    git.script("diff-tree", (128, "", "fatal"))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert d.ledger._seen["fp"].note == ("direct-push refused: could not list the pushable blobs")
    assert git.seen("push") == []


def test_direct_push_refuses_when_a_pushable_blob_cannot_be_read(tmp_path, git):
    # The listing enumerates one added blob, but reading its raw bytes fails. An unreadable
    # blob is the absence of the scan, not a clean scan, so the push is refused.
    git.script(
        "diff-tree",
        (0, ":000000 100644 " + "0" * 40 + " " + "d" * 40 + " A\tm.py\n", ""),
    )
    git.script("cat-file blob", (128, "", "fatal"))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert d.ledger._seen["fp"].note == ("direct-push refused: could not read a pushable blob")
    assert git.seen("push") == []


def test_direct_push_scans_a_root_commit_through_the_single_listing(tmp_path, git):
    # A root commit has no parent, and `diff-tree --root` lists its blobs in the SAME
    # listing every other commit goes through -- so a root commit is still fully scanned
    # with no separate branch. Each listed blob's raw bytes are read and scanned clean.
    git.script(
        "diff-tree",
        (0, ":000000 100644 " + "0" * 40 + " " + "e" * 40 + " A\tm.py\n", ""),
    )
    git.script("cat-file blob", (0, "clean content\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is True
    assert git.seen("diff-tree")
    assert git.seen("cat-file blob " + "e" * 40)


def test_direct_push_scans_a_binary_blobs_raw_bytes_and_refuses_a_credential(
    tmp_path, git, monkeypatch
):
    # THE HOLE THIS CLOSES. `git diff` renders a blob it judges binary as one summary line,
    # `Binary files a and b differ`, with none of its content -- and a single NUL byte is
    # enough to make git judge a blob binary, which whatever writes the tree chooses freely.
    # A scan that read diff output would see that summary line and zero occurrences of the
    # key. The push transfers OBJECTS, not a diff, so the scan reads each changed blob's raw
    # bytes with `cat-file blob`; a credential in those bytes must refuse the push.
    seen_blob_scan: list = []

    def _scan(text):
        # Record only whether the raw blob bytes reached the scanner, never the text itself.
        if "AKIAIOSFODNN7EXAMPLE" in text:
            seen_blob_scan.append(True)
            return False, PP.SCAN_HIT
        return True, PP.SCAN_OK

    monkeypatch.setattr(PP, "scan_content_for_secrets", _scan)
    # A realistic added-blob listing line; the raw blob is binary (NUL-wrapped) and carries
    # the key, exactly the shape a rendered diff would collapse to "Binary files ... differ".
    git.script(
        "diff-tree",
        (0, ":000000 100644 " + "0" * 40 + " " + "f" * 40 + " A\tsecret.bin\n", ""),
    )
    git.script("cat-file blob", (0, "\x00AKIAIOSFODNN7EXAMPLE\x00", ""))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert d.ledger._seen["fp"].note == (
        "direct-push refused: content scan found credential/exfiltration finding(s)"
    )
    assert git.seen("cat-file blob " + "f" * 40)
    # NON-VACUOUS: the refusal happened because the scanner actually SAW the raw blob bytes,
    # not because nothing was scanned.
    assert seen_blob_scan == [True], "the raw blob bytes never reached the scanner"
    assert git.seen("push") == []


def test_direct_push_refuses_content_the_scanner_flags(tmp_path, git, monkeypatch):
    monkeypatch.setattr(PP, "scan_content_for_secrets", lambda text: (False, PP.SCAN_HIT))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert d.ledger._seen["fp"].note == (
        "direct-push refused: content scan found credential/exfiltration finding(s)"
    )
    assert git.seen("push") == []


def test_direct_push_records_a_failed_push(tmp_path, git):
    git.script("push", (1, "", "remote rejected"))
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is False
    assert "direct-push failed" in d.ledger._seen["fp"].note
    # The recorded sha is the OBJECT THAT WAS SENT -- the full object id the gate retained
    # and pushed -- not a later re-read of `HEAD`.
    assert d.pushed_sha == COMMITTED_HEAD


def test_direct_push_reports_the_sha_that_actually_landed(tmp_path, git):
    """The sha recorded must be the object the push transferred.

    `_push_with_rebase` reports the revision it sent, and the ledger follows that rather than
    a post-push `rev-parse HEAD`: reading HEAD after the push would name whatever HEAD points
    at by then, so a concurrent move could put an unrelated commit in the ledger for a change
    that really did land. `rev-parse HEAD` is deliberately scripted to a DIFFERENT value here,
    so a regression to reading the ref reddens.
    """
    git.script("rev-parse HEAD", (0, "someotherhead\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is True
    assert d.pushed_sha == COMMITTED_HEAD
    assert d.pushed_sha != "someotherhead", "the ledger followed the ref, not the pushed object"


def test_direct_push_reporting_survives_a_blank_rev_parse(tmp_path, git):
    """Pins that this path does not fall back to the caller's snapshot when `rev-parse HEAD`
    blanks. That fallback is unreachable here, because the recorded sha comes from the object
    that was sent rather than from a post-push ref read -- which is a stronger version of the
    same guarantee ("a reporting hiccup cannot blank a real sha"). The fallback itself remains
    for callers that do not pass an explicit source.
    """
    git.script("rev-parse HEAD", (0, "   \n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path)
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is True
    assert d.pushed_sha == COMMITTED_HEAD, "a blank ref read must not affect the recorded sha"


def test_direct_push_reads_the_fetch_url_off_the_clone_when_the_profile_has_none(tmp_path, git):
    git.script("remote get-url", (0, "https://example.invalid/from-clone.git\n", ""))
    git.script("push", 0)
    d = _direct_push_driver(tmp_path, profile=_Profile(fetch_url=""))
    assert d._direct_push(fp="fp", kind="perf", target="t", sha=COMMITTED_HEAD) is True
    pushes = git.seen("push")
    # The refspec source is the OBJECT ID the gate retained, not `HEAD`: an id cannot
    # be repointed, so nothing in the clone can change what is published after the check.
    # `COMMITTED_HEAD` is what the shared `git` fixture scripts `rev-list -1` to return.
    assert pushes == [
        f"push https://example.invalid/from-clone.git {COMMITTED_HEAD}:refs/heads/auto_improvement/feature"
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


@pytest.mark.parametrize(
    "stdout,expect",
    [
        # The real shape, measured against git 2.50: `<flag> <old-oid> <new-oid> <local-ref>`.
        (f"* {'0' * 40} {'a' * 40} FETCH_HEAD\n", "a" * 40),
        # A deletion reports the NULL id, which is not a tip anything can rebase onto.
        (f"- {'a' * 40} {'0' * 40} FETCH_HEAD\n", ""),
        # Nothing usable: an older git that rejected --porcelain, or a silent fetch.
        ("", ""),
        ("From https://example.invalid/r.git\n * branch main -> FETCH_HEAD\n", ""),
        # Not an object id in the id column.
        (f"* {'0' * 40} refs/heads/main FETCH_HEAD\n", ""),
        # Short of forty hex, so not an id either.
        (f"* {'0' * 40} {'a' * 39} FETCH_HEAD\n", ""),
    ],
)
def test_fetched_tip_oid_only_accepts_a_real_object_id(stdout, expect):
    """The retry rebases onto the id the FETCH reported, never onto `FETCH_HEAD` -- that ref is a
    mutable file the pre-push reviewer can repoint at a commit nothing scanned, so neither the
    rebase nor the replayed-commit count reads it. Anything this cannot parse must come
    back empty so the caller refuses: an id it could not obtain is the absence of the check."""
    assert drv._fetched_tip_oid(stdout) == expect


def test_reset_provisional_does_nothing_without_a_pre_sha(tmp_path, git):
    """REPLACES a test that asserted no `reset --hard` when nothing advanced. The rollback is
    now a single idempotent `checkout -f -B <branch> <pre_sha>`, so "nothing advanced" needs no
    special case -- but an ABSENT pre_sha must still touch nothing at all."""
    d = _make(tmp_path)
    d._reset_provisional("")
    assert git.seen("checkout") == [], "it moved a ref with no sha to roll back to"


def test_reset_provisional_rolls_the_branch_back_atomically(tmp_path, git):
    """ONE command that NAMES the branch. `git reset --hard` acts on whatever is checked out,
    and this runs after the pre-push reviewer has had a shell in the clone where `git checkout`
    is permitted -- so a read-then-reset pair lets a backgrounded `setsid git checkout victim`
    land in between and destroy commits on `victim`. An intermediate fix read HEAD, checked the
    branch out if it differed, then reset, which was the same pair one level up. Raised across
    three rounds of the GPT review of this branch."""
    d = _make(tmp_path)
    d._reset_provisional("oldsha")
    moves = git.seen("checkout")
    assert moves == [
        "checkout -f -B auto_improvement/feature oldsha"
    ], f"the rollback is not a single branch-naming command: {moves}"
    # Nothing that acts on "whatever is checked out", and no read whose answer could go stale
    # before the write, may remain.
    assert git.seen("reset --hard") == [], "a reset on the current branch is back"
    assert git.seen("rev-parse --abbrev-ref") == [], "it reads HEAD then acts -- a stale check"


def test_restore_branch_undoes_the_promotion_when_the_checkout_fails(tmp_path, git, caplog):
    """`git branch -f` lands BEFORE the checkout can fail, so a concurrent index lock would
    otherwise leave the branch promoted to a replay this reports as unrestored -- the push
    aborts and the unpushed commit stays on the durable branch. Raised by the GPT review."""
    git.script("rev-parse b", (0, "wasthere\n", ""))
    git.script("checkout", (1, "", "index.lock exists"))
    d = _make(tmp_path)
    assert d._restore_branch("b", promote="replaysha") is False
    moves = git.seen("branch -f")
    assert "branch -f b replaysha" in moves, moves
    assert "branch -f b wasthere" in moves, "the promotion was not undone after the failure"


def test_a_failed_rollback_quarantines_the_clone_so_a_later_run_cannot_adopt_it(tmp_path, git):
    """THE GUARD MUST OUTLIVE THE PROCESS, because the thing it guards does.

    `_rollback_failed` stops the run it is set in, but the un-rolled-back commit is on DISK and
    the clone is REUSED: a later run starts with a clear latch on a clone that still carries the
    refused commit, commits the next winner on top, and publishes an ancestor its single-revision
    scan never looked at. An in-memory guard is checking something shorter-lived than the hazard.

    This crosses the boundary that matters rather than re-proving the in-run halt: it fails a
    rollback, then asserts the predicate a LATER run's clone setup branches on. Reuse in
    `clone_setup._setup_safe_clone` hinges on the canonical `<dest>/.git` being a directory, so
    once retirement has renamed the clone aside, the next run cannot adopt it and clones fresh.
    A fresh Driver on the same path is built to make the point explicit: its latch is clear, and
    that is exactly why the protection cannot live there. Raised by the GPT review of this branch;
    quarantine was the conductor's choice over a persisted latch, whose clearing condition would
    itself have to be got right."""
    d = _make(tmp_path)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    assert (clone / ".git").is_dir(), "precondition: the clone looks reusable"
    git.script("checkout -f -B", (1, "", "cannot checkout"))

    assert d._reset_provisional("oldsha") is False
    assert d._repository_retired is True, "the clone was not quarantined"

    # The predicate `_setup_safe_clone` branches on for reuse (clone_setup.py: `git_dir.is_dir()`).
    assert not (clone / ".git").is_dir(), (
        "the canonical clone still looks reusable, so a later run would adopt the commit that "
        "was refused and never published"
    )
    retirements = [p for p in tmp_path.iterdir() if p.name.startswith(".clone.unsafe-")]
    assert retirements, f"no retirement container beside the clone: {list(tmp_path.iterdir())}"
    assert (retirements[0] / "clone").is_dir(), "the bytes were not preserved for diagnosis"

    # A NEW run is a new Driver with a CLEAR latch -- which is the whole reason the protection
    # cannot be the latch alone.
    fresh = _make(tmp_path)
    assert fresh._rollback_failed is False, "the in-memory latch does not survive, as expected"


def test_a_second_rollback_after_quarantine_is_a_no_op_and_does_not_mislead(tmp_path, git, caplog):
    """`_reset_provisional` is called from five sites, so it can be reached again after the clone
    has already been renamed aside. Without the already-retired short circuit it would try to
    check out a path that is gone, fail, and then report "left unsafe in place" -- which
    is false and is exactly the wrong thing to tell whoever is reading the log during an
    incident, because the clone WAS quarantined properly."""
    d = _make(tmp_path)
    (tmp_path / "clone" / ".git").mkdir(parents=True, exist_ok=True)
    git.script("checkout -f -B", (1, "", "cannot checkout"))
    assert d._reset_provisional("oldsha") is False
    before = len(git.seen("checkout"))
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        caplog.clear()
        assert d._reset_provisional("oldsha") is False, "a retired clone reported a good rollback"
    assert len(git.seen("checkout")) == before, "it tried to roll back a clone that is gone"
    assert "left unsafe in place" not in caplog.text, (
        "it reported the clone as unquarantined after having quarantined it -- the opposite of "
        "what an operator needs during an incident"
    )


def test_a_failed_rollback_halts_the_run_and_says_why(tmp_path, git, caplog):
    """A failed rollback is not a log line. HEAD keeps the commit that was refused and never
    published, so the NEXT winner commits on top of it and that winner's single-revision scan
    (`<rev>~1..<rev>`) cannot see the parent its own push would publish -- the refused content
    lands through a scanner that never looked at it. Nothing local repairs that, since the
    rollback IS the repair, so the run stops. Raised by the GPT review of this branch."""
    git.script("checkout -f -B", (1, "", "cannot checkout"))
    d = _make(tmp_path)
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        assert d._reset_provisional("oldsha") is False, "a failed rollback reported success"
    assert d._rollback_failed is True, "the failure did not latch"
    assert d._stop is True, "the run was allowed to continue after an unrepairable HEAD"
    assert "could not roll back the provisional commit" in caplog.text
    assert "refused and never published" in caplog.text


def test_a_successful_rollback_reports_success_and_does_not_halt(tmp_path, git):
    """The positive control: the latch must not fire on the ordinary path, or the flag would
    read as "always broken" and prove nothing."""
    d = _make(tmp_path)
    assert d._reset_provisional("oldsha") is True
    assert d._rollback_failed is False
    assert d._stop is False


def test_a_failed_rollback_stops_every_further_winner_before_any_push(tmp_path, git, caplog):
    """The halt has to be UNCONDITIONAL, and the publish gate alone is not enough.

    `_apply_bug_winner` calls `pr_pipeline.emit_bug` BEFORE `_direct_push`, and that path reaches
    `pr_recipe._push_fix_branch`, which pushes `HEAD:refs/heads/<branch>` and knows nothing about
    the latch. So a second bug winner in the SAME cycle would publish the un-rolled-back commit
    through the PR branch before the direct-push gate was ever consulted. Guarded at the top of
    both winner-applying methods, which is upstream of every push either one can reach.

    The control is structural: `archive.save_candidate` is the first thing both methods do, so a
    double that raises proves the guard returned before any work -- and reddens if it is removed.
    """
    d = _make(tmp_path)
    d._rollback_failed = True

    def _boom(**_kw):
        raise AssertionError("a winner was applied after the rollback failed")

    d.archive = types.SimpleNamespace(save_candidate=_boom)  # type: ignore[assignment]
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        assert d._apply_verdict(1, "basesha", None, [], 0, "") == 0
        assert d._apply_bug_winner(1, None, None) is None
    assert "refusing to apply any further winner" in caplog.text


def test_direct_push_refuses_after_a_failed_rollback(tmp_path, git):
    """Checked at the PUBLISH gate, not only through the run's stop flag: that flag is read at
    cycle boundaries, and one cycle can hold SEVERAL bug winners (`for prop, bug_res in
    bug_winners`), each reaching `_direct_push`. So the second winner in the same cycle is
    exactly the case a stop-flag-only fix would miss."""
    d = _direct_push_driver(tmp_path)
    d._rollback_failed = True
    assert d._direct_push(fp="fp", kind="bug", target="t", sha="abc") is False
    assert "provisional rollback failed" in d.ledger._seen["fp"].note
    assert git.seen("push") == [], "it published from a clone with an unpublished commit at HEAD"


def test_reset_provisional_logs_a_failed_rollback(tmp_path, git, caplog):
    """Fail closed and SAY SO: a provisional commit left behind is resolved by the next cycle's
    stage step, but it must not pass silently."""
    git.script("checkout -f -B", (1, "", "cannot checkout"))
    d = _make(tmp_path)
    with caplog.at_level(logging.ERROR, logger=LOG.name):
        d._reset_provisional("oldsha")
    assert "could not roll back the provisional commit" in caplog.text


def test_finalize_winner_commit_skips_the_amend_for_an_empty_diff(tmp_path, git):
    git.script("rev-parse HEAD", (0, "a" * 40 + "\n", ""))
    d = _make(tmp_path)
    got = d._finalize_winner_commit(
        _proposal(diff=""), verify=_measurement(), cycle=1, diff_ref="d"
    )
    assert got == "a" * 40
    assert git.seen("commit -q --amend") == []


def test_finalize_winner_commit_amends_with_the_reproduce_numbers(tmp_path, git, monkeypatch):
    seen: dict = {}

    def _msg(**kwargs):
        seen.update(kwargs)
        return "perf: real numbers"

    monkeypatch.setattr(drv.D, "perf_commit_message", _msg)
    git.script("rev-parse HEAD", (0, "b" * 40 + "\n", ""))
    d = _make(tmp_path)
    reproduce = _measurement(delta=-4.0)
    got = d._finalize_winner_commit(
        _proposal(diff=DIFF), verify=_measurement(), reproduce=reproduce, cycle=3, diff_ref="d.diff"
    )
    assert got == "b" * 40
    assert seen["reproduce"] is reproduce
    assert git.seen("commit -q --amend")


def test_finalize_winner_commit_falls_back_to_verify_without_a_reproduce(
    tmp_path, git, monkeypatch
):
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
    git.script("rev-parse HEAD", (0, "c" * 40 + "\n", ""))
    d = _make(tmp_path)
    got = d._finalize_bug_winner_commit(
        _proposal(kind=TRACK_BUG, diff=DIFF),
        bug_res=BugGateResult(passed=True, reason=BUG_FILED),
        cycle=2,
        diff_ref="d.diff",
    )
    assert got == "c" * 40
    assert git.seen("commit -q --amend")


def test_finalize_bug_commit_skips_the_amend_for_an_empty_diff(tmp_path, git):
    git.script("rev-parse HEAD", (0, "d" * 40 + "\n", ""))
    d = _make(tmp_path)
    got = d._finalize_bug_winner_commit(
        _proposal(kind=TRACK_BUG, diff=""),
        bug_res=BugGateResult(passed=True, reason=BUG_FILED),
        cycle=1,
        diff_ref="d",
    )
    assert got == "d" * 40
    assert git.seen("commit -q --amend") == []
