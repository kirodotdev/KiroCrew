"""PR CI deselects only the exact tests the bot's open main-red issue names.

`scripts/ci_main_red_skip.py` reads the issue once in ci.yml's `changes` job
and a pytest plugin deselects its node ids on each backend shard. Every case
below pins the scope chosen: what runs and what is deselected. The issue body
is built by the watcher's own renderer (`.github/scripts/main_red_nodeids.py`),
so a change to its format fails here rather than silently running nothing.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "kirodotdev/KiroCrew"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SKIP = _load("ci_main_red_skip_test_helper", ROOT / "scripts" / "ci_main_red_skip.py")
WATCH = _load(
    "main_red_nodeids_skip_test_helper", ROOT / ".github" / "scripts" / "main_red_nodeids.py"
)

THREE = [
    "test/test_alpha.py::test_one",
    "test/sub/test_beta.py::TestBeta::test_two[case-1]",
    "src/kiro_crew/apps/thing/tests/test_gamma.py::test_three",
]


def watcher_body(nodeids: list[str]) -> str:
    failures = {nodeid: "AssertionError: boom" for nodeid in nodeids}
    url = "https://github.com/kirodotdev/KiroCrew/actions/runs/1"
    state = WATCH.next_state(None, failures, "a" * 40, 1, url)
    return WATCH.render_body(state, [], 900)


def issue(
    nodeids: list[str],
    *,
    login: str = "github-actions[bot]",
    kind: str = "Bot",
    number: int = 900,
    body: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "title": f"Main is red: {nodeids[0] if nodeids else 'x'}",
        "user": {"login": login, "type": kind},
        "labels": [{"name": "main-red"}],
        "body": watcher_body(nodeids) if body is None else body,
    }


def run_decide(issues: list[Any], **overrides: Any) -> tuple[dict[str, Any] | None, str]:
    args: dict[str, Any] = {
        "event": "pull_request",
        "base_ref": "main",
        "labels": [],
        "pr_body": "Fixes #12",
        "repo": REPO,
        "issues": issues,
        "changed_files": {"src/kiro_crew/other.py"},
    }
    args.update(overrides)
    return SKIP.decide(**args)


# -- decide: the scope each case chooses ------------------------------------


def test_no_issue_runs_everything() -> None:
    plan, reason = run_decide([])
    assert plan is None
    assert "No open main-red issue" in reason


def test_bot_issue_with_three_ids_deselects_exactly_those() -> None:
    plan, reason = run_decide([issue(THREE)])
    assert plan == {"issue": 900, "nodeids": THREE}
    assert "Deselecting 3 test(s)" in reason
    assert f"https://github.com/{REPO}/issues/900" in reason


@pytest.mark.parametrize(
    ("login", "kind"),
    [("someone", "User"), ("github-actions", "User"), ("github-actions-bot", "Bot")],
    ids=["person", "lookalike-login", "other-bot"],
)
def test_same_title_by_a_non_bot_author_is_ignored(login: str, kind: str) -> None:
    plan, reason = run_decide([issue(THREE, login=login, kind=kind)])
    assert plan is None
    assert "No open main-red issue" in reason


def test_unlabelled_closed_or_pull_request_lookalikes_are_ignored() -> None:
    unlabelled = {**issue(THREE), "labels": [{"name": "bug"}]}
    closed = {**issue(THREE), "state": "closed"}
    pull = {**issue(THREE), "pull_request": {"url": "x"}}
    other_title = {**issue(THREE), "title": "Main was red"}
    assert run_decide([unlabelled, closed, pull, other_title])[0] is None


def test_the_oldest_bot_issue_wins_over_a_hostile_newer_one() -> None:
    plan, _ = run_decide([issue(THREE[:1], number=950), issue(THREE[1:], number=901)])
    assert plan == {"issue": 901, "nodeids": THREE[1:]}


@pytest.mark.parametrize(
    "hostile",
    [
        "test/test_a.py::test_x; rm -rf /",
        "test/test_a.py::test_x$(id)",
        "test/test_a.py::test_x[`id`]",
        "test/../etc/test_a.py::test_x",
        "/etc/test_a.py::test_x",
        "test/test_a.py",
        "test/",
        "test/test_a.py::",
        "docs/test_a.py::test_x",
        "test/test_a.py::test_x[a'b]",
        "test/test_a.py::test_x\nFAILED",
        "${{ github.token }}",
    ],
    ids=[
        "semicolon",
        "subshell",
        "backtick",
        "dotdot",
        "absolute",
        "file",
        "directory",
        "empty-name",
        "docs",
        "quote",
        "newline",
        "expression",
    ],
)
def test_a_malformed_or_hostile_id_is_never_deselected(hostile: str) -> None:
    assert not SKIP.valid_nodeid(hostile)
    plan, reason = run_decide([issue([THREE[0], hostile])])
    # The hostile id runs; the valid one is still deselected.
    assert plan == {"issue": 900, "nodeids": [THREE[0]]}
    assert "1 id(s) failed validation and run" in reason


def test_only_hostile_ids_runs_everything() -> None:
    plan, _ = run_decide([issue(["test/x.py::t; id"])])
    assert plan is None


def test_a_body_without_the_watcher_record_is_not_the_watchers_issue() -> None:
    plan, reason = run_decide([issue(THREE, body="- `test/test_alpha.py::test_one`")])
    assert plan is None
    assert "No open main-red issue" in reason


def test_an_xdist_group_id_is_kept_with_its_suffix() -> None:
    grouped = "test/test_grouped.py::test_scan@tree_scan_grouped"
    assert SKIP.valid_nodeid(grouped)
    assert SKIP.without_group(grouped) == "test/test_grouped.py::test_scan"
    assert SKIP.without_group("test/t.py::t[user@host]") == "test/t.py::t[user@host]"
    assert run_decide([issue([grouped])])[0] == {"issue": 900, "nodeids": [grouped]}


def test_over_the_cap_deselects_nothing() -> None:
    many = [f"test/test_many.py::test_{n}" for n in range(SKIP.MAX_DESELECT + 1)]
    plan, reason = run_decide([issue(many)])
    assert plan is None
    assert "over the cap" in reason
    at_cap = many[: SKIP.MAX_DESELECT]
    assert run_decide([issue(at_cap)])[0] == {"issue": 900, "nodeids": at_cap}


@pytest.mark.parametrize("event", ["push", "merge_group", "workflow_dispatch"])
def test_main_push_and_merge_group_run_everything(event: str) -> None:
    plan, reason = run_decide([issue(THREE)], event=event)
    assert plan is None
    assert "full suite" in reason


def test_a_release_base_runs_everything() -> None:
    assert run_decide([issue(THREE)], base_ref="release/0.13")[0] is None


@pytest.mark.parametrize("label", ["main-red-fix", "ci-full-run"])
def test_a_fix_or_full_run_label_runs_everything(label: str) -> None:
    plan, reason = run_decide([issue(THREE)], labels=["bug", label])
    assert plan is None
    assert label in reason


@pytest.mark.parametrize(
    "body",
    ["Fixes #900", "fix for #900.", f"see https://github.com/{REPO}/issues/900"],
    ids=["fixes", "mention", "url"],
)
def test_a_pr_body_that_names_the_issue_runs_everything(body: str) -> None:
    plan, reason = run_decide([issue(THREE)], pr_body=body)
    assert plan is None
    assert "names" in reason


@pytest.mark.parametrize(
    "body",
    ["Fixes #9000", "Fixes #90", "x#900", "/pull/900"],
    ids=["longer", "shorter", "glued", "pull"],
)
def test_a_body_naming_another_number_does_not_count(body: str) -> None:
    assert run_decide([issue(THREE)], pr_body=body)[0] is not None


def test_a_test_file_the_pr_changes_still_runs() -> None:
    plan, reason = run_decide([issue(THREE)], changed_files={"test/test_alpha.py"})
    assert plan == {"issue": 900, "nodeids": THREE[1:]}
    assert "1 id(s) are in files this PR changes" in reason


def test_unknown_changed_files_runs_everything() -> None:
    assert run_decide([issue(THREE)], changed_files=None)[0] is None


# -- cmd_decide: the step's output, including the API-error path -------------


def decide_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "EVENT_NAME": "pull_request",
        "BASE_BRANCH": "main",
        "BASE_SHA": "",
        "LABELS": "[]",
        "PR_BODY": "",
        "REPO": REPO,
        "GH_TOKEN": "t",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    env.update(extra)
    return env


def test_an_api_error_runs_everything_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError("u", 403, "rate limit", None, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(SKIP.urllib.request, "urlopen", refuse)
    assert SKIP.cmd_decide(decide_env(tmp_path)) == 0
    assert (tmp_path / "out").read_text() == "main_red_skip=\n"
    assert (
        "::warning::Could not read the main-red issue (HTTPError: HTTP Error 403: rate limit)"
        in capsys.readouterr().out
    )
    assert "full suite" in (tmp_path / "summary").read_text()


def test_a_non_list_api_answer_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        SKIP, "fetch_open_issues", lambda *_a: (_ for _ in ()).throw(ValueError("x"))
    )
    SKIP.cmd_decide(decide_env(tmp_path))
    assert (tmp_path / "out").read_text() == "main_red_skip=\n"


def test_the_step_writes_the_plan_for_a_bot_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SKIP, "fetch_open_issues", lambda *_a: [issue(THREE)])
    monkeypatch.setattr(SKIP, "changed_paths", lambda _sha: set())
    SKIP.cmd_decide(decide_env(tmp_path))
    out = (tmp_path / "out").read_text()
    assert out.startswith("main_red_skip=") and out.endswith("\n") and out.count("\n") == 1
    assert json.loads(out[len("main_red_skip=") :]) == {"issue": 900, "nodeids": THREE}
    assert "Main-red skip: Deselecting 3 test(s)" in (tmp_path / "summary").read_text()


def test_a_push_run_never_calls_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> Any:
        raise AssertionError("a push run must not read the issue")

    monkeypatch.setattr(SKIP, "fetch_open_issues", boom)
    SKIP.cmd_decide(decide_env(tmp_path, EVENT_NAME="push", BASE_BRANCH=""))
    assert (tmp_path / "out").read_text() == "main_red_skip=\n"


def test_changed_paths_refuses_a_non_sha_base() -> None:
    assert SKIP.changed_paths("") is None
    assert SKIP.changed_paths("HEAD; id") is None


# -- the pytest plugin half -------------------------------------------------


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[Any] = []

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected.extend(items)


class _Config:
    def __init__(self, worker: bool = False) -> None:
        self.hook = _Hook()
        if worker:
            self.workerinput = {"workerid": "gw0"}


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


def configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str | None, worker: bool = False
) -> _Config:
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    if raw is None:
        monkeypatch.delenv(SKIP.ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(SKIP.ENV_NAME, raw)
    config = _Config(worker)
    SKIP.pytest_configure(config)
    return config


def collect(config: _Config, nodeids: list[str]) -> list[str]:
    items = [_Item(nodeid) for nodeid in nodeids]
    SKIP.pytest_collection_modifyitems(None, config, items)
    return [item.nodeid for item in items]


COLLECTED = [
    "test/test_alpha.py::test_one",
    "test/test_alpha.py::test_one_more",
    "test/sub/test_beta.py::TestBeta::test_two[case-1]",
    "test/sub/test_beta.py::TestBeta::test_two[case-2]",
]


def test_the_plugin_deselects_exact_ids_and_lists_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = json.dumps({"issue": 900, "nodeids": THREE})
    config = configure(monkeypatch, tmp_path, raw)
    kept = collect(config, COLLECTED)
    # Exact ids only: the sibling test and the other parameter still run.
    assert kept == [
        "test/test_alpha.py::test_one_more",
        "test/sub/test_beta.py::TestBeta::test_two[case-2]",
    ]
    assert [item.nodeid for item in config.hook.deselected] == [COLLECTED[0], COLLECTED[2]]
    # pytest runs unprivileged on a Linux shard: the plugin never writes the summary.
    assert not (tmp_path / "summary").exists()


def summary_env(tmp_path: Path, raw: str | None) -> dict[str, str]:
    env = {"GITHUB_REPOSITORY": REPO, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}
    if raw is not None:
        env[SKIP.ENV_NAME] = raw
    return env


def test_the_summary_step_lists_the_ids_and_links_the_issue(tmp_path: Path) -> None:
    raw = json.dumps({"issue": 900, "nodeids": THREE})
    assert SKIP.cmd_summary(summary_env(tmp_path, raw)) == 0
    summary = (tmp_path / "summary").read_text()
    assert "### Deselected: 3 test(s) red on main" in summary
    assert f"https://github.com/{REPO}/issues/900" in summary
    for nodeid in THREE:
        assert f"- `{nodeid}`" in summary


@pytest.mark.parametrize(
    "raw",
    [None, "", "not json", json.dumps({"issue": 900, "nodeids": ["test/"]})],
    ids=["unset", "empty", "garbage", "a-directory"],
)
def test_the_summary_step_writes_nothing_and_passes_on_no_plan(
    tmp_path: Path, raw: str | None
) -> None:
    assert SKIP.cmd_summary(summary_env(tmp_path, raw)) == 0
    assert not (tmp_path / "summary").exists()


def test_the_plugin_matches_an_xdist_group_id_on_either_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = json.dumps({"issue": 900, "nodeids": ["test/test_g.py::test_scan@tree_scan_g"]})
    config = configure(monkeypatch, tmp_path, raw)
    kept = collect(
        config,
        ["test/test_g.py::test_scan", "test/test_g.py::test_other", "test/test_g.py::test_scan@x"],
    )
    assert kept == ["test/test_g.py::test_other"]


def test_an_xdist_worker_deselects_the_same_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = configure(
        monkeypatch, tmp_path, json.dumps({"issue": 900, "nodeids": THREE}), worker=True
    )
    assert collect(config, COLLECTED) == [COLLECTED[1], COLLECTED[3]]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        json.dumps({"issue": 900, "nodeids": THREE + ["test/../x.py::t"]}),
        json.dumps({"issue": 900, "nodeids": ["test/"]}),
        json.dumps({"issue": 900, "nodeids": [f"test/t.py::t_{n}" for n in range(51)]}),
        json.dumps({"issue": True, "nodeids": THREE}),
        json.dumps({"issue": 900, "nodeids": []}),
        json.dumps([THREE]),
    ],
    ids=[
        "unset",
        "empty",
        "garbage",
        "one-hostile",
        "a-directory",
        "over-cap",
        "bad-issue",
        "no-ids",
        "list",
    ],
)
def test_the_plugin_deselects_nothing_on_a_bad_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str | None
) -> None:
    config = configure(monkeypatch, tmp_path, raw)
    assert collect(config, COLLECTED) == COLLECTED
    assert not (tmp_path / "summary").exists()


# -- the workflow wiring ----------------------------------------------------


def _ci() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_the_changes_job_reads_the_issue_once_and_fails_open() -> None:
    changes = _ci()["jobs"]["changes"]
    assert changes["permissions"] == {"contents": "read", "issues": "read"}
    assert changes["outputs"]["main_red_skip"] == "${{ steps.main-red.outputs.main_red_skip }}"
    step = next(step for step in changes["steps"] if step.get("id") == "main-red")
    assert step["continue-on-error"] is True
    assert step["run"].strip() == "python3 scripts/ci_main_red_skip.py decide"
    # Untrusted text reaches the script only as env values.
    assert "${{" not in step["run"]
    assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"


@pytest.mark.parametrize("job", ["backend-test", "backend-test-windows"])
def test_each_backend_shard_lists_the_skips_before_its_tests(job: str) -> None:
    steps = _ci()["jobs"][job]["steps"]
    names = [step.get("name", "") for step in steps]
    summary = names.index("List the tests skipped as red on main")
    assert summary < next(i for i, name in enumerate(names) if name.startswith("Run tests"))
    step = steps[summary]
    assert step["env"]["CI_MAIN_RED_SKIP"] == "${{ needs.changes.outputs.main_red_skip }}"
    assert step["run"].split()[1:] == ["scripts/ci_main_red_skip.py", "summary"]
    # The default shell, not ci-shell: the summary file is the runner identity's.
    assert "ci-shell" not in str(step.get("shell", ""))


@pytest.mark.parametrize("job", ["backend-test", "backend-test-windows"])
def test_each_backend_shard_loads_the_plugin_with_the_plan_in_env(job: str) -> None:
    steps = _ci()["jobs"][job]["steps"]
    runs = [
        step
        for step in steps
        if "pytest" in str(step.get("run", "")) and "--file-shards" in str(step.get("run", ""))
    ]
    assert runs
    for step in runs:
        assert step["env"]["CI_MAIN_RED_SKIP"] == "${{ needs.changes.outputs.main_red_skip }}"
        assert "-p scripts.ci_main_red_skip" in step["run"]
        assert "main_red_skip" not in step["run"].replace("scripts.ci_main_red_skip", "")
