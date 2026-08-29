"""An overlapping close must not be reported as a definitive refusal.

A "definitive, retry" answer tells the client the slot is provably still open. While a
second close of the same object can still pop it, that advice can aim the retry at whatever
holds the key next -- a same-key replacement the other close is about to hand over.

The count lives on the SLOT, so the surviving row can also PUBLISH it: a read reporting
``closing=False`` is the terminal outcome that lets a withheld row be released once every
close of that object has finished without popping it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.dashboard import chat_handlers


class _Slot:
    def __init__(self, in_flight: int = 0) -> None:
        self._closes_in_flight = in_flight


class _State:
    def __init__(self, slots: dict[str, object]) -> None:
        self._slots = slots


def test_presence_is_definitive_when_this_close_is_the_only_one() -> None:
    slot = _Slot(1)
    state = _State({"chat-1": slot})

    assert chat_handlers._slot_present_and_ours(state, "chat-1", slot) is True


def test_presence_is_not_definitive_while_another_close_can_still_pop() -> None:
    slot = _Slot(2)
    state = _State({"chat-1": slot})

    assert chat_handlers._slot_present_and_ours(state, "chat-1", slot) is False


def test_presence_is_never_definitive_for_a_replaced_object() -> None:
    state = _State({"chat-1": _Slot(1)})

    assert chat_handlers._slot_present_and_ours(state, "chat-1", _Slot(1)) is False


def test_a_replacements_own_closes_never_count_as_overlapping() -> None:
    original = _Slot(1)
    replacement = _Slot(5)
    state = _State({"chat-1": original})
    assert replacement._closes_in_flight == 5

    assert chat_handlers._slot_present_and_ours(state, "chat-1", original) is True


def test_the_tracker_counts_overlapping_closes_and_clears_itself() -> None:
    seen: list[bool] = []
    release = asyncio.Event()

    @chat_handlers._tracks_close
    async def fake_close(state: object, slot: object, name: str) -> None:
        seen.append(chat_handlers._overlapping_close(slot))
        await release.wait()

    slot = _Slot()

    async def drive() -> None:
        first = asyncio.create_task(fake_close(None, slot, "chat-1"))
        second = asyncio.create_task(fake_close(None, slot, "chat-1"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(drive())

    assert seen[0] is False
    assert seen[1] is True
    assert slot._closes_in_flight == 0


@pytest.mark.parametrize("in_flight", [0, 1])
def test_a_lone_close_leaves_presence_usable(in_flight: int) -> None:
    slot = _Slot(in_flight)
    state = _State({"chat-1": slot})

    assert chat_handlers._slot_present_and_ours(state, "chat-1", slot) is True
