"""The gh-pr FETCHER: transport, completeness, and what it hands the judge.

The module under test makes no wake decision, so nothing here asserts one. What
it must get right is a reading that is either whole or says it is not, which is
what these tests pin:

- every gh call bounded, audited, retried, and host-pinned;
- a rate-limited window backed off from rather than hammered;
- check runs paginated against the API's own ``total_count``, so a page that
  returns fewer rows than the count declares is caught;
- a failed page reported as ``partial`` rather than silently short;
- comment and review bodies carried, clipped per item and in total, with the
  bot's own comments skipped;
- a message that can never be valid refused permanently, because that is a watch
  to remove rather than a tick to retry.

The seam throughout is ``run_gh``, the single chokepoint every spawn in the
module goes through. Faking above it would leave a real subprocess in the path.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew import irq
from kiro_crew.probes import gh_pr


def _iso(age_secs: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(**overrides) -> str:
    base: dict = {"repo": "acme/widgets", "pr": 42, "host": "github.com"}
    base.update(overrides)
    return json.dumps(base)


def _core(**overrides) -> dict:
    base: dict = {
        "state": "OPEN",
        "mergedAt": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "reviewDecision": "REVIEW_REQUIRED",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "comments": [],
        "reviews": [],
    }
    base.update(overrides)
    return base


def _check(name: str, conclusion: str = "SUCCESS", **extra) -> dict:
    """One check-run row in the shape the REST endpoint really serves.

    The workflow's name is deliberately absent. REST check runs carry the app that
    posted them and a details URL holding the workflow RUN's id; the workflow's own
    name is a GraphQL field. A fake that supplies it tests a payload the fetcher
    never receives, and the identity defect that hides behind the app slug -- shared
    by every Actions row -- cannot be expressed at all.
    """
    run = extra.pop("run", "11")
    row = {
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "app": {"slug": "github-actions"},
        "details_url": f"https://github.com/acme/widgets/actions/runs/{run}/job/{run}0",
    }
    row.update(extra)
    return row


def _reply(body: dict | str, *, rc: int = 0, headers: str = "", stderr: str = ""):
    """One canned gh response, headers spelled the way ``gh api --include`` emits."""
    payload = body if isinstance(body, str) else json.dumps(body)
    prefix = f"HTTP/2.0 200 OK\r\n{headers}\r\n" if headers else ""
    if prefix:
        prefix += "\r\n"
    return types.SimpleNamespace(returncode=rc, stdout=prefix + payload, stderr=stderr)


class _Forge:
    """A scripted forge: one queue of replies per endpoint family.

    A queue rather than a single value, because the behaviours under test are about
    what happens on the SECOND attempt -- a retry after a 5xx, a page after the
    first one, a call after a rate-limit wait.
    """

    def __init__(self, core: dict | None = None) -> None:
        self.core = [_reply(core if core is not None else _core())]
        self.check_pages: list = [_reply({"total_count": 0, "check_runs": []})]
        self.statuses: list = [_reply({"state": "pending", "total_count": 0, "statuses": []})]
        #: Which workflow each run id belongs to. A run absent from here answers
        #: the way the forge does for anything unscripted: unreadable.
        self.runs: dict[str, str] = {"11": "CI", "12": "CI"}
        self.argv: list[list[str]] = []
        self.slept: list[float] = []
        #: The per-call timeout each spawn was made under, so a test can assert the
        #: whole-tick deadline actually bounds it.
        self.timeouts: list[float] = []

    def install(self, monkeypatch) -> "_Forge":
        monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
        monkeypatch.setattr(gh_pr, "run_gh", self._run)
        monkeypatch.setattr(
            gh_pr._Transport, "_sleep", lambda _self, seconds: self.slept.append(seconds)
        )
        return self

    @staticmethod
    def _next(queue: list):
        if not queue:
            return _reply({}, rc=1, stderr="no scripted reply left")
        return queue[0] if len(queue) == 1 else queue.pop(0)

    def _run(self, argv, **kwargs):
        self.argv.append(list(argv))
        if "timeout" in kwargs:
            self.timeouts.append(float(kwargs["timeout"]))
        args = list(argv)[1:]
        if args[:2] == ["pr", "view"]:
            return self._next(self.core)
        target = args[-1]
        if "check-runs" in target:
            return self._next(self.check_pages)
        if "/actions/runs/" in target:
            run = target.rsplit("/", 1)[-1]
            workflow = self.runs.get(run)
            if workflow is None:
                return _reply({}, rc=1, stderr=f"run {run} unreadable")
            return _reply({"path": f".github/workflows/{workflow}.yml", "workflow_id": 1})
        return self._next(self.statuses)

    def paginate(self, rows: list[dict], declared: int | None = None) -> "_Forge":
        """Serve *rows* as real pages of the API's own size, with a truthful count."""
        size = gh_pr._CHECK_PAGE_SIZE
        total = len(rows) if declared is None else declared
        pages = [rows[i : i + size] for i in range(0, len(rows), size)] or [[]]
        self.check_pages = [_reply({"total_count": total, "check_runs": page}) for page in pages]
        return self


def _core_calls(forge: "_Forge") -> int:
    """How many times the forge was asked for the pull request itself.

    The transport keeps no attempt counter -- a count of its own calls has no reader
    in the product -- so the number of attempts is read from the fake that served
    them, which is the same fact without a field that exists only for a test.
    """
    return len([argv for argv in forge.argv if list(argv)[1:3] == ["pr", "view"]])


class TestTheMessageIsValidatedOnce:
    """A message that can never be valid is refused, not retried.

    Every refusal here is permanent, so the caller converts it to a removed watch.
    A tick that raises every interval auto-pauses the job instead, which is a watch
    that dies from a configuration typo with nothing on screen to say so.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "not json at all",
            "[1, 2, 3]",
            json.dumps({"pr": 42}),
            json.dumps({"repo": "acme/widgets"}),
            json.dumps({"repo": "acme/widgets", "pr": 0}),
            json.dumps({"repo": "acme/widgets", "pr": -1}),
            json.dumps({"repo": "acme/widgets", "pr": True}),
            json.dumps({"repo": "acme/widgets", "pr": "42"}),
            json.dumps({"repo": "no-slash", "pr": 42}),
            json.dumps({"repo": "acme/widgets/extra", "pr": 42}),
        ],
    )
    def test_a_malformed_message_raises_rather_than_returning(self, message: str) -> None:
        with pytest.raises(ValueError):
            gh_pr.fetch(message)

    def test_a_host_other_than_the_pinnable_one_is_refused(self) -> None:
        """An arbitrary host would point a credentialed call at a chosen server.

        Whoever composes a watch message must not be able to choose the server. An
        enterprise host is selected by the operator's own gh configuration, so the
        one value accepted here PINS the public host against an ambient ``GH_HOST``
        rather than choosing one.
        """
        with pytest.raises(ValueError):
            gh_pr.fetch(_msg(host="ghe.internal.example"))

    def test_a_deeply_nested_message_is_refused_rather_than_crashing(self) -> None:
        """Nested JSON blows the interpreter stack inside the parser.

        ``RecursionError`` is not a decode error, so without catching it too the
        exception escapes as something no caller expects instead of the removal a
        permanently-invalid message deserves.
        """
        with pytest.raises(ValueError):
            gh_pr.fetch("[" * 3_000 + "]" * 3_000)

    def test_keys_this_build_does_not_read_are_ignored(self, monkeypatch) -> None:
        """A watch armed by an earlier build keeps working.

        Refusing an unknown key would turn every already-armed watch carrying a
        retired knob into a removed one on the first tick after an upgrade.
        """
        _Forge().install(monkeypatch)
        observation = gh_pr.fetch(
            _msg(known_reds=["Lint"], wake_on_green=False, coalesce_secs=45, note="hi")
        )
        assert observation.status == gh_pr.STATUS_OK


class TestTheTransport:
    """Retry, backoff, rate limits, and the bounds each call is made under."""

    def test_the_host_is_pinned_on_every_call(self, monkeypatch) -> None:
        """A bare ``owner/name`` slug resolves through the ambient ``GH_HOST``.

        The module never passes ``--hostname``, so on a machine configured for an
        enterprise server the same slug names a DIFFERENT repository -- where a pull
        request of that number could be merged, which would end a watch on a live one.
        """
        seen: list[str] = []

        def _fake(argv, **kwargs):
            seen.append(kwargs.get("pin_host", ""))
            return _reply(_core())

        monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
        monkeypatch.setattr(gh_pr, "run_gh", _fake)
        gh_pr.fetch(_msg())
        assert seen and set(seen) == {"github.com"}

    def test_an_unpinned_subject_keeps_the_operators_own_resolution(self, monkeypatch) -> None:
        """A watch that named no host is not re-pointed at one either.

        An operator deliberately watching an enterprise pull request arms a message
        with no host, and forcing the public host on it would observe the wrong
        repository just as surely as the reverse.
        """
        seen: list[str] = []

        def _fake(argv, **kwargs):
            seen.append(kwargs.get("pin_host", ""))
            return _reply(_core())

        monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
        monkeypatch.setattr(gh_pr, "run_gh", _fake)
        gh_pr.fetch(json.dumps({"repo": "acme/widgets", "pr": 42}))
        assert seen and set(seen) == {""}

    def test_a_server_fault_is_retried_with_exponential_backoff(self, monkeypatch) -> None:
        """A 5xx is transient, so one reading should not be lost to it.

        The backoff carries jitter because several loops on one host tick on the same
        cadence: a fixed delay retries them in step, which is the pattern a rate
        limiter exists to refuse.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [
            _reply({}, rc=1, stderr="HTTP 502: Bad gateway"),
            _reply(_core()),
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_OK
        assert _core_calls(forge) == 2, "the 502 was retried once"
        assert forge.slept and all(0 <= delay <= gh_pr._BACKOFF_CAP_SECS for delay in forge.slept)

    def test_attempts_are_bounded_and_exhaustion_reads_as_unavailable(self, monkeypatch) -> None:
        """A call that keeps failing reports a status, never raises.

        The whole point of the status vocabulary is that a refusal the fetcher cannot
        get past becomes data its caller can act on.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [_reply({}, rc=1, stderr="HTTP 503: unavailable")]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_UNAVAILABLE
        assert _core_calls(forge) == gh_pr._MAX_ATTEMPTS
        assert "pull request unread" in observation.incomplete

    def test_a_refusal_that_names_itself_is_not_retried(self, monkeypatch) -> None:
        """Retrying a permanent refusal spends the budget to be refused identically.

        A missing pull request and a missing credential answer the same way on every
        attempt, so the attempts are better spent on the reading than on the repeat.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [_reply({}, rc=1, stderr="GraphQL: Could not resolve to a PullRequest")]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_UNAVAILABLE
        assert _core_calls(forge) == 1, "a named permanent refusal is answered once"

    def test_a_rate_limit_waits_for_the_window_the_response_names(self, monkeypatch) -> None:
        """``retry-after`` is obeyed, capped, and then the call is made again.

        A secondary rate limit says in seconds how long to hold off and carries no
        reset epoch, so it is the only signal available for that case.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [
            _reply(
                {},
                rc=1,
                stderr="You have exceeded a secondary rate limit",
                headers="retry-after: 3",
            ),
            _reply(_core()),
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_OK
        assert 3.0 in forge.slept

    def test_a_rate_limit_wait_is_capped_rather_than_obeyed_literally(self, monkeypatch) -> None:
        """A reading delayed past its own interval describes some later moment."""
        forge = _Forge().install(monkeypatch)
        forge.core = [
            _reply({}, rc=1, stderr="API rate limit exceeded", headers="retry-after: 4000"),
            _reply(_core()),
        ]
        gh_pr.fetch(_msg())
        assert forge.slept
        assert max(forge.slept) <= gh_pr._MAX_RATE_LIMIT_WAIT_SECS

    def test_no_call_is_given_longer_than_the_tick_has_left(self, monkeypatch) -> None:
        """A per-call timeout is bounded by the REMAINING budget, with no floor.

        A floor is how a hanging call outlives the deadline the whole budget exists
        to enforce: with a fraction of a second left, a one-second floor hands that
        call several times the tick it is running inside. A timeout too small to
        finish is the honest answer -- the tick is over, and the reading says so.
        """
        forge = _Forge().install(monkeypatch)
        gh_pr.fetch(_msg(), budget_secs=1.0)
        assert forge.timeouts, "the spawn must be given a timeout at all"
        assert all(
            0 < seen <= 1.0 for seen in forge.timeouts
        ), "no call may be given longer than the tick's own budget"

    def test_a_secondary_rate_limit_is_classified_by_the_shared_marker_set(
        self, monkeypatch
    ) -> None:
        """One marker set classifies this binary's refusals for every reader.

        GitHub spells a budget refusal several ways, and a second private marker list
        beside one caller drifts from the shared one in both directions -- the reader
        holding the shorter list retries where the other waits.
        """
        from kiro_crew.monitoring.pull_request import (
            ProviderErrorKind,
            classify_provider_error_text,
        )

        for text in ("You have exceeded a secondary rate limit", "abuse detection mechanism"):
            assert classify_provider_error_text(text) is ProviderErrorKind.RATE_LIMITED, text
        forge = _Forge().install(monkeypatch)
        forge.core = [
            _reply({}, rc=1, stderr="You have exceeded a secondary rate limit"),
            _reply(_core()),
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_OK
        assert forge.slept, "a budget refusal waits rather than retrying immediately"

    def test_a_nearly_spent_window_is_yielded_to_before_the_next_call(self, monkeypatch) -> None:
        """The budget is shared with everything else on the host.

        Spending it to zero refuses every other caller too, so the fetcher yields
        while a little is left rather than discovering the floor by being refused.
        """
        import time as _time

        forge = _Forge().install(monkeypatch)
        forge.core = [
            _reply(
                _core(),
                headers=f"x-ratelimit-remaining: 0\r\nx-ratelimit-reset: {int(_time.time()) + 2}",
            )
        ]
        gh_pr.fetch(_msg())
        assert forge.slept, "a spent window must be waited on before the next call"

    def test_a_window_that_cannot_reopen_in_time_gives_up_rather_than_waiting(
        self, monkeypatch
    ) -> None:
        """Past the cap the tick reports what it has instead of sleeping through."""
        import time as _time

        forge = _Forge().install(monkeypatch)
        reset = int(_time.time()) + 9_000
        forge.core = [
            _reply(
                _core(),
                headers=f"x-ratelimit-remaining: 0\r\nx-ratelimit-reset: {reset}",
            )
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert any("rate-limit" in note or "unread" in note for note in observation.incomplete)

    def test_response_headers_are_stripped_from_the_body(self, monkeypatch) -> None:
        """``--include`` prepends the status line and headers to the payload.

        Parsing the whole thing as JSON fails, and a failed parse reads as an
        unreadable subject -- so a fetcher that asked for headers and did not split
        them off would report every tick blind.
        """
        headers, body = gh_pr._parse_headers(
            'HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 12\r\n\r\n{"ok": true}'
        )
        assert headers["x-ratelimit-remaining"] == "12"
        assert json.loads(body) == {"ok": True}

    def test_a_payload_with_no_header_block_is_returned_whole(self) -> None:
        """A caller that guessed wrong about the shape must not lose the response."""
        headers, body = gh_pr._parse_headers('{"ok": true}')
        assert headers == {}
        assert json.loads(body) == {"ok": True}


class TestTheCheckBoardIsReadWhole:
    """Pagination against the API's own count, and what a short read reports."""

    def test_a_board_past_one_page_is_paginated(self, monkeypatch) -> None:
        """A single page of 100 silently drops row 101.

        The count is the reason this reads the check-runs endpoint rather than the
        rollup served beside the pull request: the rollup is a bare array, so a
        truncated read of it cannot be detected at all.
        """
        rows = [_check(f"Lane {n}") for n in range(150)]
        _Forge().install(monkeypatch).paginate(rows)
        observation = gh_pr.fetch(_msg())
        assert observation.checks_declared == 150
        assert observation.checks_read == 150
        assert len(observation.bucket("passing")) == 150
        assert observation.checks_complete
        assert observation.status == gh_pr.STATUS_OK

    def test_a_count_the_rows_do_not_reach_reports_an_incomplete_board(self, monkeypatch) -> None:
        """The case the count exists to catch, and it must not read as a small board.

        A criterion about failing checks means something different when the board is
        short, and the tallies cannot show that -- so the reading says it, and the
        status says the whole reading is partial.
        """
        rows = [_check(f"Lane {n}") for n in range(5)]
        _Forge().install(monkeypatch).paginate(rows, declared=90)
        observation = gh_pr.fetch(_msg())
        assert not observation.checks_complete
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert any("read 5 of 90" in note for note in observation.incomplete)

    def test_a_failed_page_reports_partial_rather_than_a_short_board(self, monkeypatch) -> None:
        """Rows already read are kept; the reading says a page is missing."""
        forge = _Forge().install(monkeypatch)
        forge.check_pages = [
            _reply({"total_count": 200, "check_runs": [_check(f"Lane {n}") for n in range(100)]}),
            _reply({}, rc=1, stderr="GraphQL: something went wrong"),
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert not observation.checks_complete
        assert len(observation.bucket("passing")) == 100, "what was read is kept"
        assert any("page 2" in note for note in observation.incomplete)

    def test_commit_statuses_are_read_beside_the_check_runs(self, monkeypatch) -> None:
        """A required gate can be published as a commit status and appears on no page.

        A reading without them is short by exactly the rows most likely to be gating.
        """
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check("Lane 1")])
        forge.statuses = [
            _reply(
                {
                    "state": "failure",
                    "total_count": 1,
                    "statuses": [{"context": "PR Readiness", "state": "failure"}],
                }
            )
        ]
        observation = gh_pr.fetch(_msg())
        assert "PR Readiness" in observation.bucket("failing")
        assert observation.checks_complete

    def test_unreadable_commit_statuses_make_the_reading_partial(self, monkeypatch) -> None:
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check("Lane 1")])
        forge.statuses = [_reply({}, rc=1, stderr="GraphQL: nope")]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert "statuses page 1 unread" in observation.incomplete

    def test_statuses_past_one_page_are_paginated_like_the_check_runs(self, monkeypatch) -> None:
        """The sibling sequence is read whole, for the same reason and by the same rule.

        A gating lane can arrive as a commit status, so a first-page-only read of them
        omits exactly the rows most likely to be gating -- and unlike a short check
        board, nothing else on the reading would have said so.
        """
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check("Lane 1")])
        size = gh_pr._CHECK_PAGE_SIZE
        rows = [{"context": f"gate {index}", "state": "success"} for index in range(size)]
        rows.append({"context": "late gate", "state": "failure"})
        forge.statuses = [
            _reply({"state": "failure", "total_count": len(rows), "statuses": rows[:size]}),
            _reply({"state": "failure", "total_count": len(rows), "statuses": rows[size:]}),
        ]
        observation = gh_pr.fetch(_msg())
        assert "late gate" in observation.bucket("failing"), "the second page carries the red"
        assert observation.checks_complete is True
        assert observation.status == gh_pr.STATUS_OK

    def test_a_status_count_the_rows_do_not_reach_reports_an_incomplete_board(
        self, monkeypatch
    ) -> None:
        """A count that disagrees with the rows is the whole reason the count is read."""
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check("Lane 1")])
        forge.statuses = [
            _reply(
                {
                    "state": "success",
                    "total_count": 40,
                    "statuses": [{"context": "one gate", "state": "success"}],
                }
            )
        ]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert "statuses read 1 of 40" in observation.incomplete
        assert observation.checks_complete is False

    def test_a_check_run_and_a_status_sharing_a_name_stay_two_rows(self, monkeypatch) -> None:
        """They are separate sequences in the forge's model, so they are never one lane.

        The pair that folds them is ordinary output: a status carrying no target URL
        has no qualifier, and neither has a check run whose name needed no resolution.
        A newer passing status would then stand for a failing check and the board would
        still call itself whole.
        """
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check("PR Readiness", "FAILURE", started_at="2026-09-25T01:00:00Z")])
        forge.statuses = [
            _reply(
                {
                    "state": "success",
                    "total_count": 1,
                    "statuses": [
                        {
                            "context": "PR Readiness",
                            "state": "success",
                            "created_at": "2026-09-25T02:00:00Z",
                        }
                    ],
                }
            )
        ]
        observation = gh_pr.fetch(_msg())
        assert len(observation.checks) == 2, "one row here is a failing gate reported as a pass"
        assert observation.bucket("failing") == ("PR Readiness",)
        assert "PR Readiness" in observation.bucket("passing")

    def test_a_rerun_supersedes_the_stale_row_of_the_same_identity(self, monkeypatch) -> None:
        """Recency arbitrates in BOTH directions, which is what makes it right.

        A re-run leaves the old row and the new one on the board, so a tally over raw
        rows counts a superseded attempt as a current one. A re-run green supersedes a
        stale red, and a re-run red supersedes a stale green.
        """
        _Forge().install(monkeypatch).paginate(
            [
                _check("Lint", "FAILURE", started_at="2026-09-25T01:00:00Z"),
                _check("Lint", "SUCCESS", started_at="2026-09-25T02:00:00Z"),
                _check("Tests", "SUCCESS", started_at="2026-09-25T01:00:00Z"),
                _check("Tests", "FAILURE", started_at="2026-09-25T02:00:00Z"),
            ]
        )
        observation = gh_pr.fetch(_msg())
        assert observation.bucket("passing") == ("Lint",)
        assert observation.bucket("failing") == ("Tests",)
        assert observation.checks_read == 4, "the fold is reported, so the tally is not a guess"

    def test_undated_duplicates_keep_the_conservative_row(self, monkeypatch) -> None:
        """When recency cannot arbitrate, the choice is declared rather than hidden.

        A row may carry no start time because it is a just-queued re-run or because it
        is a commit status, which has none at all -- so silence about when it began is
        never evidence that it is stale. With nothing to order them by, the row that
        says something may be unfinished is the one to keep.
        """
        _Forge().install(monkeypatch).paginate(
            [
                _check("Lint", "SUCCESS"),
                _check("Lint", conclusion=None, status="QUEUED"),
            ]
        )
        observation = gh_pr.fetch(_msg())
        rows = [row for row in observation.checks if row.bare == "Lint"]
        assert len(rows) == 1
        assert rows[0].bucket == "pending"

    def test_the_same_check_name_from_two_workflows_stays_two_rows(self, monkeypatch) -> None:
        """Collapsing them lets one workflow's green swallow the other's red.

        Every Actions row carries the same app slug, so the slug cannot separate two
        workflow files that each define a job called ``Tests``. The run id in the
        details URL resolves to the workflow, which can.
        """
        forge = _Forge().install(monkeypatch)
        forge.runs = {"11": "backend", "12": "frontend"}
        forge.paginate(
            [
                _check("Tests", "SUCCESS", run="11", started_at="2026-09-25T01:00:00Z"),
                _check("Tests", "FAILURE", run="12", started_at="2026-09-25T02:00:00Z"),
            ]
        )
        observation = gh_pr.fetch(_msg())
        assert len(observation.checks) == 2, "one row here is a failure reported as a pass"
        assert observation.bucket("failing") == (".github/workflows/frontend.yml / Tests",)
        assert observation.bucket("passing") == (".github/workflows/backend.yml / Tests",)
        assert observation.checks_complete is True

    def test_two_runs_of_one_workflow_still_collapse_by_recency(self, monkeypatch) -> None:
        """Separating workflows must not stop a re-run superseding its own stale row.

        A concurrency group cancels an earlier run of the same workflow on the same
        head, leaving two runs whose rows are one lane: the later one is current.
        """
        forge = _Forge().install(monkeypatch)
        forge.runs = {"11": "ci", "12": "ci"}
        forge.paginate(
            [
                _check("Tests", "CANCELLED", run="11", started_at="2026-09-25T01:00:00Z"),
                _check("Tests", "SUCCESS", run="12", started_at="2026-09-25T02:00:00Z"),
            ]
        )
        observation = gh_pr.fetch(_msg())
        assert observation.bucket("passing") == (".github/workflows/ci.yml / Tests",)
        assert observation.bucket("failing") == ()
        rows = [row for row in observation.checks if row.bare == "Tests"]
        assert len(rows) == 1, "two runs of one workflow are one lane"

    def test_an_unresolved_workflow_never_lets_a_green_erase_a_red(self, monkeypatch) -> None:
        """Without the workflow, two rows may be two lanes, so recency proves nothing.

        The reading keeps the failure and reports itself short of what it needs --
        the direction that costs a redundant look instead of a silently dropped gate.
        Saying so is the READING's job, not the row's: ``partial`` plus the note is
        what a consumer reads, and a per-row flag none of them opens adds nothing.
        """
        forge = _Forge().install(monkeypatch)
        forge.runs = {}
        forge.paginate(
            [
                _check("Tests", "FAILURE", run="11", started_at="2026-09-25T01:00:00Z"),
                _check("Tests", "SUCCESS", run="12", started_at="2026-09-25T02:00:00Z"),
            ]
        )
        observation = gh_pr.fetch(_msg())
        rows = [row for row in observation.checks if row.bare == "Tests"]
        assert len(rows) == 1
        assert rows[0].bucket == "failing"
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert observation.checks_complete is False
        assert any("workflow identity" in note for note in observation.incomplete)

    def test_an_unambiguous_board_resolves_nothing_and_spends_no_call(self, monkeypatch) -> None:
        """A check name appearing once needs no qualifier, so it costs no lookup.

        This is the ordinary board. Measured on a real one: forty rows across
        nineteen workflow runs with no name shared between two of them, so resolving
        every run would spend nineteen calls a tick to separate nothing.
        """
        forge = _Forge().install(monkeypatch)
        forge.paginate([_check(f"lane {index}", run=str(index)) for index in range(40)])
        observation = gh_pr.fetch(_msg())
        assert [argv for argv in forge.argv if "/actions/runs/" in argv[-1]] == []
        assert observation.status == gh_pr.STATUS_OK
        assert "lane 7" in observation.bucket("passing"), "an unshared name is its own row"

    def test_only_the_runs_behind_a_shared_name_are_resolved(self, monkeypatch) -> None:
        """The lookup is spent where identity is genuinely in question, and nowhere else."""
        forge = _Forge().install(monkeypatch)
        forge.runs = {"11": "backend", "12": "frontend"}
        forge.paginate(
            [
                _check("Tests", run="11"),
                _check("Tests", "FAILURE", run="12"),
                _check("Docs Lint", run="13"),
            ]
        )
        gh_pr.fetch(_msg())
        resolved = sorted(argv[-1].rsplit("/", 1)[-1] for argv in forge.argv if "runs/" in argv[-1])
        assert resolved == ["11", "12"], "run 13 holds no shared name, so it is not looked up"

    def test_a_row_naming_its_workflow_needs_no_resolution(self, monkeypatch) -> None:
        """A payload carrying the workflow name is believed without a second call."""
        forge = _Forge().install(monkeypatch)
        forge.paginate([{"name": "Tests", "conclusion": "SUCCESS", "workflowName": "CI"}])
        observation = gh_pr.fetch(_msg())
        assert observation.bucket("passing") == ("CI / Tests",)
        assert [argv for argv in forge.argv if "/actions/runs/" in argv[-1]] == []

    def test_two_external_apps_posting_one_name_stay_two_rows(self, monkeypatch) -> None:
        """A workflow-less row is discriminated by its details URL prefix.

        Two different apps posting the same check name must not collapse, while a
        RE-RUN by the same app -- same host and prefix, a new run id deeper in the
        path -- still does.
        """
        _Forge().install(monkeypatch).paginate(
            [
                {
                    "name": "scan",
                    "conclusion": "SUCCESS",
                    "status": "COMPLETED",
                    "detailsUrl": "https://one.example/checks/1",
                },
                {
                    "name": "scan",
                    "conclusion": "FAILURE",
                    "status": "COMPLETED",
                    "detailsUrl": "https://two.example/checks/9",
                },
            ]
        )
        observation = gh_pr.fetch(_msg())
        assert len(observation.checks) == 2
        assert len(observation.bucket("failing")) == 1

    def test_an_unknown_conclusion_is_reported_as_unknown(self, monkeypatch) -> None:
        """A vocabulary this build does not know is named, not filed as passing.

        A judge can be told "one lane reports something unrecognised"; it cannot
        un-see a row folded into a bucket it may not belong to.
        """
        _Forge().install(monkeypatch).paginate([_check("Lane", "SOMETHING_NEW")])
        observation = gh_pr.fetch(_msg())
        assert observation.bucket("unknown") == ("Lane",)

    def test_a_superseded_row_is_reported_separately_from_a_failure(self, monkeypatch) -> None:
        """A cancelled row is overwhelmingly a force-push twin, and it is not a red.

        Reported in its own bucket rather than dropped: whether a cancellation matters
        is a judgment, and a reading that discards it has taken that judgment away.
        """
        _Forge().install(monkeypatch).paginate([_check("Lane", "CANCELLED")])
        observation = gh_pr.fetch(_msg())
        assert observation.bucket("noise") == ("Lane",)
        assert observation.bucket("failing") == ()
        assert observation.as_facts()["checks"]["superseded"] == ["Lane"]


class TestWhatPeopleSaidIsCarried:
    """Comment and review bodies: the evidence no typed reading produces."""

    @staticmethod
    def _comment(ident: str, age: float = 60.0, **extra) -> dict:
        row = {
            "id": ident,
            "createdAt": _iso(age),
            "author": {"login": "a-reviewer"},
            "viewerDidAuthor": False,
            "body": "please add a guard for the windows branch",
        }
        row.update(extra)
        return row

    @staticmethod
    def _review(ident: str, age: float = 60.0, **extra) -> dict:
        row = {
            "id": ident,
            "submittedAt": _iso(age),
            "author": {"login": "a-reviewer"},
            "state": "CHANGES_REQUESTED",
            "body": "one blocking finding",
        }
        row.update(extra)
        return row

    def test_a_comment_body_reaches_the_reading(self, monkeypatch) -> None:
        """A reviewer's ask sits in prose while its lane reports success.

        Without the body, a criterion about "a reviewer asked for a change" has
        nothing to read and the one signal that needs an answer is invisible.
        """
        _Forge(_core(comments=[self._comment("IC_1")])).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert [(r.kind, r.author) for r in observation.remarks] == [("comment", "a-reviewer")]
        assert "windows branch" in observation.remarks[0].body
        assert observation.bodies() == {"comment:IC_1": observation.remarks[0].body}

    def test_a_review_carries_its_verdict_and_its_body(self, monkeypatch) -> None:
        _Forge(_core(reviews=[self._review("PRR_1")])).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.remarks[0].kind == "review"
        assert observation.remarks[0].verdict == "CHANGES_REQUESTED"
        assert observation.remarks[0].body == "one blocking finding"

    def test_our_own_comment_is_skipped(self, monkeypatch) -> None:
        """Without this the watch is a feedback loop.

        The woken agent posts a disposition, the next tick reads a new comment, and it
        is woken again to read what it just wrote.
        """
        _Forge(_core(comments=[self._comment("IC_own", viewerDidAuthor=True)])).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.remarks == ()
        assert observation.remarks_total == 0, "our own comment is not a remark anyone awaits"

    def test_a_remark_past_the_horizon_is_counted_but_not_carried(self, monkeypatch) -> None:
        """The horizon is a FETCH bound, and the total is what makes it visible.

        This module holds no memory between ticks, so it cannot know which remarks a
        previous tick already carried; without a horizon, arming a watch on a long
        pull request would carry its whole history. Reporting the total is what keeps
        "two remarks" from being mistaken for "two in total".
        """
        old = gh_pr.DEFAULT_REMARK_HORIZON_SECS + 600
        _Forge(
            _core(comments=[self._comment("IC_old", age=old), self._comment("IC_new", age=30)])
        ).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert [r.ident for r in observation.remarks] == ["comment:IC_new"]
        assert observation.remarks_total == 2

    def test_a_remark_with_an_unusable_stamp_is_left_out(self, monkeypatch) -> None:
        """An age that cannot be read is not evidence of freshness.

        A remark of unknown age assumed fresh would be carried on every tick for as
        long as the watch runs, and a watch that cries wolf is turned off.
        """
        _Forge(_core(comments=[self._comment("IC_bad", createdAt="not a date")])).install(
            monkeypatch
        )
        observation = gh_pr.fetch(_msg())
        assert observation.remarks == ()

    def test_bodies_are_clipped_per_item_and_in_total(self, monkeypatch) -> None:
        """The per-item bound alone is a product.

        Six remarks at the item bound is more than the judge's whole state budget, so
        the total is what actually bounds the carry -- and it clips the OLDEST bodies,
        keeping who said something and when even when there is no room for the words.
        """
        comments = [
            self._comment(f"IC_{n}", age=30 + n, body="x" * 5_000)
            for n in range(gh_pr._MAX_REMARKS)
        ]
        _Forge(_core(comments=comments)).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert all(len(r.body) <= gh_pr._MAX_BODY_CHARS for r in observation.remarks)
        assert sum(len(r.body) for r in observation.remarks) <= gh_pr._MAX_TOTAL_BODY_CHARS
        assert all(r.clipped for r in observation.remarks)
        assert all(r.ident for r in observation.remarks), "identity survives a clipped body"

    def test_only_the_newest_remarks_are_carried(self, monkeypatch) -> None:
        """The recent end is what needs an answer.

        A pull request twenty review rounds deep holds hundreds of remarks, and the
        one still waiting on a reply is at the tail.
        """
        comments = [self._comment(f"IC_{n}", age=30 + n * 10) for n in range(20)]
        _Forge(_core(comments=comments)).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert len(observation.remarks) == gh_pr._MAX_REMARKS
        assert [r.ident for r in observation.remarks] == [
            f"comment:IC_{n}" for n in range(gh_pr._MAX_REMARKS)
        ]

    def test_control_characters_are_stripped_from_a_body(self) -> None:
        """Third-party prose reaches a log, a transcript row and a notification.

        A terminal escape sequence in it is stripped where the prose enters the
        process rather than at each of those three readers.
        """
        assert "\x1b" not in gh_pr.sanitize_body("before\x1b[31mafter\x00end")
        assert gh_pr.sanitize_body("a\n\n\n\n\nb") == "a\n\nb"
        assert gh_pr.sanitize_body(None) == ""
        assert gh_pr.sanitize_body(12) == ""

    def test_a_quiet_tick_leaves_a_digest_of_what_was_screened(self, monkeypatch) -> None:
        """A wrong quiet has to be examinable, and the bodies are gone after the tick.

        Without this the record says a remark existed and nothing about what it said,
        so nobody can check the verdict against the words it was reached on. A digest
        identifies the prose while keeping it out of the durable half.
        """
        said = self._comment("IC_1", body="please rename it")
        forge = _Forge(_core(comments=[said])).install(monkeypatch)
        forge.paginate([])
        facts = gh_pr.fetch(_msg()).as_facts()
        row = facts["remarks"][0]
        assert len(row["body_digest"]) == 12
        assert row["body_digest"] == gh_pr._body_digest("please rename it")
        assert "rename" not in json.dumps(facts), "a digest, never the prose"

    def test_a_remark_with_no_body_gets_no_digest(self, monkeypatch) -> None:
        """An empty body is not a thing to identify, so it gets no identifier."""
        assert gh_pr._body_digest("") == ""

    def test_the_durable_half_carries_no_prose(self, monkeypatch) -> None:
        """The split that keeps review text off the disk.

        ``as_facts`` is what a caller may keep, and it says who spoke and when.
        ``bodies`` is the prose, and it is a separate call so keeping the first cannot
        accidentally keep the second.
        """
        _Forge(_core(comments=[self._comment("IC_1")])).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        facts = observation.as_facts()
        assert json.dumps(facts, allow_nan=False), "the durable half must be strict JSON"
        assert "please add a guard" not in json.dumps(facts)
        digest = facts["remarks"][0]["body_digest"]
        assert (
            digest and digest not in observation.remarks[0].body
        ), "the record stands in for the body without carrying any of it"
        assert observation.bodies()["comment:IC_1"].startswith("please add a guard")

    def test_an_edited_body_changes_the_record_it_stands_in_for(self, monkeypatch) -> None:
        """Otherwise an edit that asks for something reads as an unchanged subject.

        The body lives for one tick and the digest is what the durable record keeps of
        it, so the digest is the only thing that can tell a reworded comment from the
        same one seen twice.
        """
        _Forge(_core(comments=[self._comment("IC_1")])).install(monkeypatch)
        first = gh_pr.fetch(_msg()).as_facts()["remarks"][0]["body_digest"]
        _Forge(_core(comments=[self._comment("IC_1", body="actually, ship it")])).install(
            monkeypatch
        )
        second = gh_pr.fetch(_msg()).as_facts()["remarks"][0]["body_digest"]
        assert first and second and first != second


class TestTheReadingSpeaksForItself:
    """The status and the terminal facts a caller reads, and nothing it may act on."""

    def test_no_wake_is_emitted_for_a_failing_check(self, monkeypatch) -> None:
        """The whole point: a red lane is a FACT, and what it means is not this
        module's call.

        A reading that decided would be judging the owner's criterion for them, and
        the criterion is the thing that differs between two loops on one board.
        """
        _Forge().install(monkeypatch).paginate([_check("Lint", "FAILURE")])
        probe = gh_pr.PrWatchProbe()
        ctx = types.SimpleNamespace(
            message=_msg(), job=types.SimpleNamespace(id="job-1"), in_process=True
        )
        probe.identity(ctx)
        tick = probe.observe(ctx)
        assert tick.observations == [], "a fetcher emits no wake"
        assert tick.epoch == "a" * 40, "the kernel still gets an epoch, for its own reset"
        assert probe.observation is not None
        assert probe.observation.bucket("failing") == ("Lint",)

    def test_a_merged_pull_request_reads_as_terminal_and_as_merged(self, monkeypatch) -> None:
        """The one mapping a caller acts on, stated as two separate facts.

        A merge and a close are different endings: a close leaves a question --
        reopen or abandon -- and a caller that records both as done tells the owner
        "nothing to do" about the one case that needs them.
        """
        _Forge(_core(state="MERGED", mergedAt="2026-09-25T00:00:00Z")).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.is_terminal and observation.merged
        assert observation.status == gh_pr.STATUS_OK, "nothing is missing from an ended reading"

    def test_a_closed_pull_request_is_terminal_but_not_merged(self, monkeypatch) -> None:
        _Forge(_core(state="CLOSED")).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.is_terminal and not observation.merged

    def test_a_merge_stamp_alone_is_enough(self, monkeypatch) -> None:
        """Two independent signals, so a missing ``state`` cannot hide a merge."""
        _Forge(_core(state="", mergedAt="2026-09-25T00:00:00Z")).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.is_terminal and observation.merged

    def test_an_ended_pull_request_does_not_have_its_board_read(self, monkeypatch) -> None:
        """It runs no more checks, so reading them spends calls to learn nothing."""
        forge = _Forge(_core(state="MERGED")).install(monkeypatch)
        gh_pr.fetch(_msg())
        assert not any("check-runs" in " ".join(argv) for argv in forge.argv)

    def test_an_unreachable_subject_is_never_terminal(self, monkeypatch) -> None:
        """An absence of evidence must not retire a watch.

        This is the direction that matters: a wrongly-terminal reading stops the work
        permanently, and a wrongly-live one costs one more tick.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [_reply({}, rc=1, stderr="HTTP 503")]
        observation = gh_pr.fetch(_msg())
        assert not observation.is_terminal and not observation.merged

    def test_a_head_that_cannot_be_read_reports_partial(self, monkeypatch) -> None:
        """Without a head there is no board to read, and that is not a calm tick."""
        _Forge(_core(headRefOid="")).install(monkeypatch)
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_PARTIAL
        assert "head revision unknown" in observation.incomplete

    def test_a_nested_response_reads_as_unobservable(self, monkeypatch) -> None:
        """A pathological payload is a failed reading, not an escaped exception."""
        forge = _Forge().install(monkeypatch)
        forge.core = [_reply("[" * 3_000 + "]" * 3_000)]
        observation = gh_pr.fetch(_msg())
        assert observation.status == gh_pr.STATUS_UNAVAILABLE

    def test_an_unreadable_reading_is_a_failed_tick_for_the_kernel(self, monkeypatch) -> None:
        """The kernel's consecutive-failure backstop is what reports a blind watch.

        That is state a stateless reading cannot hold, and it is the reason the kernel
        is still in the path at all.
        """
        forge = _Forge().install(monkeypatch)
        forge.core = [_reply({}, rc=1, stderr="HTTP 503")]
        probe = gh_pr.PrWatchProbe()
        ctx = types.SimpleNamespace(
            message=_msg(), job=types.SimpleNamespace(id="job-1"), in_process=True
        )
        probe.identity(ctx)
        assert probe.observe(ctx).fetch_ok is False

    def test_the_facts_use_the_key_names_the_other_reader_already_publishes(
        self, monkeypatch
    ) -> None:
        """One spelling for the two readers, so a consumer needs no second one."""
        _Forge().install(monkeypatch).paginate([_check("Lint", "FAILURE")])
        facts = gh_pr.fetch(_msg()).as_facts()
        for key in ("state", "mergeability", "review_decision", "head_revision", "checks"):
            assert key in facts
        assert facts["checks"]["failed"] == ["Lint"]
        assert facts["observation_status"] == gh_pr.STATUS_OK


class TestTheRetiredScriptDriverIsRefusedAndKept:
    """A watch driven from a subprocess is refused, and its job is kept.

    The judge that decides a wake runs in the gateway, so a script cron holding a
    copy of the retired driver can reach this probe but never reach a decision.
    Refusing on every tick is the answer, because the scheduler counts a raising job:
    the message lands in ``last_error`` and the job is auto-paused, still listed,
    saying what to arm instead. Deleting it would take the only durable record that
    the watch existed.
    """

    def test_a_script_context_is_refused_without_ending_the_job(self) -> None:
        """The exception TYPE is the behaviour, not an implementation detail.

        ``irq.run`` turns a ``ValueError`` from ``identity`` into ``Done``, and the
        scheduler answers ``Done`` by deleting the job. Anything else propagates and
        is counted as a failure instead, which is what auto-pauses it.
        """
        probe = gh_pr.PrWatchProbe()
        ctx = types.SimpleNamespace(message=_msg(), job=types.SimpleNamespace(id="cron-1"))
        with pytest.raises(RuntimeError) as caught:
            irq.run(ctx, probe)
        assert not isinstance(caught.value, irq.Done), "Done would delete the job record"
        assert not isinstance(caught.value, ValueError), "a ValueError is converted to Done"
        assert "retired" in str(caught.value)
        assert "monitor_start" in str(caught.value), "the message names what to arm instead"

    def test_a_malformed_message_still_ends_the_job(self) -> None:
        """The two refusals are not the same refusal.

        A watch driven the wrong way is recoverable by re-arming it, so its record is
        worth keeping. A cron message that can never parse names no pull request to
        watch, so there is nothing to re-arm and nothing to keep.
        """
        probe = gh_pr.PrWatchProbe()
        ctx = types.SimpleNamespace(
            message="not json at all", job=types.SimpleNamespace(id="cron-2"), in_process=True
        )
        with pytest.raises(irq.Done):
            irq.run(ctx, probe)

    def test_the_in_process_driver_is_unaffected(self, monkeypatch) -> None:
        """The same refusal must not reach the driver the judge does run behind."""
        _Forge().install(monkeypatch).paginate([_check("Lint", "FAILURE")])
        probe = gh_pr.PrWatchProbe()
        verdict = irq.poll("loop-1", _msg(), probe)
        assert verdict.outcome is not irq.Outcome.TERMINAL
        assert probe.observation is not None
        assert probe.observation.bucket("failing") == ("Lint",)
