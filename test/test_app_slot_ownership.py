"""Uninstalling an app drops its conversations' resume pointers.

The bug: a slot key an app chooses is often DETERMINISTIC — one slot per object it
tracks, named after that object. ``session.py``'s
``resume_sid = self._session_map.get(key)`` therefore hands the next slot under that
name the PREVIOUS conversation, which is correct while the app is installed and wrong
once it is gone: reinstall, open the same object, and the first turn resumes a
transcript from code that no longer exists.

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
import pathlib

from kiro_crew.apps import bridges
from kiro_crew.gateway_lock import GatewayLock
from kiro_crew.history import transcript_stem
from kiro_crew.session_map import SessionMap

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


def test_the_cli_path_drops_the_pointer_when_no_gateway_owns_the_map(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    smap = SessionMap()
    smap.set(OWNED, "sid-app", provider="acp")
    smap.set(MINE, "sid-mine", provider="acp")
    _seed_transcript(tmp_path, OWNED, app=APP)
    _seed_transcript(tmp_path, MINE)

    assert bridges.discard_app_session_pointers(APP) == 1

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
        assert bridges.discard_app_session_pointers(APP) == 0

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
