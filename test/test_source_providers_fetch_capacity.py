"""Concurrent reads: coalescing of full and checks fetches, the direct-fetch byte
reservation and pool queueing, and the retryable capacity error a client sees."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app, _rollup_reply

from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import github as github_source


@pytest.mark.asyncio
async def test_fetch_github_checks_uses_one_call_without_rewriting_cache(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE.clear()
    source._CACHE[url] = (1.0, 21, {"provider": "github", "checks": []})
    cached = source._CACHE[url]
    run = AsyncMock(
        return_value=_rollup_reply(
            [{"name": "test", "status": "IN_PROGRESS", "conclusion": "SUCCESS"}]
        )
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(url)

    run.assert_awaited_once_with(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={github_source._GITHUB_ROLLUP_QUERY}",
        "-f",
        "o=acme",
        "-f",
        "r=repo",
        "-F",
        "n=12",
        max_output_bytes=source._CHECKS_OUTPUT_BYTES,
        host="github.com",
    )
    assert checks[0]["bucket"] == "pending"
    assert source._CACHE[url] == cached
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_full_fetch_coalesces_concurrent_forced_refreshes(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    release = source.asyncio.Event()
    started = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    url = "https://github.com/acme/repo/pull/12"
    first = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await started.wait()
    second = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await source.asyncio.sleep(0)
    release.set()

    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert await first == await second
    assert calls == 1
    await source.asyncio.sleep(0)
    assert url not in source._FULL_FETCH_INFLIGHT
    assert url not in source._FULL_FETCH_TASKS
    assert url not in source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_full_fetch_projects_chip_status_under_cache_lock(monkeypatch) -> None:
    """The write-through projection must run inside ``_CACHE_LOCK``.

    Guards the TOCTOU where a provider mutation landing between the
    passing generation check and the chip projection could republish
    pre-mutation status into the chip cache — a stale ``source_status`` delta the
    full-cache invalidation cannot undo. Keeping the projection in the same
    locked transaction as the generation check and the ``_CACHE`` write closes
    the window: a mutation needs the same lock to bump the generation.
    """
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._check_cache.clear()

    async def fetch(ref):
        return {"provider": "github", "url": ref.url, "state": "OPEN"}

    monkeypatch.setattr(source, "_fetch_github", fetch)

    observed: dict[str, bool] = {}
    real_record = source.record_full_payload_status

    def spy(url, payload):
        # `_fetch_pull_request_uncached` calls the bare module global, so this
        # patched name is what it resolves at call time.
        observed["locked"] = source._CACHE_LOCK.locked()
        return real_record(url, payload)

    monkeypatch.setattr(source, "record_full_payload_status", spy)

    url = "https://github.com/acme/repo/pull/12"
    await source.fetch_pull_request(url, refresh=True)

    assert observed.get("locked") is True
    source._CACHE.clear()
    source._check_cache.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_resolve_supersedes_active_full_fetch(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    old_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    state = {"resolved": False}
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        resolved = state["resolved"]
        if calls == 1:
            old_started.set()
            # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_old.wait()
        return {"provider": "github", "url": ref.url, "resolved": resolved}

    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_thread1"}]}}}
        }
    }

    async def run(*argv: str, **_kwargs: int):
        if any("resolveReviewThread" in part and "mutation" in part for part in argv):
            state["resolved"] = True
            return {}
        return membership

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    stale_task = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await old_started.wait()

    await source.resolve_pull_request_thread(url, "PRRT_thread1")
    # flake-ok: pre-existing 0.5 s ceiling proving the fresh fetch does not join the held stale fetch, moved verbatim; tracked for a de-flake pass
    fresh = await source.asyncio.wait_for(source.fetch_pull_request(url, refresh=True), timeout=0.5)
    assert fresh["resolved"] is True
    assert calls == 2

    release_old.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    stale = await stale_task
    assert stale["resolved"] is False
    assert source._CACHE[url][2]["resolved"] is True
    await source.asyncio.sleep(0)
    assert url not in source._FULL_FETCH_INFLIGHT
    assert url not in source._FULL_FETCH_TASKS
    assert url not in source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_checks_fetch_coalesces_concurrent_requests(monkeypatch) -> None:
    source._CHECKS_FETCH_INFLIGHT.clear()
    release = source.asyncio.Event()
    started = source.asyncio.Event()
    calls = 0

    async def fetch(_ref):
        nonlocal calls
        calls += 1
        started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return [{"name": "test", "bucket": "pending"}]

    monkeypatch.setattr(source, "_fetch_github_checks", fetch)
    url = "https://github.com/acme/repo/pull/12"
    first = source.asyncio.create_task(source.fetch_pull_request_checks(url))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await started.wait()
    second = source.asyncio.create_task(source.fetch_pull_request_checks(url))
    await source.asyncio.sleep(0)
    release.set()

    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert await first == await second
    assert calls == 1
    await source.asyncio.sleep(0)
    assert not source._CHECKS_FETCH_INFLIGHT


@pytest.mark.asyncio
async def test_direct_fetch_pending_bound_is_combined_and_coalesces(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    release = source.asyncio.Event()
    full_started = source.asyncio.Event()
    checks_started = source.asyncio.Event()

    async def fetch_full(ref):
        full_started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    async def fetch_checks(_ref):
        checks_started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return [{"name": "test", "bucket": "pending"}]

    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 16)
    # Admission now WAITS for room before refusing, so a test asserting the
    # ceiling has to shrink the budget or it would sit out the real one.
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        source._FULL_FETCH_RESERVATION_BYTES + source._CHECKS_FETCH_RESERVATION_BYTES,
    )
    monkeypatch.setattr(source, "_fetch_github", fetch_full)
    monkeypatch.setattr(source, "_fetch_github_checks", fetch_checks)
    full_url = "https://github.com/acme/repo/pull/20"
    checks_url = "https://github.com/acme/repo/pull/21"
    overflow_url = "https://github.com/acme/repo/pull/22"

    full = source.asyncio.create_task(source.fetch_pull_request(full_url, refresh=True))
    checks = source.asyncio.create_task(source.fetch_pull_request_checks(checks_url))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await full_started.wait()
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await checks_started.wait()
    duplicate = source.asyncio.create_task(source.fetch_pull_request(full_url, refresh=True))
    await source.asyncio.sleep(0)

    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request(overflow_url, refresh=True)
    assert len(source._direct_fetch_tasks()) == 2
    assert len(source._DIRECT_FETCH_RESERVATIONS) == 2
    assert sum(source._DIRECT_FETCH_RESERVATIONS.values()) == (
        source._FULL_FETCH_RESERVATION_BYTES + source._CHECKS_FETCH_RESERVATION_BYTES
    )
    assert len(source._FULL_FETCH_INFLIGHT) <= 2
    assert len(source._CHECKS_FETCH_INFLIGHT) <= 2

    release.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert await full == await duplicate
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await checks
    await source.asyncio.sleep(0)
    assert not source._FULL_FETCH_INFLIGHT
    assert not source._FULL_FETCH_TASKS
    assert not source._FULL_FETCH_GENERATIONS
    assert not source._CHECKS_FETCH_INFLIGHT
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_shared_fetch_and_reservation(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    started = source.asyncio.Event()
    release = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    url = "https://github.com/acme/repo/pull/22"
    waiter = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await started.wait()
    waiter.cancel()
    with pytest.raises(source.asyncio.CancelledError):
        # flake-ok: pre-existing unbounded await of the waiter the test just cancelled, moved verbatim; tracked for a de-flake pass
        await waiter

    assert url in source._FULL_FETCH_INFLIGHT
    assert list(source._DIRECT_FETCH_RESERVATIONS.values()) == [
        source._FULL_FETCH_RESERVATION_BYTES
    ]
    coalesced = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    await source.asyncio.sleep(0)
    assert calls == 1

    release.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert (await coalesced)["url"] == url
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_stale_and_fresh_full_fetches_fit_exact_reservation_ceiling(
    monkeypatch,
) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    old_started = source.asyncio.Event()
    fresh_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    release_fresh = source.asyncio.Event()
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        if calls == 1:
            old_started.set()
            # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_old.wait()
        else:
            fresh_started.set()
            # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_fresh.wait()
        return {"provider": "github", "url": ref.url, "call": calls}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        2 * source._FULL_FETCH_RESERVATION_BYTES,
    )
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    url = "https://github.com/acme/repo/pull/23"
    stale = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await old_started.wait()
    await source._invalidate_pull_request_cache(url)
    fresh = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await fresh_started.wait()

    assert len(source._DIRECT_FETCH_RESERVATIONS) == 2
    assert sum(source._DIRECT_FETCH_RESERVATIONS.values()) == (
        source._DIRECT_FETCH_MAX_RESERVED_BYTES
    )
    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request_checks("https://github.com/acme/repo/pull/24")

    release_old.set()
    release_fresh.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await stale
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await fresh
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_direct_fetch_bound_counts_detached_stale_full_task(monkeypatch) -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    old_started = source.asyncio.Event()
    release_old = source.asyncio.Event()
    state = {"resolved": False}
    calls = 0

    async def fetch(ref):
        nonlocal calls
        calls += 1
        resolved = state["resolved"]
        if calls == 1:
            old_started.set()
            # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_old.wait()
        return {"provider": "github", "url": ref.url, "resolved": resolved}

    membership = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}}
    }

    async def run(*argv: str, **_kwargs: int):
        if any("resolveReviewThread" in part and "mutation" in part for part in argv):
            state["resolved"] = True
            return {}
        return membership

    monkeypatch.setattr(source, "_DIRECT_FETCH_PENDING_MAX", 1)
    monkeypatch.setattr(source, "_DIRECT_FETCH_WAIT_SECS", 0.05)
    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/23"
    stale = source.asyncio.create_task(source.fetch_pull_request(url, refresh=True))
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await old_started.wait()
    await source.resolve_pull_request_thread(url, "PRRT_1")

    assert url not in source._FULL_FETCH_INFLIGHT
    assert len(source._direct_fetch_tasks()) == 1
    assert list(source._DIRECT_FETCH_RESERVATIONS.values()) == [
        source._FULL_FETCH_RESERVATION_BYTES
    ]
    with pytest.raises(source.SourceProviderError, match="requests are pending"):
        await source.fetch_pull_request(url, refresh=True)

    release_old.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    assert (await stale)["resolved"] is False
    await source.asyncio.sleep(0)
    assert not source._direct_fetch_tasks()
    assert not source._DIRECT_FETCH_RESERVATIONS
    fresh = await source.fetch_pull_request(url, refresh=True)
    assert fresh["resolved"] is True
    await source.asyncio.sleep(0)
    assert not source._FULL_FETCH_INFLIGHT
    assert not source._FULL_FETCH_TASKS
    assert not source._FULL_FETCH_GENERATIONS
    source._CACHE.clear()


def _reset_direct_fetch_state() -> None:
    source._CACHE.clear()
    source._FULL_FETCH_INFLIGHT.clear()
    source._FULL_FETCH_TASKS.clear()
    source._FULL_FETCH_GENERATIONS.clear()
    source._CHECKS_FETCH_INFLIGHT.clear()
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()
    source._DIRECT_FETCH_RESERVATIONS.clear()
    source._DIRECT_FETCH_WAITERS.clear()


def _reserved_bytes() -> int:
    tasks = source._direct_fetch_tasks()
    return sum(
        amount
        for task, amount in source._DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )


@pytest.mark.asyncio
async def test_saturated_pool_queues_the_next_fetch_instead_of_refusing(monkeypatch) -> None:
    """A caller arriving at a full pool WAITS for room and then succeeds.

    The panel's read is a user-facing load: refusing it outright turned ordinary
    concurrent use (two windows on two PRs) into an error card with a manual
    Retry. Waiting keeps the memory ceiling intact while removing the dead end.
    """
    _reset_direct_fetch_state()
    release = source.asyncio.Event()
    started = 0

    async def fetch(ref):
        nonlocal started
        started += 1
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source,
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        2 * source._FULL_FETCH_RESERVATION_BYTES,
    )
    first = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/1", refresh=True)
    )
    second = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/2", refresh=True)
    )
    while started < 2:
        await source.asyncio.sleep(0)
    assert _reserved_bytes() == source._DIRECT_FETCH_MAX_RESERVED_BYTES

    queued = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/3", refresh=True)
    )
    for _ in range(10):
        await source.asyncio.sleep(0)
    # Queued, not resolved and not failed, and it did not breach the ceiling to
    # get there.
    assert not queued.done()
    assert source._DIRECT_FETCH_WAITERS
    assert _reserved_bytes() <= source._DIRECT_FETCH_MAX_RESERVED_BYTES

    release.set()
    # flake-ok: pre-existing 5 s ceiling on the queued fetch the test released, moved verbatim; tracked for a de-flake pass
    assert (await source.asyncio.wait_for(queued, timeout=5))["url"] == (
        "https://github.com/acme/repo/pull/3"
    )
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await first
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await second
    await source.asyncio.sleep(0)
    assert not source._DIRECT_FETCH_RESERVATIONS
    assert not source._DIRECT_FETCH_WAITERS
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_queued_fetch_waits_outside_the_cache_lock(monkeypatch) -> None:
    """The admission wait must not hold ``_CACHE_LOCK``.

    An in-flight fetch takes the same lock to write its result, so waiting for
    capacity while holding it would deadlock: the queued caller would block the
    very completion that frees its room. This is the regression guard for that
    shape -- move the wait back inside the lock and this test times out.
    """
    _reset_direct_fetch_state()
    release = source.asyncio.Event()
    started = 0

    async def fetch(ref):
        nonlocal started
        started += 1
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "url": ref.url}

    monkeypatch.setattr(source, "_fetch_github", fetch)
    monkeypatch.setattr(
        source, "_DIRECT_FETCH_MAX_RESERVED_BYTES", source._FULL_FETCH_RESERVATION_BYTES
    )
    holder = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/4", refresh=True)
    )
    while started < 1:
        await source.asyncio.sleep(0)
    queued = source.asyncio.create_task(
        source.fetch_pull_request("https://github.com/acme/repo/pull/5", refresh=True)
    )
    for _ in range(10):
        await source.asyncio.sleep(0)
    assert not queued.done()

    release.set()
    # Both complete: the holder could still take _CACHE_LOCK to write its cache
    # entry while the queued caller was waiting.
    # flake-ok: pre-existing 5 s ceiling on the holder fetch the test released, moved verbatim; tracked for a de-flake pass
    assert await source.asyncio.wait_for(holder, timeout=5)
    # flake-ok: pre-existing 5 s ceiling on the queued fetch the test released, moved verbatim; tracked for a de-flake pass
    assert await source.asyncio.wait_for(queued, timeout=5)
    source._CACHE.clear()


@pytest.mark.asyncio
async def test_capacity_error_is_marked_retryable_for_the_client(monkeypatch) -> None:
    """The wait-budget error carries ``code: source_busy``.

    A generic provider error (auth, missing PR) must stay fail-fast in the UI, so
    the retryable case needs its own machine-readable marker rather than the
    client pattern-matching on prose.
    """
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceCapacityError("Too many source requests are pending.")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert response.status == 503
        assert (await response.json())["code"] == "source_busy"


@pytest.mark.asyncio
async def test_provider_error_is_not_marked_retryable(monkeypatch) -> None:
    """Revert guard for the marker: a real provider failure must NOT be busy."""
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceProviderError("gh could not authenticate")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert response.status == 503
        assert (await response.json())["code"] == "provider_error"


@pytest.mark.asyncio
async def test_provider_error_ships_its_login_command_as_a_field(monkeypatch) -> None:
    """A self-managed GitLab remedy reaches the client structurally, not as prose to parse."""
    _reset_direct_fetch_state()
    command = "glab auth login --hostname git.example.com"
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceProviderError("glab: 401", login_command=command)),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert response.status == 503
        assert (await response.json())["loginCommand"] == command

    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source, "fetch_pull_request", AsyncMock(side_effect=source.SourceProviderError("gh: 404"))
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
        assert "loginCommand" not in await response.json()


@pytest.mark.asyncio
async def test_capacity_error_audits_its_own_reason(monkeypatch, _mock_source_sel) -> None:
    """The audit reason must match the code the caller receives.

    Back-pressure recorded as ``provider_error`` would read, in the audit trail,
    as the provider having failed. The pairing lives in one place so the two
    cannot drift.
    """
    _reset_direct_fetch_state()
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceCapacityError("Too many source requests are pending.")),
    )
    async with TestClient(TestServer(_app())) as client:
        await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/9"}
        )
    reasons = [call.kwargs.get("error") for call in _mock_source_sel.log_api_access.call_args_list]
    assert "capacity_exhausted" in reasons
    assert "provider_error" not in reasons


@pytest.mark.asyncio
async def test_capacity_error_remains_a_source_provider_error() -> None:
    """Every existing ``except SourceProviderError`` site must still catch it."""
    assert issubclass(source.SourceCapacityError, source.SourceProviderError)
