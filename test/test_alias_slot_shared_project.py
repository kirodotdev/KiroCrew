"""Alias slots share one project directory.

Two live slots that resolve to one ``slot_history_key`` are aliases: they write one
transcript, and because ``session_key_for`` returns the shared link they also run one
session with one working directory. ``project`` is that session's directory, so the
aliases hold it in ONE cell (``TranscriptBinding``) rather than one copy each. With one
copy each, a blank alias's forced save wrote ``project: ""`` over the directory its peer
had just stored.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

from test_cleared_project_not_resurrected import _make_state

from kiro_crew.dashboard.chat_api.slot_guards import _CommitToken
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

# Names this change adds are imported inside the tests that need them, so the file still
# collects against the code before the change and its assertions are what fail there.
KEY = "dashboard:shared"
_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # A metadata line must exist so a message-less forced save reaches the merge.
    state.conversation_log.update_metadata(KEY, {"folder_id": "f1"})
    return state


def _rollback(slot, token, prior):
    from kiro_crew.dashboard.transcript_binding import rollback_project

    rollback_project(slot, token, prior)


def _stored_project(state):
    return state.conversation_log._read_metadata(KEY).get("project")


def test_blank_alias_save_keeps_peer_project(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    proj = str(tmp_path / "p")

    a.project = proj
    _save_slot_to_history(state, a, force=True)
    assert _stored_project(state) == proj
    _save_slot_to_history(state, b, force=True)

    assert (
        _stored_project(state) == proj
    ), "the alias that never set a project wrote a blank over its peer's directory"
    assert b.project == proj


def test_a_clear_on_one_alias_is_not_undone_by_the_peer(tmp_path, monkeypatch):
    """A real clear stays a clear: the peer's save must not restore the old directory."""
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    a.project = str(tmp_path / "p")
    _save_slot_to_history(state, a, force=True)

    a.project = ""
    _save_slot_to_history(state, b, force=True)

    assert _stored_project(state) == ""
    assert b.project == ""


def test_a_joining_slot_adopts_the_live_peers_directory(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    a.project = str(tmp_path / "p")

    b = state.get_or_create_slot("b", linked_session_key=KEY)

    assert b.project == a.project
    assert b._binding is a._binding


def test_a_slot_on_its_own_transcript_keeps_its_own_directory(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    other = state.get_or_create_slot("other")
    a.project = str(tmp_path / "p")

    assert other.project == ""
    assert other._binding is not a._binding
    other.project = str(tmp_path / "q")
    assert a.project == str(tmp_path / "p")


def test_a_slot_leaving_an_alias_group_keeps_its_value_and_stops_sharing(tmp_path, monkeypatch):
    from kiro_crew.dashboard.transcript_binding import bind_transcript

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    a.project = str(tmp_path / "p")

    a.linked_session_key = "dashboard:elsewhere"
    bind_transcript(state, a)

    assert a.project == str(tmp_path / "p")
    assert a._binding is not b._binding
    a.project = str(tmp_path / "q")
    assert b.project == str(tmp_path / "p")


def test_rebinding_a_lone_slot_keeps_its_binding(tmp_path, monkeypatch):
    from kiro_crew.dashboard.transcript_binding import bind_transcript

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a")
    before = a._binding

    bind_transcript(state, a)

    assert a._binding is before


def test_a_rollback_skips_a_peers_newer_write(tmp_path, monkeypatch):
    """The ``_CommitToken`` rollback reads the shared cell, so it never clobbers a peer."""
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    prior = str(tmp_path / "old")
    a.project = prior

    token = _CommitToken(str(tmp_path / "new"))
    a.project = token
    b.project = str(tmp_path / "peer")  # lands while A's commit is awaiting
    _rollback(a, token, prior)

    assert a.project == b.project == str(tmp_path / "peer")


def test_a_rollback_with_no_peer_write_restores_the_group(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    prior = str(tmp_path / "old")
    a.project = prior

    token = _CommitToken(str(tmp_path / "new"))
    a.project = token
    assert b.project is token
    _rollback(a, token, prior)

    assert a.project == b.project == prior


def test_a_fresh_slot_holds_a_private_binding():
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.dashboard.transcript_binding import TranscriptBinding

    one, two = _ChatSlot("one"), _ChatSlot("two")
    assert isinstance(one._binding, TranscriptBinding)
    assert one._binding is not two._binding
    assert one.project == ""


def test_the_shallow_copy_paths_never_write_project():
    """Edit-resend and rewind ``copy.copy`` the slot; the copy shares the binding.

    That is right, since the copy writes the same transcript, but it means a project
    write on the copy would reach the live slot and every alias. Neither path writes one.
    """
    for rel in ("dashboard/chat_regenerate.py", "dashboard/chat_rewind.py"):
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        writes = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Attribute) and target.attr in {"project", "_binding"}
        ]
        assert writes == [], f"{rel} writes project at lines {writes}"


def test_copy_copy_shares_the_binding():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("k")
    slot.project = "/p"
    assert copy.copy(slot)._binding is slot._binding


CHANNEL_KEY = "slack:1.1"


def test_surfacing_a_channel_alias_keeps_the_live_peers_directory(tmp_path, monkeypatch):
    """The disk seed writes only a private binding; a live alias's newer value wins."""
    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    live = state.get_or_create_slot("w", linked_session_key=CHANNEL_KEY)
    live.project = str(tmp_path / "new")

    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "stale")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and slot._binding is live._binding
    assert live.project == str(tmp_path / "new")


def test_surfacing_a_lone_channel_session_seeds_its_directory(tmp_path, monkeypatch):
    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "disk")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and slot.project == str(tmp_path / "disk")


def test_a_restore_read_does_not_overwrite_a_shared_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard.slot_persistence.metadata_codec import _read_project

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    lone = state.get_or_create_slot("lone")
    a.project = str(tmp_path / "live")

    _read_project(SimpleNamespace(state=state, slot=b, meta={"project": str(tmp_path / "disk")}))
    _read_project(SimpleNamespace(state=state, slot=lone, meta={"project": str(tmp_path / "d2")}))

    assert a.project == b.project == str(tmp_path / "live")
    assert lone.project == str(tmp_path / "d2")


def test_a_project_change_arms_the_reset_on_every_alias(tmp_path, monkeypatch):
    """One session, one process: the peer's next turn must not reuse the old cwd."""
    from kiro_crew.dashboard.transcript_binding import arm_project_reset

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    lone = state.get_or_create_slot("lone")

    a.project = str(tmp_path / "q")
    arm_project_reset(state, a, KEY)

    assert a._pending_reset_history_key == b._pending_reset_history_key == KEY
    assert lone._pending_reset_history_key is None


def test_the_set_project_directive_arms_the_peer(tmp_path, monkeypatch):
    import asyncio

    from kiro_crew.dashboard.session_directive_apply_slot import _set_project

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)
    a.project = str(tmp_path / "p")

    asyncio.run(_set_project(state, a, {"clear": True}))

    assert b.project == ""
    assert b._pending_reset_history_key == KEY


def test_an_alias_joining_after_the_reset_was_armed_inherits_it(tmp_path, monkeypatch):
    """Teardown can wait on attached sub-agents; a slot joining meanwhile needs the flag."""
    from kiro_crew.dashboard.transcript_binding import arm_project_reset

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    a.project = str(tmp_path / "q")
    arm_project_reset(state, a, KEY)

    b = state.get_or_create_slot("b", linked_session_key=KEY)
    other = state.get_or_create_slot("other")

    assert b._pending_reset_history_key == KEY
    assert other._pending_reset_history_key is None


def test_an_unseeded_live_alias_does_not_suppress_the_stored_directory(tmp_path, monkeypatch):
    """A workflow result tab is born linked with no project; it is not a live value.

    Resurfacing the channel tab beside it must still seed the stored directory, or the
    next save writes the blank over it.
    """
    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    fallback = state.get_or_create_slot("workflow-r1", linked_session_key=CHANNEL_KEY)

    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "disk")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and slot._binding is fallback._binding
    assert fallback.project == slot.project == str(tmp_path / "disk")


def test_an_unseeded_live_alias_takes_the_restored_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard.slot_persistence.metadata_codec import _read_project

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a", linked_session_key=KEY)
    b = state.get_or_create_slot("b", linked_session_key=KEY)

    _read_project(SimpleNamespace(state=state, slot=b, meta={"project": str(tmp_path / "disk")}))

    assert a.project == b.project == str(tmp_path / "disk")


def test_a_live_explicit_clear_outranks_stale_disk(tmp_path, monkeypatch):
    """A clear is a choice: a peer surfacing from older metadata must not undo it."""
    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    live = state.get_or_create_slot("w", linked_session_key=CHANNEL_KEY)
    live.project = str(tmp_path / "old")
    live.project = ""

    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "old")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and slot._binding is live._binding
    assert live.project == slot.project == ""


def test_a_blank_default_over_blank_is_not_a_choice():
    """A computed default that found no directory must not lock out the stored one."""
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("k")
    slot.project = ""
    assert slot._binding.assigned is False
    slot.project = "/p"
    slot.project = ""
    assert slot._binding.assigned is True


def test_a_rolled_back_commit_restores_the_unchosen_state():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("k")
    token = _CommitToken("/new")
    slot.project = token
    _rollback(slot, token, "")
    assert slot.project == "" and slot._binding.assigned is False


def test_a_rolled_back_commit_keeps_an_earlier_choice():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("k")
    slot.project = "/old"
    slot.project = ""
    token = _CommitToken("/new")
    slot.project = token
    _rollback(slot, token, "")
    assert slot.project == "" and slot._binding.assigned is True


def test_a_clear_during_a_pending_commit_is_a_choice():
    """A peer's clear replaces the token; the rollback then does nothing."""
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("k")
    token = _CommitToken("/new")
    slot.project = token
    slot.project = ""  # the unlocked set_project clear, during the switch's await
    _rollback(slot, token, "")
    assert slot.project == "" and slot._binding.assigned is True


def test_a_rolled_back_switch_on_an_unseeded_alias_still_takes_the_stored_directory(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    fallback = state.get_or_create_slot("workflow-r1", linked_session_key=CHANNEL_KEY)
    token = _CommitToken(str(tmp_path / "new"))
    fallback.project = token
    _rollback(fallback, token, "")

    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "disk")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and fallback.project == str(tmp_path / "disk")


def test_restored_aliases_rejoin_one_binding(tmp_path, monkeypatch):
    """A restore links the slot after creating it; the join must follow the link."""
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _state(tmp_path, monkeypatch)
    a = state.get_or_create_slot("a")
    codec.apply(
        state,
        a,
        {"linked_session_key": KEY, "project": str(tmp_path / "p")},
        codec.Restore(name="a"),
    )
    b = state.get_or_create_slot("b")
    codec.apply(
        state,
        b,
        {"linked_session_key": KEY, "project": str(tmp_path / "p")},
        codec.Restore(name="b"),
    )

    assert a._binding is b._binding
    assert a.project == b.project == str(tmp_path / "p")
    b.project = ""
    assert a.project == ""


def test_a_restored_tab_seeds_an_unseeded_live_alias(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _state(tmp_path, monkeypatch)
    fallback = state.get_or_create_slot("workflow-r1", linked_session_key=KEY)
    restored = state.get_or_create_slot("r")
    codec.apply(
        state,
        restored,
        {"linked_session_key": KEY, "project": str(tmp_path / "disk")},
        codec.Restore(name="r"),
    )

    assert restored._binding is fallback._binding
    assert fallback.project == restored.project == str(tmp_path / "disk")


def test_a_restored_tab_keeps_a_live_aliases_clear(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _state(tmp_path, monkeypatch)
    live = state.get_or_create_slot("w", linked_session_key=KEY)
    live.project = str(tmp_path / "old")
    live.project = ""
    restored = state.get_or_create_slot("r")
    codec.apply(
        state,
        restored,
        {"linked_session_key": KEY, "project": str(tmp_path / "old")},
        codec.Restore(name="r"),
    )

    assert live.project == restored.project == ""


def test_seeding_a_running_alias_arms_its_reset(tmp_path, monkeypatch):
    """The live process keeps its old cwd; the group must reset before trusting the new one."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _state(tmp_path, monkeypatch)
    fallback = state.get_or_create_slot("workflow-r1", linked_session_key=KEY)
    state.sessions.get_provider = MagicMock(return_value=object())
    restored = state.get_or_create_slot("r")
    codec.apply(
        state,
        restored,
        {"linked_session_key": KEY, "project": str(tmp_path / "disk")},
        codec.Restore(name="r"),
    )

    assert fallback.project == str(tmp_path / "disk")
    assert fallback._pending_reset_history_key == restored._pending_reset_history_key == KEY


def test_apply_on_a_slot_stand_in_does_not_crash():
    from types import SimpleNamespace

    from kiro_crew.dashboard.transcript_binding import live_project_outranks_disk

    assert live_project_outranks_disk(SimpleNamespace(_slots={}), SimpleNamespace()) is False


def test_surfacing_beside_a_running_unseeded_alias_arms_the_reset(tmp_path, monkeypatch):
    """Surfacing joins the binding before it seeds, so the seed itself must arm it."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import channel_slots

    state = _state(tmp_path, monkeypatch)
    fallback = state.get_or_create_slot("workflow-r1", linked_session_key=CHANNEL_KEY)
    state.sessions.get_provider = MagicMock(return_value=object())

    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "modified": 0.0},
        {"project": str(tmp_path / "disk")},
        [],
        session_key=CHANNEL_KEY,
    )

    assert slot is not None and fallback.project == str(tmp_path / "disk")
    assert fallback._pending_reset_history_key == CHANNEL_KEY
    assert slot._pending_reset_history_key == CHANNEL_KEY
