"""Every dorny/paths-filter step detects changes with local git, never the REST API.

With its default ``token`` the action answers a ``pull_request`` event by paging
the list-files endpoint with the installation token that every workflow in this
repository shares. At dozens of runs an hour that call exhausts the quota, and
the job that happens to make it fails with ``API rate limit exceeded for
installation`` -- on a step that only decides which surfaces changed.

An empty token makes the pinned build diff ``pull_request.base.sha`` against
HEAD with local git, so the job must also make that base commit available:
either a full-history checkout or an explicit fetch of the base SHA.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _paths_filter_jobs():
    yaml = pytest.importorskip("yaml")
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for index, step in enumerate(steps):
                if str(step.get("uses", "")).startswith("dorny/paths-filter@"):
                    yield path.name, job_id, steps, index


def _fetches_the_pr_base(step: dict) -> bool:
    # The fetched SHA must be the event's own base -- the one the action
    # diffs against -- not any ref whose name happens to contain "base".
    env = step.get("env") or {}
    base_vars = [
        name
        for name, value in env.items()
        if "github.event.pull_request.base.sha" in str(value).replace(" ", "")
    ]
    run = str(step.get("run", ""))
    return "git fetch" in run and any(f'"${name}"' in run for name in base_vars)


_CASES = list(_paths_filter_jobs())


def test_the_scan_finds_the_known_consumers():
    names = {name for name, *_ in _CASES}
    assert {"ci.yml", "build.yml", "macos-on-demand.yml"} <= names


@pytest.mark.parametrize(
    "workflow, job_id, steps, index", _CASES, ids=[f"{c[0]}:{c[1]}" for c in _CASES]
)
def test_paths_filter_uses_local_git(workflow, job_id, steps, index):
    step = steps[index]
    token = (step.get("with") or {}).get("token")
    assert token == "", (
        f"{workflow} job {job_id!r}: dorny/paths-filter must set `token: ''` so it "
        "diffs with local git instead of spending the shared installation quota"
    )
    earlier = steps[:index]
    full_history = any(
        str(s.get("uses", "")).startswith("actions/checkout@")
        and str((s.get("with") or {}).get("fetch-depth")) == "0"
        for s in earlier
    )
    fetches_base = any(_fetches_the_pr_base(s) for s in earlier)
    assert full_history or fetches_base, (
        f"{workflow} job {job_id!r}: with `token: ''` the action diffs against the "
        "PR base SHA, so check out with fetch-depth: 0 or fetch that SHA first"
    )
