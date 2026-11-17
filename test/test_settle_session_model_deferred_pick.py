"""The pre-turn model settle must not overwrite a deferred model pick.

A model pick the picker took mid-turn commits ``slot.model = ""`` (Auto) and
marks ``_deferred_model_switch_key`` for the boundary to apply. If
``_settle_session_model`` wrote the served (old) model over that ``""``, the
boundary would apply the old model, silently undoing the pick.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat_turn.model_fallback import _settle_session_model
from kiro_crew.dashboard.state import _ChatSlot


class _Client:
    """The provider double: ``client.client._model`` is the served model."""

    class client:  # noqa: N801 - mirrors the real attribute name the provider carries
        _model = "served-model"


async def _settle(slot: _ChatSlot) -> None:
    await _settle_session_model(
        slot,
        _Client(),
        "acp",
        "hello",
        is_new=True,
        resumed=False,
        _is_refusal_retry_turn=False,
        _synthetic_recovery_turn=False,
    )


@pytest.mark.asyncio
async def test_a_deferred_pick_is_not_overwritten_by_the_pre_turn_backfill():
    slot = _ChatSlot("s1")
    slot.model = ""  # the mid-turn Auto pick
    slot._deferred_model_switch_key = "s1"  # marked for the boundary

    await _settle(slot)

    assert slot.model == "", (
        "the pre-turn settle must leave a pending deferred pick alone; "
        f"slot.model is {slot.model!r}"
    )
