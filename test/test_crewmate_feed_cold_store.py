# SPDX-License-Identifier: Apache-2.0
"""The crewmate feed never reads a cold change-card store on the serving loop.

``crewmate_feed._settle`` runs on the loop, and the waiting list it records reads
the change-card store, whose first read opens its file (and the vault key). A
settle now warms the store off the loop first (``CardStore.warm``); if warming
fails it records the list without the store instead of reading it on the loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew.crew_log import emit
from kiro_crew.dashboard import change_cards, crewmate_feed
from kiro_crew.dashboard.change_cards import CardStore

ASK = "Open a PR for the docs fix? [OPTIONS: Open the PR | Not yet]"
SLOT = "member-dm:atlas"
KEY = b"k" * 32


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    crewmate_feed._last.clear()
    yield
    crewmate_feed._last.clear()


@pytest.fixture
def appended(monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(emit, "on_waiting_changed", lambda sid, data: seen.append(data))
    monkeypatch.setattr(emit, "on_goal_recorded", lambda sid, data: None)
    monkeypatch.setattr(crewmate_feed, "_session_of", lambda slot: "s-atlas")
    return seen


class _Slot:
    key = SLOT
    mode = "member"
    agent = "Atlas"

    def __init__(self) -> None:
        self._question_pending: dict[str, Any] = {}

    def sidebar_messages(self) -> list[dict[str, Any]]:
        return [{"role": "assistant", "content": ASK, "ts": "2026-10-18T18:40:00+00:00"}]


class _State:
    def __init__(self, store: CardStore) -> None:
        slot = _Slot()
        self._slots = {slot.key: slot}
        self._pending_questions: dict[str, Any] = {}
        self._change_card_store = store

    def get_slot(self, key: str) -> Any:
        return self._slots.get(key)


def _persisted_card(path: Path) -> None:
    """One pending card for the thread, written to *path* by a store that then goes away."""
    store = CardStore(path, key=lambda: KEY)
    params = {"path": "chat.verbosity", "value": "ultra-brief"}
    before = {"value": "standard"}
    store.propose(
        slot_key=SLOT,
        session_key=f"dashboard:{SLOT}",
        kind="setting.change",
        params=params,
        reason="you asked for shorter answers",
        preview=catalog.build_preview("setting.change", params, before, {}),
        before=before,
        context={},
    )
    asyncio.run(store.flush(strict=True))
    assert path.is_file()


@pytest.fixture
def reads(monkeypatch):
    """Every store-file read, and whether it ran on a thread with a running loop."""
    seen: list[bool] = []
    real = change_cards._read_store

    def guarded(path: Path) -> str:
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return real(path)

    monkeypatch.setattr(change_cards, "_read_store", guarded)
    return seen


def _settle(state: _State, monkeypatch) -> None:
    monkeypatch.setattr(crewmate_feed, "_state", lambda: state)

    async def go() -> None:
        crewmate_feed._pending.add("s-atlas")
        crewmate_feed._settle()
        while crewmate_feed._keep:
            await asyncio.gather(*list(crewmate_feed._keep))

    asyncio.run(go())


def test_a_cold_store_is_read_off_the_loop_and_the_snapshot_lists_its_card(
    tmp_path, appended, reads, monkeypatch
):
    path = tmp_path / "change_cards.json"
    _persisted_card(path)
    reads.clear()
    store = CardStore(path, key=lambda: KEY)

    _settle(_State(store), monkeypatch)

    assert reads == [False]  # one read, and not on the loop
    (snapshot,) = appended
    assert [item["kind"] for item in snapshot["items"]] == ["card", "options"]


def test_a_store_that_fails_to_warm_is_skipped_not_read_on_the_loop(
    tmp_path, appended, monkeypatch
):
    path = tmp_path / "change_cards.json"
    _persisted_card(path)
    on_loop: list[bool] = []

    def broken(_path: Path) -> str:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        raise OSError("unreadable")

    monkeypatch.setattr(change_cards, "_read_store", broken)

    _settle(_State(CardStore(path, key=lambda: KEY)), monkeypatch)

    assert on_loop == [False]  # the warm-up tried once, in a worker thread
    (snapshot,) = appended
    assert [item["kind"] for item in snapshot["items"]] == ["options"]
