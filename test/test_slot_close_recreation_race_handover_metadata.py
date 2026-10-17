"""What a slot-close hand-over leaves to the metadata line's own writer.

The rows-only drain keeps every slot-owned field the replacement published (the title
with its provenance and refresh mark, folder, pin, attribution, dismissed source links,
the closed flag), scoped by the line's ``tab_id``; and a close whose replacement shares
no transcript still archives the original's own file. The close-vs-recreate guards
themselves live in ``test_slot_close_recreation_race.py``; the shared fakes in
``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio

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

# --------------------------------------------------------------------------- #
# what the hand-over must NOT cost: the replacement's own metadata
# --------------------------------------------------------------------------- #
#
# The rows and the metadata line have different owners on a shared transcript.
# ``_save_slot_to_history`` is otherwise authoritative for SLOT_OWNED_META_KEYS and
# REBUILDS that line from whichever slot it is handed, so a default save here would
# revert a title, folder, tag set or pin the REPLACEMENT already published — and for
# a tab nobody types in again, revert it for good, so the next restart resurrects the
# dismissed tab's name and filing. The drain therefore writes ``rows_only``: it moves
# the rows and keeps the on-disk value for every slot-owned field, retaining only the
# close flags so the open-shaped erase still happens.
#
# The deferral is wider than the owned set, because the rebuild also writes fields
# that DESCRIBE an owned one without being owned themselves. A title's provenance and
# refresh budget travel WITH the title, so deferring the title while keeping those
# commits a line matching neither slot — separately valid halves, undetectable
# downstream — which is why the pairing is asserted below and not just the title.


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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    A recreate that has published nothing has no metadata to protect, and the line
    on disk is the ORIGINAL's own — a real title and filing for the conversation
    both slots now share. Re-deriving the line from the blank replacement would
    clear them, which is the opposite failure and just as silent. Writing rows
    while leaving the line alone is what gets both cases right at once.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    ``PATCH .../color``, the tag and pin routes and the auto-titler all mutate the
    slot, mark it ``_dirty`` and answer the caller; the durable write is the next
    periodic flush's. That flush iterates ``state._slots``, so this shape is exactly
    what a pop makes unreachable — the user has been told the rename landed and the
    only object that knows it is about to be dropped.
    """
    slot.title = title
    slot._titled = True
    slot.folder_id = folder
    slot.pinned = True
    slot._dirty = True


@pytest.mark.asyncio
async def test_delete_handover_persists_the_original_uncommitted_metadata(tmp_path) -> None:
    """Deferring to disk must not become discarding the drain's own pending edit.

    The line on disk here is the ORIGINAL's own, so there is nobody else's state on
    it to protect, and the edit the original is carrying has no other route to disk:
    the periodic flush walks ``state._slots`` and this slot is out of it for good.
    Deferring to disk on that line would answer 200 while silently discarding a
    rename, re-file and pin the user watched land.

    Metadata-only on purpose — the publish above commits the window, so ``_dirty``
    is the sole reason the drain runs at all. That keeps this test about the
    metadata half rather than riding on the row half's write.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    The two losses are not symmetric, so the tie is not broken in the drain's
    favour. The original's edit was never committed; the replacement's WAS, and for
    a tab nobody types in again nothing rewrites it — so rebuilding here would
    revert a published title and filing for good, while deferring costs an
    uncommitted one. Pinned beside the test above because a fix that simply stopped
    deferring would satisfy that one and silently undo the restraint.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    ``created_by`` and ``origin`` are not facts about the conversation, they are
    attributes of the SLOT, and each is meaningless apart from a field the drain
    already defers: session-control's member ownership boundary reads ``created_by``
    beside ``mode``, and ``origin`` decides ``slots:user`` visibility and the
    unattended approval window beside ``app``. Carrying the original's forward would
    commit a line naming an owner who never opened this session and a slot kind its
    holder never had — and for a replacement nobody types in again, no later save
    corrects it, so the next restart hands that pairing to the authorization checks.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    ``tab_id`` is minted per slot OBJECT and stamped by every save, so it is what
    separates "this line is somebody else's" from "this line is mine". Driven
    directly rather than through a race so the discriminator is the only variable:
    the same call, the same slot and the same flag, answered two ways by the id on
    the line. It is itself deferred, so a deferring write leaves the other writer's
    id in place and the next drain defers again rather than flipping.
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
    """A rows-only handover must not overwrite another live slot's source-link
    dismissals.

    ``dismissed_source_links`` is SLOT_OWNED, so it is in ROWS_ONLY_DEFERRED: a
    save that persists THIS slot's messages onto a transcript another live slot
    published must carry the on-disk dismissed set back verbatim, not clobber it
    with this slot's own. Without the fix a popped alias holding dismissal A would
    erase the replacement slot's dismissal B on a rows-only handover.
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

    ``rows_only`` protects ANOTHER writer's fields; a transcript with no metadata
    line has none, and preserving an absent line would publish a row-bearing
    transcript with no title, folder or memory mode at all. So the flag is ignored
    in that case and the drain writes the original's own line — pinned because the
    hand-over is reachable before a slot's first committed save.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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
            # flake-ok: the stub parks the close here until the test sets release; moved verbatim
            await release.wait()
            raise OSError("disk wedged")
        return await real_save(_state, _slot, *a, **kw)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    await _publish_metadata(
        state, replacement, title="REPLACEMENT TITLE", folder="folder-replacement"
    )
    release.set()
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    Open-shaped is not the same as un-closing. The drain and the replacement's own
    close race for the transcript lock, so the dismissal can land first — and both
    slots are popped by then, so erasing it resurfaces a tab the user put away with
    nothing left to re-archive it. This is the mistake the resume route's
    ``only_if_closed_before`` boundary exists to avoid one layer down, and a
    rows-only save has no such boundary to reason with.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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

    The deferral is scoped by the line's ``tab_id``, so a replacement that has
    published nothing leaves the ORIGINAL's own line on disk — where there is no
    other holder's dismissal to lose. The ordinary rebuild runs there, and a key that
    now has a live holder reads open instead of staying archived under a tab the user
    is looking at.
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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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


# --------------------------------------------------------------------------- #
# a replacement that shares NO transcript takes no archive
# --------------------------------------------------------------------------- #
#
# Slot identity is not transcript ownership. A channel-, cron- or workflow-born tab
# keeps its conversation under its ``linked_session_key``, while a replacement minted
# by a plain ``get_or_create_slot(name)`` — the shape POST /api/chat and the
# session_close verb take — is unbound and keeps its own under ``dashboard:{name}``.
# Yielding the archive on that pair costs the user their dismissal: the linked
# transcript keeps no ``closed`` flag, ``channel_slots._close_stands`` reads an absent
# flag as "never dismissed", and the reconcile pass surfaces the tab again.
#
# So the archive is gated on the FILE and only the key-scoped steps yield: the save
# runs on the original's own transcript, while the replacement keeps its slot and its
# session.


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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

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
