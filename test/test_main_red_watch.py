"""Contract tests for .github/workflows/main-red-watch.yml and .github/scripts/main_red_nodeids.py.

A red Linux or Windows test shard on main reaches nobody when the merge queue is
off. The watcher turns the failing pytest node ids of a red push run into one
issue. Four properties are pinned:

* PARSING -- real-looking job logs (colour codes, timestamps, xdist progress,
  parametrised ids) give ``{node id: message prefix}``; a Fast Gate barrier
  timeout, a cancelled job and a Coverage Gate failure give ``{}``.
* STATE -- first-red SHA and streak survive a body round trip, a re-run does not
  add to the streak, a late older run changes nothing, and a green run closes only
  a streak it came after.
* GATING -- the watched name is ``ci.yml``'s real ``name:``, the job takes push
  runs on main that failed, timed out or passed (never cancelled), the permissions
  are the three it needs, and log text never reaches a ``${{ }}`` expression.
* WRITING -- the step runs with ``gh`` stubbed: a red run with node ids files one
  issue, a barrier red files nothing, a repeat edits the open one, a green run
  closes it, and shell text in a message is never executed.

Skipped where the POSIX toolchain the step needs is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WATCHER = WORKFLOWS / "main-red-watch.yml"
SCRIPT = ROOT / ".github" / "scripts" / "main_red_nodeids.py"

mod = load_skill_script("main_red_nodeids_under_test", SCRIPT)

RUN_URL = "https://github.com/example/repo/actions/runs/1000"
SHA = "0123456789abcdef0123456789abcdef01234567"

# ── Log fixtures, shaped like the Backend Tests and gate jobs of ci.yml ──────

LINUX_SHARD_RED = """\
2026-10-26T06:58:01.1234567Z ##[group]Run python -m pytest -n auto --dist loadgroup
2026-10-26T07:12:44.1000000Z [gw3] [ 41%] \x1b[31mFAILED\x1b[0m test/test_memory_files_substitution.py::TestTheInjectedImplementationAnswers::test_pruning_removes_from_the_seam_and_leaves_local_files_alone
2026-10-26T07:12:44.2000000Z [gw1] [ 42%] \x1b[32mPASSED\x1b[0m test/test_a.py::test_ok
2026-10-26T07:19:58.0000000Z =================================== FAILURES ===================================
2026-10-26T07:19:58.0000001Z ::error file=test/test_memory_files_substitution.py,line=212,title=test/test_memory_files_substitution.py%3A%3AX::oops
2026-10-26T07:20:01.0000000Z \x1b[31m=========================== short test summary info ============================\x1b[0m
2026-10-26T07:20:01.0000001Z \x1b[31mFAILED\x1b[0m test/test_memory_files_substitution.py::TestTheInjectedImplementationAnswers::test_pruning_removes_from_the_seam_and_leaves_local_files_alone - assert 2 == 1
2026-10-26T07:20:01.0000002Z \x1b[31mFAILED\x1b[0m test/test_native_skill_projection_rewrites.py::test_two_concurrent_releases_each_prune_their_own_view - AssertionError: assert {'a': 1, 'b': 2, 'c': 3} == {'a': 1, 'b': 2, 'c': 4} and a very long tail
2026-10-26T07:20:01.0000003Z \x1b[31mFAILED\x1b[0m test/test_pinned_staging_helper_contract.py::test_probe[dir_fd - ok] - TypeError: x
2026-10-26T07:20:01.0000004Z \x1b[31mFAILED\x1b[0m test/test_no_message.py::test_bare
2026-10-26T07:20:01.0000005Z \x1b[31mERROR\x1b[0m test/test_tmp_residue.py::test_leaves_a_file - Failed: residue
2026-10-26T07:20:01.0000006Z ERROR test/test_collect.py - ImportError: no module named x
2026-10-26T07:20:02.0000000Z ======= 4 failed, 1 error, 4310 passed, 12 skipped in 1180.32s (0:19:40) =======
2026-10-26T07:20:03.0000000Z ##[error]Process completed with exit code 1.
"""

WINDOWS_SHARD_RED = """\
2026-10-26T07:30:01.0000000Z FAILED test/test_memory_files_substitution.py::TestTheInjectedImplementationAnswers::test_pruning_removes_from_the_seam_and_leaves_local_files_alone - assert 2 == 1
2026-10-26T07:30:01.0000001Z FAILED test\\test_windows_only.py::test_paths - OSError: [WinError 5]
"""

BARRIER_TIMEOUT = """\
2026-10-25T08:06:10.0000000Z ##[group]Run bash .github/scripts/await-fast-gate.sh
2026-10-25T08:06:11.0000000Z Fast Gate for 0123456789ab is pending; polling for up to 720s
2026-10-25T08:18:12.0000000Z Fast Gate did not report within 720s (job-queue wait is not counted).
2026-10-25T08:18:12.0000001Z ##[error]Process completed with exit code 1.
"""

CANCELLED_JOB = """\
2026-10-25T09:00:00.0000000Z [gw0] [ 10%] FAILED test/test_partial.py::test_started_then_cancelled
2026-10-25T09:05:00.0000000Z ##[error]The operation was canceled.
2026-10-25T09:05:01.0000000Z Terminate orphan process: pid (4242) (python)
"""

COVERAGE_GATE = """\
2026-10-26T08:00:00.0000000Z FAIL Required test coverage of 80% not reached. Total coverage: 79.12%
2026-10-26T08:00:00.0000001Z ##[error]Process completed with exit code 1.
"""

HOSTILE = """\
2026-10-26T07:20:01.0000000Z FAILED test/test_x.py::test_a - boom `touch /tmp/pwn` @octocat --> <script> $(id) \x07\u00e9
"""


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_a_red_backend_shard_gives_every_failing_node_id_with_a_message_prefix() -> None:
    found = mod.parse_failures(LINUX_SHARD_RED)
    assert list(found) == [
        "test/test_memory_files_substitution.py::TestTheInjectedImplementationAnswers::"
        "test_pruning_removes_from_the_seam_and_leaves_local_files_alone",
        "test/test_native_skill_projection_rewrites.py::"
        "test_two_concurrent_releases_each_prune_their_own_view",
        "test/test_pinned_staging_helper_contract.py::test_probe[dir_fd - ok]",
        "test/test_no_message.py::test_bare",
        "test/test_tmp_residue.py::test_leaves_a_file",
    ]
    values = list(found.values())
    assert values[0] == "assert 2 == 1"
    assert len(values[1]) == mod.MESSAGE_CHARS
    assert values[1].startswith("AssertionError: assert {'a': 1")
    assert values[2] == "TypeError: x"
    assert values[3] == ""


def test_progress_lines_annotations_and_collection_errors_are_not_failures() -> None:
    found = mod.parse_failures(LINUX_SHARD_RED)
    assert not any("test_a.py" in nodeid for nodeid in found)
    assert not any(nodeid.startswith("test/test_collect.py") for nodeid in found)


def test_a_windows_shard_with_a_backslash_path_gives_the_same_node_id_form() -> None:
    found = mod.parse_failures(WINDOWS_SHARD_RED)
    assert "test/test_windows_only.py::test_paths" in found


def test_the_same_test_red_on_two_legs_is_listed_once() -> None:
    merged = mod.merge_failures(
        [mod.parse_failures(LINUX_SHARD_RED), mod.parse_failures(WINDOWS_SHARD_RED)]
    )
    shared = "test_pruning_removes_from_the_seam_and_leaves_local_files_alone"
    assert sum(shared in nodeid for nodeid in merged) == 1
    assert "test/test_windows_only.py::test_paths" in merged


@pytest.mark.parametrize(
    "log",
    [BARRIER_TIMEOUT, CANCELLED_JOB, COVERAGE_GATE, ""],
    ids=["barrier", "cancelled", "coverage", "empty"],
)
def test_a_log_that_names_no_failing_test_gives_nothing(log: str) -> None:
    assert mod.parse_failures(log) == {}


def test_hostile_message_text_is_cut_to_printable_ascii_without_backticks() -> None:
    ((nodeid, message),) = mod.parse_failures(HOSTILE).items()
    assert nodeid == "test/test_x.py::test_a"
    assert "`" not in message
    assert message.isascii() and message.isprintable()
    assert len(message) <= mod.MESSAGE_CHARS


def test_a_hostile_line_gives_at_most_one_capped_node_id() -> None:
    """CodeQL flagged the old node id pattern: it backtracked on 'ERROR -.py::!::' and many '!::'.

    The id is now split by one pass over the text, so a long line is one entry cut
    to 200 characters, or nothing.
    """
    lines = [
        "ERROR -.py::" + "!::" * 50_000,
        "FAILED a.py::" + "[" * 100_000,
        "FAILED " + "a.py" * 50_000,
    ]
    for line in lines:
        found = mod.parse_failures(line)
        assert len(found) <= 1
        assert all(len(nodeid) <= 200 for nodeid in found)
    assert mod.parse_failures(lines[2]) == {}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("FAILED test/a.py::t[x y]", {"test/a.py::t[x y]": ""}),
        ("FAILED test/a.py::C::t[a - b] - boom", {"test/a.py::C::t[a - b]": "boom"}),
        ("FAILED test/a.py::t trailing words", {}),
        ("FAILED test/a.py::", {}),
        ("FAILED test/a.py - no node", {}),
        ("FAILED notpython::t", {}),
        ("PASSED test/a.py::t", {}),
    ],
)
def test_the_node_id_is_split_at_the_first_space_outside_brackets(
    line: str, expected: dict
) -> None:
    assert mod.parse_failures(line) == expected


def test_the_set_is_capped() -> None:
    many = {f"test/t.py::test_{i}": "" for i in range(mod.MAX_NODEIDS + 30)}
    assert len(mod.merge_failures([many])) == mod.MAX_NODEIDS


# ── State, streak and closing ────────────────────────────────────────────────


def _state(run_id: int = 1000, failures: dict[str, str] | None = None, **extra: Any) -> dict:
    state = mod.next_state(None, failures or {"test/a.py::t": "boom"}, SHA, run_id, RUN_URL)
    return {**state, **extra}


def test_the_first_red_run_starts_a_streak_of_one_at_its_own_sha() -> None:
    state = _state()
    assert (state["first_sha"], state["streak"], state["first_run_id"]) == (SHA[:12], 1, 1000)


def test_a_later_red_run_keeps_the_first_sha_adds_one_and_replaces_the_set() -> None:
    later = mod.next_state(_state(), {"test/b.py::t": ""}, "f" * 40, 1005, RUN_URL)
    assert (later["first_sha"], later["streak"], later["last_sha"]) == (SHA[:12], 2, "f" * 12)
    assert list(later["failures"]) == ["test/b.py::t"]


def test_a_rerun_of_the_same_run_does_not_add_to_the_streak() -> None:
    again = mod.next_state(_state(), {"test/a.py::t": "boom"}, SHA, 1000, RUN_URL)
    assert again["streak"] == 1


def test_an_older_run_that_finishes_late_changes_nothing() -> None:
    assert mod.next_state(_state(1005), {"test/a.py::t": ""}, SHA, 1000, RUN_URL) is None


def test_the_state_survives_the_body_even_when_a_message_would_end_the_comment() -> None:
    state = _state(failures={"test/a.py::t[--]": "x --> <b> y"})
    body = mod.render_body(state, [], 77)
    assert body.split("<!-- main-red-state", 1)[1].count("-->") == 1
    assert mod.parse_state(body) == state


def _tampered(**changes: Any) -> str:
    record = {**_state(), **changes}
    return f"<!-- main-red-state {json.dumps(record)} -->"


@pytest.mark.parametrize(
    "changes",
    [
        {"streak": "many"},
        {"streak": True},
        {"last_run_id": -1},
        {"first_sha": "main; rm"},
        {"last_url": "https://evil.example/x"},
        {"last_url": "https://github.com/a/b) [x](http://evil"},
        {"failures": ["not", "a", "dict"]},
    ],
)
def test_an_edited_record_with_an_odd_field_is_not_trusted(changes: dict) -> None:
    assert mod.parse_state(_tampered(**changes)) is None


def test_an_edited_record_has_its_node_ids_and_messages_cleaned_again() -> None:
    state = mod.parse_state(_tampered(failures={"a.py::t`x": "boom `@octocat`\x00"}))
    assert state is not None
    ((nodeid, message),) = state["failures"].items()
    assert "`" not in nodeid + message and message.isprintable()


@pytest.mark.parametrize(
    "body", ["", "no record", "<!-- main-red-state {nope} -->", "<!-- main-red-state {}"]
)
def test_a_body_without_a_valid_record_has_no_state(body: str) -> None:
    assert mod.parse_state(body) is None


def test_a_successful_rerun_of_the_last_red_run_ends_the_streak() -> None:
    assert mod.should_close(_state(1000), 1000) is True


def test_a_green_run_closes_only_a_streak_it_came_after() -> None:
    state = _state(1000)
    assert mod.should_close(state, 1001) is True
    assert mod.should_close(state, 999) is False
    assert mod.should_close(None, 1001) is False


BOT = {"login": "app/github-actions", "is_bot": True}


def test_the_oldest_issue_with_the_marker_the_title_prefix_and_our_author_is_picked() -> None:
    marked = mod.render_body(_state(), [], 5)
    title = "Main is red: test/a.py::t"
    issues = [
        {"number": 9, "title": title, "body": marked, "author": BOT},
        {"number": 5, "title": title, "body": marked, "author": BOT},
        {"number": 2, "title": "Main is red: by hand", "body": "no record", "author": BOT},
        {"number": 1, "title": "Something else", "body": marked, "author": BOT},
    ]
    assert mod.pick_issue(issues)["number"] == 5
    assert mod.pick_issue(issues[2:]) is None
    assert mod.pick_issue([]) is None


def test_an_issue_a_person_wrote_is_never_ours_even_with_a_perfect_record() -> None:
    """The forged-issue attack: our title, a valid record, a huge last_run_id."""
    forged = mod.render_body(_state(99_999_999_999), [], 3)
    title = "Main is red: x"
    people = [
        {"login": "octocat", "is_bot": False},
        {"login": "github-actions", "is_bot": False},
        {"login": "app/other-app", "is_bot": True},
        {"login": "github-actions[bot]x"},
        None,
        "github-actions[bot]",
    ]
    for author in people:
        issue = {"number": 3, "title": title, "body": forged, "author": author}
        assert mod.pick_issue([issue]) is None, author
    assert mod.pick_issue([{"number": 3, "title": title, "body": forged}]) is None


@pytest.mark.parametrize(
    "author",
    [
        {"login": "app/github-actions", "is_bot": True},
        {"login": "github-actions[bot]"},
        {"login": "github-actions", "is_bot": True},
    ],
)
def test_the_actions_token_is_recognised_in_every_spelling_the_tools_use(author: dict) -> None:
    assert mod.is_watcher_author(author) is True


# ── The fix-in-progress line ─────────────────────────────────────────────────

PR_URL = "https://github.com/example/repo/pull/23933"


def _row(**over: Any) -> dict[str, Any]:
    row = {"url": PR_URL, "state": "open", "author": "buluoray", "created": "2026-10-26T07:00:32Z"}
    return {**row, **over}


def test_with_no_fix_pr_the_line_invites_one_and_names_the_issue() -> None:
    assert mod.fix_line([], 77) == (
        "Fix in progress: none yet. Anyone may open a fix PR (put `Fixes #77` in its body)."
    )
    assert "`Fixes #<this issue>`" in mod.fix_line([], None)


def test_an_open_pr_that_cross_references_the_issue_becomes_the_line() -> None:
    prs = mod.fix_prs([_row()], "example/repo")
    assert mod.fix_line(prs, 77) == "Fix in progress: #23933 (@buluoray, opened 2026-10-26 07:00Z)"


@pytest.mark.parametrize(
    "row",
    [
        _row(state="closed"),
        _row(url="https://github.com/other/repo/pull/5"),
        _row(url="https://github.com/example/repo/pull/5x"),
        _row(author="bad name; rm -rf"),
        _row(author=""),
    ],
    ids=["closed", "other-repo", "not-a-number", "odd-login", "no-login"],
)
def test_a_row_that_is_not_an_open_pr_of_this_repo_is_ignored(row: dict) -> None:
    assert mod.fix_prs([row], "example/repo") == []


def test_a_bot_author_is_named_without_a_mention_and_the_list_is_capped_oldest_first() -> None:
    rows = [
        _row(url=f"https://github.com/example/repo/pull/{n}", created=f"2026-10-26T0{n}:00:00Z")
        for n in (9, 7, 8, 6)
    ]
    rows[0]["author"] = "dependabot[bot]"
    prs = mod.fix_prs(rows, "example/repo")
    assert [pr["number"] for pr in prs] == ["6", "7", "8"]
    prs = mod.fix_prs(rows[:1], "example/repo")
    assert "(dependabot[bot], opened" in mod.fix_line(prs, 1)


def test_timeline_output_is_read_whether_one_per_line_or_pretty_printed() -> None:
    compact = json.dumps(_row()) + "\n" + json.dumps(_row(state="closed"))
    pretty = json.dumps(_row(), indent=2) + "\n" + json.dumps(_row(state="closed"), indent=2)
    assert mod.read_json_values(compact) == mod.read_json_values(pretty)
    assert len(mod.read_json_values(compact)) == 2


def test_the_body_carries_the_set_the_first_sha_the_streak_and_the_fix_line() -> None:
    state = _state(failures={"test/a.py::t": "assert 2 == 1", "test/b.py::u": ""})
    body = mod.render_body(state, mod.fix_prs([_row()], "example/repo"), 77)
    assert "- `test/a.py::t`: `assert 2 == 1`" in body
    assert "- `test/b.py::u`\n" in body
    assert f"First red: [{SHA[:12]}]({RUN_URL})" in body
    assert "Streak: 1" in body
    assert "Fix in progress: #23933 (@buluoray, opened 2026-10-26 07:00Z)" in body
    assert "none yet" not in body
    assert mod.title_for(state["failures"]) == "Main is red: test/a.py::t"


# ── Wiring and least privilege ───────────────────────────────────────────────


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job() -> dict:
    (job,) = _load(WATCHER)["jobs"].values()
    return job


def _step() -> dict:
    (step,) = [s for s in _job()["steps"] if "run" in s]
    return step


def test_the_watched_name_is_the_real_name_of_ci_yml() -> None:
    document = _load(WATCHER)
    triggers = document.get("on") or document.get(True)
    assert triggers["workflow_run"]["workflows"] == [_load(WORKFLOWS / "ci.yml")["name"]]
    assert triggers["workflow_run"]["types"] == ["completed"]


def test_the_green_proof_job_prefix_is_a_real_job_of_ci_yml() -> None:
    prefix = _step()["env"]["PROOF_JOB_PREFIX"]
    names = [str(job.get("name", "")) for job in _load(WORKFLOWS / "ci.yml")["jobs"].values()]
    assert any(name.startswith(prefix) for name in names)


def test_the_proof_step_is_a_real_step_that_a_reduced_backend_scope_skips() -> None:
    env = _step()["env"]
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    (backend,) = [job for job in jobs.values() if job.get("name") == env["PROOF_JOB_PREFIX"]]
    (upload,) = [st for st in backend["steps"] if st.get("name") == env["PROOF_STEP_NAME"]]
    assert upload["if"] == "steps.scope.outputs.reduced != 'true'"


def test_the_job_takes_push_runs_on_main_that_failed_timed_out_or_passed() -> None:
    condition = " ".join(str(_job()["if"]).split())
    assert "github.event.workflow_run.event == 'push'" in condition
    assert "github.event.workflow_run.head_branch == 'main'" in condition
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in condition
    for conclusion in ("failure", "timed_out", "success"):
        assert f"github.event.workflow_run.conclusion == '{conclusion}'" in condition
    assert "cancelled" not in condition


def test_the_permissions_are_exactly_what_the_step_needs() -> None:
    assert _load(WATCHER)["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "write",
    }
    assert "permissions" not in _job()


def test_it_writes_issues_only_and_never_a_pull_request() -> None:
    script = _step()["run"]
    assert "pr comment" not in script and "pulls/" not in script and "gh pr " not in script
    assert "--assignee" not in script and "--add-assignee" not in script
    assert "--search" not in script


def test_actions_are_pinned_to_a_commit_and_the_checkout_keeps_no_credentials() -> None:
    uses = [s for s in _job()["steps"] if "uses" in s]
    assert uses, "the script is read from a sparse checkout of main"
    for step in uses:
        assert step["uses"].split("@")[1].isalnum() and len(step["uses"].split("@")[1]) == 40
        assert step["with"]["persist-credentials"] is False
        assert step["with"]["sparse-checkout"] == ".github/scripts"


def test_nothing_from_the_event_is_interpolated_into_the_script() -> None:
    step = _step()
    assert "${{" not in step["run"]
    assert step["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert step["env"]["RUN_URL"] == "${{ github.event.workflow_run.html_url }}"


def test_a_running_watcher_is_never_killed_and_skipped_runs_do_not_share_its_group() -> None:
    concurrency = _load(WATCHER)["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    group = " ".join(str(concurrency["group"]).split())
    assert "github.event.workflow_run.event == 'push'" in group
    assert "github.event.workflow_run.head_branch == 'main'" in group
    assert "github.run_id" in group


def test_the_script_the_step_runs_exists_and_is_small_enough_to_import() -> None:
    assert ".github/scripts/main_red_nodeids.py" in _step()["run"]
    assert len(SCRIPT.read_text(encoding="utf-8").splitlines()) < 600


# ── The step, executed with gh stubbed ───────────────────────────────────────

_needs_posix = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires a POSIX bash and jq to execute the step script",
)

_GH_STUB = r"""#!/usr/bin/env bash
d="$STUB_DIR"
printf '%s\n' "$*" >> "$d/gh.log"
filter() { while [ "$#" -gt 0 ]; do [ "$1" = "--jq" ] && { printf '%s' "$2"; return; }; shift; done; }
arg_after() { local k="$1"; shift; while [ "$#" -gt 0 ]; do [ "$1" = "$k" ] && { printf '%s' "$2"; return; }; shift; done; }
case "$1 $2" in
  "label create") [ -f "$d/label-refused" ] && exit 1; exit 0 ;;
  "issue list")
    case " $* " in
      *" --label "*) cat "$d/labelled-issues.json" ;;
      *) cat "$d/issues.json" ;;
    esac
    exit 0 ;;
  "issue create")
    arg_after --title "$@" > "$d/created-title.txt"
    cp "$(arg_after --body-file "$@")" "$d/created-body.md"
    echo "https://github.com/example/repo/issues/77"; exit 0 ;;
  "issue edit") cp "$(arg_after --body-file "$@")" "$d/edit-$3.md"; exit 0 ;;
  "issue comment") arg_after --body "$@" > "$d/comment-$3.txt"; exit 0 ;;
  "issue close") exit 0 ;;
esac
if [ "$1" = "api" ]; then
  case "$2" in
    */actions/runs/1000/jobs*) jq -r "$(filter "$@")" < "$d/jobs.json" ;;
    */actions/jobs/*/logs) id="${2#*/actions/jobs/}"; id="${id%%/*}"; cat "$d/logs-$id.log" ;;
    */issues/*/timeline*) [ -f "$d/timeline-refused" ] && exit 1; jq -c "$(filter "$@")" < "$d/timeline.json" ;;
    *) echo "unexpected gh api $2" >&2; exit 9 ;;
  esac
  exit $?
fi
echo "unexpected gh $*" >&2; exit 9
"""


def _jobs(*rows: tuple[int, str, str], coverage: str | None = None) -> str:
    """Jobs as the API lists them. ``coverage`` is the conclusion of the shard
    coverage upload step on every `Backend Tests` job (absent when ``None``)."""
    jobs = []
    for i, n, c in rows:
        steps = []
        if coverage is not None and n.startswith("Backend Tests"):
            steps = [{"name": "Upload shard coverage", "conclusion": coverage}]
        jobs.append({"id": i, "name": n, "conclusion": c, "steps": steps})
    return json.dumps({"jobs": jobs})


RED_JOBS = _jobs(
    (11, "Backend Tests (3.12, 3)", "failure"),
    (12, "Backend Tests (3.12, 4)", "success"),
    (13, "Await Fast Gate", "success"),
)
BARRIER_JOBS = _jobs((21, "Await Fast Gate", "failure"), (22, "Backend Tests (3.12, 1)", "skipped"))
GREEN_JOBS = _jobs(
    (31, "Backend Tests (3.12, 1)", "success"),
    (32, "Backend Tests (3.12, 2)", "success"),
    (33, "Backend Tests (Windows)", "success"),
    (34, "Await Fast Gate", "success"),
    coverage="success",
)
DOCS_ONLY_GREEN = _jobs(
    (41, "Detect changed surface", "success"), (42, "Backend Tests (3.12, 1)", "skipped")
)
# A frontend-only or leaf-only push: the backend shards ran a subset, so the
# shard coverage upload was skipped.
REDUCED_GREEN = _jobs(
    (51, "Backend Tests (3.12, 1)", "success"),
    (52, "Backend Tests (3.12, 2)", "success"),
    coverage="skipped",
)


def _cross_reference(*, is_pr: bool = True, url: str = PR_URL) -> dict:
    """A ``cross-referenced`` timeline event in the shape the REST API returns."""
    issue = {
        "html_url": url,
        "state": "open",
        "user": {"login": "buluoray"},
        "created_at": "2026-10-26T07:00:32Z",
    }
    if is_pr:
        issue["pull_request"] = {"url": url}
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": issue}}


def _open_issue(number: int = 5, run_id: int = 900) -> dict:
    state = mod.next_state(None, {"test/old.py::t": "x"}, "a" * 40, run_id, RUN_URL)
    return {
        "number": number,
        "title": "Main is red: test/old.py::t",
        "body": mod.render_body(state, [], number),
        "author": BOT,
    }


def _run_step(
    tmp_path: Path,
    *,
    jobs: str = RED_JOBS,
    logs: dict[int, str] | None = None,
    issues: list[dict] | None = None,
    conclusion: str = "failure",
    timeline: list[dict] | None = None,
    label_refused: bool = False,
    timeline_refused: bool = False,
    labelled_issues: list[dict] | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    stub = tmp_path / "stub"
    stub.mkdir(parents=True)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    (stub / "jobs.json").write_text(jobs, encoding="utf-8")
    (stub / "issues.json").write_text(json.dumps(issues or []), encoding="utf-8")
    # The labelled list defaults to the same issues, as when every one carries the label.
    labelled = issues if labelled_issues is None else labelled_issues
    (stub / "labelled-issues.json").write_text(json.dumps(labelled or []), encoding="utf-8")
    (stub / "timeline.json").write_text(json.dumps(timeline or []), encoding="utf-8")
    for job_id, text in (logs if logs is not None else {11: LINUX_SHARD_RED}).items():
        (stub / f"logs-{job_id}.log").write_text(text, encoding="utf-8")
    if label_refused:
        (stub / "label-refused").write_text("", encoding="utf-8")
    if timeline_refused:
        (stub / "timeline-refused").write_text("", encoding="utf-8")
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
            "RUNNER_TEMP": str(runner_temp),
            "GH_TOKEN": "stub",
            "REPO": "example/repo",
            "RUN_ID": "1000",
            "RUN_URL": RUN_URL,
            "HEAD_SHA": SHA,
            "CONCLUSION": conclusion,
            "PROOF_JOB_PREFIX": "Backend Tests",
            "PROOF_STEP_NAME": "Upload shard coverage",
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


def _wrote(stub: Path, *verbs: str) -> list[str]:
    return [c for c in _calls(stub) if any(c.startswith(f"issue {v}") for v in verbs)]


@_needs_posix
def test_a_red_run_with_node_ids_files_one_labelled_issue_titled_by_the_first(
    tmp_path: Path,
) -> None:
    out, stub = _run_step(tmp_path)
    assert out.returncode == 0, out.stderr
    assert (
        (stub / "created-title.txt")
        .read_text(encoding="utf-8")
        .startswith(
            "Main is red: test/test_memory_files_substitution.py::TestTheInjectedImplementationAnswers::"
        )
    )
    assert len(_wrote(stub, "create")) == 1
    assert any("--label main-red" in c for c in _wrote(stub, "create"))
    created = (stub / "created-body.md").read_text(encoding="utf-8")
    assert "Fix in progress: none yet." in created
    edited = (stub / "edit-77.md").read_text(encoding="utf-8")
    assert "put `Fixes #77` in its body" in edited
    assert mod.parse_state(edited)["first_sha"] == SHA[:12]
    # Only the failed job's log was read; the passing shard was not.
    assert any(c.endswith("actions/jobs/11/logs") for c in _calls(stub))
    assert not any("actions/jobs/12/logs" in c for c in _calls(stub))
    assert not any(" --assignee" in c or c.startswith("pr ") for c in _calls(stub))


@_needs_posix
def test_a_fast_gate_barrier_red_writes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, jobs=BARRIER_JOBS, logs={21: BARRIER_TIMEOUT})
    assert out.returncode == 0, out.stderr
    assert "writing nothing" in out.stdout
    assert _wrote(stub, "create", "edit", "comment", "close") == []


@_needs_posix
def test_a_cancelled_job_log_and_a_coverage_gate_failure_write_nothing(tmp_path: Path) -> None:
    jobs = _jobs((51, "Backend Tests (3.12, 1)", "failure"), (52, "Coverage Gate", "failure"))
    out, stub = _run_step(tmp_path, jobs=jobs, logs={51: CANCELLED_JOB, 52: COVERAGE_GATE})
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create", "edit", "comment", "close") == []


@_needs_posix
def test_a_failed_log_read_is_skipped_not_fatal(tmp_path: Path) -> None:
    jobs = _jobs(
        (11, "Backend Tests (3.12, 3)", "failure"), (14, "Backend Tests (3.12, 5)", "failure")
    )
    out, stub = _run_step(tmp_path, jobs=jobs, logs={11: LINUX_SHARD_RED})
    assert out.returncode == 0, out.stderr
    assert len(_wrote(stub, "create")) == 1


@_needs_posix
def test_a_repeat_red_edits_the_open_issue_and_files_no_second(tmp_path: Path) -> None:
    timeline = [
        {"event": "labeled"},
        _cross_reference(is_pr=False, url="https://github.com/example/repo/issues/9"),
        _cross_reference(),
    ]
    out, stub = _run_step(tmp_path, issues=[_open_issue()], timeline=timeline)
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create") == []
    body = (stub / "edit-5.md").read_text(encoding="utf-8")
    assert "Fix in progress: #23933 (@buluoray, opened 2026-10-26 07:00Z)" in body
    state = mod.parse_state(body)
    assert (state["first_sha"], state["streak"]) == ("a" * 12, 2)
    assert "test/test_no_message.py::test_bare" in state["failures"]


@_needs_posix
def test_a_forged_open_issue_does_not_stop_a_red_run_from_being_filed(tmp_path: Path) -> None:
    forged = {**_open_issue(number=3, run_id=99_999_999_999), "author": {"login": "octocat"}}
    out, stub = _run_step(tmp_path, issues=[forged])
    assert out.returncode == 0, out.stderr
    assert len(_wrote(stub, "create")) == 1
    assert _wrote(stub, "edit")[0].startswith("issue edit 77")
    assert any("--json number,title,body,author" in c for c in _calls(stub) if "issue list" in c)


@_needs_posix
def test_a_forged_open_issue_does_not_block_a_green_run_from_closing_the_real_one(
    tmp_path: Path,
) -> None:
    forged = {**_open_issue(number=3, run_id=99_999_999_999), "author": {"login": "octocat"}}
    out, stub = _run_step(
        tmp_path, jobs=GREEN_JOBS, conclusion="success", issues=[forged, _open_issue()]
    )
    assert out.returncode == 0, out.stderr
    assert any(c.startswith("issue close 5") for c in _calls(stub))
    assert not any(c.startswith("issue close 3") for c in _calls(stub))


@_needs_posix
def test_an_unreadable_timeline_still_updates_the_issue_with_no_fix_pr(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, issues=[_open_issue()], timeline_refused=True)
    assert out.returncode == 0, out.stderr
    assert "Fix in progress: none yet" in (stub / "edit-5.md").read_text(encoding="utf-8")


@_needs_posix
def test_an_older_red_run_finishing_late_edits_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, issues=[_open_issue(run_id=2000)])
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create", "edit") == []


@_needs_posix
def test_without_the_label_the_issue_is_filed_and_found_by_title_prefix(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, label_refused=True)
    assert out.returncode == 0, out.stderr
    assert not any("--label" in c for c in _calls(stub))
    out, stub = _run_step(tmp_path / "again", label_refused=True, issues=[_open_issue()])
    assert _wrote(stub, "create") == []
    assert any(c.startswith("issue list") and "--label" not in c for c in _calls(stub))


@_needs_posix
def test_a_green_run_that_ran_the_tests_comments_and_closes_the_open_issue(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, jobs=GREEN_JOBS, conclusion="success", issues=[_open_issue()])
    assert out.returncode == 0, out.stderr
    assert (stub / "comment-5.txt").read_text(encoding="utf-8").startswith("Main is green again")
    assert any(c.startswith("issue close 5") for c in _calls(stub))
    assert _wrote(stub, "create", "edit") == []


@_needs_posix
def test_a_successful_rerun_of_the_last_red_run_closes_the_issue(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path, jobs=GREEN_JOBS, conclusion="success", issues=[_open_issue(run_id=1000)]
    )
    assert out.returncode == 0, out.stderr
    assert any(c.startswith("issue close 5") for c in _calls(stub))


@_needs_posix
def test_a_green_run_with_a_reduced_backend_scope_closes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path, jobs=REDUCED_GREEN, conclusion="success", issues=[_open_issue()]
    )
    assert out.returncode == 0, out.stderr
    assert "full backend suite" in out.stdout
    assert _wrote(stub, "comment", "close") == []


@_needs_posix
def test_an_issue_filed_before_the_label_existed_is_still_found_and_closed(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=GREEN_JOBS,
        conclusion="success",
        issues=[_open_issue()],
        labelled_issues=[],
    )
    assert out.returncode == 0, out.stderr
    assert any(c.startswith("issue close 5") for c in _calls(stub))


@_needs_posix
def test_a_red_run_edits_an_unlabelled_issue_instead_of_filing_a_duplicate(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, issues=[_open_issue()], labelled_issues=[])
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create") == []
    assert (stub / "edit-5.md").exists()


@_needs_posix
def test_a_green_run_that_skipped_the_tests_closes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path, jobs=DOCS_ONLY_GREEN, conclusion="success", issues=[_open_issue()]
    )
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "comment", "close") == []


@_needs_posix
def test_a_green_run_older_than_the_last_red_one_closes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path, jobs=GREEN_JOBS, conclusion="success", issues=[_open_issue(run_id=2000)]
    )
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "comment", "close") == []


@_needs_posix
def test_a_green_run_with_no_open_issue_writes_nothing(tmp_path: Path) -> None:
    out, stub = _run_step(tmp_path, jobs=GREEN_JOBS, conclusion="success")
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create", "edit", "comment", "close") == []


@_needs_posix
def test_shell_text_in_a_failure_message_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    hostile = (
        f"2026-10-26T07:20:01.0000000Z FAILED test/test_x.py::test_a - $(touch {marker}) "
        f"`touch {marker}` ; touch {marker} @octocat\n"
    )
    out, stub = _run_step(tmp_path / "run", logs={11: hostile})
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    body = (stub / "created-body.md").read_text(encoding="utf-8")
    (line,) = [ln for ln in body.splitlines() if ln.startswith("- `test/test_x.py::test_a`")]
    assert re.fullmatch(r"- `[^`]+`: `[^`]*`", line), line
