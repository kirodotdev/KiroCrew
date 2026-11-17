"""What a slot-close hand-over leaves to the metadata line's own writer.

The rows-only drain keeps the slot-owned fields the replacement published, scoped by
the line's ``tab_id``. Close-vs-recreate guards live in
``test_slot_close_recreation_race.py``; shared fakes in
``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import _no_nudge_service  # noqa: F401
from slot_close_recreation_race_helpers import (
    HKEY,
    NAME,
    _arm_running_turn,
    _disk_contents,
    _json,
    _make_stale,
    _publish_metadata,
    _reached,
    _Req,
    _slot_with_committed_and_uncommitted_rows,
)

from kiro_crew import members as members_mod
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.wait import await_until, finished

# the hand-over keeps the replacement's own metadata
#
# ``_save_slot_to_history`` rebuilds SLOT_OWNED_META_KEYS from the slot it is handed,
# which would revert what the replacement published. ``rows_only`` moves the rows and
# keeps on-disk slot-owned fields. A title's provenance and refresh budget travel with
# the title, so the pairing is asserted, not just the title.


@pytest.mark.asyncio
async def test_delete_handover_keeps_the_replacement_published_metadata(tmp_path) -> None:
    """A newborn replacement's published title and folder survive the drain.

    The replacement here has no window of its own, which is the shape that
    publishes at birth. Its line is on disk and nothing else will rewrite it until
    the tab is used, so a rebuilding drain would leave the transcript filed and
    named as the tab the user dismissed for as long as the replacement stays idle.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(
        state,
        original,
        title="ORIGINAL TITLE",
        folder="folder-original",
        origin="auto",
        refresh_mark=8,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    await _publish_metadata(
        state,
        replacement,
        title="REPLACEMENT TITLE",
        folder="folder-replacement",
        origin="user",
        refresh_mark=24,
    )
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "REPLACEMENT TITLE", "the drain reverted the replacement's title"
    assert (
        meta.get("folder_id") == "folder-replacement"
    ), "the drain re-filed the transcript under the closed tab's folder"
    # The provenance travels WITH the title. Committing the replacement's name beside
    # the original's origin is a worse line than either slot's own: read back as
    # "auto" it unlocks the background refresh on a name the user typed, and read
    # back as "user" it locks a generated name out of refresh for good.
    assert (
        meta.get("title_origin") == "user"
    ), "the drain kept the closed tab's title provenance beside the replacement's title"
    # The spent-budget mark travels with the title too: rewinding it to the closed
    # tab's would hand the replacement's title a refresh milestone it already spent.
    assert (
        meta.get("title_refresh_mark") == 24
    ), "the drain rewound the replacement's spent refresh budget to the closed tab's"
    # The close flags stay owned by the write, so the open-shaped erase still runs.
    assert not meta.get("closed")
    # ...and none of that cost the rows the drain existed for.
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ], "the rows-only write dropped the handed-over slot's rows"


@pytest.mark.asyncio
async def test_delete_handover_keeps_the_original_metadata_when_nobody_replaced_it(
    tmp_path,
) -> None:
    """Deferring to disk must not become erasing: a blank replacement inherits.

    The line on disk is the original's; re-deriving it from the blank replacement
    would clear its title and filing.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert not replacement.folder_id, "the replacement must be blank for this case"
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "ORIGINAL TITLE", "a blank replacement erased the shared title"
    assert meta.get("folder_id") == "folder-original", "a blank replacement unfiled the transcript"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


def _leave_an_acknowledged_edit(slot, *, title: str, folder: str) -> None:
    """Apply a metadata edit the way every mutation route leaves it: in memory only.

    The routes mark the slot ``_dirty`` and leave the write to the periodic flush,
    which walks ``state._slots`` and so never reaches a popped slot.
    """
    slot.title = title
    slot._titled = True
    slot.folder_id = folder
    slot.pinned = True
    slot._dirty = True


@pytest.mark.asyncio
async def test_delete_handover_persists_the_original_uncommitted_metadata(tmp_path) -> None:
    """Deferring to disk must not become discarding the drain's own pending edit.

    The line is the original's own and the popped slot has no other route to disk.
    Metadata-only on purpose: the window is committed, so ``_dirty`` alone makes the
    drain run.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")
    assert original._disk_window_len == len(original.messages), "the publish left rows uncommitted"
    _leave_an_acknowledged_edit(original, title="RENAMED BY HAND", folder="folder-renamed")

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert not replacement._titled, "the replacement must have published nothing for this case"
    assert not replacement.folder_id
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "RENAMED BY HAND", "the drain discarded an acknowledged rename"
    assert meta.get("folder_id") == "folder-renamed", "the drain discarded an acknowledged re-file"
    assert meta.get("pinned") is True, "the drain discarded an acknowledged pin"
    # Still an open-shaped write: the key has a live holder.
    assert not meta.get("closed")
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_delete_handover_prefers_the_replacement_line_over_its_own_pending_edit(
    tmp_path,
) -> None:
    """The polarity: a line ANOTHER slot published still outranks the drain's edit.

    The replacement's edit was committed and the original's was not, so deferring
    costs less. Pinned beside the test above, which never deferring would satisfy.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")
    _leave_an_acknowledged_edit(original, title="RENAMED BY HAND", folder="folder-renamed")

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "REPLACEMENT TITLE", "the drain reverted a published title"
    assert meta.get("folder_id") == "folder-replacement", "the drain re-filed a live transcript"
    assert not meta.get("pinned"), "the drain pinned a live tab from the dismissed one's edit"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_delete_handover_keeps_the_replacement_authorization_attribution(
    tmp_path,
) -> None:
    """The drain must not re-attribute a live holder's session to the dismissed tab.

    ``created_by`` (read beside ``mode``) and ``origin`` (read beside ``app``) feed
    authorization checks, so carrying the original's forward would name the wrong
    owner after a restart.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    original.mode = members_mod.DM_SLOT_MODE
    original._created_by = "member-alice"
    original._origin = SlotOrigin.CRON
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")
    assert state.conversation_log.get_metadata(HKEY).get("created_by") == "member-alice"

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    replacement._created_by = "member-bob"
    replacement._origin = SlotOrigin.USER
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert (
        meta.get("created_by") == "member-bob"
    ), "the drain re-attributed a live session to the dismissed tab's creator"
    assert (
        meta.get("origin") == SlotOrigin.USER
    ), "the drain gave a live session the dismissed tab's slot kind"
    # The field each describes is deferred, so the pair has to agree: a member
    # ``mode`` beside the wrong ``created_by`` is the line matching neither slot.
    assert meta.get("mode") != members_mod.DM_SLOT_MODE
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_rows_only_defers_by_the_line_tab_id_not_by_the_flag_alone(tmp_path) -> None:
    """``rows_only`` is scoped at the save, by the one per-writer mark the line has.

    ``tab_id`` is minted per slot object; driven directly so it is the only variable.
    It is itself deferred, so the next drain defers again rather than flipping.
    """
    state = _make_state(tmp_path)
    slot = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, slot, title="COMMITTED TITLE", folder="folder-committed")

    # The line is this slot's own: nobody to defer to, so the pending edit commits.
    _leave_an_acknowledged_edit(slot, title="MINE", folder="folder-mine")
    assert await handlers.save_slot_off_loop(
        state, slot, force=True, best_effort=False, rows_only=True
    )
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "MINE", "a rows-only write deferred to the slot's own line"
    assert meta.get("folder_id") == "folder-mine"
    assert meta.get("tab_id") == slot._tab_id

    # Re-stamp the line as another writer's and the identical call defers instead.
    state.conversation_log.update_metadata(
        HKEY, {"tab_id": "0123456789ab", "title": "THEIRS", "folder_id": "folder-theirs"}
    )
    _leave_an_acknowledged_edit(slot, title="MINE AGAIN", folder="folder-mine-again")
    assert await handlers.save_slot_off_loop(
        state, slot, force=True, best_effort=False, rows_only=True
    )
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "THEIRS", "a rows-only write rebuilt over another writer's line"
    assert meta.get("folder_id") == "folder-theirs"
    assert meta.get("tab_id") == "0123456789ab", "the deferring write claimed the line's identity"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_rows_only_defers_dismissed_source_links_to_another_writer(tmp_path) -> None:
    """A rows-only handover keeps another live slot's source-link dismissals.

    ``dismissed_source_links`` is in ROWS_ONLY_DEFERRED, so a popped alias holding
    dismissal A must not erase the replacement's dismissal B.
    """
    state = _make_state(tmp_path)
    slot = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, slot, title="COMMITTED TITLE", folder="folder-committed")

    # This slot has dismissed link A in memory.
    slot._dismissed_source_links = {"gh:pull:acme:widgets:482:pull"}
    slot.invalidate_source_links()

    # The line on disk is ANOTHER writer's (foreign tab_id) holding dismissal B.
    state.conversation_log.update_metadata(
        HKEY,
        {"tab_id": "0123456789ab", "dismissed_source_links": ["gh:issue:acme:widgets:477:issue"]},
    )
    assert await handlers.save_slot_off_loop(
        state, slot, force=True, best_effort=False, rows_only=True
    )
    meta = state.conversation_log.get_metadata(HKEY)
    # The other writer's dismissal survived; this slot's A did NOT overwrite it.
    assert meta.get("dismissed_source_links") == [
        "gh:issue:acme:widgets:477:issue"
    ], "a rows-only handover must defer dismissed_source_links to the line's own writer"
    assert meta.get("tab_id") == "0123456789ab"
    assert _disk_contents(state) == ["PERSISTED-1", "PERSISTED-2", "TAIL-3", "TAIL-4"]


@pytest.mark.asyncio
async def test_handover_rows_only_write_still_creates_a_first_metadata_line(tmp_path) -> None:
    """With no line on disk yet there is nobody to defer to, so the slot's own wins.

    Keeping an absent line would leave rows with no title, folder or memory mode.
    The hand-over is reachable before a slot's first committed save.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME)
    original.title = "NEVER SAVED"
    original.folder_id = "folder-original"
    original.append("user", "TAIL-1")
    original.drain()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "NEVER SAVED", "the first line was written without the slot's state"
    assert meta.get("folder_id") == "folder-original"
    assert _disk_contents(state) == ["TAIL-1"]


@pytest.mark.asyncio
async def test_delete_handover_rows_only_keeps_both_windows(tmp_path) -> None:
    """A replacement that HAS a window keeps its rows, its line, and the drain's.

    The two slots hold different windows over one file, so the drain re-serializes
    the original's and the foreign-append scan carries the replacement's committed
    rows through. Pinned alongside the metadata because a rows-only write that
    protected the line by dropping rows would satisfy every other assertion here.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    replacement.append("user", "REPLACEMENT-5")
    replacement.drain()
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "REPLACEMENT TITLE", "the drain reverted the replacement's title"
    assert meta.get("folder_id") == "folder-replacement"
    # Sorted, not positional: two windows over one file interleave by ``ts``, and a
    # position would pin a tiebreak this test is not about. Sorted still catches the
    # two failures that matter — a dropped row and a duplicated one.
    assert sorted(_disk_contents(state)) == sorted(
        [
            "PERSISTED-1",
            "PERSISTED-2",
            "TAIL-3",
            "TAIL-4",
            "REPLACEMENT-5",
        ]
    ), "the rows-only write dropped or duplicated a row from either window"


@pytest.mark.asyncio
async def test_cleanup_handover_keeps_the_replacement_published_metadata(tmp_path) -> None:
    """The bulk path's hand-over owes the same restraint as the single-tab close.

    Both exits drain through ``_persist_handover_tail``, so ``rows_only`` rides with
    the write rather than being restated per call site — this pins that the bulk
    path really does inherit it.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")
    _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    release.set()
    resp = await finished(close, "the cleanup sweep finishing once the turn is released")

    assert resp.status == 200
    assert NAME not in _json(resp)["keys"], "a live replacement was reported archived-over"
    assert state._slots.get(NAME) is replacement
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "REPLACEMENT TITLE", "cleanup's drain reverted the replacement"
    assert meta.get("folder_id") == "folder-replacement"
    assert not meta.get("closed")
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_delete_failure_arm_handover_keeps_the_replacement_metadata(
    tmp_path, monkeypatch
) -> None:
    """The failure arm's drain carries the same restraint.

    That arm reaches the drain after its archive save already failed, so the write
    it performs is the one that would revert the replacement — and it is the arm
    whose caller sees a 500, where a silently reverted title is the last thing
    anyone would look for.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    await _publish_metadata(state, original, title="ORIGINAL TITLE", folder="folder-original")

    real_save = handlers.save_slot_off_loop
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(_state, _slot, *a, **kw):
        if kw.get("closed"):
            entered.set()
            await await_until(release, what="the test releasing the parked archive save")
            raise OSError("disk wedged")
        return await real_save(_state, _slot, *a, **kw)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    try:
        await _reached(entered, close)
        replacement = state.get_or_create_slot(NAME)
        assert replacement is not original
        await _publish_metadata(
            state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
        )
    finally:
        release.set()
    resp = await finished(close, "the slot delete answering once the save is released")

    assert resp.status == 500
    assert state._slots.get(NAME) is replacement
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("title") == "REPLACEMENT TITLE", "the failure arm's drain reverted the title"
    assert meta.get("folder_id") == "folder-replacement"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


async def _original_with_a_published_line_and_an_uncommitted_tail(state):
    """The original, its metadata line on disk, and two rows that never reached it.

    Ordering is the whole point: the tail is appended AFTER the last commit, so the
    drain has both rows to write and a metadata line to decide about. Publishing
    after the tail instead would commit it and reduce the drain to a no-op, which
    every assertion about the line would then pass vacuously.
    """
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "PERSISTED-1")
    slot.append("assistant", "PERSISTED-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    await _publish_metadata(state, slot, title="ORIGINAL TITLE", folder="folder-original")
    slot.append("user", "TAIL-3")
    slot.append("assistant", "TAIL-4")
    slot.drain()
    assert slot._disk_window_len == 2, "the tail was already committed, so no drain will run"
    return slot


@pytest.mark.asyncio
async def test_delete_handover_keeps_a_dismissal_the_replacement_committed(tmp_path) -> None:
    """``closed`` on the replacement's line is ITS dismissal, so the drain defers it.

    The replacement's close can win the lock first; with both slots popped, erasing
    it would resurface the tab (compare the resume route's ``only_if_closed_before``).
    """
    state = _make_state(tmp_path)
    original = await _original_with_a_published_line_and_an_uncommitted_tail(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    # The user dismisses the REPLACEMENT while the original's drain is still owed, and
    # its close reaches the lock first: the flag on the line is now the replacement's
    # own, stamped at its own instant.
    assert await handlers.save_slot_off_loop(
        state, replacement, closed=True, closed_at=1234.0, force=True, best_effort=False
    )
    assert state.conversation_log.get_metadata(HKEY).get("closed") is True
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("closed") is True, "the drain erased a dismissal the replacement committed"
    assert meta.get("closed_at") == 1234.0, "the dismissal lost the instant it was stamped at"
    assert meta.get("title") == "REPLACEMENT TITLE"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


@pytest.mark.asyncio
async def test_delete_handover_erases_a_stale_closed_flag_on_its_own_line(tmp_path) -> None:
    """On a line THIS slot published, the open-shaped drain still clears ``closed``.

    A replacement that published nothing leaves the original's line, with no other
    dismissal to lose, so the ordinary rebuild runs and the live key reads open.
    """
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "PERSISTED-1")
    slot.append("assistant", "PERSISTED-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    await _publish_metadata(state, slot, title="ORIGINAL TITLE", folder="folder-original")
    # A prior close of this reused key left the archive flag on the original's line.
    assert await handlers.save_slot_off_loop(
        state, slot, closed=True, closed_at=1.0, force=True, best_effort=False
    )
    assert state.conversation_log.get_metadata(HKEY).get("closed") is True
    slot.append("user", "TAIL-3")
    slot.append("assistant", "TAIL-4")
    slot.drain()
    assert slot._disk_window_len == 2, "the tail was already committed, so no drain will run"

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The replacement publishes NOTHING, so the line the drain meets is still the
    # original's — the branch where the full ownership claim applies.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not slot
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert not meta.get("closed"), "a key with a live holder stayed stamped closed"
    assert not meta.get("closed_at"), "closed_at outlived the flag it timestamps"
    assert meta.get("title") == "ORIGINAL TITLE"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ]


# a replacement that shares NO transcript takes no archive
#
# A linked tab's transcript is its ``linked_session_key``; a plain
# ``get_or_create_slot(name)`` replacement writes ``dashboard:{name}``. Skipping the
# archive there leaves no ``closed`` flag and ``channel_slots._close_stands`` resurfaces
# the tab, so the archive is gated on the file.


async def _linked_slot_with_a_tail(state, linked: str, name: str = NAME):
    """A slot whose conversation lives on *linked*, with one row past its last save."""
    slot = state.get_or_create_slot(name, linked_session_key=linked)
    slot.append("user", "PERSISTED-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    assert slot._disk_window_len == 1, "the seed save did not commit the first window"
    slot.append("assistant", "TAIL-2")
    slot.drain()
    return slot


@pytest.mark.asyncio
async def test_delete_divergent_transcript_still_archives_the_original(tmp_path) -> None:
    """An unbound recreate over a linked tab must not carry off its archive."""
    state = _make_state(tmp_path)
    linked = "cron:job7"
    original = await _linked_slot_with_a_tail(state, linked)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    assert original.running, "the turn must be live so the cancel-wait actually blocks"

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert not replacement.linked_session_key, "the replacement must be unbound for this case"
    release.set()
    resp = await finished(close, "the slot delete finishing once the turn is released")

    assert resp.status == 200
    # The archive the user asked for happened, on the file only the original writes.
    meta = state.conversation_log.get_metadata(linked)
    assert meta.get("closed"), "the dismissed linked transcript was left open to resurface"
    assert meta.get("closed_at"), "an archived transcript with no close instant keeps no close"
    assert [m.get("content", "") for m in state.conversation_log.read_messages(linked)] == [
        "PERSISTED-1",
        "TAIL-2",
    ], "archiving the original's own transcript dropped its tail"
    # ...and the key-scoped steps still yielded: the fix holds.
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    assert state.sessions.remove.await_count == 0, "the replacement's session was torn down"
    assert _disk_contents(state) == [], "rows landed on a transcript this slot never used"


@pytest.mark.asyncio
async def test_cleanup_divergent_transcript_still_archives_the_original(tmp_path) -> None:
    """The bulk path splits the same way: archive the file, yield the key.

    ``keys`` stays key-scoped and so still omits ``NAME`` — it names slot keys, and
    this one has a live holder — but the transcript is archived, which is what the
    idle sweep was for.
    """
    state = _make_state(tmp_path)
    linked = "cron:job7"
    original = await _linked_slot_with_a_tail(state, linked)
    _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    assert original.running, "the turn must be live so the cancel-wait actually blocks"

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await finished(close, "the cleanup sweep finishing once the turn is released")

    payload = _json(resp)
    assert resp.status == 200
    assert payload["failed"] == [], "the archive of the original's own transcript failed"
    assert NAME not in payload["keys"], "a key with a live holder was reported swept"
    meta = state.conversation_log.get_metadata(linked)
    assert meta.get("closed"), "the stale linked transcript was left open to resurface"
    assert [m.get("content", "") for m in state.conversation_log.read_messages(linked)] == [
        "PERSISTED-1",
        "TAIL-2",
    ], "archiving the original's own transcript dropped its tail"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by cleanup"
    assert state.sessions.remove.await_count == 0, "the replacement's session was torn down"


@pytest.mark.asyncio
async def test_a_save_the_closed_slot_issues_after_its_archive_keeps_the_close(
    tmp_path,
) -> None:
    """A late save of the dismissed slot does not bring the tab back after a restart.

    A turn that outlives the close's cancel wait still saves its slot when it ends.
    That slot reads open only because it is stale, so rebuilding the line from it
    would erase the close its own archive committed a moment earlier.
    """
    state = _make_state(tmp_path)
    slot = await _slot_with_committed_and_uncommitted_rows(state)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    archived = state.conversation_log.get_metadata(HKEY)
    assert archived.get("closed") is True, "the close did not archive the transcript"

    slot.append("assistant", "LATE-5")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("closed") is True, "a stale save of the closed slot reopened its tab"
    assert meta.get("closed_at") == archived.get("closed_at")
    assert meta.get("closed_id") == archived.get("closed_id")
    assert _disk_contents(state)[-1] == "LATE-5", "the late save did not keep its row"


@pytest.mark.asyncio
async def test_a_late_save_of_a_closed_linked_slot_keeps_the_close_time(tmp_path) -> None:
    """The late save leaves a linked transcript's mtime where the archive put it.

    A channel- or cron-linked tab reopens when its file is newer than the close
    (``channel_slots._close_stands``). A row the dismissed slot writes late is not
    activity on that conversation, so it must not move the file's time either.
    """
    state = _make_state(tmp_path)
    linked = "cron:job7"
    slot = await _linked_slot_with_a_tail(state, linked)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    path = state.conversation_log._path(linked)
    os.utime(path, (1000.0, 1000.0))

    slot.append("assistant", "LATE-3")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    assert state.conversation_log.get_metadata(linked).get("closed") is True
    assert path.stat().st_mtime == 1000.0, "the late save read as activity after the close"
