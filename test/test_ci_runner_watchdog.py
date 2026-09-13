"""The CI runner watchdog must heal exactly the orphaned-runner shape and nothing else.

``scripts/ci/runner_watchdog.py`` cancels and re-runs a ``ci.yml`` run whose
CodeBuild-routed job has sat *queued* with no runner past a threshold. Every
guard rail below exists because the alternative is a watchdog that cancels a
healthy run: a job that is merely slow to dispatch, a run that is young, a run
held by its concurrency group, a fork the token cannot re-run, a run the
watchdog already re-ran twice, or a run a human re-ran between the listing
and the heal.

The second family of tests pins ownership: once a cancel is accepted the
watchdog must see it through to a re-run -- escalating to force-cancel, sharing
one wait budget across runs, and on the next tick recovering a run whose cancel
landed after the budget ran out -- because a cancelled run that nobody re-runs
has lost its verdict for good.

The GitHub API is a fake that records every write, so each test pins both the
verdict and the exact calls made. Nothing here touches the network.
"""

from __future__ import annotations

import email.message
import http.client
import importlib.util
import io
import re
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "runner_watchdog.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci-runner-watchdog.yml"

SPEC = importlib.util.spec_from_file_location("runner_watchdog", SCRIPT)
assert SPEC and SPEC.loader
wd = importlib.util.module_from_spec(SPEC)
# The script's dataclasses resolve their postponed annotations through
# sys.modules[<module name>]; register before executing or collection crashes.
sys.modules[SPEC.name] = wd
SPEC.loader.exec_module(wd)

REPO = "example-org/example-repo"
NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CODEBUILD = "codebuild-example-gha-linux-{run}-{attempt}"


def _ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(
    run_id: int,
    *,
    minutes_ago: float = 60,
    attempt: int = 1,
    status: str = "in_progress",
    conclusion: str | None = None,
    updated_minutes_ago: float | None = None,
    fork: bool = False,
    branch: str = "main",
    event: str = "push",
) -> dict[str, Any]:
    head_repo = {"fork": fork, "full_name": "someone/example-repo" if fork else REPO}
    return {
        "id": run_id,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "created_at": _ts(minutes_ago),
        "updated_at": _ts(minutes_ago if updated_minutes_ago is None else updated_minutes_ago),
        "head_branch": branch,
        "event": event,
        "html_url": f"https://example.invalid/runs/{run_id}",
        "head_repository": head_repo,
    }


def _job(
    job_id: int,
    *,
    status: str = "queued",
    conclusion: str | None = None,
    minutes_ago: float = 60,
    completed_minutes_ago: float | None = None,
    started_minutes_ago: float | None = None,
    runner_name: str = "",
    codebuild: bool = True,
    run_id: int = 1,
    attempt: int = 1,
    large: bool = False,
) -> dict[str, Any]:
    if codebuild:
        label = CODEBUILD.format(run=run_id, attempt=attempt)
        if large:
            label += " instance-size:large"
        labels = [label]
    else:
        labels = ["ubuntu-latest"]
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "status": status,
        "conclusion": conclusion,
        "created_at": _ts(minutes_ago),
        "started_at": _ts(minutes_ago if started_minutes_ago is None else started_minutes_ago),
        "completed_at": None if completed_minutes_ago is None else _ts(completed_minutes_ago),
        "runner_name": runner_name,
        "labels": labels,
    }


def _cancelled_orphan_job(
    job_id: int, *, run_id: int = 1, queued_minutes: float = 60
) -> dict[str, Any]:
    """A job that was cancelled having never been picked up: the orphan's fingerprint."""
    return _job(
        job_id,
        status="completed",
        conclusion="cancelled",
        minutes_ago=queued_minutes + 5,
        completed_minutes_ago=5,
        run_id=run_id,
    )


EVIDENCE_RUN = _run(
    900,
    minutes_ago=20,
    status="completed",
    conclusion="success",
    updated_minutes_ago=3,
    branch="evidence",
)
EVIDENCE_JOB = _job(
    901,
    status="completed",
    conclusion="success",
    minutes_ago=19,
    started_minutes_ago=18.7,
    runner_name="evidence-runner",
    run_id=900,
)


class FakeApi:
    """Serves canned runs/jobs and records every POST.

    ``cancel_lands_after`` is how many status polls a cancelled run stays
    non-completed for (a force-cancel makes the next poll complete unless
    ``force_cancel_works`` is False); ``refuse`` maps a POST path suffix to an
    HTTP status the fake should raise; ``mutate_on_heal`` lets a test change a
    run between the listing and the heal, the way a human re-run would.
    """

    def __init__(
        self,
        runs_by_status: dict[str, list[dict[str, Any]]],
        jobs_by_run: dict[int, list[dict[str, Any]]],
        *,
        cancel_lands_after: int = 0,
        force_cancel_works: bool = True,
        refuse: dict[str, int] | None = None,
        newest_by_branch: dict[str, Any] | None = None,
        flip_after_reads: dict[int, tuple[int, dict[str, Any]]] | None = None,
        evidence: bool = True,
        completes_as: str = "cancelled",
        attempt_after_cancel: int | None = None,
        newest_after_rerun: dict[str, int] | None = None,
        newest_after_rerun_sequence: dict[str, list[int]] | None = None,
        ambiguous: dict[str, int] | None = None,
    ) -> None:
        self._runs_by_status = dict(runs_by_status)
        self._jobs_by_run = dict(jobs_by_run)
        if evidence and "completed" not in self._runs_by_status:
            # A recently finished run whose CodeBuild job started promptly: the
            # fleet is dispatching, so an orphan can be told from an outage.
            self._runs_by_status["completed"] = [EVIDENCE_RUN]
            self._jobs_by_run.setdefault(EVIDENCE_RUN["id"], [EVIDENCE_JOB])
        self._cancel_lands_after = cancel_lands_after
        self._force_cancel_works = force_cancel_works
        self._refuse = refuse or {}
        # ``ambiguous`` maps a POST path suffix to how many times the mutation is
        # APPLIED server-side but the response is lost (an ambiguous ApiError).
        self._ambiguous = dict(ambiguous or {})
        self._newest_by_branch = newest_by_branch or {}
        self._completes_as = completes_as
        self._attempt_after_cancel = attempt_after_cancel
        self._newest_after_rerun = newest_after_rerun
        self._newest_sequence = {k: list(v) for k, v in (newest_after_rerun_sequence or {}).items()}
        self._flip_after_reads = flip_after_reads or {}
        self._reads: dict[int, int] = {}
        self._rerun_seen = False
        self.posts: list[str] = []
        self.gets: list[str] = []
        self._cancelled: dict[int, int] = {}
        self._forced: set[int] = set()
        self.run_overrides: dict[int, dict[str, Any]] = {}

    def _listing_for(self, branch: str) -> list[dict[str, Any]]:
        """What GitHub would list for the branch name: every known run on it, newest first."""
        # Overrides model runs that APPEAR or change state later (a successor, a
        # hand re-run); the listing a test wants after that moment is spelled
        # out with newest_after_rerun / newest_after_rerun_sequence.
        known: dict[int, dict[str, Any]] = {}
        for runs in self._runs_by_status.values():
            for run in runs:
                known.setdefault(int(run["id"]), run)
        matching = [r for r in known.values() if r.get("head_branch") == branch]
        matching.sort(key=lambda r: r["created_at"], reverse=True)
        return [{"id": r["id"], "head_repository": r.get("head_repository")} for r in matching]

    def _listing_headed_by(self, newest: int, branch: str) -> list[dict[str, Any]]:
        """A listing whose newest same-repo run is `newest`, followed by every other run
        known on the branch (the judged run included), as GitHub would show them."""
        head = {"id": newest, "head_repository": {"full_name": REPO}}
        rest = [e for e in self._listing_for(branch) if e["id"] != newest]
        for run_id, run in self.run_overrides.items():
            if (
                run.get("head_branch") == branch
                and run_id != newest
                and all(e["id"] != run_id for e in rest)
            ):
                rest.append({"id": run_id, "head_repository": run.get("head_repository")})
        return [head] + rest

    def _run_by_id(self, run_id: int) -> dict[str, Any]:
        if run_id in self.run_overrides:
            return self.run_overrides[run_id]
        for runs in self._runs_by_status.values():
            for run in runs:
                if int(run["id"]) == run_id:
                    return run
        return {"id": run_id, "status": "in_progress", "created_at": _ts(60), "run_attempt": 1}

    def get(self, path: str) -> Any:
        self.gets.append(path)
        base, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if base.endswith("/runs") and "/workflows/" in base:
            if "branch" in params:
                table = self._newest_by_branch
                if self._rerun_seen and self._newest_after_rerun is not None:
                    table = self._newest_after_rerun
                seq = self._newest_sequence.get(params["branch"])
                if self._rerun_seen and seq:
                    answer = seq.pop(0) if len(seq) > 1 else seq[0]
                    return {"workflow_runs": self._listing_headed_by(answer, params["branch"])}
                newest = table.get(params["branch"])
                if newest is None:
                    return {"workflow_runs": self._listing_for(params["branch"])}
                if isinstance(newest, list):
                    return {"workflow_runs": newest}
                return {"workflow_runs": self._listing_headed_by(newest, params["branch"])}
            status = params["status"]
            page = int(params.get("page", "1"))
            per_page = int(params.get("per_page", "100"))
            runs = self._runs_by_status.get(status, [])
            start = (page - 1) * per_page
            return {"workflow_runs": runs[start : start + per_page]}
        if base.endswith("/jobs"):
            run_id = int(base.split("/runs/")[1].split("/")[0])
            page = int(params.get("page", "1"))
            per_page = int(params.get("per_page", "100"))
            jobs = self._jobs_by_run.get(run_id, [])
            start = (page - 1) * per_page
            return {"jobs": jobs[start : start + per_page]}
        if "/runs/" in base:
            run_id = int(base.rsplit("/", 1)[1])
            self._reads[run_id] = self._reads.get(run_id, 0) + 1
            flip = self._flip_after_reads.get(run_id)
            if flip is not None and self._reads[run_id] > flip[0]:
                return dict(flip[1])
            run = dict(self._run_by_id(run_id))
            polls = self._cancelled.get(run_id)
            if polls is not None:
                self._cancelled[run_id] = polls + 1
                forced = run_id in self._forced and self._force_cancel_works
                if polls >= self._cancel_lands_after or forced:
                    run["status"] = "completed"
                    run["conclusion"] = self._completes_as
                    if self._attempt_after_cancel is not None:
                        run["run_attempt"] = self._attempt_after_cancel
            return run
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str) -> None:
        self.posts.append(path)
        for suffix, code in self._refuse.items():
            if path.endswith(suffix):
                raise wd.ApiError(code, "refused by fake")
        run_id = int(path.split("/runs/")[1].split("/")[0])
        if path.endswith("/force-cancel"):
            self._forced.add(run_id)
        elif path.endswith("/cancel"):
            self._cancelled[run_id] = 0
        elif path.endswith("/rerun"):
            self._rerun_seen = True
        for suffix, left in list(self._ambiguous.items()):
            if path.endswith(suffix) and left > 0:
                self._ambiguous[suffix] = left - 1
                raise wd.ApiError(0, "TimeoutError: response lost", ambiguous=True)


def _policy(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        repo=REPO,
        now=NOW,
        orphan_after=timedelta(minutes=15),
        group_wait_after=timedelta(minutes=30),
        max_attempt=3,
        dry_run=False,
        max_runs=5,
        list_cap=50,
        heal_budget=timedelta(seconds=300),
        force_cancel_after=timedelta(seconds=90),
        recovery_window=timedelta(minutes=90),
        tick_budget=timedelta(seconds=540),
    )
    kwargs.update(overrides)
    return wd.Policy(**kwargs)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _tick(clock: _Clock, budget: float = 540.0) -> Any:
    """A Tick as run_watchdog builds one, for tests that drive a heal step directly."""
    return wd.Tick(
        clock=clock.now,
        sleep=clock.sleep,
        deadline=clock.now() + budget,
        started_at=NOW,
        started_clock=clock.now(),
    )


def _sweep(api: FakeApi, **overrides: Any) -> tuple[list[Any], dict[int, str]]:
    clock = _Clock()
    return wd.run_watchdog(
        api, _policy(**overrides), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )


def _verdict_of(verdicts: list[Any], run_id: int) -> Any:
    return next(v for v in verdicts if v.run_id == run_id)


def _posts(api: FakeApi, suffix: str) -> list[str]:
    return [p for p in api.posts if p.endswith(suffix)]


# ── the orphaned shape ──────────────────────────────────────────────────────


def test_an_orphaned_codebuild_job_gets_the_run_cancelled_and_rerun() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(10, status="completed", codebuild=False), _job(11, large=True)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.ORPHANED
    assert [o.job_id for o in verdict.orphans] == [11]
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


def test_the_label_prefix_is_matched_even_with_an_override_suffix() -> None:
    assert wd.is_codebuild_job({"labels": ["codebuild-p-1-1 instance-size:large"]})
    assert wd.is_codebuild_job({"labels": ["ubuntu-latest", "codebuild-p-1-1"]})
    assert not wd.is_codebuild_job({"labels": ["ubuntu-latest"]})
    assert not wd.is_codebuild_job({"labels": []})
    assert not wd.is_codebuild_job({})


def test_the_rerun_is_the_whole_run_so_the_runner_label_is_recomputed() -> None:
    """A failed-jobs re-run would reuse the first attempt's `changes` outputs and its stale label."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/1/rerun"
    assert not any(p.endswith("/rerun-failed-jobs") for p in api.posts)


def test_dry_run_reads_everything_and_writes_nothing() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api, dry_run=True)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_DRY_RUN}
    assert api.posts == []
    assert api.gets  # the detection still happened


# ── slow is not dead: CodeBuild saturation holds the watchdog back ──────────


def test_a_recent_slow_codebuild_start_means_saturation_and_nothing_is_healed() -> None:
    """Another routed job waited 6 min for a runner and started 2 min ago: CodeBuild is queueing."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {
            1: [_job(11)],
            2: [
                _job(
                    21,
                    status="in_progress",
                    minutes_ago=8,
                    started_minutes_ago=2,
                    runner_name="r",
                    run_id=2,
                )
            ],
        },
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_SATURATED
    assert "job-21" in verdict.detail and "6 min" in verdict.detail
    assert outcomes == {}
    assert api.posts == []


def test_saturation_evidence_inside_a_young_run_still_counts() -> None:
    """A 6-minute wait on a job that started 2 minutes ago, in a run created 9 minutes ago."""
    young = _run(2, minutes_ago=9, branch="other")
    slow = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi({"in_progress": [_run(1), young]}, {1: [_job(11)], 2: [slow]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_YOUNG
    assert outcomes == {}
    assert api.posts == []


def _jobs_change_on_later_reads(api: FakeApi, run_id: int, later: list[dict[str, Any]]) -> None:
    """The first jobs read of ``run_id`` answers as configured; every later one answers ``later``."""
    original_get = api.get
    seen = {"n": 0}

    def get(path: str) -> Any:
        if f"/actions/runs/{run_id}/jobs" in path:
            seen["n"] += 1
            if seen["n"] > 1:
                api.gets.append(path)
                return {"jobs": later}
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_dispatch_evidence_is_re_read_before_the_cancel_and_a_fresh_saturation_holds() -> None:
    """At the top of the tick the sibling had started within 20 s (dispatching); by the time
    the cancel is about to go out it shows a 6-minute wait. The stale evidence must not
    cancel a run whose queued job the fleet may still pick up."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    slow = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    _jobs_change_on_later_reads(api, 2, [slow])
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert api.posts == []
    assert outcomes == {}
    assert any("re-checked before the cancel" in line for line in logged)


def test_the_pre_cancel_hold_is_judged_from_the_freshly_read_orphans_not_the_stale_set() -> None:
    """At the top of the tick the run had one orphan (queued 60 min ago) and a sibling start
    25 min ago cleared the hold. By the re-read a second matrix job has crossed the
    threshold (queued 20 min ago); judged from IT, that start proves nothing, and the run
    is held instead of cancelled."""
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="c",
    )
    started_between = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60)], 3: [started_between]},
        evidence=False,
    )
    _jobs_change_on_later_reads(api, 1, [_job(11, minutes_ago=60), _job(12, minutes_ago=20)])
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert [o.job_id for o in verdict.orphans] == [11, 12]  # the summary shows what was read
    assert api.posts == []
    assert outcomes == {}


def test_the_evidence_sweep_runs_before_the_runs_re_read_so_a_hand_rerun_during_it_is_seen() -> (
    None
):
    """A human re-runs the target while the fleet is being re-read. The target's own re-read
    comes AFTER that sweep, immediately before the cancel, so it sees the new attempt and
    leaves the run alone instead of cancelling on a verdict that went stale during the sweep."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    original_get = api.get
    reads = {"n": 0}

    def hand_rerun_during_the_sweep(path: str) -> Any:
        if "/actions/runs/2/jobs" in path:
            reads["n"] += 1
            if reads["n"] == 2:  # the pre-cancel evidence sweep, not the top-of-tick scan
                api.run_overrides[1] = _run(1, attempt=2)
        return original_get(path)

    api.get = hand_rerun_during_the_sweep  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_evidence_that_cannot_be_re_read_before_the_cancel_fails_closed() -> None:
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    original_get = api.get
    seen = {"n": 0}

    def get(path: str) -> Any:
        if "/actions/runs/2/jobs" in path:
            seen["n"] += 1
            if seen["n"] > 1:
                raise wd.ApiError(502, "bad gateway")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert "could not be re-read" in verdict.detail
    assert api.posts == []
    assert outcomes == {}


def test_evidence_still_fresh_at_cancel_time_heals_as_before() -> None:
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    # The fleet was re-read exactly once for the cancel, not once per run.
    assert sum("/actions/runs/2/jobs" in path for path in api.gets) == 2


def test_a_prompt_recent_codebuild_start_is_not_saturation() -> None:
    """The sibling waited 20 s: CodeBuild is dispatching normally, so the queued job is dead."""
    started = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [started]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_an_old_slow_start_is_outside_the_saturation_lookback() -> None:
    """A slow start 45 min ago says nothing about CodeBuild now."""
    old_slow = _job(
        21, status="completed", minutes_ago=55, started_minutes_ago=45, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=60, branch="other")]},
        {1: [_job(11)], 2: [old_slow]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_dispatch_evidence_ignores_hosted_never_started_and_carried_over_jobs() -> None:
    since = NOW - timedelta(minutes=60)
    hosted_slow = _job(
        31,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=2,
        runner_name="r",
        codebuild=False,
    )
    # Carried over from an earlier attempt: created (at the re-run) AFTER it started.
    carried = _job(32, status="completed", minutes_ago=2, started_minutes_ago=30, runner_name="r")
    never_started = _job(33, minutes_ago=20)
    evidence = wd.DispatchEvidence()
    evidence.absorb([hosted_slow, carried, never_started], _policy())
    assert evidence.inconclusive(since, _policy()) and not evidence.saturated(since, _policy())
    prompt = _job(34, status="in_progress", minutes_ago=3, started_minutes_ago=2.8, runner_name="r")
    evidence.absorb([prompt], _policy())
    assert not evidence.inconclusive(since, _policy()) and not evidence.saturated(since, _policy())
    slow = _job(35, status="in_progress", minutes_ago=9, started_minutes_ago=3, runner_name="r")
    evidence.absorb([slow], _policy())
    slowest = evidence.slowest(since, _policy())
    assert (
        slowest is not None and slowest.job_id == 35 and slowest.queued_for == timedelta(minutes=6)
    )
    # Relative to an orphan that queued AFTER both starts, neither counts.
    assert evidence.inconclusive(NOW - timedelta(minutes=1), _policy())
    # Judged later in a slow tick, a start that has aged past the lookback drops out.
    later = _policy(now=NOW + timedelta(minutes=28))
    assert evidence.inconclusive(since, later)


def test_a_prompt_start_from_before_the_orphan_queued_is_not_evidence_the_fleet_is_alive() -> None:
    """The onset of an outage: the fleet was fine 25 min ago, the orphan queued 20 min ago, nothing since."""
    orphan_run = _run(1, minutes_ago=60)
    healthy_before = _run(
        2,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="other",
    )
    started_before = _job(
        21,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [orphan_run], "completed": [healthy_before]},
        {1: [_job(11, minutes_ago=20)], 2: [started_before]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {}
    assert api.posts == []


def test_a_prompt_start_after_the_orphan_queued_clears_the_hold() -> None:
    orphan_run = _run(1, minutes_ago=60)
    healthy_after = _run(
        2,
        minutes_ago=12,
        status="completed",
        conclusion="success",
        updated_minutes_ago=8,
        branch="other",
    )
    started_after = _job(
        21,
        status="completed",
        conclusion="success",
        minutes_ago=11,
        started_minutes_ago=10.7,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [orphan_run], "completed": [healthy_after]},
        {1: [_job(11, minutes_ago=20)], 2: [started_after]},
        evidence=False,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_no_recent_codebuild_start_anywhere_is_inconclusive_and_heals_nothing() -> None:
    """A total fleet outage looks exactly like an orphan from the queued side: hold and say so."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, evidence=False)
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert "rollback" in verdict.detail
    assert outcomes == {}
    assert api.posts == []
    assert any("status=completed" in p for p in api.gets)  # the completed sample was consulted


def test_a_prompt_start_in_a_recently_completed_run_is_enough_evidence() -> None:
    """No live run shows a start, but the newest completed run finished a CodeBuild job promptly."""
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}
    )  # default evidence comes from a completed run
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_the_completed_sample_is_not_read_when_nothing_is_orphaned() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="completed", codebuild=False)]})
    _sweep(api)
    assert not any("status=completed" in p for p in api.gets)


# ── things that look stuck but are not ──────────────────────────────────────


def test_a_queued_hosted_job_is_not_an_orphan() -> None:
    """Only CodeBuild labels are run-attempt specific; a hosted queue is just slow."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, codebuild=False)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert outcomes == {}
    assert api.posts == []


def test_a_codebuild_job_queued_under_the_threshold_is_left_alone() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, minutes_ago=14)]})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert api.posts == []


def test_a_running_codebuild_job_is_not_an_orphan() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="in_progress")]})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert api.posts == []


def test_a_young_run_is_never_actionable() -> None:
    """Its jobs are still read (they may carry saturation evidence), but nothing is done to it."""
    api = FakeApi({"in_progress": [_run(1, minutes_ago=5)]}, {1: [_job(11, minutes_ago=5)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_YOUNG
    assert outcomes == {}
    assert api.posts == []


def test_a_run_with_no_jobs_is_waiting_on_its_group_not_on_a_runner() -> None:
    api = FakeApi({"pending": [_run(1, minutes_ago=45, status="pending")]}, {1: []})
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.WAITING_ON_GROUP
    assert "concurrency group" in verdict.detail
    assert outcomes == {}
    assert api.posts == []


def test_a_jobless_run_under_the_group_threshold_is_just_healthy() -> None:
    api = FakeApi({"pending": [_run(1, minutes_ago=20, status="pending")]}, {1: []})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY


# ── guard rails ─────────────────────────────────────────────────────────────


def test_fork_runs_are_reported_but_never_touched() -> None:
    api = FakeApi({"in_progress": [_run(1, fork=True)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_FORK
    assert outcomes == {}
    assert api.posts == []


def test_a_head_repository_with_another_name_counts_as_a_fork() -> None:
    run = _run(1)
    run["head_repository"] = {"fork": False, "full_name": "Other-Org/example-repo"}
    assert wd.is_fork_run(run, REPO)
    run["head_repository"] = {"fork": False, "full_name": REPO.upper()}
    assert not wd.is_fork_run(run, REPO)
    run["head_repository"] = None
    assert not wd.is_fork_run(run, REPO)


def test_the_attempt_cap_stops_a_run_that_keeps_orphaning() -> None:
    """Each intervention bumps the attempt, so the cap bounds interventions per run."""
    api = FakeApi(
        {"in_progress": [_run(1, attempt=3), _run(2, attempt=2, branch="other")]},
        {1: [_job(11, attempt=3)], 2: [_job(21, attempt=2)]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_ATTEMPT_CAP
    assert _verdict_of(verdicts, 2).verdict == wd.ORPHANED
    assert outcomes == {2: wd.OUTCOME_HEALED}
    assert all("/runs/2/" in path for path in api.posts)


def test_the_per_invocation_cap_leaves_the_rest_for_the_next_tick_oldest_first() -> None:
    runs = [_run(i, minutes_ago=100 - i, branch=f"pr-{i}") for i in range(1, 8)]  # 1 is the oldest
    api = FakeApi(
        {"in_progress": list(reversed(runs))},  # the API lists newest first
        {i: [_job(i * 10, run_id=i)] for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    healed = sorted(run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED)
    assert healed == [1, 2, 3, 4, 5]
    assert outcomes[6] == outcomes[7] == wd.OUTCOME_NOT_ATTEMPTED
    assert len(_posts(api, "/cancel")) == 5


def test_a_run_listed_under_two_statuses_is_inspected_once() -> None:
    run = _run(1)
    api = FakeApi({"in_progress": [run], "queued": [run]}, {1: [_job(11)]})
    verdicts, _ = _sweep(api)
    assert [v.run_id for v in verdicts] == [1]
    assert len(_posts(api, "/cancel")) == 1


def test_the_listing_is_capped_and_paginated() -> None:
    runs = [_run(i, minutes_ago=200 - i) for i in range(1, 131)]
    api = FakeApi({"in_progress": list(reversed(runs))}, {})
    listed = wd.list_candidate_runs(api, REPO, cap=120)
    assert len(listed) == 120
    assert sorted(int(r["id"]) for r in listed) == list(range(11, 131))  # the 120 newest
    pages = [p for p in api.gets if "status=in_progress" in p]
    assert len(pages) == 2
    assert "per_page=100" in pages[0] and "page=1" in pages[0]
    # The second page keeps the page size: `page` is an offset in units of
    # `per_page`, so a 20-run second page would re-read runs 21-40 instead.
    assert "per_page=100" in pages[1] and "page=2" in pages[1]


def test_jobs_are_paginated_past_one_page() -> None:
    jobs = [_job(i, status="completed", codebuild=False) for i in range(1, 151)]
    jobs.append(_job(999))
    api = FakeApi({"in_progress": [_run(1)]}, {1: jobs})
    verdicts, _ = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.ORPHANED
    assert [o.job_id for o in verdict.orphans] == [999]
    # Listed once for the verdict, once for the dispatch evidence re-read that
    # precedes the heal, and once again immediately before the cancel.
    assert len([p for p in api.gets if "/runs/1/jobs" in p]) == 6


# ── the heal re-verifies before it cancels ──────────────────────────────────


def test_a_run_a_human_rerun_between_listing_and_heal_is_not_cancelled() -> None:
    """The verdict was computed for attempt 1; the live run is attempt 2. Hands off."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    api.run_overrides[1] = _run(1, attempt=2)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_a_run_that_completed_before_the_heal_is_not_cancelled() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    api.run_overrides[1] = _run(1, status="completed", conclusion="success")
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_a_run_whose_job_got_a_runner_before_the_heal_is_not_cancelled() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    # The job flips to running after the listing read and stays running, so the
    # pre-cancel read (the last of the reads) sees a job that has a runner.
    reads = {"n": 0}
    original_get = api.get

    def flipping_get(path: str) -> Any:
        payload = original_get(path)
        if "/runs/1/jobs" in path:
            reads["n"] += 1
            if reads["n"] >= 2:
                payload = {"jobs": [_job(11, status="in_progress")]}
        return payload

    api.get = flipping_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


# ── once cancelled, the watchdog owns the run until it is re-run ────────────


def test_a_refused_cancel_is_logged_and_the_run_is_left_for_the_next_tick() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/cancel": 409})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_CANCEL_FAILED}
    assert _posts(api, "/rerun") == []


def test_the_rerun_waits_for_the_cancel_to_land() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=3)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    # One read before the cancel, then three not-yet-completed reads, then the completed one.
    polls = [p for p in api.gets if p.endswith("/actions/runs/1")]
    assert len(polls) == 5
    assert _posts(api, "/force-cancel") == []


def test_a_slow_cancel_is_escalated_to_force_cancel_and_then_rerun() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=10_000)
    _, outcomes = _sweep(api, force_cancel_after=timedelta(seconds=30))
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert len(_posts(api, "/force-cancel")) == 1
    cancel_i = api.posts.index(f"repos/{REPO}/actions/runs/1/cancel")
    force_i = api.posts.index(f"repos/{REPO}/actions/runs/1/force-cancel")
    rerun_i = api.posts.index(f"repos/{REPO}/actions/runs/1/rerun")
    assert cancel_i < force_i < rerun_i


def test_a_cancel_that_never_lands_exhausts_the_budget_without_rerunning() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=60), force_cancel_after=timedelta(seconds=20)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    assert _posts(api, "/rerun") == []
    assert len(_posts(api, "/force-cancel")) == 1
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_the_wait_budget_is_shared_across_runs_not_multiplied() -> None:
    """Five slow cancels must cost one budget, not five -- the job has a fixed ceiling."""
    api = FakeApi(
        {"in_progress": [_run(i, minutes_ago=100 - i) for i in range(1, 6)]},
        {i: [_job(i * 10, run_id=i)] for i in range(1, 6)},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=120)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _l: None,
    )
    assert set(outcomes.values()) == {wd.OUTCOME_CANCEL_TIMED_OUT}
    assert clock.t <= 130


def test_the_cancel_wait_stops_early_enough_to_leave_the_rerun_reserve() -> None:
    """A cancel that lands with too little of the tick left to verify its re-run would
    be a mutation the job ceiling can interrupt; the wait ends before that point."""
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=300), tick_budget=timedelta(seconds=300)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _l: None,
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    # 300 s budget minus the 240 s reserve: the wait gave up around 60 s, not 300.
    assert clock.t <= 300 - wd.RERUN_RESERVE_SECONDS + 10, clock.t


def test_a_rerun_late_in_the_tick_is_deferred_not_started() -> None:
    """Two orphans are cancelled while the reserve still fits; the first one's re-run then
    eats into it (slow branch listings), so the second is not begun: started, a worst-case
    restoration would outlive the job. Deferred, the run is named and the next tick's
    recovery pass re-runs it."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    clock = _Clock()
    # Run 1's three lookups (one before its POST, two after) cost 60 s; with 30 s
    # of slack over the reserve, its own POST still fits (checked at 20 s) and
    # run 2's, checked at 65 s, does not.
    _slow_reads(api, clock, r"[?&]branch=main", 20.0)
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS + 30)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_HEALED, 2: wd.OUTCOME_RERUN_DEFERRED}
    assert _posts(api, "/cancel") == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/cancel",
    ]
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    assert wd.OUTCOME_RERUN_DEFERRED in wd.FAILED_OUTCOMES


def test_the_rerun_reserve_is_checked_again_right_before_the_post_not_only_before_the_lookup() -> (
    None
):
    """The branch lookup between the reserve check and the POST is itself slow enough to
    eat the reserve: the POST must not go out on a check that is already stale."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    clock = _Clock()
    _slow_reads(api, clock, r"[?&]branch=main", 20.0)
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS + 10)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_DEFERRED}
    assert _posts(api, "/rerun") == []
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_cancel_is_not_posted_when_its_rerun_could_not_be_verified_in_time() -> None:
    """With less than the reserve left, cancelling would discard the run's finished jobs
    and leave it cancelled until the next tick; untouched, it loses nothing by waiting."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS - 1)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_NOT_ATTEMPTED}
    assert api.posts == []
    assert any("left untouched for the next tick" in line for line in logged)


def test_pre_cancel_evidence_is_judged_against_the_current_wall_time_not_the_ticks_start() -> None:
    """A sibling started promptly 25 min before the tick began -- inside the 30-min lookback,
    so the orphan is healable. The tick is slow: 400 s pass before the cancel. Re-read at
    that moment the start is 31.7 min old, outside the lookback; with nothing newer the
    fleet's state is unknown and the cancel is held, exactly as a fresh tick would."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=25.3,
        started_minutes_ago=25,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
        evidence=False,
    )
    clock = _Clock()
    _slow_reads(api, clock, r"/actions/runs/1$", 400.0)  # the pre-cancel re-read is slow
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert api.posts == []
    assert outcomes == {}


def test_the_rerun_reserve_covers_the_longest_restoration_chain_under_the_job_ceiling() -> None:
    """The reserve is derived from the chain's own constants, and the workflow's
    `timeout-minutes` sits above the tick budget with room for checkout and the
    summary, so the ceiling is never what ends a restoration."""
    chain = (wd.MAX_RESTORE_DEPTH + 1) * (
        wd.POST_RERUN_SETTLE_SECONDS + 2 * wd.SUCCESSOR_SETTLE_SECONDS
    )
    assert wd.RESTORE_CHAIN_SECONDS == chain
    assert wd.RERUN_RESERVE_SECONDS >= chain + 30
    tick_budget = wd.Policy(repo=REPO, now=NOW).tick_budget.total_seconds()
    assert (
        tick_budget
        >= wd.RERUN_RESERVE_SECONDS + wd.Policy(repo=REPO, now=NOW).heal_budget.total_seconds()
    )
    timeout_lines = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("timeout-minutes:")
    ]
    assert len(timeout_lines) == 1, timeout_lines
    timeout_minutes = int(timeout_lines[0].split(":", 1)[1].strip())
    assert timeout_minutes * 60 >= tick_budget + 120


def test_runs_are_rerun_as_each_cancel_lands_not_after_the_slowest() -> None:
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
        cancel_lands_after=2,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED, 2: wd.OUTCOME_HEALED}
    assert len(_posts(api, "/rerun")) == 2


def test_a_run_superseded_between_cancel_and_rerun_is_not_rerun_into_its_successor() -> None:
    """A new push landed after the cancel: re-running the old run would cancel the new one."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]}, {1: [_job(11)]}, newest_by_branch={"pr": 2}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert _posts(api, "/rerun") == []
    assert any("branch=pr" in p and "event=push" in p for p in api.gets)


def test_a_run_that_finished_on_its_own_before_the_cancel_landed_is_not_rerun() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=2, completes_as="success"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_COMPLETED_ON_ITS_OWN}
    assert _posts(api, "/rerun") == []


def test_a_cancel_that_hit_a_hand_rerun_attempt_is_followed_by_a_restoring_rerun() -> None:
    """The cancel was verified against attempt 1 but landed on attempt 2: re-run, and say so."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, attempt_after_cancel=2)
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("::warning::" in line and "re-run by hand" in line for line in logged)


def test_a_successor_that_appears_during_the_rerun_is_restored_and_the_rerun_cancelled() -> None:
    """A push landed between the newest check and the re-run: yield to it."""
    successor = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = successor
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/rerun",
    ]


def test_a_successor_still_running_when_the_window_closes_is_unsettled_not_settled() -> None:
    """A group cancel can land late and a run mid-cancellation reports in_progress until it
    does, so a successor that has not reached a terminal state by the end of the window is
    never called settled: the tick fails, names it, and says what to type if it ends cancelled."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"] * 2
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    # Watched for the whole settle window, not given up on after the first read.
    assert len([p for p in api.gets if p.endswith("/actions/runs/2")]) >= 1 + int(
        wd.SUCCESSOR_SETTLE_SECONDS // wd.POST_RERUN_SETTLE_SECONDS
    )


def test_a_successor_that_finished_on_its_own_is_left_alone() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="success", branch="pr"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_FINISHED}
    assert not any(p.endswith("/runs/2/rerun") for p in api.posts)


def test_a_successor_that_turns_out_to_be_cancelling_mid_window_is_not_called_live() -> None:
    """Live jobs at first, then one concludes cancelled: the group was cancelling it all along."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"


def test_the_successor_rerun_is_bracketed_too_and_yields_to_a_run_landing_in_its_window() -> None:
    """Run 1's re-run yielded to run 2; run 2's re-run in turn sees run 3 appear and yields to it."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [],
            3: [_job(31, status="in_progress", minutes_ago=1, codebuild=False, run_id=3)],
        },
        newest_after_rerun_sequence={"pr": [2, 2, 3]},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
    )
    api.run_overrides[3] = _run(3, minutes_ago=0.5, status="in_progress", branch="pr")
    _, outcomes = _sweep(api)
    # Run 3 is still running when its window closes: unsettled, not settled.
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/rerun",
        f"repos/{REPO}/actions/runs/2/cancel",
    ]


def test_a_chain_of_superseding_pushes_is_not_chased_past_the_depth_cap() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: [], 3: [], 4: []},
        newest_after_rerun_sequence={"pr": [2, 2, 3, 3, 4]},
    )
    for run_id in (2, 3, 4):
        api.run_overrides[run_id] = _run(
            run_id, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
        )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert not any(p.endswith("/runs/4/rerun") for p in api.posts)
    assert any("not chasing further" in line and "gh run rerun 4" in line for line in logged)


def test_the_successor_is_not_judged_while_our_own_rerun_is_still_cancelling() -> None:
    """Our re-run's cancel never lands: force-cancel is tried, then the tick fails rather than guesses.

    Exercised on _restore_successor directly: through the full sweep the heal's own
    cancel would time out first and never reach a re-run."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    verdict = wd._base_verdict(_run(1, branch="pr"), NOW)
    logged: list[str] = []
    clock = _Clock()
    outcome = wd._restore_successor(
        api, f"repos/{REPO}/actions/runs/1", verdict, 2, _policy(), logged.append, tick=_tick(clock)
    )
    assert outcome == wd.OUTCOME_OWN_RERUN_UNCANCELLED
    assert wd.OUTCOME_OWN_RERUN_UNCANCELLED in wd.FAILED_OUTCOMES
    assert _posts(api, "/force-cancel") == [f"repos/{REPO}/actions/runs/1/force-cancel"]
    assert _posts(api, "/rerun") == []
    assert not any("/runs/2/jobs" in p for p in api.gets)  # never judged
    assert any("::error::" in line and "run 2" in line for line in logged)


def test_a_slow_own_rerun_cancel_is_force_cancelled_and_then_the_successor_is_judged() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        cancel_lands_after=10_000,  # a plain cancel never lands; force-cancel does
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    verdict = wd._base_verdict(_run(1, branch="pr"), NOW)
    clock = _Clock()
    outcome = wd._restore_successor(
        api,
        f"repos/{REPO}/actions/runs/1",
        verdict,
        2,
        _policy(),
        lambda _l: None,
        tick=_tick(clock),
    )
    # The successor was judged (force-cancel got our re-run out of the way) and, still
    # running at the end of its window, is unsettled rather than called settled.
    assert outcome == wd.OUTCOME_SUCCESSOR_UNSETTLED
    assert _posts(api, "/force-cancel") == [f"repos/{REPO}/actions/runs/1/force-cancel"]
    assert any(p.endswith("/actions/runs/2") for p in api.gets)  # it WAS judged
    assert len(_posts(api, "/force-cancel")) == 1


def test_a_cancelled_successor_that_was_itself_superseded_is_not_rerun() -> None:
    """A yet-newer run took the branch over: it carries the verdict, nothing is re-run."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun_sequence={"pr": [2, 3]},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_SUPERSEDED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert wd.OUTCOME_SUCCESSOR_SUPERSEDED not in wd.FAILED_OUTCOMES


def _slow_reads(api: FakeApi, clock: _Clock, pattern: str, seconds: float) -> None:
    """Every GET matching ``pattern`` costs ``seconds`` of wall clock, as a slow API would."""
    original_get = api.get

    def get(path: str) -> Any:
        if re.search(pattern, path):
            clock.t += seconds
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_the_settle_windows_are_wall_clock_so_slow_reads_cannot_stretch_them() -> None:
    """Each successor read takes 20 s. Counting sleeps alone, seven reads would stretch the
    30 s window to nearly three minutes of real time; measured on the clock, the window
    closes on schedule and the verdict is the same."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    _slow_reads(api, clock, r"/actions/runs/2$", 20.0)
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    # 5 s settle + a 30 s window + at most one read that overran it.
    assert clock.t <= 5 + wd.SUCCESSOR_SETTLE_SECONDS + 20 + wd.POST_RERUN_SETTLE_SECONDS, clock.t


def test_a_nested_rerun_without_time_left_names_the_successor_instead_of_starting() -> None:
    """Slow branch listings eat the budget before the successor ends cancelled; re-running it
    now could not be verified inside the tick, so it is reported lost by name rather than
    started and cut off by the job ceiling."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    _slow_reads(api, clock, r"[?&]branch=", 100.0)
    # The budget carries the root re-run's own slow pre-POST lookup on top of
    # the reserve, so the root is re-run; the successor's lookup then leaves
    # less than the nested reserve.
    budget = wd.RERUN_RESERVE_SECONDS + 100.0
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=budget)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    assert clock.t <= budget, clock.t  # never past the tick deadline


def test_a_successor_that_never_settles_is_a_failed_outcome_not_a_guess() -> None:
    """Status in_progress, no job live, none cancelled yet: cannot tell, so fail and keep ownership."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert any("::error::" in line and "gh run rerun 2" in line for line in logged)
    assert wd.OUTCOME_SUCCESSOR_UNSETTLED in wd.FAILED_OUTCOMES


def test_a_successor_cancelled_asynchronously_is_still_restored() -> None:
    """The first reads after yielding still show the successor in_progress with a cancelled job;
    the run-level cancellation lands later."""
    live = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [
                _job(21, status="completed", conclusion="cancelled", minutes_ago=1, codebuild=False)
            ],
        },
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (2, cancelled)},
    )
    api.run_overrides[2] = live
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"


def test_a_successor_whose_rerun_is_refused_reds_the_tick() -> None:
    successor = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
        refuse={"/runs/2/rerun": 403},
    )
    api.run_overrides[2] = successor
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}


def test_a_same_named_fork_branch_does_not_masquerade_as_the_newest_run() -> None:
    """The branch listing matches names; a fork's `main` is listed first but is not ours."""
    listing = [
        {"id": 2, "head_repository": {"full_name": "someone/example-repo"}},
        {"id": 1, "head_repository": {"full_name": REPO}},
    ]
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": listing})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_a_cancelled_orphan_is_still_recovered_past_a_same_named_fork_branch() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    listing = [
        {"id": 2, "head_repository": {"full_name": "someone/example-repo"}},
        {"id": 1, "head_repository": {"full_name": REPO}},
    ]
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": listing}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_RECOVERED}


def test_the_post_rerun_check_settles_before_declaring_the_heal_done() -> None:
    api = FakeApi({"in_progress": [_run(1, branch="pr")]}, {1: [_job(11)]})
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert len([p for p in api.gets if "branch=pr" in p]) == 3  # before, after, after settle
    assert clock.t >= wd.POST_RERUN_SETTLE_SECONDS


def test_a_refused_rerun_explained_by_someone_elses_rerun_is_superseded_not_failed() -> None:
    """Refused because the run is in flight again: somebody re-ran it between the cancel and our POST."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/runs/1/rerun": 403})
    reads = {"n": 0}
    original_get = api.get

    def in_flight_after_refusal(path: str) -> Any:
        payload = original_get(path)
        if path.endswith("/actions/runs/1") and payload.get("status") == "completed":
            reads["n"] += 1
            if reads["n"] >= 2:  # the read AFTER the refused POST sees a fresh attempt running
                payload = {**payload, "status": "in_progress", "run_attempt": 2, "conclusion": None}
        return payload

    api.get = in_flight_after_refusal  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}


def test_a_refused_rerun_after_cancelling_a_hand_rerun_attempt_is_still_a_failure() -> None:
    """The cancel landed on attempt 2 (a human's re-run); the refused re-run must compare against 2, not 1."""
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        refuse={"/runs/1/rerun": 403},
        attempt_after_cancel=2,
    )
    # After the cancel the run reports attempt 2 and stays completed/cancelled at attempt 2.
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)


def test_a_refused_rerun_whose_read_back_also_fails_is_a_failure_not_superseded() -> None:
    """During an API incident the re-run POST is refused AND the confirming read fails.
    With nothing to explain the refusal, the outcome must fail closed and name the run,
    never guess that somebody else re-ran it."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/runs/1/rerun": 403})
    _failing_reads(api, r"/actions/runs/1$", times=None, after_post="/runs/1/rerun")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)
    assert not any("already re-run by someone else" in line for line in logged)


def test_an_ambiguous_cancel_is_owned_and_reconciled_not_re_posted() -> None:
    """The cancel landed server-side but its response was lost. Re-posting would hit a
    conflict on the cancelling run and read as a refusal, stranding it cancelled; instead
    the run is owned and polled like any accepted cancel, and re-run when it completes."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, ambiguous={"/runs/1/cancel": 1})
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]  # posted ONCE
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("ambiguous result" in line for line in logged)


def test_an_ambiguous_rerun_that_landed_is_verified_like_a_clean_one() -> None:
    """The re-run POST's response was lost, but the run is in flight again: it landed, and
    it gets the same post-re-run verification as a clean POST rather than 'superseded'."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, ambiguous={"/runs/1/rerun": 1})
    original_get = api.get

    def in_flight_once_rerun_was_posted(path: str) -> Any:
        payload = original_get(path)
        if path.endswith("/actions/runs/1") and any(p.endswith("/runs/1/rerun") for p in api.posts):
            payload = {**payload, "status": "in_progress", "run_attempt": 2, "conclusion": None}
        return payload

    api.get = in_flight_once_rerun_was_posted  # type: ignore[method-assign]
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]  # posted ONCE
    assert any("it landed" in line for line in logged)
    assert not any("already re-run by someone else" in line for line in logged)


def test_an_ambiguous_rerun_that_did_not_land_is_a_failure_that_names_the_run() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_post = api.post

    def lost_without_landing(path: str) -> None:
        if path.endswith("/runs/1/rerun"):
            api.posts.append(path)
            raise wd.ApiError(0, "TimeoutError: response lost", ambiguous=True)
        original_post(path)

    api.post = lost_without_landing  # type: ignore[method-assign]
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_refused_rerun_nobody_else_explains_is_a_failed_outcome() -> None:
    """403 with the run still cancelled at our attempt: nobody re-ran it, the verdict is lost -- say so."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
        refuse={"/runs/1/rerun": 403},
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED, 2: wd.OUTCOME_HEALED}
    assert wd.OUTCOME_RERUN_REFUSED in wd.FAILED_OUTCOMES
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)


def test_a_branch_listing_that_never_reaches_our_run_fails_closed() -> None:
    """An empty (or fork-only) listing is 'cannot tell', never 'still newest'."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": []})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []
    assert wd.OUTCOME_LOOKUP_FAILED in wd.FAILED_OUTCOMES


def test_a_listing_that_shows_an_older_run_but_not_ours_is_inconclusive_not_superseded() -> None:
    """An older same-repo run first with the judged run absent contradicts newest-first ordering."""
    older_only = [{"id": 0, "head_repository": {"full_name": REPO}}]
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": older_only}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []


def test_the_hold_is_judged_per_orphan_relative_to_its_own_queue_time() -> None:
    """One prompt start 25 min ago: evidence for an orphan queued 60 min ago, none for one queued 20 min ago."""
    older = _run(1, minutes_ago=90, branch="a")
    younger = _run(2, minutes_ago=40, branch="b")
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="c",
    )
    started = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [older, younger], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60)], 2: [_job(21, minutes_ago=20, run_id=2)], 3: [started]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_a_run_with_several_orphans_is_judged_from_its_newest_orphans_queue_time() -> None:
    """A matrix run queued one job 60 min ago and another 20 min ago. A start 25 min ago says
    the fleet was alive between them -- nothing about the fleet the younger one is waiting
    on. The hold must be judged from the newest orphan, not the oldest."""
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="c",
    )
    started_between = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60), _job(12, minutes_ago=20)], 3: [started_between]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {}
    assert api.posts == []


def test_a_start_after_the_newest_orphan_queued_clears_the_hold_for_the_whole_run() -> None:
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=9,
        branch="c",
    )
    started_after = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=10.5,
        started_minutes_ago=10.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60), _job(12, minutes_ago=20)], 3: [started_after]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_the_branch_listing_is_paged_past_a_wall_of_same_named_fork_runs() -> None:
    fork = {"id": 900, "head_repository": {"full_name": "someone/example-repo"}}
    page1 = [dict(fork, id=900 + i) for i in range(wd.BRANCH_LISTING_DEPTH)]
    page2 = [{"id": 1, "head_repository": {"full_name": REPO}}]
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": page1})
    original_get = api.get

    def paged_get(path: str) -> Any:
        if "branch=main" in path and "page=2" in path:
            return {"workflow_runs": page2}
        return original_get(path)

    api.get = paged_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_main_exits_nonzero_when_a_heal_ran_out_of_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    monkeypatch.setattr(
        wd, "run_watchdog", lambda *_a, **_k: ([], {1: wd.OUTCOME_CANCEL_TIMED_OUT})
    )
    assert wd.main(argv) == 1
    for outcome in sorted(wd.FAILED_OUTCOMES):
        monkeypatch.setattr(wd, "run_watchdog", lambda *_a, _o=outcome, **_k: ([], {1: _o}))
        assert wd.main(argv) == 1, outcome
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: ([], {1: wd.OUTCOME_HEALED}))
    assert wd.main(argv) == 0
    assert summary.read_text(encoding="utf-8").count("## CI runner watchdog") == 2 + len(
        wd.FAILED_OUTCOMES
    )


# ── the API can fail mid-heal; ownership is never dropped on an exception ────


def _failing_reads(
    api: FakeApi, pattern: str, *, times: int | None, after_post: str | None = None
) -> None:
    """Make GETs of paths matching ``pattern`` raise ``ApiError`` -- ``times`` times, or
    forever when ``None`` -- optionally only once a POST ending in ``after_post`` was seen."""
    original_get = api.get
    state = {"left": times}

    def get(path: str) -> Any:
        armed = after_post is None or any(p.endswith(after_post) for p in api.posts)
        if armed and re.search(pattern, path) and (state["left"] is None or state["left"] > 0):
            if state["left"] is not None:
                state["left"] -= 1
            raise wd.ApiError(502, "bad gateway")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_a_failed_read_while_waiting_for_a_cancel_keeps_the_run_owned() -> None:
    """Two 502s on the status poll: the run stays pending, is polled again, and is re-run."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=2, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert sum("could not read" in line for line in logged) == 2


def test_reads_that_never_recover_time_the_cancel_out_instead_of_raising() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=None, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    assert _posts(api, "/rerun") == []
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_read_failure_before_the_cancel_leaves_the_run_untouched() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=None)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_NOT_ATTEMPTED}
    assert api.posts == []


def test_a_branch_listing_that_cannot_be_read_is_an_inconclusive_lookup_not_a_crash() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"[?&]branch=", times=None, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_successor_whose_reads_keep_failing_is_named_unsettled_not_abandoned() -> None:
    """The successor cannot be read for the whole window: ownership ends in a failed
    outcome that names it, never in an exception that leaves it cancelled and unnamed."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _failing_reads(api, r"/actions/runs/2$", times=None)
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)


def test_a_transient_successor_read_failure_does_not_change_the_verdict() -> None:
    """Same shape as the mid-window cancellation case, with the first successor read failing."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _failing_reads(api, r"/actions/runs/2$", times=1)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"


# ── the next tick recovers a cancel that landed after the budget ────────────


def test_a_cancelled_orphan_nobody_rerun_is_rerun_on_the_next_tick() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]},
        {
            1: [
                _job(10, status="completed", conclusion="success", codebuild=False),
                _cancelled_orphan_job(11),
            ]
        },
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_a_cancelled_run_superseded_by_a_newer_run_is_not_rerun() -> None:
    """Re-running an old pull-request run would cancel its successor through the group."""
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="pr",
    )
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"pr": 2}
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SUPERSEDED
    assert outcomes == {}
    assert api.posts == []


def test_a_cancelled_run_whose_jobs_had_runners_is_not_an_orphan() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    job = _cancelled_orphan_job(11)
    job["runner_name"] = "some-runner"
    api = FakeApi({"cancelled": [run]}, {1: [job]}, newest_by_branch={"main": 1})
    verdicts, outcomes = _sweep(api)
    assert not any(v.run_id == 1 for v in verdicts)
    assert outcomes == {}
    assert not any("branch=" in p for p in api.gets)  # the newest-run lookup was never needed


def test_a_cancelled_run_with_a_short_queue_wait_is_not_an_orphan() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11, queued_minutes=3)]},
        newest_by_branch={"main": 1},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {}
    assert api.posts == []


def test_recovery_only_looks_inside_its_window() -> None:
    run = _run(
        1, minutes_ago=300, status="completed", conclusion="cancelled", updated_minutes_ago=200
    )
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {}
    assert not any("/runs/1/jobs" in p for p in api.gets)


def test_recovery_respects_the_attempt_cap_and_the_fork_guard() -> None:
    capped = _run(
        1,
        minutes_ago=70,
        attempt=3,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="feat",
    )
    fork = _run(
        2,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        fork=True,
    )
    api = FakeApi(
        {"cancelled": [capped, fork]},
        {1: [_cancelled_orphan_job(11)], 2: [_cancelled_orphan_job(21, run_id=2)]},
        newest_by_branch={"feat": 1, "main": 2},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_ATTEMPT_CAP
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_FORK
    assert outcomes == {}
    assert api.posts == []


def test_recovery_takes_the_per_tick_cap_before_live_heals_so_a_backlog_cannot_starve_it() -> None:
    """Five live orphans every tick must not keep a cancelled orphan waiting until its
    recovery window expires: the cancelled run has already lost its verdict, the live
    ones lose nothing by waiting one more tick."""
    live = [_run(i, minutes_ago=100 - i, branch=f"pr-{i}") for i in range(1, 6)]
    cancelled = _run(
        9, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5
    )
    api = FakeApi(
        {"in_progress": live, "cancelled": [cancelled]},
        {
            **{i: [_job(i * 10, run_id=i)] for i in range(1, 6)},
            9: [_cancelled_orphan_job(91, run_id=9)],
        },
        newest_by_branch={"main": 9},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[9] == wd.OUTCOME_RECOVERED
    assert api.posts[0] == f"repos/{REPO}/actions/runs/9/rerun"  # recovery went first
    healed = [run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED]
    assert len(healed) == 4  # five slots, one spent on recovery
    assert outcomes[5] == wd.OUTCOME_NOT_ATTEMPTED  # the youngest live orphan waits a tick


def test_recovery_itself_is_bounded_by_the_per_tick_cap() -> None:
    cancelled = [
        _run(
            i,
            minutes_ago=100 - i,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=5,
            branch=f"pr-{i}",
        )
        for i in range(1, 8)
    ]
    api = FakeApi(
        {"cancelled": cancelled},
        {i: [_cancelled_orphan_job(i * 10, run_id=i)] for i in range(1, 8)},
        newest_by_branch={f"pr-{i}": i for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert sum(1 for o in outcomes.values() if o == wd.OUTCOME_RECOVERED) == 5
    assert sum(1 for o in outcomes.values() if o == wd.OUTCOME_NOT_ATTEMPTED) == 2


def test_recovery_covers_pull_request_runs_whose_heal_outlived_the_budget() -> None:
    """A PR heal whose cancel landed after the budget left the run cancelled: the next tick re-runs it."""
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
    )
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert any("event=pull_request" in p for p in api.gets)


def test_a_human_cancel_of_a_healthy_run_never_matches_the_recovery_fingerprint() -> None:
    """Jobs that ran on a runner, or were cancelled within seconds of queueing, are not orphans."""
    run = _run(1, minutes_ago=30, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    ran = _job(
        11,
        status="completed",
        conclusion="cancelled",
        minutes_ago=25,
        completed_minutes_ago=5,
        runner_name="r",
    )
    quick = _job(
        12, status="completed", conclusion="cancelled", minutes_ago=6, completed_minutes_ago=5
    )
    api = FakeApi({"cancelled": [run]}, {1: [ran, quick]}, newest_by_branch={"main": 1})
    verdicts, outcomes = _sweep(api)
    assert not any(v.run_id == 1 for v in verdicts)
    assert outcomes == {}
    assert api.posts == []


def test_recovery_reruns_a_cancelled_orphan_even_without_dispatch_evidence() -> None:
    """A cancelled orphan has no finished work left to protect: holding it under an
    outage would only let the recovery window expire and abandon it. Re-running it
    leaves it queued until the fleet returns, which is what the live pass wants."""
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    live = _run(2, minutes_ago=60, branch="other")
    api = FakeApi(
        {"cancelled": [run], "in_progress": [live]},
        {1: [_cancelled_orphan_job(11)], 2: [_job(21, run_id=2)]},
        newest_by_branch={"main": 1, "other": 2},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    # The live orphan is held (nothing to prove the fleet is alive) ...
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    # ... while the already-cancelled one is re-run regardless.
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_recovery_reruns_a_cancelled_orphan_even_when_codebuild_is_saturated() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    slow = _run(2, minutes_ago=40, branch="other")
    slow_job = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"cancelled": [run], "in_progress": [slow]},
        {1: [_cancelled_orphan_job(11)], 2: [slow_job]},
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_a_cancelled_orphan_the_listing_never_reaches_is_a_failed_outcome_not_healthy() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": []}
    )
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.LOOKUP_INCONCLUSIVE
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert api.posts == []
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert "[1](https://example.invalid/runs/1)" in summary and wd.OUTCOME_LOOKUP_FAILED in summary


def test_recovery_in_dry_run_reruns_nothing() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    _, outcomes = _sweep(api, dry_run=True)
    assert outcomes == {1: wd.OUTCOME_DRY_RUN}
    assert api.posts == []


# ── API text is untrusted: no line it carries may become a workflow command ──


FORGED_NAME = "build\n::warning::forged by a fork\r::add-mask::x\x1b[31m"


def test_control_characters_in_api_text_never_reach_a_log_line_unescaped() -> None:
    job = _job(11)
    job["name"] = FORGED_NAME
    job["labels"] = [job["labels"][0] + "\n::error::label"]
    run = _run(1, fork=True, branch="feature\u2028branch")
    api = FakeApi({"in_progress": [run]}, {1: [job]})
    lines: list[str] = []
    verdicts, _outcomes = wd.run_watchdog(
        api, _policy(dry_run=True), clock=_Clock().now, sleep=lambda _s: None, log=lines.append
    )
    assert lines, "the sweep logged nothing"
    for line in lines:
        assert "\n" not in line and "\r" not in line and "\x1b" not in line, repr(line)
        assert "\u2028" not in line, repr(line)
    assert not any(line.startswith("::") and "forged" in line for line in lines)
    assert any("::warning::forged" in line for line in lines), "the name was dropped, not escaped"
    orphan = _verdict_of(verdicts, 1).orphans[0]
    assert orphan.name == "build\\n::warning::forged by a fork\\r::add-mask::x\\x1b[31m"
    assert orphan.labels[0].startswith("codebuild-"), "escaping must not disturb the label test"


def test_the_step_summary_keeps_every_api_string_on_its_own_row() -> None:
    job = _job(11)
    job["name"] = FORGED_NAME
    api = FakeApi({"in_progress": [_run(1, branch="feat\nure")]}, {1: [job]})
    verdicts, outcomes = _sweep(api, dry_run=True)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    rows = [line for line in summary.splitlines() if line.startswith("| [1]")]
    assert len(rows) == 1, summary
    assert "::warning::forged" in rows[0] and "feat\\\\nure" in rows[0]


def test_the_step_summary_markdown_escapes_every_api_string() -> None:
    """A fork's branch name is chosen by whoever opened the fork; it must render as
    literal text, never as a link, a heading, or an extra table cell."""
    branch = "x`) | forged | [click](https://evil.invalid) # *bold* <b>"
    job = _job(11)
    job["name"] = "job | [link](https://evil.invalid) `tick`"
    fork = _run(2, fork=True, branch=branch)
    fork_job = _job(21, run_id=2)
    api = FakeApi({"in_progress": [_run(1, branch=branch), fork]}, {1: [job], 2: [fork_job]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "[click](https://evil.invalid)" not in summary
    assert "[link](https://evil.invalid)" not in summary
    assert "\\[click\\]\\(https://evil.invalid\\)" in summary
    assert "\\`tick\\`" in summary and "<b>" not in summary and "\\<b\\>" in summary
    rows = [line for line in summary.splitlines() if line.startswith("| [1]")]
    assert len(rows) == 1 and rows[0].count(" | ") == 5, rows  # still exactly six cells
    reported = [line for line in summary.splitlines() if line.startswith("- [2]")]
    assert len(reported) == 1 and "\\| forged \\|" in reported[0], reported


def test_the_step_summary_link_cannot_be_closed_by_the_url() -> None:
    run = _run(1)
    run["html_url"] = "https://example.invalid/runs/1) [forged](https://evil.invalid"
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "[forged](https://evil.invalid" not in summary
    assert "[1](https://example.invalid/runs/1%29%20%5Bforged%5D%28https://evil.invalid)" in summary


def test_api_error_bodies_are_escaped_before_they_are_logged() -> None:
    exc = wd.ApiError(403, "denied\n::error::forged")
    assert "\n" not in str(exc)
    assert str(exc) == "HTTP 403: denied\\n::error::forged"


# ── the summary ─────────────────────────────────────────────────────────────


def test_the_summary_names_every_acted_and_reported_run() -> None:
    api = FakeApi(
        {
            "in_progress": [_run(1), _run(2, fork=True)],
            "pending": [_run(3, status="pending", branch="other")],
        },
        {1: [_job(11, large=True)], 2: [_job(21, run_id=2)], 3: []},
    )
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert "## CI runner watchdog" in summary
    assert "| [1](https://example.invalid/runs/1) | 1 | main |" in summary
    assert "job-11 (60 min)" in summary
    assert wd.OUTCOME_HEALED in summary
    assert "[2](https://example.invalid/runs/2)" in summary and wd.SKIPPED_FORK in summary
    assert "[3](https://example.invalid/runs/3)" in summary and wd.WAITING_ON_GROUP in summary
    assert "re-run mode" not in summary


def test_the_summary_reports_a_saturated_hold() -> None:
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {
            1: [_job(11)],
            2: [
                _job(
                    21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r"
                )
            ],
        },
    )
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.SKIPPED_SATURATED in summary and "dispatching slowly" in summary


def test_a_quiet_sweep_says_so() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="completed")]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "Nothing stuck." in summary
    assert "dry run" in summary


# ── the CLI surface the workflow drives ─────────────────────────────────────


def test_the_parser_reads_the_workflow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("DRY_RUN", "true")
    args = wd.build_parser().parse_args([])
    assert args.repo == REPO
    assert args.dry_run is True
    monkeypatch.setenv("DRY_RUN", "false")
    assert wd.build_parser().parse_args([]).dry_run is False
    # Thresholds are Policy defaults, not flags: the workflow varies only DRY_RUN.
    with pytest.raises(SystemExit):
        wd.build_parser().parse_args(["--orphan-after", "20"])
    policy = wd.Policy(repo=REPO, now=NOW)
    assert (policy.orphan_after, policy.max_runs, policy.heal_budget) == (
        timedelta(minutes=15),
        5,
        timedelta(seconds=300),
    )


def test_main_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert wd.main(["--repo", REPO]) == 2
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert wd.main([]) == 2


def test_the_http_client_retries_once_on_a_server_error() -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def opener(request: Any, timeout: float) -> Any:
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 502, "bad gateway", email.message.Message(), io.BytesIO(b"x")
            )
        return _Response(b'{"ok": true}')

    slept: list[float] = []
    client = wd.GitHubApi("token", "https://api.example.invalid", sleep=slept.append, opener=opener)
    assert client.get("repos/x/y") == {"ok": True}
    assert len(calls) == 2 and slept == [2]


def test_the_http_client_retries_a_timeout_while_reading_the_body_and_then_gives_an_api_error() -> (
    None
):
    """The connect succeeded and the READ timed out: not a URLError, but the same transient
    class -- retried once, and surfaced as ApiError when it repeats, never as a crash."""
    calls: list[str] = []

    class _Response:
        def __init__(self, body: bytes | None) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            if self._body is None:
                raise TimeoutError("timed out")
            return self._body

    def flaky(request: Any, timeout: float) -> Any:
        calls.append(request.full_url)
        return _Response(None if len(calls) == 1 else b'{"ok": true}')

    slept: list[float] = []
    client = wd.GitHubApi("token", "https://api.example.invalid", sleep=slept.append, opener=flaky)
    assert client.get("repos/x/y") == {"ok": True}
    assert len(calls) == 2 and slept == [2]

    def always(request: Any, timeout: float) -> Any:
        return _Response(None)

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=always
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.get("repos/x/y")
    assert excinfo.value.status == 0 and "TimeoutError" in str(excinfo.value)

    def truncated(request: Any, timeout: float) -> Any:
        return _Response(b'{"ok": tr')

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=truncated
    )
    with pytest.raises(wd.ApiError):
        client.get("repos/x/y")


def test_the_http_client_treats_a_partial_read_and_an_unreadable_error_body_as_transient() -> None:
    """``IncompleteRead`` is an HTTPException, not an OSError; and the body of an HTTP error
    is itself read over the network. Neither may escape the client as a crash."""

    class _Partial:
        def __enter__(self) -> "_Partial":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"{")

    class _Ok:
        def __enter__(self) -> "_Ok":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    calls = {"n": 0}

    def partial_then_ok(request: Any, timeout: float) -> Any:
        calls["n"] += 1
        return _Partial() if calls["n"] == 1 else _Ok()

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=partial_then_ok
    )
    assert client.get("repos/x/y") == {"ok": True}

    class _UnreadableBody(io.BytesIO):
        def read(self, *_a: Any) -> bytes:
            raise TimeoutError("timed out reading the error body")

    def forbidden_with_unreadable_body(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", email.message.Message(), _UnreadableBody(b"x")
        )

    client = wd.GitHubApi(
        "token",
        "https://api.example.invalid",
        sleep=lambda _s: None,
        opener=forbidden_with_unreadable_body,
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/rerun")
    assert excinfo.value.status == 403 and "unreadable" in str(excinfo.value)


def test_the_http_client_never_retries_a_mutation_whose_result_is_unknown() -> None:
    """A GET is retried on a 5xx, a lost response or a URLError; a POST is never retried --
    the request may have been on the wire, so its effect is unknown and a repeat could only
    turn a landed mutation into a conflict. The error is marked ambiguous for the caller."""

    class _Lost:
        def __enter__(self) -> "_Lost":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            raise TimeoutError("timed out")

    calls: list[str] = []

    def server_error(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        raise urllib.error.HTTPError(
            request.full_url, 502, "bad gateway", email.message.Message(), io.BytesIO(b"x")
        )

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=server_error
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.status == 502 and excinfo.value.ambiguous
    assert calls == ["POST"]  # not retried

    calls.clear()

    def lost_response(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        return _Lost()

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=lost_response
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.ambiguous and calls == ["POST"]

    calls.clear()

    def url_error_then_ok(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        if len(calls) == 1:
            raise urllib.error.URLError("timed out")

        class _Ok:
            def __enter__(self) -> "_Ok":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        return _Ok()

    # A URLError can be a send/response timeout with the request already delivered,
    # so a mutation is ambiguous here too ...
    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=url_error_then_ok
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.ambiguous and calls == ["POST"]

    calls.clear()
    # ... while a read is simply retried.
    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=url_error_then_ok
    )
    assert client.get("repos/x/y") == {"ok": True}
    assert calls == ["GET", "GET"]


def test_the_http_client_surfaces_a_client_error_with_its_status() -> None:
    def opener(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", email.message.Message(), io.BytesIO(b"nope")
        )

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=opener
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/rerun")
    assert excinfo.value.status == 403
