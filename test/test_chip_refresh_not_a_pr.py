"""A chip whose number GitHub says is not a pull request is not re-read every minute.

A cut sub-agent task preview left a link to ``pull/4316``, an issue. Every chip
read failed with ``Could not resolve to a PullRequest``, cached no status, and
so was due again one chip TTL later: two ``gh`` calls a minute for as long as
the tab stayed in the sidebar. That answer does not change on the next read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Imported first: it loads the source_providers owner chain in its working order.
from kiro_crew.dashboard.handlers import source_providers  # noqa: F401
from kiro_crew.dashboard.source_providers import chip_refresh, chip_status, runner
from kiro_crew.testing.wait import await_until

_NOT_A_PR = (
    b"GraphQL: Could not resolve to a PullRequest with the number of 4316. "
    b"(repository.pullRequest)\n"
)
_URL = "https://github.com/acme/repo/pull/4316"


@pytest.fixture(autouse=True)
def _clean_chip_state(_floor_monkeypatch):
    stores = (
        chip_status._check_cache,
        chip_status._check_inflight,
        chip_status._check_forced_at,
        chip_status._check_force_pending,
        chip_status._check_generations,
    )
    for store in stores:
        store.clear()
    mp = _floor_monkeypatch
    mp.setattr(chip_refresh, "_idle_check_window", [float("-inf"), 0.0], raising=False)
    # A status change starts a repository visibility read (a real ``gh repo view``)
    # and notifies owner sinks; neither belongs to these tests.
    mp.setattr(chip_status, "schedule_visibility_refresh", lambda *a, **k: None)
    mp.setattr(chip_status, "_status_delta_sinks", set())
    mp.setattr(chip_status, "_check_flap", {})
    mp.setattr(chip_status, "_check_flap_damped", set())
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def clock(monkeypatch) -> dict[str, float]:
    now = {"now": 100_000.0}
    monkeypatch.setattr(chip_refresh, "time", SimpleNamespace(monotonic=lambda: now["now"]))
    return now


def _failing_reads(monkeypatch, stderr: bytes) -> list[str]:
    """Replace the provider read with one that fails as ``gh`` does with *stderr*."""
    reads: list[str] = []

    async def _fetch(url: str) -> None:
        reads.append(url)
        raise runner._provider_failure("gh", stderr)

    monkeypatch.setattr(chip_refresh, "_fetch_check_status", _fetch)
    return reads


async def _drain() -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in chip_refresh._CHECK_TASKS if task.get_loop() is loop]
    if tasks:
        await await_until(asyncio.gather(*tasks), what="the chip reads")


@pytest.mark.asyncio
async def test_no_sweep_rereads_a_number_that_is_not_a_pull_request(monkeypatch, clock):
    reads = _failing_reads(monkeypatch, _NOT_A_PR)
    assert chip_refresh.schedule_check_refresh([_URL]) == [_URL]
    await _drain()

    clock["now"] += 10 * chip_status._CHECK_TTL_SECS
    assert chip_refresh.schedule_check_refresh([_URL]) == []
    assert chip_refresh.schedule_idle_check_refresh([_URL]) == []
    assert chip_refresh.request_check_refresh_now([_URL]) == []
    await _drain()
    assert reads == [_URL]


@pytest.mark.asyncio
async def test_expanded_refresh_skips_suppressed_urls_before_capacity(monkeypatch, clock):
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", refresh)
    busy = {f"https://github.com/acme/repo/pull/{900 + n}" for n in range(4)}
    chip_status._check_inflight.update(busy)
    free = chip_refresh._CHECK_PENDING_MAX - len(busy)
    marked = [f"https://github.com/acme/repo/pull/{n}" for n in range(1, free + 2)]
    genuine = "https://github.com/acme/repo/pull/100"
    assert len(marked) > free
    for offset, url in enumerate(marked):
        chip_status._check_cache[url] = (clock["now"] - 10_000 - offset, None)
        chip_status._check_not_a_change[url] = clock["now"] - 1
    chip_status._check_cache[genuine] = (
        clock["now"] - chip_status._CHECK_TTL_SECS - 1,
        {"state": "open"},
    )

    assert chip_refresh.schedule_expanded_check_refresh([*marked, genuine]) == [genuine]
    await _drain()
    refresh.assert_awaited_once_with(genuine, None)


@pytest.mark.asyncio
async def test_idle_unread_refresh_skips_suppressed_urls_before_capacity(monkeypatch, clock):
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", refresh)
    busy = {f"https://github.com/acme/repo/pull/{900 + n}" for n in range(4)}
    chip_status._check_inflight.update(busy)
    free = chip_refresh._CHECK_PENDING_MAX - len(busy)
    marked = [f"https://github.com/acme/repo/pull/{n}" for n in range(1, free + 2)]
    genuine = "https://github.com/acme/repo/pull/100"
    assert len(marked) > free
    for offset, url in enumerate(marked):
        chip_status._check_cache[url] = (clock["now"] - 10_000 - offset, None)
        chip_status._check_not_a_change[url] = clock["now"] - 1
    chip_status._check_cache[genuine] = (
        clock["now"] - chip_status._CHECK_TTL_SECS - 1,
        None,
    )

    assert chip_refresh.schedule_idle_check_refresh([*marked, genuine]) == [genuine]
    await _drain()
    refresh.assert_awaited_once_with(genuine, None)


@pytest.mark.asyncio
async def test_the_not_a_pull_request_answer_is_read_again_once_it_ages_out(monkeypatch, clock):
    reads = _failing_reads(monkeypatch, _NOT_A_PR)
    chip_refresh.schedule_check_refresh([_URL])
    await _drain()

    clock["now"] += chip_refresh._NOT_A_CHANGE_TTL_SECS
    assert chip_refresh.schedule_check_refresh([_URL]) == [_URL]
    await _drain()
    assert reads == [_URL, _URL]


@pytest.mark.asyncio
async def test_a_full_read_that_finds_a_pull_request_drops_the_answer(monkeypatch, clock):
    reads = _failing_reads(monkeypatch, _NOT_A_PR)
    chip_refresh.schedule_check_refresh([_URL])
    await _drain()

    # The number became a pull request and the detail panel read it, then a
    # mutation dropped the chip entry: the chip must be read again.
    chip_status.record_full_payload_status(_URL, {"state": "OPEN", "checks": []})
    chip_status._invalidate_check_status(_URL)
    assert chip_refresh.schedule_check_refresh([_URL]) == [_URL]
    await _drain()
    assert reads == [_URL, _URL]


@pytest.mark.asyncio
async def test_a_read_older_than_a_full_read_leaves_no_answer(monkeypatch, clock):
    started, release = asyncio.Event(), asyncio.Event()

    async def _fetch(url: str) -> None:
        started.set()
        await release.wait()
        raise runner._provider_failure("gh", _NOT_A_PR)

    monkeypatch.setattr(chip_refresh, "_fetch_check_status", _fetch)
    chip_refresh.schedule_check_refresh([_URL])
    await await_until(started, what="the chip read to start")
    # While that read is out, the detail panel finds the pull request.
    chip_status.record_full_payload_status(_URL, {"state": "OPEN", "checks": []})
    release.set()
    await _drain()
    assert _URL not in chip_status._check_not_a_change


@pytest.mark.asyncio
async def test_any_other_failure_is_still_retried_after_the_chip_ttl(monkeypatch, clock):
    reads = _failing_reads(monkeypatch, b"error connecting to api.github.com\n")
    chip_refresh.schedule_check_refresh([_URL])
    await _drain()

    clock["now"] += chip_status._CHECK_TTL_SECS
    assert chip_refresh.schedule_check_refresh([_URL]) == [_URL]
    await _drain()
    assert reads == [_URL, _URL]
