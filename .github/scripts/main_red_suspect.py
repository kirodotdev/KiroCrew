#!/usr/bin/env python3
"""Main-red suspect: say that main may be red when several pull requests agree.

`main-red-watch.yml` files the `Main is red:` issue only after a push run of
`CI` on `main` ends red, about an hour after the commit that broke it. Pull
requests based on that commit fail the same test meanwhile. `main-red-suspect.yml`
runs this file to file a notice from those failures, and to close it again.

    detect   after a failed `CI` run of a same-repository pull request: open one
             `ci: Main may be red: <node id>` issue when the same test fails with
             the same message start on 3 pull requests
    settle   after a push run of `CI` on `main`: close the open suspected issues
             that run answers

THE ISSUE IS A NOTICE AND NOTHING ELSE. It never feeds `scripts/ci_main_red_skip.py`
or `main-red-pr-notice.yml`: both take the issue the watcher's `pick_issue`
returns, which needs the title `Main is red: ` and a `main-red-state` record, and
the skip also needs the `main-red` label. A suspected issue has another title,
another record marker and another label, so three separate checks keep it out.
Only a push run on `main` can open the issue that deselects a test, by the
watcher's own unchanged rules.

WHAT COUNTS (all of it, else nothing is written):

* The run that started this and every other run counted is a failed `CI` run of
  a pull request whose head is in this repository and whose base is `main`. A
  fork's run is never read. The failure is on the pull request's current head.
* 3 pull requests fail one test with the same first 80 characters of message.
  Their authors differ, and so do the accounts whose push started each run, so
  one account that pushes to three branches counts once.
* None of the 3 changes that test's file. A changed-file list that cannot be
  read, or that GitHub cuts at 300 files, counts as "changes it".
* The node id is a plain `test/...py::name` or `src/...py::name` id. Any other
  is not counted.
* One of their bases is contained in the other two; it is recorded. A break, if
  there is one, is in that commit or an earlier one, so a push run on `main`
  that contains it and passes the full backend suite closes the issue.
* No suspected issue for the test was closed by a green main run in the last
  `COOLDOWN_HOURS` hours. Main ran it green after 3 pull requests failed it, so
  it is more likely flaky than broken, and without this a flaky test files one
  issue per flake.
* Every set of 3 among the runs read is tried, lowest pull request numbers
  first, so neither the order of the run list nor which run started this
  decides whether an issue opens or what it says.

Annotation text is whatever a pull request's tests printed. It is read as JSON,
never placed in a shell command, cut to printable ASCII, and written only inside
code spans and a JSON record whose `<`, `>`, `--` and `@` are escaped. Account
names are compared and never written. Every value that reaches an API path is
matched first.

Nothing is stored between runs: each run reads the failed runs of the last
`WINDOW_HOURS` hours again. The reads are bounded (the newest `MAX_CANDIDATES`
other pull requests, `MAX_JOBS_PER_RUN` jobs each; a pull request is read only
when its run is one of 3 that fail a test the same way), and the run reads no
other run when the token's rate limit is below `RATE_FLOOR`.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

TITLE_PREFIX = "ci: Main may be red: "
LABEL = "main-red-suspected"
CONFIRMED_LABEL = "main-red"
AREA_LABEL = "area: ci"
STATE_MARKER = "main-red-suspect-state"
STATE_OPEN = "<!-- " + STATE_MARKER + " "
STATE_CLOSE = " -->"
MIN_WITNESSES = 3
WINDOW_HOURS = 3
COOLDOWN_HOURS = 24
MAX_CANDIDATES = 10
MAX_JOBS_PER_RUN = 3
MAX_CHANGED_FILES = 300
MAX_OPEN = 3
MAX_NODEIDS = 20
RATE_FLOOR = 1500
GH_SECONDS = 60
# The names `main-red-watch.yml` gives its green proof; the test pins them equal.
BACKEND_JOB_PREFIX = "Backend Tests"
PROOF_STEP_NAME = "Upload shard coverage"
RED = ("failure", "timed_out")

# The shape `scripts/ci_main_red_skip.py` accepts for a node id it may deselect.
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
_NODEID = re.compile(
    rf"(?:test|src)/(?:{_SEGMENT}/){{0,10}}{_SEGMENT}\.py"
    r"::[A-Za-z_][A-Za-z0-9_]{0,199}(?:::[A-Za-z_][A-Za-z0-9_]{0,199})?"
    r"(?:\[[A-Za-z0-9_.,:=+ -]{1,150}\])?"
    r"(?:@[A-Za-z0-9_.-]{1,100})?"
)
_GROUP = re.compile(r"@[A-Za-z0-9_.-]{1,100}$")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,100}(?:\[bot\])?")

Gh = Callable[[Sequence[str]], "tuple[int, str, str]"]


def _sibling() -> Any:
    path = Path(__file__).resolve().with_name("main_red_nodeids.py")
    spec = importlib.util.spec_from_file_location("main_red_nodeids", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


watch = _sibling()


# -- plain data ----------------------------------------------------------------


def valid_nodeid(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 400 and _NODEID.fullmatch(value) is not None


def without_group(nodeid: str) -> str:
    """The id without the `@group` that `--dist loadgroup` appends."""
    return _GROUP.sub("", nodeid)


def test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _login(value: Any) -> str | None:
    """An account name, lower case, for comparing; never written anywhere."""
    return value.lower() if isinstance(value, str) and _LOGIN.fullmatch(value) else None


def json_values(text: str) -> list[Any]:
    """Every JSON value in ``text``: `gh api --paginate` prints one per page."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    at = 0
    while at < len(text):
        if text[at].isspace():
            at += 1
            continue
        value, at = decoder.raw_decode(text, at)
        values.append(value)
    return values


Failures = frozenset[tuple[str, str]]


def strict_failures(pages: list[Any]) -> set[tuple[str, str]]:
    """``(node id, message start)`` of each failing test one job annotated, plain ids only.

    Every pair is kept: a test that fails with one message on Linux and another
    on Windows shows both, so which job was read first decides nothing.
    """
    rows = [row for page in pages if isinstance(page, list) for row in page]
    entries = [watch.annotation_entry(row) for row in rows]
    return {entry for entry in entries if entry is not None and valid_nodeid(entry[0])}


def backend_failed_jobs(pages: list[Any]) -> list[int]:
    """Ids of the failed backend test jobs of one run, lowest first, capped."""
    ids = set()
    for page in pages:
        for job in page.get("jobs", []) if isinstance(page, dict) else []:
            if not isinstance(job, dict) or job.get("conclusion") not in RED:
                continue
            job_id = _id(job.get("id"))
            if job_id and str(job.get("name", "")).startswith(BACKEND_JOB_PREFIX):
                ids.add(job_id)
    return sorted(ids)[:MAX_JOBS_PER_RUN]


def ran_full_backend_suite(pages: list[Any]) -> bool:
    """True when every backend test job passed its coverage upload, and one did.

    `ci.yml` skips that step on a reduced backend scope, so a green run that
    skipped it did not try the test and closes nothing.
    """
    seen: list[Any] = []
    for page in pages:
        for job in page.get("jobs", []) if isinstance(page, dict) else []:
            if not isinstance(job, dict):
                continue
            if not str(job.get("name", "")).startswith(BACKEND_JOB_PREFIX):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("name") == PROOF_STEP_NAME:
                    seen.append(step.get("conclusion"))
    return bool(seen) and all(conclusion == "success" for conclusion in seen)


def changed_files(pages: list[Any]) -> set[str] | None:
    """Paths a compare lists, or ``None`` when the list is cut short."""
    names: set[str] = set()
    for page in pages:
        files = page.get("files") if isinstance(page, dict) else None
        if not isinstance(files, list):
            return None
        for row in files:
            if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
                return None
            names.add(row["filename"])
            if isinstance(row.get("previous_filename"), str):
                names.add(row["previous_filename"])
    return names if names and len(names) < MAX_CHANGED_FILES else None


class Run(NamedTuple):
    """One failed `CI` run of a same-repository pull request that targets main."""

    run_id: int
    pr: int
    head: str
    base: str
    actor: str


def run_of(row: Any, repo: str) -> Run | None:
    """The run a workflow-runs row describes, or ``None`` when it does not count."""
    if not isinstance(row, dict) or row.get("event") != "pull_request":
        return None
    if row.get("conclusion") != "failure":
        return None
    head_repo = row.get("head_repository")
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != repo:
        return None
    pulls = row.get("pull_requests")
    if not isinstance(pulls, list) or len(pulls) != 1 or not isinstance(pulls[0], dict):
        return None
    base = pulls[0].get("base")
    if not isinstance(base, dict) or base.get("ref") != "main":
        return None
    run_id, number = _id(row.get("id")), _id(pulls[0].get("number"))
    head, base_sha = row.get("head_sha"), base.get("sha")
    who = row.get("actor")
    actor = _login(who.get("login")) if isinstance(who, dict) else None
    if not run_id or not number or actor is None:
        return None
    if not isinstance(head, str) or not isinstance(base_sha, str):
        return None
    if _SHA.fullmatch(head) is None or _SHA.fullmatch(base_sha) is None:
        return None
    return Run(run_id, number, head, base_sha, actor)


def candidate_runs(pages: list[Any], repo: str, trigger: Run) -> list[Run]:
    """The newest failed run of each other pull request, newest first, capped.

    Which runs these are depends on their ids alone, not on the order of the list.
    """
    newest: dict[int, Run] = {}
    for page in pages:
        for row in page.get("workflow_runs", []) if isinstance(page, dict) else []:
            run = run_of(row, repo)
            if run is None or run.pr == trigger.pr:
                continue
            if run.pr not in newest or run.run_id > newest[run.pr].run_id:
                newest[run.pr] = run
    ordered = sorted(newest.values(), key=lambda run: run.run_id, reverse=True)
    return ordered[:MAX_CANDIDATES]


def author_of(pull: Any, repo: str, run: Run) -> str | None:
    """The author of the pull request ``run`` belongs to, when the run still counts.

    The head must be in this repository, the base `main`, and the head commit the
    one the run tested: a failure of a commit the author has replaced is stale.
    """
    if not isinstance(pull, dict) or _id(pull.get("number")) != run.pr:
        return None
    head, base = pull.get("head"), pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return None
    head_repo = head.get("repo")
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != repo:
        return None
    if base.get("ref") != "main" or head.get("sha") != run.head:
        return None
    user = pull.get("user")
    return _login(user.get("login")) if isinstance(user, dict) else None


class Witness(NamedTuple):
    run: Run
    author: str
    failures: Failures
    changed: frozenset[str]

    def shows(self, nodeid: str, message: str) -> bool:
        return (nodeid, message) in self.failures and test_file(nodeid) not in self.changed


def in_a_group(named: dict[int, Failures]) -> set[int]:
    """Ids of the runs that fail some test the same way as enough others.

    ``named`` is ``{run id: its (node id, message start) pairs}``. Only these runs can be
    witnesses, so only their pull requests are read.
    """
    groups: dict[tuple[str, str], set[int]] = {}
    for run_id, failures in named.items():
        for item in failures:
            groups.setdefault(item, set()).add(run_id)
    return {run_id for runs in groups.values() if len(runs) >= MIN_WITNESSES for run_id in runs}


def agreed_failures(chosen: Sequence[Witness]) -> dict[str, str]:
    """The tests every one of ``chosen`` fails the same way, in node id order, capped."""
    agreed: dict[str, str] = {}
    for nodeid, message in sorted(chosen[0].failures):
        if all(witness.shows(nodeid, message) for witness in chosen):
            agreed.setdefault(nodeid, message)
    return dict(list(agreed.items())[:MAX_NODEIDS])


def agreements(witnesses: list[Witness]) -> list[tuple[Witness, ...]]:
    """Every set of ``MIN_WITNESSES`` witnesses that counts, lowest pull request numbers first.

    A set counts when its authors differ, the accounts that started its runs
    differ, and all of it fails one test the same way. Every set is tried, so
    which witness was read first never decides whether one is found.
    """
    ordered = sorted(witnesses, key=lambda witness: witness.run.pr)
    found = []
    for chosen in itertools.combinations(ordered, MIN_WITNESSES):
        if len({witness.author for witness in chosen}) < MIN_WITNESSES:
            continue
        if len({witness.run.actor for witness in chosen}) < MIN_WITNESSES:
            continue
        if agreed_failures(chosen):
            found.append(chosen)
    return found


def common_base(bases: Sequence[str], relation: Callable[[str, str], str | None]) -> str | None:
    """The base every other base contains, or ``None`` when there is none.

    ``relation(a, b)`` is the compare status of ``b`` against ``a``; `ahead` and
    `identical` mean ``b`` contains ``a``. At most one base can be contained by
    all the others, so the answer does not depend on the order of ``bases``.
    """
    distinct = sorted(set(bases))
    for candidate in distinct:
        if all(
            other == candidate or relation(candidate, other) in ("ahead", "identical")
            for other in distinct
        ):
            return candidate
    return None


# -- the issue -----------------------------------------------------------------


def dump_state(state: dict[str, Any]) -> str:
    """JSON that cannot end the HTML comment it sits in and holds no `@`."""
    return str(watch.dump_state(state)).replace("@", "\\u0040")


def parse_state(body: str) -> dict[str, Any] | None:
    """The record of a suspected issue, every field checked, or ``None``.

    The body is editable by anyone who can edit the issue.
    """
    text = body or ""
    start = text.find(STATE_OPEN)
    end = text.find(STATE_CLOSE, start) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    try:
        raw = json.loads(text[start + len(STATE_OPEN) : end])
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("failures"), dict):
        return None
    failures = raw["failures"]
    base, prs, runs = raw.get("base"), raw.get("prs"), raw.get("runs")
    if not failures or len(failures) > MAX_NODEIDS:
        return None
    if not all(valid_nodeid(key) and isinstance(value, str) for key, value in failures.items()):
        return None
    if not isinstance(base, str) or _SHA.fullmatch(base) is None:
        return None
    for numbers in (prs, runs):
        if not isinstance(numbers, list) or len(numbers) != MIN_WITNESSES:
            return None
        if not all(_id(number) for number in numbers):
            return None
    cleaned = {key: watch.clean(value, watch.MESSAGE_CHARS) for key, value in failures.items()}
    return {"failures": cleaned, "base": base, "prs": list(prs), "runs": list(runs)}


def title_for(failures: dict[str, str]) -> str:
    return TITLE_PREFIX + without_group(next(iter(failures)))


def _span(text: str) -> str:
    """Text for a code span: nothing in it can open markup or a comment."""
    return str(watch.clean(text, 400)).replace("<", "(").replace(">", ")")


def render_body(state: dict[str, Any], repo: str) -> str:
    failures: dict[str, str] = state["failures"]
    base = state["base"]
    lines = [
        "### Main may be red (not confirmed)",
        "",
        f"The same test fails with the same message start on {MIN_WITNESSES} pull requests by "
        "different authors, and none of them changes the test's file. No push run of `CI` on "
        "`main` has shown it yet.",
        "",
        "This issue is a notice. No test is skipped because of it and no check changes: "
        "pull request CI skips a test only for the `Main is red:` issue that "
        "`main-red-watch.yml` files after a red push run on `main`.",
        "",
        "Failing tests, with the first 80 characters of each message:",
        "",
    ]
    lines += [
        f"- `{_span(nodeid)}`" + (f": `{_span(message)}`" if message else "")
        for nodeid, message in failures.items()
    ]
    lines += ["", "Seen on:", ""]
    lines += [
        f"- #{pr}, CI run https://github.com/{repo}/actions/runs/{run}"
        for pr, run in zip(state["prs"], state["runs"])
    ]
    lines += [
        "",
        f"The base the other two contain is `{base[:12]}` "
        f"(https://github.com/{repo}/commit/{base}). A break, if there is one, is in that "
        "commit or an earlier one.",
        "",
        "What closes this issue: a red push run on `main` that names one of these tests in "
        "the `Main is red:` issue (then that issue tracks it), or a push run on `main` that "
        "contains that commit and passes the full backend suite (then main was green).",
        "",
        "<sub>Filed by `.github/workflows/main-red-suspect.yml`. It never assigns anyone.</sub>",
        "",
        f"<!-- {STATE_MARKER} {dump_state(state)} -->",
        "",
    ]
    return "\n".join(lines)


def _labelled(issue: dict[str, Any], label: str) -> bool:
    labels = issue.get("labels")
    return isinstance(labels, list) and any(
        isinstance(row, dict) and row.get("name") == label for row in labels
    )


def _issues(pages: list[Any], label: str, state: str = "open") -> list[dict[str, Any]]:
    rows = [row for page in pages if isinstance(page, list) for row in page]
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and "pull_request" not in row
        and row.get("state") == state
        and _id(row.get("number"))
        and _labelled(row, label)
    ]


def _filed_here(pages: list[Any], state: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(issue, record)`` of each suspected issue in ``state`` that this workflow filed."""
    found = []
    for issue in _issues(pages, LABEL, state):
        if not watch.is_watcher_author(issue.get("user")):
            continue
        if not str(issue.get("title", "")).startswith(TITLE_PREFIX):
            continue
        record = parse_state(str(issue.get("body") or ""))
        if record is not None:
            found.append((issue, record))
    return found


def suspects_in(pages: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    """``(number, record)`` of each open suspected issue this workflow filed, oldest first."""
    found = [(int(issue["number"]), record) for issue, record in _filed_here(pages, "open")]
    return sorted(found, key=lambda pair: pair[0])


def cooling_in(pages: list[Any], since: str) -> set[str]:
    """Tests whose suspected issue a green main run closed at or after ``since``.

    Main ran such a test green after 3 pull requests failed it, so it is more
    likely flaky than broken, and suspecting it again at once would file one
    issue per flake. ``since`` and `closed_at` are UTC stamps of one format, so
    they compare as text.
    """
    cooling: set[str] = set()
    for issue, record in _filed_here(pages, "closed"):
        closed = issue.get("closed_at")
        if issue.get("state_reason") != "not_planned" or not isinstance(closed, str):
            continue
        if closed >= since:
            cooling |= {without_group(nodeid) for nodeid in record["failures"]}
    return cooling


def confirmed_in(pages: list[Any]) -> tuple[int, set[str]] | None:
    """``(number, node ids)`` of the watcher's open `Main is red:` issue, or ``None``."""
    rows = [
        {
            "number": issue["number"],
            "title": issue.get("title"),
            "body": issue.get("body"),
            "author": issue.get("user"),
        }
        for issue in _issues(pages, CONFIRMED_LABEL)
    ]
    issue = watch.pick_issue(rows)
    if issue is None:
        return None
    state = watch.parse_state(str(issue.get("body") or ""))
    return int(issue["number"]), {without_group(nodeid) for nodeid in state["failures"]}


def covered(suspects: list[tuple[int, dict[str, Any]]]) -> set[str]:
    return {without_group(nodeid) for _, state in suspects for nodeid in state["failures"]}


# -- the API -------------------------------------------------------------------


def run_gh(args: Sequence[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


class Api:
    """The `gh api` calls of one run: counted, and a failed one says why."""

    def __init__(self, gh: Gh, repo: str, summary: str | None = None) -> None:
        self.gh = gh
        self.repo = repo
        self.summary = summary
        self.calls = 0
        self.lines: list[str] = []
        self._relations: dict[tuple[str, str], str | None] = {}

    def say(self, text: str, level: str = "") -> None:
        line = f"::{level}::{text}" if level else text
        self.lines.append(line)
        print(line)
        if self.summary:
            with open(self.summary, "a", encoding="utf-8") as handle:
                handle.write(f"- {text}\n")

    def _call(self, verb: str, what: str, args: list[str]) -> list[Any] | None:
        self.calls += 1
        code, out, err = self.gh(args)
        if code == 0:
            try:
                return json_values(out)
            except ValueError:
                err = "the answer was not JSON"
        self.say(f"Could not {verb} {what} ({watch.clean(err, 200)}).", "notice")
        return None

    def get(self, what: str, path: str, paginate: bool = False) -> list[Any] | None:
        """The JSON pages of one read, or ``None`` after saying why it failed."""
        args = ["api", f"repos/{self.repo}/{path}"] + (["--paginate"] if paginate else [])
        return self._call("read", what, args)

    def write(self, what: str, method: str, path: str, fields: dict[str, Any]) -> Any | None:
        args = ["api", "--method", method, f"repos/{self.repo}/{path}"]
        for key, value in fields.items():
            for item in value if isinstance(value, list) else [value]:
                args += ["-f", f"{key}[]={item}" if isinstance(value, list) else f"{key}={item}"]
        pages = self._call("write", what, args)
        return pages[0] if pages else None

    def remaining(self) -> int | None:
        """REST requests left for this token; the read itself is not counted."""
        code, out, _ = self.gh(["api", "rate_limit"])
        try:
            value = json.loads(out)["resources"]["core"]["remaining"] if code == 0 else None
        except (ValueError, KeyError, TypeError):
            value = None
        return value if isinstance(value, int) else None

    def relation(self, older: str, newer: str) -> str | None:
        """The compare status of ``newer`` against ``older``; each pair is read once."""
        if (older, newer) not in self._relations:
            what = f"how {newer[:12]} relates to {older[:12]}"
            pages = self.get(what, f"compare/{older}...{newer}")
            status = pages[0].get("status") if pages and isinstance(pages[0], dict) else None
            status = status if isinstance(status, str) else None
            mirror = {"ahead": "behind", "behind": "ahead", "identical": "identical"}
            self._relations[(older, newer)] = status
            self._relations[(newer, older)] = mirror.get(status or "", status)
        return self._relations[(older, newer)]

    def issues(self, label: str, closed_since: str = "") -> list[Any] | None:
        """The open issues under ``label``, or those closed and updated since a stamp."""
        if closed_since:
            what = f"the `{label}` issues closed since {closed_since}"
            query = f"state=closed&labels={label}&since={closed_since}"
        else:
            what, query = f"the open `{label}` issues", f"state=open&labels={label}"
        return self.get(what, f"issues?{query}&per_page=100", True)

    def failures(self, run_id: int, known: set[str]) -> Failures:
        """What a run's failed backend test jobs name, without the tests in ``known``."""
        jobs = self.get(
            f"the jobs of run {run_id}", f"actions/runs/{run_id}/jobs?per_page=100", True
        )
        found: set[tuple[str, str]] = set()
        for job_id in backend_failed_jobs(jobs or []):
            what = f"the annotations of job {job_id}"
            notes = self.get(what, f"check-runs/{job_id}/annotations?per_page=100")
            found |= strict_failures(notes or [])
        return frozenset(pair for pair in found if without_group(pair[0]) not in known)

    def witness(self, run: Run, failures: Failures) -> Witness | None:
        """``run`` as a witness: its author and the files its pull request changes."""
        pull = self.get(f"pull request {run.pr}", f"pulls/{run.pr}")
        author = author_of(pull[0] if pull else None, self.repo, run)
        if author is None:
            return None
        what = f"the files pull request {run.pr} changes"
        pages = self.get(what, f"compare/{run.base}...{run.head}?per_page=100", True)
        changed = changed_files(pages) if pages is not None else None
        if changed is None:
            return None
        return Witness(run, author, failures, frozenset(changed))

    def close(self, number: int, comment: str, reason: str) -> None:
        if self.write(
            f"a comment on #{number}", "POST", f"issues/{number}/comments", {"body": comment}
        ):
            fields = {"state": "closed", "state_reason": reason}
            self.write(f"the close of #{number}", "PATCH", f"issues/{number}", fields)
            self.say(f"Closed suspected issue #{number}.")


# -- the two commands ----------------------------------------------------------


def close_confirmed(
    api: Api, confirmed: tuple[int, set[str]] | None, suspects: list[tuple[int, dict[str, Any]]]
) -> list[tuple[int, dict[str, Any]]]:
    """Close each suspected issue the `Main is red:` issue now names; return the rest."""
    if confirmed is None:
        return suspects
    number, nodeids = confirmed
    left = []
    for suspect, state in suspects:
        if {without_group(nodeid) for nodeid in state["failures"]} & nodeids:
            text = (
                f"A push run of `CI` on `main` is red on this: #{number} tracks it. Closing "
                "this notice."
            )
            api.close(suspect, text, "completed")
        else:
            left.append((suspect, state))
    return left


def _open_state(
    api: Api,
) -> tuple[tuple[int, set[str]] | None, list[tuple[int, dict[str, Any]]]] | None:
    main_red = api.issues(CONFIRMED_LABEL)
    suspected = api.issues(LABEL) if main_red is not None else None
    if main_red is None or suspected is None:
        return None
    return confirmed_in(main_red), suspects_in(suspected)


def detect(api: Api, trigger: Run, workflow_id: int, since: str, cool_since: str) -> int:
    """Open one suspected issue when 3 pull requests with a recent failed run agree.

    The run that started this must name a failing test, which keeps the reads
    off a lint-only failure. It need not be one of the 3: every run re-reads the
    newest failed runs, so a run that was queued and replaced before it started
    is still counted by the one that replaced it.
    """
    opened = _open_state(api)
    if opened is None:
        return 0
    confirmed, suspects = opened
    suspects = close_confirmed(api, confirmed, suspects)
    if confirmed is not None:
        api.say(f"#{confirmed[0]} already says main is red; nothing to suspect.")
        return 0
    if len(suspects) >= MAX_OPEN:
        api.say(f"{len(suspects)} suspected issues are open, the most kept at once.")
        return 0
    known = covered(suspects)
    failures = api.failures(trigger.run_id, known)
    if not failures:
        api.say("This run names no failing test that is not already suspected.")
        return 0
    closed = api.issues(LABEL, cool_since)
    if closed is None:
        return 0
    known |= cooling_in(closed, cool_since)
    failures = frozenset(pair for pair in failures if without_group(pair[0]) not in known)
    if not failures:
        api.say(
            f"Main ran every test that fails here green in the last {COOLDOWN_HOURS} hours, "
            "after it was suspected; not suspecting it again yet."
        )
        return 0
    left = api.remaining()
    if left is not None and left < RATE_FLOOR:
        api.say(
            f"{left} API requests are left for this token, under {RATE_FLOOR}; stopping.", "notice"
        )
        return 0
    listing = api.get(
        "the failed pull request runs",
        f"actions/workflows/{workflow_id}/runs?event=pull_request&status=failure"
        f"&created=%3E%3D{since}&per_page=100",
        True,
    )
    if listing is None:
        return 0
    runs = [trigger, *candidate_runs(listing, api.repo, trigger)]
    named = {trigger.run_id: failures}
    for run in runs[1:]:
        named[run.run_id] = api.failures(run.run_id, known)
    grouped = in_a_group(named)
    witnesses = []
    for run in runs:
        witness = api.witness(run, named[run.run_id]) if run.run_id in grouped else None
        if witness is not None:
            witnesses.append(witness)
    sets = agreements(witnesses)
    if not sets:
        api.say(
            f"No {MIN_WITNESSES} pull requests by different authors fail one test the same way."
        )
        return 0
    chosen, base = sets[0], None
    for chosen in sets:
        base = common_base([witness.run.base for witness in chosen], api.relation)
        if base is not None:
            break
    if base is None:
        api.say("No base of those pull requests is contained in the other two.")
        return 0
    agreed = agreed_failures(chosen)
    state = {
        "failures": agreed,
        "base": base,
        "prs": [witness.run.pr for witness in chosen],
        "runs": [witness.run.run_id for witness in chosen],
    }
    # Read again just before writing: the watcher may have filed meanwhile.
    opened = _open_state(api)
    if opened is None or opened[0] is not None:
        return 0
    if len(opened[1]) >= MAX_OPEN or covered(opened[1]) & {without_group(node) for node in agreed}:
        api.say("A suspected issue for this was filed meanwhile.")
        return 0
    label = {"name": LABEL, "color": "FBCA04", "description": "Main may be red; not confirmed"}
    api.write(f"the `{LABEL}` label (it may exist)", "POST", "labels", label)
    fields = {"title": title_for(agreed), "body": render_body(state, api.repo)}
    created = api.write(
        "the new issue", "POST", "issues", {**fields, "labels": [LABEL, AREA_LABEL]}
    )
    number = _id(created.get("number")) if isinstance(created, dict) else None
    if number is None:
        api.say("Could not file the suspected issue; writing nothing.", "warning")
        return 0
    if not _labelled(created, LABEL):
        # Without its label the next run would not find it and would file another.
        api.close(number, f"Filed without the `{LABEL}` label; closing.", "not_planned")
        return 0
    api.say(f"Filed suspected issue #{number} for {len(agreed)} test(s).")
    return 0


def settle(api: Api, run_id: int, head: str, conclusion: str) -> int:
    """Close the suspected issues a push run of `CI` on main answers."""
    suspected = api.issues(LABEL)
    suspects = suspects_in(suspected or [])
    if not suspects:
        api.say("No suspected issue is open.")
        return 0
    if conclusion != "success":
        main_red = api.issues(CONFIRMED_LABEL)
        close_confirmed(api, confirmed_in(main_red or []), suspects)
        return 0
    jobs = api.get(f"the jobs of run {run_id}", f"actions/runs/{run_id}/jobs?per_page=100", True)
    if jobs is None or not ran_full_backend_suite(jobs):
        api.say("This green run did not run the full backend suite; it closes nothing.")
        return 0
    for number, state in suspects:
        if api.relation(state["base"], head) not in ("ahead", "identical"):
            continue
        text = (
            f"Main was green: the `CI` push run for `{head[:12]}` "
            f"(https://github.com/{api.repo}/actions/runs/{run_id}) contains "
            f"`{state['base'][:12]}` and passed the full backend suite. Closing as not confirmed; "
            f"these tests are not suspected again for {COOLDOWN_HOURS} hours."
        )
        api.close(number, text, "not_planned")
    return 0


def _number(env: dict[str, str], name: str) -> int | None:
    value = env.get(name, "")
    return int(value) if value.isdigit() and int(value) > 0 else None


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, gh: Gh = run_gh) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("detect", "settle"))
    command = parser.parse_args(argv).command
    env = dict(os.environ) if env is None else env
    repo, head = env.get("REPO", ""), env.get("HEAD_SHA", "")
    run_id = _number(env, "RUN_ID")
    if _REPO.fullmatch(repo) is None or _SHA.fullmatch(head) is None or run_id is None:
        print("The event has no usable repository, head commit or run id; writing nothing.")
        return 0
    api = Api(gh, repo, env.get("GITHUB_STEP_SUMMARY"))
    if command == "settle":
        return settle(api, run_id, head, env.get("CONCLUSION", ""))
    pr, workflow_id = _number(env, "PR_NUMBER"), _number(env, "WORKFLOW_ID")
    base, actor = env.get("BASE_SHA", ""), _login(env.get("ACTOR"))
    if pr is None or workflow_id is None or actor is None or _SHA.fullmatch(base) is None:
        print("The event has no usable pull request, base, workflow or account; writing nothing.")
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    since, cool_since = (
        (now - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for hours in (WINDOW_HOURS, COOLDOWN_HOURS)
    )
    code = detect(api, Run(run_id, pr, head, base, actor), workflow_id, since, cool_since)
    api.say(f"{api.calls} API call(s) made.")
    return code


if __name__ == "__main__":
    sys.exit(main())
