"""The chip status cache behind the sidebar's pull-request chips: the status
endpoint, refresh scheduling and its floors, flap damping, and what a full payload
write carries into the chip (CI and merge state)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.handlers import source_providers as source


@pytest.mark.asyncio
async def test_refresh_check_status_queues_broadcast_only_when_status_changes(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    callback = MagicMock()
    queue_update = MagicMock()
    monkeypatch.setattr(source, "_queue_check_update", queue_update)
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "open"}),
    )

    await source._refresh_check_status(url, callback)
    await source._refresh_check_status(url, callback)

    queue_update.assert_called_once_with(callback)
    assert source.get_cached_check_status(url) == {"ci": "passed", "state": "open"}
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_check_update_broadcasts_are_coalesced(monkeypatch) -> None:
    callback = MagicMock()
    source._check_update_callbacks.clear()
    source._check_update_handle = None
    monkeypatch.setattr(source, "_CHECK_UPDATE_DEBOUNCE_SECS", 0)

    source._queue_check_update(callback)
    source._queue_check_update(callback)
    # flake-ok: pre-existing 10 ms yield for the zero-debounce broadcast handle to run, moved verbatim; tracked for a de-flake pass
    await source.asyncio.sleep(0.01)

    callback.assert_called_once_with()
    assert source._check_update_handle is None


@pytest.mark.asyncio
async def test_status_endpoint_warms_allowlist_before_parsing_self_hosted_urls(
    monkeypatch,
) -> None:
    """The status endpoint parses browser-supplied URLs, so it must warm the
    allowlist first: on a cold snapshot an authorized self-managed URL would be
    dropped as unsupported and never reach scheduling. Existing endpoint tests
    use only gitlab.com/github.com URLs, which never exercise this."""
    url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"
    source._check_cache.clear()
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)

    async def fake_ensure() -> frozenset:
        source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        return frozenset({"gitlab.acme.internal"})

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)
    refresh = MagicMock(return_value=[url])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/source/pull-request/status", json={"urls": [url]})
        assert response.status == 200

    # The authorized self-managed URL survived validation and reached scheduling.
    assert refresh.call_args.args[0] == [url]


@pytest.mark.asyncio
async def test_status_endpoint_audits_cancellation_during_allowlist_warm_up(
    monkeypatch, _mock_source_sel
) -> None:
    """The status handler awaits ``ensure_gitlab_hosts_loaded()`` directly (the
    read/checks/resolve endpoints reach it through a helper already wrapped in a
    cancellation guard). A cancellation at that direct await must still emit a
    terminal audit: without the guard the handler unwinds past the ``completed``
    line and an authorized status attempt vanishes from the tamper-evident SEL
    chain. Driven without a TestClient because aiohttp's server turns a handler
    ``CancelledError`` into a connection abort and would mask the re-raise."""

    async def cancel_warm_up() -> "frozenset[str]":
        raise source.asyncio.CancelledError()

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", cancel_warm_up)

    class _FakeRequest:
        method = "POST"

        def __init__(self) -> None:
            state = MagicMock()
            state.owner_id = "U_OWNER"
            self.app = {"state": state}
            self._claims = {"user": "U_OWNER", "app": ""}

        def get(self, key, default=None):
            return self._claims.get(key, default)

        def __getitem__(self, key):
            return self._claims[key]

        def __contains__(self, key):
            return key in self._claims

        async def json(self):
            return {"urls": ["https://github.com/acme/repo/pull/1"]}

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_status(_FakeRequest())

    # Revert-verified: dropping the try/except leaves the CancelledError
    # propagating but with no log_api_access call recorded at all (the happy
    # path emits nothing before ``completed``), so this assertion fails.
    call = _mock_source_sel.log_api_access.call_args
    assert call.kwargs["operation"] == "source.pull_request.status"
    assert call.kwargs["outcome"] == "failed"
    assert call.kwargs["error"] == "request_cancelled"


@pytest.mark.asyncio
async def test_status_endpoint_serves_cached_chip_status_and_kicks_refresh(monkeypatch) -> None:
    """The Changes-tab strip reads cached status for many URLs in one request."""
    known = "https://github.com/acme/repo/pull/12"
    unknown = "https://github.com/acme/repo/pull/13"
    source._check_cache.clear()
    source._check_cache[known] = (source.time.monotonic(), {"ci": "failed", "state": "open"})
    refresh = MagicMock(return_value=[unknown])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)

    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/status",
            # A www. spelling and a duplicate both normalize to one entry; a
            # non-pull-request URL is dropped instead of failing the request.
            json={
                "urls": [
                    "https://www.github.com/acme/repo/pull/12",
                    known,
                    unknown,
                    "https://example.com/x",
                ]
            },
        )
        assert response.status == 200
        payload = await response.json()

    # Only URLs with a cached status are reported; the rest simply stay absent
    # until a background refresh lands.
    assert payload == {
        "statuses": {known: {"ci": "failed", "state": "open"}},
        # The scheduler's report is passed through verbatim so the client knows
        # to re-poll soon instead of waiting out the TTL, along with the TTL that
        # paces its steady state.
        "refreshing": [unknown],
        "ttlSecs": source.CHECK_STATUS_TTL_SECS,
    }
    assert refresh.call_args.args[0] == [known, unknown]
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_schedule_check_refresh_reports_urls_expected_to_change(monkeypatch) -> None:
    """The status endpoint's ``refreshing`` hint comes from the scheduler itself."""
    fresh = "https://github.com/acme/repo/pull/1"
    stale = "https://github.com/acme/repo/pull/2"
    already = "https://github.com/acme/repo/pull/3"
    deferred = "https://github.com/acme/repo/pull/4"
    source._check_cache.clear()
    source._check_inflight.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    now = source.time.monotonic()
    source._check_cache[fresh] = (now, {"state": "open"})
    source._check_cache[stale] = (now - source._CHECK_TTL_SECS - 1, {"state": "open"})
    source._check_inflight.add(already)
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 2)

    refreshing = source.schedule_check_refresh([fresh, stale, already, deferred])

    # Fresh entries need nothing; a started and an already-in-flight refresh are
    # both "coming shortly"; the pending-cap deferral was backed off for a TTL,
    # so promising the client a fast update for it would be a lie.
    assert refreshing == [stale, already]
    assert source._check_cache[deferred][1] is None
    source._check_cache.clear()
    source._check_inflight.clear()


def test_status_from_full_payload_projects_lifecycle_and_ci() -> None:
    """The chip projection must speak exactly the chip vocabulary."""
    assert source.status_from_full_payload(
        {"state": "OPEN", "draft": False, "checks": [{"bucket": "passed"}]}
    ) == {"ci": "passed", "state": "open"}
    # A draft with a running check, GitHub spelling.
    assert source.status_from_full_payload(
        {"state": "OPEN", "draft": True, "checks": [{"bucket": "pending"}, {"bucket": "passed"}]}
    ) == {"ci": "running", "state": "draft"}
    # Any failure dominates the rollup.
    assert source.status_from_full_payload(
        {"state": "MERGED", "checks": [{"bucket": "failed"}, {"bucket": "pending"}]}
    ) == {"ci": "failed", "state": "merged"}
    # GitLab spellings.
    assert source.status_from_full_payload({"state": "opened", "checks": []}) == {"state": "open"}
    # `locked` is GitLab's transient mid-merge state — no terminal lifecycle, so
    # the projection returns nothing for it (both paths agree on "no change")
    # rather than painting a false "closed" glyph.
    assert source.status_from_full_payload({"state": "locked"}) is None
    # An MR closed while still in draft (a common way to abandon one): GitLab
    # keeps `draft: true`, but the lifecycle is closed. Draft only applies to an
    # OPEN MR, so this projects "closed" — and the chip path must agree, or the
    # mutual invalidation ping-pongs draft<->closed forever (Arbiter item 1).
    assert source.status_from_full_payload({"state": "closed", "draft": True}) == {
        "state": "closed"
    }
    # The generic bucket rollup (GitHub's statusCheckRollup, or any payload
    # without an authoritative `ciStatus`): all-skipped/passed buckets → "passed",
    # no "failed" and no "pending". GitLab instead carries `ciStatus` (see below).
    assert source.status_from_full_payload(
        {"state": "opened", "checks": [{"bucket": "skipped"}, {"bucket": "passed"}]}
    ) == {"ci": "passed", "state": "open"}
    assert source.status_from_full_payload(
        {"state": "opened", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "passed", "state": "open"}
    # GitLab's authoritative aggregate CI (`ciStatus`) is preferred over — and
    # never diverges from — its faithful per-job buckets: an allow_failure red
    # job buckets "failed" for display, but the aggregate the chip reads is
    # "passed", so `ciStatus` wins and the two caches agree.
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "passed", "checks": [{"bucket": "failed"}]}
    ) == {"ci": "passed", "state": "open"}
    # A blocking manual gate: aggregate is "running", even though the manual job
    # buckets "skipped".
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "running", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "running", "state": "open"}
    # Nothing knowable → no entry at all, rather than a misleading default.
    assert source.status_from_full_payload({"state": "", "checks": []}) is None
    assert source.status_from_full_payload("nope") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_full_fetch_writes_through_to_chip_cache(monkeypatch) -> None:
    """One provider read feeds both surfaces, so they cannot disagree."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    monkeypatch.setattr(
        source,
        "_fetch_github",
        AsyncMock(
            return_value={"state": "MERGED", "draft": False, "checks": [{"bucket": "passed"}]}
        ),
    )

    try:
        await source.fetch_pull_request(url)

        # The sidebar chip now reads the same lifecycle the detail panel got.
        assert source.get_cached_check_status(url) == {"ci": "passed", "state": "merged"}
        # Tagged 'detail' so the client knows a full fetch produced the value;
        # the client refetches on both origins (cross-window convergence) but the
        # initiator's refetch just hits the warm cache.
        sink.assert_called_once_with(
            {"url": url, "origin": "detail", "ci": "passed", "state": "merged"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_drops_stale_full_payload_and_emits_delta(monkeypatch) -> None:
    """The reverse direction: a chip change invalidates the detail payload."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "merged"}),
    )

    try:
        await source._refresh_check_status(url)

        # The panel's cached "OPEN" payload is gone, so its next read is fresh
        # instead of rendering a lifecycle the chip has already moved past.
        assert url not in source._CACHE
        sink.assert_called_once_with(
            {"url": url, "origin": "chip", "ci": "passed", "state": "merged"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_without_change_keeps_full_payload(monkeypatch) -> None:
    """An unchanged status must not throw away a valid cached payload."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"ci": "passed", "state": "open"}),
    )

    await source._refresh_check_status(url)

    assert url in source._CACHE
    source._CACHE.clear()
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_keeps_known_ci_when_rollup_alone_fails(monkeypatch) -> None:
    """Mirror of the full-payload keep-known rule for a partial `checks`: a
    degraded rollup must not erase a glyph the chip cache already knows, and
    the internal marker must never reach the cache."""
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open", source._CHIP_CI_UNAVAILABLE: "1"}),
    )

    try:
        await source._refresh_check_status(url)

        assert source._check_cache[url][1] == {"ci": "passed", "state": "open"}
    finally:
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_lets_a_clean_empty_rollup_clear_a_stale_ci(monkeypatch) -> None:
    """A rollup that SUCCEEDS with zero checks carries no marker: the CI glyph
    was legitimately withdrawn (no checks configured), so it must clear."""
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_cache[url] = (source.time.monotonic(), {"ci": "passed", "state": "open"})
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open"}),
    )

    try:
        await source._refresh_check_status(url)

        assert source._check_cache[url][1] == {"state": "open"}
    finally:
        source._CACHE.clear()
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_manual_pipeline_matches_projection(monkeypatch) -> None:
    """A manual-gated GitLab pipeline must project the same CI on both caches.

    A `manual` pipeline aggregate is a BLOCKING gate — work is still outstanding
    — so both paths project "running" (not "passed"). The chip reads the
    aggregate directly; the full payload reads the same aggregate via `ciStatus`,
    so they cannot diverge and cannot ping-pong.
    """
    url = "https://gitlab.com/acme/repo/-/merge_requests/7"

    async def fake_run_json(*args, **kwargs):
        path = args[2] if len(args) > 2 else ""
        if "pipelines" in path:
            return [{"id": 99, "status": "manual"}]
        return {"state": "opened", "draft": False}

    monkeypatch.setattr(source, "_run_json", AsyncMock(side_effect=fake_run_json))

    chip = await source._fetch_check_status(url)

    assert chip == {"ci": "running", "state": "open"}
    # Coherence check: the full-payload projection carrying the same aggregate
    # (`ciStatus: "running"`) resolves identically, regardless of the manual
    # job's own "skipped" display bucket.
    assert source.status_from_full_payload(
        {"state": "opened", "ciStatus": "running", "checks": [{"bucket": "skipped"}]}
    ) == {"ci": "running", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_closed_draft_matches_projection(monkeypatch) -> None:
    """A closed-while-draft GitLab MR must agree on both caches; locked yields none.

    GitLab keeps `draft: true` on an MR closed while still in draft (both paths
    must project "closed", not "draft", or the mutual-invalidation ping-pongs),
    and reports `locked` transiently during a merge (both paths project NO
    lifecycle for it — a terminal "closed" glyph on a mid-merge MR is false).
    """
    base = "https://gitlab.com/acme/repo/-/merge_requests/"

    def run_json_for(details):
        async def fake_run_json(*args, **kwargs):
            path = args[2] if len(args) > 2 else ""
            if "pipelines" in path:
                return []
            return details

        return fake_run_json

    # Closed while still in draft.
    monkeypatch.setattr(
        source, "_run_json", AsyncMock(side_effect=run_json_for({"state": "closed", "draft": True}))
    )
    chip = await source._fetch_check_status(base + "7")
    assert chip == {"state": "closed"}
    assert source.status_from_full_payload({"state": "closed", "draft": True}) == chip

    # Locked (transient during merge): no lifecycle projection on either path.
    monkeypatch.setattr(
        source, "_run_json", AsyncMock(side_effect=run_json_for({"state": "locked"}))
    )
    chip = await source._fetch_check_status(base + "8")
    assert chip is None
    assert source.status_from_full_payload({"state": "locked"}) is None


@pytest.mark.asyncio
async def test_fetch_check_status_gitlab_allow_failure_matches_projection(monkeypatch) -> None:
    """An allow_failure red job shows failed in the list but never diverges the glyph.

    GitLab folds an allowed-failure job into a `success` pipeline aggregate. The
    single CI glyph is projected from that aggregate (authoritative, lossless) on
    BOTH paths — the chip reads it directly, the full payload via `ciStatus` — so
    the job's FAITHFUL "failed" display bucket (which must be preserved so the
    Checks tab does not falsely claim "all checks passed") cannot make the two
    caches disagree or ping-pong.
    """
    # `_gitlab_check` buckets are faithful: an allowed failure still shows failed.
    assert source._gitlab_check({"status": "failed", "allow_failure": True})["bucket"] == "failed"
    assert source._gitlab_check({"status": "failed", "allow_failure": False})["bucket"] == "failed"

    url = "https://gitlab.com/acme/repo/-/merge_requests/11"

    async def fake_run_json(*args, **kwargs):
        path = args[2] if len(args) > 2 else ""
        if "pipelines" in path:
            # GitLab reports the aggregate as `success` when only allow_failure
            # jobs failed.
            return [{"id": 42, "status": "success"}]
        return {"state": "opened", "draft": False}

    monkeypatch.setattr(source, "_run_json", AsyncMock(side_effect=fake_run_json))
    chip = await source._fetch_check_status(url)
    assert chip == {"ci": "passed", "state": "open"}

    # The full payload carries the SAME authoritative aggregate as `ciStatus`
    # ("passed"), so it resolves identically even though a real job bucket is
    # "failed" — the glyph comes from the aggregate, not the job rollup.
    payload = {
        "state": "opened",
        "ciStatus": "passed",
        "checks": [
            source._gitlab_check({"status": "success"}),
            source._gitlab_check({"status": "failed", "allow_failure": True}),
        ],
    }
    assert source.status_from_full_payload(payload) == {"ci": "passed", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_gitlab_full_payload_synthesizes_pipeline_when_jobs_empty(monkeypatch) -> None:
    """A pipeline with no materialized jobs must still project CI on both caches.

    When a GitLab pipeline exists but its jobs have not materialized yet, the
    chip path reads the pipeline AGGREGATE directly, but the full payload built
    `checks: []` and thus projected no `ci` — clearing a glyph the chip still
    shows and arming the mutual-invalidation ping-pong. The full payload must
    fall back to the aggregate (distinct from a genuinely pipeline-less MR, which
    keeps `checks` empty). Folds in the empty-jobs case (Arbiter item 2).
    """

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {
                "iid": 42,
                "title": "WIP",
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/42",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc123",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            return [{"id": 91, "status": "running", "web_url": "https://gitlab.com/p/91"}]
        if "/pipelines/91/jobs" in command:
            return []  # pipeline exists, jobs not yet materialized
        if "/commits?" in command:
            return []
        if "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    # `checks` is not empty: it carries the synthesized pipeline aggregate...
    assert [c["name"] for c in data["checks"]] == ["Pipeline"]
    assert data["checks"][0]["bucket"] == "pending"  # running aggregate → pending bucket
    # ...so the full-payload projection agrees with the chip aggregate ("running").
    assert source.status_from_full_payload(data) == {"ci": "running", "state": "open"}


@pytest.mark.asyncio
async def test_fetch_gitlab_full_payload_no_pipeline_keeps_checks_empty(monkeypatch) -> None:
    """A genuinely pipeline-less MR must keep `checks` empty (no CI on either side)."""

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/43"):
            return {
                "iid": 43,
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/43",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc123",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            return []  # no pipeline at all
        if "/commits?" in command or "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/43")
    )

    assert data["checks"] == []
    assert source.status_from_full_payload(data) == {"state": "open"}


def test_gitlab_aggregate_ci_vocabulary() -> None:
    """The authoritative aggregate CI mapping used by BOTH projection paths.

    A `manual` aggregate is a blocking gate → running (not passed); an
    allow_failure red job is already folded into a `success` aggregate → passed;
    a wholly skipped pipeline has no failures → passed; unknown/transient →
    running; empty → nothing.
    """
    assert source._gitlab_aggregate_ci("success") == "passed"
    assert source._gitlab_aggregate_ci("skipped") == "passed"
    assert source._gitlab_aggregate_ci("failed") == "failed"
    assert source._gitlab_aggregate_ci("canceled") == "failed"
    assert source._gitlab_aggregate_ci("manual") == "running"
    assert source._gitlab_aggregate_ci("running") == "running"
    assert source._gitlab_aggregate_ci("pending") == "running"
    assert source._gitlab_aggregate_ci("waiting_for_resource") == "running"
    assert source._gitlab_aggregate_ci("waiting_for_callback") == "running"
    assert source._gitlab_aggregate_ci("") is None


def test_gitlab_check_bucket_is_faithful() -> None:
    """Per-job buckets are for the Checks list and stay faithful to the job status."""
    assert source._gitlab_check({"status": "success"})["bucket"] == "passed"
    assert source._gitlab_check({"status": "failed"})["bucket"] == "failed"
    # An allow_failure red job still shows failed — the glyph is protected by the
    # aggregate, so hiding the job would only mislead the Checks tab.
    assert source._gitlab_check({"status": "failed", "allow_failure": True})["bucket"] == "failed"
    assert source._gitlab_check({"status": "manual"})["bucket"] == "skipped"
    assert source._gitlab_check({"status": "running"})["bucket"] == "pending"


@pytest.mark.asyncio
async def test_fetch_gitlab_full_jobs_page_keeps_aggregate_authoritative(monkeypatch) -> None:
    """A truncated (full-page) job list must not poison the CI glyph.

    When the jobs list comes back as a full page it may be truncated — a failed
    job on a later page would be invisible. The glyph is projected from the
    pipeline AGGREGATE (`ciStatus`), which stays authoritative, and the Checks
    LIST is flagged partial so the panel does not imply it is exhaustive.
    """
    page = source._SECONDARY_PAGE_SIZE

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/50"):
            return {
                "iid": 50,
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/50",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "abc",
                "author": {"username": "dev"},
            }
        if "/pipelines?" in command:
            # Aggregate says FAILED (a later-page job failed).
            return [{"id": 77, "status": "failed"}]
        if "/pipelines/77/jobs" in command:
            # A full page of green jobs — the failed one is beyond this page.
            return [{"status": "success", "name": f"job{i}"} for i in range(page)]
        if "/commits?" in command or "/discussions?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/50")
    )

    # Checks list is flagged partial (may be truncated)...
    assert "checks" in data["partialSections"]
    # ...but the glyph is the authoritative aggregate ("failed"), NOT a rollup of
    # the visible all-green page.
    assert data["ciStatus"] == "failed"
    assert source.status_from_full_payload(data) == {"ci": "failed", "state": "open"}


def test_record_full_payload_clears_flap_tracker_on_change() -> None:
    """An authoritative full-payload write resets the chip flap counter.

    The flap damper counts *consecutive identical* chip transitions. A
    full-payload write that changes the status between chip refreshes is a real,
    independent change — it must clear the tracker so three legitimate repeated
    CI runs are not mistaken for a single repeating loop and falsely damped.
    """
    url = "https://github.com/acme/repo/pull/88"
    source._check_cache.clear()
    source._check_flap.clear()
    source._check_flap_damped.clear()
    source._status_delta_sinks.clear()
    # Seed a flap tracker as if a chip transition had been recorded.
    source._check_flap[url] = (("state=open", "state=merged"), 2)
    source._check_flap_damped.add(url)
    try:
        # A full-payload write that changes the cached status clears the tracker.
        source.record_full_payload_status(
            url, {"state": "MERGED", "checks": [{"bucket": "passed"}]}
        )
        assert url not in source._check_flap
        assert url not in source._check_flap_damped
    finally:
        source._check_cache.clear()
        source._check_flap.clear()
        source._check_flap_damped.clear()


@pytest.mark.asyncio
async def test_forced_refresh_inflight_not_floored_and_requeues(monkeypatch) -> None:
    """An already-in-flight URL is not floor-stamped and gets one follow-up.

    A TTL-paced chip fetch may be in flight when the turn boundary fires. Its
    result can predate the turn's final push, so the forced call must NOT record
    it as "just forced" (which would satisfy the floor with pre-turn data and
    lock out a corrective read for the floor interval); instead it queues exactly
    one follow-up forced refresh for when the in-flight fetch completes.
    """
    url = "https://github.com/acme/repo/pull/91"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    source._check_force_pending.clear()
    # Pretend a TTL-paced refresh for this URL is already in flight.
    source._check_inflight.add(url)
    try:
        started = source.request_check_refresh_now([url])
        # It was reported as refreshing (in flight)...
        assert url in started
        # ...but NOT floor-stamped (its in-flight result may be pre-turn)...
        assert url not in source._check_forced_at
        # ...and a follow-up forced read is queued for completion.
        assert url in source._check_force_pending
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_refresh_check_status_issues_pending_follow_up_force(monkeypatch) -> None:
    """When a fetch completes for a URL with a queued force, one follow-up fires."""
    url = "https://github.com/acme/repo/pull/92"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_force_pending.clear()
    source._status_delta_sinks.clear()
    source._check_force_pending.add(url)
    monkeypatch.setattr(source, "_fetch_check_status", AsyncMock(return_value={"state": "open"}))
    schedule = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", schedule)
    try:
        await source._refresh_check_status(url)
        # The queued force was consumed and exactly one follow-up forced refresh
        # was scheduled for this URL.
        assert url not in source._check_force_pending
        schedule.assert_called_once()
        args, kwargs = schedule.call_args
        assert args[0] == [url]
        assert kwargs.get("force") is True
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_follow_up_forced_read_burns_the_floor(monkeypatch) -> None:
    """A follow-up forced read that starts is floor-stamped.

    Without the stamp, a turn boundary inside the floor that lands while the
    follow-up is in flight queues another unstamped follow-up, so forced reads
    run back to back for as long as turns keep ending during reads.
    """
    url = "https://github.com/acme/repo/pull/95"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    source._check_force_pending.clear()
    source._status_delta_sinks.clear()
    real_refresh = source._refresh_check_status
    monkeypatch.setattr(source, "_fetch_check_status", AsyncMock(return_value={"state": "open"}))
    # Started follow-ups stay "in flight" until the test clears them.
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    source._check_cache[url] = (source.time.monotonic(), {"state": "open"})
    source._check_inflight.add(url)
    try:
        # Turn boundary while a paced read is in flight: one follow-up is queued.
        assert source.request_check_refresh_now([url]) == [url]
        assert url in source._check_force_pending
        # The read completes and the follow-up forced read really starts.
        await real_refresh(url)
        assert url in source._check_inflight
        assert url not in source._check_force_pending
        assert url in source._check_forced_at
        # A second boundary inside the floor, during the follow-up, is paced: it
        # queues no further follow-up and starts no other forced read.
        source.request_check_refresh_now([url])
        assert url not in source._check_force_pending
        # After the floor it is eligible again.
        source._check_forced_at[url] = (
            source.time.monotonic() - source._CHECK_FORCE_MIN_INTERVAL_SECS - 1
        )
        assert source.request_check_refresh_now([url]) == [url]
        assert url in source._check_force_pending
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_deferred_follow_up_caches_the_result_already_due(monkeypatch) -> None:
    """A follow-up deferred by a full pending cap leaves the entry due.

    The read that completed may predate the turn's final push, so it must not be
    cached as fresh for a full TTL when its corrective follow-up could not start.
    """
    url = "https://github.com/acme/repo/pull/96"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    source._check_force_pending.clear()
    source._status_delta_sinks.clear()
    monkeypatch.setattr(source, "_fetch_check_status", AsyncMock(return_value={"state": "open"}))
    real_refresh = source._refresh_check_status
    spawned = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "_refresh_check_status", spawned)
    source._check_cache[url] = (source.time.monotonic() - 120, {"state": "open"})
    for n in range(source._CHECK_PENDING_MAX):
        source._check_inflight.add(f"https://github.com/acme/other/pull/{n}")
    source._check_force_pending.add(url)
    try:
        await real_refresh(url)
        spawned.assert_not_called()
        assert url not in source._check_force_pending
        assert url not in source._check_forced_at
        entry = source._check_cache[url]
        assert entry[1] == {"state": "open"}
        assert source._chip_refresh_due(entry, source.time.monotonic(), force=False)
    finally:
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_forced_at.clear()
        source._check_force_pending.clear()


@pytest.mark.asyncio
async def test_chip_refresh_damps_projection_flap(monkeypatch) -> None:
    """A URL whose chip status keeps flapping must stop driving the invalidation loop.

    If the chip and full-payload projections ever disagree on a URL's vocabulary
    (a bug), the chip refresh observes the identical changed transition every
    cycle and the mutual-invalidation protocol would spawn provider reads
    forever. After a small number of identical transitions the loop-breaker must
    stop invalidating the full payload and emitting deltas for that URL, so the
    divergence degrades to a stale glyph rather than an unbounded loop (Arbiter
    item 2 / Design CONCERN #1).
    """
    url = "https://gitlab.com/acme/repo/-/merge_requests/9"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    source._check_flap.clear()
    source._check_flap_damped.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_full_payload_cache", invalidate)

    # Simulate the flap: the chip always projects "draft" from the provider,
    # while a full-payload write-through keeps resetting the cache to "closed"
    # between refreshes — so every refresh sees the identical closed -> draft
    # changed transition.
    monkeypatch.setattr(source, "_fetch_check_status", AsyncMock(return_value={"state": "draft"}))

    try:
        for _ in range(source._CHECK_FLAP_DAMP_THRESHOLD + 2):
            source._check_cache[url] = (source.time.monotonic(), {"state": "closed"})
            source._CACHE[url] = (source.time.monotonic(), 10, {"state": "closed"})
            source._check_inflight.discard(url)
            await source._refresh_check_status(url)

        # Once damped, the loop drivers stop firing for this URL.
        assert url in source._check_flap_damped
        # The first (threshold) transitions still invalidated/emitted; after
        # damping, neither fires — so both counts are capped below the number of
        # rounds run.
        assert invalidate.await_count == source._CHECK_FLAP_DAMP_THRESHOLD - 1
        assert sink.call_count == source._CHECK_FLAP_DAMP_THRESHOLD - 1
        # The chip cache still tracks the latest projection (glyph stays live,
        # just does not drive the loop).
        assert source.get_cached_check_status(url) == {"state": "draft"}
        # A genuinely different transition clears the damp.
        source._check_cache[url] = (source.time.monotonic(), {"state": "draft"})
        source._check_inflight.discard(url)
        monkeypatch.setattr(
            source, "_fetch_check_status", AsyncMock(return_value={"state": "merged"})
        )
        await source._refresh_check_status(url)
        assert url not in source._check_flap_damped
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()
        source._check_inflight.clear()
        source._check_flap.clear()
        source._check_flap_damped.clear()


@pytest.mark.asyncio
async def test_chip_refresh_defers_to_concurrent_full_payload_write(monkeypatch) -> None:
    """A concurrent full fetch that lands during the chip await must win.

    The turn-boundary design fires a full fetch and a forced chip refresh for
    the same URL together. If the full fetch resolves first and writes a fresh
    projection, the chip refresh must not clobber it or emit a redundant delta
    by comparing against its own stale pre-await snapshot (Arbiter item 2).
    """
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    # Pre-await snapshot the chip refresh will capture.
    source._check_cache[url] = (source.time.monotonic(), {"ci": "running", "state": "open"})
    source._CACHE[url] = (source.time.monotonic(), 10, {"state": "OPEN"})
    sink = MagicMock()
    source.register_status_delta_sink(sink)

    async def fetch_and_simulate_concurrent_full(_url):
        # While this chip fetch is "in flight", a concurrent full fetch resolves
        # first and writes the newer projection through record_full_payload_status.
        source._check_cache[_url] = (source.time.monotonic(), {"ci": "passed", "state": "merged"})
        return {"ci": "running", "state": "open"}

    monkeypatch.setattr(
        source, "_fetch_check_status", AsyncMock(side_effect=fetch_and_simulate_concurrent_full)
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_full_payload_cache", invalidate)

    try:
        await source._refresh_check_status(url)

        # The concurrent full-payload projection is preserved, not overwritten by
        # this older chip read; no spurious invalidation or chip delta fired.
        assert source._check_cache[url][1] == {"ci": "passed", "state": "merged"}
        assert url in source._CACHE
        invalidate.assert_not_called()
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._CACHE.clear()
        source._check_cache.clear()
        source._check_inflight.clear()


@pytest.mark.asyncio
async def test_request_check_refresh_now_floors_rapid_turns(monkeypatch) -> None:
    """Turn boundaries beat the TTL, but a burst of turns cannot beat the floor."""
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    # A cache entry well inside its TTL: plain scheduling would skip it entirely.
    source._check_cache[url] = (source.time.monotonic(), {"state": "open"})

    assert source.schedule_check_refresh([url]) == []
    assert source.request_check_refresh_now([url]) == [url]
    source._check_inflight.clear()
    # Second turn inside the floor falls back to TTL pacing (i.e. does nothing)
    # rather than spawning another provider read.
    assert source.request_check_refresh_now([url]) == []

    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


@pytest.mark.asyncio
async def test_request_check_refresh_now_refreshes_again_after_floor(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    monkeypatch.setattr(source, "_refresh_check_status", AsyncMock(return_value=None))
    source._check_cache[url] = (source.time.monotonic(), {"state": "open"})

    assert source.request_check_refresh_now([url]) == [url]
    source._check_inflight.clear()
    source._check_forced_at[url] = (
        source.time.monotonic() - source._CHECK_FORCE_MIN_INTERVAL_SECS - 1
    )

    assert source.request_check_refresh_now([url]) == [url]

    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


def test_status_delta_sinks_are_deduped_and_failure_isolated() -> None:
    source._status_delta_sinks.clear()
    good = MagicMock()
    boom = MagicMock(side_effect=RuntimeError("owner socket died"))
    source.register_status_delta_sink(good)
    source.register_status_delta_sink(good)
    source.register_status_delta_sink(boom)

    try:
        assert len(source._status_delta_sinks) == 2
        source._emit_status_delta("https://github.com/acme/repo/pull/1", {"state": "open"}, "chip")
        # One broken sink must not starve the others.
        good.assert_called_once()
    finally:
        source._status_delta_sinks.clear()


def test_trim_check_cache_bounds_the_force_ledger() -> None:
    source._check_cache.clear()
    source._check_forced_at.clear()
    for index in range(source._CHECK_CACHE_MAX + 5):
        source._check_forced_at[f"https://github.com/acme/repo/pull/{index}"] = float(index)

    source._trim_check_cache()

    assert len(source._check_forced_at) == source._CHECK_CACHE_MAX
    # Oldest forced timestamps are evicted first.
    assert "https://github.com/acme/repo/pull/0" not in source._check_forced_at
    source._check_forced_at.clear()


@pytest.mark.asyncio
async def test_status_endpoint_bounds_urls_and_rejects_non_list_bodies(monkeypatch) -> None:
    refresh = MagicMock(return_value=[])
    monkeypatch.setattr(source, "schedule_check_refresh", refresh)
    source._check_cache.clear()
    urls = [
        f"https://github.com/acme/repo/pull/{index + 1}"
        for index in range(source.STATUS_URLS_MAX + 5)
    ]

    app = _app()
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post("/api/source/pull-request/status", json={"urls": urls})
        assert accepted.status == 200
        rejected = await client.post("/api/source/pull-request/status", json={"urls": "all"})
        assert rejected.status == 400

    assert len(refresh.call_args.args[0]) == source.STATUS_URLS_MAX
    source._check_cache.clear()


@pytest.mark.asyncio
async def test_status_endpoint_requires_owner_identity() -> None:
    app = _app(user="U_OTHER")
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/status",
            json={"urls": ["https://github.com/acme/repo/pull/12"]},
        )
        assert response.status == 403


@pytest.mark.asyncio
async def test_schedule_check_refresh_backs_off_overflow_without_spawning(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/99"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_inflight.add("https://github.com/acme/repo/pull/1")
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 1)
    task_count = len(source._CHECK_TASKS)

    source.schedule_check_refresh([url, url])

    assert len(source._CHECK_TASKS) == task_count
    assert source._check_cache[url][1] is None
    first_timestamp = source._check_cache[url][0]
    source.schedule_check_refresh([url])
    assert source._check_cache[url][0] == first_timestamp
    source._check_cache.clear()
    source._check_inflight.clear()


def test_forced_refresh_over_cap_stays_eligible(monkeypatch) -> None:
    """A turn-boundary force deferred by the pending cap must not be locked out.

    Recording ``_check_forced_at`` (and renewing the cache timestamp) *before*
    admission would mark a URL the pending cap rejected as "just forced" (10s
    floor) and renew its TTL — so the next turn boundary AND the periodic sweep
    both skip it, making the chip staler in exactly the contention case force
    exists for.
    """
    url = "https://github.com/acme/repo/pull/77"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()
    # Saturate the pending cap with an unrelated in-flight refresh.
    monkeypatch.setattr(source, "_CHECK_PENDING_MAX", 1)
    source._check_inflight.add("https://github.com/acme/repo/pull/1")
    # Seed a known-stale chip entry with an old timestamp.
    old_ts = source.time.monotonic() - 999
    source._check_cache[url] = (old_ts, {"state": "open", "ci": "failed"})

    started = source.request_check_refresh_now([url])

    # Nothing started (cap full) and — crucially — the URL was NOT recorded as
    # forced, so the very next turn boundary can retry it immediately.
    assert url not in started
    assert url not in source._check_forced_at
    # The stale entry's timestamp is untouched, so the periodic sweep still sees
    # it as due rather than freshly refreshed.
    assert source._check_cache[url][0] == old_ts
    source._check_cache.clear()
    source._check_inflight.clear()
    source._check_forced_at.clear()


def test_record_full_payload_preserves_ci_when_checks_partial() -> None:
    """A degraded full fetch must not erase a CI glyph the chip cache knows.

    When a provider's secondary pipelines/jobs call fails, the full payload
    comes back with ``checks: []`` and ``checks`` listed in ``partialSections``.
    The projection then omits ``ci``; without the keep-known-status guard the
    write-through would blank the sidebar's failed-CI glyph everywhere.
    """
    url = "https://github.com/acme/repo/pull/34"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (source.time.monotonic(), {"state": "open", "ci": "failed"})
    try:
        # Same lifecycle, degraded checks section: nothing actually changed once
        # the known CI is carried over, so no spurious delta is emitted.
        source.record_full_payload_status(
            url, {"state": "OPEN", "draft": False, "checks": [], "partialSections": ["checks"]}
        )
        assert source.get_cached_check_status(url) == {"state": "open", "ci": "failed"}
        sink.assert_not_called()

        # Lifecycle moved (open -> merged) but checks are still partial: the new
        # state lands AND the known CI survives, and the delta carries both.
        source.record_full_payload_status(
            url, {"state": "MERGED", "checks": [], "partialSections": ["checks"]}
        )
        assert source.get_cached_check_status(url) == {"state": "merged", "ci": "failed"}
        sink.assert_called_once_with(
            {"url": url, "origin": "detail", "state": "merged", "ci": "failed"}
        )
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()


def test_record_full_payload_keeps_settled_merge_state_when_the_read_is_unsettled() -> None:
    """An unsettled merge read must not erase a settled one.

    Both providers compute mergeability lazily, so a full fetch whose evaluation
    lapsed returns ``unknown`` for a source whose conflict is already known.
    Because every writer replaces the chip entry WHOLESALE, an omitted field is
    destructive rather than neutral: without the keep-known guard this write
    strips the pair, which reads as a changed status and drives the
    chip<->full invalidation loop.
    """
    url = "https://github.com/acme/repo/pull/36"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(
            url,
            {"state": "OPEN", "checks": [], "mergeable": "unknown", "mergeStateStatus": "unknown"},
        )

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
        # Nothing changed once the known pair is carried over, so the loop that
        # would otherwise refetch the payload never starts.
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()


def test_record_full_payload_lets_a_real_merge_answer_replace_a_settled_one() -> None:
    """Carry-forward fills a gap only — it must never pin a stale verdict."""
    url = "https://github.com/acme/repo/pull/37"
    source._check_cache.clear()
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(
            url,
            {"state": "OPEN", "checks": [], "mergeable": "mergeable", "mergeStateStatus": "clean"},
        )

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "mergeable",
            "mergeStateStatus": "clean",
        }
    finally:
        source._check_cache.clear()


def test_record_full_payload_stops_carrying_merge_state_once_the_source_closes() -> None:
    """A merged/closed source stops being asked about mergeability at all.

    Carrying the pair forward there would pin it permanently, because no later
    read can ever supply a real answer to replace it.
    """
    url = "https://github.com/acme/repo/pull/38"
    source._check_cache.clear()
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    try:
        source.record_full_payload_status(url, {"state": "MERGED", "checks": []})

        assert source.get_cached_check_status(url) == {"state": "merged"}
    finally:
        source._check_cache.clear()


@pytest.mark.asyncio
async def test_chip_refresh_keeps_settled_merge_state_and_starts_no_invalidation_loop(
    monkeypatch,
) -> None:
    """The chip refresh path needs the same keep-known rule as the full writer.

    A settled conflict followed by an unsettled poll is the exact sequence that
    made the pair vanish from the owner-gated sidebar payload (which spreads the
    entry whole) and judged itself "changed", spinning the invalidation loop.
    """
    url = "https://github.com/acme/repo/pull/39"
    source._check_cache.clear()
    source._check_inflight.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (
        source.time.monotonic(),
        {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"},
    )
    monkeypatch.setattr(
        source,
        "_fetch_check_status",
        AsyncMock(return_value={"state": "open"}),
    )
    try:
        await source._refresh_check_status(url)

        assert source.get_cached_check_status(url) == {
            "state": "open",
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
        sink.assert_not_called()
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()
        source._check_inflight.clear()


def test_record_full_payload_clears_ci_when_checks_genuinely_empty() -> None:
    """The guard must be scoped to PARTIAL checks, not merely-empty ones.

    A PR with no CI configured returns ``checks: []`` and NO ``partialSections``
    entry for checks. That is authoritative "there is no CI", so an already-known
    glyph should clear rather than linger forever.
    """
    url = "https://github.com/acme/repo/pull/35"
    source._check_cache.clear()
    source._status_delta_sinks.clear()
    sink = MagicMock()
    source.register_status_delta_sink(sink)
    source._check_cache[url] = (source.time.monotonic(), {"state": "open", "ci": "passed"})
    try:
        source.record_full_payload_status(url, {"state": "OPEN", "checks": []})
        assert source.get_cached_check_status(url) == {"state": "open"}
        sink.assert_called_once_with({"url": url, "origin": "detail", "state": "open"})
    finally:
        source.unregister_status_delta_sink(sink)
        source._check_cache.clear()
