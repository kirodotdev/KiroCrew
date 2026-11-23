"""GitHub as the miss ledger reads it, in memory, for its contract tests.

One fake of the three `gh` entry points `scripts/ci_test_selection_ledger.py`
uses (JSON, text and bytes), `install` that puts it in their place, and the
small builders the test files share. Nothing here starts a process or touches the network.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_test_selection_ledger as ledger

# The script's own binding of the shadow module: the one its calls go through.
shadow = ledger.shadow

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-18T00:00:00Z"
LATER = "2026-11-18T01:00:00Z"
VERDICT: dict[str, Any] = {
    "schema": 1,
    "base": "b" * 40,
    "head": "a" * 40,
    "bundle_commit": None,
    "verdict": "select",
    "reason": None,
    "tiers": {
        "A": ["test/test_gate.py"],
        "B": ["test/test_b.py"],
        "G": ["test/test_a.py", "test/test_c.py"],
    },
    "would_skip": ["test/test_a.py", "test/test_c.py"],
    "would_run_share": 0.5,
}
SKIP: dict[str, Any] = {
    "kind": "skip",
    "run": 1,
    "attempt": 1,
    "source": "1:pr-7",
    "fork": False,
    "pr": 7,
    "head": "a" * 40,
    "date": DAY,
    "reason": "x",
}
OLD_RUN = {**{k: v for k, v in SKIP.items() if k not in ("source", "reason")}, "kind": "run"}
FULL: dict[str, Any] = {
    **VERDICT,
    "verdict": "full",
    "reason": "no bundle",
    "tiers": {tier: [] for tier in "ABG"},
    "would_skip": [],
}


def job(job_id: int, shard: int, conclusion: str = "success", full: bool = True) -> dict[str, Any]:
    step = {
        "name": f"{ledger.FULL_SUITE_STEP} (shard {shard}/8)",
        "conclusion": "success" if full else "skipped",
    }
    return {
        "id": job_id,
        "name": f"Backend Tests (3.12, {shard})",
        "conclusion": conclusion,
        "steps": [{"name": "Select test scope", "conclusion": "success"}, step],
    }


class FakeGh:
    """GitHub as the script reads it: runs, each attempt's jobs, job logs, artifacts, issues."""

    def __init__(self, verdicts: Path) -> None:
        self.verdicts = verdicts
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.logs: dict[int, list[str]] = {}
        # A job's failure annotations, as test files; a job not listed has none.
        self.annotations: dict[int, list[str]] = {}
        self.messages: dict[str, str] = {}
        self.artifacts: dict[int, list[str]] = {}
        self.issues: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.page_size = 100
        self.overlap = 0
        # Jobs whose annotation list `gh` cannot read (a transient failure).
        self.unreadable: set[int] = set()
        self.expired: set[int] = set()
        # Runs that uploaded their artifact twice (two ids for one run).
        self.twice: set[int] = set()
        self.pool = 15000

    def add(
        self,
        run_id: int,
        pr: int | None,
        attempts: list[list[dict[str, Any]]],
        verdict: dict[str, Any] | None = VERDICT,
        date: str = DAY,
        branch: str | None = None,
        fork: bool = False,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "run_attempt": len(attempts),
            "head_sha": "a" * 40,
            "created_at": date,
            "pull_requests": [{"number": pr}] if pr else [],
            "repository": {"id": 1},
            "head_repository": {"id": 2 if fork else 1},
            "head_branch": branch or f"pr-{pr}",
        }
        self.runs.insert(0, run)
        self.set_attempts(run_id, attempts)
        if verdict is not None:
            (self.verdicts / f"{run_id}.json").write_text(json.dumps(verdict), encoding="utf-8")
        return run

    def set_attempts(self, run_id: int, attempts: list[list[dict[str, Any]]]) -> None:
        for number, jobs in enumerate(attempts, 1):
            self.jobs[(run_id, number)] = jobs
        for run in self.runs:
            if run["id"] == run_id:
                run["run_attempt"] = len(attempts)

    def _listed(self, url: str) -> list[dict[str, Any]]:
        """The runs a listing URL names: its event, branch, status and `created` window."""
        event = re.search(r"[?&]event=(\w+)", url)
        branch = re.search(r"[?&]branch=([\w/.-]+)", url)
        window = re.search(r"[?&]created=([\dTZ:-]+)\.\.([\dTZ:-]+)", url)
        failed = "status=failure" in url
        return [
            run
            for run in self.runs
            if event
            and run.get("event", "pull_request") == event[1]
            and (not branch or run.get("head_branch") == branch[1])
            and (not failed or run.get("conclusion") == "failure")
            and (not window or window[1] <= run["created_at"] <= window[2])
        ]

    def _page(self, url: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = re.search(r"[?&]page=(\d+)", url)
        start = (int(found.group(1)) - 1) * (self.page_size - self.overlap) if found else 0
        return items[start : start + self.page_size]

    def gh_json(self, args: list[str], root: Path) -> Any:
        url = args[1]
        self.calls.append(url)
        if "/workflows/ci.yml/runs?" in url:
            listed = self._listed(url)
            return {"total_count": len(listed), "workflow_runs": self._page(url, listed)}
        annotated = re.search(r"/check-runs/(\d+)/annotations\?", url)
        if annotated:
            if int(annotated.group(1)) in self.unreadable:
                raise subprocess.CalledProcessError(1, args)
            return [
                {
                    "annotation_level": "failure",
                    "title": f"{name}::test_one",
                    "message": f"{name}::test_one - {self.messages.get(name, 'assert 1 == 2')}",
                }
                for name in self.annotations.get(int(annotated.group(1)), [])
            ]
        attempt = re.search(r"/runs/(\d+)/attempts/(\d+)/jobs\?", url)
        if attempt:
            key = (int(attempt.group(1)), int(attempt.group(2)))
            if key not in self.jobs:
                raise subprocess.CalledProcessError(1, args)
            other = [{"id": 900 + n, "name": f"Lint {n}", "conclusion": "success"} for n in (1, 2)]
            jobs = other + self.jobs[key]
            return {"total_count": len(jobs), "jobs": self._page(url, jobs)}
        if "/actions/artifacts?name=" in url:
            # One artifact a run, its id the run's, created when the run was, newest first.
            dated = {run["id"]: run["created_at"] for run in self.runs}
            listed = [
                {
                    "id": run_id,
                    "name": "test-selection-verdict",
                    "expired": run_id in self.expired,
                    "created_at": dated[run_id],
                    "workflow_run": {"id": run_id},
                }
                for run_id in sorted(self.artifacts, key=lambda r: dated[r], reverse=True)
            ]
            listed += [
                {**item, "id": item["id"] + 1000} for item in listed if item["id"] in self.twice
            ]
            return {"total_count": len(listed), "artifacts": self._page(url, listed)}
        if url == "rate_limit":
            return {"resources": {"core": {"remaining": self.pool}}}
        if "/issues?" in url:
            return self.issues
        raise AssertionError(f"unexpected gh call: {url}")

    def raw(self, argv: list[str], root: Path) -> bytes:
        """An artifact's archive: one JSON file for each text the run's artifact holds."""
        self.calls.append(" ".join(argv[1:4]))
        held = io.BytesIO()
        with zipfile.ZipFile(held, "w") as archive:
            for number, text in enumerate(self.artifacts[int(argv[-1].rsplit("/", 2)[1])]):
                archive.writestr(f"verdict-{number}.json", text)
        return held.getvalue()

    def run(self, argv: list[str], root: Path) -> str:
        self.calls.append(" ".join(argv[1:4]))
        job_id = int(argv[-1].rsplit("/", 2)[1])
        if job_id not in self.logs:
            raise subprocess.CalledProcessError(1, argv)
        return "".join(
            f"2026-11-18T00:00:00.0Z FAILED {name}::test_one - "
            f"{self.messages.get(name, 'assert 1 == 2')}\n"
            for name in self.logs[job_id]
        )


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    """Put a fresh fake in place of the script's `gh` readers; each test file's `gh` fixture."""
    verdicts = tmp_path / "verdicts"
    verdicts.mkdir()
    fake = FakeGh(verdicts)
    monkeypatch.setattr(shadow, "_gh_json", fake.gh_json)
    monkeypatch.setattr(shadow, "_run", fake.run)
    monkeypatch.setattr(ledger, "_gh_bytes", fake.raw)
    return fake


def run_main(tmp_path: Path, *extra: str, offline: bool = True) -> int:
    argv = ["--ledger", str(tmp_path / "ledger.jsonl"), "--rates-out", str(tmp_path / "rates.json")]
    argv += ["--summary-out", str(tmp_path / "summary.md")]
    if offline:
        argv += ["--verdict-dir", str(tmp_path / "verdicts")]
    return ledger.main([*argv, *extra])


def rows(tmp_path: Path, kind: str | None = None) -> list[dict[str, Any]]:
    path = tmp_path / "ledger.jsonl"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    parsed = [json.loads(line) for line in text.splitlines()]
    return [row for row in parsed if kind is None or row["kind"] == kind]


def rates(tmp_path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((tmp_path / "rates.json").read_text(encoding="utf-8"))
    return loaded


def summary(tmp_path: Path) -> str:
    return (tmp_path / "summary.md").read_text(encoding="utf-8")


def failed_run(gh: FakeGh, run_id: int, pr: int, files: list[str], **kwargs: Any) -> None:
    """One full-suite run whose shard 2 failed `files` and whose shard 1 passed."""
    gh.add(run_id, pr, [[job(run_id * 10 + 1, 1), job(run_id * 10 + 2, 2, "failure")]], **kwargs)
    gh.logs[run_id * 10 + 2] = files


WIN_SELECT = {**VERDICT, "windows": {"verdict": "select", "run": ["test/test_in.py"]}}


def win_job(
    job_id: int, shard: int, conclusion: str = "success", full: bool = True
) -> dict[str, Any]:
    return {**job(job_id, shard, conclusion, full), "name": f"Backend Tests (Windows) ({shard})"}


def win_run(
    gh: FakeGh,
    run_id: int,
    pr: int | None,
    windows: list[str],
    linux: tuple[str, ...] = (),
    full: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """A full Linux run failing `linux`, beside two Windows shards, the second failing `windows`."""
    base = run_id * 10
    jobs = [job(base + 1, 1, "failure" if linux else "success")]
    red = "failure" if windows else "success"
    jobs += [win_job(base + 2, 1, full=full), win_job(base + 3, 2, red, full=full)]
    gh.logs.update({base + 1: list(linux), base + 3: windows})
    return gh.add(run_id, pr, [jobs], **{"verdict": WIN_SELECT, **kwargs})
