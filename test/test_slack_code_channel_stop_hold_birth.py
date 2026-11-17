"""Hold-move row ``session_create birth`` (``stop_hold``): a Stop at each await of a
helper's birth holds the newborn.

The press holds the channel's session, then reads its helpers from the store; the birth
writes the helper's record, then reads its creator's hold (``stop_hold.inherit``), with
no await between either side's write and its read, so one always sees the other.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_slack_code_channel_dispatch_exemption import (  # noqa: F401 -- autouse by import
    _channel,
    _enabled,
    _key,
    _owner_turn,
    _state,
)
from test_slack_code_channel_stop_hold import _stepped

from kiro_crew.dashboard import session_control as sc
from kiro_crew.slack.code_channels import stop_hold


def test_a_stop_at_any_await_of_a_helpers_birth_holds_the_newborn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    slot, _store = _channel(state, tmp_path)
    _owner_turn(slot)

    def press() -> None:
        stop_hold.press(state, slot.key, slot)

    async def birth(at: int) -> tuple[Any, int]:
        slot._stop_held = False  # a person's message released the channel's session
        stop_hold._keys.clear()
        created, total = await _stepped(
            sc.create_session(state, caller_session_key=_key(slot)), at, press
        )
        return state.get_slot(created["target"]), total

    async def drive() -> None:
        unheld, total = await birth(-2)
        assert unheld is not None and not stop_hold.holds(unheld) and total > 0
        for at in range(total + 1):
            newborn, _ = await birth(at)
            assert newborn is not None and stop_hold.holds(newborn), (at, total)

    asyncio.run(drive())
