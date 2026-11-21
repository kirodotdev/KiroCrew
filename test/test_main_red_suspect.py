"""Contract tests for .github/workflows/main-red-suspect.yml and .github/scripts/main_red_suspect.py.

When the same test fails the same way on three unrelated pull requests, the
workflow files a `ci: Main may be red:` issue before the push run on main ends.
Five properties are pinned:

* COUNTING -- three pull requests by three authors with one node id and one
  message start open one issue; two, three by one author, three pushed by one
  account, one that changes the test file, a different message, a moved head and
  a fork's pull request do not.
* NEVER A SKIP -- `scripts/ci_main_red_skip.py` deselects nothing for a
  suspected issue, whatever label it carries, and neither the watcher nor the
  skip takes its record for a `Main is red:` record. Only the watcher's own
  issue, which a red push run on main files, deselects the test.
* SETTLING -- a red push run on main whose `Main is red:` issue names the test
  closes the suspected issue pointing there; a green push run that ran the full
  backend suite and contains the recorded base closes it as not confirmed; a
  reduced green run, or one that does not contain the base, closes nothing.
* UNTRUSTED TEXT -- a message is written only in code spans and an escaped
  record, a node id of another shape is not counted, and no account name is
  written.
* WIRING -- the watched name, the gate, the permissions, the concurrency groups
  and that nothing from the event is in the step's script.

No test starts a process or reads a clock: `gh` is a recording fake.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "main-red-suspect.yml"
SCRIPT = ROOT / ".github" / "scripts" / "main_red_suspect.py"
FIXTURES = ROOT / "test" / "fixtures" / "main_red"

mod = load_skill_script("main_red_suspect_under_test", SCRIPT)
watch = load_skill_script(
    "main_red_nodeids_for_suspect_test", ROOT / ".github" / "scripts" / "main_red_nodeids.py"
)
skip = load_skill_script(
    "ci_main_red_skip_for_suspect_test", ROOT / "scripts" / "ci_main_red_skip.py"
)

REPO = "example/repo"
BOT = {"login": "github-actions[bot]", "type": "Bot"}
SINCE = "2026-11-20T00:00:00Z"
COOL_SINCE = "2026-11-19T03:00:00Z"
CLOSED_AT = "2026-11-20T02:00:00Z"
WORKFLOW_ID = 7
BASE = "b" * 40
OLDER_BASE = "a" * 40
MAIN_HEAD = "c" * 40
NODE = "test/test_widget.py::TestWidget::test_turns"
MESSAGE = "assert 1 == 2"
FAILS = {NODE: MESSAGE}
MAIN_RUN_URL = "https://github.com/example/repo/actions/runs/900"


def _head(number: int) -> str:
    return f"{number:040x}"


def _notes(failures: dict[str, str]) -> list[dict[str, Any]]:
    """Check-run annotations as the rootdir conftest writes them, one per failing test."""
    return [
        {
            "path": nodeid.split("::", 1)[0],
            "start_line": 7,
            "end_line": 7,
            "annotation_level": "failure",
            "title": nodeid,
            "message": f"{nodeid} - {message}",
        }
        for nodeid, message in failures.items()
    ]


def _pr(
    number: int,
    author: str,
    *,
    actor: str | None = None,
    failures: dict[str, str] | None = None,
    notes: list[dict[str, Any]] | None = None,
    changed: tuple[str, ...] = ("src/widget.py",),
    base: str = BASE,
    head_repo: str = REPO,
    listed_repo: str = REPO,
    moved: bool = False,
    windows: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One pull request with one failed `CI` run and one failed backend test job."""
    rows = notes if notes is not None else _notes(FAILS if failures is None else failures)
    return {
        "number": number,
        "author": author,
        "actor": actor or author,
        "notes": rows,
        "changed": changed,
        "base": base,
        "head_repo": head_repo,
        "listed_repo": listed_repo,
        "moved": moved,
        "windows": _notes(windows) if windows is not None else None,
    }


def _suspect_issue(
    number: int = 40, failures: dict[str, str] | None = None, **over: Any
) -> dict[str, Any]:
    state = {"failures": failures or FAILS, "base": BASE, "prs": [1, 2, 3], "runs": [11, 12, 13]}
    row = {
        "number": number,
        "state": "open",
        "title": mod.title_for(state["failures"]),
        "body": mod.render_body(state, REPO),
        "labels": [{"name": mod.LABEL}],
        "user": BOT,
    }
    return {**row, **over}


def _confirmed_issue(number: int = 5, failures: dict[str, str] | None = None) -> dict[str, Any]:
    """The issue `main-red-watch.yml` files after a red push run on main."""
    state = watch.next_state(None, failures or FAILS, "a" * 40, 900, MAIN_RUN_URL)
    return {
        "number": number,
        "state": "open",
        "title": watch.title_for(state["failures"]),
        "body": watch.render_body(state, [], number),
        "labels": [{"name": "main-red"}],
        "user": BOT,
    }


class FakeGh:
    """`gh api` over canned rows: it records each call and keeps what is written."""

    def __init__(
        self,
        prs: tuple[dict[str, Any], ...] = (),
        *,
        issues: tuple[dict[str, Any], ...] = (),
        remaining: int = 5000,
        refuse: tuple[str, ...] = (),
        relations: dict[str, str] | None = None,
        main_jobs: list[dict[str, Any]] | None = None,
        drop_labels: bool = False,
    ) -> None:
        self.prs = {pr["number"]: pr for pr in prs}
        self.issues = [dict(issue) for issue in issues]
        self.remaining = remaining
        self.refuse = refuse
        self.relations = relations or {}
        self.main_jobs = main_jobs or []
        self.drop_labels = drop_labels
        self.calls: list[str] = []
        self.comments: dict[int, str] = {}

    def reads(self, fragment: str) -> list[str]:
        return [call for call in self.calls if fragment in call]

    def writes(self) -> list[str]:
        return [call for call in self.calls if call.startswith(("POST", "PATCH"))]

    def open_issues(self) -> list[dict[str, Any]]:
        return [issue for issue in self.issues if issue["state"] == "open"]

    def __call__(self, args: Any) -> tuple[int, str, str]:
        words = list(args)
        assert words[0] == "api"
        if words[1] == "rate_limit":
            return 0, json.dumps({"resources": {"core": {"remaining": self.remaining}}}), ""
        if words[1] == "--method":
            method, path = words[2], words[3]
            fields: dict[str, Any] = {}
            for pair in words[5::2]:
                key, _, value = pair.partition("=")
                if key.endswith("[]"):
                    fields.setdefault(key[:-2], []).append(value)
                else:
                    fields[key] = value
            self.calls.append(f"{method} {path}")
            return self._write(method, path.removeprefix(f"repos/{REPO}/"), fields)
        path = words[1].removeprefix(f"repos/{REPO}/")
        self.calls.append(f"GET {path}")
        if any(fragment in path for fragment in self.refuse):
            return 1, "", "gh: Resource not accessible by integration (HTTP 403)"
        return self._read(path)

    def _write(self, method: str, path: str, fields: dict[str, Any]) -> tuple[int, str, str]:
        if path == "labels":
            return 1, "", "gh: Validation Failed (HTTP 422) already_exists"
        if path == "issues":
            labels = [] if self.drop_labels else [{"name": name} for name in fields["labels"]]
            number = 500 + len(self.issues)
            row = {"number": number, "state": "open", "labels": labels, "user": BOT}
            self.issues.append({**row, "title": fields["title"], "body": fields["body"]})
            return 0, json.dumps(self.issues[-1]), ""
        number = int(path.split("/")[1])
        if path.endswith("/comments"):
            self.comments[number] = fields["body"]
            return 0, json.dumps({"id": 1}), ""
        assert method == "PATCH" and fields["state"] == "closed"
        for issue in self.issues:
            if issue["number"] == number:
                issue["state"] = "closed"
                issue["state_reason"] = fields["state_reason"]
                issue["closed_at"] = CLOSED_AT
        return 0, json.dumps({"number": number}), ""

    def _read(self, path: str) -> tuple[int, str, str]:
        if path.startswith("issues?"):
            label = path.split("labels=")[1].split("&")[0]
            state = path.split("state=")[1].split("&")[0]
            assert ("since=" in path) == (state == "closed")
            rows = [
                issue
                for issue in self.issues
                if issue["state"] == state and any(lab["name"] == label for lab in issue["labels"])
            ]
            return 0, json.dumps(rows), ""
        if path.startswith("actions/workflows/"):
            rows = [
                {
                    "id": 9000 + pr["number"],
                    "event": "pull_request",
                    "conclusion": "failure",
                    "head_sha": _head(pr["number"]),
                    "head_repository": {"full_name": pr["listed_repo"]},
                    "actor": {"login": pr["actor"]},
                    "pull_requests": [
                        {"number": pr["number"], "base": {"ref": "main", "sha": pr["base"]}}
                    ],
                }
                for pr in self.prs.values()
            ]
            # Two pages back to back, as `gh api --paginate` prints them.
            half = len(rows) // 2
            pages = [{"workflow_runs": rows[:half]}, {"workflow_runs": rows[half:]}]
            return 0, "".join(json.dumps(page) for page in pages), ""
        if path.startswith("actions/runs/900/jobs"):
            return 0, json.dumps({"jobs": self.main_jobs}), ""
        if path.startswith("actions/runs/"):
            number = int(path.split("/")[2]) - 9000
            jobs = [
                {"id": 100 + number, "name": "Backend Tests (3.12, 1)", "conclusion": "failure"},
                {"id": 200 + number, "name": "Lint", "conclusion": "failure"},
                {"id": 300 + number, "name": "Backend Tests (3.12, 2)", "conclusion": "success"},
            ]
            if self.prs[number]["windows"] is not None:
                windows = {"id": 600 + number, "name": "Backend Tests (Windows)"}
                jobs.insert(0, {**windows, "conclusion": "failure"})
            return 0, json.dumps({"jobs": jobs}), ""
        if path.startswith("check-runs/"):
            job = int(path.split("/")[1])
            if job > 600:
                return 0, json.dumps(self.prs[job - 600]["windows"]), ""
            return 0, json.dumps(self.prs[job - 100]["notes"]), ""
        if path.startswith("pulls/"):
            pr = self.prs[int(path.split("/")[1])]
            head = "f" * 40 if pr["moved"] else _head(pr["number"])
            row = {
                "number": pr["number"],
                "user": {"login": pr["author"]},
                "head": {"sha": head, "repo": {"full_name": pr["head_repo"]}},
                "base": {"ref": "main", "sha": pr["base"]},
            }
            return 0, json.dumps(row), ""
        if path.startswith("compare/"):
            older, newer = path.removeprefix("compare/").split("?")[0].split("...")
            for pr in self.prs.values():
                if (older, newer) == (pr["base"], _head(pr["number"])):
                    files = [{"filename": name} for name in pr["changed"]]
                    return 0, json.dumps({"status": "ahead", "files": files}), ""
            status = self.relations.get(f"{older}...{newer}", "diverged")
            return 0, json.dumps({"status": status, "files": []}), ""
        raise AssertionError(f"unexpected read {path}")


def _detect(gh: FakeGh, trigger: int = 1, cool_since: str = COOL_SINCE) -> Any:
    """Run `detect` for the failed run of pull request ``trigger``; return the Api."""
    pr = gh.prs[trigger]
    api = mod.Api(gh, REPO)
    run = mod.Run(9000 + trigger, trigger, _head(trigger), pr["base"], pr["actor"].lower())
    assert mod.detect(api, run, WORKFLOW_ID, SINCE, cool_since) == 0
    return api


def _settle(gh: FakeGh, conclusion: str) -> Any:
    api = mod.Api(gh, REPO)
    assert mod.settle(api, 900, MAIN_HEAD, conclusion) == 0
    return api


THREE = (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger"))
FULL_GREEN = [
    {
        "name": "Backend Tests (3.12, 1)",
        "steps": [{"name": "Upload shard coverage", "conclusion": "success"}],
    },
    {
        "name": "Backend Tests (Windows)",
        "steps": [{"name": "Upload shard coverage", "conclusion": "success"}],
    },
    {"name": "Lint", "steps": []},
]
REDUCED_GREEN = [
    {
        "name": "Backend Tests (3.12, 1)",
        "steps": [{"name": "Upload shard coverage", "conclusion": "skipped"}],
    }
]


# ── Counting ─────────────────────────────────────────────────────────────────


def test_three_pull_requests_by_three_authors_open_one_suspected_issue() -> None:
    gh = FakeGh(THREE)
    _detect(gh)
    (issue,) = gh.open_issues()
    assert issue["title"] == mod.TITLE_PREFIX + NODE
    assert {lab["name"] for lab in issue["labels"]} == {mod.LABEL, mod.AREA_LABEL}
    state = mod.parse_state(issue["body"])
    assert state == {
        "failures": FAILS,
        "base": BASE,
        "prs": [1, 2, 3],
        "runs": [9001, 9002, 9003],
    }
    for number in (1, 2, 3):
        assert f"- #{number}, CI run https://github.com/example/repo/actions/runs/900{number}" in (
            issue["body"]
        )
    assert f"`{NODE}`: `{MESSAGE}`" in issue["body"]
    assert "No test is skipped because of it" in issue["body"]
    # It writes an issue and its label, and nothing on a pull request.
    assert set(gh.writes()) <= {"POST repos/example/repo/labels", "POST repos/example/repo/issues"}
    assert gh.writes().count("POST repos/example/repo/issues") == 1


def test_the_failures_of_a_real_red_shard_are_counted_from_its_annotations() -> None:
    rows = json.loads((FIXTURES / "annotations_linux_shard.json").read_text(encoding="utf-8"))
    named = watch.parse_annotations(rows)
    assert named, "the fixture names failing tests"
    gh = FakeGh(tuple(_pr(n, who, notes=rows) for n, who in ((1, "ada"), (2, "grace"), (3, "ed"))))
    _detect(gh)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["failures"] == {
        nodeid: message for nodeid, message in named.items() if mod.valid_nodeid(nodeid)
    }


@pytest.mark.parametrize(
    "prs",
    [
        (_pr(1, "ada"), _pr(2, "grace")),
        (_pr(1, "ada"), _pr(2, "ada"), _pr(3, "ada")),
        (_pr(1, "ada"), _pr(2, "Ada"), _pr(3, "grace")),
        (_pr(1, "ada"), _pr(2, "ada", actor="hopper"), _pr(3, "grace")),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "grace", actor="hopper")),
        (_pr(1, "ada"), _pr(2, "grace", actor="ada"), _pr(3, "edsger", actor="ada")),
        (_pr(1, "ada"), _pr(2, "grace", actor="mallory"), _pr(3, "edsger", actor="mallory")),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", changed=("test/test_widget.py",))),
        (_pr(1, "ada", changed=("test/test_widget.py",)), _pr(2, "grace"), _pr(3, "edsger")),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", failures={NODE: "assert 1 == 3"})),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", failures={NODE + "_too": MESSAGE})),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", moved=True)),
        (_pr(1, "ada", moved=True), _pr(2, "grace"), _pr(3, "edsger")),
        (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", changed=())),
    ],
    ids=[
        "two-pull-requests",
        "three-by-one-author",
        "an-author-in-another-case",
        "the-first-author-again-pushed-by-another-account",
        "one-author-twice-pushed-by-two-accounts",
        "pushed-by-the-first-author",
        "two-pushed-by-one-account",
        "one-changes-the-test-file",
        "the-trigger-changes-the-test-file",
        "a-different-message",
        "a-different-test",
        "a-head-that-moved-on",
        "the-trigger-head-moved-on",
        "changed-files-unknown",
    ],
)
def test_fewer_than_three_unrelated_witnesses_open_nothing(prs: tuple[dict[str, Any], ...]) -> None:
    gh = FakeGh(prs)
    _detect(gh)
    assert gh.open_issues() == []
    assert gh.writes() == []


def test_a_fourth_pull_request_stands_in_for_one_that_changes_the_test_file() -> None:
    gh = FakeGh(
        (
            _pr(1, "ada"),
            _pr(2, "grace", changed=("test/test_widget.py",)),
            _pr(3, "edsger"),
            _pr(4, "barbara"),
        )
    )
    _detect(gh)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["prs"] == [1, 3, 4]


@pytest.mark.parametrize("field", ["listed_repo", "head_repo"])
def test_a_forks_pull_request_is_never_counted(field: str) -> None:
    """Neither when the run list says so, nor when only the pull request does."""
    fork = _pr(3, "edsger", **{field: "mallory/repo"})
    gh = FakeGh((_pr(1, "ada"), _pr(2, "grace"), fork))
    _detect(gh)
    assert gh.open_issues() == []
    if field == "listed_repo":
        assert gh.reads("actions/runs/9003") == [], "a fork's run is not read at all"


def test_a_run_that_started_from_a_forks_pull_request_opens_nothing() -> None:
    gh = FakeGh((_pr(1, "mallory", head_repo="mallory/repo"), _pr(2, "grace"), _pr(3, "edsger")))
    _detect(gh)
    assert gh.open_issues() == []


def test_only_the_tests_all_three_fail_the_same_way_are_listed() -> None:
    both = {NODE: MESSAGE, "test/test_other.py::test_b": "boom"}
    gh = FakeGh((_pr(1, "ada", failures=both), _pr(2, "grace", failures=both), _pr(3, "edsger")))
    _detect(gh)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["failures"] == FAILS


def test_the_recorded_base_is_the_one_the_other_two_contain() -> None:
    prs = (_pr(1, "ada"), _pr(2, "grace", base=OLDER_BASE), _pr(3, "edsger"))
    gh = FakeGh(prs, relations={f"{OLDER_BASE}...{BASE}": "ahead"})
    api = _detect(gh)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["base"] == OLDER_BASE
    between_bases = [call for call in gh.reads("compare/") if "per_page" not in call]
    assert between_bases == [f"GET compare/{OLDER_BASE}...{BASE}"]
    assert api.relation(BASE, OLDER_BASE) == "behind", "the mirror of a read costs no read"


def test_three_whose_bases_no_one_base_is_contained_in_open_nothing() -> None:
    gh = FakeGh((_pr(1, "ada"), _pr(2, "grace", base=OLDER_BASE), _pr(3, "edsger")))
    _detect(gh)
    assert gh.open_issues() == []


def test_a_set_with_no_common_base_does_not_hide_one_that_has_one() -> None:
    """Pull requests 1, 2, 3 agree and so do 1, 2, 4; only the second set shares a base."""
    prs = (_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger", base=OLDER_BASE), _pr(4, "barbara"))
    gh = FakeGh(prs)
    _detect(gh)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["prs"] == [1, 2, 4]


LINE = ["x", "y", "z"]


def _on_a_line(older: str, newer: str) -> str | None:
    """Compare status on the line x <- y <- z; `w` is on a branch of its own."""
    if "w" in (older, newer):
        return "diverged"
    gap = LINE.index(newer) - LINE.index(older)
    return "ahead" if gap > 0 else "behind" if gap < 0 else "identical"


@pytest.mark.parametrize(
    ("bases", "expected"),
    [
        (["x", "x", "x"], "x"),
        (["y", "z", "y"], "y"),
        (["z", "y", "x"], "x"),
        (["x", "w", "y"], None),
        (["w", "w", "w"], "w"),
    ],
)
def test_the_common_base_is_the_same_in_whatever_order_the_bases_come(
    bases: list[str], expected: str | None
) -> None:
    for order in itertools.permutations(bases):
        assert mod.common_base(order, _on_a_line) == expected
    assert mod.common_base(bases, lambda older, newer: None) == (
        expected if len(set(bases)) == 1 else None
    )


# ── The verdict does not depend on order ─────────────────────────────────────


def _filed_state(gh: FakeGh) -> dict[str, Any] | None:
    issues = gh.open_issues()
    assert len(issues) <= 1
    return mod.parse_state(issues[0]["body"]) if issues else None


def test_a_witness_that_cannot_join_does_not_block_two_that_can() -> None:
    """Author B pushed by C comes first; the set of A, B pushed by B, and C still counts."""
    prs = (
        _pr(1, "ada"),
        _pr(2, "grace", actor="edsger"),
        _pr(3, "grace"),
        _pr(4, "edsger"),
    )
    gh = FakeGh(prs)
    _detect(gh)
    assert _filed_state(gh)["prs"] == [1, 3, 4]


SIX = (
    _pr(1, "ada"),
    _pr(2, "grace", actor="edsger"),
    _pr(3, "grace"),
    _pr(4, "edsger", failures={NODE: MESSAGE, "test/test_other.py::test_b": "boom"}),
    _pr(5, "barbara", base=OLDER_BASE),
    _pr(6, "ada", actor="hopper", changed=("test/test_widget.py",)),
)


def test_neither_the_list_order_nor_the_run_that_started_it_changes_what_is_filed() -> None:
    seen = set()
    for order in itertools.permutations(SIX):
        for trigger in (1, 2, 3, 4, 5):
            gh = FakeGh(order)
            _detect(gh, trigger=trigger)
            seen.add(json.dumps(_filed_state(gh), sort_keys=True))
    (only,) = seen
    assert json.loads(only)["prs"] == [1, 3, 4]


def test_an_agreement_is_filed_by_a_run_that_is_not_part_of_it() -> None:
    """A queued run that a newer one replaced is still counted: its failed run is read again."""
    newer = _pr(9, "hopper", failures={"test/test_else.py::test_mine": "x"})
    gh = FakeGh((*THREE, newer))
    _detect(gh, trigger=9)
    assert _filed_state(gh)["prs"] == [1, 2, 3]
    assert gh.reads("pulls/9") == [], "a run in no group of 3 costs no pull request read"


def test_a_test_that_fails_differently_on_two_legs_matches_on_either_message() -> None:
    """The Linux job is read first; the message the others share is the Windows one."""
    both = _pr(1, "ada", failures={NODE: "assert 1 == 3"}, windows=FAILS)
    gh = FakeGh((both, _pr(2, "grace"), _pr(3, "edsger")))
    _detect(gh)
    assert _filed_state(gh)["failures"] == FAILS
    for trigger in (2, 3):
        gh = FakeGh((both, _pr(2, "grace"), _pr(3, "edsger")))
        _detect(gh, trigger=trigger)
        assert _filed_state(gh)["failures"] == FAILS


def test_one_job_that_names_a_test_twice_keeps_both_messages() -> None:
    rows = _notes({NODE: "setup failed"}) + _notes(FAILS)
    assert mod.strict_failures([rows]) == {(NODE, "setup failed"), (NODE, MESSAGE)}
    assert mod.strict_failures([list(reversed(rows))]) == mod.strict_failures([rows])


def test_the_cap_of_open_issues_stops_a_run_whatever_it_would_find() -> None:
    held = tuple(
        _suspect_issue(40 + i, {f"test/test_held.py::test_{i}": "x"}) for i in range(mod.MAX_OPEN)
    )
    for order in itertools.permutations(THREE):
        gh = FakeGh(order, issues=held)
        _detect(gh, trigger=order[0]["number"])
        assert sorted(issue["number"] for issue in gh.open_issues()) == [40, 41, 42]


# ── Never a skip ─────────────────────────────────────────────────────────────


def _decide(issues: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    return skip.decide(
        event="pull_request",
        base_ref="main",
        labels=[],
        pr_body="",
        repo=REPO,
        issues=issues,
        changed_files=set(),
    )


def _filed_suspect() -> dict[str, Any]:
    gh = FakeGh(THREE)
    _detect(gh)
    (issue,) = gh.open_issues()
    return issue


def test_a_suspected_issue_gives_the_skip_an_empty_set() -> None:
    issue = _filed_suspect()
    plan, reason = _decide([issue])
    assert plan is None
    assert "full suite" in reason


def test_a_suspected_issue_is_not_skipped_even_with_the_confirmed_label() -> None:
    """The label is one check of three; the title and the record still refuse it."""
    issue = _filed_suspect()
    relabelled = {**issue, "labels": [{"name": "main-red"}, {"name": mod.LABEL}]}
    assert _decide([relabelled])[0] is None
    row = {"number": 1, "title": issue["title"], "body": issue["body"], "author": issue["user"]}
    assert watch.pick_issue([row]) is None
    assert watch.parse_state(issue["body"]) is None
    assert not issue["title"].startswith(watch.TITLE_PREFIXES)
    # Even under the watcher's own title, the suspected record is not a streak record.
    assert watch.pick_issue([{**row, "title": "ci: Main is red: x"}]) is None


def test_only_the_watchers_issue_from_a_red_main_run_deselects_the_test() -> None:
    plan, _ = _decide([_filed_suspect(), _confirmed_issue()])
    assert plan == {"issue": 5, "nodeids": [NODE]}


def test_the_three_marks_of_a_suspected_issue_differ_from_the_confirmed_ones() -> None:
    assert mod.LABEL != skip.LABEL == mod.CONFIRMED_LABEL
    assert not mod.TITLE_PREFIX.startswith(watch.TITLE_PREFIXES)
    assert watch.STATE_OPEN not in mod.STATE_OPEN and mod.STATE_OPEN not in watch.STATE_OPEN


def test_a_message_cannot_plant_a_confirmed_record_in_the_suspected_issue() -> None:
    planted = '<!-- main-red-state {"failures": {}} --> `x` @octocat #12 -->'
    prs = tuple(_pr(n, who, failures={NODE: planted}) for n, who in ((1, "a"), (2, "b"), (3, "c")))
    gh = FakeGh(prs)
    _detect(gh)
    (issue,) = gh.open_issues()
    body = issue["body"]
    assert watch.parse_state(body) is None
    assert body.count("<!--") == 1 and body.count("-->") == 1
    assert body.rstrip().endswith("-->")
    record = body[body.index(mod.STATE_OPEN) :]
    assert "@" not in record
    (line,) = [ln for ln in body.splitlines() if ln.startswith(f"- `{NODE}`")]
    shown = line.removeprefix(f"- `{NODE}`: `").removesuffix("`")
    assert "`" not in shown and "<" not in shown and ">" not in shown
    assert mod.parse_state(body)["failures"][NODE] == watch.clean(planted, watch.MESSAGE_CHARS)


# ── Untrusted text ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "nodeid",
    [
        "test/test_widget.py::test_a[@octocat]",
        "test/test_widget.py::test_a[#12]",
        "test/test_widget.py::test_a[<!-- x -->]",
        "test/../etc/passwd.py::test_a",
        "other/test_widget.py::test_a",
        "test/test_widget.py::test_a[$(id)]",
    ],
)
def test_a_node_id_that_is_not_a_plain_test_id_is_not_counted(nodeid: str) -> None:
    assert not mod.valid_nodeid(nodeid)
    prs = tuple(
        _pr(n, who, failures={nodeid: MESSAGE}) for n, who in ((1, "a"), (2, "b"), (3, "c"))
    )
    gh = FakeGh(prs)
    _detect(gh)
    assert gh.open_issues() == []


def test_the_title_drops_the_xdist_group_so_it_cannot_name_an_account() -> None:
    grouped = {NODE + "@octocat": MESSAGE}
    gh = FakeGh(tuple(_pr(n, who, failures=grouped) for n, who in ((1, "a"), (2, "b"), (3, "c"))))
    _detect(gh)
    (issue,) = gh.open_issues()
    assert issue["title"] == mod.TITLE_PREFIX + NODE
    outside_spans = "".join(issue["body"].split("`")[0::2])
    assert "@" not in outside_spans


def test_no_account_name_is_written_anywhere() -> None:
    gh = FakeGh((_pr(1, "ada"), _pr(2, "grace", actor="hopper"), _pr(3, "edsger")))
    api = _detect(gh)
    (issue,) = gh.open_issues()
    written = issue["title"] + issue["body"] + "\n".join(api.lines)
    for name in ("ada", "grace", "hopper", "edsger"):
        assert name not in written


@pytest.mark.parametrize(
    "change",
    [
        {"base": "not-a-sha"},
        {"prs": [1, 2]},
        {"runs": [11, 12, "13"]},
        {"failures": {}},
        {"failures": {"test/t.py::a[@x]": "m"}},
        {"failures": {f"test/t.py::t{i}": "m" for i in range(mod.MAX_NODEIDS + 1)}},
        {"failures": ["test/t.py::a"]},
    ],
)
def test_an_edited_record_with_an_odd_field_is_not_a_suspected_issue(change: dict) -> None:
    good = {"failures": FAILS, "base": BASE, "prs": [1, 2, 3], "runs": [11, 12, 13]}
    assert mod.parse_state(f"<!-- {mod.STATE_MARKER} {json.dumps(good)} -->") == good
    body = f"<!-- {mod.STATE_MARKER} {json.dumps({**good, **change})} -->"
    assert mod.parse_state(body) is None


@pytest.mark.parametrize(
    "over",
    [
        {"user": {"login": "mallory"}},
        {"title": "Main may be red: x"},
        {"body": "no record"},
        {"labels": [{"name": "bug"}]},
        {"state": "closed"},
        {"pull_request": {"url": "x"}},
    ],
)
def test_an_issue_this_workflow_did_not_file_is_never_a_suspected_issue(over: dict) -> None:
    assert mod.suspects_in([[_suspect_issue()]]) != []
    assert mod.suspects_in([[_suspect_issue(**over)]]) == []


# ── One issue, bounded reads ─────────────────────────────────────────────────


def test_a_second_failed_run_files_no_second_issue() -> None:
    gh = FakeGh(THREE)
    _detect(gh, trigger=1)
    _detect(gh, trigger=2)
    _detect(gh, trigger=3)
    assert len(gh.open_issues()) == 1
    assert gh.writes().count("POST repos/example/repo/issues") == 1


def test_an_issue_filed_between_the_reads_and_the_write_stops_the_write() -> None:
    class Racing(FakeGh):
        def _read(self, path: str) -> tuple[int, str, str]:
            listed = len(self.reads(f"issues?state=open&labels={mod.LABEL}"))
            if path.startswith("issues?") and listed == 2 and not self.issues:
                self.issues.append(_suspect_issue())
            return super()._read(path)

    gh = Racing(THREE)
    _detect(gh)
    assert [issue["number"] for issue in gh.open_issues()] == [40]
    assert gh.writes() == []


def test_an_open_main_is_red_issue_stops_the_run_after_two_reads() -> None:
    gh = FakeGh(THREE, issues=(_confirmed_issue(failures={"test/test_else.py::t": "x"}),))
    api = _detect(gh)
    assert api.calls <= 2
    assert gh.writes() == []


def test_no_more_than_the_cap_of_suspected_issues_is_open_at_once() -> None:
    held = tuple(
        _suspect_issue(40 + i, {f"test/test_held.py::test_{i}": "x"}) for i in range(mod.MAX_OPEN)
    )
    gh = FakeGh(THREE, issues=held)
    api = _detect(gh)
    assert len(gh.open_issues()) == mod.MAX_OPEN
    assert api.calls <= 2


def test_a_test_that_is_already_suspected_costs_no_read_of_another_run() -> None:
    gh = FakeGh(THREE, issues=(_suspect_issue(),))
    _detect(gh)
    assert gh.reads("actions/workflows/") == []
    assert gh.writes() == []


def test_a_run_that_names_no_test_reads_no_other_run() -> None:
    gh = FakeGh((_pr(1, "ada", notes=[]), _pr(2, "grace"), _pr(3, "edsger")))
    api = _detect(gh)
    assert gh.reads("actions/workflows/") == [] and gh.reads("pulls/") == []
    assert api.calls <= 4


def test_a_token_near_its_rate_limit_reads_no_other_run() -> None:
    gh = FakeGh(THREE, remaining=mod.RATE_FLOOR - 1)
    api = _detect(gh)
    assert gh.reads("actions/workflows/") == []
    assert gh.open_issues() == []
    assert any(f"under {mod.RATE_FLOOR}" in line for line in api.lines)


def test_only_the_newest_runs_are_read_and_only_their_failed_backend_jobs() -> None:
    others = tuple(
        _pr(n, f"author-{n}", failures={f"test/test_else.py::test_{n}": "x"}) for n in range(2, 30)
    )
    gh = FakeGh((_pr(1, "ada"), *others))
    api = _detect(gh)
    read = sorted(int(call.split("/")[2]) for call in gh.reads("GET actions/runs/"))
    assert read == [9001] + list(range(9029 - mod.MAX_CANDIDATES + 1, 9030))
    assert all(int(call.split("/")[1]) < 200 for call in gh.reads("GET check-runs/"))
    # A run that is in no group of 3 costs its jobs and one annotation set only.
    assert gh.reads("pulls/") == [] and gh.reads("compare/") == []
    assert api.calls <= 2 + 2 + 1 + 1 + 2 * mod.MAX_CANDIDATES


def test_a_run_beyond_the_newest_ten_is_not_read_so_its_evidence_waits() -> None:
    """The stated limit of re-reading: what is older than the newest 10 is not counted."""
    newer = tuple(
        _pr(n, f"author-{n}", failures={f"test/test_else.py::test_{n}": "x"}) for n in range(20, 31)
    )
    gh = FakeGh((_pr(1, "ada"), _pr(2, "grace"), _pr(3, "edsger"), *newer))
    _detect(gh, trigger=30)
    assert gh.open_issues() == []
    assert gh.reads("actions/runs/9001") == []


def test_the_newest_failed_run_of_each_pull_request_is_the_one_counted() -> None:
    row = {
        "event": "pull_request",
        "conclusion": "failure",
        "head_sha": _head(2),
        "head_repository": {"full_name": REPO},
        "actor": {"login": "grace"},
        "pull_requests": [{"number": 2, "base": {"ref": "main", "sha": BASE}}],
    }
    trigger = mod.Run(9001, 1, _head(1), BASE, "ada")
    pages = [{"workflow_runs": [{**row, "id": 50}, {**row, "id": 70}, {**row, "id": 60}]}]
    assert [run.run_id for run in mod.candidate_runs(pages, REPO, trigger)] == [70]


@pytest.mark.parametrize(
    "change",
    [
        {"event": "push"},
        {"conclusion": "success"},
        {"conclusion": "cancelled"},
        {"head_repository": {"full_name": "mallory/repo"}},
        {"head_repository": None},
        {"pull_requests": []},
        {"pull_requests": [{"number": 2, "base": {"ref": "release/1", "sha": BASE}}]},
        {"pull_requests": [{"number": 2, "base": {"ref": "main", "sha": "zz"}}]},
        {"head_sha": "$(id)"},
        {"actor": {"login": "a b"}},
        {"actor": None},
        {"id": "70"},
    ],
)
def test_a_run_row_that_is_not_a_failed_same_repository_pull_request_run_is_dropped(
    change: dict,
) -> None:
    row = {
        "id": 70,
        "event": "pull_request",
        "conclusion": "failure",
        "head_sha": _head(2),
        "head_repository": {"full_name": REPO},
        "actor": {"login": "grace"},
        "pull_requests": [{"number": 2, "base": {"ref": "main", "sha": BASE}}],
    }
    assert mod.run_of(row, REPO) == mod.Run(70, 2, _head(2), BASE, "grace")
    assert mod.run_of({**row, **change}, REPO) is None


def test_a_changed_file_list_github_cut_short_is_unknown() -> None:
    files = [{"filename": f"src/f{i}.py"} for i in range(mod.MAX_CHANGED_FILES)]
    assert mod.changed_files([{"files": files}]) is None
    assert mod.changed_files([{"files": files[:-1]}]) == {row["filename"] for row in files[:-1]}
    renamed = [{"filename": "test/new.py", "previous_filename": "test/old.py"}]
    assert mod.changed_files([{"files": renamed}]) == {"test/new.py", "test/old.py"}
    assert mod.changed_files([{"status": "ahead"}]) is None


@pytest.mark.parametrize(
    "fragment",
    ["issues?state=open", "issues?state=closed", "actions/runs/9001", "actions/workflows/"],
)
def test_a_read_that_fails_opens_nothing_and_says_why(fragment: str) -> None:
    gh = FakeGh(THREE, refuse=(fragment,))
    api = _detect(gh)
    assert gh.writes() == []
    assert any("Resource not accessible by integration" in line for line in api.lines)


def test_an_issue_filed_without_its_label_is_closed_so_no_run_files_another() -> None:
    gh = FakeGh(THREE, drop_labels=True)
    _detect(gh)
    assert gh.open_issues() == []
    assert len(gh.issues) == 1


# ── Settling ─────────────────────────────────────────────────────────────────


def test_a_red_main_run_whose_issue_names_the_test_closes_the_suspected_issue() -> None:
    gh = FakeGh(issues=(_suspect_issue(), _confirmed_issue()))
    _settle(gh, "failure")
    assert [issue["number"] for issue in gh.open_issues()] == [5]
    assert "#5 tracks it" in gh.comments[40]
    (closed,) = [issue for issue in gh.issues if issue["number"] == 40]
    assert closed["state_reason"] == "completed"


def test_the_group_suffix_does_not_hide_a_confirmed_test() -> None:
    confirmed = _confirmed_issue(failures={NODE + "@some_group": MESSAGE})
    gh = FakeGh(issues=(_suspect_issue(), confirmed))
    _settle(gh, "timed_out")
    assert [issue["number"] for issue in gh.open_issues()] == [5]


def test_a_red_main_run_on_another_test_leaves_the_suspected_issue_open() -> None:
    confirmed = _confirmed_issue(failures={"test/test_else.py::t": "x"})
    gh = FakeGh(issues=(_suspect_issue(), confirmed))
    _settle(gh, "failure")
    assert [issue["number"] for issue in gh.open_issues()] == [40, 5]
    assert gh.writes() == []


def test_a_red_main_run_with_no_main_is_red_issue_leaves_the_suspected_issue_open() -> None:
    gh = FakeGh(issues=(_suspect_issue(),))
    _settle(gh, "failure")
    assert [issue["number"] for issue in gh.open_issues()] == [40]


def test_a_failed_pull_request_run_also_closes_a_suspected_issue_main_confirmed() -> None:
    gh = FakeGh(THREE, issues=(_suspect_issue(), _confirmed_issue()))
    _detect(gh)
    assert [issue["number"] for issue in gh.open_issues()] == [5]


def test_a_green_main_run_that_contains_the_base_closes_it_as_not_confirmed() -> None:
    gh = FakeGh(
        issues=(_suspect_issue(),),
        main_jobs=FULL_GREEN,
        relations={f"{BASE}...{MAIN_HEAD}": "ahead"},
    )
    _settle(gh, "success")
    assert gh.open_issues() == []
    assert gh.comments[40].startswith("Main was green: the `CI` push run for `cccccccccccc`")
    assert "https://github.com/example/repo/actions/runs/900" in gh.comments[40]
    assert gh.issues[0]["state_reason"] == "not_planned"


def test_a_green_run_of_the_base_commit_itself_closes_it() -> None:
    gh = FakeGh(
        issues=(_suspect_issue(),),
        main_jobs=FULL_GREEN,
        relations={f"{BASE}...{MAIN_HEAD}": "identical"},
    )
    _settle(gh, "success")
    assert gh.open_issues() == []


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_green_main_run_that_does_not_contain_the_base_closes_nothing(status: str) -> None:
    gh = FakeGh(
        issues=(_suspect_issue(),),
        main_jobs=FULL_GREEN,
        relations={f"{BASE}...{MAIN_HEAD}": status},
    )
    _settle(gh, "success")
    assert len(gh.open_issues()) == 1
    assert gh.writes() == []


@pytest.mark.parametrize("jobs", [REDUCED_GREEN, [{"name": "Lint", "steps": []}], []])
def test_a_green_run_that_did_not_run_the_full_backend_suite_closes_nothing(jobs: list) -> None:
    gh = FakeGh(
        issues=(_suspect_issue(),), main_jobs=jobs, relations={f"{BASE}...{MAIN_HEAD}": "ahead"}
    )
    _settle(gh, "success")
    assert len(gh.open_issues()) == 1
    assert gh.reads("compare/") == []


def test_a_green_main_run_never_closes_the_watchers_issue() -> None:
    gh = FakeGh(
        issues=(_suspect_issue(), _confirmed_issue()),
        main_jobs=FULL_GREEN,
        relations={f"{BASE}...{MAIN_HEAD}": "ahead"},
    )
    _settle(gh, "success")
    assert [issue["number"] for issue in gh.open_issues()] == [5]


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_a_main_run_with_no_suspected_issue_open_costs_one_read(conclusion: str) -> None:
    gh = FakeGh(issues=(_confirmed_issue(),), main_jobs=FULL_GREEN)
    api = _settle(gh, conclusion)
    assert api.calls <= 1
    assert gh.writes() == []


# ── A test main ran green is not suspected again at once ─────────────────────


def _suspected_then_green() -> FakeGh:
    """Three pull requests agree, an issue is filed, and a green main run closes it."""
    gh = FakeGh(THREE, main_jobs=FULL_GREEN, relations={f"{BASE}...{MAIN_HEAD}": "ahead"})
    _detect(gh)
    _settle(gh, "success")
    assert gh.open_issues() == [] and gh.issues[0]["state_reason"] == "not_planned"
    return gh


def test_a_test_main_just_ran_green_is_not_suspected_again() -> None:
    gh = _suspected_then_green()
    for trigger in (1, 2, 3):
        api = _detect(gh, trigger=trigger)
        assert gh.open_issues() == []
        assert gh.reads("actions/workflows/")[1:] == [], "no other run is read for it"
        assert any("not suspecting it again yet" in line for line in api.lines)
    assert gh.writes().count("POST repos/example/repo/issues") == 1


def test_another_test_is_still_suspected_while_the_first_cools() -> None:
    gh = _suspected_then_green()
    other = {"test/test_other.py::test_b": "boom"}
    for number, author in ((11, "barbara"), (12, "hopper"), (13, "lovelace")):
        gh.prs[number] = _pr(number, author, failures={**FAILS, **other})
    _detect(gh, trigger=11)
    (issue,) = gh.open_issues()
    assert mod.parse_state(issue["body"])["failures"] == other


def test_after_the_cool_down_the_test_is_suspected_again() -> None:
    gh = _suspected_then_green()
    _detect(gh, cool_since="2026-11-20T02:00:01Z")
    assert len(gh.open_issues()) == 1
    gh = _suspected_then_green()
    _detect(gh, cool_since=CLOSED_AT)
    assert gh.open_issues() == [], "an issue closed exactly at the stamp still cools"


def test_an_issue_that_main_confirmed_does_not_cool_its_test() -> None:
    """Closed because main was red on it: when that break ends and comes back, it is news."""
    gh = FakeGh(THREE, issues=(_suspect_issue(), _confirmed_issue()))
    _settle(gh, "failure")
    for issue in gh.issues:
        issue["state"] = "closed"
        issue.setdefault("state_reason", "completed")
        issue["closed_at"] = CLOSED_AT
    _detect(gh)
    assert len(gh.open_issues()) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"user": {"login": "mallory"}},
        {"title": "Main may be red: x"},
        {"body": "no record"},
        {"labels": [{"name": "bug"}]},
        {"state_reason": "completed"},
        {"closed_at": None},
        {"closed_at": "2026-11-19T02:59:59Z"},
    ],
)
def test_only_an_issue_this_workflow_filed_and_main_closed_green_cools_a_test(over: dict) -> None:
    closed = {"state": "closed", "state_reason": "not_planned", "closed_at": CLOSED_AT}
    assert mod.cooling_in([[_suspect_issue(**closed)]], COOL_SINCE) == {NODE}
    assert mod.cooling_in([[_suspect_issue(**{**closed, **over})]], COOL_SINCE) == set()


# ── The command line ─────────────────────────────────────────────────────────

EVENT = {
    "REPO": REPO,
    "RUN_ID": "9001",
    "WORKFLOW_ID": str(WORKFLOW_ID),
    "HEAD_SHA": _head(1),
    "CONCLUSION": "failure",
    "ACTOR": "Ada",
    "PR_NUMBER": "1",
    "BASE_SHA": BASE,
}


def test_the_detect_command_reads_the_event_from_the_environment() -> None:
    gh = FakeGh(THREE)
    assert mod.main(["detect"], env=dict(EVENT), gh=gh) == 0
    assert len(gh.open_issues()) == 1
    (listing,) = gh.reads("actions/workflows/7/runs")
    assert "event=pull_request&status=failure&created=%3E%3D" in listing
    (closed,) = gh.reads("issues?state=closed")
    assert f"labels={mod.LABEL}&since=" in closed


def test_the_settle_command_reads_the_event_from_the_environment(tmp_path: Path) -> None:
    gh = FakeGh(issues=(_suspect_issue(), _confirmed_issue()))
    summary = tmp_path / "summary.md"
    env = {**EVENT, "RUN_ID": "900", "HEAD_SHA": MAIN_HEAD, "GITHUB_STEP_SUMMARY": str(summary)}
    assert mod.main(["settle"], env=env, gh=gh) == 0
    assert [issue["number"] for issue in gh.open_issues()] == [5]
    assert "Closed suspected issue #40." in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "change",
    [
        {"REPO": "example/repo/../x"},
        {"HEAD_SHA": "main"},
        {"RUN_ID": "9001; id"},
        {"PR_NUMBER": ""},
        {"BASE_SHA": "$(id)"},
        {"WORKFLOW_ID": "ci.yml"},
        {"ACTOR": "a b"},
    ],
)
def test_an_event_value_of_an_odd_shape_makes_no_api_call(change: dict[str, str]) -> None:
    gh = FakeGh(THREE)
    assert mod.main(["detect"], env={**EVENT, **change}, gh=gh) == 0
    assert gh.calls == []


# ── Wiring and least privilege ───────────────────────────────────────────────
#
# These read the workflow as it is and assert what it must guarantee, so a
# change that keeps the guarantee (another checked-out path, another read scope,
# a reworded expression) passes and one that breaks it does not.


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job() -> dict:
    (job,) = _load(WORKFLOW)["jobs"].values()
    return job


def _script_steps() -> list[dict]:
    return [s for s in _job()["steps"] if SCRIPT.name in str(s.get("run", ""))]


_CONTEXT = re.compile(r"\bgithub(?:\.[A-Za-z_]+|\[\d+\])+")


def _value(node: ast.AST, event: dict[str, Any]) -> Any:
    """One node of a workflow expression, with GitHub's `&&` / `||` value rules."""
    if isinstance(node, ast.BoolOp):
        result: Any = None
        for part in node.values:
            result = _value(part, event)
            if bool(result) != isinstance(node.op, ast.And):
                break
        return result
    if isinstance(node, ast.Compare):
        (op,), (right,) = node.ops, node.comparators
        same = _value(node.left, event) == _value(right, event)
        return same if isinstance(op, ast.Eq) else not same
    if isinstance(node, ast.Constant):
        return node.value
    assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name), ast.dump(node)
    args = [_value(arg, event) for arg in node.args]
    if node.func.id == "format":
        return str(args[0]).replace("{0}", str(args[1]))
    assert node.func.id == "context", node.func.id
    found: Any = event
    for key in re.findall(r"[A-Za-z_]+|\d+", args[0]):
        if key.isdigit():
            found = found[int(key)] if isinstance(found, list) and len(found) > int(key) else None
        else:
            found = found.get(key) if isinstance(found, dict) else None
    return found


def _evaluate(expression: str, event: dict[str, Any]) -> Any:
    """The value GitHub gives a `${{ }}` expression (or a bare `if:`) for ``event``."""
    text = " ".join(str(expression).split()).removeprefix("${{").removesuffix("}}")
    text = _CONTEXT.sub(lambda match: f"context({match.group(0)!r})", text)
    text = text.replace("&&", " and ").replace("||", " or ")
    return _value(ast.parse(text.strip(), mode="eval").body, event)


def _event(
    *,
    event: str = "pull_request",
    conclusion: str = "failure",
    head_repo: str = REPO,
    head_branch: str = "feature",
    base_ref: str | None = "main",
    run_id: int = 1,
) -> dict[str, Any]:
    pulls = [] if base_ref is None else [{"number": 7, "base": {"ref": base_ref, "sha": BASE}}]
    run = {
        "event": event,
        "conclusion": conclusion,
        "head_branch": head_branch,
        "head_repository": {"full_name": head_repo},
        "pull_requests": pulls,
        "actor": {"login": "ada"},
    }
    return {"github": {"repository": REPO, "run_id": run_id, "event": {"workflow_run": run}}}


def _every_event() -> list[dict[str, Any]]:
    return [
        _event(event=e, conclusion=c, head_repo=r, head_branch=b, base_ref=base)
        for e in ("pull_request", "push", "merge_group", "schedule")
        for c in ("failure", "success", "timed_out", "cancelled", "skipped", "action_required")
        for r in (REPO, "mallory/repo")
        for b in ("main", "feature", "release/1")
        for base in ("main", "release/1", None)
    ]


def _must_take(event: dict[str, Any]) -> str | None:
    """Which command a completed `CI` run is for, or ``None`` when the job must skip it."""
    run = event["github"]["event"]["workflow_run"]
    if run["head_repository"]["full_name"] != REPO:
        return None
    pulls = run["pull_requests"]
    if run["event"] == "pull_request":
        to_main = bool(pulls) and pulls[0]["base"]["ref"] == "main"
        return "detect" if run["conclusion"] == "failure" and to_main else None
    if run["event"] == "push" and run["head_branch"] == "main":
        return "settle" if run["conclusion"] in ("failure", "timed_out", "success") else None
    return None


def test_the_only_trigger_is_a_completed_run_of_ci() -> None:
    document = _load(WORKFLOW)
    triggers = document.get("on") or document.get(True)
    assert set(triggers) == {"workflow_run"}, "no trigger that could run a pull request's code"
    assert triggers["workflow_run"]["workflows"] == [_load(WORKFLOWS / "ci.yml")["name"]]
    assert set(triggers["workflow_run"]["types"]) == {"completed"}


def test_the_job_takes_failed_same_repository_pull_request_runs_and_main_push_runs() -> None:
    taken = set()
    for event in _every_event():
        expected = _must_take(event)
        assert bool(_evaluate(_job()["if"], event)) == (expected is not None), event
        taken.add(expected)
    assert taken == {"detect", "settle", None}


def test_a_forks_run_is_never_taken_whatever_else_it_says() -> None:
    forks = [
        event
        for event in _every_event()
        if event["github"]["event"]["workflow_run"]["head_repository"]["full_name"] != REPO
    ]
    assert forks and not any(_evaluate(_job()["if"], event) for event in forks)


def test_the_command_is_detect_for_a_pull_request_run_and_settle_for_a_push_run() -> None:
    (step,) = _script_steps()
    for event in _every_event():
        expected = _must_take(event)
        if expected is not None:
            assert _evaluate(step["env"]["COMMAND"], event) == expected


def test_only_a_run_the_job_takes_enters_a_shared_concurrency_group() -> None:
    """GitHub keeps one queued run per group, so a run the job skips must not replace one."""
    concurrency = _load(WORKFLOW)["concurrency"]
    assert concurrency["cancel-in-progress"] is False, "a running job is never killed"
    shared: dict[str, set[str]] = {"detect": set(), "settle": set()}
    for event in _every_event():
        expected = _must_take(event)
        group = _evaluate(concurrency["group"], event)
        other = _evaluate(concurrency["group"], _with_run_id(event, 2))
        if expected is None:
            assert group != other, f"a skipped run shares a group: {group}"
        else:
            assert group == other, "a taken run queues behind the others of its kind"
            shared[expected].add(group)
    assert len(shared["detect"]) == 1 and len(shared["settle"]) == 1
    assert shared["detect"] != shared["settle"], "filing and closing do not replace each other"


def _with_run_id(event: dict[str, Any], run_id: int) -> dict[str, Any]:
    return {"github": {**event["github"], "run_id": run_id}}


def test_the_token_can_write_issues_and_nothing_else() -> None:
    scopes = _load(WORKFLOW)["permissions"]
    assert isinstance(scopes, dict), "a bare `read-all` or `write-all` is not least privilege"
    assert {name for name, level in scopes.items() if level == "write"} == {"issues"}
    assert set(scopes.values()) <= {"read", "write"}
    # What the script reads: runs and jobs, annotations, compares, pull requests.
    assert {"actions", "checks", "contents", "pull-requests"} <= set(scopes)
    assert "permissions" not in _job(), "the job does not widen the workflow's scopes"


def _loaded_from_beside(script: Path) -> set[Path]:
    """``script`` and every file it, or one of those, loads from its own directory."""
    seen: set[Path] = set()
    todo = [script]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8")
        todo += [path.with_name(name) for name in re.findall(r'with_name\("([^"]+)"\)', text)]
    return seen


def test_the_checkout_is_mains_own_holds_no_credentials_and_has_what_the_steps_run() -> None:
    job = _job()
    assert job["continue-on-error"] is True, "it cannot fail a job"
    (checkout,) = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    pin = checkout["uses"].split("@")[1]
    assert re.fullmatch(r"[0-9a-f]{40}", pin), "the action is pinned to a commit"
    options = checkout["with"]
    assert options["persist-credentials"] is False
    # `workflow_run` checks out the default branch unless told otherwise.
    assert not {"ref", "repository"} & set(options), "never a pull request's code"
    paths = [path.rstrip("/") for path in str(options["sparse-checkout"]).split()]
    assert paths

    needed = {path.relative_to(ROOT).as_posix() for path in _loaded_from_beside(SCRIPT)}
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        if uses.startswith("./"):
            needed.add(uses.removeprefix("./").rstrip("/") + "/action.yml")
    assert SCRIPT.relative_to(ROOT).as_posix() in needed and len(needed) > 1
    for name in sorted(needed):
        assert (ROOT / name).is_file(), name
        assert any(
            name == path or name.startswith(path + "/") for path in paths
        ), f"the sparse checkout does not fetch {name}"


def test_nothing_from_the_event_is_in_the_script_text() -> None:
    for step in _job()["steps"]:
        assert "${{" not in str(step.get("run", "")), "event values reach a step through env"
    (step,) = _script_steps()
    assert SCRIPT.relative_to(ROOT).as_posix() in step["run"]
    env = step["env"]
    assert set(EVENT) <= set(env), "every value the script reads from the event is passed"
    # The account and the base are the completed run's own, not this workflow's.
    assert "github.event.workflow_run.actor.login" in env["ACTOR"]
    assert "github.event.workflow_run.pull_requests[0].base.sha" in env["BASE_SHA"]
    assert "github.event.workflow_run.head_sha" in env["HEAD_SHA"]


def test_the_rate_limit_wrapper_is_installed_before_the_script_runs() -> None:
    steps = _job()["steps"]
    wrappers = [i for i, s in enumerate(steps) if "gh-rate-limit" in str(s.get("uses", ""))]
    assert wrappers and wrappers[0] < steps.index(_script_steps()[0])
    waits = int(steps[wrappers[0]]["with"]["max-wait"])
    assert waits < _job()["timeout-minutes"] * 60 // 2, "the wait leaves room for the work"


def test_the_green_proof_names_are_the_watchers() -> None:
    envs = [
        step.get("env") or {}
        for job in _load(WORKFLOWS / "main-red-watch.yml")["jobs"].values()
        for step in job["steps"]
    ]
    assert mod.BACKEND_JOB_PREFIX in {env.get("PROOF_JOB_PREFIX") for env in envs}
    assert mod.PROOF_STEP_NAME in {env.get("PROOF_STEP_NAME") for env in envs}


def test_the_node_id_shape_is_the_one_the_skip_accepts() -> None:
    assert mod._NODEID.pattern == skip._NODEID.pattern
