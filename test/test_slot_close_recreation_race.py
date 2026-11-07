"""Close-vs-recreate race on the shared dashboard slot-close teardown.

``api_chat_slot_delete`` and ``api_chat_slots_cleanup`` pop ``name`` and then await
(cancel, closed=True save, ``sessions.remove``). A same-key recreate inside that window
must not have its transcript archived, its session torn down, or its key overwritten
by a failure-arm restore.

Two predicates guard two owners. ``_slot_still_ours`` guards the KEY-scoped steps
(``sessions.remove``, restores, cleanup's ``archived`` report).
``_replacement_shares_transcript`` guards the closed=True save, whose resource is the
TRANSCRIPT: a linked tab and an unbound same-name recreate share a key but not a file.

Each polarity is pinned on its own because a backwards guard is silent. An absent key
is the ORDINARY post-pop state, so ``get(name) is <popped>`` would skip every teardown.
For the transcript, a divergent pair read as shared loses an archive, and a folded pair
read as divergent stamps ``closed`` on a live file.

Privacy-line cases live in ``test_slot_close_recreation_race_privacy_ratchet.py``,
metadata cases in ``test_slot_close_recreation_race_handover_metadata.py``, and the
shared fakes in ``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import _no_nudge_service  # noqa: F401
from slot_close_recreation_race_helpers import (
    HKEY,
    NAME,
    RKEY,
    _arm_running_turn,
    _disk_contents,
    _json,
    _make_stale,
    _reached,
    _Req,
    _slot_with_committed_and_uncommitted_rows,
)

from kiro_crew import autonudge
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard import chat_handlers as handlers


def _state_with_slot(tmp_path, name: str = NAME):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "watch the PR")
    slot.append("assistant", "watching")
    slot.drain()
    return state


# --------------------------------------------------------------------------- #
# the two predicates
#
# The teardown's destructive steps do not all answer to the same owner, so there
# are two discriminators and each guards the step whose resource it describes:
# ``_slot_still_ours`` for the KEY (``sessions.remove`` on ``dashboard:{name}``, the
# failure-arm ``_slots`` restore, cleanup's ``archived`` report) and
# ``_replacement_shares_transcript`` for the FILE (the closed=True save).
# --------------------------------------------------------------------------- #


def test_still_ours_treats_a_freed_key_as_ours(tmp_path) -> None:
    """The predicate answers "has someone ELSE taken the key", not "is it ours".

    A freed key (``None``) is the ordinary state at every guard site — the close
    popped the slot before the awaits — so it MUST read as still ours. A predicate
    written as ``get(name) is slot`` inverts every guard: the common path skips
    ``sessions.remove`` while still answering 200.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots.pop(NAME)

    assert handlers._slot_still_ours(state, NAME, original) is True

    state._slots[NAME] = original
    assert handlers._slot_still_ours(state, NAME, original) is True

    replacement = state.get_or_create_slot("chat-2-1785")
    state._slots[NAME] = replacement
    assert handlers._slot_still_ours(state, NAME, original) is False


def test_shares_transcript_needs_a_replacement_and_the_same_file(tmp_path) -> None:
    """Only a DIFFERENT slot writing the SAME transcript makes the save yield.

    No replacement is the ordinary close. An unbound recreate over a LINKED tab shares
    no file, so the archive must still run on the original's transcript, which nothing
    else will archive.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME)
    state._slots.pop(NAME)

    # The ordinary post-pop state, and the pre-pop state, are both "no hand-over".
    assert handlers._replacement_shares_transcript(state, NAME, original) is False
    state._slots[NAME] = original
    assert handlers._replacement_shares_transcript(state, NAME, original) is False

    # Two unbound slots on one key resolve one transcript: the case these tests pin.
    state._slots.pop(NAME)
    unbound = state.get_or_create_slot(NAME)
    assert unbound is not original
    assert handlers._replacement_shares_transcript(state, NAME, original) is True

    # A linked original vs an unbound replacement: same key, two files.
    linked = state.get_or_create_slot("chat-9-1785", linked_session_key="cron:job7")
    assert handlers._replacement_shares_transcript(state, NAME, linked) is False


def test_shares_transcript_compares_files_not_key_strings(tmp_path) -> None:
    """A channel stem and its canonical key are ONE file, and must read as shared.

    ``history._safe_key`` folds ``slack:<ts>`` and ``slack_<ts>`` onto one ``.jsonl``. A
    string comparison would stamp ``closed`` on the live replacement's file;
    over-reporting shared only declines an archive the next close will make.
    """
    state = _make_state(tmp_path)
    stem = "slack_1785370133.085469"
    original = state.get_or_create_slot(stem, linked_session_key="slack:1785370133.085469")
    state._slots.pop(stem)
    replacement = state.get_or_create_slot(stem, channel_origin=True)

    assert replacement is not original
    assert not replacement.linked_session_key, "the stem must stay unresolved for this case"
    assert handlers.slot_history_key(original) != handlers.slot_history_key(
        replacement
    ), "the two keys must differ, or this test is not about the fold"
    assert handlers._replacement_shares_transcript(state, stem, original) is True


# --------------------------------------------------------------------------- #
# api_chat_slot_delete
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_recreate_during_save_preserves_replacement(tmp_path, monkeypatch) -> None:
    """(a) A recreate landing inside the closed=True save must survive intact.

    The re-check before ``sessions.remove`` must see the key is not ours and skip the
    teardown of the session the replacement now uses.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> bool:
        # Park so the recreate can interleave INSIDE the teardown window. Answer
        # True: this double models the durable write itself, and the guards
        # under test are the post-save identity re-checks, not the save's own
        # refusal arbitration.
        entered.set()
        await release.wait()
        return True

    removed_keys: list[str] = []

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)  # close is parked inside the persist
    # The concurrent same-key recreate mints a fresh slot object under NAME.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    assert removed_keys == [], "sessions.remove tore down the live replacement's session"


@pytest.mark.asyncio
async def test_delete_recreate_during_task_cancel_hits_first_guard(tmp_path, monkeypatch) -> None:
    """(a2) A recreate landing in the task-cancel await hits the FIRST guard.

    This is the only window where the pre-save guard fires: the close is parked in
    ``asyncio.wait_for(asyncio.shield(slot.task), 2.0)``, so neither the save nor
    ``sessions.remove`` runs. The other cases park inside the save and exercise only the
    later guards.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    assert original.running, "the turn must be live so the cancel-wait actually blocks"

    saved: list[tuple[bool, bool]] = []
    removed_keys: list[str] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The concurrent same-key recreate mints a fresh slot while the close is
    # still short of the pre-save guard.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()  # let the cancelled turn finish so the wait returns
    resp = await close

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement, "the first guard did not preserve the replacement"
    # The guard stops the ARCHIVE, not the write: the hand-over drain still saves the
    # original's own window, so its tail is not lost. Exactly one write, and it is
    # the shape the hand-over is allowed — closed=False, so nothing stamps the
    # archive flag on a key a live replacement holds, and rows_only, so it claims
    # the rows without rebuilding a metadata line the replacement owns.
    assert saved == [(False, True)], "the hand-over write was not the rows-only open save"
    assert removed_keys == [], "sessions.remove ran past the first guard on the replacement's key"


@pytest.mark.asyncio
async def test_delete_first_guard_keeps_the_app_dismissal_and_says_so(
    tmp_path, monkeypatch, caplog
) -> None:
    """(a3) The pre-save guard keeps the app dismissal, and logs that it did.

    Resuming the crew would re-arm an autonomous worker onto the replacement's key. This
    is the more common hand-over, so the log line is what explains a paused app worker.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]
    original._app = "issue-radar"

    undone: list[str] = []

    async def _told(_app: str, _slot_key: str) -> bool:
        return True

    async def _undo(_app: str, slot_key: str) -> bool:
        undone.append(slot_key)
        return True

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    saved: list[tuple[bool, bool]] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _told)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_close_undone", _undo)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    caplog.set_level(logging.WARNING, logger=handlers.__name__)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement
    assert saved == [(False, True)], "the hand-over write was not the rows-only open save"
    assert undone == [], "the dismissed app worker was resumed onto the replacement's key"
    assert any(
        "keeps the dismissal" in record.getMessage() for record in caplog.records
    ), "the app-dismissal hand-over left no operator record"


@pytest.mark.asyncio
async def test_delete_recreate_between_save_and_remove_skips_remove(tmp_path, monkeypatch) -> None:
    """(b) A recreate landing between the save and sessions.remove skips remove.

    ``sessions.remove`` tears down the session backing the reused key, which the
    replacement now uses; the second identity re-check must skip it.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]

    removed_keys: list[str] = []

    async def _remove(key) -> None:
        removed_keys.append(key)

    # The recreate lands AFTER the save completes but BEFORE sessions.remove.
    # The second identity re-check, immediately before the remove on the same
    # frame, is what must catch it. Recreating as the save's final act reproduces
    # exactly that interleaving deterministically.
    async def _persist_then_recreate(*_a, **_kw) -> bool:
        state.get_or_create_slot(NAME)
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist_then_recreate)
    state.sessions.remove = _remove  # type: ignore[assignment]

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    replacement = state._slots.get(NAME)
    assert replacement is not None and replacement is not original, "replacement lost"
    assert removed_keys == [], "sessions.remove tore down the live replacement's session"


@pytest.mark.asyncio
async def test_delete_failure_arm_does_not_clobber_replacement(tmp_path, monkeypatch) -> None:
    """(c) A persist that raises WHILE a replacement owns the key must not restore.

    The failure arm's ``state._slots[name] = slot`` would overwrite the live
    replacement with the failed original; the guard must leave the replacement.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    # The close still fails (its own persist raised), but the restore must NOT
    # have clobbered the live replacement.
    assert resp.status == 500
    assert state._slots.get(NAME) is replacement, "the failure arm clobbered the replacement"


@pytest.mark.asyncio
async def test_delete_failure_arm_skips_both_compensations_when_restore_skipped(
    tmp_path, monkeypatch
) -> None:
    """(c2) A skipped restore skips BOTH compensations owed to the original.

    - The retired auto-nudge loop's admission check
      (``lambda: state.get_slot(name) is slot``) is False, so
      ``AutoNudgeService._add_unserialized`` raises ``NudgeAdmissionRefused`` and the
      loop stays retired.
    - ``notify_slot_close_undone`` must not fire: the crew watchdog resolves its
      ``slot_key`` with a bare ``state.get_slot(slot_key)``, so a resume would hand the
      auto-approve grant to the user-owned replacement.

    ``test_delete_failure_arm_undoes_the_app_close_when_the_slot_is_restored`` pins that
    the undo still fires on the ordinary restore.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]
    original._app = "issue-radar"

    svc = AutoNudgeService(base_dir=tmp_path)
    monkeypatch.setattr(autonudge, "_INSTANCE", svc)
    await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24)

    undone: list[str] = []

    async def _told(_app: str, _slot_key: str) -> bool:
        return True

    async def _undo(_app: str, slot_key: str) -> bool:
        undone.append(slot_key)
        return True

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("disk wedged")

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _told)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_close_undone", _undo)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 500
    # The _slots restore was skipped: the live replacement is untouched.
    assert state._slots.get(NAME) is replacement, "the failure arm clobbered the replacement"
    # The app-notify-undo was NOT taken back: resuming the crew would target the
    # replacement's key, and its watchdog grants trust to whatever slot it finds.
    assert undone == [], "the dismissed app worker was resumed onto the replacement's key"
    # The nudge-loop restore is correctly REFUSED: its admission check
    # (state.get_slot(name) is slot) is False while a replacement owns the key,
    # so _add_unserialized raises NudgeAdmissionRefused and the loop stays retired.
    assert (
        svc.get_by_slot(NAME) is None
    ), "the retired loop was revived onto a key the original no longer owns"
    svc.stop()


@pytest.mark.asyncio
async def test_delete_failure_arm_undoes_the_app_close_when_the_slot_is_restored(
    tmp_path, monkeypatch
) -> None:
    """(c3) The other half of the coupling: a restored tab DOES resume its worker.

    With no recreate the failed close puts the original back in ``_slots``, so the
    dismissal genuinely did not happen and the durably-committed pause must be
    taken back — otherwise the user gets an error AND a silently stopped worker.
    Without this the coupling in (c2) could be satisfied by never undoing at all.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]
    original._app = "issue-radar"

    undone: list[str] = []

    async def _told(_app: str, _slot_key: str) -> bool:
        return True

    async def _undo(_app: str, slot_key: str) -> bool:
        undone.append(slot_key)
        return True

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _told)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_close_undone", _undo)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert state._slots.get(NAME) is original, "the failed close did not restore the slot"
    assert undone == [NAME], "a restored tab left its app worker paused"


@pytest.mark.asyncio
async def test_delete_ordinary_close_still_saves_and_removes(tmp_path, monkeypatch) -> None:
    """(f) With NO recreate, the guard is inert: pop, save closed=True, remove.

    Proves the guard does not change the common path.
    """
    state = _state_with_slot(tmp_path)

    saved_closed: list[bool] = []
    removed_keys: list[str] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved_closed.append(bool(kw.get("closed")))
        return True

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert NAME not in state._slots, "ordinary close did not remove the slot"
    assert saved_closed == [True], "ordinary close did not persist closed=True"
    assert removed_keys == [f"dashboard:{NAME}"], "ordinary close did not tear down the session"


# --------------------------------------------------------------------------- #
# api_chat_slots_cleanup (bulk archive)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cleanup_recreate_during_save_preserves_replacement(tmp_path, monkeypatch) -> None:
    """(d) The bulk path: a recreate inside the archive save must survive.

    The replacement must not be archived-over, must remain in ``_slots``, and its
    session must not be removed.
    """
    state = _state_with_slot(tmp_path)
    original = _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()

    removed_keys: list[str] = []

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close)  # parked inside the archive save for NAME
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert NAME not in payload["keys"], "a live replacement was reported archived-over"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by cleanup"
    assert removed_keys == [], "cleanup tore down the live replacement's session"


@pytest.mark.asyncio
async def test_cleanup_recreate_during_task_cancel_hits_first_guard(tmp_path, monkeypatch) -> None:
    """(d2) Bulk path: a recreate in the task-cancel await hits the FIRST guard.

    The first ``_slot_still_ours`` check takes the early ``continue``: no flush, no
    save, no ``sessions.remove``, and NAME is not reported in the archived ``keys``.
    """
    state = _state_with_slot(tmp_path)
    original = _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    assert original.running, "the turn must be live so the cancel-wait actually blocks"

    saved: list[tuple[bool, bool]] = []
    removed_keys: list[str] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait for NAME")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert NAME not in payload["keys"], "a live replacement was reported archived-over"
    assert state._slots.get(NAME) is replacement, "the first guard did not preserve the replacement"
    # The guard stops the ARCHIVE, not the write: the hand-over drain still saves the
    # original's own window, so its tail is not lost. Exactly one write, and it is
    # the shape the hand-over is allowed — closed=False, so nothing stamps the
    # archive flag on a key a live replacement holds, and rows_only, so it claims
    # the rows without rebuilding a metadata line the replacement owns.
    assert saved == [(False, True)], "the hand-over write was not the rows-only open save"
    assert removed_keys == [], "sessions.remove ran past the first guard on the replacement's key"


@pytest.mark.asyncio
async def test_cleanup_failure_arm_does_not_clobber_replacement(tmp_path, monkeypatch) -> None:
    """(e) Bulk failure arm: a persist that raises while a replacement owns the key.

    ``state._slots[name] = removed`` must not overwrite the live replacement.
    """
    state = _state_with_slot(tmp_path)
    original = _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert NAME in payload["failed"], "the failed original should be reported failed"
    assert state._slots.get(NAME) is replacement, "the failure arm clobbered the replacement"


@pytest.mark.asyncio
async def test_cleanup_ordinary_archive_still_saves_and_removes(tmp_path, monkeypatch) -> None:
    """(g) With NO recreate, the bulk guards are inert too.

    The delete-path sibling of this is (f). Both are needed: the two cleanup guards
    are separate call sites, and an inverted predicate makes cleanup report
    ``keys == []`` — an archive pass that silently archives nothing.
    """
    state = _state_with_slot(tmp_path)
    _make_stale(state)

    saved_closed: list[bool] = []
    removed_keys: list[str] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved_closed.append(bool(kw.get("closed")))
        return True

    async def _remove(key) -> None:
        removed_keys.append(key)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = _remove  # type: ignore[assignment]

    resp = await handlers.api_chat_slots_cleanup(_Req(state, NAME))

    payload = _json(resp)
    assert resp.status == 200
    assert payload["keys"] == [NAME], "ordinary cleanup archived nothing"
    assert NAME not in state._slots, "ordinary cleanup did not remove the slot"
    assert saved_closed == [True], "ordinary cleanup did not persist closed=True"
    assert removed_keys == [f"dashboard:{NAME}"], "ordinary cleanup did not tear down the session"


# --------------------------------------------------------------------------- #
# the KEY-SCOPED restricted marker
#
# ``state._restricted_keys`` holds ``dashboard:{name}``, a SESSION KEY, and
# ``_is_restricted_session`` checks it before the slot. An incognito original yielding
# its key must not leave a persistent replacement answering 403; a restricted
# replacement KEEPS the marker, so a blanket discard is not a legal fix.
# --------------------------------------------------------------------------- #


def _state_with_restricted_slot(tmp_path, mode: str = "temporary"):
    """A state whose only slot is a guest/incognito tab, so its key is marked."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME, memory_mode=mode)
    slot.append("user", "off the record")
    slot.drain()
    assert RKEY in state._restricted_keys, "the fixture did not mark the restricted key"
    return state


def test_resettle_reads_the_current_owner_not_the_popped_slot(tmp_path) -> None:
    """The postcondition, directly: marked iff the slot AT the key is restricted."""
    state = _state_with_restricted_slot(tmp_path)
    restricted = state._slots[NAME]

    # A live restricted owner keeps (and re-asserts) the marker.
    state._restricted_keys.discard(RKEY)
    handlers._resettle_restricted_key(state, NAME)
    assert RKEY in state._restricted_keys, "a restricted owner lost its marker"

    # An absent key is not restricted — the ordinary post-close state.
    state._slots.pop(NAME)
    handlers._resettle_restricted_key(state, NAME)
    assert RKEY not in state._restricted_keys, "a freed key kept the marker"

    # A persistent owner clears it, even though the popped slot was restricted.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not restricted and not replacement.is_restricted
    state._restricted_keys.add(RKEY)
    handlers._resettle_restricted_key(state, NAME)
    assert RKEY not in state._restricted_keys, "a persistent replacement inherited the marker"


@pytest.mark.asyncio
async def test_delete_first_guard_hands_the_marker_to_the_replacement(
    tmp_path, monkeypatch
) -> None:
    """(h) The pre-save early return must settle the marker from the NEW owner.

    It returns before the discard that follows the save, so the hand-over has to
    re-derive the marker itself. The guest tab's unsaved row is drained into the
    file the replacement writes, which tightens the replacement to the guest mode;
    the marker then describes that tightened owner, not the popped slot.
    """
    state = _state_with_restricted_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    # Records instead of raising: the drain the hand-over exit performs also goes
    # through ``save_slot_off_loop``, and a stub that fails EVERY write would fail
    # that one too and turn this exit into the drain-failure case. The pin is on the
    # shape of the writes, asserted below — exactly one, open and rows-only.
    saved: list[tuple[bool, bool]] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement
    assert saved == [(False, True)], "the closed=True save ran past the first guard"
    # The guest tab's row was handed to the file the replacement writes, so the
    # replacement itself is tightened to the guest mode and the marker follows it.
    assert replacement.memory_mode == "temporary", "the replacement kept a looser mode"
    assert RKEY in state._restricted_keys, "the marker was not re-derived from the tightened owner"


@pytest.mark.asyncio
async def test_delete_first_guard_keeps_the_marker_for_a_restricted_replacement(
    tmp_path, monkeypatch
) -> None:
    """(h2) Fail CLOSED: a replacement that is itself restricted KEEPS the marker.

    The hand-over re-derives from the new owner; a blanket discard would open
    memory writes on a guest replacement, which is the direction that must never
    happen. This test is what makes the discard in (h) a re-derivation.
    """
    state = _state_with_restricted_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    # Records instead of raising: the drain the hand-over exit performs also goes
    # through ``save_slot_off_loop``, and a stub that fails EVERY write would fail
    # that one too and turn this exit into the drain-failure case. The pin is on the
    # shape of the writes, asserted below — exactly one, open and rows-only.
    saved: list[tuple[bool, bool]] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME, memory_mode="incognito")
    assert replacement is not original and replacement.is_restricted
    release.set()
    resp = await close

    assert resp.status == 200
    assert saved == [(False, True)], "the closed=True save ran past the first guard"
    assert RKEY in state._restricted_keys, "a restricted replacement lost its own marker"


@pytest.mark.asyncio
async def test_delete_failure_arm_hands_the_marker_to_the_replacement(
    tmp_path, monkeypatch
) -> None:
    """(h3) A failed drain restores a replacement when its line stayed persistent."""
    state = _state_with_restricted_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 500
    assert state._slots.get(NAME) is replacement
    assert (
        replacement.memory_mode == "persistent"
    ), "the failed drain left the replacement restricted"
    assert RKEY not in state._restricted_keys, "the failed drain kept the guest tab's marker"


@pytest.mark.asyncio
async def test_delete_failure_arm_keeps_the_marker_when_the_original_returns(
    tmp_path, monkeypatch
) -> None:
    """(h4) A restored guest tab keeps its marker — the restore is not a downgrade."""
    state = _state_with_restricted_slot(tmp_path)
    original = state._slots[NAME]

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert state._slots.get(NAME) is original
    assert RKEY in state._restricted_keys, "a restored guest tab lost its marker"


@pytest.mark.asyncio
async def test_cleanup_first_guard_hands_the_marker_to_the_replacement(
    tmp_path, monkeypatch
) -> None:
    """(h5) The bulk path's pre-save ``continue`` has the same duty as (h)."""
    state = _state_with_restricted_slot(tmp_path)
    original = _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    # Records instead of raising: the drain the hand-over exit performs also goes
    # through ``save_slot_off_loop``, and a stub that fails EVERY write would fail
    # that one too and turn this exit into the drain-failure case. The pin is on the
    # shape of the writes, asserted below — exactly one, open and rows-only.
    saved: list[tuple[bool, bool]] = []

    async def _persist(_state, _slot, *_a, **kw) -> bool:
        saved.append((bool(kw.get("closed")), bool(kw.get("rows_only"))))
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert NAME not in payload["keys"]
    assert payload["failed"] == [], "the hand-over drain did not commit"
    assert saved == [(False, True)], "the closed=True save ran past the first guard"
    # The guest tab's row was handed to the file the replacement writes, so the
    # replacement itself is tightened to the guest mode and the marker follows it.
    assert replacement.memory_mode == "temporary", "the replacement kept a looser mode"
    assert RKEY in state._restricted_keys, "the marker was not re-derived from the tightened owner"


@pytest.mark.asyncio
async def test_cleanup_failure_arm_hands_the_marker_to_the_replacement(
    tmp_path, monkeypatch
) -> None:
    """(h6) The bulk failure arm skips the restore, so it must settle the marker."""
    state = _state_with_restricted_slot(tmp_path)
    original = _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    assert NAME in _json(resp)["failed"]
    assert state._slots.get(NAME) is replacement
    assert (
        replacement.memory_mode == "persistent"
    ), "the failed drain left the replacement restricted"
    assert RKEY not in state._restricted_keys, "the failed drain kept the guest tab's marker"


# --------------------------------------------------------------------------- #
# what the hand-over must NOT cost: the original's unpersisted tail
# --------------------------------------------------------------------------- #
#
# The periodic flush walks ``state._slots``, so a popped slot has no retry path. These
# drive the REAL ``save_slot_off_loop`` and pin both halves: the replacement survives
# AND every row survives.


@pytest.mark.asyncio
async def test_delete_handover_persists_the_tail_and_keeps_the_replacement(tmp_path) -> None:
    """The pre-save hand-over exit must cost neither the replacement nor the tail.

    This exit opens while a turn is in flight, so the rows at risk are usually the
    reply the user was watching. The drain writes them with ``closed=False``.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    assert original.running, "the turn must be live so the cancel-wait actually blocks"

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    # Half one: the fix holds.
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    assert state.sessions.remove.await_count == 0, "the replacement's session was torn down"
    # Half two: nothing the original held was dropped on the way out.
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ], "the handed-over slot's unpersisted rows were lost"
    assert not state.conversation_log.get_metadata(HKEY).get(
        "closed"
    ), "a key a live replacement holds was stamped closed"


@pytest.mark.asyncio
async def test_delete_handover_writes_a_linked_slot_own_transcript(tmp_path) -> None:
    """The drain must authorize the slot's OWN transcript, never a derived one.

    A linked tab lives at its ``linked_session_key``, and ``_save_slot_to_history``
    refuses the whole save when ``expected_history_key`` names another transcript. The
    recreate here binds the SAME linked key (a cron re-injecting its tab); the unbound
    case is ``test_delete_divergent_transcript_still_archives_the_original``.
    """
    state = _make_state(tmp_path)
    linked = "cron:job7"
    original = state.get_or_create_slot(NAME, linked_session_key=linked)
    original.append("user", "PERSISTED-1")
    original.drain()
    assert await handlers.save_slot_off_loop(state, original, best_effort=False)
    assert original._disk_window_len == 1, "the seed save did not commit the first window"
    original.append("assistant", "TAIL-2")
    original.drain()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME, linked_session_key=linked)
    assert replacement is not original
    release.set()
    resp = await close

    assert resp.status == 200
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    assert [m.get("content", "") for m in state.conversation_log.read_messages(linked)] == [
        "PERSISTED-1",
        "TAIL-2",
    ], "the linked slot's tail never reached its own transcript"
    assert not state.conversation_log.get_metadata(linked).get("closed")
    assert _disk_contents(state) == [], "rows landed on a transcript this slot never used"


@pytest.mark.asyncio
async def test_delete_handover_write_failure_fails_the_close_and_names_the_rows(
    tmp_path, monkeypatch, caplog
) -> None:
    """A store that cannot take the tail owes the caller an error AND a row count.

    Nothing is left to retry, so the close answers ``history_save_failed`` and logs the
    lost row count, derived as ``len(messages) - _disk_window_len``.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    async def _persist(*_a, **_kw) -> bool:
        raise OSError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    caplog.set_level(logging.ERROR, logger=handlers.__name__)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    release.set()
    resp = await close

    assert resp.status == 500, "a lost hand-over tail was reported as a successful close"
    assert _json(resp)["code"] == "history_save_failed"
    # The replacement is still untouched: reporting the loss is not a licence to
    # undo the hand-over, because there is no tab to put back.
    assert state._slots.get(NAME) is replacement
    assert state.sessions.remove.await_count == 0, "the replacement's session was torn down"
    assert any(
        "2 unpersisted row(s) could not be written" in record.getMessage()
        for record in caplog.records
    ), "an unrecoverable hand-over loss was not reported"


@pytest.mark.asyncio
async def test_cleanup_handover_write_failure_is_reported_failed(tmp_path, monkeypatch) -> None:
    """The bulk path owes the same report, in the column it has for it.

    ``failed`` is the honest answer: the key is absent from ``keys`` either way, so
    without this a pass that dropped a slot's tail is indistinguishable from a pass
    that found nothing to do.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    _make_stale(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    async def _persist(*_a, **_kw) -> bool:
        raise OSError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    release.set()
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert payload["failed"] == [NAME], "a dropped hand-over tail was reported as a clean pass"
    assert NAME not in payload["keys"], "a live replacement was reported archived-over"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by cleanup"


@pytest.mark.asyncio
async def test_delete_failure_arm_handover_persists_the_tail(tmp_path, monkeypatch) -> None:
    """The failure arm's "restore so data isn't lost" must hold for a hand-over too.

    The restore is skipped, so the arm writes the rows to the shared transcript itself.
    Only the closed=True save fails: a store that rejected one write can take the next.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)

    real_save = handlers.save_slot_off_loop
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(_state, _slot, *a, **kw):
        if kw.get("closed"):
            # Park so the recreate lands INSIDE the archive save, then fail it.
            entered.set()
            await release.wait()
            raise OSError("disk wedged")
        return await real_save(_state, _slot, *a, **kw)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close)
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    release.set()
    resp = await close

    # The close still reports its own failure; what must not happen is losing
    # either the replacement or the rows.
    assert resp.status == 500
    assert state._slots.get(NAME) is replacement, "the failure arm clobbered the replacement"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
    ], "the failure arm dropped the handed-over slot's unpersisted rows"
    assert not state.conversation_log.get_metadata(HKEY).get("closed")


@pytest.mark.asyncio
async def test_cleanup_handover_persists_the_tail_and_the_held_notes(tmp_path) -> None:
    """The bulk path's hand-over owes the tail AND the notes it is still holding.

    ``_deferred_notes`` is in-memory only, and this exit is upstream of the
    ``flush_deferred_notes()`` the ordinary archive runs — so the popped object is
    the sole copy of a held note. The drain flushes them into the window first,
    which is what makes the note durable instead of merely counted.
    """
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    original._deferred_notes.append(
        {"content": "HELD-NOTE", "cls": "msg msg-note", "session": HKEY}
    )
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
    resp = await close

    payload = _json(resp)
    assert resp.status == 200
    assert NAME not in payload["keys"], "a live replacement was reported archived-over"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by cleanup"
    assert state.sessions.remove.await_count == 0, "the replacement's session was torn down"
    assert _disk_contents(state) == [
        "PERSISTED-1",
        "PERSISTED-2",
        "TAIL-3",
        "TAIL-4",
        "HELD-NOTE",
    ], "cleanup's hand-over dropped the original's unpersisted rows or its held note"
    assert not state.conversation_log.get_metadata(HKEY).get("closed")
    assert not original._deferred_notes, "the held note was left in memory on a dropped slot"
