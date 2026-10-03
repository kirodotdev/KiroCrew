"""The Fast Gate barrier: what `await-fast-gate` must guarantee for the split to be safe.

The cheap blocking gates live in ``.github/workflows/fast-gate.yml`` so that
two consumers can key on them before the expensive work starts: ci.yml's heavy jobs
wait through ``await-fast-gate``, and the five fork reviewers trigger on the
workflow's completion. A ``needs:`` edge cannot cross a workflow file, so that
barrier is a job that READS the other workflow's run -- and everything that makes a
read trustworthy has to be asserted, because none of it is enforced by GitHub.

Three properties, each of which fails silently rather than loudly if it regresses:

1. The barrier identifies the run by the full identity triple. A head SHA is not a
   unique key: two pull requests can carry the same head commit, and each gets its
   own Fast Gate run on it. Keyed on the SHA alone the barrier reads whichever run
   is newest -- possibly another PR's -- and releases this PR's matrix on a gate
   that never ran against its base. Raised as a blocking finding by GPT 5.6 on the
   commit that introduced the barrier.

2. The barrier fails CLOSED in every direction. A barrier that passes when it could
   not read its subject is worse than no barrier, because the matrix runs anyway
   and the log claims it was cleared to.

3. Every job that costs real runner time waits on it, and the jobs that must NOT
   wait on it still do not -- `changes` because it produces the outputs the gating
   conditions read, and the two coverage reporters because a skipped required check
   reads to GitHub as satisfied.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_FAST_GATE = _REPO_ROOT / ".github" / "workflows" / "fast-gate.yml"
_BUILD = _REPO_ROOT / ".github" / "workflows" / "build.yml"

# The gates the split moved. Named explicitly rather than derived from the
# file, so a gate silently DROPPED during a future edit fails here. scrub-lint is
# gone: its replacement is the internal-content-scan check, which runs in its own
# workflow because it needs OIDC and a private ruleset, not a repo script.
_GATE_JOBS = (
    "vendor-manifest",
    "brand-lint",
    "comment-history-lint",
    "focus-cue-lint",
    "feature-map-lint",
    "changelog-history",
    "decision-ledger-history",
    "builtin-skill-scope",
    "loop-bound-locks",
    "testpaths-coverage",
    "cwd-relative-repo-reads",
    "harness-parity",
    "docs-lint",
)
# Every job in fast-gate.yml, in file order: the moved gates plus the one gate
# that was born there. Explicit for the same reason as _GATE_JOBS -- a job added
# without the push/variable clause would be the one job left running on a
# queue-on push, and a file-derived list would admit it silently.
_FAST_GATE_JOBS = _GATE_JOBS[:-1] + ("memory-store-seam", "docs-lint")

#: The exact `if` clause that trims a job off the push path while the repository
#: variable MERGE_QUEUE_ENABLED is 'true', and keeps it there while it is unset.
_PUSH_SKIP_CLAUSE = "github.event_name != 'push' || vars.MERGE_QUEUE_ENABLED != 'true'"
#: The exact clause under which the boot leg admits a push with no needs to lean on.
_PUSH_ADMIT_CLAUSE = "github.event_name == 'push' && vars.MERGE_QUEUE_ENABLED == 'true'"
#: The exact run-level concurrency group of every workflow a push to main runs:
#: a group per COMMIT only on the queue-on push, the per-ref group otherwise.
_PUSH_GROUP_EXPR = (
    "${{ " + _PUSH_ADMIT_CLAUSE + " && format('{0}-{1}', github.workflow, github.sha)"
    " || format('{0}-{1}', github.workflow, github.ref) }}"
)

# DENY-BY-DEFAULT: every job in ci.yml must wait for the barrier unless it is
# exempt here for a reason that is not about cost. An enumeration of the jobs that
# MUST carry the edge goes stale the moment someone adds a job -- the new one would
# race the gates while this file stayed green, which is precisely the defect class
# this change harvested. Inverting it makes a new job fail until its author either
# wires the edge or records why it cannot have one.
# Must not reach the barrier at all, by any path.
_MUST_NOT_REACH: dict[str, str] = {
    "changes": (
        "produces the surface outputs every gating `if:` reads; behind the barrier "
        "those outputs are empty strings and each consumer silently flips"
    ),
    "await-fast-gate": "is the barrier",
}

# These DO reach the barrier, unavoidably -- they consume the shards' artifacts. What
# protects them is not the absence of the dependency but a guard that still emits a
# verdict when an upstream is skipped, asserted in
# test_the_coverage_reporters_survive_a_gate_skipped_upstream below. Listing them here
# says "reaching the barrier is expected", not "unchecked".
_REACHES_BUT_SURVIVES_A_SKIP: dict[str, str] = {
    "coverage-gate": (
        "runs `if: always()` so a required check emits a real verdict -- GitHub "
        "reports a SKIPPED required check as satisfied, so a silent skip here would "
        "remove the coverage floor exactly when the barrier skips the shards"
    ),
    "frontend-coverage-merge": (
        "`!cancelled()` plus an explicit `!= 'skipped'` so a FAILED shard set still "
        "gets stitched while a skipped one does not produce an empty merge"
    ),
}

_EDGE_EXEMPT: dict[str, str] = {**_MUST_NOT_REACH, **_REACHES_BUT_SURVIVES_A_SKIP}


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci() -> dict:
    return _workflow(_CI)


@pytest.fixture(scope="module")
def fast_gate() -> dict:
    return _workflow(_FAST_GATE)


@pytest.fixture(scope="module")
def barrier_step(ci: dict) -> dict:
    steps = ci["jobs"]["await-fast-gate"]["steps"]
    # The job checks out .github/scripts (so the poll script is on disk) and
    # then runs it: a checkout step followed by the one run step. Pin that
    # shape so neither a dropped checkout nor a smuggled extra run step slips
    # through.
    assert len(steps) == 2, "the barrier is checkout + one run step; update if it grows"
    checkout, run_step = steps
    assert checkout.get("uses", "").startswith(
        "actions/checkout@"
    ), "the barrier must check out the script before running it"
    assert (
        checkout.get("with", {}).get("sparse-checkout") == ".github/scripts"
    ), "the checkout should fetch only .github/scripts"
    # The checkout uses the default ref (the merge ref on a pull_request) so
    # the script this job adds is present. A base-pinned ref would predate the
    # script and fail the step exit 127, and it would not make the barrier
    # fork-safe anyway: on a pull_request the fork supplies this step's run:/env:
    # too, so the real control against a forged barrier is approval + CODEOWNERS
    # (fork-workflow-guard.yml), not a checkout pin.
    ref = checkout.get("with", {}).get("ref", "")
    assert ref == "", (
        "the barrier checkout must use the default merge ref so the newly added "
        "script is present; a base pin predates it and fails exit 127"
    )
    assert "run" in run_step, "the second step runs the poll script"
    return run_step


# The poll body lives in a script so its whole control flow runs against a
# stubbed gh and a fake clock (test_await_fast_gate_poll.py). The step only
# NAMES the script, so the properties below are read out of the script file;
# the step itself owns the identity env and the one-line invocation, asserted
# below.
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "await-fast-gate.sh"


@pytest.fixture(scope="module")
def barrier_script() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _assert_push_to_main_reaches_exactly_the_macos_boot_leg(ci: dict) -> None:
    jobs = ci["jobs"]
    skipped_on_push = {
        name for name, spec in jobs.items() if _PUSH_SKIP_CLAUSE in str(spec.get("if", ""))
    }
    assert skipped_on_push == {
        "changes",
        "await-fast-gate",
        "coverage-gate",
        "frontend-coverage-merge",
    }
    # A bare `!= 'push'` anywhere else would trim a job off the push path with
    # the variable unset, which is the state this contract keeps at full matrix.
    bare = {
        name
        for name, spec in jobs.items()
        if "github.event_name != 'push'" in str(spec.get("if", ""))
    }
    assert (
        bare == skipped_on_push
    ), f"push-skips not gated on MERGE_QUEUE_ENABLED: {bare - skipped_on_push}"

    for name, spec in jobs.items():
        if name in skipped_on_push or name == "e2e-boot-matrix":
            continue
        needs = spec.get("needs") or []
        direct_needs = {needs} if isinstance(needs, str) else set(needs)
        assert direct_needs & {"changes", "await-fast-gate"}, (
            f"{name} does not directly need changes or await-fast-gate, so a push "
            "could reach it after those jobs skip"
        )
        guard = str(spec.get("if", ""))
        assert (
            "always()" not in guard and "!cancelled()" not in guard
        ), f"{name} overrides the skipped dependency with {guard!r} and can run on push"

    boot = jobs["e2e-boot-matrix"]
    boot_guard = str(boot["if"])
    assert _PUSH_ADMIT_CLAUSE in boot_guard
    assert "!cancelled()" in boot_guard
    # The admission is the whole conjunction, never a bare push: with the
    # variable unset a push must go through the needs like every other event.
    assert boot_guard.count("github.event_name == 'push'") == 1
    assert "(github.event_name == 'push' ||" not in boot_guard

    matrix_os = str(boot["strategy"]["matrix"]["os"])
    sides = matrix_os.split("||")
    assert (
        len(sides) == 3
    ), f"expected non-push / queue-on / queue-off matrix split, got: {matrix_os}"
    non_push_side, queue_on_side, queue_off_side = sides
    assert "github.event_name != 'push'" in non_push_side
    assert "vars.MERGE_QUEUE_ENABLED == 'true'" in queue_on_side
    assert "&&" not in queue_off_side, "the fallback literal must be unguarded"

    def platforms(side: str) -> list[str]:
        start = side.index("[")
        end = side.index("]", start) + 1
        parsed = json.loads(side[start:end])
        assert isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)
        return parsed

    non_push_platforms = platforms(non_push_side)
    queue_on_platforms = platforms(queue_on_side)
    queue_off_platforms = platforms(queue_off_side)
    assert non_push_platforms == ["ubuntu-latest", "windows-latest"]
    assert queue_on_platforms == ["macos-15"]
    assert queue_off_platforms == ["ubuntu-latest", "macos-15", "windows-latest"]
    assert not any("macos" in platform for platform in non_push_platforms)


class TestTheGatesLiveInTheGateWorkflow:
    def test_all_gates_are_in_fast_gate_and_none_left_in_ci(
        self, ci: dict, fast_gate: dict
    ) -> None:
        missing = [job for job in _GATE_JOBS if job not in fast_gate["jobs"]]
        assert not missing, f"gate job(s) absent from fast-gate.yml: {missing}"
        # The other direction matters just as much: a gate re-added to ci.yml would
        # run beside the matrix again, which is the arrangement this split removed.
        strays = [job for job in _GATE_JOBS if job in ci["jobs"]]
        assert not strays, f"gate job(s) back in ci.yml, racing the matrix again: {strays}"

    @pytest.mark.parametrize("job", tuple(_workflow(_FAST_GATE)["jobs"]))
    def test_every_gate_skips_only_the_queued_push(self, fast_gate: dict, job: str) -> None:
        # A `needs:` lets a failed sibling skip it and an `if:` lets a diff shape
        # dodge it. The ONE condition a gate may carry is the exact push/variable
        # clause: on a push to main while MERGE_QUEUE_ENABLED is 'true' the merge
        # group already ran every gate on this tree, so the push run skips
        # whole -- and then neither this workflow nor ci.yml requests a fleet
        # slot on the queue-on push path (only fleet-labelled jobs can be
        # orphaned; build.yml's matrix resolver and the heal-exempt ratchet
        # audit are what remain). Equality, not containment: an extra `&&` term is a way
        # to dodge, and `==`/`!=` swapped would skip every PR instead.
        spec = fast_gate["jobs"][job]
        assert "needs" not in spec, f"{job} gained a dependency and can now be skipped"
        assert (
            spec.get("if") == _PUSH_SKIP_CLAUSE
        ), f"{job} must carry exactly the push/variable clause, got {spec.get('if')!r}"

    def test_the_queued_push_run_is_all_skipped_not_failed(self, fast_gate: dict) -> None:
        # Job by job above, and here as a whole: the job list is pinned so a gate
        # ADDED without the clause (which would be the one job left running on the
        # queue-on push, holding a fleet slot) fails, and so does one dropped.
        assert tuple(fast_gate["jobs"]) == _FAST_GATE_JOBS
        carrying = {
            name for name, spec in fast_gate["jobs"].items() if spec.get("if") == _PUSH_SKIP_CLAUSE
        }
        assert carrying == set(_FAST_GATE_JOBS)
        # No barrier or aggregate job exists to turn a skipped sibling into a
        # failure: nothing in the file has a `needs:` at all.
        assert not any("needs" in spec for spec in fast_gate["jobs"].values())

    def test_the_gate_workflow_matches_ci_triggers(self, ci: dict, fast_gate: dict) -> None:
        """Re-derived stronger: pin both workflows' complete, identical trigger dictionaries."""
        # `on` is a YAML 1.1 boolean, so PyYAML keys the trigger block on True.
        ci_on = dict(ci.get("on", ci.get(True)))
        fg_on = dict(fast_gate.get("on", fast_gate.get(True)))
        expected = {
            "push": {"branches": ["main"]},
            "pull_request": {"branches": ["main"]},
            "merge_group": {"types": ["checks_requested"]},
        }
        assert ci_on == expected
        # Fast Gate runs on a push too: with MERGE_QUEUE_ENABLED unset, ci.yml's
        # barrier consumes that run before the full push matrix.
        assert fg_on == expected, (
            "Fast Gate's triggers drifted from ci.yml's. A WIDER filter newly reviews "
            "fork PRs on a non-main base (the fork reviewers key on this workflow), and "
            "a NARROWER one leaves await-fast-gate waiting for a run that never starts."
        )
        assert list(fg_on) == list(expected), "keep the trigger order matching ci.yml"

    @pytest.mark.parametrize("path", [_CI, _FAST_GATE, _BUILD], ids=lambda p: p.name)
    def test_a_push_is_grouped_per_commit_only_while_the_queue_is_on(self, path: Path) -> None:
        """Re-derived stronger: the exact concurrency block, on every workflow a push runs.

        With MERGE_QUEUE_ENABLED unset a push runs the FULL matrix, so it must keep
        today's per-ref group: one running plus one pending, later pushes evict the
        pending one. A per-SHA group there would let N concurrent main matrices hold
        2N hosted macOS jobs with nothing evicting or queueing them. Only the trimmed
        queue-on push (one macOS leg per commit) earns a group per commit.
        """
        assert _workflow(path)["concurrency"] == {
            "group": _PUSH_GROUP_EXPR,
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }
        # The variable gates the per-SHA arm only; the per-ref fallback is unguarded.
        sha_arm, _, ref_arm = _PUSH_GROUP_EXPR.partition(" || ")
        assert sha_arm.startswith("${{ " + _PUSH_ADMIT_CLAUSE + " && ")
        assert "github.sha" in sha_arm and "github.sha" not in ref_arm
        assert "vars." not in ref_arm and "github.ref" in ref_arm

    def test_a_push_to_main_reaches_exactly_the_macos_boot_leg(self, ci: dict) -> None:
        """Re-derived stronger: pin the push path job by job under both states of
        MERGE_QUEUE_ENABLED -- trimmed to the mac boot leg when set, full matrix when unset."""
        _assert_push_to_main_reaches_exactly_the_macos_boot_leg(ci)


class TestFastGatePythonRuntime:
    @staticmethod
    def _assert_runtime(spec: dict) -> None:
        steps = spec["steps"]
        setups = [
            i
            for i, step in enumerate(steps)
            if step.get("uses", "").startswith("actions/setup-python@")
        ]
        assert len(setups) == 1, "expected exactly one Python setup"
        index = setups[0]
        setup = steps[index]
        assert setup["uses"] == (
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
        ), "Python setup must use the pinned action"
        assert setup.get("with", {}).get("python-version") == "3.12", "expected Python 3.12"
        assert "if" not in setup, "Python setup must be unconditional"
        assert "continue-on-error" not in setup, "Python setup must be blocking"
        assert "continue-on-error" not in spec, "the runtime failure must fail the job"
        assert index > 0 and steps[index - 1].get("uses", "").startswith(
            "actions/checkout@"
        ), "Python setup must follow checkout"
        runs = [i for i, step in enumerate(steps) if "run" in step]
        assert runs and index < min(runs), "Python setup must precede every run step"

    @pytest.mark.parametrize("job", tuple(_workflow(_FAST_GATE)["jobs"]))
    def test_every_actual_job_sets_up_python_before_running(self, fast_gate: dict, job: str):
        self._assert_runtime(fast_gate["jobs"][job])

    @pytest.mark.parametrize(
        "defect",
        [
            "missing",
            "duplicate",
            "old-version",
            "unpinned",
            "late",
            "conditional",
            "soft-step",
            "soft-job",
        ],
    )
    def test_runtime_contract_rejects_broken_setup(self, defect: str) -> None:
        # Fresh workflow data keeps each mutation independent of the module fixture.
        spec = _workflow(_FAST_GATE)["jobs"]["memory-store-seam"]
        self._assert_runtime(spec)
        steps = spec["steps"]
        setup = steps[1]
        if defect == "missing":
            steps.pop(1)
        elif defect == "duplicate":
            steps.insert(2, dict(setup))
        elif defect == "old-version":
            setup["with"]["python-version"] = "3.11"
        elif defect == "unpinned":
            setup["uses"] = "actions/setup-python@v7"
        elif defect == "late":
            steps.append(steps.pop(1))
        elif defect == "conditional":
            setup["if"] = "runner.environment == 'github-hosted'"
        elif defect == "soft-step":
            setup["continue-on-error"] = True
        else:
            spec["continue-on-error"] = True
        with pytest.raises(AssertionError):
            self._assert_runtime(spec)


class TestTheBarrierIdentifiesTheRightRun:
    def test_the_step_invokes_the_extracted_script(self, barrier_step: dict) -> None:
        # The poll is one script, called with the identity env and nothing else.
        # A step that grew an inline body again would defeat the extraction and
        # leave test_await_fast_gate_poll.py running a stale copy.
        assert barrier_step["run"].strip() == "bash .github/scripts/await-fast-gate.sh"

    def test_the_lookup_binds_branch_and_head_repository_not_just_the_sha(
        self, barrier_step: dict, barrier_script: str
    ) -> None:
        env, script = barrier_step["env"], barrier_script

        # The identity triple has to be available to the step at all...
        for var in ("SHA", "EVENT", "BRANCH", "HEAD_REPO"):
            assert var in env, f"the barrier no longer resolves {var}"
        # ...and every part of it has to reach the query or the selection.
        assert "head_sha=$SHA" in script
        assert "event=$EVENT" in script
        assert "branch=$BRANCH" in script, (
            "the run lookup dropped the branch filter: two PRs sharing a head commit "
            "would then read each other's Fast Gate verdict"
        )
        assert ".head_branch == $branch" in script, (
            "the branch match must be re-asserted on the selected run, not left to a "
            "server-side filter that could be ignored"
        )
        assert ".head_repository.full_name == $repo" in script, (
            "without the head-repository match, a fork pushing the same branch name at "
            "the same commit answers for this PR"
        )

    def test_the_sha_is_the_head_not_the_merge_commit(self, barrier_step: dict) -> None:
        # github.sha on a pull_request event is the ephemeral merge commit, which no
        # Fast Gate run is ever keyed to.
        sha = barrier_step["env"]["SHA"]
        assert "github.event.pull_request.head.sha" in sha
        assert "github.sha" in sha, "the push path still needs a sha"

    def test_the_run_is_selected_after_filtering_never_before(self, barrier_script: str) -> None:
        # Scoped to the jq program, not the whole script: the prose above it names
        # max_by(.id) while explaining why the order matters, and searching the raw
        # script would match that comment and "prove" the ordering from a sentence.
        selector = TestTheSelectorBehavesOnRealPayloadShapes._selector(barrier_script)
        select_at = selector.find(".head_repository.full_name == $repo")
        collapse_at = selector.find("max_by(.id)")
        assert select_at != -1, "the selector lost its head-repository match"
        assert collapse_at != -1, "the selector lost its collapse"
        assert select_at < collapse_at, (
            "max_by(.id) runs before the identity filter, so the NEWEST run wins "
            "regardless of whose it is -- the exact collision this guards"
        )


class TestTheBarrierFailsClosed:
    def test_all_three_unreadable_outcomes_exit_non_zero(self, barrier_script: str) -> None:
        script = barrier_script
        # A run that never appears, one that never completes, and one that completed
        # non-success are three distinct paths, and each must be an error exit.
        assert (
            script.count("::error::") >= 3
        ), "one of the barrier's failure paths stopped reporting an error"
        assert script.count("exit 1") >= 3, (
            "one of the barrier's failure paths stopped exiting non-zero -- a barrier "
            "that returns 0 when it could not confirm the gates clears the matrix on "
            "no evidence"
        )
        assert "exit 0" in script, "the success path must still release the matrix"

    def test_the_only_success_path_is_a_successful_conclusion(self, barrier_script: str) -> None:
        script = barrier_script
        head, _, tail = script.partition("success)")
        assert tail, "the conclusion case statement lost its success branch"
        # `exit 0` may appear only under that branch; anything earlier would release
        # the matrix before the conclusion was read.
        assert "exit 0" not in head, "the barrier can exit 0 before reading a conclusion"

    def test_the_budgets_are_bounded_and_the_job_has_a_timeout(
        self, ci: dict, barrier_script: str
    ) -> None:
        script = barrier_script
        assert "APPEAR_BUDGET=" in script and "TOTAL_BUDGET=" in script
        # The default is what a real runner uses (the seam form is
        # `${TOTAL_BUDGET:-720}`), so read the default, not an override. The job cap
        # has to outlast the poll budget, or the step is killed before it can report
        # its own fail-closed verdict and the job reports a timeout instead.
        total = int(script.split("TOTAL_BUDGET:-", 1)[1].split("}", 1)[0].strip())
        cap_seconds = int(ci["jobs"]["await-fast-gate"]["timeout-minutes"]) * 60
        assert cap_seconds > total, (
            f"timeout-minutes ({cap_seconds}s) must exceed TOTAL_BUDGET ({total}s) so "
            "the step reports the verdict rather than being killed mid-poll"
        )

    def test_reading_another_workflows_runs_is_granted_explicitly(self, ci: dict) -> None:
        # Job-level permissions REPLACE the top-level grant, so actions:read has to be
        # restated here or the API read 404s and the barrier fails closed on every run.
        perms = ci["jobs"]["await-fast-gate"]["permissions"]
        assert perms.get("actions") == "read"
        assert perms.get("contents") == "read"


class TestTheEdgeReachesEveryExpensiveJob:
    @staticmethod
    def _needs(ci: dict, job: str) -> list[str]:
        needs = ci["jobs"][job].get("needs") or []
        return [needs] if isinstance(needs, str) else list(needs)

    @classmethod
    def _waits_for_barrier(cls, ci: dict, job: str) -> bool:
        """Is the barrier reachable from this job through `needs`?

        Reachability, not a direct edge: coverage-combine needs backend-test, which
        carries the edge, so a red gate skips backend-test and coverage-combine skips
        with it. Requiring the edge on its own `needs:` would force either a redundant
        edge or an exemption -- and an exemption granted to a job that is in fact
        gated is how a genuine hole gets waved through later.
        """
        seen: set[str] = set()
        frontier = [job]
        while frontier:
            current = frontier.pop()
            for parent in cls._needs(ci, current):
                if parent == "await-fast-gate":
                    return True
                if parent not in seen and parent in ci["jobs"]:
                    seen.add(parent)
                    frontier.append(parent)
        return False

    def test_every_job_waits_for_the_barrier_unless_it_is_exempt(self, ci: dict) -> None:
        unguarded = [
            job
            for job in ci["jobs"]
            if job not in _EDGE_EXEMPT and not self._waits_for_barrier(ci, job)
        ]
        assert not unguarded, (
            f"job(s) in ci.yml start without the Fast Gate verdict: {sorted(unguarded)}. "
            "Add `await-fast-gate` to their `needs:`, or -- if one genuinely must run "
            "before the gates are known -- add it to _EDGE_EXEMPT with the reason."
        )

    def test_the_exemption_list_measures_something(self, ci: dict) -> None:
        # A stale exemption is how deny-by-default rots back into an allowlist: a
        # renamed job leaves an entry that exempts nothing while the real job goes
        # unchecked.
        stale = sorted(set(_EDGE_EXEMPT) - set(ci["jobs"]))
        assert not stale, f"_EDGE_EXEMPT names job(s) that no longer exist: {stale}"
        # And the inversion only means anything if it is actually guarding jobs.
        guarded = [job for job in ci["jobs"] if job not in _EDGE_EXEMPT]
        assert len(guarded) >= 10, (
            f"only {len(guarded)} job(s) are subject to the edge requirement; the "
            "exemption list has grown until the contract checks almost nothing"
        )

    @pytest.mark.parametrize("job", sorted(_MUST_NOT_REACH))
    def test_a_job_that_must_not_reach_the_barrier_does_not(self, ci: dict, job: str) -> None:
        # Load-bearing in the other direction: wiring the edge into `changes` breaks
        # every gating condition in a way that reads as extra safety. Checked
        # transitively, because inheriting the wait through a parent is just as fatal
        # as declaring it.
        assert not self._waits_for_barrier(
            ci, job
        ), f"{job} must not depend on the barrier -- {_MUST_NOT_REACH[job]}"

    @pytest.mark.parametrize("job", sorted(_REACHES_BUT_SURVIVES_A_SKIP))
    def test_a_reporter_that_reaches_the_barrier_can_survive_a_skip(
        self, ci: dict, job: str
    ) -> None:
        # These are allowed to reach it, so the guard is what has to be present: a
        # bare `success()` here turns a red gate into a SKIPPED required check, which
        # GitHub reports as satisfied.
        guard = str(ci["jobs"][job].get("if", ""))
        assert "always()" in guard or "!cancelled()" in guard, (
            f"{job} reaches the barrier but has no always()/!cancelled() guard, so a "
            f"red gate would skip it silently -- {_REACHES_BUT_SURVIVES_A_SKIP[job]}"
        )

    def test_the_coverage_reporters_survive_a_gate_skipped_upstream(self, ci: dict) -> None:
        # coverage-gate keeps always() because GitHub reports a SKIPPED required check
        # as satisfied: without it, a red gate would skip the shards and take the
        # coverage floor with them.
        assert "always()" in str(ci["jobs"]["coverage-gate"]["if"])
        # frontend-coverage-merge solves the mirror-image problem the other way: it
        # still runs when a shard FAILED (there is a report to stitch) but not when the
        # shards were skipped (there is not), so it cannot go red for a reason
        # unrelated to the gate the author has to fix.
        merge_if = str(ci["jobs"]["frontend-coverage-merge"]["if"])
        assert "!cancelled()" in merge_if
        assert "needs.frontend-test.result != 'skipped'" in merge_if


class TestTheSelectorBehavesOnRealPayloadShapes:
    """The assertions above pin the selector's TEXT. This one runs it.

    A jq program can contain every required clause and still pick the wrong run, so
    the extracted program is executed against payloads shaped like the real
    ``/actions/workflows/{id}/runs`` response.
    """

    _BRANCH = "feature/mine"
    _REPO = "kirodotdev/KiroCrew"

    @staticmethod
    def _selector(script: str) -> str:
        start = script.find("'[(.workflow_runs")
        assert start != -1, "could not locate the selector program in the barrier step"
        end = script.find("'", start + 1)
        assert end != -1
        return script[start + 1 : end]

    @staticmethod
    def _run(rid: int, branch: str, repo: str) -> dict:
        return {
            "id": rid,
            "head_branch": branch,
            "status": "completed",
            "conclusion": "success",
            "html_url": f"https://github.com/x/actions/runs/{rid}",
            "head_repository": {"full_name": repo},
        }

    def _select(self, script: str, payload: dict) -> int | None:
        if shutil.which("jq") is None:  # pragma: no cover - CI images ship jq
            pytest.skip("jq is not installed")
        proc = subprocess.run(
            [
                "jq",
                "-c",
                "--arg",
                "branch",
                self._BRANCH,
                "--arg",
                "repo",
                self._REPO,
                self._selector(script),
            ],
            input=json.dumps(payload),
            capture_output=True,
            check=True,
            # jq emits JSON, which is UTF-8 by specification, so its encoding is
            # knowable and pinning it is correct. Bare `text=True` would decode with
            # the locale code page -- the Windows ANSI page on the windows-latest
            # shard this test also runs on.
            **UTF8_TEXT,
        )
        chosen = json.loads(proc.stdout.strip())
        return chosen["id"] if isinstance(chosen, dict) else None

    def test_a_colliding_run_on_another_branch_is_refused(self, barrier_script: str) -> None:
        payload = {"workflow_runs": [self._run(900, "other/pr", self._REPO)]}
        assert self._select(barrier_script, payload) is None

    def test_a_fork_reusing_the_branch_name_is_refused(self, barrier_script: str) -> None:
        payload = {"workflow_runs": [self._run(901, self._BRANCH, "attacker/KiroCrew")]}
        assert self._select(barrier_script, payload) is None

    def test_a_newer_colliding_run_does_not_outrank_my_older_one(self, barrier_script: str) -> None:
        payload = {
            "workflow_runs": [
                self._run(904, self._BRANCH, self._REPO),
                self._run(999, "other/pr", self._REPO),
            ]
        }
        assert self._select(barrier_script, payload) == 904

    def test_my_own_rerun_collapses_to_the_newest(self, barrier_script: str) -> None:
        payload = {
            "workflow_runs": [
                self._run(905, self._BRANCH, self._REPO),
                self._run(906, self._BRANCH, self._REPO),
            ]
        }
        assert self._select(barrier_script, payload) == 906

    @pytest.mark.parametrize("payload", [{"workflow_runs": []}, {}])
    def test_an_empty_or_absent_list_yields_nothing_rather_than_erroring(
        self, barrier_script: str, payload: dict
    ) -> None:
        # The caller treats null as "keep waiting", so a jq error here would turn a
        # transient empty page into a hard failure on the first poll.
        assert self._select(barrier_script, payload) is None


class TestTheConclusionArmsBehaveOnRealConclusions:
    """The assertions above pin the case statement's TEXT. This one runs it.

    ``action_required`` is what GitHub reports for a fork run that is still awaiting
    maintainer approval, and it arrives with ``status=completed``. An arm order or a
    glob that swallowed it back into the terminal ``*)`` branch would put the matrix
    back at the mercy of which of the two pending runs a maintainer approves first,
    so the extracted arms are executed rather than only read.
    """

    _POLLING = "__barrier_would_poll_again__"
    _STATUS = "__barrier_status__="

    @staticmethod
    def _case(script: str) -> str:
        start = script.find('case "$conclusion" in')
        assert start != -1, "could not locate the conclusion case statement"
        end = script.find("esac", start)
        assert end != -1, "the conclusion case statement lost its esac"
        return script[start : end + len("esac")]

    def _exec(self, script: str, conclusion: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        if shutil.which("sh") is None:  # pragma: no cover - CI images ship sh
            pytest.skip("no POSIX shell available")
        program = (
            "set -eu\n"
            'conclusion="$1"\n'
            'url="https://github.com/x/actions/runs/1"\n'
            # The raw API value at this point in the loop, so a rename is visible.
            'status="completed"\n'
            f"{self._case(script)}\n"
            # Reached only when no arm exited: the loop falls through to its
            # TOTAL_BUDGET check and sleeps for another poll. $status is what the
            # budget-spent message below the case interpolates, so it is reported
            # too rather than inspected as source text.
            f'printf "%s\\n" "{self._POLLING}"\n'
            f'printf "{self._STATUS}%s\\n" "$status"\n'
        )
        return subprocess.run(
            ["sh", "-c", program, "sh", conclusion],
            capture_output=True,
            # The program is shell text sliced out of ci.yml at runtime, so a
            # future arm could carry a relative-path write. Spawning in the
            # checkout would leave that file behind; tmp_path cannot.
            cwd=cwd,
            **UTF8_TEXT,
        )

    def _status_after(self, script: str, conclusion: str, cwd: Path) -> str:
        proc = self._exec(script, conclusion, cwd)
        assert proc.returncode == 0, proc.stderr
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith(self._STATUS)]
        assert len(line) == 1, f"the case did not report a single $status: {proc.stdout!r}"
        return line[0][len(self._STATUS) :]

    def test_a_pending_fork_approval_keeps_polling_rather_than_failing(
        self, barrier_script: str, tmp_path: Path
    ) -> None:
        proc = self._exec(barrier_script, "action_required", tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert self._POLLING in proc.stdout, (
            "action_required left the case with a non-zero exit instead of falling "
            "through to the TOTAL_BUDGET check, so a fork PR still loses its whole "
            "matrix to whichever of the two pending runs is approved first"
        )
        assert "::error::" not in proc.stdout

    def test_a_pending_fork_approval_names_the_state_it_waits_on(
        self, barrier_script: str, tmp_path: Path
    ) -> None:
        # The fail-closed message below the case interpolates $status, whose raw API
        # value here is "completed": left alone it reports a completed run as still
        # waiting and names no pending approval for the reader to act on. Asserted on
        # the value the shell actually leaves behind, not on the arm's source text,
        # so re-assigning the same "completed" back cannot satisfy it.
        status = self._status_after(barrier_script, "action_required", tmp_path)
        assert status != "completed", (
            "the action_required arm no longer renames $status, so the budget-spent "
            "error reads \"still 'completed'\" about a run nobody has judged"
        )
        assert "approval" in status.lower(), (
            "the renamed state does not name the pending approval, which is the one "
            f"thing the reader has to act on: {status!r}"
        )

    def test_success_still_releases_the_matrix(self, barrier_script: str, tmp_path: Path) -> None:
        proc = self._exec(barrier_script, "success", tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert self._POLLING not in proc.stdout, "success no longer leaves the loop"

    @pytest.mark.parametrize(
        "conclusion",
        ["failure", "cancelled", "timed_out", "startup_failure", "neutral", "skipped"],
    )
    def test_every_other_conclusion_is_still_terminal(
        self, barrier_script: str, conclusion: str, tmp_path: Path
    ) -> None:
        proc = self._exec(barrier_script, conclusion, tmp_path)
        assert proc.returncode == 1, f"{conclusion} stopped failing closed"
        assert "::error::" in proc.stdout
        assert self._POLLING not in proc.stdout
        assert self._STATUS not in proc.stdout
