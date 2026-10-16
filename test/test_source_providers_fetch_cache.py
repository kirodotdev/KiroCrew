"""The full-payload cache: its aggregate size bound, how long a terminal pull
request is kept, and the conditional (``If-None-Match``) revalidation that serves an
unchanged payload without a full read."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew.dashboard.handlers import source_providers as source


@pytest.mark.asyncio
async def test_fetch_rejects_aggregate_payload_over_limit(monkeypatch) -> None:
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"provider": "github", "description": "x" * 200})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 100)
    url = "https://github.com/acme/repo/pull/10"

    with pytest.raises(source.SourceProviderError, match="payload was too large"):
        await source.fetch_pull_request(url)

    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_fetch_cache_evicts_oldest_entry_by_aggregate_weight(monkeypatch) -> None:
    source._CACHE.clear()

    async def fake_fetch(ref):
        return {"provider": "github", "url": ref.url, "description": "x" * 80}

    monkeypatch.setattr(source, "_fetch_github", fake_fetch)
    monkeypatch.setattr(source, "_CACHE_MAX_BYTES", 180)
    monkeypatch.setattr(source, "_MAX_PAYLOAD_BYTES", 1_000)
    first = "https://github.com/acme/repo/pull/10"
    second = "https://github.com/acme/repo/pull/11"

    await source.fetch_pull_request(first)
    await source.fetch_pull_request(second)

    assert first not in source._CACHE
    assert second in source._CACHE
    stored_at, stored_size, stored_payload = source._CACHE[second]
    assert stored_at > 0
    assert stored_size == source._payload_size_bytes(stored_payload)
    assert sum(entry[1] for entry in source._CACHE.values()) <= source._CACHE_MAX_BYTES


# ── Terminal-state retention ─────────────────────────────────────────────────
# A merged or closed pull request never moves on its own, so both caches keep it
# for `_TERMINAL_TTL_SECS` instead of re-reading it on the open-PR cadence for
# as long as its chip sits in a sidebar.


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"state": "OPEN"}, source._CACHE_TTL_SECS),
        ({"state": "OPEN", "draft": True}, source._CACHE_TTL_SECS),
        ({"state": "opened"}, source._CACHE_TTL_SECS),
        ({"state": "MERGED"}, source._TERMINAL_TTL_SECS),
        ({"state": "merged"}, source._TERMINAL_TTL_SECS),
        ({"state": "CLOSED"}, source._CLOSED_TTL_SECS),
        # A closed-while-draft GitLab MR is closed, not draft (see _project_state).
        ({"state": "closed", "draft": True}, source._CLOSED_TTL_SECS),
        # Transient / unknown lifecycles are not terminal.
        ({"state": "locked"}, source._CACHE_TTL_SECS),
        ({}, source._CACHE_TTL_SECS),
    ],
)
def test_full_payload_ttl_is_decided_by_projected_lifecycle(payload, expected) -> None:
    assert source._full_payload_ttl(payload) == expected


def test_terminal_ttl_is_the_longer_one() -> None:
    """The property every retention test below rests on: merged outlives
    closed, and both outlive the open cadence of either cache."""
    assert source._TERMINAL_TTL_SECS > source._CLOSED_TTL_SECS
    assert source._CLOSED_TTL_SECS > source._CACHE_TTL_SECS
    assert source._CLOSED_TTL_SECS > source._CHECK_TTL_SECS
    assert source._TERMINAL_CHIP_STATES == {"merged", "closed"}


@pytest.mark.asyncio
async def test_fetch_pull_request_serves_a_terminal_payload_past_the_open_ttl(monkeypatch) -> None:
    """With no conditional read available (GitLab), a merged MR aged past the
    open TTL is still a cache hit -- no provider read at all."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/21"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "merged", "checks": []})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "merged", "url": url, "marker": "cached"}
    source._CACHE[url] = (stale_for_open, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        # The explicit refresh button still bypasses every TTL.
        result = await source.fetch_pull_request(url, refresh=True)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_closed_payload_ages_on_the_closed_clock_without_a_conditional_read(
    monkeypatch,
) -> None:
    """Closed outlives the open TTL but not the merged one: it can be reopened."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/24"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "closed"})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    cached = {"state": "closed", "marker": "cached"}
    try:
        source._CACHE[url] = (source.time.monotonic() - source._CLOSED_TTL_SECS + 60, 10, cached)
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        source._CACHE[url] = (source.time.monotonic() - source._CLOSED_TTL_SECS - 1, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_terminal_github_payload_is_revalidated_with_the_issue_probe_only(
    monkeypatch,
) -> None:
    """A finished github.com pull request keeps accruing discussion and a closed
    one can be reopened, so it is revalidated on the open cadence -- but its CI
    is over, so only the issue probe is sent (one rate-limit-free request)."""
    url = "https://github.com/acme/repo/pull/21"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fetch = AsyncMock(return_value={"state": "MERGED", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i"'),
        source._ConditionalRead(200, 'W/"never-asked"'),
        source._ConditionalRead(200, 'W/"never-asked"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    source._REVALIDATORS[url] = source._Revalidator('"i"', "", "", "a" * 40, 1.0)
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "MERGED", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale_for_open, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fetch.assert_not_awaited()
        assert set(probes.sent) == {"issue"}
        assert source._CACHE[url][0] > stale_for_open

        # A post-merge comment (or a reopen) moves the issue ETag: full read.
        moved = _FakeProbes(
            source._ConditionalRead(200, 'W/"i2"'),
            source._ConditionalRead(304, '"c"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", moved)
        source._CACHE[url] = (stale_for_open, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        fetch.assert_awaited_once()
        assert set(moved.sent) == {"issue"}
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_fetch_pull_request_refetches_an_open_payload_past_the_open_ttl(monkeypatch) -> None:
    """The open-PR cadence is unchanged: the same age on an OPEN payload is a miss."""
    url = "https://github.com/acme/repo/pull/22"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "OPEN", "checks": []})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale_for_open, 10, {"state": "OPEN", "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_terminal_payload_re_reads_once_past_its_own_ttl(monkeypatch) -> None:
    """Terminal retention is long, not infinite: past its own TTL it re-reads."""
    url = "https://gitlab.com/acme/repo/-/merge_requests/23"
    source._CACHE.clear()
    fetch = AsyncMock(return_value={"state": "merged", "checks": []})
    monkeypatch.setattr(source, "_fetch_gitlab", fetch)
    expired = source.time.monotonic() - source._TERMINAL_TTL_SECS - 5
    source._CACHE[url] = (expired, 10, {"state": "merged", "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result.get("marker") is None
        fetch.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_write_sweep_keeps_a_terminal_entry_older_than_the_open_ttl(monkeypatch) -> None:
    """The on-write expiry sweep ages each entry by its OWN lifecycle TTL."""
    source._CACHE.clear()
    monkeypatch.setattr(source, "_fetch_github", AsyncMock(return_value={"state": "OPEN"}))
    merged = "https://github.com/acme/repo/pull/30"
    open_pr = "https://github.com/acme/repo/pull/31"
    written = "https://github.com/acme/repo/pull/32"
    stale_for_open = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[merged] = (stale_for_open, 10, {"state": "MERGED"})
    source._CACHE[open_pr] = (stale_for_open, 10, {"state": "OPEN"})
    try:
        await source.fetch_pull_request(written)
        assert merged in source._CACHE
        assert open_pr not in source._CACHE
        assert written in source._CACHE
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.parametrize(
    ("entry_state", "age", "force", "due"),
    [
        # Open PRs: the chip TTL, and force bypasses it.
        ("open", 1, False, False),
        ("open", None, False, True),  # None = past _CHECK_TTL_SECS
        ("open", 1, True, True),
        ("draft", 1, True, True),
        # Closed: the closed clock (shorter than merged), and a turn boundary may
        # re-read it (an agent can reopen).
        ("closed", None, False, False),
        ("closed", None, True, True),
        ("closed", "closed", False, True),  # past _CLOSED_TTL_SECS
        ("closed", "terminal", False, True),  # past _TERMINAL_TTL_SECS
        # Merged: long TTL and NOT force-read — it cannot change.
        ("merged", None, False, False),
        ("merged", None, True, False),
        ("merged", "closed", False, False),  # the closed clock is not merged's
        ("merged", "terminal", False, True),
        ("merged", "terminal", True, True),
        # No lifecycle known yet: chip TTL applies (status may be None).
        ("", 1, False, False),
        ("", None, False, True),
    ],
)
def test_chip_refresh_due_by_lifecycle(entry_state, age, force, due) -> None:
    now = 1_000_000.0
    if age is None:
        age = source._CHECK_TTL_SECS + 1
    elif age == "closed":
        age = source._CLOSED_TTL_SECS + 1
    elif age == "terminal":
        age = source._TERMINAL_TTL_SECS + 1
    status = {"state": entry_state} if entry_state else None
    entry = (now - age, status)
    assert source._chip_refresh_due(entry, now, force=force) is due


def test_chip_refresh_due_for_a_missing_entry() -> None:
    assert source._chip_refresh_due(None, 0.0, force=False) is True
    assert source._chip_refresh_due(None, 0.0, force=True) is True


@pytest.mark.asyncio
async def test_schedule_check_refresh_skips_finished_pull_requests(monkeypatch) -> None:
    """The periodic chip sweep spends no provider read on a merged or closed PR
    whose entry is past the open-PR TTL; a turn boundary re-reads a closed one
    but never a merged one."""
    merged = "https://github.com/acme/repo/pull/40"
    closed = "https://github.com/acme/repo/pull/41"
    open_pr = "https://github.com/acme/repo/pull/42"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "_refresh_check_status", refresh)
    stale = source.time.monotonic() - source._CHECK_TTL_SECS - 1
    source._check_cache[merged] = (stale, {"state": "merged", "ci": "passed"})
    source._check_cache[closed] = (stale, {"state": "closed"})
    source._check_cache[open_pr] = (stale, {"state": "open", "ci": "running"})
    try:
        assert source.schedule_check_refresh([merged, closed, open_pr]) == [open_pr]
        assert source._check_inflight == {open_pr}
        source._check_inflight.clear()

        forced = source.request_check_refresh_now([merged, closed, open_pr])
        assert set(forced) == {closed, open_pr}
        assert merged not in source._check_inflight
        assert merged not in source._check_forced_at
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()


# ── Conditional revalidation (If-None-Match probes) ──────────────────────────


def _gh_i_output(status: int, reason: str, etag: str, body: str, sep: str = "\r\n") -> bytes:
    lines = [f"HTTP/2.0 {status} {reason}", "Content-Type: application/json; charset=utf-8"]
    if etag:
        lines.append(f"Etag: {etag}")
    return (sep.join(lines) + sep + sep + body).encode()


def test_parse_conditional_get_reads_status_and_etag_and_ignores_the_body() -> None:
    parse = source._parse_conditional_get("gh")
    read = parse(0, _gh_i_output(200, "OK", 'W/"abc"', '{"updated_at": "x"}'), b"")
    assert read == source._ConditionalRead(200, 'W/"abc"')


def test_parse_conditional_get_treats_304_exit_1_as_success() -> None:
    """`gh` exits 1 on every non-2xx status, so the 304 the request exists for
    arrives as a failure exit code and must be read from the status line."""
    parse = source._parse_conditional_get("gh")
    read = parse(1, _gh_i_output(304, "Not Modified", '"abc"', ""), b"gh: HTTP 304\n")
    assert read == source._ConditionalRead(304, '"abc"')


def test_parse_conditional_get_accepts_lf_separated_headers_and_missing_etag() -> None:
    parse = source._parse_conditional_get("gh")
    read = parse(0, _gh_i_output(200, "OK", "", "[]", sep="\n"), b"")
    assert read == source._ConditionalRead(200, "")


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, _gh_i_output(404, "Not Found", "", '{"message": "Not Found"}'), b"gh: Not Found"),
        (1, _gh_i_output(401, "Unauthorized", "", "{}"), b"gh: HTTP 401: Bad credentials"),
        (1, b"", b"gh: authentication required"),
        (0, b"not a status line\r\n\r\n{}", b""),
    ],
)
def test_parse_conditional_get_rejects_everything_but_200_and_304(
    returncode, stdout, stderr
) -> None:
    parse = source._parse_conditional_get("gh")
    with pytest.raises(source.SourceProviderError):
        parse(returncode, stdout, stderr)


def test_parse_conditional_get_appends_login_hint_on_auth_failure() -> None:
    parse = source._parse_conditional_get("gh")
    with pytest.raises(source.SourceProviderError, match="gh auth login"):
        parse(1, b"", b"gh: authentication required")


@pytest.mark.asyncio
async def test_gh_conditional_get_sends_if_none_match_only_when_known(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_provider(*argv, **kwargs):
        calls.append(argv)
        assert kwargs["parse"] is not None
        return source._ConditionalRead(304, '"e2"')

    monkeypatch.setattr(source, "_run_provider", fake_run_provider)
    await source._gh_conditional_get("repos/acme/repo/issues/7", "")
    await source._gh_conditional_get("repos/acme/repo/issues/7", 'W/"e1"')
    assert calls == [
        ("gh", "api", "repos/acme/repo/issues/7", "-i"),
        ("gh", "api", "repos/acme/repo/issues/7", "-i", "-H", 'If-None-Match: W/"e1"'),
    ]


class _FakeProbes:
    """Scripted `_gh_conditional_get`: answers by path and records the validator
    each probe was sent. `status` defaults to a 304 echo so tests written around
    the issue/check-runs pair stay focused on them."""

    def __init__(
        self,
        issue: source._ConditionalRead,
        checks: source._ConditionalRead,
        status: source._ConditionalRead | None = None,
    ) -> None:
        self.issue = issue
        self.checks = checks
        self.status = status or source._ConditionalRead(304, '"s1"')
        self.sent: dict[str, str] = {}

    async def __call__(self, path: str, etag: str, **_: object) -> source._ConditionalRead:
        if "/check-runs" in path:
            kind = "checks"
        elif path.endswith("/status"):
            kind = "status"
        else:
            kind = "issue"
        self.sent[kind] = etag
        if isinstance(getattr(self, kind), Exception):
            raise getattr(self, kind)
        return getattr(self, kind)


_OPEN_PAYLOAD = {"state": "OPEN", "headSha": "a" * 40, "url": "https://github.com/acme/repo/pull/7"}


_REF = source.parse_source_url("https://github.com/acme/repo/pull/7")


@pytest.mark.asyncio
async def test_payload_unchanged_first_probe_learns_validators_and_is_unknown(monkeypatch) -> None:
    """With nothing to send, a 200 is the only possible answer, and a 200 is
    never read as unchanged -- it returns the validators for the caller to
    commit once the full read has landed."""
    source._REVALIDATORS.clear()
    probes = _FakeProbes(
        source._ConditionalRead(200, 'W/"i1"'),
        source._ConditionalRead(200, 'W/"c1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        outcome = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert outcome.unchanged is False
        assert probes.sent == {"issue": "", "checks": "", "status": ""}
        learned = outcome.learned
        assert learned is not None
        assert (learned.issue_etag, learned.checks_etag, learned.head_sha) == (
            'W/"i1"',
            'W/"c1"',
            "a" * 40,
        )
        # Nothing is committed on a 200 until the fanout that follows succeeds.
        assert _REF.url not in source._REVALIDATORS
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_requires_both_probes_to_answer_304(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator(
        'W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0
    )
    try:
        both = _FakeProbes(
            source._ConditionalRead(304, '"i1"'), source._ConditionalRead(304, '"c1"')
        )
        monkeypatch.setattr(source, "_gh_conditional_get", both)
        assert (await source._probe_github_payload(_REF, _OPEN_PAYLOAD)).unchanged is True
        assert both.sent == {"issue": 'W/"i1"', "checks": 'W/"c1"', "status": 'W/"s1"'}
        # A 304 echoes the validator in the strong form; whichever form the
        # server sent last is what goes out next.
        assert source._REVALIDATORS[_REF.url].issue_etag == '"i1"'

        # CI moved: check-runs answers 200 while the issue half is still 304.
        ci_moved = _FakeProbes(
            source._ConditionalRead(304, '"i1"'),
            source._ConditionalRead(200, 'W/"c2"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", ci_moved)
        moved = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert moved.unchanged is False
        assert moved.learned is not None and moved.learned.checks_etag == 'W/"c2"'
        # ...and the committed validators still describe the payload the cache holds.
        assert source._REVALIDATORS[_REF.url].checks_etag == '"c1"'

        # A review landed: the issue half answers 200.
        review = _FakeProbes(
            source._ConditionalRead(200, 'W/"i2"'),
            source._ConditionalRead(304, '"c2"'),
        )
        monkeypatch.setattr(source, "_gh_conditional_get", review)
        assert (await source._probe_github_payload(_REF, _OPEN_PAYLOAD)).unchanged is False
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_does_not_reuse_check_runs_etag_across_a_push(monkeypatch) -> None:
    """The check-runs validator belongs to a commit; after a push the old one
    would keep answering 304 for a head nobody is looking at."""
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator(
        'W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0
    )
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i1"'),
        source._ConditionalRead(200, 'W/"c-new"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        pushed = {**_OPEN_PAYLOAD, "headSha": "b" * 40}
        outcome = await source._probe_github_payload(_REF, pushed)
        assert outcome.unchanged is False
        assert probes.sent["checks"] == ""
        assert probes.sent["issue"] == 'W/"i1"'
        assert outcome.learned is not None and outcome.learned.head_sha == "b" * 40
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_payload_unchanged_is_unknown_on_probe_failure_or_missing_head(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator(
        'W/"i1"', 'W/"c1"', 'W/"s1"', "a" * 40, 1.0
    )
    failing = _FakeProbes(
        source.SourceProviderError("gh: HTTP 500"),  # type: ignore[arg-type]
        source._ConditionalRead(304, '"c1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", failing)
    try:
        assert await source._probe_github_payload(_REF, _OPEN_PAYLOAD) == source._PROBE_UNKNOWN
        # Validators learned before the failure survive it untouched.
        assert source._REVALIDATORS[_REF.url].issue_etag == 'W/"i1"'
        assert await source._probe_github_payload(_REF, {"state": "OPEN"}) == source._PROBE_UNKNOWN
    finally:
        source._REVALIDATORS.clear()


def test_revalidators_map_is_bounded(monkeypatch) -> None:
    source._REVALIDATORS.clear()
    monkeypatch.setattr(source, "_REVALIDATORS_MAX", 3)
    try:
        for index in range(5):
            source._REVALIDATORS[f"u{index}"] = source._Revalidator("", "", "", "", float(index))
        source._trim_revalidators()
        assert set(source._REVALIDATORS) == {"u2", "u3", "u4"}
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo/pull/7", True),
        # GitLab's API is not probed.
        ("https://gitlab.com/acme/repo/-/merge_requests/7", False),
    ],
)
def test_revalidation_applies_to_github_only(url, expected) -> None:
    ref = source.parse_source_url(url)
    assert source._revalidation_applies(ref) is expected


@pytest.mark.asyncio
async def test_terminal_payload_probes_the_issue_only(monkeypatch) -> None:
    """CI is over for a merged or closed pull request, so the two commit-level
    probes are not sent; the issue probe alone decides (reopen, comments)."""
    source._REVALIDATORS.clear()
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i1"'),
        source._ConditionalRead(200, 'W/"c"'),
        source._ConditionalRead(200, 'W/"s"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    source._REVALIDATORS[_REF.url] = source._Revalidator('"i1"', "", "", "a" * 40, 1.0)
    try:
        for state in ("MERGED", "CLOSED"):
            probes.sent.clear()
            payload = {**_OPEN_PAYLOAD, "state": state}
            assert (await source._probe_github_payload(_REF, payload)).unchanged is True
            assert set(probes.sent) == {"issue"}
        # Validators for the commit half are carried, not blanked, by the skip.
        assert source._REVALIDATORS[_REF.url].checks_etag == ""
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_open_payload_requires_the_commit_status_probe_too(monkeypatch) -> None:
    """Legacy commit statuses are a separate resource from check runs and the
    panel's rollup renders both, so a moved status ETag alone re-reads."""
    source._REVALIDATORS.clear()
    source._REVALIDATORS[_REF.url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    probes = _FakeProbes(
        source._ConditionalRead(304, '"i"'),
        source._ConditionalRead(304, '"c"'),
        source._ConditionalRead(200, 'W/"s2"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    try:
        outcome = await source._probe_github_payload(_REF, _OPEN_PAYLOAD)
        assert outcome.unchanged is False
        assert probes.sent == {"issue": '"i"', "checks": '"c"', "status": '"s"'}
        assert outcome.learned is not None and outcome.learned.status_etag == 'W/"s2"'
    finally:
        source._REVALIDATORS.clear()


@pytest.mark.asyncio
async def test_validators_from_a_200_are_committed_only_after_the_fanout_succeeds(
    monkeypatch,
) -> None:
    """A probe that answers 200 describes a payload the cache does not hold
    yet. If the fanout then fails, the old validators must stay: committing the
    new ones would pair the pre-change payload with post-change ETags, and every
    later probe would answer 304 against it and re-stamp the stale payload as
    current for good."""
    url = "https://github.com/acme/repo/pull/23"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    source._REVALIDATORS[url] = source._Revalidator('"i1"', '"c1"', '"s1"', "a" * 40, 1.0)
    probes = _FakeProbes(
        source._ConditionalRead(200, 'W/"i2"'),
        source._ConditionalRead(304, '"c1"'),
        source._ConditionalRead(304, '"s1"'),
    )
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    stale = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale, 10, cached)
    fetch = AsyncMock(side_effect=source.SourceProviderError("gh: HTTP 503"))
    monkeypatch.setattr(source, "_fetch_github", fetch)
    try:
        with pytest.raises(source.SourceProviderError):
            await source.fetch_pull_request(url)
        assert source._REVALIDATORS[url].issue_etag == '"i1"'

        # The same probe answer followed by a fanout that lands commits them.
        fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
        monkeypatch.setattr(source, "_fetch_github", fetch)
        source._CACHE[url] = (stale, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        assert source._REVALIDATORS[url].issue_etag == 'W/"i2"'
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_revalidation_forces_a_full_read_past_the_max_age(monkeypatch) -> None:
    """Re-stamping rests on the issue ETag moving for every rendered field,
    which GitHub does not promise; past the ceiling one full read runs without
    probing and the validators are dropped so the next cycle learns afresh."""
    url = "https://github.com/acme/repo/pull/29"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    now = source.time.monotonic()
    aged = now - source._REVALIDATED_MAX_AGE_SECS - 1
    source._REVALIDATORS[url] = source._Revalidator(
        '"i"', '"c"', '"s"', "a" * 40, now, read_at=aged
    )
    probes = _FakeProbes(source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"'))
    monkeypatch.setattr(source, "_gh_conditional_get", probes)
    fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    stale = now - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale, 10, {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"})
    try:
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        fetch.assert_awaited_once()
        assert probes.sent == {}
        assert url not in source._REVALIDATORS
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_all_304_carries_read_at_forward_and_a_full_read_resets_it(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/31"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    now = source.time.monotonic()
    source._REVALIDATORS[url] = source._Revalidator(
        '"i"', '"c"', '"s"', "a" * 40, now, read_at=now - 60
    )
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"')),
    )
    stale = now - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale, 10, cached)
    fetch = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fetch)
    try:
        assert await source.fetch_pull_request(url) is cached
        assert source._REVALIDATORS[url].read_at == now - 60

        monkeypatch.setattr(
            source,
            "_gh_conditional_get",
            _FakeProbes(
                source._ConditionalRead(200, 'W/"i2"'), source._ConditionalRead(304, '"c"')
            ),
        )
        source._CACHE[url] = (stale, 10, cached)
        assert (await source.fetch_pull_request(url)).get("marker") == "fresh"
        read_at = source._REVALIDATORS[url].read_at
        assert read_at is not None and read_at >= now
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_open_payload_is_served_when_probes_answer_304(monkeypatch) -> None:
    """The user-visible effect: an idle open PR past its TTL costs two probes
    and no fanout, and its entry is re-stamped fresh."""
    url = "https://github.com/acme/repo/pull/50"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(source._ConditionalRead(304, '"i"'), source._ConditionalRead(304, '"c"')),
    )
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    stale_at = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    cached = {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"}
    source._CACHE[url] = (stale_at, 10, cached)
    try:
        assert await source.fetch_pull_request(url) is cached
        fanout.assert_not_awaited()
        stored_at, size, payload = source._CACHE[url]
        assert stored_at > stale_at and size == 10 and payload is cached
        # ...and a second read inside the TTL is a plain hit: no probes either.
        monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
        assert await source.fetch_pull_request(url) is cached
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_expired_open_payload_falls_through_to_the_fanout_on_a_change(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/51"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40, "marker": "fresh"})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(
        source,
        "_gh_conditional_get",
        _FakeProbes(source._ConditionalRead(200, 'W/"i2"'), source._ConditionalRead(304, '"c"')),
    )
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    stale_at = source.time.monotonic() - source._CACHE_TTL_SECS - 5
    source._CACHE[url] = (stale_at, 10, {"state": "OPEN", "headSha": "a" * 40, "marker": "cached"})
    try:
        result = await source.fetch_pull_request(url)
        assert result["marker"] == "fresh"
        fanout.assert_awaited_once()
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_refresh_and_cold_reads_never_probe(monkeypatch) -> None:
    """An explicit refresh reads in full, and a cache miss has nothing to
    revalidate -- neither spends a probe."""
    url = "https://github.com/acme/repo/pull/52"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    fanout = AsyncMock(return_value={"state": "OPEN", "headSha": "a" * 40})
    monkeypatch.setattr(source, "_fetch_github", fanout)
    monkeypatch.setattr(source, "_gh_conditional_get", AsyncMock(side_effect=AssertionError))
    try:
        await source.fetch_pull_request(url)  # cold
        source._CACHE[url] = (
            source.time.monotonic() - source._CACHE_TTL_SECS - 5,
            10,
            {"state": "OPEN", "headSha": "a" * 40},
        )
        await source.fetch_pull_request(url, refresh=True)
        assert fanout.await_count == 2
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_revalidation_does_not_restamp_an_entry_a_mutation_dropped(monkeypatch) -> None:
    """Generation guard: a mutation landing while the probes were in flight has
    already dropped the entry; the confirmed payload is still returned, but the
    pre-mutation entry must not be written back."""
    url = "https://github.com/acme/repo/pull/53"
    source._CACHE.clear()
    source._REVALIDATORS.clear()
    ref = source.parse_source_url(url)
    cached_entry = (0.0, 10, {"state": "OPEN", "headSha": "a" * 40})

    async def probes_then_mutation(*_args, **_kwargs):
        source._FULL_FETCH_GENERATIONS[url] = 7
        source._CACHE.pop(url, None)
        return source._ConditionalRead(304, '"x"')

    monkeypatch.setattr(source, "_gh_conditional_get", probes_then_mutation)
    source._REVALIDATORS[url] = source._Revalidator('"i"', '"c"', '"s"', "a" * 40, 1.0)
    source._CACHE[url] = cached_entry
    try:
        result = await source._revalidate_pull_request(ref, 0, cached_entry)
        assert result is cached_entry[2]
        assert url not in source._CACHE
    finally:
        source._CACHE.clear()
        source._REVALIDATORS.clear()
        source._FULL_FETCH_GENERATIONS.pop(url, None)
