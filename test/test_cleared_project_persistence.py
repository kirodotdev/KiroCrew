"""A cleared project must survive a gateway restart.

``project`` and the ``project_cleared`` marker are two halves of one answer, so both have
to reach disk. A record carrying the directory alone cannot say the user removed it, and
the merge behind the save cannot delete a key — so restoring such a record rebinds the
turn to the project that was cleared, and it then reads and writes there succeeding the
whole way, with the wrong directory as the only symptom.

These tests pin the round trip in both directions — a clear that must come back
cleared, and a project set again afterwards that must not — across BOTH restore paths,
which read the same metadata through two different functions.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import CWD_CLEARED
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    save_slot_off_loop,
)
from kiro_crew.history import ROWS_ONLY_DEFERRED_META_KEYS, SLOT_OWNED_META_KEYS

KEY = "chat-11813-1"
HISTORY_KEY = f"dashboard:{KEY}"

RESTORE_PATHS = pytest.mark.parametrize("restore", ["history", "recent"])


def _seeded_slot(state):
    """A slot with a transcript, which is what restoration reads."""
    state.conversation_log.append(HISTORY_KEY, "user", "hello")
    return state.get_or_create_slot(KEY)


def _save(state, slot):
    asyncio.run(save_slot_off_loop(state, slot, force=True))


def _restored(tmp_path, restore):
    """Restore through a second state over the same directory — a restart."""
    state = _make_state(tmp_path / "sessions")
    if restore == "history":
        return _rehydrate_slot_from_history(state, KEY)
    _apply_recent_session(
        state,
        HISTORY_KEY,
        KEY,
        {},
        state.conversation_log.get_metadata(HISTORY_KEY),
        state.conversation_log._read_messages(HISTORY_KEY),
        conv_log=state.conversation_log,
        kiro_model_map={},
        restore_cfg=KiroCrewConfig.load(),
    )
    return state._slots.get(KEY)


@RESTORE_PATHS
def test_a_cleared_project_survives_a_restart(tmp_path, restore):
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project = "/old"
    _save(state, slot)

    slot.project = ""
    slot.project_cleared = True
    _save(state, slot)

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project == ""
    assert restored.project_cleared is True
    assert restored.claim_cwd == CWD_CLEARED


@RESTORE_PATHS
def test_a_cleared_project_survives_a_restart_of_an_active_chat(tmp_path, restore):
    """The save has TWO metadata paths, and only the message-less one was covered."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.messages.append({"role": "user", "content": "hello"})
    slot.project = "/old"
    _save(state, slot)

    slot.project = ""
    slot.project_cleared = True
    _save(state, slot)

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project == ""
    assert restored.project_cleared is True
    assert restored.claim_cwd == CWD_CLEARED


@RESTORE_PATHS
def test_an_active_chats_new_project_is_not_restored_as_cleared(tmp_path, restore):
    """The other direction through the same rebuild, so the write cannot pass by always
    claiming a clear."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.messages.append({"role": "user", "content": "hello"})
    slot.project = ""
    slot.project_cleared = True
    _save(state, slot)

    slot.project = "/new"
    slot.project_cleared = False
    _save(state, slot)

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project == "/new"
    assert restored.project_cleared is False
    assert restored.claim_cwd == "/new"


def test_the_marker_is_slot_owned_and_travels_with_the_project():
    """The marker DESCRIBES ``project``, so the two must share an ownership class."""
    assert "project" in SLOT_OWNED_META_KEYS
    assert "project_cleared" in SLOT_OWNED_META_KEYS
    assert "project" in ROWS_ONLY_DEFERRED_META_KEYS
    assert "project_cleared" in ROWS_ONLY_DEFERRED_META_KEYS


def test_a_rows_only_write_leaves_the_holders_marker_alone(tmp_path):
    """A save that only needs its rows down must not restate another slot's project."""
    state = _make_state(tmp_path / "sessions")
    holder = _seeded_slot(state)
    holder.project = "/holder-project"
    holder.project_cleared = False
    holder.messages.append({"role": "user", "content": "the holder's turn"})
    _save(state, holder)
    assert state.conversation_log.get_metadata(HISTORY_KEY).get("project") == "/holder-project"

    popped = state.get_or_create_slot("chat-11813-2")
    popped._tab_id = "otherslot"
    popped.linked_session_key = HISTORY_KEY
    popped.project = ""
    popped.project_cleared = True
    popped.append("user", "handover row")
    popped.drain()

    _save_slot_to_history(state, popped, rows_only=True)

    meta = state.conversation_log.get_metadata(HISTORY_KEY)
    assert meta.get("project") == "/holder-project"
    assert meta.get("project_cleared") is not True


@RESTORE_PATHS
def test_a_never_set_project_is_not_restored_as_cleared(tmp_path, restore):
    """The control: absence of a project is not a clear, and states no cwd at all."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    _save(state, slot)

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project_cleared is False
    assert restored.claim_cwd is None


@RESTORE_PATHS
def test_a_project_set_again_after_a_clear_is_not_restored_as_cleared(tmp_path, restore):
    """The marker has to be overwritable, not merely writable."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project = "/old"
    _save(state, slot)

    slot.project = ""
    slot.project_cleared = True
    _save(state, slot)

    slot.project = "/new"
    slot.project_cleared = False
    _save(state, slot)

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project == "/new"
    assert restored.project_cleared is False
    assert restored.claim_cwd == "/new"


@RESTORE_PATHS
def test_a_string_valued_marker_does_not_restore_an_uncleared_slot_as_cleared(tmp_path, restore):
    """Only a literal ``True`` is a clear, because the record is a file on disk."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project = "/old"
    _save(state, slot)
    state.conversation_log.update_metadata(HISTORY_KEY, {"project_cleared": "false"})

    restored = _restored(tmp_path, restore)

    assert restored is not None
    assert restored.project_cleared is False
    assert restored.project == "/old"
    assert restored.claim_cwd == "/old"


def test_a_clear_racing_a_new_project_selection_leaves_the_selection_alone(tmp_path):
    """The clear resolves the default workspace across an await, and must re-read after it."""
    from kiro_crew.dashboard.session_directive_apply import _set_project

    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project = "/old"
    armed_keys: list[str] = []
    state.sessions.mark_retire_on_next_claim = lambda key, cwd: armed_keys.append(key)

    async def _resolve_then_race(key, cwd):
        slot.project = "/new"  # a concurrent selection, mid-await
        return "/default/workspace"

    state.sessions.resolve_arm_cwd = _resolve_then_race

    result = asyncio.run(_set_project(state, slot, {"clear": True}))

    assert "changed while the clear resolved" in result
    assert slot.project == "/new"
    assert slot.project_cleared is False
    assert armed_keys == []


@pytest.mark.asyncio
async def test_a_cleared_slots_turn_hands_the_clear_to_allocation_unchanged():
    """A clear survives only by identity, so every claim the turn makes must carry it as-is."""
    from turn_harness import TurnScript, run_turn

    def clear(ctx):
        ctx.slot.project = ""
        ctx.slot.project_cleared = True

    record = await run_turn(TurnScript(setup=clear))
    assert record.allocations, "the turn allocated no session -- the assertion below is vacuous"
    assert [call.kwargs.get("cwd") is CWD_CLEARED for call in record.allocations] == [True] * len(
        record.allocations
    )


def test_the_marker_binds_a_clear_the_session_map_no_longer_records(tmp_path):
    """After a restart, a map entry rebound to the old project leaves only the marker to bind it."""
    from session_pool_helpers import _make_manager

    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project, slot.project_cleared = "", True
    _save(state, slot)
    mgr, factory = _make_manager()
    mgr._session_map.set(HISTORY_KEY, "", cwd=str(tmp_path))

    claim = _restored(tmp_path, "history").claim_cwd
    asyncio.run(mgr.get_or_create(HISTORY_KEY, agent="kirocrew", cwd=claim))
    assert factory.call_args.kwargs.get("cwd") is CWD_CLEARED


def test_an_empty_string_is_not_a_clear():
    """``x or ""`` must not read as a clear: the sentinel equals only itself."""
    assert CWD_CLEARED != ""
    assert "" != CWD_CLEARED
    assert CWD_CLEARED == CWD_CLEARED
    assert not CWD_CLEARED


def test_a_stale_cleared_marker_does_not_redirect_a_project_that_is_set(tmp_path):
    """The marker describes an EMPTY project, so a project that is set answers for itself."""
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project_cleared = True

    slot.project = "/new"
    assert slot.claim_cwd == "/new"

    # The other direction, so the gate cannot pass by ignoring the marker altogether.
    slot.project = ""
    assert slot.claim_cwd == CWD_CLEARED


def test_every_materializer_that_restores_a_project_restores_the_marker_too():
    """A materializer that copies the project alone re-loses the clear it just restored."""
    import ast
    import pathlib

    import kiro_crew.dashboard as dashboard_pkg

    def _restores_a_project(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            targets = [t for t in sub.targets if isinstance(t, ast.Attribute)]
            if not any(t.attr == "project" for t in targets):
                continue
            src = ast.unparse(sub.value)
            if src.startswith("meta[") or src.startswith("meta.get(") or src.endswith(".project"):
                return True
        return False

    def _sets_marker(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Attribute) and sub.attr == "project_cleared"
            for sub in ast.walk(node)
        )

    root = pathlib.Path(dashboard_pkg.__file__).parent
    materializers, missing = [], []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _restores_a_project(node):
                continue
            materializers.append(f"{path.name}:{node.name}")
            if not _sets_marker(node):
                missing.append(f"{path.name}:{node.name}")

    assert materializers, "no materializer found -- the assertion below is vacuous"
    assert not missing, f"restore the project without its marker: {missing}"
