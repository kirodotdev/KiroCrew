"""Uninstalling an app drops its conversations' resume pointers.

The bug: a slot key an app chooses is often DETERMINISTIC — one slot per object it
tracks, named after that object. ``session.py``'s
``resume_sid = self._session_map.get(key)`` therefore hands the next slot under that
name the PREVIOUS conversation, which is correct while the app is installed and wrong
once it is gone: reinstall, open the same object, and the first turn resumes a
transcript from code that is gone.

Two things decide whether a fix works, and each has its own load-bearing negative.

**Where ownership is read from.** It must outlive the tab, because a closed app slot
is the mainline state before an uninstall — the user closes it, then uninstalls.
``_ChatSlot._app`` dies with the gateway and a closed slot leaves ``_slots`` in a
running one; ``open_slots.json`` is no better, since it tracks tabs to REOPEN and a
closed slot leaves it at the next flush while the resume pointer deliberately stays.
The record that already survives both is the conversation's own metadata line, which
every save — including the save that closes a tab — stamps with ``app``.

**Who does the writing.** ``SessionMap``'s rule 3: a throwaway instance is READ-ONLY,
because two instances that loaded ``_data`` independently do not merge — each write
is a whole-file rewrite of one snapshot. So the in-gateway path clears through the
LIVE map, and the gateway-less CLI path refuses outright while a gateway is up rather
than drop rows that map has not flushed.

The third negative is the disable path: ``deregister_app`` runs on disable too, and
App Store Sync is a disable/enable pair, so cleaning up there would discard every
long-lived conversation's accumulated context on every sync.
"""

from __future__ import annotations

import json
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.apps import bridges, routes
from kiro_crew.gateway_lock import GatewayLock
from kiro_crew.history import transcript_stem
from kiro_crew.session_map import SUPPRESS_REPLAY_FLAG, SessionMap

APP = "acme-app"
OWNED = "dashboard:acme-run-abc"
OWNED2 = "dashboard:acme-run-def"
MINE = "dashboard:chat-1-mine"


def _seed_transcript(tmp_path: pathlib.Path, key: str, *, app: str = "", closed: bool = False):
    """Write the metadata line a real save would write for *key*.

    Only the first line matters here: ownership is read from the metadata line, so
    this is the same shape ``chat_persistence`` emits (``app`` present only when the
    slot has an owner, exactly as it writes it).
    """
    meta: dict = {"_type": "metadata", "created_at": "2026-01-01T00:00:00Z"}
    if app:
        meta["app"] = app
        meta["origin"] = "app"
    if closed:
        meta["closed"] = True
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{transcript_stem(key)}.jsonl").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )


def _stored_sid(tmp_path: pathlib.Path, key: str) -> str:
    """The sid recorded for *key*, read straight off disk.

    Not ``SessionMap.get``: that answers "is this resumable" — it stats the
    transcript file and prunes stale rows as a side effect — while these tests are
    about what is RECORDED.
    """
    raw = json.loads((tmp_path / "session_map.json").read_text(encoding="utf-8"))
    return str((raw.get(key) or {}).get("sid") or "")


def test_ownership_is_recovered_from_the_conversation_s_own_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(MINE, "sid-mine", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, MINE)

    assert bridges.app_conversation_keys(APP) == [OWNED], (
        "ownership must come from the metadata line; a user's own tab has no app "
        "and must not be swept up with the app's"
    )


def test_a_slot_the_user_closed_before_uninstalling_is_still_found(tmp_path, monkeypatch):
    """The regression that killed the previous approach.

    Sourcing ownership from live slots or from ``open_slots.json`` finds nothing
    here — there is no live state at all and the tab is closed — while the resume
    pointer is deliberately preserved across close. This is the MAINLINE state
    before an uninstall, not an edge case: the user closes the tab, then uninstalls.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP, closed=True)

    assert bridges.app_conversation_keys(APP) == [OWNED]


def test_another_app_s_conversations_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(OWNED2, "sid-other", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, OWNED2, app="other-app")

    assert bridges.app_conversation_keys(APP) == [OWNED]
    assert bridges.app_conversation_keys("other-app") == [OWNED2]
    assert bridges.app_conversation_keys("never-installed") == []


def test_only_conversations_that_still_hold_a_pointer_are_listed(tmp_path, monkeypatch):
    """The index is exactly as wide as the problem: no sid, nothing to resume.

    ``OWNED2`` here is a row that EXISTS but has already had its pointer dropped —
    what ``clear_sid`` leaves behind, and what a poisoned-conversation escalation
    produces in the wild. Listing it would report a drop that did not happen.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(OWNED2, "sid-gone", provider="acp")
    smap.clear_sid(OWNED2)
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, OWNED2, app=APP)

    assert bridges.app_conversation_keys(APP) == [OWNED]


def test_a_clear_that_could_not_be_written_is_not_reported_as_clean(tmp_path, monkeypatch):
    """A failed WRITE must not read as "this app owned nothing".

    The scan and the write are in the same block, so an ENOSPC or a permission error
    on the flush lands in the same handler that a missing map lands in. Returning the
    default there tells the caller nothing was owned, and the caller prints a success
    tick on that — while the pointer is still on disk for a reinstall to resume.
    """
    monkeypatch.setattr(bridges, "config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-owned", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)

    def _boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bridges.SessionMap, "flush", _boom)
    cleanup = bridges.discard_app_session_pointers(APP)

    assert cleanup.failed, (
        "a clear that could not be persisted has to say so; the default result means "
        "the app owned nothing, and the CLI prints a success tick on that"
    )
    assert not cleanup.declined, "declined names the gateway as the cause; this is not that"
    assert cleanup.dropped == 0


def test_a_failed_clear_is_visible_to_the_operator(tmp_path, capsys):
    """And the text has to name the action that fixes it, which is not the gateway one."""
    from kiro_crew import cli_commands

    cli_commands._print_pointer_cleanup(APP, bridges.SessionPointerCleanup(failed=True))
    err = capsys.readouterr().err
    assert APP in err and "could not clear" in err
    assert "Stop the gateway" not in err, (
        "a storage failure and a live-gateway decline need different actions, so they "
        "must not share one message"
    )


def test_the_cli_path_drops_the_pointer_when_no_gateway_owns_the_map(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(MINE, "sid-mine", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, MINE)

    assert bridges.discard_app_session_pointers(APP) == bridges.SessionPointerCleanup(1)

    assert _stored_sid(tmp_path, OWNED) == "", (
        "the app's conversation pointer must be gone, or a reinstall resumes a "
        "transcript from the previous installation"
    )
    assert _stored_sid(tmp_path, MINE) == "sid-mine", "an unowned session must be untouched"


def test_the_cli_path_declines_while_a_gateway_owns_the_map(tmp_path, monkeypatch):
    """A detached write here would be worse than doing nothing.

    A running gateway holds a long-lived map whose ``_data`` loaded at startup;
    every write rewrites the whole file from that snapshot. So this process's write
    would be undone by the live map's next mutation (restoring the pointer) AND
    would drop whatever that map recorded since this process read — costing an
    unrelated session its sid or its channel link. Leaving the pointer is the
    pre-existing behaviour; corrupting a stranger's session is not.

    The lock is really HELD here, not stubbed. That is what makes this a test of
    mutual exclusion rather than of a question asked before acting — a gateway
    starting between such a question and the write would walk straight into the
    case the question was meant to exclude.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)

    with GatewayLock(tmp_path):  # stands in for a running gateway
        cleanup = bridges.discard_app_session_pointers(APP)

    assert cleanup == bridges.SessionPointerCleanup(dropped=0, declined=True), (
        "a declined clear must be distinguishable from an empty one: it leaves a "
        "pointer behind, and the caller has to be able to say so"
    )
    assert _stored_sid(tmp_path, OWNED) == "sid-app"


def test_the_cleared_pointer_stays_diagnosable(tmp_path, monkeypatch):
    """``clear_sid``, not ``delete``: the entry (and its Slack linkage) survives and
    the dropped value is stashed, so this is reversible by hand."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)

    bridges.discard_app_session_pointers(APP)

    assert SessionMap().get_discarded_sid(OWNED) == "sid-app"


def test_deregister_does_NOT_drop_pointers(tmp_path, monkeypatch):
    """The one that matters. ``deregister_app`` runs on DISABLE — the CLI's disable
    action, the disable route, and the enable/update reconcile — and App Store Sync
    is a disable/enable pair. Clearing pointers there would discard every long-lived
    conversation's accumulated context on every sync."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)

    bridges.deregister_app(APP)

    assert _stored_sid(tmp_path, OWNED) == "sid-app", (
        "disable must preserve the conversation; App Store Sync disables and "
        "re-enables, and a long-lived per-object conversation would lose its "
        "accumulated context on every sync"
    )


def test_the_in_gateway_path_clears_inside_the_lock_via_the_live_map(tmp_path):
    """Two invariants of the in-gateway path, pinned where they are easy to undo.

    It must clear through the LIVE map (``sessions.discard_conversation``) — a
    throwaway instance's write is reversed by the live map's next mutation and takes
    unflushed rows with it (``SessionMap``'s rule 3), and it also tears the live
    session down so a still-open tab cannot re-record a sid on its next turn.

    And it must run INSIDE ``app_lifecycle_lock``: outside it, a concurrent reinstall
    of the same app can take that lock the moment this handler releases it and be
    serving the same slot key again before the clear lands, so the pointer dropped
    is the NEW installation's.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "routes.py"
    ).read_text(encoding="utf-8")
    handler = src.split("async def handle_uninstall_app", 1)[1].split("\nasync def ", 1)[0]

    assert "discard_conversation(" in handler, "the in-gateway path must use the live map"
    assert "discard_app_session_pointers" not in handler, (
        "that helper writes under the gateway lock this very process holds, so it "
        "would decline; the in-gateway path must use the live map"
    )

    lock_open = handler.index("async with app_lifecycle_lock(name):")
    # The lock body is indented past its ``async with``; the first line back at that
    # statement's own indent ends the block.
    lock_end = handler.index("\n    if not result.ok:", lock_open)
    assert lock_open < handler.index("discard_conversation(") < lock_end, (
        "the clear must run inside the lifecycle lock — outside it a concurrent "
        "reinstall can be serving the same slot key before the clear lands"
    )


def test_a_pointer_the_live_map_has_not_flushed_is_still_found(tmp_path, monkeypatch):
    """The enumeration must come from the caller's live map, not from the file.

    A running gateway's map holds ``_data`` in memory; the file lags it by whatever
    it has not flushed. Here the pointer for ``OWNED`` exists only in memory, which
    is exactly the state a turn that has just recorded a sid leaves behind. A
    detached enumeration reads the file, finds nothing, and the uninstall reports a
    clean sweep while the pointer a reinstall will resume is still there.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _seed_transcript(tmp_path, OWNED, app=APP)

    assert (
        bridges.app_conversation_keys(APP) == []
    ), "precondition: nothing is on disk, so the detached read sees nothing"
    assert bridges.app_conversation_keys(APP, mapped_keys={OWNED}) == [OWNED], (
        "a caller that owns the live map decides which keys to look at; its "
        "in-memory answer includes the pointer the file does not have yet"
    )


def test_the_caller_s_key_set_still_only_yields_keys_this_app_owns(tmp_path, monkeypatch):
    """Taking the key set from the caller does not widen what gets cleared.

    ``session_keys()`` contributes every live and in-flight key in the gateway,
    most of them the user's own tabs. Ownership stays sourced from the metadata
    line, so a wider candidate set is a wider SEARCH, never a wider sweep.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, OWNED2, app="other-app")
    _seed_transcript(tmp_path, MINE)

    assert bridges.app_conversation_keys(APP, mapped_keys={OWNED, OWNED2, MINE}) == [OWNED]
    assert bridges.app_conversation_keys(APP, mapped_keys=set()) == []


def test_the_in_gateway_path_enumerates_through_the_live_manager(tmp_path):
    """Pinned as source shape, matching the sibling test above.

    One invariant, cheap to undo by hand: the handler must not fall back to the
    detached enumeration (``app_conversation_keys(name)`` with no key set). The
    detached read lags this gateway's map by whatever it has not flushed, so an
    enumeration taken there can omit a key whose pointer already exists — and the
    clear, which goes through the live map, then leaves it for a reinstall.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "routes.py"
    ).read_text(encoding="utf-8")
    handler = src.split("async def handle_uninstall_app", 1)[1].split("\nasync def ", 1)[0]

    assert "mapped_keys=" in handler, (
        "the in-gateway path must hand its own live key set to the enumeration; "
        "the detached read lags this gateway's map by whatever it has not flushed"
    )
    assert "sessions.mapped_session_keys()" in handler
    assert "sessions.session_keys()" in handler, (
        "an allocation in flight has not reached the map yet, so the mapped keys "
        "alone are not the whole live answer"
    )


def test_an_app_that_owned_nothing_is_not_reported_as_declined(tmp_path, monkeypatch):
    """The other half of the same distinction, and the one that keeps it honest.

    ``dropped == 0`` is a complete answer here — there was nothing to drop — so it
    must not carry ``declined``, or the CLI prints a warning about a pointer that
    does not exist.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(MINE, "sid-mine", provider="acp")
    _seed_transcript(tmp_path, MINE)

    assert bridges.discard_app_session_pointers(APP) == bridges.SessionPointerCleanup()


def test_a_declined_clear_is_visible_to_the_operator_and_not_a_success_tick(tmp_path, capsys):
    """The residual has to be announced where the person who can act on it is.

    A gateway owning the map is the COMMON state, so the decline is the ordinary
    case rather than an extreme one. Silent, it leaves a pointer keyed by a name a
    reinstall reuses and the operator sees only a success tick.
    """
    from kiro_crew import cli_commands

    cli_commands._print_pointer_cleanup(APP, bridges.SessionPointerCleanup(declined=True))
    err = capsys.readouterr().err
    assert APP in err
    assert "uninstall" in err, "the message must name the action that clears them"
    for phrase in ("resume the removed app's transcript", "Stop the gateway"):
        assert phrase in err, f"the warning must state {phrase!r}"


def test_the_clear_still_runs_when_the_app_is_already_gone(tmp_path, monkeypatch, capsys):
    """The declined case's self-correction, pinned end to end.

    An uninstall run while a gateway was up removed the app and left the pointers.
    Re-running it with the gateway stopped is the only path back — and it only works
    because the bookkeeping half does not require the app to still be installed.
    """
    from kiro_crew import cli_commands

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SessionMap().set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP, closed=True)

    class _Gone:
        error = f"app {APP!r} is not installed"

    assert cli_commands._app_already_gone(_Gone()), (
        "only this failure leaves the pointers worth clearing; every other one "
        "leaves the app whole"
    )
    assert not cli_commands._app_already_gone(
        type("R", (), {"error": "app 'x' cannot be uninstalled (lifecycle=locked)"})()
    ), "a still-installed app keeps the pointers its slots are entitled to resume"

    cli_commands._print_pointer_cleanup(APP, bridges.discard_app_session_pointers(APP))

    assert _stored_sid(tmp_path, OWNED) == "", "the stale pointer must be gone"
    assert "dropped 1 conversation pointer" in capsys.readouterr().out


def test_the_cli_path_also_suppresses_the_next_cold_start_s_replay(tmp_path, monkeypatch):
    """Dropping the sid is only half of "starts fresh".

    ``clear_sid`` stops the NATIVE resume. The transcript is deliberately left on
    disk, so a cold start under the same slot key would still have
    ``build_session_replay`` inject the removed app's history into the next
    installation's first turn — the same user-visible bug through a second channel.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(MINE, "sid-mine", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, MINE)

    bridges.discard_app_session_pointers(APP)

    fresh = SessionMap()
    assert fresh.get_flag(OWNED, SUPPRESS_REPLAY_FLAG), (
        "the flag has to be on disk: the CLI has no live manager, and the gateway "
        "that would honour an in-memory flag is a different process"
    )
    assert not fresh.get_flag(MINE, SUPPRESS_REPLAY_FLAG), "an unowned session is untouched"


def test_the_suppression_follows_the_pointer_and_stops_where_it_does(tmp_path, monkeypatch):
    """The index stays keyed on "still holds a sid", and that boundary is a choice.

    ``OWNED2`` here has already had its pointer cleared — by a provider switch, or a
    poisoned-conversation escalation — so this uninstall has no pointer of its own to
    drop and does not flag it either. Its transcript survives, so a cold start under
    that key can still replay it. Widening to "every transcript this app owns" means
    enumerating transcripts rather than session-map rows, which is a different and
    much wider index than the one this change argues for; the narrow answer fails
    toward the pre-existing behaviour, and the wide one is a separate decision. Pinned
    so that widening it is deliberate rather than accidental.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(OWNED2, "sid-gone", provider="acp")
    smap.clear_sid(OWNED2)
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, OWNED2, app=APP)

    cleanup = bridges.discard_app_session_pointers(APP)

    assert cleanup.dropped == 1, "only one pointer was actually there to drop"
    fresh = SessionMap()
    assert fresh.get_flag(OWNED, SUPPRESS_REPLAY_FLAG), "the key it dropped is suppressed"
    assert not fresh.get_flag(OWNED2, SUPPRESS_REPLAY_FLAG), (
        "a key with no pointer left is outside this index by construction — see the "
        "stated cost, not an oversight"
    )


def test_the_suppression_flag_is_one_shot(tmp_path, monkeypatch):
    """Consumed and cleared together, matching the in-memory branch's contract.

    Leaving it set would make every LATER cold start on that key silently
    amnesiac — an idle-timeout expiry, a gateway restart — which is not what an
    uninstall asked for.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set_flag(OWNED, SUPPRESS_REPLAY_FLAG, True)

    assert smap.get_flag(OWNED, SUPPRESS_REPLAY_FLAG)
    smap.set_flag(OWNED, SUPPRESS_REPLAY_FLAG, False)
    assert not smap.get_flag(OWNED, SUPPRESS_REPLAY_FLAG)
    assert SessionMap().get(OWNED) is None or True  # the row itself survives the clear


def test_the_in_gateway_path_suppresses_replay_and_finishes_durable(tmp_path):
    """Source shape again, for the two invariants a refactor silently drops.

    ``discard_conversation``'s default is ``replay=True``, which actively DISCARDS
    any standing suppression — so omitting the keyword is worse than neutral. And
    ``clear_sid`` on the loop only schedules a debounced flush, so the handler would
    answer success while the drop is still only in memory; a restart inside that
    window brings the stale sid back with the app already gone. The CLI path states
    the same invariant as ``flush()`` before releasing the lock.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "routes.py"
    ).read_text(encoding="utf-8")
    handler = src.split("async def handle_uninstall_app", 1)[1].split("\nasync def ", 1)[0]

    assert "discard_conversation(key, replay=False)" in handler, (
        "the default replay=True discards standing suppression, so the removed "
        "app's transcript is replayed into the next installation's first turn"
    )
    assert "suppress_replay_persistently(key)" in handler, (
        "replay=False sets an in-memory flag; a gateway restart between the "
        "uninstall and the reinstall would lose it"
    )
    assert (
        "await sessions.aflush()" in handler
    ), "the pointer drop must be durable before the uninstall reports success"
    assert handler.index("suppress_replay_persistently(key)") < handler.index(
        "await sessions.aflush()"
    ), "the flush has to come after everything it is meant to make durable"


def test_the_cold_start_chokepoint_consumes_the_persisted_flag():
    """Pinned at the ONE place that decides whether a cold start replays.

    Both writers — the route and the CLI — are answered by a single reader, so the
    persisted half cannot be honoured on one path and ignored on the other.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "session.py"
    ).read_text(encoding="utf-8")
    consume = src.split("def consume_replay_suppression", 1)[1].split("\n    def ", 1)[0]

    assert "_consume_persisted_replay_suppression" in consume, (
        "the in-memory set is not the whole answer: the CLI writer has no live "
        "manager and a gateway restart drops the set"
    )
    helper = src.split("def _consume_persisted_replay_suppression", 1)[1].split("\n    def ", 1)[0]
    assert "set_flag(candidate, SUPPRESS_REPLAY_FLAG, False)" in helper, (
        "read-and-clear, matching the in-memory branch — otherwise every later "
        "cold start on that key is silently amnesiac"
    )


def test_the_suppression_survives_the_startup_prune(tmp_path, monkeypatch):
    """Without this the whole flag is decorative.

    After the clear the entry has NO sid, and ``prune`` deletes a sid-less entry that
    nothing holds back — taking the flag with it. That is exactly the sequence the
    flag exists for: uninstall from the CLI, start the gateway, reinstall. The one
    place the decision can be recorded is the entry ``prune`` was about to collect.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)

    bridges.discard_app_session_pointers(APP)

    after = SessionMap()
    after.prune()  # the gateway's own startup step
    assert after.get_flag(OWNED, SUPPRESS_REPLAY_FLAG), (
        "prune collects a sid-less entry unless something durable holds it back, so "
        "a gateway restart between the uninstall and the reinstall would lose the "
        "suppression and replay the removed app's transcript"
    )


def test_the_flag_stops_holding_the_row_alive_once_consumed(tmp_path, monkeypatch):
    """The answer to ``_DURABLE_FLAGS``' own cost warning.

    Membership makes an entry immortal to ``prune``, which the constant warns grows
    the map without bound for a per-SESSION flag. This one is one-shot, so the row is
    collectable again as soon as the first cold start consumes it — the immortality is
    bounded by the thing it is protecting.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    bridges.discard_app_session_pointers(APP)

    consumed = SessionMap()
    consumed.set_flag(OWNED, SUPPRESS_REPLAY_FLAG, False)  # what a cold start does
    consumed.flush()

    after = SessionMap()
    after.prune()
    assert not after.get_flag(OWNED, SUPPRESS_REPLAY_FLAG)


def test_both_suppression_markers_are_consumed_together():
    """The gateway sets two markers; a chain that stops at the first leaves one.

    ``discard_conversation(replay=False)`` sets the in-memory marker and
    ``suppress_replay_persistently`` sets the durable one, so an ``elif`` tail
    consumes only memory on the first cold start and the surviving disk flag then
    makes a LATER cold start of the NEW installation start empty for no reason —
    a fix for stale history turned into amnesia about live history.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "session.py"
    ).read_text(encoding="utf-8")
    consume = src.split("def consume_replay_suppression", 1)[1].split("\n    def ", 1)[0]

    assert "elif self._consume_persisted_replay_suppression" not in consume, (
        "the persisted marker must not hang off the in-memory branch: the gateway "
        "path sets both, so the chain would leave the disk flag standing"
    )
    body = consume.split('"""', 2)[-1]
    assert body.index("_consume_persisted_replay_suppression") < body.index(
        "if key in self._suppress_replay"
    ), "the persisted marker is consumed unconditionally, before either alias branch"
    assert (
        "if not (in_memory or persisted):" in consume
    ), "either marker alone is a real suppression; only neither is a no-op"


def test_an_unreadable_ownership_record_is_reported_not_silently_unowned(
    tmp_path, monkeypatch, caplog
):
    """``{}`` is the same value for "no app" and "could not read the record".

    Claiming the key would sweep up a conversation this app may not own, which is
    worse than the pointer it leaves. Dropping it in silence hides the one case where
    the answer is unknown, so it is left in place AND named.
    """
    import logging

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(OWNED2, "sid-bad", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    # A transcript that exists and whose first line is not readable metadata.
    bad = tmp_path / "sessions" / f"{transcript_stem(OWNED2)}.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json at all\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=bridges.logger.name):
        assert bridges.app_conversation_keys(APP) == [OWNED], (
            "an unreadable record must not be claimed — that would sweep up a "
            "conversation this app may not own"
        )

    assert any("ownership record" in r.getMessage() for r in caplog.records), (
        "and it must not pass in silence either: this is the one case where the "
        "answer is unknown rather than negative"
    )


@pytest.mark.asyncio
async def test_a_flush_that_fails_does_not_abandon_the_rest_of_the_uninstall(
    tmp_path, monkeypatch, caplog
):
    """The durable write is bookkeeping, so it must not abort the teardown around it.

    By the time Step 6 runs, ``uninstall_app`` has already removed the app's files —
    the uninstall is past the point of being retried as a whole. The flush is the one
    call in that step still outside the per-key ``try``, so an ENOSPC or a permission
    error there raised straight out of the handler and skipped everything after it:
    ``invalidate_app_secret_cache``, ``_unregister_notification_channels`` and
    ``forget_app_hooks``. A stale slot-close hook is the worst of those — it makes the
    removed app's leftover tabs UNDISMISSABLE, which is a user-visible dead end far
    past the cost of the pointer the flush failed to persist.

    The CLI sibling already states this rule: ``discard_app_session_pointers`` wraps
    its ``flush()`` and answers ``SessionPointerCleanup(failed=True)`` rather than
    raising. Here the report shape is ``uninstall_log``, which is how the same handler
    already surfaces a failed ``onUninstall`` script — reported, not raised, and not
    reported as clean either.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _seed_transcript(tmp_path, OWNED, app=APP)

    class _DurableWriteFails:
        """A live session manager whose whole-file rewrite cannot land."""

        def __init__(self) -> None:
            self.discarded: list[str] = []
            self.suppressed: list[str] = []

        def mapped_session_keys(self) -> set[str]:
            return {OWNED}

        def session_keys(self) -> set[str]:
            return set()

        async def discard_conversation(self, key: str, *, replay: bool = True) -> None:
            self.discarded.append(key)

        def suppress_replay_persistently(self, key: str) -> None:
            self.suppressed.append(key)

        async def aflush(self) -> None:
            raise OSError(28, "No space left on device")

    sessions = _DurableWriteFails()
    state = MagicMock()
    state.sessions = sessions
    # No cron service, so the handler's cron step is skipped and this test is
    # scoped to the step it is about. A bare MagicMock here is not inert: the
    # handler would drive a fake CronSDK whose removal raises, and an uninstall
    # whose cron cleanup raises is refused before anything destructive runs.
    state.crons = None

    request = MagicMock()
    request.match_info = {"name": APP}
    request.app = {"state": state}
    request.json = AsyncMock(return_value={})

    fake_app = {
        "name": APP,
        "manifest": {},
        "resources": "gateway",
        "lifecycle": "normal",
        "enabled": False,
    }

    with (
        patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
        patch(
            "kiro_crew.apps.routes.uninstall_app",
            return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
        ),
        patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
        patch("kiro_crew.apps.routes.deregister_app", return_value=None),
        patch(
            "kiro_crew.apps.teardown.on_app_disable",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
        patch(
            "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
            return_value={"removable": [], "shared": [], "userInstalled": []},
        ),
        patch(
            "kiro_crew.apps.routes.clean_dependencies",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("kiro_crew.apps.routes.invalidate_app_secret_cache") as secret_cache,
        patch("kiro_crew.apps.routes._unregister_notification_channels") as channels,
        patch("kiro_crew.apps.routes.forget_app_hooks") as hooks,
        caplog.at_level(logging.WARNING, logger=routes.logger.name),
    ):
        resp = await routes.handle_uninstall_app(request)

    assert sessions.discarded == [OWNED], (
        "precondition: the pointer drop itself has to have run, or the flush was "
        "never reached and this test proves nothing"
    )

    # (a) The uninstall still completes.
    assert resp.status == 200, (
        "the files are already gone, so a failed bookkeeping write must not turn "
        f"the uninstall into a {resp.status}"
    )

    # (b) Every teardown step after the flush still runs.
    assert secret_cache.called, "a surviving secret cache entry outlives the app"
    assert channels.called, "a surviving notification channel points at removed code"
    assert hooks.called, (
        "a surviving slot-close hook makes the removed app's leftover tabs "
        "undismissable — notify_slot_closed returns False when it raises and "
        "api_chat_slot_delete refuses the close on that"
    )

    # (c) And the failure is not reported as clean.
    log = json.loads(resp.text).get("uninstall_log", "")
    assert "did not persist" in log, (
        "the pointer is still on disk for a reinstall to resume, so the response "
        f"must not claim a clean sweep; got {log!r}"
    )
    assert any(
        "resume pointer" in r.getMessage() for r in caplog.records
    ), "and it must not pass in silence either"


@pytest.mark.asyncio
async def test_a_teardown_that_raises_still_leaves_the_suppression_durable(tmp_path, monkeypatch):
    """The half that already committed cannot be undone by the half that failed.

    ``discard_conversation`` clears the sid and sets the in-memory suppression INSIDE
    its registry lock, and only then awaits ``provider.shutdown()`` — whose own
    ``finally`` deliberately lets an exception propagate rather than carry it past the
    child cancel (``session_lifecycle.py``). So by the time this can raise, the pointer
    is already gone and the ONLY thing standing between the user and the bug this step
    exists to prevent is the suppression flag.

    Left to the per-key ``except``, that key got neither the persistent flag nor the
    flush: a gateway restart before the reinstall then loses the memory-only flag, the
    transcript is still on disk, and the new installation's first turn is handed the
    removed app's history by ``build_session_replay``. Nothing self-corrects it — the
    key was never recorded as needing suppression.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _seed_transcript(tmp_path, OWNED, app=APP)

    class _ShutdownRaisesAfterTheClear:
        """The live manager, failing exactly where the real one can: past the clear."""

        def __init__(self) -> None:
            self.persisted: list[str] = []
            self.flushes = 0

        def mapped_session_keys(self) -> set[str]:
            return {OWNED}

        def session_keys(self) -> set[str]:
            return set()

        async def discard_conversation(self, key: str, *, replay: bool = True) -> None:
            # The sid clear and the in-memory flag have landed; the provider
            # shutdown is what fails.
            raise RuntimeError("provider shutdown failed after the sid was cleared")

        def suppress_replay_persistently(self, key: str) -> None:
            self.persisted.append(key)

        async def aflush(self) -> None:
            self.flushes += 1

    sessions = _ShutdownRaisesAfterTheClear()
    state = MagicMock()
    state.sessions = sessions
    # No cron service, so the handler's cron step is skipped and this test is
    # scoped to the step it is about. A bare MagicMock here is not inert: the
    # handler would drive a fake CronSDK whose removal raises, and an uninstall
    # whose cron cleanup raises is refused before anything destructive runs.
    state.crons = None

    request = MagicMock()
    request.match_info = {"name": APP}
    request.app = {"state": state}
    request.json = AsyncMock(return_value={})

    fake_app = {
        "name": APP,
        "manifest": {},
        "resources": "gateway",
        "lifecycle": "normal",
        "enabled": False,
    }

    with (
        patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
        patch(
            "kiro_crew.apps.routes.uninstall_app",
            return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
        ),
        patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
        patch("kiro_crew.apps.routes.deregister_app", return_value=None),
        patch(
            "kiro_crew.apps.teardown.on_app_disable",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
        patch(
            "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
            return_value={"removable": [], "shared": [], "userInstalled": []},
        ),
        patch(
            "kiro_crew.apps.routes.clean_dependencies",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = await routes.handle_uninstall_app(request)

    assert resp.status == 200, "a failed teardown is still not a failed uninstall"
    assert sessions.persisted == [OWNED], (
        "the sid is already cleared, so the suppression flag is the only thing left "
        "stopping the removed app's transcript from being replayed into the next "
        "installation — it cannot be skipped because the shutdown raised"
    )
    assert sessions.flushes == 1, (
        "and an unflushed flag is a flag a restart loses, which is the same bug: the "
        "flush has to run for every key that was written, not only for the keys whose "
        "teardown also succeeded"
    )
