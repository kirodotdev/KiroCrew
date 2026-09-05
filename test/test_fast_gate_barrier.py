"""The Fast Gate barrier: what `await-fast-gate` must guarantee for the split to be safe.

The eleven cheap blocking gates live in ``.github/workflows/fast-gate.yml`` so that
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

# The eleven gates the split moved. Named explicitly rather than derived from the
# file, so a gate silently DROPPED during a future edit fails here.
_GATE_JOBS = (
    "scrub-lint",
    "vendor-manifest",
    "brand-lint",
    "focus-cue-lint",
    "feature-map-lint",
    "changelog-history",
    "builtin-skill-scope",
    "loop-bound-locks",
    "testpaths-coverage",
    "harness-parity",
    "docs-lint",
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
    assert len(steps) == 1, "the barrier is one step; update this contract if it grows"
    return steps[0]


class TestTheGatesLiveInTheGateWorkflow:
    def test_all_eleven_gates_are_in_fast_gate_and_none_left_in_ci(
        self, ci: dict, fast_gate: dict
    ) -> None:
        missing = [job for job in _GATE_JOBS if job not in fast_gate["jobs"]]
        assert not missing, f"gate job(s) absent from fast-gate.yml: {missing}"
        # The other direction matters just as much: a gate re-added to ci.yml would
        # run beside the matrix again, which is the arrangement this split removed.
        strays = [job for job in _GATE_JOBS if job in ci["jobs"]]
        assert not strays, f"gate job(s) back in ci.yml, racing the matrix again: {strays}"

    @pytest.mark.parametrize("job", _GATE_JOBS)
    def test_every_gate_is_unconditional(self, fast_gate: dict, job: str) -> None:
        # A `needs:` lets a failed sibling skip it and an `if:` lets a diff shape
        # dodge it. These gates are cheap precisely so that neither is needed.
        spec = fast_gate["jobs"][job]
        assert "needs" not in spec, f"{job} gained a dependency and can now be skipped"
        assert "if" not in spec, f"{job} gained a condition and can now be dodged"

    def test_the_gate_workflow_matches_ci_triggers(self, ci: dict, fast_gate: dict) -> None:
        # `on` is a YAML 1.1 boolean, so PyYAML keys the trigger block on True.
        ci_on = ci.get("on", ci.get(True))
        fg_on = fast_gate.get("on", fast_gate.get(True))
        assert fg_on == ci_on, (
            "Fast Gate's triggers drifted from ci.yml's. They must match: a WIDER "
            "filter newly reviews fork PRs on a non-main base (the fork reviewers key "
            "on this workflow), and a NARROWER one leaves await-fast-gate waiting for "
            "a run that never starts."
        )


class TestTheBarrierIdentifiesTheRightRun:
    def test_the_lookup_binds_branch_and_head_repository_not_just_the_sha(
        self, barrier_step: dict
    ) -> None:
        env, script = barrier_step["env"], barrier_step["run"]

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

    def test_the_run_is_selected_after_filtering_never_before(self, barrier_step: dict) -> None:
        # Scoped to the jq program, not the whole step: the prose above it names
        # max_by(.id) while explaining why the order matters, and searching the raw
        # script would match that comment and "prove" the ordering from a sentence.
        selector = TestTheSelectorBehavesOnRealPayloadShapes._selector(barrier_step["run"])
        select_at = selector.find(".head_repository.full_name == $repo")
        collapse_at = selector.find("max_by(.id)")
        assert select_at != -1, "the selector lost its head-repository match"
        assert collapse_at != -1, "the selector lost its collapse"
        assert select_at < collapse_at, (
            "max_by(.id) runs before the identity filter, so the NEWEST run wins "
            "regardless of whose it is -- the exact collision this guards"
        )


class TestTheBarrierFailsClosed:
    def test_all_three_unreadable_outcomes_exit_non_zero(self, barrier_step: dict) -> None:
        script = barrier_step["run"]
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

    def test_the_only_success_path_is_a_successful_conclusion(self, barrier_step: dict) -> None:
        script = barrier_step["run"]
        head, _, tail = script.partition("success)")
        assert tail, "the conclusion case statement lost its success branch"
        # `exit 0` may appear only under that branch; anything earlier would release
        # the matrix before the conclusion was read.
        assert "exit 0" not in head, "the barrier can exit 0 before reading a conclusion"

    def test_the_budgets_are_bounded_and_the_job_has_a_timeout(
        self, ci: dict, barrier_step: dict
    ) -> None:
        script = barrier_step["run"]
        assert "APPEAR_BUDGET=" in script and "TOTAL_BUDGET=" in script
        # The job cap has to outlast the poll budget, or the step is killed before it
        # can report its own fail-closed verdict and the job reports a timeout instead.
        total = int(script.split("TOTAL_BUDGET=", 1)[1].split("\n", 1)[0].strip())
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

    def test_a_colliding_run_on_another_branch_is_refused(self, barrier_step: dict) -> None:
        payload = {"workflow_runs": [self._run(900, "other/pr", self._REPO)]}
        assert self._select(barrier_step["run"], payload) is None

    def test_a_fork_reusing_the_branch_name_is_refused(self, barrier_step: dict) -> None:
        payload = {"workflow_runs": [self._run(901, self._BRANCH, "attacker/KiroCrew")]}
        assert self._select(barrier_step["run"], payload) is None

    def test_a_newer_colliding_run_does_not_outrank_my_older_one(self, barrier_step: dict) -> None:
        payload = {
            "workflow_runs": [
                self._run(904, self._BRANCH, self._REPO),
                self._run(999, "other/pr", self._REPO),
            ]
        }
        assert self._select(barrier_step["run"], payload) == 904

    def test_my_own_rerun_collapses_to_the_newest(self, barrier_step: dict) -> None:
        payload = {
            "workflow_runs": [
                self._run(905, self._BRANCH, self._REPO),
                self._run(906, self._BRANCH, self._REPO),
            ]
        }
        assert self._select(barrier_step["run"], payload) == 906

    @pytest.mark.parametrize("payload", [{"workflow_runs": []}, {}])
    def test_an_empty_or_absent_list_yields_nothing_rather_than_erroring(
        self, barrier_step: dict, payload: dict
    ) -> None:
        # The caller treats null as "keep waiting", so a jq error here would turn a
        # transient empty page into a hard failure on the first poll.
        assert self._select(barrier_step["run"], payload) is None
