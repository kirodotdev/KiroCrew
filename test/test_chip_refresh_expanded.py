"""The expand's chip read admits only what the pending cap has room for.

Handing a whole expanded list to ``schedule_check_refresh`` would let the cap
stamp every overflow URL as backed off with no status, so the same first URLs
would win on every expand and the tail would never be read.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

# Imported first: it loads the source_providers owner chain in its working order.
from kiro_crew.dashboard.handlers import source_providers  # noqa: F401
from kiro_crew.dashboard.source_providers import chip_refresh, chip_status
from kiro_crew.testing.wait import await_until


def _pr(number: int) -> str:
    return f"https://github.com/acme/repo/pull/{number}"


@pytest.fixture(autouse=True)
def _clean_chip_state(_floor_monkeypatch):
    stores = (
        chip_status._check_cache,
        chip_status._check_inflight,
        chip_status._check_forced_at,
        chip_status._check_force_pending,
    )
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


async def _drain() -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in chip_refresh._CHECK_TASKS if task.get_loop() is loop]
    if tasks:
        await await_until(asyncio.gather(*tasks), what="the chip reads")


@pytest.mark.asyncio
async def test_a_long_list_reads_only_the_free_capacity_and_stamps_no_overflow(monkeypatch):
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", AsyncMock(return_value=None))
    busy = {_pr(900 + n) for n in range(4)}
    chip_status._check_inflight.update(busy)
    urls = [_pr(n) for n in range(1, 41)]

    free = chip_refresh._CHECK_PENDING_MAX - len(busy)
    assert chip_refresh.schedule_expanded_check_refresh(urls) == urls[:free]
    # The overflow was neither read nor stamped, so it is still due next time.
    assert not any(url in chip_status._check_cache for url in urls[free:])
    await _drain()


@pytest.mark.asyncio
async def test_never_read_urls_go_first_and_fresh_or_inflight_ones_are_skipped(monkeypatch):
    monkeypatch.setattr(chip_refresh, "_refresh_check_status", AsyncMock(return_value=None))
    now = chip_refresh.time.monotonic()
    fresh, stale, unread, running = _pr(1), _pr(2), _pr(3), _pr(4)
    chip_status._check_cache[fresh] = (now, {"state": "open"})
    chip_status._check_cache[stale] = (now - 10_000, {"state": "open"})
    chip_status._check_inflight.add(running)

    assert chip_refresh.schedule_expanded_check_refresh([fresh, stale, unread, running]) == [
        unread,
        stale,
    ]
    await _drain()
