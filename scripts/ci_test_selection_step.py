#!/usr/bin/env python3
"""SHADOW step of ci.yml's `changes` job: score this pull request, write its verdict.

SHADOW ONLY: nothing reads the verdict to skip a test, and the step sets no job
output. It fetches the newest selection bundle a nightly `test-map.yml` run
published, scores the checked-out merge commit against its first parent with
`scripts/ci_test_selection.py`, and writes the verdict JSON that the workflow
uploads as `test-selection-verdict` for `scripts/ci_test_selection_ledger.py`.

    python3 scripts/ci_test_selection_step.py

It reads `GH_TOKEN`, `GITHUB_REPOSITORY`, `RUNNER_TEMP` and `GITHUB_STEP_SUMMARY`,
and takes no argument.

Which bundle is trusted
-----------------------
An artifact's name proves nothing: a dispatch on any branch, or a pull request
that adds a trigger to its own copy of `test-map.yml`, uploads one of the same
name. The bundle is taken only from a run for which GitHub itself reports every
one of (`trusted_run`): the workflow file `test-map.yml`, the branch `main`,
completed with success, the event `schedule` or `workflow_dispatch`, and this
repository as both the run's repository and its head repository. The file
inside must then name that run's commit.

Failing closed, and never failing the job
-----------------------------------------
No trusted run, no artifact or an expired one, a refused or failed request (a
rate limit is never waited for: the `changes` job gates every other job), an
archive that is not the one expected file, a bundle built more than
`MAX_BUNDLE_AGE_DAYS` days before the head commit or one that does not say when
it was built: each gives verdict `full` with the reason. So does anything the
scorer cannot do inside what is left of `BUDGET_SECS`. Exit 0 always.

Requests: three GETs per run (the workflow's runs, the run's artifacts, the
archive), none repeated.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_main_red_skip as red  # noqa: E402  (path set immediately above)
import ci_test_selection as selection  # noqa: E402
import ci_test_selection_windows as windows  # noqa: E402

API_URL = "https://api.github.com"
WORKFLOW = "test-map.yml"
BRANCH = "main"
EVENTS = ("schedule", "workflow_dispatch")
ARTIFACT = "test-selection"
MEMBER = "test-selection.json.gz"
VERDICT_DIR = "test-selection-verdict"
VERDICT_NAME = "verdict.json"
# Newest successful runs looked at. A night that could not build a whole bundle
# uploads nothing and is not successful, so the first one normally serves.
RUNS_READ = 5
MAX_BUNDLE_AGE_DAYS = 3
# Seconds for the whole step: the job's other steps are done by then, but every
# other CI job waits for this job to end.
BUDGET_SECS = 60.0
REQUEST_TIMEOUT = 10.0
# The least time worth starting the scorer with.
MIN_SCORE_SECS = 5.0
# Characters of the reason the summary line shows; the verdict file holds all of it.
SUMMARY_REASON_CHARS = 200
# Ten times the bundle of the day this was written (2.4 MB): an archive past it
# is not the bundle.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# One GET: `(status, lower-cased headers, body)` for a URL and a token ("" sends none).
Get = Callable[[str, str], tuple[int, dict[str, str], bytes]]


class NoBundle(Exception):
    """No trusted bundle could be read; the message is the verdict's reason."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hands a redirect back as the answer, so its target is fetched without the token."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get(
    url: str, token: str, schemes: tuple[str, ...] = ("https",)
) -> tuple[int, dict[str, str], bytes]:
    """`Get` over HTTPS. A body past `MAX_ARCHIVE_BYTES` is cut there, which no reader accepts."""
    if url.split("://", 1)[0] not in schemes:
        raise NoBundle("a request was not for an https URL")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=headers)
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- scheme checked above
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            return response.status, _lowered(response.headers), response.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, _lowered(exc.headers), b""


def _lowered(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers.items() if headers else [])}


def trusted_run(run: object, repo: str) -> bool:
    """Whether GitHub reports `run` as a successful nightly `test-map.yml` run on `repo`'s main."""
    if not isinstance(run, dict):
        return False
    names = [
        side.get("full_name") if isinstance(side, dict) else None
        for side in (run.get("repository"), run.get("head_repository"))
    ]
    return (
        isinstance(run.get("id"), int)
        and run.get("path") == f".github/workflows/{WORKFLOW}"
        and run.get("head_branch") == BRANCH
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("event") in EVENTS
        and names == [repo, repo]
    )


def _json(get: Get, url: str, token: str) -> Any:
    """The JSON of a 200 answer; anything else raises `NoBundle` and is not asked again."""
    status, headers, body = get(url, token)
    if status != 200:
        limit = red.rate_limit((status, headers, body), time.time())
        raise NoBundle(f"{limit[0]} rate limit" if limit else f"HTTP {status} from the API")
    return json.loads(body.decode("utf-8"))


def find_artifact(get: Get, repo: str, token: str) -> tuple[dict[str, Any], int]:
    """The newest trusted run and the id of its live `ARTIFACT`."""
    runs = _json(
        get,
        f"{API_URL}/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={BRANCH}&status=success&per_page={RUNS_READ}",
        token,
    )
    listed = runs.get("workflow_runs") if isinstance(runs, dict) else None
    run = next((r for r in listed or [] if trusted_run(r, repo)), None)
    if run is None:
        raise NoBundle(f"no trusted {WORKFLOW} run among the newest {RUNS_READ}")
    found = _json(
        get, f"{API_URL}/repos/{repo}/actions/runs/{run['id']}/artifacts?name={ARTIFACT}", token
    )
    for artifact in found.get("artifacts") or [] if isinstance(found, dict) else []:
        if (
            isinstance(artifact, dict)
            and artifact.get("name") == ARTIFACT
            and artifact.get("expired") is False
            and isinstance(artifact.get("id"), int)
            and (artifact.get("workflow_run") or {}).get("id") == run["id"]
        ):
            return run, artifact["id"]
    raise NoBundle(f"run {run['id']} has no live {ARTIFACT} artifact")


def download(get: Get, repo: str, artifact: int, token: str) -> bytes:
    """The artifact's archive. The API answers with a redirect, followed without the token."""
    status, headers, body = get(f"{API_URL}/repos/{repo}/actions/artifacts/{artifact}/zip", token)
    if status in (301, 302, 303, 307, 308) and headers.get("location"):
        status, headers, body = get(headers["location"], "")
    if status != 200:
        limit = red.rate_limit((status, headers, body), time.time())
        raise NoBundle(f"{limit[0]} rate limit" if limit else f"HTTP {status} for the archive")
    if len(body) > MAX_ARCHIVE_BYTES:
        raise NoBundle(f"the archive is over {MAX_ARCHIVE_BYTES} bytes")
    return body


def member(archive: bytes) -> bytes:
    """The one file the publisher uploads, read out of the artifact's zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            infos = zipped.infolist()
            if [info.filename for info in infos] != [MEMBER]:
                raise NoBundle(f"the archive does not hold exactly {MEMBER}")
            if infos[0].file_size > MAX_ARCHIVE_BYTES:
                raise NoBundle(f"{MEMBER} is over {MAX_ARCHIVE_BYTES} bytes")
            return zipped.read(infos[0])
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise NoBundle(f"unreadable archive: {type(exc).__name__}") from exc


def bundle_age(built_at: object, at: datetime) -> timedelta | None:
    """How long before `at` the bundle was built; None when it does not say, with a UTC offset."""
    if not isinstance(built_at, str):
        return None
    try:
        return at - datetime.fromisoformat(built_at)
    except (ValueError, TypeError):
        return None


def fetch_bundle(get: Get, repo: str, token: str, path: Path, at: datetime) -> timedelta:
    """Write the trusted bundle to `path` and return its age at `at`; else raise `NoBundle`."""
    if not _REPO.fullmatch(repo):
        raise NoBundle("GITHUB_REPOSITORY is not an owner/name pair")
    try:
        run, artifact = find_artifact(get, repo, token)
        path.write_bytes(member(download(get, repo, artifact, token)))
        doc = selection.ctm.load_map(path)
    except NoBundle:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise NoBundle(f"{type(exc).__name__}: {exc}") from exc
    if doc.get("commit") != run.get("head_sha"):
        raise NoBundle(f"the bundle is not of run {run['id']}'s commit")
    missing = [table for table in selection.ctm.BUNDLE_TABLES if not doc.get(table)]
    if missing:
        raise NoBundle(f"the bundle has no {', '.join(missing)} table")
    age = bundle_age(doc.get("built_at"), at)
    if age is None:
        raise NoBundle("the bundle does not say when it was built")
    if age > timedelta(days=MAX_BUNDLE_AGE_DAYS):
        raise NoBundle(f"bundle_stale: built {age.days} days before the head commit")
    return age


def summary_line(verdict: Mapping[str, Any], age: timedelta | None) -> str:
    """The step's one line: verdict, reason, would-run share, bundle age."""
    built = "no bundle" if age is None else f"bundle {age.total_seconds() / 3600:.1f} h old"
    return (
        f"Test selection (shadow, nothing acts on it): verdict `{verdict['verdict']}`, "
        f"reason: {str(verdict['reason'])[:SUMMARY_REASON_CHARS]}, "
        f"would run {verdict['would_run_share']:.1%} of the Linux "
        f"test node ids, {built}."
    )


def score(
    root: Path,
    out: Path,
    env: Mapping[str, str],
    get: Get = http_get,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], timedelta | None]:
    """The verdict for the checked-out merge commit against its first parent, and the bundle's age."""
    started = clock()
    meta = {"base": "HEAD^1", "head": "HEAD"}
    bundle = out.parent / MEMBER
    age: timedelta | None = None
    try:
        at = windows.commit_time(meta["head"], root)
        age = fetch_bundle(
            get, env.get("GITHUB_REPOSITORY", ""), env.get("GH_TOKEN", ""), bundle, at
        )
        left = BUDGET_SECS - (clock() - started)
        if left < MIN_SCORE_SECS:
            raise NoBundle(f"{left:.0f}s of the step's {BUDGET_SECS:g}s were left to score in")
    except Exception as exc:  # noqa: BLE001  (the step never fails the job)
        reason = str(exc) if isinstance(exc, NoBundle) else f"{type(exc).__name__}: {exc}"
        return selection.unscored(meta, f"no_bundle: {reason}"), None
    args = argparse.Namespace(
        root=str(root),
        bundle=str(bundle),
        rates=str(selection.RATES),
        name_status=None,
        timeout=left,
        **meta,
    )
    return selection.run(args), age


def main(env: Mapping[str, str] | None = None, root: Path = REPO_ROOT, get: Get = http_get) -> int:
    env = os.environ if env is None else env
    try:
        out = Path(env["RUNNER_TEMP"]) / VERDICT_DIR / VERDICT_NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        verdict, age = score(root, out, env, get)
        out.write_text(json.dumps(verdict, sort_keys=True) + "\n", encoding="utf-8")
        line = summary_line(verdict, age)
        print(line)
        if env.get("GITHUB_STEP_SUMMARY"):
            with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(line + "\n")
    except Exception as exc:  # noqa: BLE001  (the step never fails the job)
        print(f"::notice::Test selection (shadow): no verdict written: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
