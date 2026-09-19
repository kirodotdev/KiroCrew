"""Truncating history rewrites must refuse at the commit boundary, not after it.

A truncating rewrite (rewind, edit-resend, regenerate, switch-variant) freezes a
shortened window against ONE slot incarnation and then dispatches the write off
the event loop. The executor wait frees the loop, and a same-name
close-and-recreate is not serialized against the slot's own lock: the cleanup
pops ``state._slots[name]`` and ``get_or_create_slot`` re-inserts, neither taking
it. A recreate that resumes the SAME transcript keeps the history key identical,
so ``expected_history_key`` -- the only axis re-read inside the write before
``expected_slot_name`` existed -- waves it through, and the frozen truncation
lands on the replacement's transcript. No recovery path: the file is written.

Both handlers do compare the slot object, and both comparisons are too late to
help the file. ``chat_rewind`` re-checks routing only, AFTER the save settles.
``chat_regenerate``'s ``_commit_target_intact`` does compare the object, but
every call to it is also after the save, so it refuses the live commit and the
dispatch while the truncated bytes are already on disk. The commit-boundary pin
is what turns that post-mortem into a refusal: ``state._slots[name]`` re-read
inside the write, under the transcript lock, with no await before it.

So these tests assert on the FILE, not on the HTTP status. A handler that
notices the substitution one step too late still answers 5xx, which is why the
status alone cannot tell the fixed shape from the broken one.

The interleaving seam is ``get_metadata_status``, which the save calls inside
the lock and before the guard, and it fires only when called OFF the main
thread. That placement is the claim under test: the recreate lands after every
check the handler can run on the loop, so a pre-dispatch comparison -- however
many axes it compares -- cannot see it. Firing on the loop instead would let an
earlier check take the credit and the test would pass on unfixed code.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_persistence

NAME = "src"
HISTORY_KEY = "dashboard:src"

FULL = [
    ("user", "first question"),
    ("assistant", "first answer"),
    ("user", "second question"),
    ("assistant", "second answer"),
]


@pytest.fixture(autouse=True)
def _no_real_turn(monkeypatch):
    """Neither handler may spawn kiro-cli; the dispatch is not what is measured."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_regenerate._run_chat", AsyncMock(return_value=None)
    )


def _seed(state):
    """A slot with four visible rows, and the SAME four already durable.

    Pre-seeding disk is what makes the harm observable as a subtraction: the
    assertion is that four rows survive, not merely that some file is absent.
    Without the flush a refused save and a landed truncation both leave a file
    the test would have to tell apart by length alone.
    """
    slot = state.get_or_create_slot(NAME)
    for i, (role, content) in enumerate(FULL):
        slot.append(role, content, f"msg msg-{role[0]}", ts=f"2026-05-21T16:00:0{i}Z")
    slot.drain()
    state.sessions._session_map.get = MagicMock(return_value="")
    assert chat_persistence._save_slot_to_history(state, slot, force=True) is not False
    assert [
        (m["role"], m["content"]) for m in state.conversation_log.read_messages(HISTORY_KEY)
    ] == FULL
    return slot


def _arm_recreate_inside_the_write(state, monkeypatch):
    """Publish a same-name replacement from inside the save's worker thread.

    Returns the replacement so a test can assert the substitution really was
    installed -- a seam that never fired would make every assertion below pass
    vacuously.
    """
    original = state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    # The recreate resumes the same transcript, so the routing axis cannot tell
    # the two apart -- that identity is the whole point of the case.
    state._slots[NAME] = original

    real = state.conversation_log.get_metadata_status
    main_thread = threading.current_thread()
    fired: list[str] = []

    def _recreate_then_read(key, *a, **kw):
        if not fired and threading.current_thread() is not main_thread:
            fired.append(key)
            state._slots[NAME] = replacement
        return real(key, *a, **kw)

    monkeypatch.setattr(state.conversation_log, "get_metadata_status", _recreate_then_read)
    return replacement, fired


def _durable_rows(state):
    return [(m["role"], m["content"]) for m in state.conversation_log.read_messages(HISTORY_KEY)]


def _app_with_edit_resend(state):
    """``_make_app`` registers rewind but not edit-resend; add it here only.

    Kept local rather than widened in the shared helper: every other module
    importing that helper would gain a route it does not exercise.
    """
    from kiro_crew.dashboard.chat import api_chat_slot_edit_resend

    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.mark.asyncio
async def test_rewind_refuses_the_write_when_a_recreate_wins_the_race(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = _seed(state)
    replacement, fired = _arm_recreate_inside_the_write(state, monkeypatch)

    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{NAME}/rewind",
            json={"at_message_index": 0, "content": "edited first question"},
        )

    assert fired, "the seam never ran off the main thread; the race was not exercised"
    assert state._slots[NAME] is replacement
    # The transcript the replacement now resumes still holds every row. This is
    # the assertion the pin exists for; the status below only records that the
    # handler declined to build on a write it did not make.
    assert _durable_rows(state) == FULL
    assert resp.status == 503
    if slot.task:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_write_when_a_recreate_wins_the_race(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = _seed(state)
    replacement, fired = _arm_recreate_inside_the_write(state, monkeypatch)

    app = _app_with_edit_resend(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{NAME}/edit-resend",
            json={"index": 0, "content": "edited first question"},
        )

    assert fired, "the seam never ran off the main thread; the race was not exercised"
    assert state._slots[NAME] is replacement
    assert _durable_rows(state) == FULL
    assert resp.status >= 400
    if slot.task:
        slot.task.cancel()


def test_every_truncating_rewrite_carries_the_incarnation_pin():
    """A new truncating call site must not inherit the gap silently.

    The two behavioral tests above cover the two sites that had it. This one is
    the ratchet: a save that hands the persistence layer a frozen ``messages``
    snapshot has, by construction, authorized that snapshot against one slot
    incarnation, so it owes the pin. Enumerated by hand rather than scanned
    because the distinction is about the snapshot's provenance, which the source
    text does not carry.
    """
    from pathlib import Path

    dashboard = Path(chat_persistence.__file__).parent
    sites = {
        "chat_regenerate.py": 3,  # regenerate, switch-variant, edit-resend
        "chat_rewind.py": 1,  # rewind
    }
    for filename, expected in sites.items():
        text = (dashboard / filename).read_text(encoding="utf-8")
        found = text.count("expected_slot_name=name")
        assert found == expected, f"{filename}: {found} pinned site(s), expected {expected}"
