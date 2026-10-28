"""Session control create: folder filing, model pins and empty-newborn saves.

Covers ``sc.create_session`` filing at birth: the slot and its birth metadata
carry ``folder_id``, a folder's or an ancestor's ``project_dir`` is inherited, an
unknown or mid-create-deleted folder fails with ``folder_not_found``, and an
app-scoped caller is refused despite a valid folder. A committed create un-hides a
hidden folder and a refused one does not. A requested ``model`` is pinned at birth;
a picker-refused, credential-shaped, over-long or bad-charset one fails the create.

``save_slot_off_loop`` on a message-less newborn persists re-filing, tags, a pin,
a close and a named ``memory_store``, reads slot state at write time, raises on an
unreadable record with ``best_effort=False`` and recreates no deleted file.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.validation import MAX_SHORT_STRING

# ── session_create: filing at birth ─────────────────────────────────


def test_create_schema_bounds_the_folder_reference():
    """`folder` takes an id OR a human path, bounded like every folder ref.

    The two readings share no charset, so the schema checks only the length --
    the same contract `chat_folder_move_session.folder` carries. Over-length is
    refused at validation, before any resolution work.
    """
    from kiro_crew.validation import SESSION_CREATE_SCHEMA, ValidationError, validate_tool_args

    out = validate_tool_args({"folder": "aaaaaaaaaaaa"}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "aaaaaaaaaaaa", "an id-shaped reference must pass"
    out = validate_tool_args({"folder": "Goals/Q3 push"}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "Goals/Q3 push", "a '/'-separated human path must pass"
    out = validate_tool_args({}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "", "omitted means unfiled, not an error"
    with pytest.raises(ValidationError):
        validate_tool_args({"folder": "x" * 4097}, SESSION_CREATE_SCHEMA)


def _folder(state, fid: str, name: str, **extra):
    row = {"id": fid, "name": name, "parent_id": "", **extra}
    state._folders.append(row)
    return row


def test_create_files_the_slot_at_birth(tmp_path):
    """A named folder is applied at creation AND rides the birth metadata.

    The disk half is the load-bearing one: the normal save path returns early on
    an empty message window, so for a created-then-idle session the birth
    metadata line is the only durable record of the placement. Dropping
    `folder_id` from that dict files the session in memory and loses it on the
    next restart.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.folder_id == "fold00000001", "the slot must be filed, not left for a move"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("folder_id") == "fold00000001", (
        "the placement must reach the persist-at-birth metadata -- the save path "
        "writes nothing for an empty session, so this line is the only record"
    )


def test_create_inherits_the_folders_project_dir(tmp_path):
    """A dispatched session filed in a project folder runs in that project."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    folder_project = tmp_path / "folder-project"
    folder_project.mkdir()
    _folder(
        state,
        "fold00000002",
        "Project",
        project_dir=str(folder_project),
    )

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000002")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.workspace == caller.workspace
    assert child.project == str(folder_project.resolve())
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("project") == str(folder_project.resolve())


def test_create_inherits_an_ancestor_folders_project_dir(tmp_path):
    """Project inheritance follows the same nearest-ancestor rule as dashboard creation."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    folder_project = tmp_path / "ancestor-project"
    folder_project.mkdir()
    _folder(
        state,
        "fold00000003",
        "Project",
        project_dir=str(folder_project),
    )
    child_folder = _folder(state, "fold00000004", "Worker")
    child_folder["parent_id"] = "fold00000003"

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000004")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.project == str(folder_project.resolve())


def test_create_refuses_an_unknown_folder(tmp_path):
    """An unresolvable folder refuses the WHOLE create, allocating nothing.

    The caller asked for a session filed in this folder; 'created but unfiled'
    would silently honor half of that. No session exists yet, so refusal loses
    nothing -- the same posture the move path takes on an unknown folder.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="nope00000000")
        )

    assert exc.value.code == "folder_not_found"
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"


def test_create_pins_the_requested_model_at_birth(tmp_path):
    """A named model is pinned on the slot AND rides the birth metadata.

    Same disk argument as the folder: an idle newborn's birth line is its only
    durable record, so a model set only in memory would come back as the default
    after a restart. The pick-generation bump is what makes it an explicit
    choice the fallback restore probe will not override.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), model="claude-sonnet-4.6")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.model == "claude-sonnet-4.6"
    assert child._model_pick_gen == 1
    assert created["model"] == "claude-sonnet-4.6"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("model") == "claude-sonnet-4.6"


def test_create_without_a_model_leaves_the_default(tmp_path):
    """Omitting `model` changes nothing: no pin, no metadata key, no pick bump."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.model == ""
    assert child._model_pick_gen == 0
    assert "model" not in created
    assert "model" not in state.conversation_log.get_metadata(slot_history_key(child))


def test_create_refuses_a_model_the_picker_refuses(tmp_path, monkeypatch):
    """The picker's own guard decides, and a refusal allocates nothing."""
    import kiro_crew.dashboard.chat_handlers as ch

    monkeypatch.setattr(
        ch, "_model_rejected_reason", lambda name, provider=None: f"{name} is display-only"
    )
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), model="fable-5-1m"))

    assert exc.value.code == "model_rejected"
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"


def test_create_refuses_a_credential_shaped_model(tmp_path):
    credential_shaped_id = "AKIA" + "Q" * 16
    assert sc.redact(credential_shaped_id) != credential_shaped_id
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(
                state,
                caller_session_key=_key(caller),
                model=credential_shaped_id,
            )
        )

    assert exc.value.code == "model_rejected"
    assert credential_shaped_id not in exc.value.message
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"


@pytest.mark.parametrize(
    "bad_model",
    ["x y", "a" * (MAX_SHORT_STRING + 1)],
    ids=["bad-charset", "over-length"],
)
def test_create_refuses_an_unbounded_or_bad_charset_model(tmp_path, bad_model):
    """The HTTP route does not run SESSION_CREATE_SCHEMA, so create bounds model itself."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), model=bad_model))

    assert exc.value.code == "model_rejected"
    assert bad_model not in exc.value.message
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"


def test_a_folder_deleted_mid_create_is_refused_under_the_lock(tmp_path, monkeypatch):
    """Folder existence is decided under the folder-store lock, late.

    `create_session` suspends before the allocation (project dir, config load),
    and a folder delete can land in those windows. Existence is therefore
    confirmed READ-ONLY under the folder-store lock (`state.read_folders`) as
    the last suspension before the re-gate. Simulated by deleting the folder
    inside the project-dir resolution. Mutation guard: a check that reads the
    unlocked list before those awaits would let this create succeed into a
    folder that is already gone.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    def _delete_folder_then_resolve(_workspace):
        # Stand in for the interleaving: the delete lands while the project
        # directory is still being resolved off-loop.
        state._folders[:] = [f for f in state._folders if f["id"] != "fold00000001"]
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _delete_folder_then_resolve)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
        )
    assert exc.value.code == "folder_not_found"


def test_filing_at_birth_unhides_the_folder_like_a_move(tmp_path):
    """Model-B semantics apply at create exactly as they do on a move.

    Moving a session into a hidden folder un-hides it (`_unhide_folder`), so a
    session filed at creation must not land invisibly inside a folder the user
    cannot see -- that would be a session the sidebar hides by construction.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    row = _folder(state, "fold00000001", "Goal", hidden=True)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    assert state.get_slot(created["target"]).folder_id == "fold00000001"
    assert row["hidden"] is False, "filing into a hidden folder must un-hide it"


def test_a_folder_cannot_smuggle_creation_past_the_caller_refusals(tmp_path):
    """Caller eligibility precedes every folder consideration.

    The move path's app-ownership rule never has to run here because an
    app-scoped caller cannot create a session at all -- that refusal is the
    guard the filing inherits, and it must keep firing FIRST so the folder
    argument cannot become a probe for folder existence.
    """
    state = _make_state(tmp_path)
    app_caller = _slot(state, "chat-app")
    app_caller._app = "some-app"
    _folder(state, "fold00000001", "Goal")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(app_caller), folder_id="fold00000001")
        )
    assert (
        exc.value.code == "app_scoped_caller"
    ), "the caller refusal must precede folder handling -- not folder_not_found"


def test_a_refused_create_leaves_a_hidden_folder_hidden(tmp_path, monkeypatch):
    """No durable folder-tree mutation may survive a refused create.

    The Model-B un-hide persists `hidden = False` to the folder store, and the
    re-gate can still refuse AFTER the folder was confirmed -- so un-hiding
    early would durably reverse a choice the user made, for a call that failed.
    Simulated with the caller closing mid-create (the same interleaving the
    re-gate exists for). Mutation guard: moving the un-hide back before the
    re-gate flips the folder visible here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    row = _folder(state, "fold00000001", "Goal", hidden=True)

    def _close_caller_then_resolve(_workspace):
        state._slots.pop(caller.key, None)
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _close_caller_then_resolve)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
        )
    assert exc.value.code == "caller_not_open"
    assert row["hidden"] is True, "a refused create must not un-hide the folder"


def test_moving_an_empty_newborn_before_its_first_message_survives_a_restart(tmp_path):
    """A filed-at-birth session that is re-filed while still empty persists it.

    Birth metadata made empty sessions durable, which made the save path's
    empty-window early return newly consequential: the folder PATCH route and
    the folder-delete sweep persist via `save_slot_off_loop(force=True)`, whose
    full save has no window to write for a message-less slot -- without the
    metadata merge, a restart would resurrect the BIRTH placement the user
    already changed (or point at a folder they deleted).
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")
    _folder(state, "fold00000002", "Other")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    # The user drags it into another folder before any message lands.
    child.folder_id = "fold00000002"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    moved = state.conversation_log.get_metadata(slot_history_key(child))
    assert moved.get("folder_id") == "fold00000002", "the move must overwrite the birth filing"

    # And unfiling durably clears it (a falsy value reads as unfiled).
    child.folder_id = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))
    unfiled = state.conversation_log.get_metadata(slot_history_key(child))
    assert not unfiled.get("folder_id"), "unfiling must not resurrect the birth filing"

    # An ordinary empty tab has no metadata line, and a forced save must not
    # materialize one -- a session with no line does not survive a restart, so
    # there is nothing to reconcile.
    plain = _slot(state, "chat-plain")
    asyncio.run(save_slot_off_loop(state, plain, force=True))
    meta, readable = state.conversation_log.get_metadata_status(slot_history_key(plain))
    assert readable and not meta, "no metadata line may be invented for a plain empty tab"


def test_metadata_mutations_on_an_empty_newborn_survive_a_restart(tmp_path):
    """Tags and a pin acknowledged before the first message persist.

    The tag routes and the pin route persist ONLY through
    ``save_slot_off_loop(force=True)`` -- for a message-less newborn that is
    the empty-window merge, so a folder-only merge would acknowledge the
    mutation and then silently drop it on restart. Clears must be explicit:
    the merge cannot delete a key, so untag/unpin write falsy values that
    rehydrate reads as cleared.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    # The user tags, pins, mode-switches, and binds it before any message lands.
    child.tags = ["tag00000001"]
    child.pinned = True
    child.mode = "design-critique"
    child._artifact = "my-artifact"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("tags") == ["tag00000001"], "an acknowledged tag must reach disk"
    assert meta.get("pinned") is True, "an acknowledged pin must reach disk"
    assert meta.get("mode") == "design-critique", "an acknowledged mode switch must reach disk"
    assert meta.get("artifact") == "my-artifact", "an acknowledged binding must reach disk"
    assert meta.get("folder_id") == "fold00000001", "the merge must not drop the birth filing"

    # And the clears are explicit -- a restart must not resurrect them.
    child.tags = []
    child.pinned = False
    child.mode = ""
    child._artifact = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("tags") == [], "untagging must overwrite the persisted tags"
    assert not meta.get("pinned"), "unpinning must overwrite the persisted pin"
    assert not meta.get("mode"), "a mode reset must overwrite the persisted mode"
    assert not meta.get("artifact"), "an unbind must overwrite the persisted binding"


def test_the_empty_window_merge_mirrors_the_full_saves_slot_owned_fields(tmp_path):
    """Drift guard: every restart-relevant slot-owned field survives the merge.

    The empty-window merge must persist what the FULL save's metadata line
    would persist, or whichever route happens to flow through it silently
    drops an acknowledged mutation on restart. Keyed to
    ``SLOT_OWNED_META_KEYS`` so a field added to the full save later fails
    here instead of regressing quietly. Exclusions are the history-layer /
    conditional-identity keys the merge legitimately writes only when the
    slot carries them.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.history import SLOT_OWNED_META_KEYS

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    child.tags = ["tag00000001"]
    child.pinned = True
    child.mode = "design-critique"
    child._artifact = "my-artifact"
    child.reasoning_effort = "high"
    child.color_index = 3
    child.title = "Pinned title"
    child._titled = True
    child._title_origin = "user"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))

    # History-layer bookkeeping the merge must NOT touch, the closed pair
    # (exercised by its own test), and identity fields a plain user newborn
    # does not carry.
    excluded = {
        "_type",
        "created_at",
        "last_consolidated",
        "closed",
        "closed_at",
        "app",
        "forked_from",
        "linked_session_key",
        # The relay-archive binding, written all-three-or-none by both the
        # full save and the merge. A plain local newborn carries none of it.
        "executor",
        "instance_id",
        "remote_slot",
        # Written only for a NON-DEFAULT memory store, because absence is what
        # means "the global store" -- so a newborn on the default store must NOT
        # carry it, and writing "default" here would make a session that predates
        # per-agent memory stores read differently from one saved today. The
        # named-store half is pinned by the next test, which is the direction that
        # can lose data: the key is slot-owned, so a merge that failed to write it
        # would drop the binding and silently return that session to the global
        # store.
        "memory_store",
    }
    for key in sorted(SLOT_OWNED_META_KEYS - excluded):
        assert key in meta, f"slot-owned field {key!r} missing after an empty-window forced save"
    assert meta.get("artifact") == "my-artifact"
    assert meta.get("reasoning_effort") == "high"
    assert meta.get("color_index") == 3
    assert meta.get("title") == "Pinned title"
    assert meta.get("title_origin") == "user"
    from kiro_crew.context import store_of_session

    assert (
        store_of_session(state.conversation_log, slot_history_key(child)) == ""
    ), "a newborn on the default store names no silo"


def test_the_empty_window_merge_keeps_a_named_memory_store(tmp_path):
    """A crew's silo must survive the merge, and the default must stay absent.

    ``memory_store`` is slot-owned, so ``carry_unowned_metadata`` will NOT
    preserve it from the previous record -- the merge has to write it or the key
    is gone. Losing it does not fail loudly: the session simply consolidates into
    the operator's global memory from then on, which is the one outcome per-crew
    isolation exists to prevent. Asserted in both directions, because the retract
    path (rebinding a crew back to the default store) depends on absence.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    child.memory_store = "coding"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("memory_store") == "coding"

    # Rebinding to the default RETRACTS it. The merge cannot delete a key, so the
    # cleared form is a falsy value; what must hold is that the consolidator
    # resolves it to the global store again.
    from kiro_crew.context import store_of_session

    child.memory_store = "default"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert not meta.get("memory_store"), meta.get("memory_store")
    assert store_of_session(state.conversation_log, slot_history_key(child)) == ""


def test_the_empty_window_merge_reads_slot_state_at_write_time(tmp_path):
    """The merged fields are evaluated under the history lock, not snapshotted.

    Two concurrent force-saves of the same empty newborn can commit in either
    order; a dict snapshotted before the lock would let the older save land
    second and silently revert an acknowledged newer mutation (a tag save
    restoring ``pinned=False`` over a pin that already committed). Pinning the
    guard-time read: a slot mutation landing after the save call but before
    the locked write must be what reaches disk.
    """
    from unittest.mock import patch

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    real = state.conversation_log.update_metadata_if

    def _mutate_then_write(key, fields, guard, **kwargs):
        # Simulates a concurrent pin committing between this save's call and
        # its locked write: the merge must pick up the NEW value.
        child.pinned = True
        return real(key, fields, guard, **kwargs)

    child.pinned = False
    with patch.object(state.conversation_log, "update_metadata_if", _mutate_then_write):
        asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("pinned") is True, "the merge must write the slot state current at lock time"


def test_closing_an_empty_newborn_does_not_resurrect_it_open(tmp_path):
    """A close acknowledged for a message-less newborn persists to its line.

    Birth metadata made empty sessions durable -- without the closed merge the
    line stays open-shaped and the next restart resurrects a tab the user
    dismissed.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    asyncio.run(save_slot_off_loop(state, child, closed=True, closed_at=123.0))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("closed") is True, "the close must reach the birth metadata line"
    assert meta.get("closed_at") == 123.0, "the close instant must be the caller-supplied one"


def test_an_unreadable_record_fails_the_empty_window_merge_loudly(tmp_path):
    """A merge skipped because the record could not be READ must raise.

    ``update_metadata_if`` fails closed on an unreadable record without
    invoking the guard; swallowing that would report the save as durable -- a
    close would remove the tab while the on-disk line stays open-shaped, and
    the next restart resurrects it. The by-design skip (guard ran, record
    empty: a line-less plain tab) must stay silent.
    """
    from unittest.mock import patch

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    def _unreadable(key, fields, guard, **_kwargs):
        # Mirrors update_metadata_if's fail-closed path: guard NOT invoked.
        return False

    with patch.object(state.conversation_log, "update_metadata_if", _unreadable):
        with pytest.raises(Exception):
            asyncio.run(save_slot_off_loop(state, child, closed=True, best_effort=False))

    # The by-design skip stays silent: a plain empty tab has no metadata line,
    # the guard runs, sees an empty record, and refuses without error.
    plain = _slot(state, "chat-plain2")
    assert asyncio.run(save_slot_off_loop(state, plain, force=True, best_effort=False)) is True


def test_an_unhide_failure_does_not_fail_a_committed_create(tmp_path, monkeypatch):
    """The Model-B un-hide is best-effort once the create has committed.

    By the time it runs, the slot is published and persisted at birth -- a
    folder-store write failure propagating from here would return 500 for a
    session that EXISTS, and the caller's natural retry would create a
    duplicate. Mutation guard: letting the exception escape fails this create.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal", hidden=True)

    async def _boom(_state, _fid):
        raise RuntimeError("folder store write failed")

    monkeypatch.setattr(sc, "_unhide_folder", _boom)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child is not None, "the committed create must be reported as a success"
    assert child.folder_id == "fold00000001"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("folder_id") == "fold00000001", "the filing itself must have landed"


def test_the_empty_window_merge_cannot_resurrect_a_deleted_session(tmp_path):
    """The existence guard and the merge run under ONE lock.

    The plain metadata update is an upsert, so a checked-then-written pair
    would let a permanent deletion land between the read and the write and be
    recreated as a fresh file. `update_metadata_if` re-makes the decision inside
    the cross-process lock; a session file deleted before the forced save stays
    deleted.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])
    history_key = slot_history_key(child)
    path = state.conversation_log._path(history_key)
    assert path.exists(), "birth metadata must be on disk before the deletion"

    # A permanent deletion lands, then the racing forced save arrives.
    path.unlink()
    child.folder_id = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))

    assert not path.exists(), "the merge must not resurrect a deleted session file"


def test_a_crew_mode_root_creates_its_worker_outside_the_persons_folders(tmp_path, request):
    """The server drops a Crew Mode caller's folder, whoever sent it (``crew_mode``)."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent_kind = "template"
    # The caller's own agent is the configured Crew Mode conductor, so the case does
    # not depend on a conductor spec being installed in the test home.
    from types import SimpleNamespace

    from kiro_crew import crew_mode

    caller.agent = "kirocrew"
    # The chat turned Crew Mode on: its root slot carries the stamp.
    caller.crew_mode_agent = caller.agent
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="kirocrew")))
    request.addfinalizer(
        lambda: crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="")))
    )
    folder_project = tmp_path / "goal-project"
    folder_project.mkdir()
    _folder(state, "fold00000001", "Goal", project_dir=str(folder_project))

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert not child.folder_id
    assert created.get("folder_ignored") == "crew_mode"
    assert created.get("crew_mode") is True
    # Not filed, but the folder still gives the worker its project directory, so it
    # does its work in the folder's repository rather than the workspace default.
    assert child.project == str(folder_project.resolve())

    # A worker of that root is held to the same rule for the sessions IT opens.
    grandchild = asyncio.run(
        sc.create_session(state, caller_session_key=_key(child), folder_id="fold00000001")
    )
    assert not state.get_slot(grandchild["target"]).folder_id
    assert grandchild.get("folder_ignored") == "crew_mode"


@pytest.mark.parametrize("conductor", [True, False], ids=["crew-mode", "plain"])
@pytest.mark.parametrize("folder_id", ["", "fold00000002"], ids=["inherited", "named"])
def test_fork_filing_respects_the_callers_crew_mode_tree(
    tmp_path, monkeypatch, conductor, folder_id
):
    """Crew workers are unfiled at birth; plain forks keep normal folder inheritance."""
    from kiro_crew import crew_mode
    from kiro_crew.dashboard import chat_fork
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent_kind = "template"
    caller.agent = "kirocrew"
    if conductor:
        caller.crew_mode_agent = caller.agent
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", "kirocrew" if conductor else "other-conductor")
    _folder(state, "fold00000001", "Parent", hidden=True)
    named_folder = _folder(state, "fold00000002", "Requested", hidden=True)
    caller.folder_id = "fold00000001"
    caller.append("user", "Split this investigation", "msg msg-u")
    caller.append("assistant", "Investigation context", "msg msg-a")
    caller.drain()
    _save_slot_to_history(state, caller, closed=False)
    caller._resumed_count = len(caller.messages)
    caller._disk_window_len = len(caller.messages)
    caller._dirty = False
    birth_folders = []
    real_save = chat_fork.save_slot_off_loop

    async def _save(st, slot, *args, **kwargs):
        if slot is not caller:
            birth_folders.append(slot.folder_id)
        return await real_save(st, slot, *args, **kwargs)

    monkeypatch.setattr(chat_fork, "save_slot_off_loop", _save)
    forked = asyncio.run(
        sc.fork_session(state, caller_session_key=_key(caller), folder_id=folder_id)
    )

    child = state.get_slot(forked["target"])
    expected_folder = "" if conductor else folder_id or caller.folder_id
    assert child is not None
    assert child.folder_id == expected_folder
    assert birth_folders == [expected_folder], "the first save must already have the final filing"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert (written.get("folder_id") or "") == expected_folder
    assert forked["folder_id"] == (expected_folder or None)
    assert caller.folder_id == "fold00000001"
    if conductor:
        assert forked.get("crew_mode") is True
        assert forked.get("folder_ignored") == ("crew_mode" if folder_id else None)
        assert named_folder["hidden"] is True
    else:
        assert "crew_mode" not in forked
        assert "folder_ignored" not in forked


def test_a_plain_caller_still_files_its_child(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")
    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    assert state.get_slot(created["target"]).folder_id == "fold00000001"
    assert "folder_ignored" not in created
    assert "crew_mode" not in created


def _unstamped_conductor_root(state, monkeypatch):
    """A root chat on the configured conductor that never turned Crew Mode on."""
    from kiro_crew import crew_mode

    caller = _slot(state, "chat-1")
    caller.agent_kind = "template"
    caller.agent = "kirocrew"
    caller.crew_mode_agent = ""
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", "kirocrew")
    assert crew_mode.slot_is_crew_mode_root(caller)
    return caller


def test_an_unstamped_conductor_root_still_files_its_child(tmp_path, monkeypatch):
    """Folder effects follow the root's stamp, not the conductor template alone."""
    from kiro_crew import crew_mode

    state = _make_state(tmp_path)
    caller = _unstamped_conductor_root(state, monkeypatch)
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child.folder_id == "fold00000001"
    assert "folder_ignored" not in created
    assert "crew_mode" not in created
    # The row stamps still mark the tree: only the folder rule reads the stamp.
    assert crew_mode.slot_in_crew_tree(state._slots, child.key)
    assert not crew_mode.slot_in_turned_on_tree(state._slots, child.key)

    grandchild = asyncio.run(
        sc.create_session(state, caller_session_key=_key(child), folder_id="fold00000001")
    )
    assert state.get_slot(grandchild["target"]).folder_id == "fold00000001"
    assert "folder_ignored" not in grandchild


def test_an_unstamped_conductor_roots_fork_inherits_its_folder(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    state = _make_state(tmp_path)
    caller = _unstamped_conductor_root(state, monkeypatch)
    _folder(state, "fold00000001", "Parent")
    caller.folder_id = "fold00000001"
    caller.append("user", "Split this investigation", "msg msg-u")
    caller.append("assistant", "Investigation context", "msg msg-a")
    caller.drain()
    _save_slot_to_history(state, caller, closed=False)
    caller._resumed_count = len(caller.messages)
    caller._disk_window_len = len(caller.messages)
    caller._dirty = False

    forked = asyncio.run(sc.fork_session(state, caller_session_key=_key(caller)))

    assert state.get_slot(forked["target"]).folder_id == "fold00000001"
    assert forked["folder_id"] == "fold00000001"
    assert "crew_mode" not in forked
    assert "folder_ignored" not in forked


def test_turned_on_tree_reads_the_root_rows_stamp():
    from kiro_crew import crew_mode

    stamped = {"key": "r1", "crew_mode_root": True, "crew_mode_agent": "kirocrew"}
    unstamped = {"key": "r2", "crew_mode_root": True, "crew_mode_agent": ""}
    rows = {
        "r1": stamped,
        "r2": unstamped,
        "w1": {"key": "w1", "crew_root": "r1"},
        "w2": {"key": "w2", "crew_root": "r2"},
        "w3": {"key": "w3", "crew_root": "closed-root"},
        "p": {"key": "p"},
    }
    assert crew_mode.row_in_turned_on_tree(rows["r1"], rows)
    assert crew_mode.row_in_turned_on_tree(rows["w1"], rows)
    assert not crew_mode.row_in_turned_on_tree(rows["r2"], rows)
    assert not crew_mode.row_in_turned_on_tree(rows["w2"], rows)
    assert not crew_mode.row_in_turned_on_tree(rows["w3"], rows)
    assert not crew_mode.row_in_turned_on_tree(rows["p"], rows)
    assert not crew_mode.row_in_turned_on_tree(None, rows)
