"""A recording fake of `gh api` and the rows the main-red suspect tests share.

`test_main_red_suspect.py` and `test_main_red_suspect_notice.py` run
`.github/scripts/main_red_suspect.py` against this fake: it answers the reads the
script makes from canned pull requests and issues, records every call, and
keeps what is written, so a second run sees what the first one wrote. No test
starts a process or reads a clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        cross_references: bool = True,
    ) -> None:
        self.prs = {pr["number"]: pr for pr in prs}
        self.issues = [dict(issue) for issue in issues]
        self.remaining = remaining
        self.refuse = refuse
        self.relations = relations or {}
        self.main_jobs = main_jobs or []
        self.drop_labels = drop_labels
        self.cross_references = cross_references
        self.calls: list[str] = []
        self.comments: dict[int, str] = {}
        self.threads: dict[int, list[dict[str, Any]]] = {}

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
            self.threads.setdefault(number, []).append({"user": BOT, "body": fields["body"]})
            return 0, json.dumps({"id": 1}), ""
        assert method == "PATCH" and fields["state"] == "closed"
        for issue in self.issues:
            if issue["number"] == number:
                issue["state"] = "closed"
                issue["state_reason"] = fields["state_reason"]
                issue["closed_at"] = CLOSED_AT
        return 0, json.dumps({"number": number}), ""

    def _timeline(self, issue: int) -> list[dict[str, Any]]:
        """The cross-references GitHub adds when a comment on a pull request names the issue."""
        if not self.cross_references:
            return []
        events: list[dict[str, Any]] = [{"event": "labeled", "actor": BOT}]
        for number, rows in sorted(self.threads.items()):
            for row in rows:
                if f"#{issue}," in row["body"] or f"#{issue} " in row["body"]:
                    url = f"https://github.com/{REPO}/pull/{number}"
                    found = {"number": number, "pull_request": {}, "html_url": url}
                    source = {"type": "issue", "issue": found}
                    events.append(
                        {"event": "cross-referenced", "actor": row["user"], "source": source}
                    )
        return events

    def _read(self, path: str) -> tuple[int, str, str]:
        if path.startswith("issues/"):
            number = int(path.split("/")[1])
            if path.split("/")[2].startswith("timeline?"):
                return 0, json.dumps(self._timeline(number)), ""
            assert path.split("/")[2].startswith("comments?")
            return 0, json.dumps(self.threads.get(number, [])), ""
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


def _suspected_then_green() -> FakeGh:
    """Three pull requests agree, an issue is filed, and a green main run closes it."""
    gh = FakeGh(THREE, main_jobs=FULL_GREEN, relations={f"{BASE}...{MAIN_HEAD}": "ahead"})
    _detect(gh)
    _settle(gh, "success")
    assert gh.open_issues() == [] and gh.issues[0]["state_reason"] == "not_planned"
    return gh
