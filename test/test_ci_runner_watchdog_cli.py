"""The entry point of ``scripts/ci/runner_watchdog.py``: what it reads, writes and exits with.

API text is untrusted, so no line it carries can become a workflow command or
break out of its step-summary row. The summary names every run the tick acted
on or held. The parser reads the workflow's environment, the HTTP client
counts and retries its calls, a kicked tick stands down when another tick ran
inside the schedule interval, and a GitHub rate limit ends the tick with a
non-zero exit instead of a crash.
"""

from __future__ import annotations

import email.message
import http.client
import io
import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from ci_runner_watchdog_helpers import (
    NOW,
    REPO,
    WORKFLOW,
    FakeApi,
    _cancelled_orphan_job,
    _Clock,
    _job,
    _policy,
    _run,
    _saturation_band_api,
    _sweep,
    _tick,
    _verdict_of,
    wd,
)

# ── API text is untrusted: no line it carries may become a workflow command ──


FORGED_NAME = "build\n::warning::forged by a fork\r::add-mask::x\x1b[31m"


def test_control_characters_in_api_text_never_reach_a_log_line_unescaped() -> None:
    job = _job(11)
    job["name"] = FORGED_NAME
    job["labels"] = [job["labels"][0] + "\n::error::label"]
    run = _run(1, fork=True, branch="feature\u2028branch")
    api = FakeApi({"in_progress": [run]}, {1: [job]})
    lines: list[str] = []
    verdicts, _outcomes = wd.run_watchdog(
        api, _policy(dry_run=True), clock=_Clock().now, sleep=lambda _s: None, log=lines.append
    )
    assert lines, "the sweep logged nothing"
    for line in lines:
        assert "\n" not in line and "\r" not in line and "\x1b" not in line, repr(line)
        assert "\u2028" not in line, repr(line)
    assert not any(line.startswith("::") and "forged" in line for line in lines)
    assert any("::warning::forged" in line for line in lines), "the name was dropped, not escaped"
    orphan = _verdict_of(verdicts, 1).orphans[0]
    assert orphan.name == "build\\n::warning::forged by a fork\\r::add-mask::x\\x1b[31m"
    assert orphan.labels[0].startswith("codebuild-"), "escaping must not disturb the label test"


def test_the_step_summary_keeps_every_api_string_on_its_own_row() -> None:
    job = _job(11)
    job["name"] = FORGED_NAME
    api = FakeApi({"in_progress": [_run(1, branch="feat\nure")]}, {1: [job]})
    verdicts, outcomes = _sweep(api, dry_run=True)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    rows = [line for line in summary.splitlines() if line.startswith("| [1]")]
    assert len(rows) == 1, summary
    assert "::warning::forged" in rows[0] and "feat\\\\nure" in rows[0]


def test_the_step_summary_markdown_escapes_every_api_string() -> None:
    """A fork's branch name is chosen by whoever opened the fork; it must render as
    literal text, never as a link, a heading, or an extra table cell."""
    branch = "x`) | forged | [click](https://evil.invalid) # *bold* <b>"
    job = _job(11)
    job["name"] = "job | [link](https://evil.invalid) `tick`"
    fork = _run(2, fork=True, branch=branch)
    fork_job = _job(21, run_id=2)
    api = FakeApi({"in_progress": [_run(1, branch=branch), fork]}, {1: [job], 2: [fork_job]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "[click](https://evil.invalid)" not in summary
    assert "[link](https://evil.invalid)" not in summary
    assert "\\[click\\]\\(https://evil.invalid\\)" in summary
    assert "\\`tick\\`" in summary and "<b>" not in summary and "\\<b\\>" in summary
    rows = [line for line in summary.splitlines() if line.startswith("| [1]")]
    assert len(rows) == 1 and rows[0].count(" | ") == 5, rows  # still exactly six cells
    reported = [line for line in summary.splitlines() if line.startswith("- [2]")]
    assert len(reported) == 1 and "\\| forged \\|" in reported[0], reported


def test_the_step_summary_link_cannot_be_closed_by_the_url() -> None:
    run = _run(1)
    run["html_url"] = "https://example.invalid/runs/1) [forged](https://evil.invalid"
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "[forged](https://evil.invalid" not in summary
    assert "[1](https://example.invalid/runs/1%29%20%5Bforged%5D%28https://evil.invalid)" in summary


def test_api_error_bodies_are_escaped_before_they_are_logged() -> None:
    exc = wd.ApiError(403, "denied\n::error::forged")
    assert "\n" not in str(exc)
    assert str(exc) == "HTTP 403: denied\\n::error::forged"


# ── the summary ─────────────────────────────────────────────────────────────


def test_the_summary_names_every_acted_and_reported_run() -> None:
    api = FakeApi(
        {
            "in_progress": [_run(1), _run(2, fork=True)],
            "pending": [_run(3, status="pending", branch="other")],
        },
        {1: [_job(11, large=True)], 2: [_job(21, run_id=2)], 3: []},
    )
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert "## CI runner watchdog" in summary
    assert "| [1](https://example.invalid/runs/1) | 1 | main |" in summary
    assert "job-11 (60 min)" in summary
    assert wd.OUTCOME_HEALED in summary
    assert "[2](https://example.invalid/runs/2)" in summary and wd.SKIPPED_FORK in summary
    assert "[3](https://example.invalid/runs/3)" in summary and wd.WAITING_ON_GROUP in summary
    assert "re-run mode" not in summary


def test_the_summary_reports_a_saturated_hold() -> None:
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {
            1: [_job(11)],
            2: [
                _job(
                    21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r"
                )
            ],
        },
    )
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.SKIPPED_SATURATED in summary and "dispatching slowly" in summary


def test_the_summary_reports_a_partial_evidence_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The partial hold must reach the summary, or a held tick renders "Nothing stuck.".

    This is the only path that renders SKIPPED_PARTIAL_EVIDENCE through
    `render_summary`, so without it dropping the verdict from that function's
    `reported` tuple reds nothing and a tick that deliberately held reports itself as
    quiet -- the regression this pins.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    # The unread run is affordable to read, and a tick with an orphan reads it
    # (`top_up_unread_evidence`); this pins the hold itself, so the top-up is priced out.
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 0)
    # On another fleet (`instance-size:large`): this test pins the label-blind path, and
    # a prompt start on the orphan's OWN queue would take the saturation question
    # (though not this hold) down the own-queue path instead.
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=19,
        runner_name="r",
        run_id=2,
        large=True,
    )
    api = FakeApi(
        {
            "in_progress": [
                _run(1),
                _run(500, minutes_ago=30, status="queued", branch="older"),
                _run(2, minutes_ago=20, branch="other"),
            ]
        },
        {1: [_job(11)], 2: [prompt], 500: []},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.SKIPPED_PARTIAL_EVIDENCE in summary
    assert "Nothing stuck." not in summary
    assert api.posts == []


def test_the_summary_reports_a_hold_taken_under_a_narrow_read_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold the watchdog deliberately took must not read as "Nothing stuck."."""
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    verdicts, outcomes = _sweep(_saturation_band_api())
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.SKIPPED_SATURATED in summary
    assert "Nothing stuck." not in summary


def test_a_quiet_sweep_says_so() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="completed")]})
    verdicts, outcomes = _sweep(api)
    summary = wd.render_summary(verdicts, outcomes, _policy(dry_run=True))
    assert "Nothing stuck." in summary
    assert "dry run" in summary


# ── the CLI surface the workflow drives ─────────────────────────────────────


def test_the_parser_reads_the_workflow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("DRY_RUN", "true")
    args = wd.build_parser().parse_args([])
    assert args.repo == REPO
    assert args.dry_run is True
    monkeypatch.setenv("DRY_RUN", "false")
    assert wd.build_parser().parse_args([]).dry_run is False
    # Thresholds are Policy defaults, not flags: the workflow varies only DRY_RUN.
    with pytest.raises(SystemExit):
        wd.build_parser().parse_args(["--orphan-after", "20"])
    policy = wd.Policy(repo=REPO, now=NOW)
    assert (policy.orphan_after, policy.max_runs, policy.heal_budget) == (
        timedelta(minutes=15),
        5,
        timedelta(seconds=300),
    )


def test_main_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert wd.main(["--repo", REPO]) == 2
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert wd.main([]) == 2


def test_a_cheap_rate_limit_reset_in_recovery_is_waited_out_not_aborted() -> None:
    """The re-raise must not cost a verdict a five-second wait would have saved.

    Making a rate-limited recovery read propagate stopped it exiting green, but it
    also sent a CHEAP reset -- one the gather phase simply waits out -- straight to
    the abort handler. For a cancelled run with less window left than one schedule
    interval there is no next tick, so a five-second wait was costing the verdict.
    Recovery's reads now go through the same wait-and-retry.
    """
    candidate = _run(
        1,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=85,
        branch="orphan",
    )
    api = FakeApi(
        {"cancelled": [candidate]},
        {1: [_cancelled_orphan_job(11, run_id=1)]},
        newest_by_branch={"orphan": 1},
    )
    _rate_limit_get(api, r"/actions/runs/1/jobs", times=1, retry_after=5.0)
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes.get(1) == wd.OUTCOME_RECOVERED
    assert wd.RATE_LIMIT_MARKER_ID not in outcomes
    assert clock.t >= 5.0


def test_a_recovery_mutation_is_never_retried_through_the_read_wrapper() -> None:
    """Reads get the retry; a cancel or re-run that may be on the wire never does."""
    posted: list[str] = []

    class _Recording:
        def get(self, path: str) -> Any:
            raise AssertionError("the wrapper must route reads through the given reader")

        def post(self, path: str) -> None:
            posted.append(path)

    reads: list[str] = []
    wrapper = wd._ReadsThrough(_Recording(), lambda path: reads.append(path) or {"ok": True})
    assert wrapper.get("repos/x/y") == {"ok": True}
    assert reads == ["repos/x/y"]
    wrapper.post("repos/x/y/cancel")
    assert posted == ["repos/x/y/cancel"]


def test_the_http_client_counts_its_calls_and_remembers_the_quota_it_was_told() -> None:
    """The watchdog's own footprint against the quota whose exhaustion started this.

    Logged, never enforced: a self-imposed call cap would silently stop healing,
    which is the failure mode this script exists to end. A response without headers
    is tolerated -- the reading is observability and must not crash a good read.
    """

    class _Response:
        def __init__(self, remaining: str | None) -> None:
            self.headers = email.message.Message()
            if remaining is not None:
                self.headers["X-RateLimit-Remaining"] = remaining

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    class _Headerless(_Response):
        def __init__(self) -> None:
            super().__init__(None)
            del self.headers

    seen: list[Any] = [_Response("4321"), _Response("4320"), _Headerless()]

    def opener(request: Any, timeout: float) -> Any:
        return seen.pop(0)

    client = wd.GitHubApi("token", "https://api.example.invalid", opener=opener)
    assert client.calls == 0 and client.rate_limit_remaining is None
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (1, "4321")
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (2, "4320")
    # A headerless response still counts, and does not erase the last reading.
    client.get("repos/x/y")
    assert (client.calls, client.rate_limit_remaining) == (3, "4320")


def test_the_http_client_retries_once_on_a_server_error() -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def opener(request: Any, timeout: float) -> Any:
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 502, "bad gateway", email.message.Message(), io.BytesIO(b"x")
            )
        return _Response(b'{"ok": true}')

    slept: list[float] = []
    client = wd.GitHubApi("token", "https://api.example.invalid", sleep=slept.append, opener=opener)
    assert client.get("repos/x/y") == {"ok": True}
    assert len(calls) == 2 and slept == [2]


def test_the_http_client_retries_a_timeout_while_reading_the_body_and_then_gives_an_api_error() -> (
    None
):
    """The connect succeeded and the READ timed out: not a URLError, but the same transient
    class -- retried once, and surfaced as ApiError when it repeats, never as a crash."""
    calls: list[str] = []

    class _Response:
        def __init__(self, body: bytes | None) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            if self._body is None:
                raise TimeoutError("timed out")
            return self._body

    def flaky(request: Any, timeout: float) -> Any:
        calls.append(request.full_url)
        return _Response(None if len(calls) == 1 else b'{"ok": true}')

    slept: list[float] = []
    client = wd.GitHubApi("token", "https://api.example.invalid", sleep=slept.append, opener=flaky)
    assert client.get("repos/x/y") == {"ok": True}
    assert len(calls) == 2 and slept == [2]

    def always(request: Any, timeout: float) -> Any:
        return _Response(None)

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=always
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.get("repos/x/y")
    assert excinfo.value.status == 0 and "TimeoutError" in str(excinfo.value)

    def truncated(request: Any, timeout: float) -> Any:
        return _Response(b'{"ok": tr')

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=truncated
    )
    with pytest.raises(wd.ApiError):
        client.get("repos/x/y")


def test_the_http_client_treats_a_partial_read_and_an_unreadable_error_body_as_transient() -> None:
    """``IncompleteRead`` is an HTTPException, not an OSError; and the body of an HTTP error
    is itself read over the network. Neither may escape the client as a crash."""

    class _Partial:
        def __enter__(self) -> "_Partial":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"{")

    class _Ok:
        def __enter__(self) -> "_Ok":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    calls = {"n": 0}

    def partial_then_ok(request: Any, timeout: float) -> Any:
        calls["n"] += 1
        return _Partial() if calls["n"] == 1 else _Ok()

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=partial_then_ok
    )
    assert client.get("repos/x/y") == {"ok": True}

    class _UnreadableBody(io.BytesIO):
        def read(self, *_a: Any) -> bytes:
            raise TimeoutError("timed out reading the error body")

    def forbidden_with_unreadable_body(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", email.message.Message(), _UnreadableBody(b"x")
        )

    client = wd.GitHubApi(
        "token",
        "https://api.example.invalid",
        sleep=lambda _s: None,
        opener=forbidden_with_unreadable_body,
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/rerun")
    assert excinfo.value.status == 403 and "unreadable" in str(excinfo.value)


def test_the_http_client_never_retries_a_mutation_whose_result_is_unknown() -> None:
    """A GET is retried on a 5xx, a lost response or a URLError; a POST is never retried --
    the request may have been on the wire, so its effect is unknown and a repeat could only
    turn a landed mutation into a conflict. The error is marked ambiguous for the caller."""

    class _Lost:
        def __enter__(self) -> "_Lost":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            raise TimeoutError("timed out")

    calls: list[str] = []

    def server_error(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        raise urllib.error.HTTPError(
            request.full_url, 502, "bad gateway", email.message.Message(), io.BytesIO(b"x")
        )

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=server_error
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.status == 502 and excinfo.value.ambiguous
    assert calls == ["POST"]  # not retried

    calls.clear()

    def lost_response(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        return _Lost()

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=lost_response
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.ambiguous and calls == ["POST"]

    calls.clear()

    def url_error_then_ok(request: Any, timeout: float) -> Any:
        calls.append(request.get_method())
        if len(calls) == 1:
            raise urllib.error.URLError("timed out")

        class _Ok:
            def __enter__(self) -> "_Ok":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        return _Ok()

    # A URLError can be a send/response timeout with the request already delivered,
    # so a mutation is ambiguous here too ...
    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=url_error_then_ok
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/cancel")
    assert excinfo.value.ambiguous and calls == ["POST"]

    calls.clear()
    # ... while a read is simply retried.
    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=url_error_then_ok
    )
    assert client.get("repos/x/y") == {"ok": True}
    assert calls == ["GET", "GET"]


def test_the_http_client_surfaces_a_client_error_with_its_status() -> None:
    def opener(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", email.message.Message(), io.BytesIO(b"nope")
        )

    client = wd.GitHubApi(
        "token", "https://api.example.invalid", sleep=lambda _s: None, opener=opener
    )
    with pytest.raises(wd.ApiError) as excinfo:
        client.post("repos/x/y/actions/runs/1/rerun")
    assert excinfo.value.status == 403


def test_the_scheduled_tick_is_armed() -> None:
    """Detection without action cost two six-hour `main` outages, so the schedule acts.

    A manual dispatch stays governed by its own `dry_run` input, which is what
    lets a maintainer inspect without cancelling; that coupling is asserted here
    too, because arming is only safe while it holds.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'WATCHDOG_ARMED: "true"' in text
    assert "WATCHDOG_ARMED != 'true'" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run" in text


class _TickListing:
    """A GitHub fake that serves only the watchdog's own run listing."""

    def __init__(
        self,
        runs: list[dict[str, Any]] | BaseException,
        *,
        refuse_post: BaseException | None = None,
    ) -> None:
        self.runs = runs
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.calls = 0
        self.rate_limit_remaining: str | None = None
        self._refuse_post = refuse_post

    def get(self, path: str) -> Any:
        self.gets.append(path)
        self.calls += 1
        assert path.startswith(f"repos/{REPO}/actions/workflows/{wd.WATCHDOG_WORKFLOW}/runs"), path
        if isinstance(self.runs, BaseException):
            raise self.runs
        return {"workflow_runs": self.runs}

    def post(self, path: str) -> None:
        self.posts.append(path)
        self.calls += 1
        if self._refuse_post is not None:
            raise self._refuse_post


def _tick_row(
    run_id: int,
    minutes_ago: float,
    status: str = "completed",
    *,
    conclusion: str | None = None,
    now: datetime = NOW,
) -> dict[str, Any]:
    created = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conclusion is None and status == "completed":
        conclusion = "success"
    return {"id": run_id, "status": status, "conclusion": conclusion, "created_at": created}


def _live_row(run_id: int, minutes_ago: float) -> dict[str, Any]:
    """A row for the main() tests, whose Policy.now is the real clock."""
    return _tick_row(run_id, minutes_ago, now=datetime.now(timezone.utc))


_INTERVAL = timedelta(minutes=10)


def _redundant(api: Any) -> str | None:
    return wd.kick_is_redundant(
        api, REPO, now=NOW, self_run_id=99, interval=_INTERVAL, log=lambda _m: None
    )


def test_a_kick_stands_down_when_another_tick_started_inside_the_schedule_interval() -> None:
    """The kick exists to fill the schedule's GAPS, not to multiply its ticks.

    On a day the schedule keeps its `*/10` cadence every Fast Gate completion
    would otherwise buy a full tick -- four repo-wide indexes and up to fifty job
    reads -- against the quota whose exhaustion started all this. A started tick
    inside the interval means this moment is covered; the kick costs its one read.
    """
    api = _TickListing([_tick_row(1, 3), _tick_row(2, 40)])
    reason = _redundant(api)
    assert reason is not None and "3 min ago" in reason and "stands down" in reason
    assert api.calls == 1


def test_a_kick_runs_when_the_newest_other_tick_is_older_than_the_interval() -> None:
    """The late-schedule shape: ticks an hour or more apart, an orphan ageing between them."""
    assert _redundant(_TickListing([_tick_row(1, 11), _tick_row(2, 100)])) is None


def test_the_kick_ignores_itself_and_ticks_that_have_not_started() -> None:
    """Two kicks must not stand down for each other.

    The concurrency group holds the later kick `queued` behind this one. Were a
    not-yet-started sibling counted, this kick would see it (seconds old) and stand
    down, then the sibling would see this one and stand down too: nobody runs. Only
    `in_progress` / `completed` ticks are evidence that a tick happened; this run's
    own row is never evidence.
    """
    api = _TickListing(
        [
            _tick_row(99, 0.5),  # self
            _tick_row(98, 0.2, status="queued"),
            _tick_row(97, 1, status="pending"),
            _tick_row(96, 1, status="waiting"),
            _tick_row(1, 45),
        ]
    )
    assert _redundant(api) is None
    # ...and an in_progress other tick inside the interval does count.
    assert _redundant(_TickListing([_tick_row(98, 2, status="in_progress")])) is not None


def test_an_empty_or_malformed_tick_listing_runs_the_kick() -> None:
    for runs in ([], [{"id": "x"}], [{"id": 5, "status": "completed"}]):
        assert _redundant(_TickListing(runs)) is None, runs


def test_a_chain_of_stood_down_kicks_does_not_debounce_the_next_one() -> None:
    """Busy day: Fast Gates finish every few minutes, the cron is late.

    K1 stood down (a tick 5 min before it), K2 stood down seeing K1, K3 ... If a
    stood-down kick counted as a tick, this chain would never end: every kick sees
    the one before it, inside the interval, and no full tick runs between crons --
    the kick would be off on exactly the days it is for. A stood-down kick cancels
    itself (`stand_down`), so its row is `cancelled` and does not count; the chain
    is judged against the last tick that RAN, 15 minutes ago, and this kick runs.
    A kick the concurrency group evicted while queued is `cancelled` too, and a
    `skipped` row never ran either.
    """
    rows = [
        _tick_row(1, 15),  # the last full tick (cron)
        _tick_row(2, 7, conclusion="cancelled"),  # K1 stood down
        _tick_row(3, 3, conclusion="cancelled"),  # K2 stood down
        _tick_row(4, 2, conclusion="cancelled"),  # a kick evicted while queued
        _tick_row(5, 1, conclusion="skipped"),
    ]
    assert _redundant(_TickListing(rows)) is None
    # The same chain with a full tick 7 minutes ago stands down -- it is the tick
    # that ran, not the kicks around it, that the debounce measures from.
    rows[1] = _tick_row(2, 7, conclusion="failure")  # ran, and found something red
    assert _redundant(_TickListing(rows)) is not None


def test_the_debounce_measures_from_run_start_not_queue_time() -> None:
    """A tick that sat queued 20 minutes behind a saturated hosted pool and started
    3 minutes ago covers this moment; its `created_at` alone would say it did not."""
    row = _tick_row(1, 23)
    row["run_started_at"] = (NOW - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _redundant(_TickListing([row])) is not None


def test_a_stood_down_kick_cancels_itself_and_waits_for_the_stop() -> None:
    api = _TickListing([])
    waited: list[float] = []
    clock = iter([0.0, 10.0, 70.0])
    wd.stand_down(
        api,
        REPO,
        self_run_id=99,
        reason="r",
        sleep=waited.append,
        clock=lambda: next(clock),
        log=lambda _m: None,
    )
    assert api.posts == [f"repos/{REPO}/actions/runs/99/cancel"]
    assert waited == [5.0]  # kept waiting while the clock was inside the bound, then gave up


def test_a_stood_down_kick_without_a_run_id_or_with_a_refused_cancel_still_exits_cleanly() -> None:
    api = _TickListing([])
    wd.stand_down(api, REPO, self_run_id=0, reason="r", sleep=pytest.fail, log=lambda _m: None)
    assert api.posts == []
    api = _TickListing([], refuse_post=wd.ApiError(403, "forbidden"))
    wd.stand_down(api, REPO, self_run_id=99, reason="r", sleep=pytest.fail, log=lambda _m: None)
    assert api.posts == [f"repos/{REPO}/actions/runs/99/cancel"]


def _main_with(
    monkeypatch: pytest.MonkeyPatch, api: Any, *, event: str, run_id: str = "99"
) -> tuple[int, list[Any]]:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    # `main()` defaults `--summary` to GITHUB_STEP_SUMMARY and APPENDS to it. Under
    # Actions that is the real job summary, so a helper-driven run must not inherit
    # it: cleared here, the summary goes to captured stdout and nothing is written.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(wd, "GitHubApi", lambda *_a, **_k: api)
    # A stood-down kick waits for its own cancel to stop the step; nothing stops a
    # test, so the wait is zeroed and the stand-down is read from the fake's posts.
    monkeypatch.setattr(wd, "STAND_DOWN_CANCEL_WAIT_SECONDS", 0.0)
    ran: list[Any] = []

    def fake_run(_api: Any, policy: Any, **_k: Any) -> tuple[list[Any], dict[int, str]]:
        ran.append(policy)
        return [], {1: wd.OUTCOME_HEALED}

    monkeypatch.setattr(wd, "run_watchdog", fake_run)
    return wd.main(["--repo", REPO]), ran


def test_main_on_a_kick_debounces_before_any_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel_self = f"repos/{REPO}/actions/runs/99/cancel"
    api = _TickListing([_live_row(1, 3)])
    code, ran = _main_with(monkeypatch, api, event=wd.KICK_EVENT)
    assert code == 0 and ran == [] and api.gets and api.posts == [cancel_self]

    api = _TickListing([_live_row(1, 30)])
    code, ran = _main_with(monkeypatch, api, event=wd.KICK_EVENT)
    assert code == 0 and len(ran) == 1 and api.calls == 1 and api.posts == []


def test_main_on_a_schedule_or_dispatch_never_reads_the_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled tick is the contract; the kick is additive and must not reshape it."""
    for event in ("schedule", "workflow_dispatch", ""):
        api = _TickListing([_live_row(1, 1)])
        code, ran = _main_with(monkeypatch, api, event=event)
        assert code == 0 and len(ran) == 1 and api.calls == 0 and api.posts == [], event


def test_a_rate_limited_debounce_read_stands_the_kick_down_and_any_other_error_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limited debounce read means the full tick's first listing would abort
    red for nothing; standing down costs no quota and the next heartbeat retries.
    A transient 5xx is the schedule's own posture: run."""
    api = _TickListing(wd.ApiError(403, "API rate limit exceeded for installation"))
    code, ran = _main_with(monkeypatch, api, event=wd.KICK_EVENT)
    assert code == 0 and ran == [] and api.posts == [f"repos/{REPO}/actions/runs/99/cancel"]

    api = _TickListing(wd.ApiError(502, "bad gateway"))
    code, ran = _main_with(monkeypatch, api, event=wd.KICK_EVENT)
    assert code == 0 and len(ran) == 1


def test_a_kicked_tick_writes_its_stand_down_to_the_step_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_NAME", wd.KICK_EVENT)
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setattr(wd, "GitHubApi", lambda *_a, **_k: _TickListing([_live_row(1, 2)]))
    monkeypatch.setattr(wd, "STAND_DOWN_CANCEL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: pytest.fail("must not list"))
    summary = tmp_path / "summary.md"
    assert wd.main(["--repo", REPO, "--summary", str(summary)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("## CI runner watchdog") and "stood down" in text


def test_the_workflow_is_kicked_by_a_completed_fast_gate_and_its_kick_cannot_red_a_pull_request() -> (
    None
):
    """Two pins, one trigger.

    `workflow_run` on `Fast Gate` completing is the heartbeat the schedule lacks;
    the event name the workflow declares must be the one the script debounces on.
    And a `workflow_run` run carries the TRIGGERING run's head, so a kicked tick
    that reds would attach a red check to an unrelated pull request (what a hand
    dispatch on a PR branch once did): the step must swallow its own
    status on that event, and ONLY on that event, so scheduled ticks stay loud.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    entries = {
        keys: value.partition("#")[0].strip() for keys, value in wd._yaml_mapping_entries(text)
    }
    assert entries.get(("on", "workflow_run", "workflows")) == '["Fast Gate"]', entries
    assert entries.get(("on", "workflow_run", "types")) == "[completed]", entries
    assert wd.KICK_EVENT == "workflow_run"
    body = wd._workflow_without_full_line_comments(text)
    assert "continue-on-error: ${{ github.event_name == 'workflow_run' }}" in body
    assert body.count("continue-on-error:") == 1
    # The debounce window is the schedule's own interval, so the cron the
    # workflow declares and the interval the script debounces on must agree.
    assert '- cron: "*/10 * * * *"' in body
    assert wd.Policy.schedule_interval == _INTERVAL


def test_a_live_tick_under_the_read_bound_reads_every_run() -> None:
    runs = [_run(i, minutes_ago=100 - i, status="queued") for i in range(1, 4)]
    api = FakeApi({"queued": list(reversed(runs))}, {i: [] for i in range(1, 4)}, evidence=False)
    clock = _Clock()
    logged: list[str] = []
    wd.run_watchdog(api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append)
    assert not any("live job-read cap" in line for line in logged)


def test_a_live_tick_under_the_read_bound_behaves_as_before() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


# ── a GitHub rate limit ends the tick non-fatally, never as a crash ─────────


def _rate_limit_get(
    api: FakeApi, pattern: str, *, times: int, retry_after: float | None = None
) -> None:
    """Make the next ``times`` GETs matching ``pattern`` raise a rate-limited ApiError."""
    original_get = api.get
    left = {"n": times}

    def get(path: str) -> Any:
        if left["n"] > 0 and re.search(pattern, path):
            left["n"] -= 1
            raise wd.ApiError(
                403,
                "API rate limit exceeded for installation ID 12345",
                remaining="0",
                retry_after=retry_after,
            )
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_a_rate_limit_mid_listing_aborts_the_tick_and_preserves_already_decided_heals() -> None:
    """A 403 rate limit while reading run 2's jobs must not lose the tick: run 1, classified
    before it, is still healed, and the tick ends with the aborted outcome rather than a crash.
    Negative control: reverted code has no rate-limit handling, so the ApiError propagates out
    of run_watchdog and _sweep raises."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="a"), _run(2, minutes_ago=59, branch="b")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    _rate_limit_get(api, r"/actions/runs/2/jobs", times=1)  # one page fails, then serves
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes[1] == wd.OUTCOME_HEALED  # the already-classified orphan is still acted on
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in wd.FAILED_OUTCOMES
    assert any(v.verdict == wd.TICK_ABORTED_RATE_LIMITED for v in verdicts)
    assert any("rate limit" in line for line in logged)


def test_a_page_two_rate_limit_keeps_and_heals_page_one_candidates() -> None:
    orphan = _run(1, branch="orphan")
    filler = [
        _run(i, minutes_ago=5, branch=f"filler-{i}", workflow="issue-triage.yml")
        for i in range(2, wd.PAGE_SIZE + 1)
    ]
    api = FakeApi({"in_progress": [orphan] + filler}, {1: [_job(11)]})
    _rate_limit_get(api, r"/actions/runs\?.*page=2", times=1)
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes[1] == wd.OUTCOME_HEALED
    assert outcomes[wd.RATE_LIMIT_MARKER_ID] == wd.OUTCOME_ABORTED_RATE_LIMITED
    assert api.posts[:2] == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


def test_a_rate_limited_tick_that_classifies_nothing_exits_nonzero_and_annotates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    marker = wd.RunVerdict(
        run_id=wd.RATE_LIMIT_MARKER_ID,
        run_attempt=0,
        head_branch="",
        head_repo="",
        event="",
        status="",
        url="",
        age=timedelta(0),
        verdict=wd.TICK_ABORTED_RATE_LIMITED,
        workflow="",
        detail="HTTP 403: API rate limit exceeded",
    )
    monkeypatch.setattr(
        wd,
        "run_watchdog",
        lambda *_a, **_k: ([marker], {wd.RATE_LIMIT_MARKER_ID: wd.OUTCOME_ABORTED_RATE_LIMITED}),
    )
    assert wd.main(argv) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed
    assert "recovery pass did not run" in printed
    text = summary.read_text(encoding="utf-8")
    assert "Aborted (rate limited)" in text
    assert "Inspected 0 run(s)." in text  # the marker is not counted as a run


def test_a_rate_limited_tick_exits_nonzero_even_after_acting_on_a_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    # Live work done before the abort does not make the tick healthy: the same
    # abort skipped the recovery pass, and a cancelled orphan in the last
    # tick-interval of its window ages out before any later tick reaches it.
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    classified = wd._base_verdict(_run(1), NOW)
    classified.verdict = wd.ORPHANED
    marker = wd.RunVerdict(
        run_id=wd.RATE_LIMIT_MARKER_ID,
        run_attempt=0,
        head_branch="",
        head_repo="",
        event="",
        status="",
        url="",
        age=timedelta(0),
        verdict=wd.TICK_ABORTED_RATE_LIMITED,
        workflow="",
        detail="HTTP 403: API rate limit exceeded",
    )
    monkeypatch.setattr(
        wd,
        "run_watchdog",
        lambda *_a, **_k: (
            [classified, marker],
            {
                1: wd.OUTCOME_HEALED,
                wd.RATE_LIMIT_MARKER_ID: wd.OUTCOME_ABORTED_RATE_LIMITED,
            },
        ),
    )
    assert wd.main(argv) == 1
    assert "recovery" in capsys.readouterr().out


def test_a_normal_healthy_tick_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: ([], {}))
    assert wd.main(["--repo", REPO, "--summary", str(summary)]) == 0
    assert "::error::" not in capsys.readouterr().out


def test_a_cheap_rate_limit_reset_is_waited_out_and_retried() -> None:
    """A rate limit whose window resets within budget is honoured with one wait-and-retry, not
    an abort. Negative control: reverted code neither knows retry_after nor retries, so it
    raises."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _rate_limit_get(api, r"/actions/runs/1/jobs", times=1, retry_after=5.0)
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}  # retried, not aborted
    assert clock.t >= 5.0  # the reset was waited out


def test_a_500_mid_listing_still_raises() -> None:
    """A server error is not a rate limit: it must propagate exactly as before, so the abort
    path never swallows it. Negative control: broadening the gather's except to all ApiError
    (not only rate_limited) would turn this raise into a silent abort."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_get = api.get

    def get(path: str) -> Any:
        if re.search(r"/actions/runs/1/jobs", path):
            raise wd.ApiError(500, "server error")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    with pytest.raises(wd.ApiError) as excinfo:
        _sweep(api)
    assert excinfo.value.status == 500


def test_the_rate_limit_hint_reads_retry_after_and_reset_headers() -> None:
    msg = email.message.Message()
    msg["Retry-After"] = "12"
    assert wd._rate_limit_hints(msg) == (12.0, None)
    reset = email.message.Message()
    reset["X-RateLimit-Remaining"] = "0"
    reset["X-RateLimit-Reset"] = str(int(NOW.timestamp()) + 40)
    wait, remaining = wd._rate_limit_hints(reset)
    assert remaining == "0" and wait is not None and wait > 0
    # Quota not spent: the reset is not a wait.
    plenty = email.message.Message()
    plenty["X-RateLimit-Remaining"] = "1000"
    plenty["X-RateLimit-Reset"] = str(int(NOW.timestamp()) + 40)
    assert wd._rate_limit_hints(plenty) == (None, "1000")
    assert wd._rate_limit_hints(None) == (None, None)


def test_a_rate_limited_error_is_recognised_from_message_or_headers() -> None:
    assert wd.ApiError(403, "API rate limit exceeded for installation").rate_limited
    assert wd.ApiError(429, "You have exceeded a secondary rate limit").rate_limited
    assert wd.ApiError(403, "forbidden", remaining="0").rate_limited
    assert not wd.ApiError(403, "resource not accessible").rate_limited
    assert not wd.ApiError(404, "not found").rate_limited
    assert not wd.ApiError(500, "rate limit").rate_limited  # only 403/429 count


def test_a_rate_limited_job_read_in_recovery_reaches_the_callers_handler() -> None:
    """A rate limit is not evidence the run is healthy.

    The caller records ``aborted-rate-limited`` -- a FAILED outcome -- and that is the
    only thing standing between an unclassified cancelled run and a green tick.
    Swallowing the limit inside the pass hid it from that handler, so the tick went
    green having never read the run's jobs.
    """
    cancelled = _run(
        1, minutes_ago=105, status="completed", conclusion="cancelled", updated_minutes_ago=85
    )
    api = FakeApi({"cancelled": [cancelled]}, {1: []}, evidence=False)
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            raise wd.ApiError(403, "API rate limit exceeded", remaining="0")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    with pytest.raises(wd.ApiError) as excinfo:
        wd.recover_cancelled_runs(
            api, _policy(), budget=1, tick=_tick(_Clock()), log=lambda _l: None
        )
    assert excinfo.value.rate_limited


def test_a_plain_job_read_failure_in_recovery_is_still_swallowed() -> None:
    """The narrow half of the rule above: only a RATE LIMIT escapes.

    A 500 or a 404 on one run's jobs is genuinely "nothing was touched, the next tick
    reads it again", and letting those out would abort the whole pass over one
    unreadable run.
    """
    cancelled = _run(
        1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=20
    )
    api = FakeApi({"cancelled": [cancelled]}, {1: []}, evidence=False)
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            raise wd.ApiError(500, "server error")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = wd.recover_cancelled_runs(
        api, _policy(), budget=1, tick=_tick(_Clock()), log=lambda _l: None
    )
    assert verdicts == []
    assert outcomes == {}


def test_the_classify_cap_refuses_a_green_tick_for_a_run_it_will_never_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tail run inside one schedule interval of expiry is recorded, not dropped.

    The cap is fair to a run that will be listed again; it is not fair to one the
    window check will drop before the cap is next consulted. With 6 minutes of a
    90-minute window left and a 10-minute schedule, run 2 gets no further tick, so the
    pass records it as inconclusive and the tick exits nonzero instead of green.
    Run 3 has 70 minutes left and is genuinely deferrable, so it is NOT recorded --
    otherwise every ordinary burst would turn the watchdog red.
    """
    served = _run(
        1, minutes_ago=105, status="completed", conclusion="cancelled", updated_minutes_ago=85
    )
    near_expiry = _run(
        2, minutes_ago=104, status="completed", conclusion="cancelled", updated_minutes_ago=84
    )
    deferrable = _run(
        3, minutes_ago=90, status="completed", conclusion="cancelled", updated_minutes_ago=20
    )
    api = FakeApi(
        {"cancelled": [served, near_expiry, deferrable]}, {1: [], 2: [], 3: []}, evidence=False
    )
    monkeypatch.setattr(wd, "RECOVERY_CLASSIFY_READS", 1)
    verdicts, outcomes = wd.recover_cancelled_runs(
        api, _policy(), budget=0, tick=_tick(_Clock()), log=lambda _l: None
    )
    assert outcomes.get(2) == wd.OUTCOME_LOOKUP_FAILED
    assert 3 not in outcomes
    assert wd.OUTCOME_LOOKUP_FAILED in wd.FAILED_OUTCOMES
    flagged = [v for v in verdicts if v.run_id == 2]
    assert flagged and flagged[0].verdict == wd.LOOKUP_INCONCLUSIVE
    assert "no later tick will list it" in flagged[0].detail


def test_the_classify_cap_costs_no_extra_api_read_to_flag_the_tail() -> None:
    """Flagging is decided from the run payload already in hand.

    If it cost a job read per tail run, the fix would spend exactly the budget the cap
    exists to protect.
    """
    runs = [
        _run(
            i,
            minutes_ago=105,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=85 - i * 0.1,
        )
        for i in range(1, 6)
    ]
    api = FakeApi({"cancelled": runs}, {i: [] for i in range(1, 6)}, evidence=False)
    reads = {"jobs": 0}
    original_get = api.get

    def get(path: str) -> Any:
        if "/jobs?" in path:
            reads["jobs"] += 1
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    wd.RECOVERY_CLASSIFY_READS  # documents that the cap is what bounds the reads below
    import unittest.mock as _mock

    with _mock.patch.object(wd, "RECOVERY_CLASSIFY_READS", 2):
        wd.recover_cancelled_runs(
            api, _policy(), budget=0, tick=_tick(_Clock()), log=lambda _l: None
        )
    assert reads["jobs"] == 2
