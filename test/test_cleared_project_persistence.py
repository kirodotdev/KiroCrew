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
    """The save has TWO metadata paths, and only the message-less one was covered.

    An empty window reaches disk through a metadata MERGE; a slot holding a window --
    every conversation anyone has actually talked in -- rebuilds the line from scratch
    instead, so it persists exactly the fields it names and nothing else. A rebuild
    that names the directory alone restores as ``project_cleared=False``, ``claim_cwd``
    then states no directory at all, and the warm pool's stored-cwd override binds the
    turn to the project the user removed.
    """
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
    """The marker DESCRIBES ``project``, so the two must share an ownership class.

    Split, a line commits a pair matching neither slot -- one slot's directory beside
    another's verdict on whether it was removed -- and each half is separately valid, so
    nothing downstream can detect it. Membership is the whole mechanism here, which is
    why it is asserted directly rather than inferred from a round trip: the full-save
    rebuild now always names the key, so ownership never decides that path.
    """
    assert "project" in SLOT_OWNED_META_KEYS
    assert "project_cleared" in SLOT_OWNED_META_KEYS
    assert "project" in ROWS_ONLY_DEFERRED_META_KEYS
    assert "project_cleared" in ROWS_ONLY_DEFERRED_META_KEYS


def test_a_rows_only_write_leaves_the_holders_marker_alone(tmp_path):
    """A save that only needs its rows down must not restate another slot's project.

    A popped slot writing a handover row onto the line a live holder published rebuilds
    that line, and every slot-owned field is deferred back from disk so the holder's
    acknowledged state survives. Undeferred, THIS slot's marker lands beside the
    HOLDER's directory: the holder's project reads as removed and its next claim answers
    the default workspace.
    """
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
    """The marker has to be overwritable, not merely writable.

    Written only when true, a stale ``True`` would outlive the clear it recorded and
    every later restart would report a bound project as cleared.
    """
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
    """Only a literal ``True`` is a clear, because the record is a file on disk.

    A hand-edit or an older writer can leave the STRING ``"false"`` there, which is
    truthy. Read loosely, a slot that never had its project cleared comes back cleared:
    it drops its resume SID and its warm-pool hit, and binds the default workspace
    instead of the project it is still supposed to be in.
    """
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
    """The clear resolves the default workspace across an await, and must re-read after it.

    A selection landing in that window is NEWER than this clear. Committing anyway writes
    ``""`` over it and arms the default workspace against a key the user has just pointed
    somewhere else, so the next turn binds neither the old project nor the new one.
    """
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


def test_both_session_claims_state_the_slots_claim_cwd():
    """``claim_cwd`` only reaches the provider if the CLAIM passes it.

    The property can be correct, persisted and restored and still change nothing: the
    turn binds whatever cwd its ``get_or_create`` states, so a claim spelling
    ``slot.project`` discards the cleared sentinel on the way past and the session is
    bound to the directory the user removed.

    Scoped to the whole package, not the runner: ``CWD_CLEARED`` is the empty string, so
    the ``slot.project or ""`` idiom states a CLEAR -- bypassing the pool and dropping the
    persisted resume SID -- while reading like a harmless "no preference". Any module that
    grows a claim can spell it that way, so every claim naming a project anywhere must
    spell ``claim_cwd``.
    """
    import ast
    import pathlib

    import kiro_crew

    def _cwd_claims(tree: ast.AST) -> list[tuple[int, str]]:
        """Every ``get_or_create(cwd=...)`` in *tree*, with bare locals resolved.

        A claim usually states the expression inline, but one may bind it a statement
        earlier (``project = slot.project or None`` then ``cwd=project``). Read literally
        the second form hides whether the value can be the empty string, so a bare name is
        replaced by what the module assigns to it. Deliberately module-wide and
        many-valued: a name bound differently in two functions is checked against EVERY
        binding, so the pin errs toward firing rather than toward missing a site.
        """
        bound: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            if node.value is None:
                continue
            rendered = ast.unparse(node.value)
            for target in targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(rendered)

        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "get_or_create":
                continue
            for kw in node.keywords:
                if kw.arg != "cwd":
                    continue
                expr = ast.unparse(kw.value)
                resolved = bound.get(expr) if isinstance(kw.value, ast.Name) else None
                for candidate in dict.fromkeys(resolved or [expr]):
                    found.append((node.lineno, candidate))
        return found

    package = pathlib.Path(kiro_crew.__file__).parent
    runner_stated: list[tuple[int, str]] = []
    project_stated: list[tuple[str, int, str]] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, expr in _cwd_claims(tree):
            if path.name == "chat_runner.py":
                runner_stated.append((lineno, expr))
            if "project" in expr:
                project_stated.append((path.name, lineno, expr))

    assert runner_stated, "no get_or_create call states a cwd -- the assertion below is vacuous"
    assert [expr for _, expr in runner_stated] == ["slot.claim_cwd"] * len(
        runner_stated
    ), runner_stated
    assert project_stated, "no claim reaches a project at all -- the assertion below is vacuous"
    # A claim may legitimately state something other than a slot's project (an arm's own
    # directory, a workflow step's), and one that coalesces to ``None`` states "no
    # preference" -- the meaning a falsy cwd has always had. What no claim may do is hand
    # the boundary an expression that can EVALUATE to ``""``, because that spelling now
    # states a clear: it bypasses the pool and drops the persisted resume SID.
    sentinel_capable = [
        site
        for site in project_stated
        if site[2] != "slot.claim_cwd" and not site[2].endswith(" or None")
    ]
    assert sentinel_capable == [], sentinel_capable


def test_a_stale_cleared_marker_does_not_redirect_a_project_that_is_set(tmp_path):
    """The marker describes an EMPTY project, so a project that is set answers for itself.

    Nothing resets the marker at the ~18 sites that assign ``slot.project``, so an MCP clear
    followed by a dashboard selection leaves it set beside a real directory. Consulted
    ungated, the claim would state the default workspace and the turn's relative reads and
    writes would land outside the project just chosen. Gating the READ is what makes that
    unreachable, rather than trusting every assignment site to stay correct.
    """
    state = _make_state(tmp_path / "sessions")
    slot = _seeded_slot(state)
    slot.project_cleared = True

    slot.project = "/new"
    assert slot.claim_cwd == "/new"

    # The other direction, so the gate cannot pass by ignoring the marker altogether.
    slot.project = ""
    assert slot.claim_cwd == CWD_CLEARED


def test_every_materializer_that_restores_a_project_restores_the_marker_too():
    """A materializer that copies the project alone re-loses the clear it just restored.

    Five functions build a slot's project from persisted metadata or from another slot,
    and each guards the assignment on a truthy project -- which a cleared one, spelled
    ``""``, does not satisfy. So the marker cannot ride along inside that guard, and a
    materializer taking only the project leaves ``claim_cwd`` stating NO directory: the
    warm pool's stored-cwd override then binds the session to the directory the user
    removed. Asserted over the whole dashboard package, so a sixth materializer added
    later cannot reintroduce the loss.
    """
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
