"""Contract tests for .github/workflows/main-red-pr-notice.yml and main_red_pr_notice.py.

``main-red-watch.yml`` files one ``Main is red:`` issue with the failing test ids.
This notice tells the author of a pull request, on the pull request, which of their
failing tests are red on main. Four properties are pinned:

* CLASSIFYING -- a failure is "same as main" only when its node id is in the issue's
  set, its message start equals main's, the pull request does not change its test
  file and its base contains the issue's last-red-run commit; everything else is the author's.
* TEXT -- the comment carries a hidden marker, no ``@`` and no backtick from log text,
  and never says the pull request is fine.
* GATING -- the watched name is ``ci.yml``'s real ``name:``, the job takes failed
  same-repository pull request runs that target main, the permissions equal the
  watcher's, and the job cannot fail (``continue-on-error``).
* WRITING -- the step runs with ``gh`` stubbed: one comment, edited in place, only
  when a failing test is red on main, and nothing else is written.

Skipped where the POSIX toolchain the step needs is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
NOTICE = WORKFLOWS / "main-red-pr-notice.yml"
WATCHER = WORKFLOWS / "main-red-watch.yml"
SCRIPTS = ROOT / ".github" / "scripts"

watch = load_skill_script("main_red_nodeids_for_notice_test", SCRIPTS / "main_red_nodeids.py")
mod = load_skill_script("main_red_pr_notice_under_test", SCRIPTS / "main_red_pr_notice.py")

RUN_URL = "https://github.com/example/repo/actions/runs/1000"
HEAD = "1" * 40
BASE = "b" * 40
FIRST = "a" * 40
LAST = "c" * 40
MAIN = {"test/test_a.py::test_x": "assert 2 == 1"}

# ── Classifying ──────────────────────────────────────────────────────────────


def _classify(pr: dict, *, changed: set | None = frozenset(), base_ok: bool = True) -> tuple:
    return mod.classify(pr, MAIN, None if changed is None else set(changed), base_ok)


def test_a_failure_with_the_same_id_and_message_start_is_the_same_as_main() -> None:
    same, own = _classify({"test/test_a.py::test_x": "assert 2 == 1", "test/test_b.py::t": "no"})
    assert same == MAIN
    assert own == {"test/test_b.py::t": "no"}


@pytest.mark.parametrize(
    "pr, kwargs",
    [
        pytest.param({"test/test_a.py::test_x": "KeyError: 'b'"}, {}, id="different-message"),
        pytest.param(
            {"test/test_a.py::test_x": "assert 2 == 1"},
            {"changed": {"test/test_a.py"}},
            id="pr-edits-the-test-file",
        ),
        pytest.param(
            {"test/test_a.py::test_x": "assert 2 == 1"},
            {"changed": None},
            id="changed-files-unknown",
        ),
        pytest.param(
            {"test/test_a.py::test_x": "assert 2 == 1"},
            {"base_ok": False},
            id="base-lacks-last-red",
        ),
        pytest.param(
            {"test/test_a.py::test_other": "assert 2 == 1"}, {}, id="other-test-same-file"
        ),
    ],
)
def test_any_failed_rule_makes_the_failure_the_authors(pr: dict, kwargs: dict) -> None:
    same, own = _classify(pr, **kwargs)
    assert same == {} and own == pr


def test_a_change_to_another_test_file_does_not_stop_the_match() -> None:
    same, _ = _classify({"test/test_a.py::test_x": "assert 2 == 1"}, changed={"src/x.py"})
    assert same == MAIN


# ── The comment ──────────────────────────────────────────────────────────────


def _issue_body(prs: list | None = None, failures: dict | None = None) -> str:
    state = watch.next_state(None, failures or MAIN, FIRST, 900, RUN_URL)
    return watch.render_body(state, prs or [], 5)


PR_URL = "https://github.com/example/repo/pull/23933"


def _row(**over: Any) -> dict[str, Any]:
    row = {"url": PR_URL, "state": "open", "author": "buluoray", "created": "2026-10-26T07:00:32Z"}
    return {**row, **over}


def test_the_fix_line_is_rebuilt_from_the_timeline_without_a_mention() -> None:
    line = mod.fix_line([_row()], "example/repo", 5)
    assert line == "Fix in progress: #23933 (buluoray, opened 2026-10-26 07:00Z)"
    assert mod.fix_line([_row(state="closed")], "example/repo", 5).startswith(
        "Fix in progress: none yet"
    )
    assert mod.fix_line(None, "example/repo", 5) == "Fix in progress: unknown."


def test_an_edited_fix_line_in_the_issue_body_is_never_read() -> None:
    body = _issue_body() + "\nFix in progress: [fixed here](http://evil.example) @octocat\n"
    assert "evil" not in mod.fix_line([], "example/repo", 5)
    assert mod.watch.parse_state(body) is not None


def test_event_values_are_printed_only_when_they_match() -> None:
    assert mod.run_link(HEAD, RUN_URL) == f"[{HEAD[:12]}]({RUN_URL})"
    assert mod.run_link("not a sha", RUN_URL) == f"[this run]({RUN_URL})"
    assert mod.run_link(HEAD, "http://evil.example/x)") == HEAD[:12]
    assert mod.run_link("x) [y](http://evil.example", "javascript:alert(1)") == "this run"


@pytest.mark.timeout(20)
def test_a_hostile_issue_body_is_read_in_linear_time() -> None:
    body = "Fix in progress: " * 20000 + "<!-- main-red-state " + "{" * 50000
    assert mod.watch.parse_state(body) is None
    assert mod.watch.pick_issue([{"number": 1, "title": "Main is red: x", "body": body}]) is None


def test_the_comment_has_the_marker_both_lists_and_never_excuses_the_pull_request() -> None:
    text = mod.render_comment(
        MAIN, {"test/test_b.py::t": "boom"}, MAIN, 5, "Fix in progress: none yet.", HEAD, RUN_URL
    )
    assert text.startswith("<!-- main-red-pr-notice -->")
    assert "tracked in #5" in text and "Fix in progress: none yet" in text
    assert "- `test/test_a.py::test_x`: `assert 2 == 1`" in text
    assert "- `test/test_b.py::t`: `boom`" in text
    assert f"[{HEAD[:12]}]({RUN_URL})" in text
    assert "always yours" in text and "changes no check result" in text
    for phrase in ("is fine", "safe to merge", "not your"):
        assert phrase not in text


def test_a_long_list_is_cut() -> None:
    many = {f"test/t.py::t{i}": "" for i in range(30)}
    text = mod.render_comment({}, many, MAIN, 5, "Fix in progress: none yet.", HEAD, RUN_URL)
    assert "- and 10 more" in text and text.count("\n- `") == 20


# ── Wiring and least privilege ───────────────────────────────────────────────


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job() -> dict:
    (job,) = _load(NOTICE)["jobs"].values()
    return job


def _step() -> dict:
    (step,) = [s for s in _job()["steps"] if "run" in s]
    return step


def test_the_watched_name_is_the_real_name_of_ci_yml() -> None:
    document = _load(NOTICE)
    triggers = document.get("on") or document.get(True)
    assert triggers["workflow_run"]["workflows"] == [_load(WORKFLOWS / "ci.yml")["name"]]
    assert triggers["workflow_run"]["types"] == ["completed"]


def test_the_job_takes_failed_same_repository_pull_request_runs_that_target_main() -> None:
    condition = " ".join(str(_job()["if"]).split())
    assert "github.event.workflow_run.event == 'pull_request'" in condition
    assert "github.event.workflow_run.conclusion == 'failure'" in condition
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in condition
    assert "github.event.workflow_run.pull_requests[0].base.ref == 'main'" in condition
    assert "push" not in condition and "cancelled" not in condition


def test_the_permissions_equal_the_watchers_and_the_job_cannot_fail() -> None:
    assert _load(NOTICE)["permissions"] == _load(WATCHER)["permissions"]
    assert _load(NOTICE)["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "write",
    }
    assert "permissions" not in _job()
    assert _job()["continue-on-error"] is True


def test_it_writes_one_comment_and_nothing_else() -> None:
    script = _step()["run"]
    for banned in (
        "--add-assignee",
        "--assignee",
        "--add-label",
        "gh pr ",
        "issue close",
        "issue edit",
        "statuses",
        "check-runs",
        "pulls/",
        "--search",
    ):
        assert banned not in script
    assert script.count("--method POST") == 1 and script.count("--method PATCH") == 1
    assert 'issues/$PR_NUMBER/comments"' in script and "issues/comments/$comment_id" in script


def test_actions_are_pinned_to_a_commit_and_the_checkout_keeps_no_credentials() -> None:
    uses = [s for s in _job()["steps"] if "uses" in s]
    assert uses
    for step in uses:
        assert len(step["uses"].split("@")[1]) == 40
        assert step["with"]["persist-credentials"] is False
        assert step["with"]["sparse-checkout"] == ".github/scripts"


def test_nothing_from_the_event_is_interpolated_into_the_script() -> None:
    step = _step()
    assert "${{" not in step["run"]
    assert step["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert step["env"]["PR_NUMBER"] == "${{ github.event.workflow_run.pull_requests[0].number }}"


def test_a_running_notice_is_never_killed_and_skipped_runs_do_not_share_its_group() -> None:
    concurrency = _load(NOTICE)["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    group = " ".join(str(concurrency["group"]).split())
    assert "pull_requests[0].number" in group and "github.run_id" in group


def test_the_scripts_the_step_runs_exist_and_the_notice_is_small() -> None:
    run = _step()["run"]
    assert (
        ".github/scripts/main_red_pr_notice.py" in run
        and ".github/scripts/main_red_nodeids.py" in run
    )
    assert len((SCRIPTS / "main_red_pr_notice.py").read_text(encoding="utf-8").splitlines()) < 250


# ── The step, executed with gh stubbed ───────────────────────────────────────

_needs_posix = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires a POSIX bash and jq to execute the step script",
)

_GH_STUB = r"""#!/usr/bin/env bash
d="$STUB_DIR"
printf '%s\n' "$*" >> "$d/gh.log"
filter() { while [ "$#" -gt 0 ]; do [ "$1" = "--jq" ] && { printf '%s' "$2"; return; }; shift; done; }
body_file() { for a in "$@"; do case "$a" in body=@*) printf '%s' "${a#body=@}"; return ;; esac; done; }
if [ "$1 $2" = "issue list" ]; then cat "$d/issues.json"; exit 0; fi
[ "$1" = "api" ] || { echo "unexpected gh $*" >&2; exit 9; }
case "$2" in
  --method)
    case "$3" in
      POST) cp "$(body_file "$@")" "$d/posted.md"; exit 0 ;;
      PATCH) cp "$(body_file "$@")" "$d/patched.md"; exit 0 ;;
    esac ;;
  */actions/runs/1000/jobs*) jq -r "$(filter "$@")" < "$d/jobs.json"; exit $? ;;
  */actions/jobs/*/logs) id="${2#*/actions/jobs/}"; id="${id%%/*}"; cat "$d/logs-$id.log"; exit $? ;;
  */actions/runs/950) [ -f "$d/red-run-refused" ] && exit 1; printf '%s\n' "$RED_SHA"; exit 0 ;;
  *compare/*per_page=100*) [ -f "$d/files-refused" ] && exit 1; cat "$d/files.txt"; exit 0 ;;
  *compare/*) [ -f "$d/relation-refused" ] && exit 1; cat "$d/relation.txt"; exit 0 ;;
  */issues/5/timeline*) [ -f "$d/timeline-refused" ] && exit 1; jq -c "$(filter "$@")" < "$d/timeline.json"; exit $? ;;
  */issues/42/comments*) cat "$d/comment-ids.txt"; exit 0 ;;
esac
echo "unexpected gh api $2" >&2; exit 9
"""

PR_LOG = (
    "2026-10-26T08:00:00.0000000Z FAILED test/test_a.py::test_x - assert 2 == 1\n"
    "2026-10-26T08:00:00.0000001Z FAILED test/test_mine.py::test_y - boom\n"
)
PR_JOBS = json.dumps(
    {
        "jobs": [
            {"id": 11, "name": "Backend Tests (3.12, 1)", "conclusion": "failure"},
            {"id": 12, "name": "Backend Tests (3.12, 2)", "conclusion": "success"},
        ]
    }
)


def _open_issue() -> dict[str, Any]:
    first = watch.next_state(None, {"test/test_old.py::t": "x"}, FIRST, 900, RUN_URL)
    state = watch.next_state(first, MAIN, LAST, 950, RUN_URL)
    return {
        "number": 5,
        "title": "Main is red: test/test_a.py::test_x",
        "body": watch.render_body(state, [], 5),
        "author": {"login": "app/github-actions"},
    }


def _run_step(
    tmp_path: Path,
    *,
    log: str = PR_LOG,
    issues: list | None = None,
    files: str = "src/x.py\n",
    relation: str = "ahead",
    comment_ids: str = "",
    timeline: list | None = None,
    refuse: tuple[str, ...] = (),
    env_over: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    stub = tmp_path / "stub"
    stub.mkdir(parents=True)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    (stub / "jobs.json").write_text(PR_JOBS, encoding="utf-8")
    (stub / "logs-11.log").write_text(log, encoding="utf-8")
    (stub / "issues.json").write_text(
        json.dumps([_open_issue()] if issues is None else issues), encoding="utf-8"
    )
    (stub / "timeline.json").write_text(json.dumps(timeline or []), encoding="utf-8")
    (stub / "files.txt").write_text(files, encoding="utf-8")
    (stub / "relation.txt").write_text(relation + "\n", encoding="utf-8")
    (stub / "comment-ids.txt").write_text(comment_ids, encoding="utf-8")
    for name in refuse:
        (stub / name).write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "STUB_DIR": str(stub),
            "RED_SHA": LAST,
            "RUNNER_TEMP": str(runner_temp),
            "GH_TOKEN": "stub",
            "REPO": "example/repo",
            "RUN_ID": "1000",
            "RUN_URL": RUN_URL,
            "HEAD_SHA": HEAD,
            "PR_NUMBER": "42",
            "BASE_SHA": BASE,
            **(env_over or {}),
        }
    )
    out = subprocess.run(
        ["bash", "-c", _step()["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return out, stub


def _calls(stub: Path) -> list[str]:
    log = stub / "gh.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _writes(stub: Path) -> list[str]:
    writing = ("issue comment", "issue edit", "issue create", "issue close", "pr ")
    return [c for c in _calls(stub) if "--method" in c or c.startswith(writing)]


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n\n", 2)[1] if heading in text else ""


@_needs_posix
def test_a_pull_request_failing_a_test_red_on_main_gets_one_comment_naming_it(
    tmp_path: Path,
) -> None:
    out, stub = _run_step(tmp_path)
    assert out.returncode == 0, out.stderr
    assert len(_writes(stub)) == 1 and _writes(stub)[0].startswith(
        "api --method POST repos/example/repo/issues/42/comments"
    )
    text = (stub / "posted.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- main-red-pr-notice -->")
    assert "Failing here and on main with the same message start:" in text
    same = _section(text, "Failing here and on main with the same message start:")
    own = _section(text, "Failing here and not shown to be on main.")
    assert "test/test_a.py::test_x" in same and "test_mine" not in same
    assert "test/test_mine.py::test_y" in own and "test_a.py" not in own
    # Only the failed job's log was read.
    assert any(c.endswith("actions/jobs/11/logs") for c in _calls(stub))
    assert not any("actions/jobs/12/logs" in c for c in _calls(stub))


@_needs_posix
def test_a_later_failed_run_edits_the_existing_comment_and_posts_no_second(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, comment_ids="7001\n")
    assert out.returncode == 0, out.stderr
    assert [c.split()[2] for c in _writes(stub)] == ["PATCH"]
    assert "repos/example/repo/issues/comments/7001" in _writes(stub)[0]
    assert (
        (stub / "patched.md").read_text(encoding="utf-8").startswith("<!-- main-red-pr-notice -->")
    )
    assert not (stub / "posted.md").exists()


@_needs_posix
def test_with_no_open_main_red_issue_nothing_is_read_or_written(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, issues=[])
    assert out.returncode == 0, out.stderr
    assert _writes(stub) == [] and not any("actions/" in c for c in _calls(stub))


@_needs_posix
def test_a_pull_request_failing_only_its_own_tests_writes_nothing(tmp_path: Path) -> None:
    log = "2026-10-26T08:00:00.0000000Z FAILED test/test_mine.py::test_y - boom\n"
    out, stub = _run_step(tmp_path, log=log)
    assert out.returncode == 0, out.stderr
    assert _writes(stub) == [] and "writing nothing" in out.stdout


@_needs_posix
def test_a_failure_that_names_no_test_writes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path, log="2026-10-26T08:00:00.0000000Z ##[error]ruff found 3 errors\n"
    )
    assert out.returncode == 0, out.stderr
    assert _writes(stub) == []


@_needs_posix
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"files": "test/test_a.py\nsrc/x.py\n"}, id="pr-edits-the-test-file"),
        pytest.param({"relation": "diverged"}, id="base-lacks-the-last-red-commit"),
        pytest.param({"refuse": ("relation-refused",)}, id="relation-unreadable"),
        pytest.param({"refuse": ("red-run-refused",)}, id="last-red-run-unreadable"),
        pytest.param({"refuse": ("files-refused",)}, id="changed-files-unreadable"),
        pytest.param(
            {"files": "".join(f"f{i}.py\n" for i in range(300))}, id="changed-files-cut-at-300"
        ),
    ],
)
def test_a_rule_that_cannot_be_shown_leaves_the_failure_with_the_author(
    tmp_path: Path, kwargs: dict
) -> None:
    out, stub = _run_step(tmp_path, **kwargs)
    assert out.returncode == 0, out.stderr
    text = (stub / "posted.md").read_text(encoding="utf-8")
    assert "Failing here and on main with the same message start:" not in text
    assert "- `test/test_a.py::test_x`: `assert 2 == 1`" in _section(
        text, "Failing here and not shown to be on main."
    )


@_needs_posix
def test_shell_text_and_mentions_in_a_failure_message_are_neither_run_nor_posted(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pwned"
    log = f"2026-10-26T08:00:00.0000000Z FAILED test/test_a.py::test_x - $(touch {marker}) `touch {marker}` @octocat\n"
    state = watch.next_state(
        None,
        {"test/test_a.py::test_x": watch.clean(f"$(touch {marker}) 'touch {marker}' @octocat", 80)},
        LAST,
        950,
        RUN_URL,
    )
    issue = {**_open_issue(), "body": watch.render_body(state, [], 5)}
    out, stub = _run_step(tmp_path / "run", log=log, issues=[issue])
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    text = (stub / "posted.md").read_text(encoding="utf-8")
    assert text.count("`") % 2 == 0
    assert "- `test/test_a.py::test_x`: `$(touch" in text


def _cross_reference(url: str = PR_URL) -> dict:
    issue = {
        "html_url": url,
        "state": "open",
        "user": {"login": "buluoray"},
        "created_at": "2026-10-26T07:00:32Z",
        "pull_request": {"url": url},
    }
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": issue}}


@_needs_posix
def test_the_fix_line_comes_from_the_timeline_and_names_no_one_with_a_mention(
    tmp_path: Path,
) -> None:
    out, stub = _run_step(tmp_path, timeline=[{"event": "labeled"}, _cross_reference()])
    assert out.returncode == 0, out.stderr
    text = (stub / "posted.md").read_text(encoding="utf-8")
    assert "Fix in progress: #23933 (buluoray, opened 2026-10-26 07:00Z)" in text
    assert "@" not in text.replace("@octocat", "")


@_needs_posix
def test_an_unreadable_timeline_still_comments_and_says_the_fix_is_unknown(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, refuse=("timeline-refused",))
    assert out.returncode == 0, out.stderr
    assert "Fix in progress: unknown." in (stub / "posted.md").read_text(encoding="utf-8")


@_needs_posix
def test_an_issue_that_someone_else_filed_is_never_read(tmp_path: Path) -> None:
    forged = {**_open_issue(), "author": {"login": "mallory"}}
    out, stub = _run_step(tmp_path, issues=[forged])
    assert out.returncode == 0, out.stderr
    assert _writes(stub) == [] and not any("actions/" in c for c in _calls(stub))


@_needs_posix
def test_an_edited_body_with_a_bad_record_field_writes_nothing(tmp_path: Path) -> None:
    issue = _open_issue()
    issue["body"] = issue["body"].replace('"last_run_id": 950', '"last_run_id": "950; touch x"')
    out, stub = _run_step(tmp_path, issues=[issue])
    assert out.returncode == 0, out.stderr
    assert _writes(stub) == []


@_needs_posix
@pytest.mark.parametrize(
    "env_over",
    [
        pytest.param({"HEAD_SHA": "$(touch pwned)"}, id="head-sha"),
        pytest.param({"RUN_ID": "1000; touch pwned"}, id="run-id"),
        pytest.param({"PR_NUMBER": "42/../../x"}, id="pr-number"),
    ],
)
def test_an_event_value_that_is_not_a_sha_or_a_number_reaches_no_api_path(
    tmp_path: Path, env_over: dict
) -> None:
    out, stub = _run_step(tmp_path, env_over=env_over)
    assert out.returncode == 0, out.stderr
    assert _calls(stub) == [] and not (ROOT / "pwned").exists()


@_needs_posix
def test_the_base_must_contain_the_run_that_supplied_the_set_not_the_first_red_run(
    tmp_path: Path,
) -> None:
    out, stub = _run_step(tmp_path)
    assert out.returncode == 0, out.stderr
    calls = _calls(stub)
    assert f"api repos/example/repo/compare/{LAST}...{BASE} --jq .status" in calls
    assert not any(c.endswith("actions/runs/900 --jq .head_sha") for c in calls)
    assert not any(FIRST in c for c in calls)
