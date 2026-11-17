"""Releasing shared claims survives a cancel that lands while it runs.

The release is the rollback of a cancelled ``warm_mint_all``, and a second cancel can reach it
there (a dropped request, then gateway shutdown). A row left ``minting`` with no caller holding
its token is never withdrawn and keeps the shared process from ever retiring, so every claimed
row must leave the table, and its holdings must still be released, however the release is cut.
"""

from __future__ import annotations

import asyncio
from typing import Any

import connections_warm_helpers
import pytest
from connections_warm_helpers import _claim, _provider

from kiro_crew.connections import warm
from kiro_crew.connections.mint import _mints
from kiro_crew.testing.wait import async_wait_until, await_until

_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime
_stub_activation = connections_warm_helpers._stub_activation


class _HeldDispose:
    """A ``_dispose_mint`` that parks until released, recording every row it was handed."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.disposed: list[str] = []

    async def __call__(self, entry: Any) -> None:
        self.entered.set()
        await self.release.wait()  # released by the test once its cancel has landed
        self.disposed.append(str(entry.get("token")))


@pytest.mark.asyncio
async def test_a_cancel_mid_release_still_removes_every_claimed_row(
    monkeypatch: pytest.MonkeyPatch,
):
    claims = await _claim("linear", "notion")
    held = _HeldDispose()
    monkeypatch.setattr(warm, "_dispose_mint", held)

    task = asyncio.create_task(warm._release_shared_claims(claims))
    try:
        await async_wait_until(held.entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled release")
        assert _mints == {}, "no claimed row may stay minting with no caller"
        assert warm._shared_mints_pending() is False
    finally:
        held.release.set()
    await async_wait_until(lambda: len(held.disposed) == 2)
    assert sorted(held.disposed) == sorted(claims.values())


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_rollback_strands_no_row(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    activating = asyncio.Event()

    async def _hangs(*, slug: str = "") -> None:
        activating.set()
        await asyncio.Event().wait()  # cancelled by the test below

    held = _HeldDispose()
    monkeypatch.setattr(warm, "_warm_activate", _hangs)
    monkeypatch.setattr(warm, "_dispose_mint", held)

    task = asyncio.create_task(warm.warm_mint_all([_provider("linear"), _provider("notion")]))
    try:
        await async_wait_until(activating.is_set)
        task.cancel()  # the first cancel: the rollback starts and parks in a dispose
        await async_wait_until(held.entered.is_set)
        task.cancel()  # the second cancel lands inside the rollback
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the twice-cancelled warm mint")
        assert _mints == {}
        assert warm._shared_mints_pending() is False
    finally:
        held.release.set()
    await async_wait_until(lambda: len(held.disposed) == 2)
