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

    assert [r["reason"] for r in await store.take_retired(SLOT)] == [store_mod.REASON_DISMISSED]


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
