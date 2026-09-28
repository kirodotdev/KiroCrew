"""Close-vs-recreate race on the shared dashboard slot-close teardown.

The defect these pin: both ``api_chat_slot_delete`` and
``api_chat_slots_cleanup`` pop ``name`` out of ``state._slots`` and then run a
sequence of AWAITS — cancel the task, ``save_slot_off_loop(..., closed=True)``,
``state.sessions.remove(_history_key_for(name))``. A concurrent same-key
recreate (a POST /api/chat, or the session_close MCP verb) can mint a
REPLACEMENT slot for the same key inside that window. The original, still in
flight, then (a) writes ITS transcript over the shared history key as closed and
(b) tears down the session the replacement now uses. The failure arms compound
it: they blindly ``state._slots[name] = <original>`` over whatever now owns the
key.

The fix re-checks ownership after the pop, at BOTH sites, with TWO predicates
because the destructive steps answer to two different owners. ``_slot_still_ours``
(no DIFFERENT object owns ``name``) guards the KEY-scoped steps: ``sessions.remove``
on ``dashboard:{name}``, the failure-arm restores, cleanup's ``archived`` report.
``_replacement_shares_transcript`` (a different object owns ``name`` AND resolves the
same file) guards the closed=True save, whose resource is the TRANSCRIPT rather than
the key — a linked tab and an unbound same-name recreate hold one key and two files,
and yielding the archive there would leave the original's transcript unarchived for
the reconcile pass to resurface.

These tests interleave a recreate across the teardown awaits (via an
``asyncio.Event`` the monkeypatched ``save_slot_off_loop`` parks on) and assert the
replacement's identity, history, and session survive, that the destructive step was
skipped, that the original's own tail and archive were not paid for it, and that a
tail that could NOT be persisted is reported rather than answered 200. Each would
FAIL if its guard were reverted.

Each predicate's polarity is pinned on its own, because getting either backwards is
silent. For the key one, an absent key is the ORDINARY post-pop state, so a guard
reading ``get(name) is <popped>`` would fire on EVERY close and skip the teardown it
exists to protect — leaking the per-tab session, with a 200 either way. For the
transcript one, both directions fail quietly: reading a divergent pair as shared
loses an archive nothing else will make, and reading a folded pair as divergent
stamps ``closed`` on a file a live slot is writing. The two ``ordinary`` tests below
and the three predicate tests are that pin.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from pathlib import Path

import pytest
from chat_test_helpers import _make_state

from kiro_crew import autonudge, execution_context
from kiro_crew import history as history_mod
from kiro_crew import members as members_mod
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import chat_runner, chat_summary
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.history import is_incognito_transcript
from kiro_crew.memory_stores import UnknownMemoryStore

NAME = "chat-1-1785"


@pytest.fixture(autouse=True)
def _no_nudge_service(monkeypatch):
    """No auto-nudge service: these tests isolate the teardown-race guard.

    The nudge-loop retirement and its rollbacks are pinned by
    test_slot_close_nudge_race.py; here the close path must find nothing to
    retire so the only variable is the post-pop identity re-check.
    """
    monkeypatch.setattr(autonudge, "_INSTANCE", None)


class _Stream:
    """The ``request.content`` half of the fake: a one-shot chunked reader.

    ``read_bounded_json`` reads the body off the stream rather than calling
    ``request.json()`` whenever a byte cap is in force, so a double that only
    stubs ``json`` reads an EMPTY body on the capped path. Chunking at the
    helper's own 8 KiB step keeps the fake honest for a body large enough to
    arrive in several iterations.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i : i + n]


class _Req:
    """Minimal stand-in for the aiohttp request the handlers read.

    The race tests drive the handlers directly rather than through a client: the
    interleaving of the concurrent recreate has to be scheduled deterministically
    inside the teardown window, and a client's own awaits would let it run before
    the handler even reached the pop.

    The body surface mirrors what ``read_bounded_json`` actually touches --
    ``can_read_body`` first, then ``content_length``/``content``/``charset`` on the
    capped path -- not just ``json()``. ``api_chat_slots_cleanup`` moved onto that
    helper, and a double missing ``can_read_body`` does not merely fail: the
    handler raises before it reaches the seam the race tests park on, so the
    ``entered`` event never fires and every interleaved test reports the park
    rather than the attribute error underneath it. ``_reached`` is what keeps that
    shape a named failure instead of a run-length timeout.
    """

    def __init__(self, state, slot: str = NAME, body: dict | None = None) -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}
        self._has_body = body is not None
        self._body = body if body is not None else {}
        # ``body is None`` is NO body, which is what every call site here sends and
        # what ``can_read_body`` must read as False. An explicitly-passed ``{}`` is a
        # body that is PRESENT and empty, so keying off truthiness instead would make
        # the double answer False for a request aiohttp reports as readable.
        self._raw = b"" if body is None else json.dumps(self._body).encode()
        self.charset = "utf-8"
        self.content = _Stream(self._raw)

    @property
    def content_length(self) -> int | None:
        return len(self._raw) or None

    def get(self, key: str, default: str = "") -> str:
        del key
        return default

    @property
    def can_read_body(self) -> bool:
        """Mirror ``aiohttp.web.BaseRequest.can_read_body``: unread body pending.

        Every caller in this file omits ``body``, so the double must report the
        same "nothing to read" outcome the real request would for a bodyless
        request — that is what lets ``read_bounded_json(..., allow_absent=True)``
        take its empty-object path instead of raising on a stand-in that never
        modeled this property.
        """
        return self._has_body

    async def json(self) -> dict:
        return self._body


def _state_with_slot(tmp_path, name: str = NAME):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "watch the PR")
    slot.append("assistant", "watching")
    slot.drain()
    return state


def _arm_running_turn(slot, entered: asyncio.Event, release: asyncio.Event):
    """Give ``slot`` a live, parkable turn so its task-cancel await blocks.

    The close pops the slot and then, if ``slot.running``, cancels ``slot.task``
    and waits on ``asyncio.wait_for(asyncio.shield(slot.task), 2.0)``. To land a
    recreate in THAT await (the window the first pre-save guard protects) the
    task has to still be pending when the recreate is minted. This turn swallows
    the handler's cancel, signals ``entered`` so the test can mint the
    replacement, then parks on ``release`` before returning — so the handler's
    shielded wait is still blocked at the exact instant the key changes owner.
    ``slot.running`` is ``self.task is not None and not self.task.done()``, so
    assigning this task is all it takes to make the slot look busy.
    """

    async def _turn() -> None:
        try:
            await asyncio.Event().wait()  # block until cancelled
        except asyncio.CancelledError:
            entered.set()
            await release.wait()

    slot.task = asyncio.create_task(_turn())
    return slot.task


async def _reached(
    entered: asyncio.Event,
    close: asyncio.Task[object],
    *,
    seam: str = "save_slot_off_loop",
) -> None:
    """Wait for the close to reach its interleave seam, BOUNDED so a miss names itself.

    Every interleaving test below opens the recreate's window by parking on an
    event only the close's own progress can set — the monkeypatched
    ``save_slot_off_loop``, or the cancelled turn ``_arm_running_turn`` armed. The
    whole premise is that the close REACHES that seam, and a close that returns,
    raises or blocks short of it never sets the event: a teardown that stops
    cancelling the live turn (``slot.running`` read through a renamed field, say)
    leaves every ``_arm_running_turn`` case unarmed, and a preamble that raises or
    answers 404 before the pop — the ``can_read_body`` shape ``_Req`` documents —
    leaves every ``_persist`` case unentered.

    Unbounded, such a regression does not FAIL these tests, it PARKS them until the
    repo-wide ``--timeout``: on Linux and macOS a Timeout traceback and minutes of
    shard time per test instead of the one line below, and on Windows — no SIGALRM,
    so pytest-timeout kills the xdist worker, and CI runs
    ``--max-worker-restart=0`` — an aborted run whose unreached results do not
    exist at all (testing-conventions flake class 6). 5.0s is orders of magnitude
    more than the single loop hop this needs and far under that ceiling, so the
    deadline stays on the await whose property is under test and the failure is
    attributed to this test rather than to the run.

    Only the TEST side is bounded. The ``release.wait()`` calls inside the
    ``_persist`` stubs and ``_arm_running_turn`` run on the ``close`` task, which
    pytest tears down with the loop; bounding those would change what the close
    parks on, which is the interleave itself. The pending ``close`` is cancelled on
    the timeout path so the named assertion is not followed by a task destroyed at
    loop teardown and blamed on whichever test runs next.
    """
    try:
        await asyncio.wait_for(entered.wait(), 5.0)
    except asyncio.TimeoutError:
        close.cancel()
        raise AssertionError(
            f"the close never reached {seam}: it returned or blocked short of the "
            "interleave window, so the recreate could never be minted inside it"
        ) from None


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

    Three answers, and each is a different guard site's decision. No replacement
    (a freed key, or our own object still there) is the ordinary close, where the
    archive must run. An unbound recreate over a LINKED tab is a replacement that
    shares no file, where the archive must also run — on the original's own
    transcript, which nothing else will ever archive. Only a replacement resolving
    the same file may take the archive away.
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

    ``history._safe_key`` folds ``slack:<ts>`` and the ``slack_<ts>`` filename stem
    onto the same ``.jsonl``, so a bound original and a ``channel_origin``
    replacement whose stem never resolved hold two DIFFERENT key strings for one
    transcript. A string comparison would call that "not shared" and let the archive
    stamp ``closed`` on the file the live replacement is writing — the exact harm the
    guard exists for, and the asymmetric direction: over-reporting shared only
    declines an archive the next close will make.
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

    While the close is parked inside ``save_slot_off_loop`` a concurrent
    ``get_or_create_slot(NAME)`` mints a replacement. After the close returns the
    replacement must still own the key, and ``sessions.remove`` must NOT have been
    called for its key (the second identity re-check, before the remove, must see
    the key is not ours and skip the destructive teardown). Without the
    guard the close would run ``sessions.remove`` and tear down the session the
    replacement now uses.
    """
    state = _state_with_slot(tmp_path)
    original = state._slots[NAME]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> None:
        # Park so the recreate can interleave INSIDE the teardown window.
        entered.set()
        await release.wait()

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

    This is the ONLY window where the pre-save guard fires: the recreate is
    minted while the close is parked in ``asyncio.wait_for(asyncio.shield(
    slot.task), 2.0)`` — BEFORE ``save_slot_off_loop`` is reached — so the first
    ``_slot_still_ours`` check (immediately after the cancel block) sees the key
    is not ours and takes the early ``return {"ok": True}``. That means the
    closed=True save is NEVER attempted for the original and ``sessions.remove``
    is NEVER called: the replacement keeps its slot, its (unclosed) history, and
    its session. Reverting ONLY the first guard would let the close fall through
    to the save and the remove, so this case fails without it — the other cases
    park inside the save and so exercise only the second/failure-arm guards.
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

    This exit takes the same decision as the failure arm — the original is popped,
    cancelled and not coming back, so its dismissal stands, and resuming the crew
    would re-arm an autonomous worker onto the replacement's key. It is also the
    MORE common of the two hand-overs, so the operator-visible record matters more
    here than on the failure arm: without it the frequent case is the silent one and
    a paused app worker has no trace explaining why.
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
    async def _persist_then_recreate(*_a, **_kw) -> None:
        state.get_or_create_slot(NAME)

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

    The failed close owes the ORIGINAL two rollbacks, and both are conditional on
    the original getting its key back:

    - the retired auto-nudge loop goes through
      ``_restore_slot_nudge_loop(retired_loop, lambda: state.get_slot(name) is
      slot)``. With a replacement on ``name`` that admission check is False, so
      ``AutoNudgeService._add_unserialized`` raises ``NudgeAdmissionRefused`` and
      ``_restore_slot_nudge_loop`` swallows it — the loop stays retired.
    - app-notify-undo (``notify_slot_close_undone``) is coupled to the ``_slots``
      restore for the same reason. Resuming a crew re-arms an autonomous worker
      whose ``slot_key`` its watchdog resolves with a bare
      ``state.get_slot(slot_key)`` and no ownership test, so it would hand the
      auto-approve grant — and then an unbounded nudge clock — to the user-owned
      replacement now holding that key. The original is popped, cancelled and not
      coming back, so the dismissal stands; the crew keeps its ``paused_reason``
      row, which is the same state the pre-save guard leaves.

    Gating on ``slot._app`` alone is what makes that escalation reachable, so this
    pins the coupling in both directions: the undo must NOT fire here, and
    ``test_delete_failure_arm_undoes_the_app_close_when_the_slot_is_restored``
    pins that it still does on the ordinary restore.
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

    async def _persist(_state, _slot, *_a, **kw) -> None:
        saved_closed.append(bool(kw.get("closed")))

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


def _make_stale(state, name: str = NAME):
    """Age the slot's last activity past the 3-day cleanup cutoff."""
    slot = state._slots[name]
    slot.created_at = "2000-01-01T00:00:00+00:00"
    for m in slot.messages:
        m["ts"] = "2000-01-01T00:00:00+00:00"
    return slot


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

    The stale slot has a live turn, so the per-iteration cancel block awaits
    ``asyncio.wait_for(asyncio.shield(removed.task), 2.0)``. A recreate minted in
    THAT await lands before the flush+save, so the first ``_slot_still_ours``
    check takes the early ``continue`` — the flush and closed=True save never run
    for the original, ``sessions.remove`` is never called, and NAME is NOT
    appended to the archived ``keys`` (a live replacement must never be reported
    archived-over). Reverting only the first guard would let the pass flush, save
    onto the replacement's key, remove its session, and report it archived.
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

    async def _persist(_state, _slot, *_a, **kw) -> None:
        saved_closed.append(bool(kw.get("closed")))

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
# ``state._restricted_keys`` holds ``dashboard:{name}`` — a SESSION KEY, not a slot
# identity — and ``_is_restricted_session`` tests that set BEFORE it looks at the
# slot. So an incognito/guest original that yields its key to a persistent
# replacement makes every memory, artifact and mcp-apps call on the replacement
# answer 403 unless the marker is re-derived from the new owner. Each exit that
# yields the key is pinned below, plus the fail-CLOSED direction (a restricted
# replacement KEEPS the marker, so a blanket discard is not a legal fix).
# --------------------------------------------------------------------------- #

RKEY = f"dashboard:{NAME}"


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


def _json(resp) -> dict:
    """Decode an aiohttp json_response body to a dict."""
    import json

    return json.loads(resp.body)


# --------------------------------------------------------------------------- #
# what the hand-over must NOT cost: the original's unpersisted tail
# --------------------------------------------------------------------------- #
#
# Yielding the key preserves the replacement, and the object it yields from is the
# only reference to whatever the original never committed: the periodic flush walks
# ``state._slots``, so a popped, unreferenced slot has no retry path at all. These
# drive the REAL ``save_slot_off_loop`` against a real ``ConversationLog`` and pin
# BOTH halves at once — the replacement survives AND every row survives — because
# either alone is satisfiable by a change that breaks the other: skipping the write
# loses the tail, and doing the original write takes the replacement down with it.


HKEY = f"dashboard:{NAME}"


def _disk_contents(state) -> list[str]:
    """The transcript's message contents, in file order, read back from disk."""
    return [m.get("content", "") for m in state.conversation_log.read_messages(HKEY)]


def _summary_cfg():
    """A config with the session summary ON, so its gate is decided by the slot."""
    from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig

    cfg = KiroCrewConfig()
    cfg.session_summary = SessionSummaryConfig(enabled=True)
    return cfg


def _floor_after_disk_tail(state, slot) -> None:
    """Tell a fresh same-key *slot* the newest ``ts`` already in its transcript file.

    A slot learns the file's tail only at its own first save boundary
    (``note_disk_tail``), so a replacement's FIRST row is stamped from the bare
    clock. On a host whose clock ticks coarsely (Windows advances it in ~15.6 ms
    steps) that stamp can tie the rows the dismissed original committed a moment
    earlier, and the save's two-way ``ts`` merge keeps the window's line first on
    an exact tie -- so the replacement's row would read back AHEAD of rows that
    were written before it. These tests assert file order to prove every row
    survived, not to probe that merge, so the replacement is told the on-disk
    tail up front, the same floor its first save would have recorded.
    """
    slot.note_disk_tail(*(m.get("ts") for m in state.conversation_log.read_messages(HKEY)))


async def _slot_with_committed_and_uncommitted_rows(state, name: str = NAME):
    """A slot with two rows on disk and two rows that have never been written.

    ``_disk_window_len`` is what the last committed save covered, so this is the
    shape that makes "the tail was lost" observable: a hand-over that writes
    nothing leaves the file holding only the first two.
    """
    slot = state.get_or_create_slot(name)
    slot.append("user", "PERSISTED-1")
    slot.append("assistant", "PERSISTED-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    assert slot._disk_window_len == 2, "the seed save did not commit the first window"
    slot.append("user", "TAIL-3")
    slot.append("assistant", "TAIL-4")
    slot.drain()
    return slot


@pytest.mark.asyncio
async def test_delete_handover_persists_the_tail_and_keeps_the_replacement(tmp_path) -> None:
    """The pre-save hand-over exit must cost neither the replacement nor the tail.

    This exit needs no store failure to reach: it returns BEFORE the save is
    attempted, in a window that opens while a turn is in flight — so the rows at
    risk are typically the reply the user was watching. The drain writes the same
    window the close was about to write, with ``closed=False``, which is the one
    difference that matters: the transcript gains the rows and does NOT gain the
    archive flag on a key a live replacement holds.
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

    A cron-, channel- or workflow-injected tab carries a ``linked_session_key``, and
    that key — not ``dashboard:{name}`` — is where its conversation lives.
    ``_save_slot_to_history`` resolves its write target through
    ``slot_history_key(slot)`` and REFUSES the whole save when the caller's
    ``expected_history_key`` names a different transcript, so a derived pin makes the
    drain write nothing at all for exactly the slots whose transcript is shared with
    something outside the dashboard — and names a row-less file in the report.

    The recreate here is bound to the SAME linked key, which is what a cron
    re-injecting the same job's tab produces. That is what puts both slots on one
    transcript and so makes this the hand-over case at all; an UNBOUND recreate over
    the same tab shares nothing and is
    ``test_delete_divergent_transcript_still_archives_the_original``.
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

    The drain is the only path to durability on this exit, so when it fails the loss
    is unrecoverable — and then the two things left that matter are that the caller
    is not told the close succeeded, and that the loss is not silent. Returning 200
    would claim durability this close does not have, with nothing left to retry it;
    the ``history_save_failed`` code is the same one an ordinary failed archive
    raises, because from the caller's side it is one thing. The count is derived the
    way the drain derives it (``len(messages) - _disk_window_len``), so the log line
    names the rows rather than just the slot.
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

    When the key went to a replacement the restore is skipped, and skipping it is
    correct — but the reason the restore existed does not go away with it. The
    original is unreferenced from that point, so the arm has to get its rows onto
    the shared transcript itself. The seeded failure is the closed=True save only:
    a real store that rejected one write can still take the next, and a lock lost
    to the recreate is exactly that case.
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


async def _publish_metadata(
    state, slot, *, title: str, folder: str, origin: str = "auto", refresh_mark: int = 0
) -> None:
    """Commit ``slot``'s title and folder the way every metadata route does.

    A forced save is what the tag / folder / pin / recreate-PATCH routes use, and
    for a message-less slot it is the empty-window ``update_metadata_if`` merge —
    the exact shape a replacement born from ``POST /api/chat/slots`` with a folder
    or a pinned title takes.

    ``origin`` sets the title's provenance the way the titling paths do — ``"auto"``
    for a generated name, ``"user"`` for a manual rename — and ``refresh_mark`` the
    background-refresh budget already spent, because the save persists BOTH
    alongside the title. A pairing test that left these at their defaults would read
    a consistent line no matter which slot each half came from.
    """
    slot.title = title
    slot.folder_id = folder
    slot._titled = True
    slot._title_origin = origin
    slot._title_refresh_mark = refresh_mark
    assert await handlers.save_slot_off_loop(state, slot, force=True, best_effort=False)


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
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_delete_handover_of_a_restricted_tail_tightens_a_persistent_line(
    tmp_path, caplog, monkeypatch, mode
) -> None:
    """A restricted tail draining under a persistent replacement's line tightens it.

    The rows-only drain defers ``memory_mode`` to the line the replacement
    published, and that field is what every learning reader gates on. Writing
    the restricted original's tail under a line that says persistent would make
    consolidation, the history tools and the summary treat private content as
    ordinary; refusing the write would lose the reply the user was watching, with
    no retry path (the slot is popped). The line is a ratchet any writer may
    tighten, so the drain folds the retained mode in and lands the rows under the
    stricter value -- the same file the other race order reaches, where the
    original's committed line ratchets the replacement's own save (see
    ``test_delete_handover_of_a_restricted_tail_lands_under_the_ratcheted_line``).
    A restricted line names no store, so the replacement's store name goes with
    the loosening; its title and folder are not the drain's to touch and stay.

    The line is not the only thing that must tighten. The replacement is LIVE
    and has already bound a persistent execution -- a durable record on the same
    line, which ``read_session_execution`` answers from -- and the readers that
    gate on the live slot rather than the line (the session summary, the export)
    read ``slot.memory_mode`` and then the whole transcript from disk. So the
    drain tightens the replacement slot itself before the rows are written, the
    save folds the carried execution record with the line, and the summary gate
    refuses the replacement afterwards: the private rows are on disk only once
    every reader of that file, by line or by slot, already sees them as restricted.

    The original committed NOTHING before the close, so the persistent line is
    the replacement's alone -- the only shape in which the drain meets a looser
    line. The replacement's first row is floored after the original's rows so the
    file order asserted below is a property of the write sequence and not of the
    host clock's resolution.
    """
    state = _make_state(tmp_path)
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "TAIL-1")
    original.append("assistant", "TAIL-2")
    original.drain()
    assert not state.conversation_log._path(HKEY).exists()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    caplog.set_level(logging.INFO)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert replacement.memory_mode == "persistent"
    replacement.note_disk_tail(*(m.get("ts") for m in original.messages))
    replacement.append("user", "REPLACEMENT-1")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    # The replacement's turn-start binding: a persistent DURABLE carrier on the line.
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "persistent"
    assert "memory_store" in meta, "the persistent binding wrote no store field to drop"
    assert meta[execution_context.EXECUTION_CONTEXT_KEY]["memory_mode"] == "persistent"
    assert execution_context.read_session_execution(HKEY).memory_mode == "persistent"
    assert not chat_summary._should_summarize(
        _summary_cfg(), replacement, 4, force=True
    ), "the persistent replacement was not summarizable before the drain"
    release.set()
    resp = await close

    assert resp.status == 200, "the restricted tail was refused instead of landing"
    assert state._slots.get(NAME) is replacement
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the drain filed private rows under a persistent line"
    assert "memory_store" not in meta, "the tightened line still names a store"
    assert meta.get("title") == "REPLACEMENT TITLE"
    assert meta.get("folder_id") == "f-r"
    assert _disk_contents(state) == ["TAIL-1", "TAIL-2", "REPLACEMENT-1"]
    # The live holder of the file is restricted too, not just the line.
    assert replacement.memory_mode == mode, "the live replacement still reads persistent"
    assert HKEY in state._restricted_keys, "the key-scoped marker was not re-derived"
    record = meta[execution_context.EXECUTION_CONTEXT_KEY]
    assert record["memory_mode"] == mode, "the carried execution record still says persistent"
    assert record["store"] == {"store_id": "default", "member_id": None}, "the identity moved"
    assert execution_context.read_session_execution(HKEY).memory_mode == mode
    assert (
        chat_summary._should_summarize(_summary_cfg(), replacement, 4, force=True) == "memory_mode"
    ), "the summary would still read the private rows off disk for the replacement"
    messages = [record.getMessage() for record in caplog.records]
    assert not any(
        "unpersisted row(s) were not written" in message for message in messages
    ), "the drain reported rows lost that it wrote"
    assert any(
        f"tightened another holder's persistent line to {mode}" in message for message in messages
    ), "the save did not report the tightening"

    # The live replacement's own next save folds the tightened line back in: the
    # ratchet holds, the store stays off the line, and every row is still there.
    replacement.append("user", "REPLACEMENT-2")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the replacement's full save loosened the line"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["TAIL-1", "TAIL-2", "REPLACEMENT-1", "REPLACEMENT-2"]


@pytest.mark.asyncio
async def test_tool_result_rows_tighten_a_same_transcript_replacement(
    tmp_path, monkeypatch
) -> None:
    """The app-render rows-only writer protects a replacement before its write."""
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("tool", "PRIVATE TOOL RESULT")
    original.drain()
    state._slots.pop(NAME)

    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)
    assert replacement.memory_mode == "persistent"
    assert execution_context.read_session_execution(HKEY).memory_mode == "persistent"

    assert await chat_runner._persist_tool_result_rows(state, original)

    meta = state.conversation_log.get_metadata(HKEY)
    assert (
        replacement.memory_mode == "temporary"
    ), "the live replacement stayed looser while private tool rows reached disk"
    assert HKEY in state._restricted_keys, "the replacement's key marker was not re-derived"
    assert meta.get("memory_mode") == "temporary", "the tool rows landed under a looser line"
    assert meta[execution_context.EXECUTION_CONTEXT_KEY]["memory_mode"] == "temporary"
    assert execution_context.read_session_execution(HKEY).memory_mode == "temporary"
    runner_source = inspect.getsource(chat_runner._run_chat)
    assert (
        "persist_rows=lambda: _persist_tool_result_rows(state, slot)" in runner_source
    ), "the app-render path bypasses the tested rows-only privacy wrapper"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_delete_handover_of_a_restricted_tail_lands_under_the_ratcheted_line(
    tmp_path, mode
) -> None:
    """A restricted original's committed line ratchets the replacement, so its tail lands.

    The persistent replacement publishes over a line the restricted original
    committed; the fold keeps that line's mode, so the drain finds no looser line
    to refuse and the tail is written under the mode the rows were spoken in.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PERSISTED-1")
    original.append("assistant", "PERSISTED-2")
    original.drain()
    assert await handlers.save_slot_off_loop(state, original, best_effort=False)
    original.append("user", "TAIL-3")
    original.append("assistant", "TAIL-4")
    original.drain()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode
    release.set()
    resp = await close

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the replacement's publish loosened the line"
    assert not meta.get("memory_store")
    assert meta.get("title") == "REPLACEMENT TITLE"
    assert _disk_contents(state) == ["PERSISTED-1", "PERSISTED-2", "TAIL-3", "TAIL-4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["close", "tool"])
@pytest.mark.parametrize("failure", ["declined", "raised"])
async def test_failed_handover_restores_replacement_when_no_private_rows_landed(
    tmp_path, monkeypatch, caplog, caller, failure
) -> None:
    """A persistent line proves a failed hand-over wrote no private rows."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "persistent"

    async def _failed_save(*_args, **_kwargs):
        if failure == "raised":
            raise OSError("disk wedged")
        return False

    target = handlers if caller == "close" else chat_runner
    monkeypatch.setattr(target, "save_slot_off_loop", _failed_save)
    caplog.set_level(logging.WARNING)

    if caller == "close":
        result = await handlers._persist_handover_tail(state, NAME, original)
        assert result.rows_committed is False
    elif failure == "raised":
        with pytest.raises(OSError, match="disk wedged"):
            await chat_runner._persist_tool_result_rows(state, original)
    else:
        assert await chat_runner._persist_tool_result_rows(state, original) is False

    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "persistent"
    assert (
        replacement.memory_mode == "persistent"
    ), "the failed save left the replacement restricted"
    assert (
        RKEY not in state._restricted_keys
    ), "the failed save left the replacement marked restricted"
    if caller == "close":
        messages = [record.getMessage() for record in caplog.records]
        expected = "could not be written" if failure == "raised" else "were not written"
        assert any(expected in message for message in messages)


@pytest.mark.asyncio
async def test_failed_handover_does_not_undo_a_concurrent_title_fold(tmp_path, monkeypatch) -> None:
    """A title writer ordered after the read leaves its pending mode before unlock."""
    from kiro_crew.dashboard import chat_persistence, chat_utils

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    tightening = chat_utils.tighten_replacement_to_restricted_original(state, NAME, original)
    assert tightening is not None

    log = state.conversation_log
    real_hold = log.derivation_hold

    @contextlib.contextmanager
    def _title_fold_after_read(stems):
        with real_hold(stems):
            yield
        fields = {"title": "CONCURRENT TITLE"}

        def _fold(meta):
            fields["memory_mode"] = execution_context.stricter_memory_mode(
                execution_context.canonical_memory_mode(meta.get("memory_mode")),
                "temporary",
            )
            return True

        assert log.update_metadata_if(
            HKEY,
            fields,
            _fold,
            after_commit_under_lock=lambda: chat_persistence._record_pending_memory_mode(
                replacement, fields["memory_mode"]
            ),
        )

    monkeypatch.setattr(log, "derivation_hold", _title_fold_after_read)

    assert not await chat_utils.restore_replacement_if_handover_did_not_land(
        state, NAME, tightening, HKEY
    )
    assert log.get_metadata(HKEY).get("memory_mode") == "temporary"
    assert log.get_metadata(HKEY).get("title") == "CONCURRENT TITLE"
    assert replacement.memory_mode == "temporary"
    assert RKEY in state._restricted_keys
    assert (
        execution_context.read_live_session_execution(chat_utils.effective_session_key(replacement))
        == tightening.tightened_execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "busy"])
async def test_failed_handover_keeps_replacement_restricted_when_line_unverifiable(
    tmp_path, monkeypatch, failure
) -> None:
    """An unreadable line or unavailable transcript lock cannot authorize rollback."""
    from kiro_crew.dashboard import chat_utils

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    tightening = chat_utils.tighten_replacement_to_restricted_original(state, NAME, original)
    assert tightening is not None

    if failure == "unreadable":
        monkeypatch.setattr(state.conversation_log, "get_metadata_status", lambda _key: ({}, False))
    else:

        @contextlib.contextmanager
        def _busy(_stems):
            raise history_mod.TranscriptBusy("held by writer")
            yield

        monkeypatch.setattr(state.conversation_log, "derivation_hold", _busy)

    assert not await chat_utils.restore_replacement_if_handover_did_not_land(
        state, NAME, tightening, HKEY
    )
    assert replacement.memory_mode == "temporary"
    assert RKEY in state._restricted_keys
    assert (
        execution_context.read_live_session_execution(chat_utils.effective_session_key(replacement))
        == tightening.tightened_execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["close", "tool"])
async def test_failed_handover_keeps_replacement_restricted_when_atomic_write_landed(
    tmp_path, monkeypatch, caller
) -> None:
    """A tightened line proves the atomic rows-and-metadata replacement landed."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")

    target = handlers if caller == "close" else chat_runner
    real_save = target.save_slot_off_loop

    async def _land_then_decline(*args, **kwargs):
        assert await real_save(*args, **kwargs)
        return False

    monkeypatch.setattr(target, "save_slot_off_loop", _land_then_decline)

    if caller == "close":
        result = await handlers._persist_handover_tail(state, NAME, original)
        assert result.rows_committed is False
    else:
        assert await chat_runner._persist_tool_result_rows(state, original) is False

    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "temporary"
    assert replacement.memory_mode == "temporary", "the landed rows lost their live restriction"
    assert RKEY in state._restricted_keys, "the landed rows lost their restricted marker"


def test_tightening_retries_carrier_cas_before_mutating_replacement(tmp_path, monkeypatch) -> None:
    """One carrier rebind is retried before the slot and marker become restricted."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    state._restricted_keys.discard(RKEY)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "initial-template",
    )
    rebound = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "rebound-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    real_tighten = chat_utils_mod.tighten_live_session_execution
    calls = 0

    def _rebind_once(session_key, memory_mode, *, expected=...):
        nonlocal calls
        calls += 1
        if calls == 1:
            with execution_context._EXECUTION_LOCK:
                execution_context._LIVE_EXECUTIONS[execution_context._live_key(session_key)] = (
                    rebound
                )
        return real_tighten(session_key, memory_mode, expected=expected)

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _rebind_once)
    try:
        tightening = chat_utils_mod.tighten_replacement_to_restricted_original(
            state, NAME, original
        )

        assert calls == 2, "the carrier conflict was not retried exactly once"
        assert tightening is not None
        assert tightening.previous_execution == rebound, "the retry kept the stale carrier witness"
        assert replacement.memory_mode == "temporary"
        assert RKEY in state._restricted_keys
        tightened = execution_context.read_live_session_execution(skey)
        assert tightened is not None
        assert tightened.memory_mode == "temporary"
        assert tightened.template_id == rebound.template_id, "the retry replaced carrier identity"
    finally:
        execution_context.clear_session_execution(skey)


def test_second_carrier_cas_failure_leaves_replacement_unmodified(tmp_path, monkeypatch) -> None:
    """Two carrier rebinds propagate without partially tightening slot-owned state."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    state._restricted_keys.discard(RKEY)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "initial-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    attempted_expected = []

    def _always_fail(_session_key, _memory_mode, *, expected=...):
        attempted_expected.append(expected)
        raise UnknownMemoryStore(
            "Execution memory is unavailable: session changed during privacy tightening; "
            "Global was not used"
        )

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _always_fail)
    try:
        with pytest.raises(UnknownMemoryStore, match="session changed during privacy tightening"):
            chat_utils_mod.tighten_replacement_to_restricted_original(state, NAME, original)

        assert attempted_expected == [initial, initial], "the carrier conflict was not retried once"
        assert (
            replacement.memory_mode == "persistent"
        ), "the failed CAS partially tightened the slot"
        assert RKEY not in state._restricted_keys, "the failed CAS partially tightened the marker"
        assert execution_context.read_live_session_execution(skey) == initial
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
async def test_delete_handover_saves_tail_after_second_carrier_cas_failure(
    tmp_path, monkeypatch
) -> None:
    """A CAS refusal cannot skip the rows-only save that owns the popped tail."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "replacement-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    calls = 0

    def _always_fail(_session_key, _memory_mode, *, expected=...):
        nonlocal calls
        calls += 1
        raise UnknownMemoryStore(
            "Execution memory is unavailable: session changed during privacy tightening; "
            "Global was not used"
        )

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _always_fail)
    try:
        release.set()
        resp = await close

        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        assert calls == 2
        assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]
        assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "temporary"
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
async def test_tightening_a_live_replacement_keeps_and_rolls_back_its_carrier(tmp_path):
    """The hand-over tightening must not evict a live member-bound carrier.

    Clearing the replacement's live execution would leave the rest of its turn
    with no execution to check the store binding against (the check skips on
    ``None`` -- the fail-OPEN direction for a private silo) and the next turn's
    fold with nothing to fold into. The carrier is tightened in place instead:
    same identity, stricter mode, and a failed write restores that generation.
    """
    from kiro_crew.dashboard.chat_utils import (
        effective_session_key,
        restore_replacement_if_handover_did_not_land,
        slot_history_key,
        tighten_replacement_to_restricted_original,
    )

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)  # popped as a close does, still referenced here
    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    skey = effective_session_key(replacement)
    live = execution_context.ExecutionContext(
        "id-alice",
        execution_context.MemoryStoreRef("member-alice", "id-alice"),
        "member",
        "shared-template",
        "incognito",
    )
    # A live carrier for the replacement (as a restricted member binding leaves).
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = live
    try:
        # Model the original being stricter than the live carrier says.
        original.memory_mode = "temporary"
        tightening = tighten_replacement_to_restricted_original(state, NAME, original)

        assert replacement.memory_mode == "temporary"
        after = execution_context.read_live_session_execution(skey)
        assert after is not None, "the live carrier was cleared instead of tightened"
        assert after.memory_mode == "temporary"
        assert (after.member_id, after.store) == (live.member_id, live.store), "the identity moved"

        assert await restore_replacement_if_handover_did_not_land(
            state, NAME, tightening, slot_history_key(original)
        )
        assert replacement.memory_mode == "persistent"
        assert RKEY not in state._restricted_keys
        assert execution_context.read_live_session_execution(skey) == live
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_save_over_an_unreadable_line_is_deferred_not_folded_as_persistent(
    tmp_path, monkeypatch, mode
) -> None:
    """The ratchet must not fold a line it could not read.

    A restricted original's committed line, then a persistent recreate on the
    freed key whose first save meets a transient read failure on that line
    (``get_metadata_status`` -> ``({}, False)``). An empty dict folds as
    ``persistent``, so without the guard this save would relabel the restricted
    file persistent and stamp a store name on it -- the one write the ratchet
    exists to prevent. The save must fail CLOSED like the identity check does:
    raise, leaving ``_dirty`` armed so the flush retries once the read clears.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PRIVATE-1")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode

    replacement = state.get_or_create_slot(NAME)
    replacement.memory_store = "coding"
    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-2")
    replacement.drain()

    log = state.conversation_log
    real_status = type(log).get_metadata_status
    monkeypatch.setattr(
        type(log),
        "get_metadata_status",
        lambda self, key: ({}, False) if key == HKEY else real_status(self, key),
    )
    with pytest.raises(OSError, match="deferred for retry"):
        await handlers.save_slot_off_loop(state, replacement, best_effort=False)

    monkeypatch.setattr(type(log), "get_metadata_status", real_status)
    meta = log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the unreadable read relabeled the line"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["PRIVATE-1"], "rows were written past a refused save"


def _corrupt_first_line(state) -> None:
    """Truncate the transcript's metadata line to bytes that are not JSON.

    The shape a torn write leaves: the line still opens like a metadata line
    (``"_type"`` and all) but never closes, so it is JSON to no parser and will
    not become one on a retry.
    """
    path = state.conversation_log._path(HKEY)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines and lines[0].startswith('{"_type": "metadata"')
    lines[0] = lines[0][: len('{"_type": "metadata", "cre')] + "\n"
    path.write_text("".join(lines), encoding="utf-8")
    # The damage lands from outside this process (a torn write, a disk fault),
    # so the reader's identity cache -- keyed on mtime, which a coarse clock
    # can tie with the save a moment earlier -- must not answer for it here.
    state.conversation_log._invalidate_cache(HKEY)


@pytest.mark.asyncio
async def test_a_save_over_a_corrupt_line_rewrites_it_strictest_and_lands_the_rows(
    tmp_path,
) -> None:
    """A permanently corrupt first line must not wedge every later save.

    Deferring is right for a TRANSIENT read failure, but a first line that is
    not JSON never becomes readable, so a save that kept deferring would never
    persist another row, ``closed`` would never land and the tab would
    resurrect on every restart. The full save rewrites the line instead -- the
    rows after it survive, ``closed`` can follow -- and stamps the STRICTEST
    mode with no store: the line's real contract is unknowable and the ratchet
    forbids relabelling it looser.
    """
    from kiro_crew.execution_context import STRICTEST_MEMORY_MODE
    from kiro_crew.history import METADATA_LINE_CORRUPT, METADATA_LINE_READABLE

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.memory_store = "coding"
    slot.append("user", "ROW-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    identity = log.get_metadata(HKEY)["created_at"]
    assert slot._disk_meta_created_at == identity, "the slot must have observed its line"

    _corrupt_first_line(state)
    assert log.metadata_line_state(HKEY) == METADATA_LINE_CORRUPT
    assert log.get_metadata_status(HKEY) == ({}, False)

    slot.append("assistant", "ROW-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    assert log.metadata_line_state(HKEY) == METADATA_LINE_READABLE
    meta = log.get_metadata(HKEY)
    assert meta.get("_type") == "metadata", "the corrupt line was not rewritten"
    assert meta.get("memory_mode") == STRICTEST_MEMORY_MODE
    assert "memory_store" not in meta, "a restricted line names no store"
    assert meta.get("created_at") == identity, "the full save restores the slot's identity"
    assert _disk_contents(state) == ["ROW-1", "ROW-2"], "the rows did not land"
    assert slot.memory_mode == STRICTEST_MEMORY_MODE, "the live slot must follow the line"

    assert await handlers.save_slot_off_loop(state, slot, closed=True, best_effort=False) is True
    assert log.get_metadata(HKEY).get("closed") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_transient_read_failure_still_defers_the_save(tmp_path, monkeypatch, mode) -> None:
    """Only a CORRUPT line is rewritten; an OSError on the read stays deferred.

    The reader answers ``transient`` after its bounded retries, the save raises,
    ``_dirty`` stays armed and no row is written; once the read clears the next
    attempt lands the rows under the line's own (folded) mode, untouched.
    """
    from kiro_crew import history_projection

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME, memory_mode=mode)
    slot.append("user", "ROW-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    path = log._path(HKEY)

    real_open = open
    failing = {"on": True}

    def _flaky_open(file, *args, **kwargs):
        if failing["on"] and Path(file) == path:
            raise OSError("device not ready")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(history_projection, "open", _flaky_open, raising=False)
    monkeypatch.setattr(history_mod, "_METADATA_READ_RETRY_SECS", 0.0)
    slot.append("assistant", "ROW-2")
    slot.drain()
    slot._dirty = True
    assert await handlers.save_slot_off_loop(state, slot, best_effort=True) is True
    assert slot._dirty is True, "a deferred save must leave the retry armed"
    failing["on"] = False
    assert _disk_contents(state) == ["ROW-1"], "rows were written past a deferred save"
    assert log.get_metadata(HKEY).get("memory_mode") == mode

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    assert _disk_contents(state) == ["ROW-1", "ROW-2"]
    assert log.get_metadata(HKEY).get("memory_mode") == mode, "the retry relabeled the line"


def test_a_corrupt_line_withholds_derivation(tmp_path) -> None:
    """Readers keep failing closed on a corrupt line: no rows are derived from it."""
    from kiro_crew.history import transcript_withholds_derivation

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "ROW-1")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True) is True
    assert transcript_withholds_derivation(log, HKEY) is False

    _corrupt_first_line(state)
    assert transcript_withholds_derivation(log, HKEY) is True
    with pytest.raises(history_mod.TranscriptWithheld):
        log.derive_messages(HKEY)


def test_a_metadata_merge_over_a_corrupt_line_heals_it_strictest(tmp_path, monkeypatch) -> None:
    """``update_metadata_if`` rewrites a corrupt line and defers a transient one.

    The empty-window merge and the title upsert reach the line through it, so a
    corrupt line must not wedge ``closed`` for an empty tab either. The healed
    line keeps the rows, carries the strictest mode whatever the fields say, no
    store, and no minted ``created_at``.
    """
    from kiro_crew import history_projection
    from kiro_crew.execution_context import STRICTEST_MEMORY_MODE

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "ROW-1")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True) is True
    _corrupt_first_line(state)

    seen: list[dict] = []
    applied = log.update_metadata_if(
        HKEY,
        {"closed": True, "memory_mode": "persistent", "memory_store": "coding"},
        lambda meta: seen.append(dict(meta)) or True,
    )
    assert applied is True
    assert seen == [{"memory_mode": STRICTEST_MEMORY_MODE}], "the guard saw the rebuilt line"
    meta = log.get_metadata(HKEY)
    assert meta.get("closed") is True
    assert meta.get("memory_mode") == STRICTEST_MEMORY_MODE, "fields relabeled a healed line"
    assert "memory_store" not in meta
    assert "created_at" not in meta, "a healed line must not mint an identity"
    assert _disk_contents(state) == ["ROW-1"]

    monkeypatch.setattr(
        type(log),
        "_read_metadata_state",
        lambda self, key: ({}, history_projection.METADATA_LINE_TRANSIENT),
    )
    assert log.update_metadata_if(HKEY, {"title": "T"}, lambda meta: True) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_same_key_persistent_recreate_cannot_relabel_a_restricted_line(
    tmp_path, mode
) -> None:
    """The on-disk ``memory_mode`` is a ratchet: a later writer can only tighten it.

    A restricted slot commits rows and closes cleanly. Its file stays, and
    ``get_or_create_slot`` hands the freed key to a persistent slot whose saves
    rebuild the metadata line from their own state. Without the fold, the first
    such save would write ``memory_mode: persistent`` plus a store name over the
    committed private rows, and the consolidator and the MCP history tools --
    which read only that line -- would learn from them. Both writers are driven:
    the empty-window merge a newborn's metadata route takes, and the full save
    that carries the replacement's first row.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert NAME not in state._slots
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]

    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    replacement.memory_store = "coding"
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the empty-window merge relabeled the line"
    assert not meta.get("memory_store"), "the merge named a store on a restricted line"
    assert meta.get("title") == "REPLACEMENT TITLE"

    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-3")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the full save relabeled the line"
    assert "memory_store" not in meta, "the full save named a store on a restricted line"
    assert replacement.memory_mode == mode, "the full save left the live replacement persistent"
    assert HKEY in state._restricted_keys
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2", "REPLACEMENT-3"]


@pytest.mark.asyncio
async def test_mixed_case_restricted_line_survives_persistent_recreate(tmp_path) -> None:
    """Case canonicalisation keeps hand-edited restricted metadata restrictive."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert NAME not in state._slots

    await asyncio.to_thread(
        state.conversation_log.update_metadata,
        HKEY,
        {"memory_mode": "Incognito"},
    )
    line_meta = state.conversation_log.get_metadata(HKEY)
    assert line_meta.get("memory_mode") == "Incognito"
    assert is_incognito_transcript(line_meta.get("memory_mode"))

    replacement = state.get_or_create_slot(NAME)
    replacement.memory_store = "coding"
    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-3")
    replacement.drain()
    assert await handlers.save_slot_off_loop(
        state,
        replacement,
        force=True,
        best_effort=False,
    )

    meta = state.conversation_log.get_metadata(HKEY)
    assert (
        meta.get("memory_mode") == "incognito"
    ), "the mixed-case restricted line was relabeled persistent"
    assert "memory_store" not in meta, "the full save named a store on a restricted line"
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2", "REPLACEMENT-3"]


@pytest.mark.asyncio
async def test_save_canonicalises_a_rehydrated_slot_mode(tmp_path) -> None:
    """A raw mixed-case mode restored onto a slot remains saveable and restricted."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.memory_mode = "Incognito"
    slot.memory_store = "coding"
    slot.append("user", "PRIVATE-1")
    slot.drain()

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "incognito"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["PRIVATE-1"]


@pytest.mark.asyncio
async def test_persistent_turn_binding_after_restricted_recreate_keeps_line_and_rows(
    tmp_path, monkeypatch
) -> None:
    state = _make_state(tmp_path)
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200

    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)

    meta = state.conversation_log.get_metadata(HKEY)
    assert meta["memory_mode"] == "incognito"
    assert "memory_store" not in meta
    assert execution_context.EXECUTION_CONTEXT_KEY not in meta
    live = execution_context.read_live_session_execution(HKEY)
    assert live is not None
    assert live.memory_mode == "incognito"
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]


@pytest.mark.asyncio
async def test_delete_handover_of_a_persistent_tail_keeps_a_restricted_line(tmp_path) -> None:
    """The reverse direction commits: the line's stricter mode stays, rows land."""
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME, memory_mode="incognito")
    assert replacement is not original
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "incognito"
    release.set()
    resp = await close

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "incognito", "the drain loosened the replacement's mode"
    assert _disk_contents(state) == ["PERSISTED-1", "PERSISTED-2", "TAIL-3", "TAIL-4"]


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
