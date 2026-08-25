"""Warm-table tests: the row-liveness registry and the withdrawal chokepoint."""

from __future__ import annotations

import pytest

from kiro_crew.connections import warm
from kiro_crew.connections.mint import _mints


@pytest.fixture(autouse=True)
def _clean_mint_table():
    _mints.clear()
    yield
    _mints.clear()


class _Runtime:
    """A stand-in for one kiro-cli process, with the liveness answer we choose."""

    def __init__(self, alive: bool | BaseException) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        if isinstance(self._alive, BaseException):
            raise self._alive
        return self._alive


# ── redeemability takes TWO questions, and they die independently ──


def test_a_row_stamped_with_no_holder_at_all_is_never_alive():
    assert warm._warm_mint.generation_is_live(0) is False
    assert warm._warm_mint.activation_is_live(0) is False


def test_the_current_generation_is_live_exactly_while_its_process_is(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    assert warm._warm_mint.generation_is_live(4) is True
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    assert warm._warm_mint.generation_is_live(4) is False


def test_a_parked_generation_stays_live_while_its_own_process_can_still_redeem(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked process still holds its peers' verifiers, so answering False for it would
    withdraw a URL the user can still redeem."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(3, _Runtime(True)), (4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(3) is True
    assert warm._warm_mint.generation_is_live(4) is False
    assert warm._warm_mint.generation_is_live(5) is False


def test_a_liveness_probe_that_raises_reads_as_dead_rather_than_failing_the_scan():
    """``expire_dead_mints`` runs on every status request, so a raising probe must not
    take the request down with it."""
    assert warm._runtime_alive(_Runtime(OSError("no such process"))) is False
    assert warm._runtime_alive(None) is False


def test_a_live_process_with_a_dead_session_does_not_keep_a_row_alive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Process liveness alone passed a terminated-session row -- the observed failure that
    put the session question into the predicate at all."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 2)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    row = {"state": "waiting", "shared": True, "generation": 2, "activation": 6}
    assert warm._warm_row_alive(row) is False  # type: ignore[arg-type]
    monkeypatch.setattr(warm._warm_mint, "_sessions", {6: object()})
    assert warm._warm_row_alive(row) is True  # type: ignore[arg-type]


# ── withdrawal is keyed on the FACT that the holder is gone ──


@pytest.mark.asyncio
async def test_a_row_whose_generation_is_gone_is_withdrawn():
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 99,
        "activation": 1,
    }
    assert await warm.expire_dead_mints() == ["linear"]
    assert _mints["linear"]["state"] == "expired"
    assert _mints["linear"]["reason"] == "mint_process_gone"


@pytest.mark.asyncio
async def test_a_cold_row_is_left_to_the_cold_engine():
    """``_mint_holder_alive`` answers False for a shared row, so the warm chokepoint
    must judge only shared rows -- and leave a cold row's own verdict alone."""
    _mints["linear"] = {"state": "waiting", "oauth_url": "https://cold", "client": object()}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_a_shared_row_not_yet_serving_a_url_is_left_alone():
    """Only a row actually SERVING a URL can be serving a dead one; a claim still minting
    is the activation's to fill or release."""
    _mints["linear"] = {"state": "minting", "shared": True, "generation": 99}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "minting"


@pytest.mark.asyncio
async def test_a_row_whose_process_and_session_both_live_keeps_its_url(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: object()})
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 5,
        "activation": 2,
    }
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"
