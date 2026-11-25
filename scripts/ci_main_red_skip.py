"""Deselect, on a pull request, the tests that are already red on main.

`.github/workflows/main-red-watch.yml` keeps one open `Main is red:` issue while
a push run of `CI` on main names failing tests. A pull request opened meanwhile
fails the same tests and its author loops on a red they did not cause. This
module lets PR CI skip exactly those tests, and nothing else.

It has two halves, so the trust rules live in one place:

    python3 scripts/ci_main_red_skip.py decide
        Run ONCE per CI run, in the `changes` job. Reads the open issue through
        the REST API and writes `main_red_skip=<json>` to $GITHUB_OUTPUT, or an
        empty value for "run everything". It also writes one line saying why to
        $GITHUB_STEP_SUMMARY.

    python -m pytest -p scripts.ci_main_red_skip ...
        Loaded by every backend shard. Reads $CI_MAIN_RED_SKIP (the output
        above), validates it again, and deselects the exact node ids it collects.

    python3 scripts/ci_main_red_skip.py summary
        A shard step before the tests, as the runner identity: lists the same
        node ids, with the issue link, in the shard's job summary.

Guards (each is pinned by `test/test_ci_main_red_skip.py`):

1. Only an OPEN issue with the `main-red` label is considered, and of those
   only the one the watcher's own `pick_issue` (in
   `.github/scripts/main_red_nodeids.py`) takes as its own: the Actions bot as
   author, the `Main is red: ` title and a valid state record. Its node ids
   come from the watcher's own `parse_state`. Each must also match
   ``_NODEID`` (a path under test/ or src/ ending .py, then ::name, with an
   optional `@group`). An id that does not match is not deselected; it runs.
2. Only exact node ids are deselected, never a file, directory or shard. More
   than ``MAX_DESELECT`` ids deselects nothing.
3. Only a `pull_request` run whose base is `main` reads the issue. A push,
   merge_group or release/** run always runs everything, so the watcher still
   sees the break and closes it.
4. A PR labelled `main-red-fix` or `ci-full-run`, or whose body names the
   issue, runs everything: the fix must prove itself. A node id whose test
   file the PR changes is kept too.
5. Any failure to read or parse the issue runs everything, with a ::warning.
   This module fails open to running tests, never to skipping them. A read
   the API refuses for a rate limit is tried once without the token, then
   waited for within ``MAX_WAIT_SECONDS``; a refusal that names no rate limit
   is not retried. Each answer passes the same checks as a first one.
6. Each shard's job summary lists what it deselected and links the issue.

Nothing read from the issue reaches a shell word or a ``${{ }}`` expression in
a `run:` script: the workflow hands the JSON to pytest through an `env:` value,
and this module reads it there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

LABEL = "main-red"
FIX_LABEL = "main-red-fix"
FULL_RUN_LABEL = "ci-full-run"
ENV_NAME = "CI_MAIN_RED_SKIP"
MAX_DESELECT = 50
# The watcher's own module: which issue is its own, and its state record, are
# read with the same functions `main-red-watch.yml` and `main-red-pr-notice.yml`
# use, never a second copy.
_WATCH_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "main_red_nodeids.py"

# A path under test/ or src/: segments start with a letter, digit or underscore,
# so `..`, `.hidden` and an absolute path cannot match. Then `::name`, at most
# one `::method`, an optional parameter id of plain characters (no shell
# metacharacter, no quote, no backslash, no slash), and the optional `@group`
# that `--dist loadgroup` appends to an `xdist_group` test's id.
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
_GROUP = re.compile(r"@[A-Za-z0-9_.-]{1,100}$")
_NODEID = re.compile(
    rf"(?:test|src)/(?:{_SEGMENT}/){{0,10}}{_SEGMENT}\.py"
    r"::[A-Za-z_][A-Za-z0-9_]{0,199}(?:::[A-Za-z_][A-Za-z0-9_]{0,199})?"
    r"(?:\[[A-Za-z0-9_.,:=+ -]{1,150}\])?"
    r"(?:@[A-Za-z0-9_.-]{1,100})?"
)
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")


def valid_nodeid(value: Any) -> bool:
    """True for one exact pytest node id this module may deselect."""
    return (
        isinstance(value, str)
        and len(value) <= 400
        and ".." not in value
        and _NODEID.fullmatch(value) is not None
    )


def without_group(nodeid: str) -> str:
    """The id without its `@group` suffix: xdist adds it after this plugin's hook runs."""
    return _GROUP.sub("", nodeid)


def issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


# -- decide: the `changes` job half ------------------------------------------


def _watch_module() -> Any:
    spec = importlib.util.spec_from_file_location("main_red_nodeids_for_skip", _WATCH_SCRIPT)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {_WATCH_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_watcher_rows(issues: list[Any]) -> list[dict[str, Any]]:
    """REST issues in the `gh issue list --json number,title,body,author` shape.

    Keeps only open, `main-red`-labelled issues that are not pull requests; the
    watcher's `pick_issue` then decides which one is its own.
    """
    rows = []
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state") != "open":
            continue
        labels = issue.get("labels")
        if not isinstance(labels, list) or not any(
            isinstance(lab, dict) and lab.get("name") == LABEL for lab in labels
        ):
            continue
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        rows.append(
            {
                "number": number,
                "title": issue.get("title"),
                "body": issue.get("body"),
                "author": issue.get("user"),
            }
        )
    return rows


def body_names_issue(body: str, repo: str, number: int) -> bool:
    """True when a PR body links issue ``number`` (``#N`` or its URL)."""
    if re.search(rf"(?<![\w/#])#{number}(?!\d)", body):
        return True
    return (
        re.search(rf"github\.com/{re.escape(repo)}/issues/{number}(?!\d)", body, re.I) is not None
    )


def decide(
    *,
    event: str,
    base_ref: str,
    labels: list[str],
    pr_body: str,
    repo: str,
    issues: list[Any],
    changed_files: set[str] | None,
) -> tuple[dict[str, Any] | None, str]:
    """``(plan, reason)``. ``plan`` is None for "run everything".

    ``issues`` is the open-issue list the API returned. ``changed_files`` is the
    set of paths the PR changes, or None when it could not be read.
    """
    if event != "pull_request":
        return None, f"{event} run: full suite (the main-red watcher needs it)."
    if base_ref != "main":
        return None, f"base is {base_ref or 'unknown'}, not main: full suite."
    if FULL_RUN_LABEL in labels:
        return None, f"`{FULL_RUN_LABEL}` label: full suite."
    watch = _watch_module()
    issue = watch.pick_issue(as_watcher_rows(issues))
    if issue is None:
        return None, "No open main-red issue filed by the watcher: full suite."
    number = int(issue["number"])
    link = issue_url(repo, number)
    if FIX_LABEL in labels:
        return None, f"`{FIX_LABEL}` label: full suite, so the fix for {link} proves itself."
    if body_names_issue(pr_body, repo, number):
        return None, f"The PR body names {link}: full suite, so the fix proves itself."
    nodeids = list(watch.parse_state(str(issue.get("body") or ""))["failures"])
    if len(nodeids) > MAX_DESELECT:
        return None, (
            f"{link} names {len(nodeids)} tests, over the cap of {MAX_DESELECT}: full suite."
        )
    if changed_files is None:
        return None, "Could not list the PR's changed files: full suite."
    kept: list[str] = []
    rejected = touched = 0
    for nodeid in nodeids:
        if not valid_nodeid(nodeid):
            rejected += 1
        elif nodeid.split("::", 1)[0] in changed_files:
            touched += 1
        elif nodeid not in kept:
            kept.append(nodeid)
    note = f" {rejected} id(s) failed validation and run." if rejected else ""
    note += f" {touched} id(s) are in files this PR changes and run." if touched else ""
    if not kept:
        return None, f"{link} leaves nothing to deselect for this PR: full suite.{note}"
    plan = {"issue": number, "nodeids": kept}
    return plan, f"Deselecting {len(kept)} test(s) red on main, from {link}.{note}"


API_URL = "https://api.github.com"
REQUEST_TIMEOUT = 20
# Seconds one `decide` may sleep for rate-limit resets, in total. The `changes`
# job gates every other CI job and holds its runner while this sleeps, so the
# budget is a third of that job's timeout: a slow full-history checkout and
# this wait together still end inside it. A reset further away is not waited
# for at all. `test_the_wait_budget_fits_the_changes_job` pins the fit.
MAX_WAIT_SECONDS = 300
MAX_TOKEN_READS = 3
# Added to a reset time: the runner's clock and GitHub's are not the same one.
RESET_MARGIN_SECONDS = 5
# GitHub's guidance for a secondary limit that names no `Retry-After`.
SECONDARY_WAIT_SECONDS = 60
_ERROR_BODY_BYTES = 4000
_WHOLE = re.compile(r"[0-9]{1,12}")

Response = tuple[int, dict[str, str], bytes]


class IssueReadError(Exception):
    """The issue was not read. The caller runs the full suite."""


class RateLimited(IssueReadError):
    """A rate limit refused the read and the bounded wait did not clear it."""


def _send(url: str, token: str) -> Response:
    """One GET: ``(status, lower-cased headers, body)``. No ``token`` reads anonymously."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- scheme and host pinned to API_URL, repo checked against _REPO
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            return response.status, _lowered(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_ERROR_BODY_BYTES)
        except (OSError, ValueError, AttributeError):
            body = b""
        return exc.code, _lowered(exc.headers), body


def _lowered(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers.items() if headers else [])}


def _whole(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if _WHOLE.fullmatch(text) else None


def rate_limit(response: Response, now: float) -> tuple[str, int | None] | None:
    """``(kind, seconds to wait)`` for a refusal that names a rate limit, else None.

    A 403 or 429 names one through a `Retry-After` header, through
    `X-RateLimit-Remaining: 0`, by being a 429, or in its body text. A 403
    with none of these is a permission error. The wait is None when the
    primary limit's reset time cannot be read.
    """
    status, headers, body = response
    if status not in (403, 429):
        return None
    if "retry-after" in headers:
        after = _whole(headers["retry-after"])
        return "secondary", SECONDARY_WAIT_SECONDS if after is None else max(after, 1)
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        reset = _whole(headers.get("x-ratelimit-reset"))
        if reset is None:
            return "primary", None
        return "primary", max(reset - int(now), 0) + RESET_MARGIN_SECONDS
    if status == 429 or b"rate limit" in body.lower():
        return "secondary", SECONDARY_WAIT_SECONDS
    return None


def issue_page(response: Response) -> list[Any]:
    """The issues of one complete 200 answer. Anything less raises."""
    _status, headers, body = response
    if 'rel="next"' in headers.get("link", ""):
        raise ValueError("the open main-red issues do not fit one page")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("the issues API did not return a list")
    return data


def fetch_open_issues(
    repo: str,
    token: str,
    *,
    send: Callable[[str, str], Response] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> list[Any]:
    """The open `main-red` issues, or an exception: the caller then runs everything.

    The token draws on the rate limit every workflow of the repository shares.
    When that refuses the read, one read without the token is tried first: it
    draws on the runner address's own small limit and costs no wait. Only then
    does this sleep for the token's reset, when that is inside what is left of
    ``MAX_WAIT_SECONDS``.
    """
    if _REPO.fullmatch(repo) is None:
        raise ValueError("REPO has an unexpected shape")
    if not token:
        raise IssueReadError("GH_TOKEN is empty")
    send = send or _send
    query = urllib.parse.urlencode({"state": "open", "labels": LABEL, "per_page": "100"})
    url = f"{API_URL}/repos/{repo}/issues?{query}"
    waited = 0
    anonymous = ""
    for read in range(1, MAX_TOKEN_READS + 1):
        response = send(url, token)
        if response[0] == 200:
            return issue_page(response)
        limit = rate_limit(response, clock())
        if limit is None:
            raise IssueReadError(f"HTTP {response[0]} that names no rate limit, so not retried")
        kind, delay = limit
        if not anonymous:
            try:
                without_token = send(url, "")
            except OSError as exc:
                anonymous = f"; the read without a token failed ({type(exc).__name__})"
            else:
                if without_token[0] == 200:
                    log(f"Main-red skip: {kind} rate limit on the token; read without it.")
                    return issue_page(without_token)
                anonymous = f"; the read without a token got HTTP {without_token[0]}"
        if delay is None:
            raise RateLimited(f"{kind} rate limit with no readable reset time{anonymous}")
        if read == MAX_TOKEN_READS:
            raise RateLimited(f"{kind} rate limit still set after {read} reads and {waited}s")
        if waited + delay > MAX_WAIT_SECONDS:
            raise RateLimited(
                f"{kind} rate limit resets in {delay}s, past what is left of the "
                f"{MAX_WAIT_SECONDS}s wait budget ({waited}s used){anonymous}"
            )
        log(f"::notice::Main-red skip: {kind} rate limit; waiting {delay}s, then reading again.")
        sleep(delay)
        waited += delay
    raise RateLimited("the read loop ended without an answer")


def changed_paths(base_sha: str) -> set[str] | None:
    if _SHA.fullmatch(base_sha) is None:
        return None
    done = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base_sha, "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        return None
    return {line for line in done.stdout.splitlines() if line}


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def cmd_decide(env: dict[str, str]) -> int:
    plan: dict[str, Any] | None = None
    repo = env.get("REPO", "")
    try:
        event = env.get("EVENT_NAME", "")
        base_ref = env.get("BASE_BRANCH", "")
        labels = json.loads(env.get("LABELS") or "[]")
        if not isinstance(labels, list):
            raise ValueError("LABELS is not a JSON list")
        labels = [str(name) for name in labels]
        if event != "pull_request" or base_ref != "main" or FULL_RUN_LABEL in labels:
            issues: list[Any] = []
        else:
            issues = fetch_open_issues(repo, env.get("GH_TOKEN", ""))
        changed = changed_paths(env.get("BASE_SHA", "")) if issues else set()
        plan, reason = decide(
            event=event,
            base_ref=base_ref,
            labels=labels,
            pr_body=env.get("PR_BODY", ""),
            repo=repo,
            issues=issues,
            changed_files=changed,
        )
    except Exception as exc:  # noqa: BLE001 - every failure runs the full suite
        plan = None
        detail = " ".join(str(exc).split())[:160]
        reason = f"Could not read the main-red issue ({type(exc).__name__}: {detail}): full suite."
        print(f"::warning::{reason}")
    value = json.dumps(plan, separators=(",", ":")) if plan else ""
    _append(env.get("GITHUB_OUTPUT"), f"main_red_skip={value}\n")
    _append(env.get("GITHUB_STEP_SUMMARY"), f"Main-red skip: {reason}\n")
    print(f"Main-red skip: {reason}")
    return 0


# -- the pytest plugin half --------------------------------------------------


def read_plan(raw: str | None, repo: str) -> tuple[int, list[str]] | None:
    """``(issue number, node ids)`` from $CI_MAIN_RED_SKIP, or None to deselect nothing.

    Validated again here: the value crossed a job boundary, and an id that
    fails the pattern, a count over the cap, or a malformed value deselects
    nothing at all.
    """
    if not raw:
        return None
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("not an object")
    number = plan.get("issue")
    nodeids = plan.get("nodeids")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("bad issue number")
    if not isinstance(nodeids, list) or not nodeids or len(nodeids) > MAX_DESELECT:
        raise ValueError("bad node id count")
    if not all(valid_nodeid(nodeid) for nodeid in nodeids):
        raise ValueError("a node id failed validation")
    if _REPO.fullmatch(repo) is None:
        raise ValueError("GITHUB_REPOSITORY has an unexpected shape")
    return number, list(dict.fromkeys(nodeids))


def summary_text(repo: str, number: int, nodeids: list[str]) -> str:
    lines = [
        f"### Deselected: {len(nodeids)} test(s) red on main, wherever this shard collects them",
        "",
        f"From {issue_url(repo, number)}. Push runs on main still run them. "
        f"Add the `{FIX_LABEL}` label, or name the issue in the PR body, to run them here.",
        "",
    ]
    lines += [f"- `{nodeid}`" for nodeid in nodeids]
    return "\n".join(lines) + "\n\n"


_PLAN_ATTR = "_ci_main_red_skip_plan"


def pytest_configure(config: Any) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        plan = read_plan(os.environ.get(ENV_NAME), repo)
    except (ValueError, TypeError) as exc:
        plan = None
        if not hasattr(config, "workerinput"):
            print(f"::warning::{ENV_NAME} ignored ({exc}); running every test.", file=sys.stderr)
    # No job summary from here: the Linux shards run pytest as an unprivileged
    # user that cannot write $GITHUB_STEP_SUMMARY (run-as-runner). The workflow's
    # own `summary` step, as the runner identity, writes it.
    setattr(config, _PLAN_ATTR, plan)


def pytest_collection_modifyitems(session: Any, config: Any, items: list[Any]) -> None:
    plan = getattr(config, _PLAN_ATTR, None)
    if plan is None:
        return
    wanted = {without_group(nodeid) for nodeid in plan[1]}
    deselected = [item for item in items if without_group(item.nodeid) in wanted]
    if deselected:
        items[:] = [item for item in items if without_group(item.nodeid) not in wanted]
        config.hook.pytest_deselected(items=deselected)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: Any, config: Any) -> None:
    plan = getattr(config, _PLAN_ATTR, None)
    if plan is not None:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        terminalreporter.write_line(
            f"main-red: deselected wherever collected, {len(plan[1])} node id(s) red on main "
            f"({issue_url(repo, plan[0])})"
        )


def cmd_summary(env: dict[str, str]) -> int:
    """Write the shard's job summary for $CI_MAIN_RED_SKIP; never fails the step."""
    repo = env.get("GITHUB_REPOSITORY", "")
    try:
        plan = read_plan(env.get(ENV_NAME), repo)
    except (ValueError, TypeError) as exc:
        print(f"::warning::{ENV_NAME} ignored ({exc}); running every test.")
        return 0
    if plan is None:
        print("Main-red skip: nothing deselected on this shard.")
        return 0
    text = summary_text(repo, *plan)
    _append(env.get("GITHUB_STEP_SUMMARY"), text)
    print(text, end="")
    return 0


COMMANDS = {"decide": cmd_decide, "summary": cmd_summary}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print("usage: ci_main_red_skip.py decide|summary", file=sys.stderr)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](dict(os.environ)))
