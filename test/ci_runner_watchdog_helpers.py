"""Shared fakes and builders for the CI runner watchdog tests.

``test_ci_runner_watchdog.py`` and its ``test_ci_runner_watchdog_*.py`` siblings
import from here: ``wd``, the ``scripts/ci/runner_watchdog.py`` module loaded
once per process; the run and job builders; ``FakeApi``, the GitHub API fake
that serves listings and records every write, so a test pins both the verdict
and the exact calls made; the injectable ``_Clock``; and the ``_tick`` and
``_sweep`` drivers. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    pr_numbers: tuple[int, ...] = (),
) -> dict[str, Any]:
    head_repo = {"fork": fork, "full_name": "someone/example-repo" if fork else REPO}
    return {
        # Most real same-repository runs carry an EMPTY `pull_requests`; a test that
        # wants the same-SHA judgement to be decidable names the numbers explicitly.
        "pull_requests": [{"number": number} for number in pr_numbers],
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
        open_pull_heads: dict[str, list[str] | BaseException] | None = None,
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
        # ``open_pull_heads`` maps a head branch to the head SHAs of its OPEN pull
        # requests, the way `GET /pulls?head=owner:branch&state=open` answers; a
        # branch not listed has no open pull request. An exception value is raised
        # from the read, so a test can make the pulls API fail.
        self._open_pull_heads = open_pull_heads or {}
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
        return [
            {
                "id": r["id"],
                "head_repository": r.get("head_repository"),
                "head_sha": r.get("head_sha"),
                "pull_requests": r.get("pull_requests") or [],
            }
            for r in matching
        ]

    def _listing_headed_by(self, newest: int, branch: str) -> list[dict[str, Any]]:
        """A listing whose newest same-repo run is `newest`, followed by every other run
        known on the branch (the judged run included), as GitHub would show them."""
        newest_run = self._run_by_id(newest)
        head = {
            "id": newest,
            "head_repository": {"full_name": REPO},
            "head_sha": newest_run.get("head_sha", f"sha-{newest}"),
            "pull_requests": newest_run.get("pull_requests") or [],
        }
        rest = [e for e in self._listing_for(branch) if e["id"] != newest]
        for run_id, run in self.run_overrides.items():
            if (
                run.get("head_branch") == branch
                and run_id != newest
                and all(e["id"] != run_id for e in rest)
            ):
                rest.append(
                    {
                        "id": run_id,
                        "head_repository": run.get("head_repository"),
                        "head_sha": run.get("head_sha"),
                        "pull_requests": run.get("pull_requests") or [],
                    }
                )
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
        if base.endswith("/pulls"):
            assert params.get("state") == "open", path
            owner, _, branch = params["head"].partition(":")
            assert owner == REPO.partition("/")[0], path
            heads = self._open_pull_heads.get(branch, [])
            if isinstance(heads, BaseException):
                raise heads
            return [
                {"number": 100 + index, "head": {"sha": sha, "ref": branch}}
                for index, sha in enumerate(heads)
            ]
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
            # ``total_count`` because the real endpoint always sends it and
            # ``list_jobs`` bounds its paging by it; a fake that omitted it would
            # exercise only the no-total_count fallback.
            return {"total_count": len(jobs), "jobs": jobs[start : start + per_page]}
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


def _saturation_band_api() -> FakeApi:
    """An orphan, a MIDDLE run holding the only slow start, and a young prompt start.

    The middle run is 20 minutes old and its routed job waited 8 for a runner, so it
    is both old enough to be reserved for evidence and slow enough to hold. The young
    run is 3 minutes old, under the 5-minute saturation line, so it cannot contain a
    wait that crossed it and can only ever show the fleet dispatching.
    """
    middle = _run(2, minutes_ago=20, branch="middle")
    young = _run(3, minutes_ago=3, branch="young")
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
