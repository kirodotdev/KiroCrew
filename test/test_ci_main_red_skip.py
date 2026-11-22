"""PR CI deselects only the exact tests the bot's open main-red issue names.

`scripts/ci_main_red_skip.py` reads the issue once in ci.yml's `changes` job
and a pytest plugin deselects its node ids on each backend shard. Every case
below pins the scope chosen: what runs and what is deselected. The issue body
is built by the watcher's own renderer (`.github/scripts/main_red_nodeids.py`),
so a change to its format fails here rather than silently running nothing.
"""

from __future__ import annotations

import email.message
import functools
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


# -- the issue read: rate limits, the bounded wait, and the fail-open after it --

NOW = 1_800_000_000
TOKEN = "synthetic-token-value"
PRIMARY = {"x_ratelimit_remaining": "0", "x_ratelimit_reset": str(NOW + 120)}


def answer(status: int, body: Any = "", **headers: str) -> tuple[int, dict[str, str], bytes]:
    raw = body if isinstance(body, str) else json.dumps(body)
    return status, {name.replace("_", "-"): value for name, value in headers.items()}, raw.encode()


def refused(status: int = 403, /, **headers: str) -> tuple[int, dict[str, str], bytes]:
    """A refusal with the given headers, or a spent primary limit that resets in 120 s."""
    return answer(status, "", **(headers or PRIMARY))


class FakeApi:
    """Scripted answers, one queue per identity; no network and no real clock."""

    def __init__(self, token: list[Any], anonymous: list[Any] | None = None) -> None:
        self.queues = {"token": list(token), "anonymous": list(anonymous or [refused()])}
        self.calls: list[str] = []
        self.now = NOW
        self.sleeps: list[float] = []
        self.logs: list[str] = []

    def send(self, url: str, token: str) -> Any:
        assert url.startswith(f"https://api.github.com/repos/{REPO}/issues?")
        who = "token" if token else "anonymous"
        assert token in ("", TOKEN)
        self.calls.append(who)
        reply = self.queues[who].pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += int(seconds)

    def fetch(self) -> list[Any]:
        result: list[Any] = SKIP.fetch_open_issues(
            REPO,
            TOKEN,
            send=self.send,
            clock=lambda: self.now,
            sleep=self.sleep,
            log=self.logs.append,
        )
        return result

    def decide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> tuple[str, str]:
        """Run the whole step over this API: ``(the step output value, its log)``."""
        wired = functools.partial(
            SKIP.fetch_open_issues,
            send=self.send,
            clock=lambda: self.now,
            sleep=self.sleep,
            log=self.logs.append,
        )
        monkeypatch.setattr(SKIP, "fetch_open_issues", wired)
        monkeypatch.setattr(SKIP, "changed_paths", lambda _sha: set())
        assert SKIP.cmd_decide(decide_env(tmp_path, GH_TOKEN=TOKEN)) == 0
        out = (tmp_path / "out").read_text(encoding="utf-8")
        assert out.startswith("main_red_skip=") and out.count("\n") == 1
        log = capsys.readouterr().out + "\n".join(self.logs)
        assert TOKEN not in log + (tmp_path / "summary").read_text(encoding="utf-8")
        return out[len("main_red_skip=") :].strip(), log


OK = answer(200, [issue(THREE)])
PLAN = {"issue": 900, "nodeids": THREE}


def test_a_spent_limit_then_a_good_read_applies_the_skip_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi([refused(), OK])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert json.loads(value) == PLAN
    assert api.calls == ["token", "anonymous", "token"]
    assert api.sleeps == [120 + SKIP.RESET_MARGIN_SECONDS]
    assert "primary rate limit; waiting 125s" in log


def test_a_read_without_the_token_is_tried_before_any_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi([refused()], anonymous=[OK])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert json.loads(value) == PLAN
    assert api.calls == ["token", "anonymous"]
    assert api.sleeps == []
    assert "primary rate limit on the token; read without it" in log


def test_a_reset_past_the_budget_runs_everything_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    far = refused(x_ratelimit_remaining="0", x_ratelimit_reset=str(NOW + 740))
    api = FakeApi([far])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert api.sleeps == []
    assert (
        "::warning::Could not read the main-red issue (RateLimited: primary rate limit resets "
        "in 745s, past what is left of the 300s wait budget (0s used); the read without a "
        "token got HTTP 403): full suite." in log
    )
    assert "full suite" in (tmp_path / "summary").read_text(encoding="utf-8")


def test_a_limit_that_outlasts_the_waits_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    short = refused(retry_after="100")
    api = FakeApi([short, short, short, OK])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert api.calls == ["token", "anonymous", "token", "token"]
    assert api.sleeps == [100, 100]
    assert "RateLimited: secondary rate limit still set after 3 reads and 200s" in log


def test_the_waits_of_one_run_share_one_budget() -> None:
    long = refused(retry_after="200")
    api = FakeApi([long, long, OK])
    with pytest.raises(SKIP.RateLimited, match=r"budget \(200s used\)"):
        api.fetch()
    assert api.sleeps == [200]
    assert sum(api.sleeps) <= SKIP.MAX_WAIT_SECONDS


@pytest.mark.parametrize(
    "denied",
    [answer(403), answer(403, {"message": "Resource not accessible by integration"})],
    ids=["bare", "permission-message"],
)
def test_a_403_that_names_no_rate_limit_fails_open_at_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    denied: tuple[int, dict[str, str], bytes],
) -> None:
    api = FakeApi([denied, OK], anonymous=[OK])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert api.calls == ["token"]
    assert api.sleeps == []
    assert "IssueReadError: HTTP 403 that names no rate limit, so not retried" in log


@pytest.mark.parametrize("status", [401, 404, 500, 502])
def test_another_http_error_is_not_retried(status: int) -> None:
    api = FakeApi([answer(status, x_ratelimit_remaining="0", retry_after="1"), OK])
    with pytest.raises(SKIP.IssueReadError, match=f"HTTP {status}"):
        api.fetch()
    assert api.calls == ["token"] and api.sleeps == []


def test_a_429_waits_for_its_retry_after() -> None:
    api = FakeApi([refused(429, retry_after="30"), OK])
    assert api.fetch() == [issue(THREE)]
    assert api.sleeps == [30]


@pytest.mark.parametrize(
    ("first", "sleeps"),
    [
        (refused(x_ratelimit_remaining="0", x_ratelimit_reset=str(NOW - 3600)), [5]),
        (refused(x_ratelimit_remaining="0", x_ratelimit_reset=str(NOW)), [5]),
        (refused(retry_after="0"), [1]),
        (refused(retry_after="soon"), [60]),
        (refused(retry_after="-5"), [60]),
        (answer(429), [60]),
        (answer(403, {"message": "You have exceeded a secondary rate limit."}), [60]),
        (answer(403, {"message": "API rate limit exceeded for installation."}), [60]),
    ],
    ids=[
        "reset-long-past",
        "reset-now",
        "retry-after-zero",
        "retry-after-words",
        "retry-after-negative",
        "bare-429",
        "secondary-in-body",
        "primary-in-body-only",
    ],
)
def test_an_odd_rate_limit_answer_waits_a_bounded_time(
    first: tuple[int, dict[str, str], bytes], sleeps: list[int]
) -> None:
    api = FakeApi([first, OK])
    assert api.fetch() == [issue(THREE)]
    assert api.sleeps == sleeps
    assert 0 < sleeps[0] <= SKIP.MAX_WAIT_SECONDS


@pytest.mark.parametrize(
    ("headers", "why"),
    [
        ({"x_ratelimit_reset": str(10**11)}, "past what is left"),
        ({"x_ratelimit_reset": "the whole budget away"}, "past what is left"),
        ({"x_ratelimit_reset": "9" * 40}, "no readable reset time"),
        ({"x_ratelimit_reset": "tomorrow"}, "no readable reset time"),
        ({"x_ratelimit_reset": "-1"}, "no readable reset time"),
        ({"x_ratelimit_reset": "1e9"}, "no readable reset time"),
        ({}, "no readable reset time"),
        ({"retry_after": "86400"}, "past what is left"),
    ],
    ids=[
        "absurdly-far",
        "one-margin-over",
        "too-long",
        "words",
        "negative",
        "float",
        "absent",
        "retry-after-a-day",
    ],
)
def test_an_unusable_reset_time_fails_open_without_waiting(
    headers: dict[str, str], why: str
) -> None:
    if "retry_after" not in headers:
        headers = {"x_ratelimit_remaining": "0", **headers}
    if headers.get("x_ratelimit_reset") == "the whole budget away":
        # The margin added to a reset puts this one just past the budget.
        headers["x_ratelimit_reset"] = str(NOW + SKIP.MAX_WAIT_SECONDS)
    api = FakeApi([refused(**headers), OK])
    with pytest.raises(SKIP.RateLimited, match=why):
        api.fetch()
    assert api.sleeps == []
    assert api.calls == ["token", "anonymous"]


PARTIAL = [
    answer(200, json.dumps([issue(THREE)])[:-40]),
    answer(200, {"message": "ok", "items": [issue(THREE)]}),
    answer(200, [issue(THREE)], link='<https://api.github.com/x?page=2>; rel="next"'),
    answer(200, ""),
    (200, {}, b"\xff\xfe["),
]
PARTIAL_IDS = ["truncated-json", "not-a-list", "a-second-page", "empty", "not-utf8"]


@pytest.mark.parametrize("partial", PARTIAL, ids=PARTIAL_IDS)
@pytest.mark.parametrize("path", ["first-read", "after-a-wait", "without-token"])
def test_a_partial_answer_on_any_read_runs_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    partial: tuple[int, dict[str, str], bytes],
    path: str,
) -> None:
    api = {
        "first-read": FakeApi([partial, OK], anonymous=[OK]),
        "after-a-wait": FakeApi([refused(), partial, OK]),
        "without-token": FakeApi([refused(), OK], anonymous=[partial]),
    }[path]
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert "Could not read the main-red issue (" in log and "full suite" in log
    # A partial answer ends the read: no later answer is asked for.
    assert api.queues["token"][-1] is OK


def test_an_issue_without_its_state_record_after_a_wait_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = answer(200, [issue(THREE, body="- `test/test_alpha.py::test_one`")])
    value, log = FakeApi([refused(), bare]).decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert "No open main-red issue" in log


def test_a_network_error_is_not_retried_and_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi([urllib.error.URLError("no route"), OK])
    value, log = api.decide(tmp_path, monkeypatch, capsys)
    assert value == ""
    assert api.calls == ["token"] and api.sleeps == []
    assert "Could not read the main-red issue (URLError:" in log


def test_a_failed_read_without_the_token_still_waits_for_the_token() -> None:
    api = FakeApi([refused(), OK], anonymous=[TimeoutError("slow")])
    assert api.fetch() == [issue(THREE)]
    assert api.calls == ["token", "anonymous", "token"]
    assert api.sleeps == [125]


def test_an_empty_token_or_an_odd_repo_never_calls_the_api() -> None:
    api = FakeApi([OK])
    with pytest.raises(SKIP.IssueReadError, match="GH_TOKEN is empty"):
        SKIP.fetch_open_issues(REPO, "", send=api.send)
    with pytest.raises(ValueError, match="unexpected shape"):
        SKIP.fetch_open_issues("a/b/../c", TOKEN, send=api.send)
    assert api.calls == []


def _http_error(status: int, headers: dict[str, str] | None, body: bytes) -> urllib.error.HTTPError:
    message = None
    if headers is not None:
        message = email.message.Message()
        for name, value in headers.items():
            message[name] = value
    return urllib.error.HTTPError("u", status, "Forbidden", message, io.BytesIO(body))  # type: ignore[arg-type]


def test_the_transport_reports_a_refusal_and_sends_the_token_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def refuse(request: Any, **_kwargs: Any) -> Any:
        seen.append(request)
        raise _http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "12"}, b"spent")

    monkeypatch.setattr(SKIP.urllib.request, "urlopen", refuse)
    url = "https://api.github.com/repos/o/r/issues"
    reply = SKIP._send(url, TOKEN)
    assert reply == (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "12"}, b"spent")
    assert SKIP.rate_limit(reply, 0) == ("primary", 12 + SKIP.RESET_MARGIN_SECONDS)
    SKIP._send(url, "")
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert seen[1].get_header("Authorization") is None


def test_a_refusal_with_no_headers_runs_everything_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise _http_error(403, None, b"")

    monkeypatch.setattr(SKIP.urllib.request, "urlopen", refuse)
    assert SKIP.cmd_decide(decide_env(tmp_path)) == 0
    assert (tmp_path / "out").read_text(encoding="utf-8") == "main_red_skip=\n"
    assert (
        "::warning::Could not read the main-red issue (IssueReadError: HTTP 403 that names no "
        "rate limit, so not retried): full suite." in capsys.readouterr().out
    )
    assert "full suite" in (tmp_path / "summary").read_text(encoding="utf-8")


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


def test_the_wait_budget_fits_the_changes_job() -> None:
    changes = _ci()["jobs"]["changes"]
    step = next(step for step in changes["steps"] if step.get("id") == "main-red")
    # The script bounds its own wait; the step sets no timeout that could end it early.
    assert "timeout-minutes" not in step
    reads = SKIP.MAX_TOKEN_READS + 1
    longest = SKIP.MAX_WAIT_SECONDS + reads * SKIP.REQUEST_TIMEOUT
    # At least half the job's timeout is left for its full-history checkout.
    assert longest <= changes["timeout-minutes"] * 60 // 2
    assert SKIP.SECONDARY_WAIT_SECONDS + SKIP.RESET_MARGIN_SECONDS <= SKIP.MAX_WAIT_SECONDS


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
