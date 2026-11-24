"""The waiting-on-you store's agent clear and its one-ask retirement.

The agent's own clear keeps the retired records the next turn still owes it. Retiring
one ask by its ``ask_id`` is idempotent: the row that answered it and the turn that row
opens both ask, and only the first changes anything.
"""

from __future__ import annotations

import pytest
from waiting_on_you_helpers import (  # noqa: F401 -- fixtures are requested by name
    SLOT,
    ask,
    clock_fixture,
    new_store,
    pin,
    store_fixture,
)

from kiro_crew.dashboard import waiting_on_you as store_mod


@pytest.mark.asyncio
async def test_the_agents_own_clear_keeps_the_retired_records(store):
    pinned = await pin(store, ask())
    await store.dismiss(SLOT, "scope-override", pinned["ask_id"])

    await store.clear(SLOT, None)

    _pinned, records, dropped = await store.turn_view(SLOT)
    assert ([r["reason"] for r in records], dropped) == ([store_mod.REASON_DISMISSED], 0)


@pytest.mark.asyncio
async def test_retiring_one_ask_is_idempotent_and_survives_a_restart(store, clock):
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))

    gone = await store.retire(SLOT, first["ask_id"], store_mod.REASON_ANSWERED)
    again = await store.retire(SLOT, first["ask_id"], store_mod.REASON_ANSWERED)

    assert gone is not None and gone["key"] == "first" and gone["reason"] == "answered"
    assert again is None
    restarted = new_store(clock)
    await restarted.warm()
    assert [a["key"] for a in restarted.asks(SLOT)] == ["second"]
    retired = restarted.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("first", "answered")]


@pytest.mark.asyncio
async def test_retiring_a_replaced_declaration_leaves_its_replacement_pinned(store):
    old = await pin(store, ask())
    new = await pin(store, {**ask(), "title": "Replaced"})

    assert await store.retire(SLOT, old["ask_id"], store_mod.REASON_ANSWERED) is None
    assert [a["ask_id"] for a in store.asks(SLOT)] == [new["ask_id"]]


async def _retire_in_turn(store, count: int) -> None:
    for n in range(count):
        pinned = await pin(store, ask(f"ask-{n}"))
        await store.dismiss(SLOT, f"ask-{n}", pinned["ask_id"])


@pytest.mark.asyncio
async def test_retired_records_past_the_bound_are_counted_and_survive_a_restart(store, clock):
    await _retire_in_turn(store, store_mod.MAX_RETIRED + 2)

    restarted = new_store(clock)
    await restarted.warm()
    _pinned, records, dropped = await restarted.turn_view(SLOT)

    assert [r["key"] for r in records] == ["ask-2", "ask-3", "ask-4", "ask-5"]
    assert dropped == 2
    await restarted.acknowledge_retired(SLOT, records, dropped)
    assert await restarted.turn_view(SLOT) == ([], [], 0)


@pytest.mark.asyncio
async def test_the_dropped_count_is_capped(store):
    await _retire_in_turn(store, store_mod.MAX_RETIRED)
    rec = store._slots[SLOT]
    rec[store_mod.RETIRED_DROPPED] = store_mod.MAX_RETIRED_DROPPED
    await _retire_in_turn(store, 1)

    assert store.snapshot_for_turn(SLOT)[store_mod.RETIRED_DROPPED] == (
        store_mod.MAX_RETIRED_DROPPED
    )


@pytest.mark.asyncio
async def test_the_dropped_count_expires_with_the_records_it_outlived(store, clock):
    await _retire_in_turn(store, store_mod.MAX_RETIRED + 1)
    clock.now += store_mod.RETIRED_TTL_SECONDS
    await pin(store, ask("kept"))

    await store.sweep()

    snapshot = store.snapshot_for_turn(SLOT)
    assert snapshot["retired"] == []
    assert store_mod.RETIRED_DROPPED not in snapshot
