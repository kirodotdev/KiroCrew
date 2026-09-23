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

import base64
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
import test_ci_fleet_routing_expression_parity as parity

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
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def _wf_of(run: dict[str, Any]) -> str | None:
    """The workflow file a fake run belongs to, from its ``path``."""
    name = str(run.get("path") or "").rsplit("/", 1)[-1]
    return name or None


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
    workflow: str = "ci.yml",
    head_sha: str | None = None,
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
        "head_sha": head_sha or f"sha-{run_id}",
        "path": f".github/workflows/{workflow}",
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
        workflow_contents: dict[tuple[str, str], Any] | None = None,
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
        self._workflow_contents = workflow_contents or {}
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
        if "/contents/.github/workflows/" in base:
            workflow = urllib.parse.unquote(base.split("/contents/.github/workflows/", 1)[1])
            key = (params.get("ref", ""), workflow)
            value = self._workflow_contents.get(key)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                value = (WORKFLOWS_DIR / workflow).read_text(encoding="utf-8")
            if isinstance(value, dict):
                return value
            return {
                "encoding": "base64",
                "content": base64.b64encode(str(value).encode("utf-8")).decode("ascii"),
            }
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
            workflow = base.split("/workflows/", 1)[1].split("/")[0]
            runs = [r for r in self._runs_by_status.get(status, []) if _wf_of(r) == workflow]
            start = (page - 1) * per_page
            return {"workflow_runs": runs[start : start + per_page]}
        if base.endswith("/actions/runs"):
            # Repo-wide listing: `GET /repos/{repo}/actions/runs?status=…` returns
            # runs of EVERY workflow for that status, newest first, paginated. The
            # script filters to the watched set client-side off each run's `path`.
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
    assert outcomes == {1: wd.OUTCOME_EVIDENCE_REREAD_DEFERRED}


def test_a_rate_limit_on_the_fresh_evidence_read_has_an_accurate_outcome() -> None:
    """A rate limit is a CONDITION, not a one-off, so the deferral it causes reds the tick.

    The 502 case above stays green and deferrable: nothing was touched and the next
    tick re-reads. A rate limit is what killed the tick in the incident, and while it
    persists every tick would defer and every tick would look healthy, with the orphan
    still parking every later push behind it.
    """
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_get = api.get
    reads = {"in_progress": 0}

    def get(path: str) -> Any:
        if re.search(r"/actions/runs\?.*status=in_progress", path):
            reads["in_progress"] += 1
            if reads["in_progress"] > 1:
                raise wd.ApiError(
                    403,
                    "API rate limit exceeded for installation ID 12345",
                    remaining="0",
                )
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {1: wd.OUTCOME_ABORTED_RATE_LIMITED}
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in wd.FAILED_OUTCOMES
    # The plain deferral stays green: that is the 502 path, pinned above.
    assert wd.OUTCOME_EVIDENCE_REREAD_DEFERRED not in wd.FAILED_OUTCOMES
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in summary
    assert wd.OUTCOME_NOT_ATTEMPTED not in summary


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


def test_stale_completed_runs_do_not_hide_recent_fleet_evidence() -> None:
    stale = [
        _run(
            100 + i,
            minutes_ago=50 + i,
            status="completed",
            conclusion="success",
            updated_minutes_ago=40 + i,
            branch=f"stale-{i}",
            workflow="ci.yml",
        )
        for i in range(wd.COMPLETED_SAMPLE)
    ]
    recent = _run(
        200,
        minutes_ago=12,
        status="completed",
        conclusion="success",
        updated_minutes_ago=3,
        branch="recent",
        workflow="fast-gate.yml",
    )
    api = FakeApi(
        {"in_progress": [_run(1, workflow="build.yml")], "completed": stale + [recent]},
        {
            1: [_job(11)],
            **{
                int(run["id"]): [_job(1000 + i, run_id=int(run["id"]))]
                for i, run in enumerate(stale)
            },
            200: [
                _job(
                    2001,
                    status="completed",
                    conclusion="success",
                    minutes_ago=11,
                    started_minutes_ago=10.7,
                    runner_name="recent-runner",
                    run_id=200,
                )
            ],
        },
        evidence=False,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    completed_listings = [path for path in api.gets if "status=completed" in path]
    assert "/workflows/ci.yml/runs?" in completed_listings[0]
    assert any("/workflows/fast-gate.yml/runs?" in path for path in completed_listings)
    assert not any(f"/runs/{run['id']}/jobs" in path for run in stale for path in api.gets)


def test_completed_run_reads_stay_within_the_sample_bound() -> None:
    completed = [
        _run(
            300 + i,
            minutes_ago=10 - i / 10,
            status="completed",
            conclusion="success",
            updated_minutes_ago=2,
            branch=f"recent-{i}",
        )
        for i in range(wd.COMPLETED_SAMPLE + 5)
    ]
    api = FakeApi(
        {"completed": completed},
        {
            int(run["id"]): [_job(3000 + i, run_id=int(run["id"]))]
            for i, run in enumerate(completed)
        },
        evidence=False,
    )
    evidence = wd.DispatchEvidence()
    wd.sample_completed_runs(api, _policy(), evidence)
    completed_ids = {int(run["id"]) for run in completed}
    reads = [
        path
        for path in api.gets
        if "/jobs" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in completed_ids
    ]
    assert len(reads) == wd.COMPLETED_SAMPLE


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


def test_the_candidate_listing_is_repo_wide_and_paginated() -> None:
    """One paginated repo-wide listing per status covers every workflow; no newest-N
    truncation, because an orphan is an OLD run at the tail of the newest-first pages.
    Negative control: reverted per-workflow code issues `/workflows/<wf>/runs` calls
    and never the repo-wide `/actions/runs?status=` call this asserts."""
    runs = [_run(i, minutes_ago=200 - i) for i in range(1, 131)]
    api = FakeApi({"in_progress": list(reversed(runs))}, {})
    listed = wd.list_all_candidate_runs(api, REPO)
    # Every watched run is examined -- all 130, not the newest 50 -- so the oldest
    # (the orphans) are never truncated away.
    assert sorted(int(r["id"]) for r in listed) == list(range(1, 131))
    in_progress_pages = [
        p
        for p in api.gets
        if p.startswith(f"repos/{REPO}/actions/runs?") and "status=in_progress" in p
    ]
    assert len(in_progress_pages) == 2  # 100 then 30 (< PAGE_SIZE) stops paging
    assert "per_page=100" in in_progress_pages[0] and "page=1" in in_progress_pages[0]
    # The second page keeps the page size: `page` is an offset in units of
    # `per_page`, so a 20-run second page would re-read runs 21-40 instead.
    assert "per_page=100" in in_progress_pages[1] and "page=2" in in_progress_pages[1]
    # Every listing is the repo-wide endpoint, never a per-workflow one.
    assert not any("/actions/workflows/" in p and "/runs?status=" in p for p in api.gets)


def test_the_repo_wide_paging_respects_its_page_cap() -> None:
    """A listing that always returns a full page would page forever; the cap stops it.
    Negative control: reverted code has no `_iter_repo_runs` and no repo-wide page cap,
    so this exercises a path that does not exist there."""
    api = FakeApi({}, {})
    full_page = [{"id": i, "path": ".github/workflows/ci.yml"} for i in range(1, wd.PAGE_SIZE + 1)]
    api.get = lambda _path: {"workflow_runs": full_page}  # type: ignore[method-assign]
    collected = list(
        wd._iter_repo_runs(api, REPO, status="in_progress", max_pages=wd.REPO_LISTING_MAX_PAGES)
    )
    assert len(collected) == wd.PAGE_SIZE * wd.REPO_LISTING_MAX_PAGES


def test_cancelled_recovery_reaches_past_the_shared_listing_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recently cancelled, old-created run sits on page nine.

    The negative control gives cancelled recovery the shared live-listing cap;
    that run is absent and cannot recover. The recovery-specific cap reaches it.
    """

    def api_with_deep_orphan() -> FakeApi:
        newer_created = [
            _run(
                1000 + i,
                minutes_ago=1 + i,
                status="completed",
                conclusion="cancelled",
                updated_minutes_ago=200,
                branch=f"filler-{i}",
            )
            for i in range(wd.PAGE_SIZE * wd.REPO_LISTING_MAX_PAGES)
        ]
        orphan = _run(
            1,
            minutes_ago=2_000,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=5,
            branch="deep-orphan",
        )
        return FakeApi(
            {"cancelled": newer_created + [orphan]},
            {1: [_cancelled_orphan_job(11, run_id=1)]},
            newest_by_branch={"deep-orphan": 1},
            evidence=False,
        )

    recovery_cap = wd.RECOVERY_LISTING_MAX_PAGES
    assert recovery_cap > wd.REPO_LISTING_MAX_PAGES
    monkeypatch.setattr(wd, "RECOVERY_LISTING_MAX_PAGES", wd.REPO_LISTING_MAX_PAGES)
    control = api_with_deep_orphan()
    _, control_outcomes = _sweep(control)
    assert 1 not in control_outcomes
    assert not _posts(control, "/rerun")
    monkeypatch.setattr(wd, "RECOVERY_LISTING_MAX_PAGES", recovery_cap)
    api = api_with_deep_orphan()
    _, outcomes = _sweep(api)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("status=cancelled" in path and "page=9" in path for path in api.gets)


def test_repo_listing_warns_only_when_the_page_cap_truncates() -> None:
    full_page = [{"id": i, "path": ".github/workflows/ci.yml"} for i in range(wd.PAGE_SIZE)]
    saturated = FakeApi({}, {})
    saturated.get = lambda _path: {"workflow_runs": full_page}  # type: ignore[method-assign]
    saturated_log: list[str] = []
    saturated_runs = list(
        wd._iter_repo_runs(
            saturated,
            REPO,
            status="in_progress",
            max_pages=2,
            log=saturated_log.append,
        )
    )
    assert len(saturated_runs) == 2 * wd.PAGE_SIZE
    assert any("truncated" in line and "in_progress" in line for line in saturated_log)

    short = FakeApi({"in_progress": [_run(1)]}, {})
    short_log: list[str] = []
    short_runs = list(
        wd._iter_repo_runs(
            short,
            REPO,
            status="in_progress",
            max_pages=2,
            log=short_log.append,
        )
    )
    assert [run["id"] for run in short_runs] == [1]
    assert not short_log


def test_a_pathless_run_is_not_classified_or_changed() -> None:
    run = _run(1)
    run.pop("path")
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert all(verdict.run_id != 1 for verdict in verdicts)
    assert 1 not in outcomes
    assert not any("/runs/1/jobs" in path for path in api.gets)
    assert not any("/runs/1/" in path for path in api.posts)


def test_the_completed_sample_listing_stays_workflow_scoped_and_paginated() -> None:
    """The completed-run evidence sample keeps its per-workflow, globally-capped read."""
    runs = [_run(i, minutes_ago=200 - i, status="completed") for i in range(1, 131)]
    api = FakeApi({"completed": list(reversed(runs))}, {})
    listed = wd.list_runs(api, REPO, "ci.yml", status="completed", cap=120)
    assert len(listed) == 120
    assert sorted(int(r["id"]) for r in listed) == list(range(11, 131))  # the 120 newest
    pages = [p for p in api.gets if "/actions/workflows/ci.yml/runs?" in p]
    assert len(pages) == 2
    assert "per_page=100" in pages[0] and "page=1" in pages[0]
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


def test_a_per_sha_audit_run_is_heal_exempt_and_never_cancelled() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml")]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []


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


def _saturation_band_api() -> FakeApi:
    """An orphan, a MIDDLE run holding the only slow start, and a young prompt start.

    Under a narrow bound the middle run is the band that gets dropped, so the sweep
    sees prompt starts and nothing slow. Under a wide bound it reads the slow start
    and holds as saturated, which is what proves the fixture really does contain
    saturation and the narrow-bound hold is not an artefact of an empty fixture.
    """
    middle = _run(2, minutes_ago=20, branch="middle")
    young = _run(3, minutes_ago=9, branch="young")
    slow = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    prompt = _job(
        31, status="in_progress", minutes_ago=3, started_minutes_ago=2.8, runner_name="r", run_id=3
    )
    return FakeApi(
        {"in_progress": [_run(1), middle, young]},
        {1: [_job(11)], 2: [slow], 3: [prompt]},
    )


def test_a_successor_we_cancelled_and_will_not_restore_is_lost_not_a_clean_handoff() -> None:
    """Declining at depth 0 is healthy; declining AFTER our own re-run cancelled it is not.

    Run 1 is heal-safe and healed, its re-run's concurrency group cancels the successor
    run 2 that landed in the settle window, and run 2's OWN revision then reads
    heal-unsafe, so the watchdog will not restore it. At depth 0 that verdict means
    "nothing was touched" and the tick stays green; here it means the watchdog
    destroyed a run and walked away, so it must carry the same red as a refused
    re-run or the run vanishes behind a green tick with no fingerprint for recovery.
    """
    successor = _run(
        2,
        minutes_ago=1,
        status="completed",
        conclusion="cancelled",
        branch="pr",
        head_sha="successor-publishes",
    )
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
        workflow_contents={
            ("successor-publishes", "ci.yml"): (
                "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n"
                "    runs-on: ubuntu-latest\n    steps:\n      - run: npm publish\n"
            )
        },
    )
    api.run_overrides[2] = successor
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert wd.OUTCOME_SUCCESSOR_LOST in wd.FAILED_OUTCOMES
    assert not any(path.endswith("/runs/2/rerun") for path in api.posts)
    assert any("::error::" in line and "gh run rerun 2" in line for line in logged)


def test_partial_dispatch_evidence_holds_instead_of_authorizing_a_heal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep that read only part of its listing must not rule saturation out.

    The bound drops the MIDDLE band, so the slow CodeBuild start there is never read
    and "prompt starts, nothing slow" is not established. The paired wide bound reads
    it and holds as saturated, proving the saturation is really in the fixture.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    narrow = _saturation_band_api()
    verdicts, outcomes = _sweep(narrow)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    assert narrow.posts == []

    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 50)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 10)
    wide = _saturation_band_api()
    wide_verdicts, _ = _sweep(wide)
    assert _verdict_of(wide_verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert wide.posts == []
    assert outcomes is not None


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


def test_recovery_read_budget_serves_the_run_nearest_updated_at_expiry_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed_old_run = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
    )
    expiring_newer_run = _run(
        2,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=80,
    )
    api = FakeApi(
        {"cancelled": [expiring_newer_run, refreshed_old_run]},
        {1: [], 2: []},
        evidence=False,
    )
    monkeypatch.setattr(wd, "RECOVERY_CLASSIFY_READS", 1)
    clock = _Clock()
    wd.recover_cancelled_runs(api, _policy(), budget=0, tick=_tick(clock), log=lambda _l: None)
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert job_reads == [f"repos/{REPO}/actions/runs/2/jobs?per_page=100&page=1&filter=latest"]


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


def test_a_runs_own_revision_that_adds_publish_is_exempt() -> None:
    run = _run(1, head_sha="publish-sha")
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: npm publish\n"
    )
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("publish-sha", "ci.yml"): own_revision},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []


def test_a_runs_own_clean_revision_is_healed_and_read_once() -> None:
    run = _run(1, head_sha="clean-sha")
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("clean-sha", "ci.yml"): own_revision},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/ci.yml?" in path]
    assert len(revision_reads) == 1
    assert api.posts[:2] == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


@pytest.mark.parametrize(
    "workflow_content",
    [
        wd.ApiError(500, "contents unavailable"),
        {"encoding": "base64", "content": "%%%not-base64%%%"},
    ],
)
def test_an_unreadable_run_revision_is_a_failed_outcome_not_a_silent_exemption(
    workflow_content: Any,
) -> None:
    run = _run(1, head_sha="unreadable-sha")
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("unreadable-sha", "ci.yml"): workflow_content},
    )
    verdicts, outcomes = _sweep(api)
    # An unreadable revision is UNKNOWN, not unsafe: nothing is cancelled, and
    # the outcome is a failure so a run whose safety nobody could establish
    # cannot age out of its recovery window behind a green tick.
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEAL_SAFETY_UNKNOWN}
    assert wd.OUTCOME_HEAL_SAFETY_UNKNOWN in wd.FAILED_OUTCOMES
    assert api.posts == []


def test_a_run_without_a_head_sha_cannot_be_judged_and_is_a_failed_outcome() -> None:
    run = _run(1)
    run.pop("head_sha")
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEAL_SAFETY_UNKNOWN}
    assert api.posts == []


def test_a_publish_or_deploy_orphan_is_observed_but_requires_a_human() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, workflow="pages.yml")]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.HEAL_EXEMPT
    assert "human" in verdict.detail
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.OUTCOME_HUMAN_REQUIRED in summary


def test_recovery_uses_the_cancelled_runs_own_revision() -> None:
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        head_sha="publish-sha",
    )
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: npm publish\n"
    )
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"main": 1},
        workflow_contents={("publish-sha", "ci.yml"): own_revision},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert _posts(api, "/rerun") == []
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/ci.yml?" in path]
    assert len(revision_reads) == 1


def test_recovery_never_reruns_a_publish_or_deploy_workflow() -> None:
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        workflow="release.yml",
    )
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []
    assert any("branch=main" in path for path in api.gets)


def test_recovery_reaches_an_orphan_behind_newer_cancelled_runs() -> None:
    orphan = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="orphan",
    )
    superseded = [
        _run(
            i,
            minutes_ago=70 - i,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=4,
            branch=f"superseded-{i}",
        )
        for i in range(2, 53)
    ]
    api = FakeApi(
        {"cancelled": list(reversed(superseded)) + [orphan]},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            **{
                run["id"]: [_cancelled_orphan_job(run["id"] * 10, run_id=run["id"])]
                for run in superseded
            },
        },
        newest_by_branch={
            "orphan": 1,
            **{run["head_branch"]: 100 + run["id"] for run in superseded},
        },
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert len(_posts(api, "/rerun")) == 1


def test_recovery_classifies_the_oldest_in_window_runs_within_its_read_cap() -> None:
    """The read cap bounds job reads per tick AND spends them oldest-first.

    Every `main` push cancels the run it supersedes, so a recovery window holds far
    more cancelled runs than a tick should read. The orphan here is the OLDEST
    in-window run, sitting behind more newer ones than the cap allows, so a cap
    applied to a newest-first walk would never reach it.
    """
    orphan = _run(
        1,
        minutes_ago=88,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=70,
        branch="orphan",
    )
    # A fixed count, independent of the cap, so the cap is the only thing the
    # read assertion below measures.
    newer = [
        _run(
            i,
            minutes_ago=80 - i / 10,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=10,
            branch=f"newer-{i}",
        )
        for i in range(2, 72)
    ]
    api = FakeApi(
        {"cancelled": list(reversed(newer)) + [orphan]},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            **{
                run["id"]: [_cancelled_orphan_job(run["id"] * 10, run_id=run["id"])]
                for run in newer
            },
        },
        newest_by_branch={
            "orphan": 1,
            **{run["head_branch"]: 100 + run["id"] for run in newer},
        },
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    cancelled_ids = {1} | {int(run["id"]) for run in newer}
    job_reads = [
        path
        for path in api.gets
        if "/jobs" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in cancelled_ids
    ]
    assert len(job_reads) <= wd.RECOVERY_CLASSIFY_READS
    # Independent of the cap's value: a tick must not read every in-window run.
    assert len(job_reads) < 1 + len(newer)


def test_a_cancelled_run_too_short_lived_to_hold_an_orphan_is_excluded_for_free() -> None:
    """Pins the recovery pass's zero-read exclusion to the CLASSIFIER, not to a number.

    The exclusion skips a cancelled run whose lifetime is under ``orphan_after``
    without spending a job read. That is only sound if such a run can never be
    classified as an orphan, so the classifier is driven here with the most
    favourable job the lifetime permits -- created with the run, still queueing
    when the run was cancelled -- and must find nothing. The paired case lifts
    the lifetime to the threshold and the same job does fingerprint, proving the
    exclusion tracks the classifier rather than a hardcoded bound.
    """
    policy = _policy()

    def verdict_for(lifetime_minutes: float) -> Any:
        created, cancelled = 80.0, 80.0 - lifetime_minutes
        run = _run(
            1,
            minutes_ago=created,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=cancelled,
        )
        job = _job(
            11,
            status="completed",
            conclusion="cancelled",
            minutes_ago=created,
            completed_minutes_ago=cancelled,
        )
        return wd.classify_cancelled_run(run, [job], policy, newest_check=lambda: True)

    threshold = policy.orphan_after.total_seconds() / 60
    assert verdict_for(threshold - 1).orphans == []
    assert verdict_for(threshold - 1).verdict != wd.CANCELLED_ORPHAN
    assert verdict_for(threshold).verdict == wd.CANCELLED_ORPHAN


def test_the_recovery_pass_spends_no_read_on_a_run_too_short_lived_to_hold_an_orphan() -> None:
    """What keeps the classify bound off runs that were never candidates.

    Every `main` push cancels the run it supersedes, and those live seconds. Were
    each one read, the bound would be spent on them and a real candidate behind
    them could age out of the window unread.
    """
    supersession = [
        _run(
            i,
            minutes_ago=30,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=30 - 0.1,
            branch=f"superseded-{i}",
        )
        for i in range(2, 9)
    ]
    candidate = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=20,
        branch="orphan",
    )
    api = FakeApi(
        {"cancelled": supersession + [candidate]},
        {1: [_cancelled_orphan_job(11, run_id=1)], **{run["id"]: [] for run in supersession}},
        newest_by_branch={"orphan": 1},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert job_reads == [f"repos/{REPO}/actions/runs/1/jobs?per_page=100&page=1&filter=latest"]


def test_a_run_too_short_lived_to_hold_an_orphan_does_not_red_the_tick_from_the_tail() -> None:
    """The near-expiry flag asks a human to look. It must not ask about a non-candidate."""
    tail = [
        _run(
            i,
            minutes_ago=89,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=89 - 0.1,
            branch=f"superseded-{i}",
        )
        for i in range(2, 6)
    ]
    verdicts: list[Any] = []
    outcomes: dict[int, str] = {}
    wd._flag_unclassified_near_expiry(tail, _policy(), verdicts, outcomes, lambda _l: None)
    assert verdicts == []
    assert outcomes == {}


def test_a_stuck_run_in_a_heal_exempt_workflow_reds_the_tick() -> None:
    """Most of the watched set is heal-exempt, `main-ratchet-audit.yml` among them, and it
    was in the incident this script exists for. A warning inside a passing scheduled run
    nobody watches would leave that incident's shape intact."""
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml")]},
        {1: [_job(11)]},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert wd.OUTCOME_HUMAN_REQUIRED in wd.FAILED_OUTCOMES


def test_the_completed_sample_order_covers_exactly_the_watched_set() -> None:
    """The sample order is a permutation of the watched set, so priority cannot
    introduce an unwatched workflow or drop a watched one from sampling."""
    assert sorted(wd.COMPLETED_SAMPLE_WORKFLOWS) == sorted(wd.WATCHED_WORKFLOWS)
    assert len(set(wd.COMPLETED_SAMPLE_WORKFLOWS)) == len(wd.COMPLETED_SAMPLE_WORKFLOWS)


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


def test_a_pull_request_run_is_reported_and_never_healed() -> None:
    """Replaces an earlier contract that recovered PR runs too. That was wrong.

    The successor check asks whether a NEWER run of this branch is in flight, and
    GitHub's runs listing can only be filtered by branch NAME, so two pull requests
    open on one head branch read as each other's successor — abandoning a cancelled
    orphan behind a green `skipped-superseded`. Matching by pull-request number is
    not available: of 20 sampled same-repository `pull_request` runs only 9 carried
    `pull_requests[].number`, so that route fails closed on most PR runs.

    So a pull-request run is reported and left alone, like a fork run, and green for
    the same reason: its owner is reading their own pull request's checks. `main`
    orphans, which nobody watches and which the incident was made of, still heal.
    """
    cancelled = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
    )
    api = FakeApi(
        {"cancelled": [cancelled]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_PULL_REQUEST
    assert outcomes == {}
    assert api.posts == []
    assert wd.render_summary(verdicts, outcomes, _policy()).count(wd.SKIPPED_PULL_REQUEST) == 1

    live = FakeApi({"in_progress": [_run(2, event="pull_request", branch="pr")]}, {2: [_job(21)]})
    live_verdicts, live_outcomes = _sweep(live)
    assert _verdict_of(live_verdicts, 2).verdict == wd.SKIPPED_PULL_REQUEST
    assert live_outcomes == {} and live.posts == []

    # The push path is untouched: that is where the incident's orphans were.
    push = FakeApi(
        {
            "cancelled": [
                _run(
                    3,
                    minutes_ago=70,
                    status="completed",
                    conclusion="cancelled",
                    updated_minutes_ago=5,
                    branch="pushed",
                )
            ]
        },
        {3: [_cancelled_orphan_job(31, run_id=3)]},
        newest_by_branch={"pushed": 3},
    )
    _, push_outcomes = _sweep(push)
    assert push_outcomes == {3: wd.OUTCOME_RECOVERED}


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


def test_the_summary_reports_a_partial_evidence_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hold the watchdog deliberately took must not read as "Nothing stuck."."""
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    verdicts, outcomes = _sweep(_saturation_band_api())
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.SKIPPED_PARTIAL_EVIDENCE in summary
    assert "Nothing stuck." not in summary


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


def test_a_cheap_rate_limit_reset_in_recovery_is_waited_out_not_aborted() -> None:
    """The re-raise must not cost a verdict a five-second wait would have saved.

    Making a rate-limited recovery read propagate stopped it exiting green, but it
    also sent a CHEAP reset -- one the gather phase simply waits out -- straight to
    the abort handler. For a cancelled run with less window left than one schedule
    interval there is no next tick, so a five-second wait was costing the verdict.
    Recovery's reads now go through the same wait-and-retry.
    """
    candidate = _run(
        1,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=85,
        branch="orphan",
    )
    api = FakeApi(
        {"cancelled": [candidate]},
        {1: [_cancelled_orphan_job(11, run_id=1)]},
        newest_by_branch={"orphan": 1},
    )
    _rate_limit_get(api, r"/actions/runs/1/jobs", times=1, retry_after=5.0)
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes.get(1) == wd.OUTCOME_RECOVERED
    assert wd.RATE_LIMIT_MARKER_ID not in outcomes
    assert clock.t >= 5.0


def test_a_recovery_mutation_is_never_retried_through_the_read_wrapper() -> None:
    """Reads get the retry; a cancel or re-run that may be on the wire never does."""
    posted: list[str] = []

    class _Recording:
        def get(self, path: str) -> Any:
            raise AssertionError("the wrapper must route reads through the given reader")

        def post(self, path: str) -> None:
            posted.append(path)

    reads: list[str] = []
    wrapper = wd._ReadsThrough(_Recording(), lambda path: reads.append(path) or {"ok": True})
    assert wrapper.get("repos/x/y") == {"ok": True}
    assert reads == ["repos/x/y"]
    wrapper.post("repos/x/y/cancel")
    assert posted == ["repos/x/y/cancel"]


def test_the_http_client_counts_its_calls_and_remembers_the_quota_it_was_told() -> None:
    """The watchdog's own footprint against the quota whose exhaustion started this.

    Logged, never enforced: a self-imposed call cap would silently stop healing,
    which is the failure mode this script exists to end. A response without headers
    is tolerated -- the reading is observability and must not crash a good read.
    """

    class _Response:
        def __init__(self, remaining: str | None) -> None:
            self.headers = email.message.Message()
            if remaining is not None:
                self.headers["X-RateLimit-Remaining"] = remaining

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    class _Headerless(_Response):
        def __init__(self) -> None:
            super().__init__(None)
            del self.headers

    seen: list[Any] = [_Response("4321"), _Response("4320"), _Headerless()]

    def opener(request: Any, timeout: float) -> Any:
        return seen.pop(0)

    client = wd.GitHubApi("token", "https://api.example.invalid", opener=opener)
    assert client.calls == 0 and client.rate_limit_remaining is None
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (1, "4321")
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (2, "4320")
    # A headerless response still counts, and does not erase the last reading.
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (3, "4320")


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


# ── the watched set covers every fleet-routed workflow, and only those ──────


def _fleet_routed_workflow_files() -> set[str]:
    """The fleet-routed workflow files, projected from the parity suite's pinned inventory.

    Reuses ``test_ci_fleet_routing_expression_parity._EXPECTED_ROUTED_JOBS`` instead of
    parsing ``runs-on`` a second time. That suite already pins the routed (file, job)
    pairs against the real workflows and fails when one gains a route, so a second
    scanner here would be a second place to update and a second thing to get wrong;
    importing it is also how ``test_ci_additional_fleet_routes.py`` reuses that suite.
    The watchdog's own workflow is absent because its job runs on a hosted runner and
    the fleet label appears there only in a comment.
    """
    return {workflow for workflow, _ in parity._EXPECTED_ROUTED_JOBS}


def test_the_watched_set_is_exactly_the_fleet_routed_workflows() -> None:
    """Drift guard: a workflow that gains (or loses) a fleet route must be added to
    (or removed from) WATCHED_WORKFLOWS, or this fails."""
    assert set(wd.WATCHED_WORKFLOWS) == _fleet_routed_workflow_files()


def test_heal_safe_and_exempt_workflows_form_the_exact_partition() -> None:
    heal_safe = frozenset(
        {
            "build.yml",
            "ci.yml",
            "fast-gate.yml",
        }
    )
    exempt = frozenset(
        {
            "release.yml",
            "pages.yml",
            "main-ratchet-audit.yml",
            "build-wheel.yml",
            "dependency-vulnerability.yml",
            "pr-merge-conflict-label.yml",
            "code-review.yml",
            "cross-platform.yml",
            "dependency-review.yml",
            "pr-scope.yml",
            "screenshot-evidence.yml",
            # Ref-keyed and publishes nothing, but triggered ONLY by `pull_request`,
            # and pull-request runs are never healed -- so declaring it heal-safe
            # would read as coverage no run could ever use.
            "macos-on-demand.yml",
        }
    )
    assert wd.HEAL_SAFE_WORKFLOWS == heal_safe
    assert wd.heal_exempt_workflows() == exempt
    assert heal_safe | exempt == frozenset(wd.WATCHED_WORKFLOWS)
    assert not heal_safe & exempt


def test_every_declared_heal_safe_workflow_is_ref_keyed_not_pr_keyed() -> None:
    """The successor guard filters by head branch, never by pull-request number.

    Two pull requests can share a head branch, so for a PR-keyed concurrency group
    another PR's newer run reads as this run's successor and the cancelled verdict is
    left unrestored. Declaring a PR-keyed workflow heal-safe would therefore break the
    one protection healing has, and this fails if someone adds one back.
    """
    pr_keyed = []
    for name in sorted(wd.HEAL_SAFE_WORKFLOWS):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        if "pull_request.number" in text.split("jobs:", 1)[0]:
            pr_keyed.append(name)
    assert pr_keyed == [], f"PR-keyed workflows cannot be auto-healed: {pr_keyed}"
    # The exempt tier really does hold the PR-keyed ones, so the assertion above is
    # not passing merely because the repo has none.
    assert "code-review.yml" in wd.heal_exempt_workflows()


def test_every_declared_heal_safe_workflow_has_a_trigger_a_heal_can_reach() -> None:
    """A declaration that can never act reads as coverage that does not exist.

    Pull-request runs are never healed, so a workflow triggered ONLY by
    `pull_request` can sit in `HEAL_SAFE_WORKFLOWS` and never produce one healable
    run -- `macos-on-demand.yml` was exactly that until it was removed. This fails if
    such an entry is added back, or if a declared workflow's triggers narrow to
    pull-request only.
    """
    unreachable = []
    for name in sorted(wd.HEAL_SAFE_WORKFLOWS):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        triggers = {
            keys[1]
            for keys, _ in wd._yaml_mapping_entries(text)
            if len(keys) >= 2 and keys[0] == "on"
        }
        if triggers and triggers <= {"pull_request", "pull_request_target"}:
            unreachable.append(name)
    assert unreachable == [], (
        "these workflows are declared heal-safe but only pull-request runs can "
        f"trigger them, and those are never healed: {unreachable}"
    )
    # macos-on-demand.yml really is pull-request only, so the check above is not
    # vacuous -- it is the case that motivated it.
    macos = (WORKFLOWS_DIR / "macos-on-demand.yml").read_text(encoding="utf-8")
    macos_triggers = {
        keys[1] for keys, _ in wd._yaml_mapping_entries(macos) if len(keys) >= 2 and keys[0] == "on"
    }
    assert macos_triggers == {"pull_request"}
    assert "macos-on-demand.yml" in wd.heal_exempt_workflows()


def test_every_declared_heal_safe_workflow_passes_the_derived_gate_on_its_real_yaml() -> None:
    """The symmetric pin: the derived gate must ADMIT the four we declared safe.

    `workflow_is_heal_safe_at_revision` is fail-closed -- a heal only proceeds when the
    text gate returns True -- so the gate can silently DISABLE healing, not only enable
    it. A benign future edit to one of these files (an `aws s3 cp` artifact fetch, a
    concurrency group re-spelled in a way the hand-rolled parser misreads) would flip it
    to `human-required` and the first signal would arrive at the next incident. This
    fails at the edit instead.
    """
    refused = [
        name
        for name in sorted(wd.HEAL_SAFE_WORKFLOWS)
        if wd.workflow_text_is_heal_safe((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
        is not True
    ]
    assert refused == [], (
        "these workflows are declared heal-safe but the derived gate refuses their own "
        f"YAML, so healing is silently disabled for them: {refused}"
    )


def test_an_exempt_orphan_at_the_attempt_cap_still_asks_for_a_human() -> None:
    """The attempt cap must not turn a promised red into a green.

    That cap stops US looping re-runs, and an exempt workflow is never re-run by us, so
    it is not the operative reason nobody touched the run -- while it carries no outcome,
    which would leave the stuck orphan behind a green tick. A run with no orphan evidence
    is untouched by this: the plain attempt-cap report is still what it gets.
    """
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml", attempt=3)]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.HEAL_EXEMPT
    assert "attempt 3" in verdict.detail
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []

    healthy = FakeApi(
        {"in_progress": [_run(2, workflow="main-ratchet-audit.yml", attempt=3)]},
        {2: [_job(21, status="completed")]},
    )
    quiet_verdicts, quiet_outcomes = _sweep(healthy)
    assert _verdict_of(quiet_verdicts, 2).verdict != wd.HEAL_EXEMPT
    assert quiet_outcomes == {}


def test_a_rate_limited_recovery_keeps_the_heal_slots_it_already_spent() -> None:
    """Partial recovery results survive the abort, so the five-per-tick cap holds.

    The tuple return never lands when the pass raises, so a recovery that already
    re-ran a run was invisible to the caller's slot accounting and the tick healed
    five MORE live runs on top of it -- the global cap bypassed by an abort. The
    caller owns the containers now, so an abort leaves it holding what was done.
    """
    recovered_orphan = _run(
        1,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=30,
        branch="recovered",
    )
    limited = _run(
        2,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=31,
        branch="limited",
    )
    live = [_run(100 + i, branch=f"live-{i}") for i in range(5)]
    api = FakeApi(
        {"cancelled": [limited, recovered_orphan], "in_progress": live},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            2: [_cancelled_orphan_job(21, run_id=2)],
            **{run["id"]: [_job(run["id"] * 10, run_id=run["id"])] for run in live},
        },
        newest_by_branch={"recovered": 1, "limited": 2, **{f"live-{i}": 100 + i for i in range(5)}},
    )
    original_get = api.get

    def get(path: str) -> Any:
        # Run 2 is the OLDER cancellation, so it is classified and re-run first;
        # the limit then lands on run 1's job read, mid-pass.
        if "/runs/1/jobs" in path:
            raise wd.ApiError(403, "API rate limit exceeded", remaining="0")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes.get(2) == wd.OUTCOME_RECOVERED
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    reruns = _posts(api, "/rerun")
    assert len(reruns) <= 5, f"the five-per-tick cap was bypassed: {reruns}"


def test_a_newly_watched_workflow_is_heal_exempt_until_declared_safe() -> None:
    # Classification reads the declaration alone, so a workflow nobody has
    # classified is exempt whatever its YAML says.
    assert wd.heal_exempt_workflows(("newcomer.yml",), frozenset()) == frozenset({"newcomer.yml"})
    assert wd.heal_exempt_workflows(("newcomer.yml",), frozenset({"newcomer.yml"})) == frozenset()


@pytest.mark.parametrize(
    ("group", "safe"),
    [
        ("${{ github.workflow }}-${{ github.ref }}", True),
        ("${{ github.ref_name }}", True),
        ("${{ github.head_ref }}", True),
        # A PR-keyed group is NOT heal-safe, and that is the point: the successor
        # check filters the runs listing by branch NAME, so it cannot tell one pull
        # request's run from another's on a shared head branch. Admitting this here
        # would let the derived gate bless a workflow the declaration test forbids.
        ("code-review-${{ github.event.pull_request.number }}", False),
        ("a-constant-group", False),
        ("audit-${{ github.sha }}", False),
        ("run-${{ github.run_id }}", False),
    ],
)
def test_heal_safety_requires_a_ref_keyed_group(group: str, safe: bool) -> None:
    text = "on: push\nconcurrency:\n  group: " + group + "\n  cancel-in-progress: true\n"
    assert wd.workflow_text_is_heal_safe(text) is safe


def test_a_workflow_with_no_concurrency_group_is_heal_exempt() -> None:
    assert (
        wd.workflow_text_is_heal_safe("on: push\njobs:\n  one:\n    runs-on: ubuntu-latest\n")
        is False
    )


@pytest.mark.parametrize(
    "step",
    [
        "run: npm publish",
        "run: gh release create v1",
        "uses: actions/deploy-pages@abc",
        "uses: actions/upload-pages-artifact@abc",
        "uses: ./.github/workflows/publish-cli.yml",
        "run: docker push example/image:tag",
        "run: docker buildx build . --push",
        "run: aws s3 cp artifact s3://bucket/key",
        "run: aws codeartifact publish-package-version --domain d",
        "run: twine upload --repository-url https://d.codeartifact.example wheel",
        "run: twine upload dist/*",
        "uses: ./.github/workflows/sign-and-notarize.yml",
    ],
)
def test_a_publish_or_deploy_step_makes_a_workflow_unsafe(step: str) -> None:
    safe = "concurrency:\n  group: ${{ github.ref }}\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    assert wd.workflow_text_is_heal_safe(safe) is True
    assert wd.workflow_text_is_heal_safe(safe + f"    steps:\n      - {step}\n") is False


def test_a_job_environment_makes_a_workflow_unsafe() -> None:
    text = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  deploy:\n"
        "    runs-on: ubuntu-latest\n    environment: production\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is False


def test_no_publish_pattern_chases_a_toolchain_this_repo_does_not_use() -> None:
    """Keeps the backstop from drifting into a spelling chase.

    A pattern earns its place by naming a publish route this repository could really
    grow: it either already appears in `.github/workflows`, or it is one step from a
    route that does. `cargo publish` and `uv publish` were added because a review
    round named them, and this repo has neither Rust nor uv -- they are gone. The
    four below match nothing today and stay, each for a stated reason; anything else
    unmatched fails here, so the next addition has to justify itself.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    ).lower()
    allowed_unmatched = {
        # An npm/Electron app: `npx electron-builder` already runs here with
        # `--publish never`, so dropping that flag is the whole distance to a publish.
        r"\bnpm\s+publish\b": "npm/Electron project",
        # `docker push` matches and `publish-docker.yml` exists; buildx with --push
        # is the same route spelled in one command.
        r"\bdocker\s+buildx\s+build\b[^\n]*\s--push\b": "docker publishing exists",
        # The repo authenticates to CodeArtifact already; publishing to it is the
        # same endpoint in the other direction.
        r"\baws\s+codeartifact\s+publish-package-version\b": "CodeArtifact is wired up",
        # `build-wheel.yml` builds a Python wheel; twine is how a wheel leaves.
        r"\btwine\s+upload\b": "a wheel is built here",
    }
    unmatched = {p.pattern for p in wd._PUBLISH_OR_DEPLOY_PATTERNS if not p.search(text)}
    unjustified = unmatched - set(allowed_unmatched)
    assert unjustified == set(), (
        "these publish patterns match nothing in .github/workflows and name no route "
        f"this repo could grow, so they are a spelling chase: {sorted(unjustified)}"
    )
    # Every allowance is doing work rather than passing vacuously, and a pattern that
    # starts matching must lose its allowance so the reasons stay true.
    assert set(allowed_unmatched) == unmatched


@pytest.mark.parametrize("permission", ["pages", "packages", "deployments"])
def test_a_durable_write_permission_makes_a_workflow_unsafe(permission: str) -> None:
    text = (
        f"concurrency:\n  group: ${{{{ github.ref }}}}\npermissions:\n  contents: read\n"
        f"  {permission}: write\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is False


def test_id_token_write_alone_leaves_a_workflow_safe() -> None:
    text = (
        "concurrency:\n  group: ${{ github.ref }}\npermissions:\n  contents: read\n"
        "  id-token: write\njobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is True


def test_a_publish_marker_only_in_a_comment_leaves_a_workflow_safe() -> None:
    text = (
        "# run: npm publish\nconcurrency:\n  group: ${{ github.ref }}\n"
        "jobs:\n  safe:\n    runs-on: ubuntu-latest\n"
    )
    assert wd.workflow_text_is_heal_safe(text) is True


def test_the_watchdog_never_watches_its_own_workflow() -> None:
    assert wd.WATCHDOG_WORKFLOW == "ci-runner-watchdog.yml"
    assert wd.WATCHDOG_WORKFLOW not in wd.WATCHED_WORKFLOWS
    text = (WORKFLOWS_DIR / wd.WATCHDOG_WORKFLOW).read_text(encoding="utf-8")
    # A naive whole-file grep WOULD pull it in; the runs-on rule keeps it out.
    assert "codebuild-kirocrew-gha" in text
    assert wd.WATCHDOG_WORKFLOW not in _fleet_routed_workflow_files()


def test_the_drift_guard_flags_a_new_unregistered_fleet_route() -> None:
    """Negative control: a workflow that gains a fleet route without joining the watched
    set breaks the equality above.

    The route is added to the borrowed inventory rather than to a synthetic workflow
    directory, because the scan itself is the parity suite's job and is negative-controlled
    there; what this guards is the projection reaching WATCHED_WORKFLOWS.
    """
    routed = _fleet_routed_workflow_files() | {"surprise.yml"}
    assert set(wd.WATCHED_WORKFLOWS) != routed


# ── the watchdog heals orphans in every watched workflow, capped globally ───


def test_an_orphan_in_a_non_ci_watched_workflow_is_healed() -> None:
    """The 21-hour orphans sat in fast-gate.yml, not ci.yml. It is found through the
    repo-wide listing and filtered in by its `path`. Negative control: reverted
    pre-coverage code (WORKFLOW_FILE = "ci.yml") never lists fast-gate.yml, so run 1
    is not healed; and the repo-wide `/actions/runs?status=` call this asserts is
    absent on any per-workflow-loop revision."""
    api = FakeApi({"in_progress": [_run(1, workflow="fast-gate.yml")]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _verdict_of(verdicts, 1).workflow == "fast-gate.yml"
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert any(p.startswith(f"repos/{REPO}/actions/runs?status=") for p in api.gets)


def test_an_unwatched_workflows_run_in_the_repo_wide_listing_is_filtered_out() -> None:
    """The repo-wide listing returns runs of EVERY workflow, so an unwatched one
    (issue-triage.yml) with the exact orphan shape appears beside a watched orphan and
    must be filtered out client-side -- classified nowhere, healed never. Negative
    control: on a per-workflow-loop revision the unwatched run is never listed at all,
    so the repo-wide `/actions/runs?status=` call this asserts is absent and the
    filter it proves is untested."""
    api = FakeApi(
        {"in_progress": [_run(1, workflow="fast-gate.yml"), _run(2, workflow="issue-triage.yml")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    verdicts, outcomes = _sweep(api)
    # The unwatched run was returned by the repo-wide listing ...
    assert any(p.startswith(f"repos/{REPO}/actions/runs?status=") for p in api.gets)
    # ... but is filtered out: never classified, never acted on. Were the filter
    # dropped, run 2 would be ORPHANED and healed and both assertions below fail.
    assert [v.run_id for v in verdicts] == [1]
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert not any("/runs/2/" in p for p in api.posts)


def test_the_five_per_tick_cap_is_global_across_workflows() -> None:
    """Seven orphans spread over the four heal-safe workflows: exactly five heal, oldest
    first, the other two wait. Two workflows carry two orphans each, which is what shows
    the cap counts RUNS globally rather than allowing five per workflow. Negative control:
    reverted code sees only the one ci.yml run, so five never heal."""
    heal_safe = sorted(wd.HEAL_SAFE_WORKFLOWS)
    wfs = [heal_safe[i % len(heal_safe)] for i in range(7)]
    runs = [
        _run(i, minutes_ago=100 - i, branch=f"pr-{i}", workflow=wfs[i - 1]) for i in range(1, 8)
    ]
    api = FakeApi(
        {"in_progress": runs},
        {i: [_job(i * 10, run_id=i)] for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    healed = sorted(run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED)
    assert healed == [1, 2, 3, 4, 5]  # oldest first, across workflows
    assert outcomes[6] == outcomes[7] == wd.OUTCOME_NOT_ATTEMPTED
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/" in path]
    assert len(revision_reads) == 5


def test_live_job_read_bound_keeps_the_oldest_and_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The oldest runs are the only actionable ones; the newest carry the dispatch
    # evidence a saturation hold is judged by. A bound that kept only the oldest
    # would leave the sweep unable to tell a dead fleet from a busy one.
    runs = [_run(i, minutes_ago=100 - i, status="queued") for i in range(1, 7)]
    api = FakeApi({"queued": list(reversed(runs))}, {i: [] for i in range(1, 7)}, evidence=False)
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 3)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    logged: list[str] = []
    clock = _Clock()
    wd.run_watchdog(api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append)
    live_ids = set(range(1, 7))
    reads = [
        int(path.split("/runs/", 1)[1].split("/", 1)[0])
        for path in api.gets
        if "/jobs?" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in live_ids
    ]
    assert reads == [1, 2, 6]
    assert any("live job-read cap of 3" in line for line in logged)


def test_a_live_tick_under_the_read_bound_reads_every_run() -> None:
    runs = [_run(i, minutes_ago=100 - i, status="queued") for i in range(1, 4)]
    api = FakeApi({"queued": list(reversed(runs))}, {i: [] for i in range(1, 4)}, evidence=False)
    clock = _Clock()
    logged: list[str] = []
    wd.run_watchdog(api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append)
    assert not any("live job-read cap" in line for line in logged)


def test_a_live_tick_under_the_read_bound_behaves_as_before() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


# ── a GitHub rate limit ends the tick non-fatally, never as a crash ─────────


def _rate_limit_get(
    api: FakeApi, pattern: str, *, times: int, retry_after: float | None = None
) -> None:
    """Make the next ``times`` GETs matching ``pattern`` raise a rate-limited ApiError."""
    original_get = api.get
    left = {"n": times}

    def get(path: str) -> Any:
        if left["n"] > 0 and re.search(pattern, path):
            left["n"] -= 1
            raise wd.ApiError(
                403,
                "API rate limit exceeded for installation ID 12345",
                remaining="0",
                retry_after=retry_after,
            )
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_a_rate_limit_mid_listing_aborts_the_tick_and_preserves_already_decided_heals() -> None:
    """A 403 rate limit while reading run 2's jobs must not lose the tick: run 1, classified
    before it, is still healed, and the tick ends with the aborted outcome rather than a crash.
    Negative control: reverted code has no rate-limit handling, so the ApiError propagates out
    of run_watchdog and _sweep raises."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="a"), _run(2, minutes_ago=59, branch="b")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    _rate_limit_get(api, r"/actions/runs/2/jobs", times=1)  # one page fails, then serves
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes[1] == wd.OUTCOME_HEALED  # the already-classified orphan is still acted on
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in wd.FAILED_OUTCOMES
    assert any(v.verdict == wd.TICK_ABORTED_RATE_LIMITED for v in verdicts)
    assert any("rate limit" in line for line in logged)


def test_a_page_two_rate_limit_keeps_and_heals_page_one_candidates() -> None:
    orphan = _run(1, branch="orphan")
    filler = [
        _run(i, minutes_ago=5, branch=f"filler-{i}", workflow="issue-triage.yml")
        for i in range(2, wd.PAGE_SIZE + 1)
    ]
    api = FakeApi({"in_progress": [orphan] + filler}, {1: [_job(11)]})
    _rate_limit_get(api, r"/actions/runs\?.*page=2", times=1)
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes[1] == wd.OUTCOME_HEALED
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    assert api.posts[:2] == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


def test_a_rate_limited_tick_that_classifies_nothing_exits_nonzero_and_annotates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    marker = wd.RunVerdict(
        run_id=wd.RATE_LIMIT_MARKER_ID,
        run_attempt=0,
        head_branch="",
        head_repo="",
        event="",
        status="",
        url="",
        age=timedelta(0),
        verdict=wd.TICK_ABORTED_RATE_LIMITED,
        workflow="",
        detail="HTTP 403: API rate limit exceeded",
    )
    monkeypatch.setattr(
        wd,
        "run_watchdog",
        lambda *_a, **_k: ([marker], {wd.RATE_LIMIT_MARKER_ID: wd.OUTCOME_ABORTED_RATE_LIMITED}),
    )
    assert wd.main(argv) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed
    assert "recovery pass did not run" in printed
    text = summary.read_text(encoding="utf-8")
    assert "Aborted (rate limited)" in text
    assert "Inspected 0 run(s)." in text  # the marker is not counted as a run


def test_a_rate_limited_tick_exits_nonzero_even_after_acting_on_a_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    # Live work done before the abort does not make the tick healthy: the same
    # abort skipped the recovery pass, and a cancelled orphan in the last
    # tick-interval of its window ages out before any later tick reaches it.
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    classified = wd._base_verdict(_run(1), NOW)
    classified.verdict = wd.ORPHANED
    marker = wd.RunVerdict(
        run_id=wd.RATE_LIMIT_MARKER_ID,
        run_attempt=0,
        head_branch="",
        head_repo="",
        event="",
        status="",
        url="",
        age=timedelta(0),
        verdict=wd.TICK_ABORTED_RATE_LIMITED,
        workflow="",
        detail="HTTP 403: API rate limit exceeded",
    )
    monkeypatch.setattr(
        wd,
        "run_watchdog",
        lambda *_a, **_k: (
            [classified, marker],
            {
                1: wd.OUTCOME_HEALED,
                wd.RATE_LIMIT_MARKER_ID: wd.OUTCOME_ABORTED_RATE_LIMITED,
            },
        ),
    )
    assert wd.main(argv) == 1
    assert "recovery" in capsys.readouterr().out


def test_a_normal_healthy_tick_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: ([], {}))
    assert wd.main(["--repo", REPO, "--summary", str(summary)]) == 0
    assert "::error::" not in capsys.readouterr().out


def test_a_cheap_rate_limit_reset_is_waited_out_and_retried() -> None:
    """A rate limit whose window resets within budget is honoured with one wait-and-retry, not
    an abort. Negative control: reverted code neither knows retry_after nor retries, so it
    raises."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _rate_limit_get(api, r"/actions/runs/1/jobs", times=1, retry_after=5.0)
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}  # retried, not aborted
    assert clock.t >= 5.0  # the reset was waited out


def test_a_500_mid_listing_still_raises() -> None:
    """A server error is not a rate limit: it must propagate exactly as before, so the abort
    path never swallows it. Negative control: broadening the gather's except to all ApiError
    (not only rate_limited) would turn this raise into a silent abort."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_get = api.get

    def get(path: str) -> Any:
        if re.search(r"/actions/runs/1/jobs", path):
            raise wd.ApiError(500, "server error")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    with pytest.raises(wd.ApiError) as excinfo:
        _sweep(api)
    assert excinfo.value.status == 500


def test_the_rate_limit_hint_reads_retry_after_and_reset_headers() -> None:
    msg = email.message.Message()
    msg["Retry-After"] = "12"
    assert wd._rate_limit_hints(msg) == (12.0, None)
    reset = email.message.Message()
    reset["X-RateLimit-Remaining"] = "0"
    reset["X-RateLimit-Reset"] = str(int(NOW.timestamp()) + 40)
    wait, remaining = wd._rate_limit_hints(reset)
    assert remaining == "0" and wait is not None and wait > 0
    # Quota not spent: the reset is not a wait.
    plenty = email.message.Message()
    plenty["X-RateLimit-Remaining"] = "1000"
    plenty["X-RateLimit-Reset"] = str(int(NOW.timestamp()) + 40)
    assert wd._rate_limit_hints(plenty) == (None, "1000")
    assert wd._rate_limit_hints(None) == (None, None)


def test_a_rate_limited_error_is_recognised_from_message_or_headers() -> None:
    assert wd.ApiError(403, "API rate limit exceeded for installation").rate_limited
    assert wd.ApiError(429, "You have exceeded a secondary rate limit").rate_limited
    assert wd.ApiError(403, "forbidden", remaining="0").rate_limited
    assert not wd.ApiError(403, "resource not accessible").rate_limited
    assert not wd.ApiError(404, "not found").rate_limited
    assert not wd.ApiError(500, "rate limit").rate_limited  # only 403/429 count


def test_a_rate_limited_job_read_in_recovery_reaches_the_callers_handler() -> None:
    """A rate limit is not evidence the run is healthy.

    The caller records ``aborted-rate-limited`` -- a FAILED outcome -- and that is the
    only thing standing between an unclassified cancelled run and a green tick.
    Swallowing the limit inside the pass hid it from that handler, so the tick went
    green having never read the run's jobs.
    """
    cancelled = _run(
        1, minutes_ago=105, status="completed", conclusion="cancelled", updated_minutes_ago=85
    )
    api = FakeApi({"cancelled": [cancelled]}, {1: []}, evidence=False)
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            raise wd.ApiError(403, "API rate limit exceeded", remaining="0")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    with pytest.raises(wd.ApiError) as excinfo:
        wd.recover_cancelled_runs(
            api, _policy(), budget=1, tick=_tick(_Clock()), log=lambda _l: None
        )
    assert excinfo.value.rate_limited


def test_a_plain_job_read_failure_in_recovery_is_still_swallowed() -> None:
    """The narrow half of the rule above: only a RATE LIMIT escapes.

    A 500 or a 404 on one run's jobs is genuinely "nothing was touched, the next tick
    reads it again", and letting those out would abort the whole pass over one
    unreadable run.
    """
    cancelled = _run(
        1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=20
    )
    api = FakeApi({"cancelled": [cancelled]}, {1: []}, evidence=False)
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            raise wd.ApiError(500, "server error")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = wd.recover_cancelled_runs(
        api, _policy(), budget=1, tick=_tick(_Clock()), log=lambda _l: None
    )
    assert verdicts == []
    assert outcomes == {}


def test_the_classify_cap_refuses_a_green_tick_for_a_run_it_will_never_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tail run inside one schedule interval of expiry is recorded, not dropped.

    The cap is fair to a run that will be listed again; it is not fair to one the
    window check will drop before the cap is next consulted. With 6 minutes of a
    90-minute window left and a 10-minute schedule, run 2 gets no further tick, so the
    pass records it as inconclusive and the tick exits nonzero instead of green.
    Run 3 has 70 minutes left and is genuinely deferrable, so it is NOT recorded --
    otherwise every ordinary burst would turn the watchdog red.
    """
    served = _run(
        1, minutes_ago=105, status="completed", conclusion="cancelled", updated_minutes_ago=85
    )
    near_expiry = _run(
        2, minutes_ago=104, status="completed", conclusion="cancelled", updated_minutes_ago=84
    )
    deferrable = _run(
        3, minutes_ago=90, status="completed", conclusion="cancelled", updated_minutes_ago=20
    )
    api = FakeApi(
        {"cancelled": [served, near_expiry, deferrable]}, {1: [], 2: [], 3: []}, evidence=False
    )
    monkeypatch.setattr(wd, "RECOVERY_CLASSIFY_READS", 1)
    verdicts, outcomes = wd.recover_cancelled_runs(
        api, _policy(), budget=0, tick=_tick(_Clock()), log=lambda _l: None
    )
    assert outcomes.get(2) == wd.OUTCOME_LOOKUP_FAILED
    assert 3 not in outcomes
    assert wd.OUTCOME_LOOKUP_FAILED in wd.FAILED_OUTCOMES
    flagged = [v for v in verdicts if v.run_id == 2]
    assert flagged and flagged[0].verdict == wd.LOOKUP_INCONCLUSIVE
    assert "no later tick will list it" in flagged[0].detail


def test_the_classify_cap_costs_no_extra_api_read_to_flag_the_tail() -> None:
    """Flagging is decided from the run payload already in hand.

    If it cost a job read per tail run, the fix would spend exactly the budget the cap
    exists to protect.
    """
    runs = [
        _run(
            i,
            minutes_ago=105,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=85 - i * 0.1,
        )
        for i in range(1, 6)
    ]
    api = FakeApi({"cancelled": runs}, {i: [] for i in range(1, 6)}, evidence=False)
    reads = {"jobs": 0}
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            reads["jobs"] += 1
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    wd.RECOVERY_CLASSIFY_READS  # documents that the cap is what bounds the reads below
    import unittest.mock as _mock

    with _mock.patch.object(wd, "RECOVERY_CLASSIFY_READS", 2):
        wd.recover_cancelled_runs(
            api, _policy(), budget=0, tick=_tick(_Clock()), log=lambda _l: None
        )
    assert reads["jobs"] == 2


# ── a superseded orphan is not protected by the fleet hold ──────────────────


def test_a_superseded_orphan_is_cancelled_even_with_no_dispatch_evidence() -> None:
    """The hold protects a result someone wants; a superseded run has none.

    This is the shape of the 6-hour `fast-gate.yml` block: the tick held on evidence,
    and the run it held kept every later commit's Fast Gate out of the group. The heal
    still declines to RE-RUN it, so the cancel frees the slot without reviving a
    verdict the branch has moved past.
    """
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
        newest_by_branch={"main": 2},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert not _posts(api, "/rerun")
    assert outcomes[1] not in wd.FAILED_OUTCOMES


def test_without_supersession_the_same_run_is_still_held_on_missing_evidence() -> None:
    """Negative control: the evidence hold itself is not weakened.

    Same runs, same absent evidence, only the branch listing differs. Without this
    the change could have cleared every hold and read as passing.
    """
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert api.posts == []
    assert outcomes == {}


def test_the_supersession_question_is_not_asked_when_no_hold_applies() -> None:
    """The ordinary orphan pays no extra branch listing: the heal path already asks.

    Asserted on the CALL, not on a listing count, because the heal path legitimately
    lists the branch more than once and an absolute count would pin that instead.
    """
    import unittest.mock as _mock

    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    with _mock.patch.object(wd, "supersession_clears_hold") as spy:
        _sweep(api)
    assert spy.call_count == 0


def test_the_supersession_question_is_asked_when_a_hold_applies() -> None:
    """Positive control for the test above: the spy would be vacuous without it."""
    import unittest.mock as _mock

    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
    )
    with _mock.patch.object(wd, "supersession_clears_hold", return_value=False) as spy:
        _sweep(api)
    assert spy.call_count >= 1


def test_the_cleared_hold_is_logged_as_a_decision_not_as_a_cancel() -> None:
    """The line is emitted from the hold loop, before anything has decided to cancel.

    The scheduled tick runs with `dry_run` today (`WATCHDOG_ARMED` is false), so a line
    asserting the run WAS cancelled would be false on every real tick, and a reader
    debugging from the log would look for a cancel that never happened.
    """
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": 2})
    lines: list[str] = []
    assert wd.supersession_clears_hold(api, _policy(), verdict, lines.append)
    line = " ".join(lines)
    assert "is not applied" in line
    assert "it is cancelled" not in line


def test_a_pull_request_orphan_is_never_read_as_superseded() -> None:
    """Two open pull requests can share one head branch, so a newer run of that branch
    does not establish that THIS run was superseded."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="feature",
        head_repo=REPO,
        event="pull_request",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    api = FakeApi({}, {}, newest_by_branch={"feature": 2})
    assert not wd.supersession_clears_hold(api, _policy(), verdict, lambda _l: None)


def test_a_fork_orphan_is_never_read_as_superseded() -> None:
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo="someone/example-repo",
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    api = FakeApi({}, {}, newest_by_branch={"main": 2})
    assert not wd.supersession_clears_hold(api, _policy(), verdict, lambda _l: None)


def test_an_unanswerable_supersession_lookup_leaves_the_hold_standing() -> None:
    """Cancelling needs supersession ESTABLISHED, never assumed from a failed read."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )

    def explode(*_a: Any, **_k: Any) -> bool:
        raise wd.LookupInconclusive("the branch listing could not be read")

    import unittest.mock as _mock

    api = FakeApi({}, {})
    with _mock.patch.object(wd, "is_newest_for_branch", explode):
        assert not wd.supersession_clears_hold(api, _policy(), verdict, lambda _l: None)
