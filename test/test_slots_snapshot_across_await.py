"""Build gate + tests: a slot-wide loop that awaits must iterate a SNAPSHOT.

``api_chat_folder_delete`` and ``api_chat_tag_delete`` walk every slot and
persist each one. Since the persist became ``await save_slot_off_loop(...)``,
each iteration contains a yield point — and ``state._slots`` is mutated by other
coroutines that can run during it (``session_transfer`` and ``session_control``
pop keys, ``openai_compat`` pops on its cleanup paths, ``get_or_create_slot``
assigns). Iterating the live ``.values()`` view across that yield raises
``RuntimeError: dictionary changed size during iteration``.

That failure is worse than a 500. Both loops mutate before they persist, so the
raise lands with the work half-applied and the compensating path skipped: the
folder delete abandons the unfile partway and never reaches
``_restore_unfiled``, and the tag delete has already removed the tag row, so
some slots keep a tag id whose vocabulary entry is gone.

The fix is one word per site (``list(...)``), which is the form the slot-wide
loops in ``state.py`` already use. The gate is here because the defect is
invisible at the call site: nothing about ``for slot in state._slots.values()``
looks wrong until you notice the ``await`` nested inside it, and the next
slot-wide loop someone writes will read just as naturally.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import threading
import time
from collections.abc import Callable

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_folder_app, _make_state, _make_tags_app

from kiro_crew.dashboard.chat_persistence import SweepMergeOutcome
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot

# A nested def/lambda is a different execution frame, so a loop inside one is
# not this loop; walk it on its own terms.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: The dict views that iterate ``_slots`` LAZILY, so a concurrent insert or pop
#: during the loop invalidates the iteration. ``keys`` is included for the same
#: reason as the other two even though no such loop exists today: the hazard is a
#: property of the view, not of which projection the loop happens to read, and a
#: gate that covers only the spellings already in the tree stops the defect that
#: was written and not the one that will be.
_LIVE_VIEW_ACCESSORS = frozenset({"values", "items", "keys"})


# ── STRUCTURAL tier ───────────────────────────────────────────────────────────


def _src_root() -> pathlib.Path:
    """Locate the kiro_crew source tree (import-first, repo-path fallback)."""
    try:
        import kiro_crew  # noqa: PLC0415

        return pathlib.Path(kiro_crew.__file__).resolve().parent
    except Exception:
        return pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _is_live_slots_view(node: ast.expr) -> bool:
    """True for a LAZY ``<anything>._slots`` view — i.e. NOT wrapped in ``list()``.

    Matching the attribute chain rather than a receiver name catches ``state``,
    ``self`` and ``ds`` alike, so the gate does not depend on what the local
    happens to be called. All of ``values()``, ``items()`` and ``keys()`` count:
    every one of them iterates the live dict, so the hazard is identical, and
    pinning a single spelling would let the next loop reintroduce the defect by
    reading a different projection of the same view.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LIVE_VIEW_ACCESSORS
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_slots"
    )


def _adopt_without_explicit_clear(source: str, rel: str) -> list[tuple]:
    """Report modules that adopt closed sessions but never clear the flag.

    Only meaningful once ``closed`` leaves ``SLOT_OWNED_META_KEYS`` -- see
    :func:`test_removing_closed_from_the_owned_set_requires_an_explicit_adopt_clear`,
    which is the sole caller and applies that precondition. Module-level rather than
    call-level on purpose: the adopt and the clear are routinely in different
    functions of the same module (restore decides, an offloaded helper writes), so a
    same-scope check would report a site that is in fact handled.
    """
    if "adopt_closed=True" not in source or "clear_closed" in source:
        return []
    # Skip in-package test trees (apps/builtins/*/tests/): a fixture stubbing the
    # adopt signature is not a production restore path, and reporting it would send
    # whoever takes the layer decision to edit a stub.
    if "/tests/" in rel or pathlib.Path(rel).name.startswith("test_"):
        return []
    return [(rel, source.count("adopt_closed=True"))]


def _scope_nodes(node: ast.AST):
    """Yield nodes reachable from *node* without crossing a nested scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        yield child
        yield from _scope_nodes(child)


def _awaits_in_scope(node: ast.AST):
    """Yield Await nodes reachable from *node* without crossing a nested scope."""
    for child in _scope_nodes(node):
        if isinstance(child, ast.Await):
            yield child


def find_violations(source: str, path: str = "<source>") -> list[tuple[str, int]]:
    """Return ``(path, lineno)`` for live-view slot loops that await inside."""
    tree = ast.parse(source)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        if not _is_live_slots_view(node.iter):
            continue
        if not any(_awaits_in_scope(node)):
            continue
        out.append((path, node.lineno))
    return out


def collect_repo_violations(
    find: Callable[[str, str], list[tuple]] = find_violations,
) -> list[tuple]:
    """Walk every ``kiro_crew/**/*.py`` and collect what *find* reports.

    ONE walk for every repo-wide gate in this file. The rglob/read/relativise/parse
    sequence — including which failures are skipped rather than raised — is the part
    that must not drift between gates: a second copy that silently stopped reading a
    subtree would make its gate pass vacuously, and the two copies gave no signal when
    they disagreed. Only the DETECTOR differs, so only the detector is a parameter.

    *find* takes ``(source, relative_path)`` and returns that rule's violation tuples;
    the shape is per-rule (the live-view gate reports ``(path, lineno)``, the tag-write
    gate ``(path, lineno, func, writer)``), which is why the annotation is a bare
    ``tuple``. Defaults to the live-view detector so the common call reads plainly.
    """
    root = _src_root()
    base = root.parent
    out: list[tuple] = []
    for py in sorted(root.rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        try:
            rel = str(py.relative_to(base))
        except ValueError:  # pragma: no cover - defensive
            rel = str(py)
        try:
            out.extend(find(src, rel))
        except SyntaxError:  # pragma: no cover - defensive
            continue
    return out


def test_no_slot_wide_loop_awaits_over_a_live_view() -> None:
    """A loop that awaits per slot must iterate ``list(...)``, not the view."""
    violations = collect_repo_violations()
    if violations:
        detail = "\n".join(f"  {path}:{lineno}" for path, lineno in violations)
        raise AssertionError(
            "a `for ... in <x>._slots.values()` loop contains an `await`.\n\n"
            "The await is a yield point inside the iteration, and other "
            "coroutines mutate _slots while it runs (session_transfer / "
            "session_control / openai_compat pop, get_or_create_slot assigns), "
            "so this raises 'dictionary changed size during iteration' with the "
            "loop's work half-applied. Iterate a snapshot instead:\n"
            "    for slot in list(state._slots.values()):\n"
            f"{detail}"
        )


def test_the_gate_scanned_a_non_empty_tree() -> None:
    """Positive control: an empty scan would make the gate above pass vacuously."""
    files = list(_src_root().rglob("*.py"))
    assert len(files) > 100, f"expected the kiro_crew tree, scanned {len(files)} files"


# ── Meta-tests: prove the detector fires and stays quiet ─────────────────────


def test_detector_flags_the_live_view_shape() -> None:
    src = (
        "async def f(state):\n"
        "    for slot in state._slots.values():\n"
        "        await save(slot)\n"
    )
    assert [v[1] for v in find_violations(src)] == [2]


def test_detector_accepts_the_snapshot_shape() -> None:
    src = (
        "async def f(state):\n"
        "    for slot in list(state._slots.values()):\n"
        "        await save(slot)\n"
    )
    assert find_violations(src) == []


def test_detector_ignores_a_live_view_with_no_await() -> None:
    """Without a yield point the live view is safe — that is the pre-#334 shape."""
    src = "def f(state):\n    for slot in state._slots.values():\n        save(slot)\n"
    assert find_violations(src) == []


def test_detector_matches_any_receiver_name() -> None:
    """``self``/``ds`` must be caught too, or the gate is one rename from useless."""
    src = (
        "async def f(self):\n"
        "    for slot in self._slots.values():\n"
        "        await save(slot)\n"
    )
    assert [v[1] for v in find_violations(src)] == [2]


def test_detector_flags_the_items_view_shape() -> None:
    """``.items()`` iterates the same live dict, so it carries the same hazard.

    Pinned because matching ``values()`` alone would make it a
    spelling rule rather than a property rule: a loop reading key and slot together
    could reintroduce the exact defect while the gate stayed green. The one live
    ``_slots.items()`` loop in the tree today sits in a synchronous ``def``, where an
    ``await`` is a syntax error, so widening the gate flags nothing now -- and that is
    precisely why it had to be widened before something makes that function async.
    """
    src = (
        "async def f(state):\n"
        "    for key, slot in state._slots.items():\n"
        "        await save(slot)\n"
    )
    assert [v[1] for v in find_violations(src)] == [2]


def test_detector_flags_the_keys_view_shape() -> None:
    """``.keys()`` is lazy too; a loop that awaits per key invalidates the same way."""
    src = (
        "async def f(state):\n"
        "    for key in state._slots.keys():\n"
        "        await save(state._slots[key])\n"
    )
    assert [v[1] for v in find_violations(src)] == [2]


def test_detector_accepts_a_snapshotted_items_view() -> None:
    """The remedy is the same for every accessor: wrap it in ``list()``."""
    src = (
        "async def f(state):\n"
        "    for key, slot in list(state._slots.items()):\n"
        "        await save(slot)\n"
    )
    assert find_violations(src) == []


def test_detector_ignores_a_lookalike_attribute(tmp_path=None) -> None:
    """A same-named accessor on a DIFFERENT attribute must not be flagged.

    The gate keys on the ``_slots`` chain, so widening the accessor set must not make
    it fire on every ``.items()`` in the codebase -- that would be a gate nobody can
    keep green, and it would be deleted rather than obeyed.
    """
    src = (
        "async def f(state):\n"
        "    for key, val in state._folders.items():\n"
        "        await save(val)\n"
    )
    assert find_violations(src) == []


def test_detector_ignores_an_await_in_a_nested_scope() -> None:
    """A nested def is a separate frame; its await does not yield this loop."""
    src = (
        "async def f(state):\n"
        "    for slot in state._slots.values():\n"
        "        async def _later():\n"
        "            await save(slot)\n"
        "        schedule(_later)\n"
    )
    assert find_violations(src) == []


# ── BEHAVIOURAL tier: drive the two real handlers ────────────────────────────


def _commit_vocabulary(state) -> None:
    """Publish ``state._folders`` as the COMMITTED folder vocabulary.

    Production publishes this in exactly two places -- ``load_folders`` when it
    parsed an existing file, and ``mutate_folders`` after a write confirms -- so a
    test that wants the validator ENABLED has to stand in for one of them. Derived
    from ``_folders`` rather than passed in so the fixture cannot drift from the
    list the same test then asserts against.

    ``None`` is the opposite state (vocabulary UNKNOWN) and tests set that
    directly, because there is no production call that publishes unknown-ness
    other than a reset.
    """
    state._committed_folder_ids = frozenset(
        f["id"] for f in state._folders if isinstance(f.get("id"), str) and f["id"]
    )


def _slot(key: str, **kw) -> _ChatSlot:
    slot = _ChatSlot(key)
    for attr, value in kw.items():
        setattr(slot, attr, value)
    return slot


def _popping_save(state, victim: str):
    """A ``save_slot_off_loop`` stand-in that mutates ``_slots`` mid-iteration.

    This is what makes the test deterministic rather than a race: the real
    concurrent popper (session_transfer, session_control, openai_compat) is
    modelled by popping during the awaited save, which is precisely the window
    the yield point opens. It also awaits, so the coroutine genuinely suspends.
    """
    popped: list[str] = []

    async def _fake(*args, **kwargs):
        await asyncio.sleep(0)
        if victim in state._slots:
            state._slots.pop(victim, None)
            popped.append(victim)

    return _fake, popped


@pytest.mark.asyncio
async def test_folder_delete_survives_a_concurrent_slot_pop(tmp_path, monkeypatch) -> None:
    """Unfiling every slot must not break when one is closed mid-loop."""
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, folder_id="f1")

    fake, popped = _popping_save(state, "c")
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    # The pop DID happen during the loop, so the hazard window was really open —
    # without this the test could pass by never reaching the mutation at all.
    assert popped == ["c"], "the concurrent pop never fired; the test proves nothing"
    assert resp.status == 200
    assert not any(f["id"] == "f1" for f in state._folders)
    # Slots that survived are unfiled; iteration completed rather than raising.
    assert state._slots["a"].folder_id == ""
    assert state._slots["b"].folder_id == ""


@pytest.mark.asyncio
async def test_tag_delete_survives_a_concurrent_slot_pop(tmp_path, monkeypatch) -> None:
    """Stripping a deleted tag id must not break when a slot closes mid-loop."""
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "T1", "color": "#111111", "order": 0}]
    state._tag_boards = []
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, tags=["t1"])

    fake, popped = _popping_save(state, "c")
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert popped == ["c"], "the concurrent pop never fired; the test proves nothing"
    assert resp.status == 200
    assert state._tags == []
    # The tag row is gone AND every surviving slot was stripped — a raise here
    # would have left some slot holding an id with no vocabulary entry.
    assert state._slots["a"].tags == []
    assert state._slots["b"].tags == []


# ── CONCURRENT CLOSE: the snapshot holds OBJECTS, so `list(...)` is not enough ──
#
# `list(...)` stops the RuntimeError, but the snapshot it takes is a snapshot of
# OBJECTS. Across the await another task can CLOSE a slot -- which pops it from
# `state._slots` and persists `closed=True` -- and the loop, still holding the
# pre-close object, mutates it and force-saves it straight back OVER that close.
# `closed` is slot-owned metadata where an absent field means "cleared", so the
# close is ERASED and the dismissed tab returns on the next restore.
#
# A test that merely deletes a folder and checks the remaining slots PASSES UNDER
# DEFECT AND FIX ALIKE. The assertion carrying the novel coverage is the one about
# the CONCURRENTLY-CLOSED slot: its `closed` metadata must SURVIVE the delete. So
# these model the persisted metadata rather than counting calls, and they fail
# pre-fix with an erased-close assertion, NOT with a RuntimeError.


def _closing_save(state, victim: str, persisted: dict):
    """A ``save_slot_off_loop`` stand-in that CLOSES ``victim`` mid-iteration.

    Models the real close path (`chat_handlers.api_chat_slot_delete`): pop the
    slot from ``state._slots``, then persist ``closed=True``. It fires while the
    loop is suspended at its FIRST await, i.e. inside the hazard window, so the
    interleaving is deterministic rather than raced.

    Every save records what it persisted, so an ordinary (non-closed) save landing
    after the close is visible as ``closed`` going back to False -- which is the
    data loss, expressed exactly as the user experiences it.
    """
    fired: list[str] = []

    async def _fake(_state, slot, *args, closed: bool = False, **kwargs):
        await asyncio.sleep(0)
        if not fired and victim in state._slots:
            # The concurrent close, landing INSIDE the await window.
            state._slots.pop(victim, None)
            persisted[victim] = {"closed": True}
            fired.append(victim)
        persisted[slot.key] = {"closed": closed}

    return _fake, fired


@pytest.mark.asyncio
async def test_folder_delete_does_not_erase_a_concurrent_close(tmp_path, monkeypatch) -> None:
    """A slot closed mid-unfile keeps its close; the delete must not resurrect it."""
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    # Insertion order is the snapshot order, so "a" is processed first and its
    # await is the window the close lands in; "c" is reached afterwards.
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, folder_id="f1")

    persisted: dict[str, dict] = {}
    fake, fired = _closing_save(state, "c", persisted)
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    # Guard: the close must really have landed inside the window, or the novel
    # assertion below is vacuous.
    assert fired == ["c"], "the concurrent close never fired; the test proves nothing"
    assert resp.status == 200
    # THE NOVEL ASSERTION. Pre-fix the loop reaches the popped object, clears its
    # folder_id and force-saves it, rewriting closed=False over the close.
    assert persisted["c"] == {"closed": True}, (
        "the concurrently-closed slot was force-saved after its close, erasing "
        "closed metadata — the dismissed tab returns after restart"
    )
    # The feature still works for the slots that were NOT closed, so the identity
    # guard is not simply skipping everything.
    assert state._slots["a"].folder_id == ""
    assert state._slots["b"].folder_id == ""


@pytest.mark.asyncio
async def test_tag_delete_does_not_erase_a_concurrent_close(tmp_path, monkeypatch) -> None:
    """A slot closed mid-strip keeps its close; the tag delete must not resurrect it."""
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "T1", "color": "#111111", "order": 0}]
    state._tag_boards = []
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, tags=["t1"])

    persisted: dict[str, dict] = {}
    fake, fired = _closing_save(state, "c", persisted)
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert fired == ["c"], "the concurrent close never fired; the test proves nothing"
    assert resp.status == 200
    assert persisted["c"] == {"closed": True}, (
        "the concurrently-closed slot was force-saved after its close, erasing "
        "closed metadata — the dismissed tab returns after restart"
    )
    assert state._slots["a"].tags == []
    assert state._slots["b"].tags == []


# ── TRANSIENT absence: a close that FAILS and is restored ────────────────────
#
# Absence from ``state._slots`` does NOT prove a close committed. The close path
# pops the slot, saves with ``best_effort=False``, and RESTORES it in the except
# arm when that save raises ("Save failed — restore slot so data isn't lost").
# So a loop that skipped the whole body on absence left a merely transient
# absentee still holding the deleted ``folder_id`` once it came back — a dangling
# folder reference, while the folder removal had already committed.
#
# The fix splits the two operations the single guard conflated: mutate in memory
# unconditionally (writes nothing, so it cannot erase a close) and gate only the
# PERSIST on identity. These tests pin the split from both sides.


def _popping_then_restored_save(state, victim: str, persisted: dict, holder: dict):
    """A save stand-in modelling a close that pops ``victim`` and later fails.

    The pop lands inside the FIRST await, i.e. inside the hazard window, so the
    interleaving is deterministic. The slot is handed to ``holder`` so the test
    can restore it afterwards exactly as the close handler's except arm does.
    """
    fired: list[str] = []

    async def _fake(_state, slot, *args, closed: bool = False, **kwargs):
        await asyncio.sleep(0)
        if not fired and victim in state._slots:
            holder[victim] = state._slots.pop(victim)
            fired.append(victim)
        persisted[slot.key] = {"closed": closed}

    return _fake, fired


@pytest.mark.asyncio
async def test_folder_delete_clears_a_transiently_absent_slot(tmp_path, monkeypatch) -> None:
    """A slot whose close FAILS must not come back holding the deleted folder."""
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, folder_id="f1")

    persisted: dict[str, dict] = {}
    holder: dict[str, object] = {}
    fake, fired = _popping_then_restored_save(state, "c", persisted, holder)
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert fired == ["c"], "the concurrent pop never fired; the test proves nothing"
    assert resp.status == 200
    # The folder removal COMMITTED, so no slot may still name it.
    assert not any(f["id"] == "f1" for f in state._folders)

    # The close's own save failed, so the close handler puts the slot back.
    state._slots["c"] = holder["c"]

    # THE NOVEL ASSERTION. Pre-fix the loop skipped the whole body on absence, so
    # the restored slot still pointed at a deleted folder.
    dangling = state._slots["c"].folder_id
    assert dangling == "", f"restored slot holds deleted folder_id={dangling!r} (dangling ref)"
    # The clear must also be able to reach disk once the slot is live again.
    armed = state._slots["c"]._dirty
    assert armed is True, "in-memory clear not armed for the periodic flush; lost on restart"
    # And the close must NOT have been written over: no persist for that slot.
    assert "c" not in persisted, "pre-close object was force-saved; that erases a close"


@pytest.mark.asyncio
async def test_tag_delete_strips_a_transiently_absent_slot(tmp_path, monkeypatch) -> None:
    """Same split for the tag strip: a restored slot must not keep a dead tag."""
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "T1", "color": "#111111", "order": 0}]
    state._tag_boards = []
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, tags=["t1"])

    persisted: dict[str, dict] = {}
    holder: dict[str, object] = {}
    fake, fired = _popping_then_restored_save(state, "c", persisted, holder)
    monkeypatch.setattr(mod, "save_slot_off_loop", fake)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=fake, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert fired == ["c"], "the concurrent pop never fired; the test proves nothing"
    assert resp.status == 200
    assert state._tags == []

    state._slots["c"] = holder["c"]
    left = state._slots["c"].tags
    assert left == [], f"restored slot holds deleted tag id(s)={left!r}"
    assert "c" not in persisted, "pre-close object was force-saved; that erases a close"


@pytest.mark.asyncio
async def test_committed_unfile_is_armed_for_the_flush(tmp_path, monkeypatch) -> None:
    """Converse of the above: once the removal COMMITS, the clear must be armed.

    Without this, deferring the arm would silently drop the clear's durability
    and a returning slot would render unfiled but reload into a dead folder.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    for key in ("a", "b", "c"):
        state._slots[key] = _slot(key, folder_id="f1")

    holder: dict[str, object] = {}
    fired: list[str] = []

    async def _fake_save(_state, slot, *args, **kwargs):
        await asyncio.sleep(0)
        if not fired and "c" in state._slots:
            holder["c"] = state._slots.pop("c")
            fired.append("c")

    monkeypatch.setattr(mod, "save_slot_off_loop", _fake_save)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_fake_save, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert fired == ["c"], "the concurrent pop never fired; the test proves nothing"
    assert resp.status == 200
    assert not any(f["id"] == "f1" for f in state._folders)
    slot = holder["c"]
    assert slot.folder_id == "", "the clear must survive on the withheld object"
    assert slot._dirty is True, (
        "a COMMITTED clear was left unflushable, so the returning slot would "
        "reload pointing at the deleted folder"
    )


@pytest.mark.asyncio
async def test_slot_published_after_the_snapshot_is_still_unfiled(tmp_path, monkeypatch) -> None:
    """A slot filed into the folder DURING the handler's awaits must not dangle.

    The sweep takes ``list(state._slots.values())`` after the removal has
    committed, so the window it must cover is a slot published while
    ``mutate_folders`` is still in flight. The dangling folder_id that would
    result is durable rather than self-healing: the loader reads folder_id
    without checking it against the folder list. ``api_chat_slot_folder``
    refusing a file into an unknown folder with 400 closes the HTTP path, but
    NOT channel surfacing, which takes folder_id straight off a persisted
    metadata line -- so the sweep runs AFTER the removal has committed, and every
    copy site validates the id it publishes rather than the sweep chasing arrivals.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    state._slots["a"] = _slot("a", folder_id="f1")

    published: list[str] = []
    real_mutate = state.mutate_folders

    async def _noop_save(_state, slot, *args, **kwargs):
        await asyncio.sleep(0)

    async def _publish_then_commit(fn):
        # A concurrent request creates and files a slot while the removal is still
        # in flight. This is the publication that precedes the sweep's snapshot.
        state._slots["late"] = _slot("late", folder_id="f1")
        published.append("late")
        return await real_mutate(fn)

    monkeypatch.setattr(mod, "save_slot_off_loop", _noop_save)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_noop_save, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
    monkeypatch.setattr(state, "mutate_folders", _publish_then_commit)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert published == ["late"], "no slot was published mid-flight; test proves nothing"
    assert resp.status == 200
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"

    late = state._slots["late"].folder_id
    assert late == "", (
        f"slot published after the snapshot still names the deleted folder "
        f"(folder_id={late!r}); the loader does not validate it, so this is durable"
    )


@pytest.mark.asyncio
async def test_failed_folder_delete_does_not_durably_unfile_a_closing_slot(
    tmp_path, monkeypatch
) -> None:
    """A folder write that FAILS must leave no conversation recorded as unfiled.

    The clear and the persist run only AFTER ``mutate_folders`` commits, so a
    failed folder write cannot mutate a slot at all. Before that ordering, the
    unfile loop cleared ``folder_id`` on the slot OBJECT while a concurrent close
    was still serialising it, and then withheld its own persist on the identity
    gate -- so the close wrote the cleared value and the rollback, gated the same
    way, could not repair it. Folder present, conversation durably Unfiled.

    The close is modelled inside the ``mutate_folders`` call: it pops the slot and
    serialises the object AS IT THEN STANDS, and only then does the folder store
    fail. That window exists in both orderings, so the test discriminates them
    rather than depending on a pre-commit persist that the fix removes.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot_a = _slot("a", folder_id="f1")
    state._slots["a"] = slot_a

    log = state.conversation_log
    key_a = slot_history_key(slot_a)
    # Pre-existing durable truth: "a" lives in f1.
    log.update_metadata(key_a, {"folder_id": "f1"})
    assert log.get_metadata(key_a).get("folder_id") == "f1", "fixture precondition"

    async def _noop_persist(_state, _slot_arg, *args, **kwargs):
        return None

    async def _close_writes_then_folder_store_fails(fn):
        # The concurrent close completes HERE: it pops the slot and serialises the
        # object as it currently stands. Only then does the folder store fail.
        state._slots.pop("a", None)
        log.update_metadata(key_a, {"folder_id": slot_a.folder_id, "closed": True})
        raise OSError("folder store write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _noop_persist)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_noop_persist, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
    monkeypatch.setattr(state, "mutate_folders", _close_writes_then_folder_store_fails)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 500, "the folder write failed, so the request must not succeed"
    # Negative controls: the pop and the close write both really happened, so the
    # hazard window was opened rather than the test passing vacuously.
    assert (
        state._slots.get("a") is not slot_a
    ), "the concurrent close never popped 'a'; the hazard window was not opened"
    assert (
        log.get_metadata(key_a).get("closed") is True
    ), "the close never serialised the slot; the hazard window was not opened"
    assert any(
        f["id"] == "f1" for f in state._folders
    ), "fixture: the folder must still be present after the failed write"
    assert log.get_metadata(key_a).get("folder_id") == "f1", (
        "the folder write FAILED and f1 is still present, but the conversation is "
        "durably recorded as unfiled: the delete mutated the slot before the commit, "
        "so the close serialised the cleared folder_id and the rollback could not "
        "repair an absent slot"
    )


@pytest.mark.asyncio
async def test_folder_delete_sweeps_every_slot_before_the_first_save_awaits(
    tmp_path, monkeypatch
) -> None:
    """No matching slot may still name the folder once the sweep starts awaiting.

    The sweep's ``await`` is a yield point, so anything that copies slot metadata
    while it runs -- a fork, for instance -- reads whatever the snapshot has not
    reached yet. Clearing one slot, awaiting its save, then clearing the next
    leaves every later slot still naming the deleted folder for the duration of
    that await, and a copy taken then persists the dangling reference OUTSIDE the
    snapshot, where no later pass will sweep it.

    So the clear must be a first pass over all matching slots with no await in
    it, and the persists a second pass.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    # Insertion order is the sweep order: "a" is persisted first, so "b" is the
    # slot a concurrent reader can still catch unswept.
    state._slots["a"] = _slot("a", folder_id="f1")
    state._slots["b"] = _slot("b", folder_id="f1")

    log = state.conversation_log
    fork_key = "dashboard:fork-of-b"
    forked: list[str] = []

    async def _save_then_fork_copies_b(_state, slot, *args, **kwargs):
        await asyncio.sleep(0)
        if forked:
            return
        # A concurrent fork copies "b"'s metadata while this save is suspended.
        source = state._slots["b"]
        log.update_metadata(fork_key, {"folder_id": source.folder_id})
        forked.append("b")

    monkeypatch.setattr(mod, "save_slot_off_loop", _save_then_fork_copies_b)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_save_then_fork_copies_b, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- holds on unfixed code too, so a vacuous green is visible:
    # the fork genuinely ran inside the await window and genuinely wrote a record.
    assert forked == ["b"], "the fork never ran inside the await window; test proves nothing"
    assert "folder_id" in log.get_metadata(fork_key), "the fork wrote no metadata to copy from"
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"

    copied = log.get_metadata(fork_key).get("folder_id")
    assert copied == "", (
        f"a fork copied folder_id={copied!r} from a slot the sweep had not reached, so a "
        "reference to the deleted folder is now durable outside the snapshot; clear every "
        "matching slot before the first await, then persist in a second pass"
    )


@pytest.mark.asyncio
async def test_tag_delete_sweeps_every_slot_before_the_first_save_awaits(
    tmp_path, monkeypatch
) -> None:
    """Same two-pass requirement for the tag strip, for the same reason."""
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    tag = {"id": "t1", "name": "urgent", "color": "#ff0000"}
    state._tags = [tag]
    state._slots["a"] = _slot("a", tags=["t1"])
    state._slots["b"] = _slot("b", tags=["t1"])

    log = state.conversation_log
    fork_key = "dashboard:fork-of-b"
    forked: list[str] = []

    async def _save_then_fork_copies_b(_state, slot, *args, **kwargs):
        await asyncio.sleep(0)
        if forked:
            return
        source = state._slots["b"]
        log.update_metadata(fork_key, {"tags": list(source.tags)})
        forked.append("b")

    monkeypatch.setattr(mod, "save_slot_off_loop", _save_then_fork_copies_b)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_save_then_fork_copies_b, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- holds on unfixed code too.
    assert forked == ["b"], "the fork never ran inside the await window; test proves nothing"
    assert "tags" in log.get_metadata(fork_key), "the fork wrote no metadata to copy from"
    assert all(t["id"] != "t1" for t in state._tags), "the tag delete committed"

    copied = log.get_metadata(fork_key).get("tags")
    assert copied == [], (
        f"a fork copied tags={copied!r} from a slot the sweep had not reached, so a "
        "reference to the deleted tag is now durable outside the snapshot; strip every "
        "matching slot before the first await, then persist in a second pass"
    )


@pytest.mark.asyncio
async def test_folder_delete_prunes_a_metadata_surfaced_slot_at_the_source(
    tmp_path, monkeypatch
) -> None:
    """A slot surfaced from STALE METADATA during the persist pass is handled at its source.

    Scoped deliberately to the ``meta["folder_id"]`` arrival path, which is the reason
    the sweep repeated: ``reconcile_channel_slots`` reads session
    metadata off disk across its own await and the surfacing helper took
    ``meta["folder_id"]`` verbatim, publishing through ``get_or_create_slot``
    without consulting ``state._folders``. So a stale metadata line could hand a
    deleted folder_id to a brand-new slot that arrived after the snapshot.

    That copy site now validates against the vocabulary, so an arrival on THIS path
    is already unfiled and a second pass would find nothing. What changed is which
    layer provides the guarantee -- validation at the source rather than a chase
    from the consumer.

    The DEFAULT-FILING branch of the same helper was the last arrival the repeat
    still covered, and it is covered at its source too now: see
    ``test_default_filed_slot_surfaced_during_persistence_is_pruned_at_assignment``,
    which pins that such an arrival is pruned at assignment. With no producer left,
    the sweep is a single round.

    The injection therefore goes through the REAL ``surface_channel_session``. A
    test that modelled the copy inline would bypass the prune and keep demanding
    the repeat, proving only that the model was stale.
    """
    from kiro_crew.dashboard import channel_slots
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    # load_folders() publishes this at boot, so True is the production state.
    _commit_vocabulary(state)
    state._slots["a"] = _slot("a", folder_id="f1")

    # The stale persisted metadata line a channel session would be surfaced from.
    stale_meta = {"folder_id": "f1", "agent": ""}
    surfaced: list[str] = []
    persisted: list[str] = []

    async def _persist_then_surface_a_channel_slot(_state, slot, *args, **kwargs):
        persisted.append(slot.key)
        await asyncio.sleep(0)
        if surfaced:
            return
        # Models reconcile_channel_slots completing across its own await, through
        # the real surfacing helper so the prune actually applies.
        late = channel_slots.surface_channel_session(
            state,
            {"key": "slack:9.9", "title": "", "modified": 1_700_000_000.0},
            stale_meta,
            [{"role": "user", "content": "hi"}],
        )
        surfaced.append(late.key if late is not None else "")

    monkeypatch.setattr(mod, "save_slot_off_loop", _persist_then_surface_a_channel_slot)

    async def _merge_shim(_st, _sl, _fields, __f=_persist_then_surface_a_channel_slot, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- the surfacing genuinely happened inside the persist
    # window, so a vacuous green is visible.
    assert surfaced and surfaced[0], "no slot was surfaced mid-persist; test proves nothing"
    late_key = surfaced[0]
    assert late_key in state._slots, "the surfaced slot never entered _slots"
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"

    # THE GUARANTEE, NARROWED: the sweep covers every slot in its snapshot, while a slot
    # arriving later from stale metadata keeps a VISIBLE dangling id. See history.md.
    swept = [s.key for s in state._slots.values() if s.key != late_key and s.folder_id == "f1"]
    assert not swept, (
        f"a slot the sweep COULD see still names the deleted folder ({swept}); the sweep "
        "itself must clear every slot in its snapshot"
    )
    assert state._slots[late_key].folder_id == "f1", (
        "the late-arriving slot was unfiled on arrival. That is the durable-mass-unfile "
        "path: this handler reads a persisted folder_id it cannot date, so a "
        "readable-but-stale folders.json would unfile channel conversations whose folders "
        "still exist. A visible dangling id here is the deliberate lesser harm -- it "
        "renders in the sidebar's Unfiled bucket and the resume path clears it under the "
        "in-lock existence verdict."
    )


@pytest.mark.asyncio
async def test_default_filed_slot_surfaced_during_persistence_is_pruned_at_assignment(
    tmp_path, monkeypatch
) -> None:
    """A default-filed slot arriving mid-persist must not keep the deleted folder id.

    RE-POINTED TWICE, and the second time INVERTED -- read this before trusting the
    name. The first version relied on the default-filing branch assigning its
    caller's folder verbatim; that branch then began revalidating, so the test was
    re-pointed onto the one window the revalidation declined to judge: an arrival
    landing while the folder store lock was held, where ``folder_id_for_restore``
    failed open by way of a ``_folders_lock.locked()`` probe and ONLY a later sweep
    pass could clear the id.

    That window is closed. The validator reads ``_committed_folder_ids``,
    which the delete's own ``mutate_folders`` has already updated by the time any
    arrival lands, so the deleted id is pruned AT ASSIGNMENT and the lock is
    irrelevant. The premise control below therefore asserts the OPPOSITE of the
    retired probe's expectation, and that inversion is the point: it is the observable consequence of
    retiring the probe.

    SO THIS TEST DOES NOT EXERCISE A REPEAT, and nothing here should be read as
    covering one. What it pins is the assignment-time prune, and that prune is what
    retired the repeat: this was the last arrival a second round still had to catch,
    so the sweep is now a SINGLE round. The two-pass split inside that round survives
    and is a different property -- pass one is yield-free so no fork can observe a
    half-swept set, pass two does all the awaiting -- asserted by the pass-one tests
    above and NOT by this one.
    """
    from kiro_crew.dashboard import channel_slots
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    # load_folders() publishes this at boot, so True is the production state.
    _commit_vocabulary(state)
    state._slots["a"] = _slot("a", folder_id="f1")

    # NO folder_id on the record and none of the filing markers, so
    # needs_default_filing() is True and the default-filing branch is what runs.
    unfiled_meta = {"agent": ""}
    surfaced: list[str] = []
    kept_deleted_id: list[bool] = []

    async def _persist_then_surface_default_filed(_state, slot, *args, **kwargs):
        await asyncio.sleep(0)
        if surfaced:
            return
        # Models reconcile_channel_slots resuming after its persist await, still
        # carrying the default folder it resolved BEFORE the delete committed -- and
        # landing while a CONCURRENT folder mutation holds the store lock, which used
        # to make the revalidation fail open. Through the REAL helper, so the outcome
        # is the code's property and not this test's model of it.
        async with state._folders_lock:
            late = channel_slots.surface_channel_session(
                state,
                {"key": "slack:9.9", "title": "", "modified": 1_700_000_000.0},
                unfiled_meta,
                [{"role": "user", "content": "hi"}],
                folder_id="f1",
            )
            # Recorded INSIDE the lock, which is exactly where the old probe would
            # have failed open: proves whether the arrival kept the deleted id.
            kept_deleted_id.append(late is not None and late.folder_id == "f1")
        surfaced.append(late.key if late is not None else "")

    monkeypatch.setattr(mod, "save_slot_off_loop", _persist_then_surface_default_filed)

    async def _merge_shim(_st, _sl, _fields, __f=_persist_then_surface_default_filed, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- the arrival genuinely happened inside the persist window,
    # so a vacuous green is visible rather than passing as a fix.
    assert surfaced and surfaced[0], "no slot was surfaced mid-persist; test proves nothing"
    late_key = surfaced[0]
    assert late_key in state._slots, "the surfaced slot never entered _slots"
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"
    # POSITIVE CONTROL on the PATH -- proves this arrival took the unpruned
    # default-filing branch, not the already-covered metadata branch.
    assert getattr(state._slots[late_key], "_channel_folder_filed", False), (
        "the arrival did not take the default-filing branch; this test would then be "
        "re-covering the metadata path its sibling already covers"
    )
    # PREMISE CONTROL, INVERTED by the committed-snapshot change. The arrival lands
    # inside a held folder-store lock, which is precisely where the retired
    # A ``locked()`` probe fails open and hands the deleted id through, so it is not a
    # substitute for pruning at assignment: the delete's own confirmed write already
    # dropped
    # f1 from the committed vocabulary, and the lock says nothing about that. If this
    # ever reads True the probe -- or an equivalent live-list read -- is back.
    assert kept_deleted_id == [False], (
        "the arrival KEPT the deleted folder id while the store lock was held; the "
        "validator is reading uncommitted state again instead of the committed "
        "vocabulary snapshot"
    )

    still_naming = [s.key for s in state._slots.values() if s.folder_id == "f1"]
    assert not still_naming, (
        f"slots still name the deleted folder after the handler ({still_naming}); "
        "a slot default-filed into it during the persist pass lands outside the first "
        "snapshot, so the sweep must repeat until an await-free pass finds none"
    )


@pytest.mark.asyncio
async def test_folder_delete_clears_a_slot_popped_during_the_folder_write(
    tmp_path, monkeypatch
) -> None:
    """A slot popped while the folder store is being written must still be unfiled.

    Committing the folder removal first closed the hazard where a FAILED write
    mutated a closing slot, but it moved the sweep's snapshot after the write --
    and a concurrent close pops its slot for the whole of that write. Such a slot
    is in no sweep's snapshot at all, so the bounded loop does not reach it
    either: the first pass finds nothing left to clear and terminates. When the
    close's own save then fails, ``chat_handlers.py:3516`` puts the very same
    object back into ``_slots`` still naming the folder that has just been
    deleted, and ``folder_id`` is read back unvalidated at load
    (``chat_persistence.py:577``), so the dangling reference is durable.

    Capturing the matching slot objects BEFORE the commit fixes it without
    reopening the earlier hazard, because a capture only reads: a failed folder
    write still mutates nothing.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot_a = _slot("a", folder_id="f1")
    state._slots["a"] = slot_a

    popped: list[str] = []
    real_mutate = state.mutate_folders

    async def _close_pops_during_the_folder_write(fn):
        # The concurrent close reaches its pop (chat_handlers.py:3488) while the
        # folder store write is in flight, so the slot is absent from _slots for
        # the entire post-commit sweep. The removal itself still commits.
        state._slots.pop("a", None)
        popped.append("a")
        return await real_mutate(fn)

    async def _noop_persist(_state, _slot_arg, *args, **kwargs):
        return None

    monkeypatch.setattr(mod, "save_slot_off_loop", _noop_persist)

    # The sweeps persist via the metadata-only merge now; route the same
    # injection through it so the concurrency window is still opened.
    async def _merge_shim(_st, _sl, _fields, __f=_noop_persist, **_kw):
        await __f(_st, _sl)
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
    monkeypatch.setattr(state, "mutate_folders", _close_pops_during_the_folder_write)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    absent_at_return = state._slots.get("a") is None
    # The close's save fails, so the handler restores the slot object it popped.
    state._slots["a"] = slot_a

    assert resp.status == 200
    # NEGATIVE CONTROLS -- each holds on unfixed code too, so a vacuous green is
    # visible: the pop really happened, the slot really was absent for the whole
    # sweep, and the folder removal really committed.
    assert popped == ["a"], "the close never popped the slot; the hazard window was not opened"
    assert absent_at_return, "the slot was still in _slots, so the sweep could have seen it"
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"

    assert slot_a.folder_id == "", (
        f"the folder is gone but the restored conversation still names it "
        f"(folder_id={slot_a.folder_id!r}); it was popped for the whole of the folder "
        "write, so no post-commit snapshot contained it -- capture the matching slots "
        "before the commit and include them in the clear pass"
    )
    assert slot_a._dirty is True, (
        "the restored slot was cleared in memory but the flush was not armed, so the "
        "stale folder_id survives on disk until something else happens to save it"
    )


# ── Loader-side folder_id validation (the root fix the sweep machinery
# compensates for): a dangling folder_id must not survive a restart, mirroring
# the tag vocabulary prune that already heals the tag side of the same class. ──


def _log_session(state, key: str, meta: dict) -> None:
    """Put one session on disk with *meta* so a restore path can read it back."""
    log = state.conversation_log
    log.append(key, "user", "hello")
    log.update_metadata(key, meta)


@pytest.mark.asyncio
async def test_a_handler_cancelled_during_the_sweep_still_drains_it() -> None:
    """The drain, exercised directly: cancelling mid-sweep must not truncate it.

    The delete-handler tests above cannot reach this path. There the cancellation comes
    from the COMMIT, so the helper is awaited by a task with no pending cancellation and
    the shield returns normally. The drain is for the other shape -- a client disconnect
    landing while the sweep itself is suspended -- which is when truncating it would leave
    some slots swept and the rest still naming the deleted row.
    """
    from kiro_crew.dashboard.snapshot_commit import sweep_to_completion_despite_cancellation

    started = asyncio.Event()
    finished: list[str] = []

    async def _sweep() -> None:
        started.set()
        await asyncio.sleep(0.02)
        finished.append("swept")

    task = asyncio.ensure_future(sweep_to_completion_despite_cancellation(_sweep()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["swept"], (
        "the sweep was truncated by the cancellation; a partially swept delete leaves some "
        "slots still naming the removed row"
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_while_draining_does_not_truncate_the_sweep() -> None:
    """The ``continue`` arm: an already-cancelled task gets a fresh cancellation per await.

    Without it the second cancellation escapes the drain and the caller unwinds with the
    sweep still running -- so this pins the drain's repeated await, not just a single one.

    Keyed on the caller STILL BEING PENDING mid-sweep rather than on the sweep completing.
    The sweep runs as a detached task, so it finishes either way: asserting only that it
    finished would pass with the drain deleted, which is the vacuous shape this avoids.
    """
    from kiro_crew.dashboard.snapshot_commit import sweep_to_completion_despite_cancellation

    started = asyncio.Event()
    finished: list[str] = []

    async def _sweep() -> None:
        started.set()
        for _ in range(8):
            await asyncio.sleep(0)
        finished.append("swept")

    task = asyncio.ensure_future(sweep_to_completion_despite_cancellation(_sweep()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    # The second delivery lands on the drain's own await, which is the arm under test.
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), (
        "the caller unwound while the sweep was still running -- a repeated cancellation "
        "escaped the drain, so the loop is not holding until the sweep completes"
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["swept"], "the sweep never completed"


@pytest.mark.asyncio
async def test_a_sweep_failure_supersedes_the_cancellation() -> None:
    """The sweep's own error must reach the caller, not be masked by the cancellation.

    We arrive in the drain having never seen the sweep's exception, so re-raising the
    cancellation instead would discard it and leave a caller's ``except Exception``
    unreached -- the same rule the sibling commit helper states for a failed write.
    """
    from kiro_crew.dashboard.snapshot_commit import sweep_to_completion_despite_cancellation

    started = asyncio.Event()

    async def _sweep() -> None:
        started.set()
        await asyncio.sleep(0.02)
        raise RuntimeError("the sweep itself failed")

    task = asyncio.ensure_future(sweep_to_completion_despite_cancellation(_sweep()))
    await started.wait()
    task.cancel()

    with pytest.raises(RuntimeError, match="the sweep itself failed"):
        await task


@pytest.mark.asyncio
async def test_the_sweep_scrubs_the_transcript_pass_one_found_not_the_rebound_one(
    tmp_path, monkeypatch
) -> None:
    """The sweep pins its transcript key across the pass-two await.

    Pass one records the slot; pass two awaits the merge, which must not resolve
    ``slot_history_key(slot)`` at write time. A concurrent rebind landing in that window
    therefore retargeted the scrub onto the NEW transcript and left the deleted folder id
    on the record that actually carries it -- durable, because the sweep never revisits.

    Every other write site in these handlers already pins ``expected_history_key``; the
    sweep was the one that resolved routing late.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    original_key = slot_history_key(slot)
    _log_session(state, original_key, {"folder_id": "f1"})

    real_merge = cp._merge_slot_meta
    merged_keys: list[str] = []

    async def _rebind_then_merge(st, sl, fields, *, guard, expected_history_key):
        # The concurrent rebind: routing moves while pass two is suspended.
        sl.key = "a-rebound"
        merged_keys.append(expected_history_key)
        return await real_merge(
            st, sl, fields, guard=guard, expected_history_key=expected_history_key
        )

    monkeypatch.setattr(cp, "_merge_slot_meta", _rebind_then_merge)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    assert merged_keys == [original_key], (
        f"the merge targeted {merged_keys!r} rather than the transcript pass one found "
        f"({original_key!r}); a rebind across the pass-two await retargeted the scrub"
    )
    meta = state.conversation_log.get_metadata(original_key)
    assert meta.get("folder_id") == "", (
        f"the transcript pass one found still names the deleted folder "
        f"(folder_id={meta.get('folder_id')!r}). The scrub landed on the rebound "
        "transcript instead, so the dangling reference is durable. Capture the history key "
        "in pass one and pin the merge to it."
    )


@pytest.mark.asyncio
async def test_a_close_failure_restore_prunes_a_tag_deleted_inside_its_own_window(
    tmp_path, monkeypatch
) -> None:
    """The tag mirror of the folder transition above, and the arm that keeps the fix honest.

    Its sibling's tag is already absent when the close starts, so that fixture passes
    whether this restore validates tags at all. Here the tag is COMMITTED as the close
    begins and is deleted while the save is in flight, which is the only shape that
    distinguishes a real delete from a snapshot that simply predates the assignment -- so
    the restore owes a prune, and without this nothing asserts that it still happens.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#111111", "order": 0}]
    state.publish_committed_tag_ids(state._tags)
    assert state._committed_tag_ids == frozenset({"t-doomed"}), (
        "fixture: the tag must be COMMITTED before the close, or this exercises the "
        "already-absent arm and proves nothing about the transition"
    )

    slot = _slot("a", tags=["t-doomed"])
    state._slots["a"] = slot

    async def _delete_the_tag_then_fail(*_a, **_kw):
        state._tags = []
        state.publish_committed_tag_ids(state._tags)
        raise RuntimeError("history write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _delete_the_tag_then_fail)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    restored = state._slots.get("a")
    assert restored is slot, (
        "fixture: the failed save must have restored the popped slot, or the restore arm "
        "under test never ran"
    )
    assert restored.tags == [], (
        f"the restore kept tags={restored.tags!r} after the tag was deleted inside the "
        "close's own save window. The vocabulary was observed BEFORE the await, so this is "
        "a provable committed-present -> committed-absent transition and the id must be "
        "dropped, not carried back into _slots"
    )


@pytest.mark.asyncio
async def test_a_close_failure_restore_persists_the_prune_it_just_made(
    tmp_path, monkeypatch
) -> None:
    """The restore's prune must reach DISK, not merely the in-memory slot.

    Its sibling above asserts the in-memory list, which holds whether or not the restore
    arms the flush, so it cannot see this gap. The periodic flush reaches a restored slot
    only through ``flush_slot_now``, which returns before saving when ``_dirty`` is unset.
    So an unarmed restore leaves the deleted ids on disk. The bulk restore readers do
    prune against the loaded vocabulary on the next boot, so the window closes at a
    restart -- but until then the ids sit on disk with the in-memory prune already lost.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f-doomed", "name": "Doomed", "parent_id": "", "owner_app": ""}]
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#111111", "order": 0}]
    _commit_vocabulary(state)
    state.publish_committed_tag_ids(state._tags)
    assert state._committed_tag_ids == frozenset({"t-doomed"}), (
        "fixture: both vocabularies must be COMMITTED before the close, or the restore "
        "has no transition to prune on"
    )

    slot = _slot("a", folder_id="f-doomed", tags=["t-doomed"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["a"] = slot
    key = slot_history_key(slot)
    _log_session(state, key, {"folder_id": "f-doomed", "tags": ["t-doomed"]})

    async def _delete_both_then_fail(*_a, **_kw):
        state._folders = []
        state._tags = []
        _commit_vocabulary(state)
        state.publish_committed_tag_ids(state._tags)
        raise RuntimeError("history write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _delete_both_then_fail)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    restored = state._slots.get("a")
    assert restored is slot, (
        "fixture: the failed save must have restored the popped slot, or the restore arm "
        "under test never ran"
    )
    assert restored.tags == [], (
        "fixture: the restore must have pruned the tag vocabulary in memory before this "
        f"test can say anything about durability; got {restored.tags!r}"
    )

    await asyncio.to_thread(state._flush_dirty_slots)

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("tags", []) == [], (
        f"the flush left tags={meta.get('tags')!r} on disk after the restore pruned them "
        "in memory. The restore owes a guarded metadata correction, since the flush "
        "reaches a restored slot only when something made it durable"
    )


@pytest.mark.asyncio
async def test_a_restore_does_not_clobber_a_refile_that_landed_in_the_close_window(
    tmp_path, monkeypatch
) -> None:
    """A restored slot must not overwrite a metadata edit that reached disk meanwhile.

    The restore holds values read BEFORE the close, so the flush is the wrong instrument
    for making them durable: it rebuilds every slot-owned key from the in-memory object,
    which is older than any edit that landed while the close was in flight. A slot with
    messages is exactly where that bites, because the flush does carry it -- so a refile
    the user performed during the window is silently and durably reverted. The correction
    is guarded on disk still holding the pre-restore values, so it declines instead.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_a", "name": "A", "parent_id": "", "owner_app": ""},
        {"id": "f_b", "name": "B", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)

    slot = _slot("a", folder_id="f_a", tags=[])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["a"] = slot
    key = slot_history_key(slot)
    _log_session(state, key, {"folder_id": "f_a", "tags": []})

    async def _refile_then_fail(*_a, **_kw):
        # The user refiles the conversation while the close's history save is in flight.
        state.conversation_log.update_metadata(key, {"folder_id": "f_b"})
        raise RuntimeError("history write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _refile_then_fail)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    assert state._slots.get("a") is slot, (
        "fixture: the failed save must have restored the popped slot, or the restore arm "
        "under test never ran"
    )

    await asyncio.to_thread(state._flush_dirty_slots)

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("folder_id") == "f_b", (
        f"the refile that landed during the close window was reverted to "
        f"{meta.get('folder_id')!r}: the restore armed the full-slot flush, which "
        "rebuilt folder_id from the older in-memory slot. A restore owes a GUARDED "
        "metadata correction, which declines when disk has moved, not a full save"
    )


@pytest.mark.asyncio
async def test_a_close_restore_correction_does_not_overwrite_a_concurrent_refile(
    tmp_path, monkeypatch
) -> None:
    """The message-less correction must LOSE to a placement persisted while it was away.

    The correction carries what the slot held before revalidation, and it acquires the
    record lock last, so an unguarded write wins purely by arriving second -- overwriting
    a refile the user made during the failed close. Guarding on record presence alone
    cannot see this: the record is present, it simply says something newer.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f-old", "name": "Old", "parent_id": "", "owner_app": ""},
        {"id": "f-new", "name": "New", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)

    slot = _slot("a", folder_id="f-old")
    slot.messages = []
    state._slots["a"] = slot
    key = slot_history_key(slot)
    _log_session(state, key, {"folder_id": "f-old"})

    async def _refile_then_fail(*_a, **_kw):
        state.conversation_log.update_metadata(key, {"folder_id": "f-new"})
        raise RuntimeError("history write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _refile_then_fail)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    assert state._slots.get("a") is slot, (
        "fixture: the failed save must have restored the popped slot, or the correction "
        "arm under test never ran"
    )
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("folder_id") == "f-new", (
        f"the restore correction overwrote a concurrent refile (folder_id="
        f"{meta.get('folder_id')!r}). It must be guarded against the PRE-revalidation "
        "value, so a placement persisted during the close keeps its own truth"
    )


@pytest.mark.asyncio
async def test_a_cancellation_before_the_write_does_not_unfile_anything(
    tmp_path, monkeypatch
) -> None:
    """An UNCONFIRMED removal must sweep nothing -- the folder still exists.

    The sibling test above covers a cancellation AFTER the write confirmed, where the sweep
    still owes. This is the mirror case and the opposite hazard: a cancellation delivered
    while awaiting the store lock writes nothing, so unfiling on it would strip every
    conversation out of a folder that is still there.

    ``state._folders`` cannot distinguish the two -- the mutator edits it in place before
    the write and a cancellation does not roll it back -- so the sweep keys on PUBLICATION,
    which happens only after the write confirms.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": "f1"})

    async def _cancel_before_writing(mutate):
        # No write is issued at all: this models cancellation while awaiting the lock.
        raise asyncio.CancelledError()

    monkeypatch.setattr(state, "mutate_folders", _cancel_before_writing)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/folders/f1")

    assert "f1" in (
        state._committed_folder_ids or frozenset()
    ), "fixture: the committed vocabulary must still hold f1, since no write landed"
    assert state._slots["a"].folder_id == "f1", (
        "the in-memory slot was unfiled on an UNCONFIRMED removal; the folder still exists, "
        "so this strips conversations out of a live folder"
    )
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("folder_id") == "f1", (
        f"the unfiling was PERSISTED on an unconfirmed removal "
        f"(folder_id={meta.get('folder_id')!r}). Confirm the removal published before "
        "sweeping; a cancellation alone does not prove the write landed."
    )


@pytest.mark.asyncio
async def test_cancelling_the_folder_delete_after_the_commit_still_unfiles_the_slots(
    tmp_path, monkeypatch
) -> None:
    """Commit-and-sweep must be atomic with respect to cancellation.

    ``commit_snapshot_while_holding_the_lock`` shields the snapshot write, so a cancelled
    delete still LANDS the folder removal and then re-raises. Everything that unfiles the
    slots runs after that await, so a cancellation arriving in the gap left disk
    self-inconsistent: no folder row, but slot metadata still naming it.

    Restore does not paper over it -- cold start keeps an id absent from
    the loaded vocabulary on purpose (it adopts verbatim) -- so this window is now a
    durable dangling reference rather than a transient one, which is why closing it is part
    of the same change.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": "f1"})

    real_mutate = state.mutate_folders
    committed: list[bool] = []

    async def _commit_then_cancel(mutate):
        # EXACTLY the repository's cancelled-after-shielded-write shape: the mutation
        # commits, then CancelledError propagates out of mutate_folders.
        await real_mutate(mutate)
        committed.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(state, "mutate_folders", _commit_then_cancel)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/folders/f1")

    assert committed == [True], (
        "fixture: the commit never landed, so the cancellation window was never opened "
        "and this test proves nothing"
    )
    assert not any(
        f["id"] == "f1" for f in state._folders
    ), "fixture: the folder removal must have committed"
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("folder_id") == "", (
        f"persisted slot metadata still names the deleted folder "
        f"(folder_id={meta.get('folder_id')!r}). The cancellation landed between the "
        "committed removal and the sweep, so the sweep never ran -- and cold-start restore "
        "now keeps that id rather than pruning it, making the dangling reference durable. "
        "Shield and drain the whole commit-and-sweep unit."
    )


@pytest.mark.asyncio
async def test_cancelling_the_tag_delete_after_the_commit_still_strips_the_slots(
    tmp_path, monkeypatch
) -> None:
    """The tag side shares the folder side's cancellation protocol, so it shares the test.

    Fixed together deliberately: both delete handlers commit a vocabulary snapshot through
    the same shielded helper and then sweep, so a fix applied to one leaves the other
    holding the identical durable-dangling-reference window.
    """
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "T1", "color": "#111111", "order": 0}]
    state._tag_boards = []
    slot = _slot("a", tags=["t1"])
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"tags": ["t1"]})

    real_commit = mod._commit_tags_snapshot
    committed: list[bool] = []

    async def _commit_then_cancel(st, snapshot):
        await real_commit(st, snapshot)
        committed.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "_commit_tags_snapshot", _commit_then_cancel)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/tags/t1")

    assert committed == [True], "fixture: the commit never landed; the window never opened"
    assert not any(t["id"] == "t1" for t in state._tags), "fixture: the removal must commit"
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert "t1" not in (meta.get("tags") or []), (
        f"persisted slot metadata still carries the deleted tag (tags={meta.get('tags')!r}). "
        "The cancellation landed between the committed removal and the strip, so the strip "
        "never ran. Shield and drain the whole commit-and-sweep unit."
    )


@pytest.mark.asyncio
async def test_dangling_folder_id_is_kept_on_rehydrate_when_vocabulary_is_unknown(
    tmp_path,
) -> None:
    """FAIL-OPEN: an UNKNOWN folders vocabulary must not prune anything.

    Same discipline the tag prune already documents. If ``folders.json`` failed
    to parse or could not be read, ``state._folders`` is not evidence of absence
    -- pruning against it would unfile EVERY conversation and the next save would
    persist the loss.
    """
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    state = _make_state(tmp_path)
    state._folders = []
    state._committed_folder_ids = None  # load_folders() hit a parse/I/O error

    _log_session(state, "dashboard:s1", {"folder_id": "f-deleted"})
    slot = _rehydrate_slot_from_history(state, "s1")

    assert slot is not None
    assert slot.folder_id == "f-deleted", (
        "the folders vocabulary was UNKNOWN and the assignment was pruned anyway; "
        "an unreadable vocabulary must fail open or a transient I/O error unfiles "
        "every conversation and the next save makes it permanent"
    )


@pytest.mark.asyncio
async def test_empty_authoritative_folder_vocabulary_still_prunes(tmp_path) -> None:
    """An empty vocabulary does NOT unfile at cold start either.

    Also reversed with the cold-start prune withdrawal, and this is the case that shows
    why the reversal matters most: an empty ``folders.json`` is exactly what a
    half-written or freshly-restored store looks like, and it is indistinguishable at
    load from "the user deleted their last folder". Under the old rule that ambiguity
    resolved to unfiling EVERY conversation in one boot. It now resolves to keeping them,
    because the reversible outcome is the safe one.
    """
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    state = _make_state(tmp_path)
    state._folders = []
    _commit_vocabulary(state)  # folders.json parsed fine as []

    _log_session(state, "dashboard:s1", {"folder_id": "f-deleted"})
    slot = _rehydrate_slot_from_history(state, "s1")

    assert slot is not None
    assert slot.folder_id == "f-deleted", (
        "an empty vocabulary unfiled a well-formed id at cold start. An empty "
        "folders.json is what a half-synced store looks like, so this is the mass-unfile "
        "path -- it must keep the id and let a folder operation settle it later."
    )


def test_dangling_folder_id_is_pruned_by_the_recent_session_restore(tmp_path) -> None:
    """The SECOND restore site needs the same prune as the first.

    ``_apply_recent_session`` is the bulk startup path, so a fix applied only to
    ``_rehydrate_slot_from_history`` would leave the dangling reference intact for
    every conversation restored at boot -- which is nearly all of them.
    """
    from kiro_crew.dashboard.chat_persistence import _apply_recent_session

    state = _make_state(tmp_path)
    state._folders = [{"id": "f-live", "name": "Live", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    for name, fid in (("gone", "f-deleted"), ("kept", "f-live")):
        _apply_recent_session(
            state,
            f"dashboard:{name}",
            name,
            {},
            {"folder_id": fid},
            [],
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=None,
        )

    # NEGATIVE CONTROL -- a live folder_id must survive this path too.
    assert (
        state._slots["kept"].folder_id == "f-live"
    ), "the bulk restore dropped a folder_id that IS in the vocabulary"
    assert state._slots["gone"].folder_id == "f-deleted", (
        "the bulk startup restore unfiled a well-formed folder_id absent from the loaded "
        "vocabulary. This is the widest instance of the mass-unfile path -- it runs for "
        "nearly every conversation at boot -- so the vocabulary prune is withheld here "
        "too. Only a MALFORMED value is dropped on this path."
    )


def test_load_folders_sets_the_authoritative_flag(tmp_path, monkeypatch) -> None:
    """``load_folders()`` must publish the signal the prune's fail-open relies on.

    Authoritative ONLY when an existing file parsed as a list -- including a
    legitimately-empty ``[]``, which is the user having deleted their last folder.
    NOT authoritative on a parse failure, a non-list document, or a MISSING file:
    an absent store cannot be told apart from one that was deleted or is
    unreadable, and calling that an empty vocabulary unfiles every conversation
    that had a folder. Unlike ``load_tags``, which seeds defaults when its file is
    missing and so genuinely does know its vocabulary afterwards, there is no
    folder seeding to make the absent case knowable.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    path = tmp_path / "folders.json"

    # Legitimately-empty vocabulary: parsed OK -> authoritative.
    path.write_text("[]", encoding="utf-8")
    state._committed_folder_ids = None
    state.load_folders()
    assert state._committed_folder_ids is not None
    assert state._folders == []

    # Missing file: INDISTINGUISHABLE from a deleted or unreadable store, so the
    # vocabulary is unknown and pruning must fail open. A fresh install loses
    # nothing by this -- it has no persisted folder_id to prune -- while an
    # existing install whose folders.json vanished would otherwise have every
    # conversation unfiled, permanently, on the next save.
    path.unlink()
    _commit_vocabulary(state)
    state.load_folders()
    assert state._committed_folder_ids is None

    # Corrupt file: parse failure -> NOT authoritative, data untouched.
    path.write_text("{not json", encoding="utf-8")
    state._folders = [{"id": "keep-me", "name": "Keep", "parent_id": ""}]
    _commit_vocabulary(state)
    state.load_folders()
    assert state._committed_folder_ids is None
    assert state._folders[0]["id"] == "keep-me"  # not wiped

    # Valid JSON but NOT a list: vocabulary state unknown -> NOT authoritative.
    path.write_text("{}", encoding="utf-8")
    state._folders = [{"id": "keep-me", "name": "Keep", "parent_id": ""}]
    _commit_vocabulary(state)
    state.load_folders()
    assert state._committed_folder_ids is None
    assert state._folders[0]["id"] == "keep-me"


# ── The persist must be a metadata-only MERGE, not a full-slot save. A full save
# reconstructs every SLOT_OWNED_META_KEY from the in-memory object, and in that
# set "absent" means "cleared" — so a `closed=True` written by a concurrent close
# between the identity check and the save is silently erased. ──


def _closed_on_disk(state, key: str) -> bool:
    return bool(state.conversation_log.get_metadata(key).get("closed"))


@pytest.mark.asyncio
async def test_folder_delete_persist_does_not_erase_a_close_written_after_the_check(
    tmp_path,
) -> None:
    """The unfile persist must not clobber a close that landed post-check.

    The identity check and the persist are adjacent, but the check is taken
    BEFORE the await. A close that commits in that window persists
    ``closed=True`` to the record and pops the slot -- and a full-slot save then
    rebuilds the metadata line from the still-live in-memory object, which has no
    ``closed``. Because ``closed`` is a SLOT_OWNED_META_KEY where an absent field
    means "cleared", the close is erased and the dismissed tab returns on the
    next restart.

    The fixture puts the record in exactly the state that window produces: the
    close has already written ``closed=True`` to disk while the slot object is
    still live in ``state._slots``, so the identity check passes and the persist
    runs. Only a metadata-only merge of ``folder_id`` leaves the close standing.
    """
    from kiro_crew.dashboard import chat_folders as mod  # noqa: F401  (route module)

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot = _slot(
        "a", folder_id="f1", messages=[{"role": "user", "content": "hello", "ts": 1_699_999_000.0}]
    )
    state._slots["a"] = slot

    log = state.conversation_log
    key = slot_history_key(slot)
    log.append(key, "user", "hello")
    log.update_metadata(key, {"folder_id": "f1"})
    # The concurrent close commits here -- after the sweep's identity check would
    # have passed, before its persist writes.
    log.update_metadata(key, {"closed": True, "closed_at": 1_700_000_000.0})
    assert _closed_on_disk(state, key), "fixture precondition: the close is on disk"

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    meta = log.get_metadata(key)
    # NEGATIVE CONTROL -- holds on unfixed code too, so a persist that silently
    # did nothing at all cannot masquerade as a correct merge: the unfile must
    # actually have been written through to the record.
    assert meta.get("folder_id", "") == "", (
        f"the unfile was not persisted at all (folder_id={meta.get('folder_id')!r}); "
        "a merge that writes nothing is not a fix"
    )
    assert meta.get("closed") is True, (
        "the persist erased a close that committed after the identity check: a "
        "full-slot save rebuilds every SLOT_OWNED_META_KEY from the live object, "
        "where an absent `closed` means cleared, so the dismissed tab returns "
        "after restart. Merge only folder_id instead."
    )
    assert meta.get("closed_at") == 1_700_000_000.0, (
        "closed_at was dropped alongside closed; the channel-slot reconciler "
        "compares against it, so losing it re-surfaces the conversation"
    )


@pytest.mark.asyncio
async def test_tag_delete_persist_does_not_erase_a_close_written_after_the_check(
    tmp_path,
) -> None:
    """Same requirement for the tag strip, for the same reason and same window."""
    from kiro_crew.dashboard import chat_tags as mod  # noqa: F401  (route module)

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "urgent", "color": "#ff0000"}]
    slot = _slot(
        "a", tags=["t1"], messages=[{"role": "user", "content": "hello", "ts": 1_699_999_000.0}]
    )
    state._slots["a"] = slot

    log = state.conversation_log
    key = slot_history_key(slot)
    log.append(key, "user", "hello")
    log.update_metadata(key, {"tags": ["t1"]})
    log.update_metadata(key, {"closed": True, "closed_at": 1_700_000_000.0})
    assert _closed_on_disk(state, key), "fixture precondition: the close is on disk"

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert resp.status == 200
    meta = log.get_metadata(key)
    # NEGATIVE CONTROL -- the strip must really have been persisted.
    # An emptied list is written as an ABSENT key -- absence IS the cleared state
    # for a SLOT_OWNED_META_KEY -- so accept either shape here.
    assert not meta.get("tags"), (
        f"the tag strip was not persisted at all (tags={meta.get('tags')!r}); "
        "a merge that writes nothing is not a fix"
    )
    assert meta.get("closed") is True, (
        "the persist erased a close that committed after the identity check; "
        "merge only tags instead of rewriting the whole slot record"
    )
    assert meta.get("closed_at") == 1_700_000_000.0, "closed_at was dropped too"


def test_a_stale_vocabulary_does_not_unfile_a_newer_filing_through_the_next_save(
    tmp_path,
) -> None:
    """The full blocking path: stale store -> restore -> save, and the filing survives BOTH.

    The other cold-start tests stop at the restored slot. This one carries it through the
    SAVE, because that is what made the old behaviour irreversible: clearing the id in
    memory was recoverable until the next flush rewrote the metadata line without it.

    The fixture is the exact shape the finding names -- a `folders.json` that is READABLE
    and therefore KNOWN, but STALE: it lists an older folder while the conversation names a
    newer one created after that snapshot. The None-is-UNKNOWN fail-open cannot help here,
    because the vocabulary parsed fine. Only withholding the prune does.
    """
    from kiro_crew.dashboard.chat_persistence import (
        _rehydrate_slot_from_history,
        _save_slot_to_history,
    )

    state = _make_state(tmp_path)
    # STALE but perfectly readable: parsed as a list, so KNOWN, and missing the newer id.
    state._folders = [{"id": "f-old", "name": "Older", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    _log_session(state, "dashboard:newer", {"folder_id": "f-created-after-the-backup"})

    slot = _rehydrate_slot_from_history(state, "newer")
    assert slot is not None, "fixture: the session must restore at all"
    assert (
        slot.folder_id == "f-created-after-the-backup"
    ), "restore unfiled a newer valid filing against a stale-but-readable vocabulary"

    state._slots["newer"] = slot
    _save_slot_to_history(state, slot)

    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("folder_id") == "f-created-after-the-backup", (
        f"the save persisted the unfiling (folder_id={meta.get('folder_id')!r}), making it "
        "durable. This is the irreversible half of the finding: an in-memory clear is "
        "recoverable, a rewritten metadata line is not."
    )


def test_removing_closed_from_the_owned_set_requires_an_explicit_adopt_clear() -> None:
    """The clobber fix cannot be a one-line key-set change; this pins why.

    Measured while attempting exactly that fix, and recorded here so the next
    attempt does not repeat it. Two requirements collide on one field:

    * ANTI-CLOBBER wants a full save to CARRY an on-disk ``closed``, because the
      save rebuilds from an in-memory slot that may not know about a concurrent
      close. Dropping ``closed``/``closed_at`` from ``SLOT_OWNED_META_KEYS`` gets
      this for free at every ``force=True`` site at once, via
      :func:`~kiro_crew.history.carry_unowned_metadata`.
    * ADOPT-REOPEN wants a full save to ERASE it. A session restored with
      ``adopt_closed=True`` is live in memory while ``closed=True`` is still on
      disk, and it is the next ordinary save's OMISSION that clears the flag so the
      session restores after the next restart. Live consumers:
      ``handlers/members.py`` and ``slack/gateway.py``.

    A full save cannot satisfy both, because it cannot tell "stale, does not know
    about the close" from "deliberately reopened" -- both present as a slot that
    reads open. So the real fix needs the save to be TOLD which it is (an explicit
    clear at the adopt sites, or a tri-state ``closed`` argument), which is a
    signature change plus a per-site intent audit -- not a key-set edit.

    Note also that persisting ``closed`` POSITIVELY does not close the window: the
    payload is rebuilt from a slot that reads open, so an explicit ``False``
    overwrites an on-disk ``True`` exactly as an absent key erases it. The encoding
    changes; the staleness does not.

    THIS GATE fires only if someone drops the keys from the owned set without first
    making the adopt paths clear positively -- the exact half-fix that silently
    strands adopted sessions closed forever.
    """
    from kiro_crew.history import SLOT_OWNED_META_KEYS

    if "closed" in SLOT_OWNED_META_KEYS:
        # The deferral is still in force: nothing to check, and the anti-clobber
        # requirement is carried by persist_swept_slot_meta at the two sweep sites.
        assert "closed_at" in SLOT_OWNED_META_KEYS, (
            "closed and closed_at must leave the owned set together: carrying one "
            "while rebuilding the other leaves a close whose instant the "
            "channel-slot reconciler compares against missing"
        )
        return

    adopt_sites = collect_repo_violations(_adopt_without_explicit_clear)
    assert not adopt_sites, (
        "these modules restore sessions with adopt_closed=True but never clear the "
        f"closed flag: {sorted({p for p, _ in adopt_sites})}. `closed` is no longer "
        "slot-owned, so the flag is now carried forever and an adopted session will "
        "not restore after the next restart. Clear it positively via "
        "ConversationLog.clear_closed at each site."
    )


@pytest.mark.asyncio
async def test_merge_does_not_recreate_a_record_deleted_after_its_existence_check(
    tmp_path, monkeypatch
) -> None:
    """The merge's existence check must be re-taken INSIDE the record's lock.

    The check guards against upserting a record for a never-persisted session --
    ``update_metadata`` creates the file when it is absent. Taking that check off
    the lock makes it a decision based on a snapshot: acquiring the lock can
    itself mean waiting, so "checked, then wrote" is not "checked at the moment
    of writing". A session deleted in that window is RECREATED by the write, and
    the deleted history comes back.

    The fixture puts the record in exactly that state -- present to the check,
    absent at write time -- by letting the pre-check see a metadata line while
    no file exists on disk. Only a guard evaluated under the lock can refuse.
    """
    from kiro_crew.dashboard.chat_persistence import _merge_slot_meta

    state = _make_state(tmp_path)
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot

    log = state.conversation_log
    key = slot_history_key(slot)
    real_get = log.get_metadata
    assert not real_get(key), "fixture precondition: the session is not persisted"

    def _sees_a_record_that_is_already_gone(k, *a, **kw):
        """What the pre-check observed before the deletion committed."""
        if k == key:
            return {"_type": "metadata", "folder_id": "f1"}
        return real_get(k, *a, **kw)

    monkeypatch.setattr(log, "get_metadata", _sees_a_record_that_is_already_gone)

    # ``guard`` is mandatory, so state the belief a real caller would: the record
    # exists. The point of the test is that this is re-checked under the lock.
    outcome, _observed = await _merge_slot_meta(
        state, slot, {"folder_id": ""}, lambda meta: bool(meta), slot_history_key(slot)
    )

    assert outcome is not SweepMergeOutcome.COMMITTED, (
        "the merge reported a write it could not legitimately make: no record "
        "exists, so there was nothing to merge into"
    )
    assert outcome is SweepMergeOutcome.UNCONFIRMED, (
        "an absent record was reported as SUPERSEDED; that would tell the caller "
        "another writer owns the field and suppress the _dirty retry"
    )
    assert not real_get(key), (
        "the merge RECREATED a deleted session's metadata record. "
        "``update_metadata`` upserts, so an existence check taken outside the "
        "lock authorises a write that resurrects history deleted in the window. "
        "Re-take the check inside the lock via ``update_metadata_if``."
    )


@pytest.mark.asyncio
async def test_folder_unfile_does_not_overwrite_a_newer_placement_on_disk(
    tmp_path,
) -> None:
    """The unfile must not clobber a reassignment that already reached disk.

    The sweep decides to write ``folder_id=""`` from an IN-MEMORY read of the
    slot. If another writer has since moved that conversation into a different,
    live folder, the on-disk value is the newer truth and the sweep's blank is
    stale. Writing it unconditionally destroys the user's placement.

    Both slots below name the deleted folder in memory, so the sweep selects
    both. They differ only in what is on disk, which is what the lock-held guard
    must discriminate on -- ``b`` is the NEGATIVE CONTROL proving the guard is
    not simply refusing every write.
    """
    from kiro_crew.dashboard import chat_folders as mod  # noqa: F401  (route module)

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": "f2", "name": "Personal", "parent_id": "", "owner_app": ""},
    ]
    msgs = [{"role": "user", "content": "hello", "ts": 1_699_999_000.0}]
    reassigned = _slot("a", folder_id="f1", messages=list(msgs))
    stale_free = _slot("b", folder_id="f1", messages=list(msgs))
    state._slots["a"] = reassigned
    state._slots["b"] = stale_free

    log = state.conversation_log
    key_a = slot_history_key(reassigned)
    key_b = slot_history_key(stale_free)
    log.append(key_a, "user", "hello")
    log.append(key_b, "user", "hello")
    # Another writer moved ``a`` into the live folder f2 and it is already
    # durable; our in-memory copy still says f1.
    log.update_metadata(key_a, {"folder_id": "f2"})
    log.update_metadata(key_b, {"folder_id": "f1"})

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- the guard must still let the legitimate unfile through,
    # so a merge that refuses everything cannot pass this test.
    assert log.get_metadata(key_b).get("folder_id", "") == "", (
        "the legitimate unfile was refused: slot b's record still named the "
        "deleted folder, so clearing it was correct and the guard over-refused"
    )
    assert log.get_metadata(key_a).get("folder_id") == "f2", (
        "the unfile overwrote a newer placement: the record had already been "
        "moved to the live folder f2, and the sweep wrote its stale in-memory "
        "blank over it. The guard must confirm the record STILL names the "
        "folder being deleted before merging."
    )
    assert reassigned.folder_id == "f2", (
        "disk was preserved but memory was left holding the stale blank -- the "
        "periodic flush writes folder_id from the in-memory object and an empty "
        "value clears the key, so the reassignment would be clobbered later "
        "anyway. Adopt the value the guard observed."
    )


@pytest.mark.asyncio
async def test_folder_delete_completes_when_the_unfile_persist_raises(
    tmp_path, monkeypatch
) -> None:
    """A failing unfile persist must not abandon the committed folder delete.

    ``mutate_folders(_remove)`` has already committed by the time the sweep
    persists, and the merge deliberately lets an error propagate so the caller
    can arm ``_dirty``. Unguarded, that error escapes the handler AFTER the
    delete committed: the response is a 500, and ``push_slots_update()``, the
    audit record for a delete that really happened, and ``_dirty`` arming on the
    remaining slots are all skipped. The sibling tag path already wraps the
    identical call.

    Two slots, so "remaining" is observable: a failure on the first must not
    stop the second from being attempted and armed.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot_a = _slot("a", folder_id="f1")
    slot_b = _slot("b", folder_id="f1")
    state._slots["a"] = slot_a
    state._slots["b"] = slot_b

    attempted: list[str] = []

    async def _raising_merge(_state, slot, _fields, **_kw):
        attempted.append(slot.key)
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _raising_merge)

    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )
    pushes: list[int] = []
    monkeypatch.setattr(state, "push_slots_update", lambda *a, **kw: pushes.append(1))

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200, (
        "a failing unfile persist escaped the handler and turned a committed "
        "folder delete into a 500; wrap the merge the way the tag strip does"
    )
    # NEGATIVE CONTROL -- the raising merge must actually have been reached, so a
    # handler that skipped the persist entirely cannot pass by accident.
    assert attempted == ["a", "b"], (
        f"the persist was not attempted for every cleared slot (attempted={attempted}); "
        "a failure on the first must not abandon the rest"
    )
    assert pushes, "push_slots_update() was skipped, so the sidebar keeps the deleted folder"
    assert any(
        e.get("operation") == "chat.folder_delete" for e in audit
    ), "no audit record was written for a delete that actually happened"
    assert slot_a._dirty is True and slot_b._dirty is True, (
        "_dirty was not armed on the cleared slots, so the periodic flush will "
        "never retry the unfile and the dangling reference stays on disk"
    )


def test_channel_surfacing_keeps_folder_id_when_the_vocabulary_is_unknown(tmp_path) -> None:
    """FAIL-OPEN: an unreadable folders.json must not unfile everything.

    Pruning against a vocabulary that could not be read would strip every
    conversation's placement, and the next save would persist that loss. The
    unknown case must therefore keep the reference, exactly as the restore sites
    do.
    """
    from kiro_crew.dashboard import channel_slots

    state = _make_state(tmp_path)
    state._folders = []
    state._committed_folder_ids = None

    slot = channel_slots.surface_channel_session(
        state,
        {"key": "slack:3.3", "title": "", "modified": 1_700_000_000.0},
        {"folder_id": "f-unknown"},
        [{"role": "user", "content": "hi"}],
    )

    assert slot is not None
    assert slot.folder_id == "f-unknown", (
        "the folder_id was pruned against a vocabulary that is not authoritative; "
        "an unreadable folders.json would then unfile every conversation"
    )


# A persisted metadata line is JSON, so a corrupt or hand-edited ``folder_id`` can
# be any JSON type -- and a non-empty array or object is TRUTHY, so it passes the
# ``if meta.get("folder_id"):`` guard at every copy site and reaches the
# vocabulary membership test. ``x not in {...}`` then raises ``TypeError:
# unhashable type`` for exactly those two shapes, so restore crashes on a value it
# was supposed to be validating. Hashable-but-wrong types (int, bool, None) do not
# crash, but they can never name a real folder either, so the guard drops the whole
# class rather than just the unhashable half.
_MALFORMED_FOLDER_IDS = [
    pytest.param(["f-live"], id="list"),
    pytest.param({"id": "f-live"}, id="dict"),
    pytest.param(17, id="int"),
    pytest.param(True, id="bool"),
]


@pytest.mark.asyncio
async def test_restore_validation_fails_open_while_a_folder_write_is_in_flight(
    tmp_path,
) -> None:
    """An UNCOMMITTED folder removal must not be treated as committed.

    ``mutate_folders`` applies its callback to the LIVE ``_folders`` list and only
    then persists off-loop, restoring the pre-callback list if that write raises --
    the hazard ``read_folders`` documents for unlocked readers. The restore
    validator is exactly such a reader, and it is synchronous, so it cannot take
    the store lock.

    Left trusting the list, a restore landing inside that window prunes a
    ``folder_id`` whose folder is about to come BACK, and the slot's next save
    makes the unfiling permanent after the delete was already undone.

    The fixture stages the transient removal exactly as a mid-transaction mutation
    would, then asserts the id survives.

    RE-POINTED at the committed snapshot rather than at a lock probe. The validator
    reads ``_committed_folder_ids``, which only a confirmed write publishes, so the
    guarantee does not depend on ``_folders_lock`` at all -- asserted below BOTH inside
    and outside the hold. That is what lets the negative control go through the REAL
    commit path rather than simulating one by poking ``_folders`` and releasing the
    lock, which is strictly stronger: neither a validator that simply stopped pruning nor
    one that treats a bare list poke as committed state can pass.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f-live", "name": "Live", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    # Committed state: the id is known, so it survives.
    assert state.folder_id_for_restore("f-live") == "f-live"

    async with state._folders_lock:
        # Mid-transaction: the removal is applied to the live list, the write has
        # not been confirmed, and a rollback may still restore it.
        state._folders[:] = []
        assert state.folder_id_for_restore("f-live") == "f-live", (
            "the restore validator pruned against an UNCOMMITTED folder list; a "
            "failed write rolls that removal back, so the conversation ends up "
            "unfiled with its folder still present"
        )

    # Still uncommitted once the lock is RELEASED, and that is the improvement over
    # the probe: an unconfirmed removal is not evidence whoever happens to hold a
    # lock, so no unrelated folder writer can disable pruning store-wide any more.
    assert state.folder_id_for_restore("f-live") == "f-live", (
        "a list poke with no confirmed write was treated as a committed removal "
        "once the lock dropped; only a confirmed write may retire an id"
    )

    # NEGATIVE CONTROL -- a REAL committed removal must prune. Without this, simply
    # never pruning would pass every assertion above.
    state._folders[:] = [{"id": "f-live", "name": "Live", "parent_id": "", "owner_app": ""}]

    def _drop(folders):
        folders[:] = []
        return True, None

    await state.mutate_folders(_drop)
    assert state._committed_folder_ids == frozenset(), "the commit published an empty vocabulary"
    assert state.folder_id_for_restore("f-live") == "", (
        "the validator failed open after a CONFIRMED removal; a committed delete "
        "must prune or dangling ids survive every restart"
    )


@pytest.mark.asyncio
async def test_malformed_folder_id_is_dropped_even_mid_transaction(tmp_path) -> None:
    """The malformed drop is unconditional -- it precedes every fail-open.

    A non-string can never equal a folder's ``id``, so no state of the vocabulary
    (unknown, uncommitted, or committed) could vindicate it, and keeping it leaves
    the ``TypeError: unhashable type`` crash reachable on the very paths where the
    list is least trustworthy.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f-live", "name": "Live", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    async with state._folders_lock:
        for bad in (["f-live"], {"id": "f-live"}, 17, True):
            assert state.folder_id_for_restore(bad) == "", (
                f"a malformed folder_id ({bad!r}) was kept because a transaction "
                "was in flight; the malformed drop must precede the fail-opens"
            )


@pytest.mark.asyncio
async def test_tag_strip_guard_rejection_does_not_arm_the_flush_to_clobber(
    tmp_path,
) -> None:
    """A refused tag merge must RECONCILE, not re-arm the write it refused.

    The guard exists to stop the strip writing an in-memory tag list over a newer
    on-disk one. Arming ``_dirty`` after that refusal hands the same stale list to
    the periodic flush, which full-saves ``slot.tags`` from memory -- so the guard
    prevents the clobber and the retry performs it. The refusal has to be treated
    as "another writer owns this", which means adopting what the lock saw.

    Fixture: the record has already been retagged (``t1`` gone, ``t9`` added) while
    our in-memory slot still holds the pre-strip list, so the guard refuses.
    """
    from kiro_crew.dashboard import chat_tags as mod  # noqa: F401  (route module)

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "t1", "name": "urgent", "color": "#ff0000"},
        {"id": "t9", "name": "later", "color": "#00ff00"},
    ]
    slot = _slot(
        "a",
        tags=["t1"],
        messages=[{"role": "user", "content": "hello", "ts": 1_699_999_000.0}],
    )
    state._slots["a"] = slot

    log = state.conversation_log
    key = slot_history_key(slot)
    log.append(key, "user", "hello")
    # Another writer retagged this conversation: t1 is already gone, t9 is new.
    log.update_metadata(key, {"tags": ["t9"]})

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- the guard must actually have refused, i.e. the newer
    # on-disk list is still intact. If the merge had written, this would read [].
    assert log.get_metadata(key).get("tags") == ["t9"], (
        "the strip overwrote a newer on-disk tag list; the guard should have " "refused the merge"
    )
    assert slot._dirty is not True, (
        "_dirty was armed after the guard REFUSED the write: the periodic flush "
        "full-saves slot.tags from memory, so it would write the stale list over "
        "the retag the guard just protected. Reconcile from what the lock saw "
        "instead of re-arming the very save that was refused."
    )
    assert slot.tags == ["t9"], (
        f"in-memory tags were left stale (tags={slot.tags!r}); adopt the on-disk "
        "list the guard observed so memory and disk agree"
    )


def test_a_lost_folders_file_does_not_unfile_every_conversation(tmp_path, monkeypatch) -> None:
    """An ABSENT ``folders.json`` must not be read as an authoritative empty set.

    The loader cannot tell a FRESH INSTALL (no folders yet) from an EXISTING
    install whose ``folders.json`` was deleted or is unreadable -- both arrive as
    "no file". Treating that as the vocabulary makes every persisted ``folder_id``
    name no known folder, so restore prunes all of them and the next slot save
    makes the unfiling permanent. That is the durable-unfiling class the sweep
    exists to close, reintroduced through the loader.

    The asymmetry decides it: a fresh install has no persisted ``folder_id`` to
    prune, so failing open costs it nothing, while an existing install loses every
    conversation's placement. The tag loader's "absent is authoritative" does NOT
    transfer as precedent -- ``load_tags`` SEEDS ``_DEFAULT_TAGS`` and saves when
    the file is missing, so its vocabulary really is known afterwards; there is no
    equivalent folder seeding.

    Both arms are asserted, so a fix that simply stops pruning cannot pass.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    path = tmp_path / "folders.json"
    if path.exists():
        path.unlink()

    # ARM 1 -- file absent. Indistinguishable from a lost store, so fail open.
    state.load_folders()
    assert state._committed_folder_ids is None, (
        "an absent folders.json was treated as an authoritative empty vocabulary; "
        "a deleted or unreadable store then prunes every persisted folder_id"
    )
    assert state.folder_id_for_restore("f-was-real") == "f-was-real", (
        "a persisted folder_id was pruned against a vocabulary that is missing "
        "entirely; the next slot save would make that unfiling permanent"
    )

    # ARM 2 -- NEGATIVE CONTROL. A present file that parses, including a
    # legitimately-empty [], IS the vocabulary and must still prune. Without this
    # arm, never pruning at all would pass.
    path.write_text("[]", encoding="utf-8")
    state.load_folders()
    assert state._committed_folder_ids is not None, (
        "a present, well-formed folders.json was not treated as authoritative; "
        "then a genuinely-deleted folder's id survives every restart"
    )
    assert state.folder_id_for_restore("f-was-real") == "", (
        "a dangling folder_id survived an authoritative empty vocabulary -- the "
        "user deleted their last folder, so pruning is correct here"
    )

    # ARM 3 -- unreadable/corrupt is unknown too, same fail-open as absent.
    path.write_text("{not json", encoding="utf-8")
    state.load_folders()
    assert state._committed_folder_ids is None
    assert state.folder_id_for_restore("f-was-real") == "f-was-real"


@pytest.mark.asyncio
async def test_a_committed_folder_store_write_promotes_the_restore_validator(
    tmp_path, monkeypatch
) -> None:
    """A committed folder-store write makes the vocabulary AUTHORITATIVE.

    ``load_folders`` deliberately leaves the flag False when ``folders.json`` is
    ABSENT: that state cannot be told from a store that was deleted or is
    unreadable, so pruning against it would unfile every conversation. What was
    missing is the other half -- nothing PROMOTED the flag once the store
    demonstrably existed. ``mutate_folders`` writes the store and never touched the
    flag, and folder create goes THROUGH ``mutate_folders``, so the first-run
    sequence::

        no folders.json -> create a folder -> file a slot -> delete the folder

    left the restore validator disabled for the life of the process, and a slot
    restored afterwards kept a ``folder_id`` naming the deleted folder. That is
    DURABLE, not self-healing: the loader reads ``folder_id`` back without
    validating it against the folder list.

    The promotion belongs to the WRITE rather than to the delete. The same gap
    applies to create and rename, and ``mutate_folders`` is the one place that knows
    the store was successfully written -- so fixing it at the delete alone would
    leave create-then-restore still failing open.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    # ARM 1 -- NEGATIVE CONTROL. Must hold both before AND after the fix: with no
    # store on disk the vocabulary is genuinely UNKNOWN, so a legitimate id has to
    # survive. A "fix" that set the flag unconditionally would break this arm.
    assert not (tmp_path / state._FOLDERS_FILE).exists()
    state.load_folders()
    assert state._committed_folder_ids is None, "an absent store must stay non-authoritative"
    assert (
        state.folder_id_for_restore("f1") == "f1"
    ), "fail-open must survive: an absent store is not evidence the folder is gone"

    # ARM 2 -- a committed write promotes the flag.
    def _add(folders):
        folders.append({"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""})
        return True, None

    await state.mutate_folders(_add)
    assert (tmp_path / state._FOLDERS_FILE).exists(), "the store really was written"
    assert state._committed_folder_ids == frozenset(
        {"f1"}
    ), "a committed folder-store write must publish the written ids as the vocabulary"
    assert state.folder_id_for_restore("f1") == "f1", "a live folder still validates"

    # ARM 3 -- the sequence the finding names: delete, then restore.
    def _remove(folders):
        folders[:] = [f for f in folders if f["id"] != "f1"]
        return True, None

    await state.mutate_folders(_remove)
    assert state._committed_folder_ids is not None
    assert state.folder_id_for_restore("f1") == "", (
        "a slot restored after the delete still names the deleted folder -- the "
        "restore validator stayed disabled because no committed write promoted the flag"
    )


@pytest.mark.asyncio
async def test_folder_adoption_does_not_clobber_an_edit_that_lands_mid_merge(
    tmp_path, monkeypatch
) -> None:
    """Adopting ``observed`` must not overwrite a NEWER in-memory edit.

    ``_merge_slot_meta`` awaits. On a ``superseded`` refusal the sweep
    adopts what the record's lock saw, which is right when nothing else moved the
    conversation -- but the adoption was unconditional, so a move landing INSIDE
    that await window was overwritten by an on-disk value that is older than it.

    The decision has to be a compare-and-swap: adopt only while the slot still
    holds the value SUBMITTED to the merge (the blank pass one wrote). If it
    changed, someone newer owns the placement and their value must stand.

    Both arms run in one handler pass so the control cannot drift from the subject.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": "f_newer", "name": "Newer", "parent_id": "", "owner_app": ""},
        {"id": "f_ondisk", "name": "OnDisk", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)
    state._slots["edited"] = _slot("edited", folder_id="f1")
    state._slots["quiet"] = _slot("quiet", folder_id="f1")

    async def _merge_shim(_st, sl, _fields, **_kw):
        # The yield point the real merge has.
        await asyncio.sleep(0)
        if sl.key == "edited":
            # A concurrent move commits while we are suspended. This is the newer
            # truth; the on-disk value the lock saw predates it.
            sl.folder_id = "f_newer"
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_ondisk"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- adoption still HAPPENS when nothing moved in the window.
    # Without this a fix that simply stopped adopting would pass the subject arm.
    assert state._slots["quiet"].folder_id == "f_ondisk", (
        "adoption must still occur for an untouched slot; otherwise the periodic "
        "flush writes our blank over the placement the guard refused to clobber"
    )
    # THE SUBJECT -- the newer edit must survive.
    assert state._slots["edited"].folder_id == "f_newer", (
        "an edit that landed while the merge awaited was overwritten by the older "
        "on-disk value; adoption must be conditional on the slot still holding what "
        "was submitted to the merge"
    )


@pytest.mark.asyncio
async def test_tag_adoption_does_not_clobber_a_retag_that_lands_mid_merge(
    tmp_path, monkeypatch
) -> None:
    """The tag twin of the folder case, and the same compare-and-swap.

    Pass one strips the deleted id in memory and pass two submits that stripped
    list. On a ``superseded`` refusal the sweep rebuilt ``slot.tags`` from
    ``observed`` unconditionally, so a retag landing inside the merge's await was
    replaced by the older on-disk list.
    """

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "t1", "name": "urgent", "color": "#ff0000"},
        {"id": "t9", "name": "later", "color": "#00ff00"},
        {"id": "t_newer", "name": "newer", "color": "#0000ff"},
    ]
    state._slots["edited"] = _slot("edited", tags=["t1"])
    state._slots["quiet"] = _slot("quiet", tags=["t1"])

    async def _merge_shim(_st, sl, _fields, **_kw):
        await asyncio.sleep(0)
        if sl.key == "edited":
            # A concurrent retag commits while we are suspended.
            sl.tags = ["t_newer"]
        return (SweepMergeOutcome.SUPERSEDED, {"tags": ["t1", "t9"]})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- adoption still happens, and still drops the deleted id.
    assert state._slots["quiet"].tags == [
        "t9"
    ], "adoption must still occur for an untouched slot, minus the deleted tag"
    # THE SUBJECT -- the newer retag must survive.
    assert state._slots["edited"].tags == ["t_newer"], (
        "a retag that landed while the merge awaited was overwritten by the older "
        "on-disk list; adoption must be conditional on the slot still holding what "
        "was submitted to the merge"
    )


@pytest.mark.asyncio
async def test_an_alias_write_in_the_adopt_window_cannot_be_overwritten_by_a_stale_observation(
    tmp_path, monkeypatch
) -> None:
    """The adopt must read the record under ITS lock, not decide from an earlier read.

    Confirming that an observation is current and then adopting it are two steps, and any
    interval between them is one a shared-transcript alias write can land in. Whatever the
    first step concluded is then wrong: adopt imports metadata the record does not hold,
    and a later full save persists it. Adopting against the locked record removes the
    interval instead of shrinking it, so the value applied is the value held at that
    instant -- which also means a write that lands first is RECONCILED rather than
    discarded, where the two-step shape could only withhold or import stale.
    """
    from kiro_crew.dashboard import chat_folders as mod  # noqa: F401  (app wiring)

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_doomed", "name": "Doomed", "parent_id": "", "owner_app": ""},
        {"id": "f_obs", "name": "Observed", "parent_id": "", "owner_app": ""},
        {"id": "f_alias", "name": "Alias", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)

    slot = _slot("raced", folder_id="f_doomed")
    state._slots["raced"] = slot
    key = slot_history_key(slot)
    _log_session(state, "raced", {"folder_id": "f_obs"})
    state.conversation_log.update_metadata(key, {"folder_id": "f_obs"})

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        # An alias write on the shared transcript lands before the adopt runs.
        state.conversation_log.update_metadata(key, {"folder_id": "f_alias"})
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_obs"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert slot.folder_id == "f_alias", (
        f"the slot adopted {slot.folder_id!r} instead of the alias write the record "
        "actually holds. A stale observation was applied after the record moved, so the "
        "next full save makes it durable; the adopt must read under the record's lock"
    )


@pytest.mark.asyncio
async def test_a_rebind_inside_the_confirmation_await_refuses_the_adopt(
    tmp_path, monkeypatch
) -> None:
    """The routing identity must be re-taken after the confirmation await, not before it.

    Confirming that the observation is still current is itself an await, so a routing
    check taken before it is a decision made against a slot that may since have been
    rebound. A channel bind landing in that window repoints the slot at another
    transcript, and adopting then writes a placement drawn from the transcript it just
    left -- the same hazard the pre-await check exists to refuse, reintroduced by the
    confirmation that was added to close a different one.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_doomed", "name": "doomed"},
        {"id": "f_stale", "name": "stale"},
    ]
    _commit_vocabulary(state)
    rebound = _slot("rebound", folder_id="f_doomed")
    state._slots["rebound"] = rebound
    settled = _slot("settled", folder_id="f_doomed")
    state._slots["settled"] = settled

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_stale"})

    async def _confirm_shim(st, key, apply):
        await asyncio.sleep(0)
        if key == slot_history_key(rebound):
            # A channel bind commits while the adopt is in flight.
            st._slots["rebound"].linked_session_key = "slack_elsewhere"
        apply({"folder_id": "f_stale"})
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence._adopt_against_the_locked_record", _confirm_shim
    )

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert settled.folder_id == "f_stale", (
        "adoption must still occur for a slot nobody rebound; otherwise the re-check is "
        "refusing every adopt rather than the rerouted one"
    )
    assert rebound.folder_id != "f_stale", (
        "a slot rebound while the confirmation was in flight was written anyway, so the "
        "placement came from a transcript the slot had already left; the routing identity "
        "must be re-established after that await, not only before it"
    )


@pytest.mark.asyncio
async def test_a_folder_move_that_overtook_the_observation_is_not_adopted_over(
    tmp_path, monkeypatch
) -> None:
    """Adoption must refuse an observation the store has already moved past.

    ``observed`` is read under the record's lock, but the lock is gone by the time the
    caller adopts, and awaiting the merge lets another task run. A move committing in that
    gap leaves the observation OLDER than disk, so adopting it writes a placement the store
    has already superseded. Disk stays right and the sidebar only glitches -- until an
    unrelated full save flushes the slot, which makes the stale value durable and looks
    like the move silently reverted itself.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_doomed", "name": "doomed"},
        {"id": "f_stale", "name": "stale"},
        {"id": "f_newer", "name": "newer"},
    ]
    _commit_vocabulary(state)
    slot = _slot("moved", folder_id="f_doomed")
    state._slots["moved"] = slot
    _log_session(state, "moved", {"folder_id": "f_newer"})
    quiet = _slot("quiet", folder_id="f_doomed")
    state._slots["quiet"] = quiet
    _log_session(state, "quiet", {"folder_id": "f_stale"})
    # Written under the key the sweep reads: a newer move landed for one record, while
    # the other still holds exactly what the merge observed.
    state.conversation_log.update_metadata(slot_history_key(slot), {"folder_id": "f_newer"})
    state.conversation_log.update_metadata(slot_history_key(quiet), {"folder_id": "f_stale"})

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_stale"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert quiet.folder_id == "f_stale", (
        "adoption must still occur for a record nobody else wrote; withholding it would "
        "strand the sweep's own reconciliation"
    )
    assert slot.folder_id != "f_stale", (
        "an observation older than the persisted record was adopted, so the newer move "
        "is now contradicted in memory and the next full save writes the stale "
        "placement to disk"
    )


@pytest.mark.asyncio
async def test_an_absent_tags_key_reconciles_as_a_clear_not_as_no_information(
    tmp_path,
) -> None:
    """A cleared tag list is stored as ABSENCE, so absence has to reconcile as empty.

    The full save writes ``tags`` only when the list is non-empty, so a user clearing every
    tag leaves a record with no ``tags`` key at all. Reading that as "the record says
    nothing" makes the reconcile skip the field and keep the in-memory list, and the next
    full save writes those tags back -- undoing a clear the user was told had happened.
    A present-but-malformed value is a different case and must still be refused.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    state._tags = [{"id": "t_stale", "name": "Stale"}]
    state.publish_committed_tag_ids(state._tags)
    slot = _slot("cleared", tags=["t_stale"])
    state._slots["cleared"] = slot
    key = slot_history_key(slot)

    class _LogWhoseRecordHasNoTagsKey:
        def update_metadata_if(self, k, fields, guard):
            # Exactly what an ordinary clear leaves behind: no ``tags`` key.
            return bool(guard({"_type": "metadata", "folder_id": ""}))

    state.conversation_log = _LogWhoseRecordHasNoTagsKey()

    await cp.persist_meta_correction_without_messages(
        state,
        slot,
        {"tags": ["t_stale"]},
        cp.meta_unchanged_guard("", ["t_stale"]),
        key,
        "close restore",
    )

    assert list(slot.tags) == [], (
        f"the reconcile kept tags={list(slot.tags)!r} because the record had no 'tags' key, "
        "so the next full save writes them back and the user's clear is undone"
    )


@pytest.mark.asyncio
async def test_reconciling_tags_rotates_the_revision_so_a_stale_put_is_rejected(
    tmp_path, monkeypatch
) -> None:
    """The declined-correction reconcile changes tags, so it owes the same rotation.

    Its sibling adopt callback already treats this hazard as real. This path reaches
    ``slot.tags`` by a different route -- UNCONFIRMED merge, message-less slot, guard
    refused under the record's lock -- and imports the newer persisted list without
    rotating, leaving the pre-reconcile revision current. The PUT handler's compare-and-set
    then accepts a queued client PUT and durably overwrites what was just reconciled.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "t_keep", "name": "Keep"},
        {"id": "t_doomed", "name": "Doomed"},
        {"id": "t_added_by_the_alias", "name": "Alias"},
    ]
    state.publish_committed_tag_ids(state._tags)
    slot = _slot("reconciled", tags=["t_keep", "t_doomed"])
    state._slots["reconciled"] = slot
    key = slot_history_key(slot)
    state.conversation_log.update_metadata(
        key, {"tags": ["t_keep", "t_added_by_the_alias"], "_type": "metadata"}
    )

    revision_before_the_reconcile = slot.tags_revision

    await cp.persist_meta_correction_without_messages(
        state,
        slot,
        {"tags": ["t_keep", "t_doomed"]},
        cp.meta_unchanged_guard("", ["t_keep", "t_doomed"]),
        key,
        "tag delete",
    )

    reconciled = list(slot.tags)
    assert (
        "t_added_by_the_alias" in reconciled
    ), f"fixture: the reconcile did not import the newer persisted list, tags={reconciled!r}"

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.put(
            "/api/chat/slots/reconciled/tags",
            json={
                "tags": ["t_keep", "t_doomed"],
                "base_tags_revision": revision_before_the_reconcile,
            },
        )

    assert resp.status == 409, (
        f"a PUT holding the pre-reconcile revision was accepted ({resp.status}), so it "
        f"overwrote the reconciled list {reconciled!r} and the alias's retag is durably lost"
    )
    assert (
        "t_added_by_the_alias" in slot.tags
    ), f"the stale PUT dropped the reconciled tag: tags={list(slot.tags)!r}"


@pytest.mark.asyncio
async def test_adopting_a_newer_tag_list_rotates_the_revision_so_a_stale_put_is_rejected(
    tmp_path, monkeypatch
) -> None:
    """Adoption changes tags, so it owes the revision rotation the PUT handler's CAS reads.

    The PUT handler is compare-and-set on ``tags_revision``: a client composes a list, then
    submits the revision it composed against. Every other writer rotates the revision when
    it changes tags, which is what makes a queued stale PUT fail closed. An adoption that
    imports a NEWER on-disk list without rotating leaves the pre-adopt revision current, so
    the queued PUT passes CAS and durably overwrites the list just adopted.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "t_keep", "name": "Keep"},
        {"id": "t_doomed", "name": "Doomed"},
        {"id": "t_added_by_the_alias", "name": "Alias"},
    ]
    state.publish_committed_tag_ids(state._tags)
    slot = _slot("retagged", tags=["t_keep", "t_doomed"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["retagged"] = slot
    _log_session(state, "retagged", {"tags": ["t_keep", "t_doomed"]})
    state.conversation_log.update_metadata(
        slot_history_key(slot), {"tags": ["t_keep", "t_doomed", "t_added_by_the_alias"]}
    )

    revision_before_the_adopt: list[str] = []

    async def _superseded_by_the_alias_retag(_st, sl, _fields, **_kw):
        # Runs AFTER the strip pass has already rotated the revision, so this sample
        # isolates the adoption's own obligation from the strip's.
        revision_before_the_adopt.append(sl.tags_revision)
        # The record's lock saw a list the alias had already extended.
        return (
            cp.SweepMergeOutcome.SUPERSEDED,
            {"tags": ["t_keep", "t_doomed", "t_added_by_the_alias"]},
        )

    monkeypatch.setattr(cp, "_merge_slot_meta", _superseded_by_the_alias_retag)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        assert (await client.delete("/api/chat/tags/t_doomed")).status == 200

        assert revision_before_the_adopt, "fixture: the sweep never reached the merge"
        stale_revision = revision_before_the_adopt[0]
        adopted = list(slot.tags)
        assert (
            "t_added_by_the_alias" in adopted
        ), f"fixture: the adoption did not import the alias retag, tags={adopted!r}"

        # The PUT that was composed before the adoption, carrying the revision it saw.
        resp = await client.put(
            "/api/chat/slots/retagged/tags",
            json={"tags": ["t_keep"], "base_tags_revision": stale_revision},
        )

    assert resp.status == 409, (
        f"a PUT holding the pre-adopt revision was accepted ({resp.status}), so it overwrote "
        f"the adopted list {adopted!r} with its own stale composition and the alias's retag "
        "is durably lost"
    )
    assert (
        "t_added_by_the_alias" in slot.tags
    ), f"the stale PUT dropped the adopted tag: tags={list(slot.tags)!r}"


@pytest.mark.asyncio
async def test_cancelling_the_correction_keeps_the_flush_held_until_the_worker_lands(
    tmp_path,
) -> None:
    """The hold has to outlive its write: a thread cannot be cancelled, only abandoned.

    Cancellation delivered while the worker waits for the record lock unwinds the awaiting
    coroutine immediately, but the ``to_thread`` worker keeps running and its write still
    lands. If the hold drops at that moment, the periodic flush is free to rebuild every
    owned key from the restored in-memory slot and overwrite the concurrent refile the
    worker is in the middle of respecting.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("cancelled", folder_id="f_stale_restore")
    slot._dirty = True
    state._slots["cancelled"] = slot
    key = slot_history_key(slot)

    in_the_worker = threading.Event()
    let_the_worker_finish = threading.Event()
    sampled = threading.Event()
    held_while_writing: list[int] = []

    class _LogWhoseWorkerBlocks:
        def update_metadata_if(self, k, fields, guard):
            in_the_worker.set()
            let_the_worker_finish.wait(5)
            # Sampled from inside the write: the hold must still be up right here.
            held_while_writing.append(getattr(slot, "_metadata_persist_inflight", 0))
            sampled.set()
            return False

    state.conversation_log = _LogWhoseWorkerBlocks()

    correction = asyncio.ensure_future(
        cp.persist_meta_correction_without_messages(
            state,
            slot,
            {"folder_id": "f_stale_restore"},
            cp.meta_unchanged_guard("f_stale_restore", []),
            key,
            "close restore",
        )
    )
    await asyncio.to_thread(in_the_worker.wait, 5)
    correction.cancel()
    await asyncio.sleep(0.05)

    assert not correction.done(), (
        "the correction unwound while its worker was still writing, so the flush hold is "
        "already released and a periodic flush in this window rebuilds every owned key "
        "from the restored slot over the concurrent refile"
    )

    let_the_worker_finish.set()
    with contextlib.suppress(asyncio.CancelledError):
        await correction
    await asyncio.to_thread(sampled.wait, 5)

    assert held_while_writing, "fixture: the worker never reached its sampling point"
    assert held_while_writing[0] >= 1, (
        f"the flush hold read {held_while_writing[0]} while the correction's worker was still "
        "writing, so a periodic flush in that window rebuilds every owned key from the "
        "restored slot and overwrites the concurrent refile"
    )


@pytest.mark.asyncio
async def test_a_malformed_persisted_tags_value_never_reaches_the_slot(tmp_path) -> None:
    """The reconcile takes values from a hand-editable record, so it owes validation.

    ``tags`` on disk is a plain JSON field a user can edit, and every reader of
    ``slot.tags`` treats it as a list of ids. Assigning a scalar straight from the record
    turns a declined correction into a crash on the next list operation, so the reconcile
    routes through the same restore validator the rehydrate paths use.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("malformed", tags=["t_keep"])
    state._slots["malformed"] = slot
    key = slot_history_key(slot)

    class _LogWithAHandEditedRecord:
        def update_metadata_if(self, k, fields, guard):
            # A scalar where a list belongs: hand-edited, or written by an older shape.
            return bool(guard({"tags": "t_keep", "_type": "metadata"}))

    state.conversation_log = _LogWithAHandEditedRecord()

    await cp.persist_meta_correction_without_messages(
        state,
        slot,
        {"tags": ["t_keep"]},
        cp.meta_unchanged_guard("", ["t_keep"]),
        key,
        "close restore",
    )

    assert isinstance(slot.tags, list), (
        f"the reconcile assigned {slot.tags!r} to slot.tags straight from the record, so the "
        "next list operation on it raises"
    )
    assert all(
        isinstance(t, str) for t in slot.tags
    ), f"slot.tags carries a non-string entry: {slot.tags!r}"
    # The list operation the crash would surface on.
    assert "t_gone" not in slot.tags


@pytest.mark.asyncio
async def test_a_declined_correction_reconciles_the_restored_slot(tmp_path) -> None:
    """A refusal proves disk moved, so leaving the restored copy in memory loses the edit.

    The correction carries what the slot held before revalidation, and the guard refuses it
    precisely when a concurrent refile has already persisted something newer. Refusing the
    WRITE is right; leaving the stale value in the in-memory slot is not, because the next
    full save rebuilds every owned key from that slot and makes the stale copy durable.
    The reconcile has to happen under the record's own lock, which here is the guard call.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("restored", folder_id="f_restored_stale")
    state._slots["restored"] = slot
    key = slot_history_key(slot)

    guard_calls: list[dict] = []

    class _LogWhoseRecordMovedOn:
        def update_metadata_if(self, k, fields, guard):
            # The lock-held record carries a newer placement than the restore is offering.
            meta = {"folder_id": "f_moved_by_another_writer", "_type": "metadata"}
            guard_calls.append(meta)
            return bool(guard(meta))

    state.conversation_log = _LogWhoseRecordMovedOn()

    await cp.persist_meta_correction_without_messages(
        state,
        slot,
        {"folder_id": "f_restored_stale"},
        cp.meta_unchanged_guard("f_restored_stale", []),
        key,
        "close restore",
    )

    assert guard_calls, "fixture: the guard must have been consulted under the record lock"
    assert slot.folder_id == "f_moved_by_another_writer", (
        f"the declined correction left folder_id={slot.folder_id!r} in memory while disk "
        "holds 'f_moved_by_another_writer', so the next full save rebuilds every owned key "
        "from this slot and overwrites the concurrent refile durably"
    )


@pytest.mark.asyncio
async def test_a_lock_timeout_in_the_reread_does_not_adopt_the_stale_snapshot(
    tmp_path, monkeypatch
) -> None:
    """A failed locked reread is not an empty record, and must not license the fallback.

    The SUPERSEDED arm re-reads under the record's lock so the value it adopts is the value
    the record holds at that instant. When that reread cannot take the lock it tells us
    NOTHING about the record -- and the record may have moved again since the merge
    observed it. Adopting the merge-time snapshot then imports a placement the store has
    already superseded, and because that path arms neither the dirty flag nor the per-field
    retry, the next full save makes the stale value durable with no recovery path.
    """
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.history import HistoryLockTimeout

    state = _make_state(tmp_path)
    slot = _slot("raced", folder_id="f_origin")
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["raced"] = slot
    key = slot_history_key(slot)

    calls: list[str] = []

    class _LogThatLosesTheLockOnTheReread:
        def update_metadata_if(self, k, fields, guard):
            if not calls:
                calls.append("merge")
                # The merge observes the record under lock and its guard refuses, so the
                # handler takes the SUPERSEDED arm with this as ``observed``.
                guard({"folder_id": "f_seen_by_the_merge"})
                return False
            calls.append("reread")
            raise HistoryLockTimeout(f"another writer holds {k}")

    state.conversation_log = _LogThatLosesTheLockOnTheReread()

    def _adopt(sl, current, fields) -> None:
        sl.folder_id = current.get("folder_id", sl.folder_id)

    await cp.persist_swept_slot_meta(
        state,
        slot,
        {"folder_id": ""},
        guard=lambda meta: False,
        adopt=_adopt,
        label="folder delete",
        expected_history_key=key,
    )

    assert calls == ["merge", "reread"], f"fixture: expected a merge then a reread, got {calls}"
    assert slot.folder_id != "f_seen_by_the_merge", (
        "the handler adopted the merge-time snapshot after the locked reread failed, so a "
        "placement the store may already have superseded is now in memory and the next "
        "full save makes it durable"
    )
    owed = set(getattr(slot, "_meta_retry_fields", ()) or ())
    assert slot._dirty and "folder_id" in owed, (
        f"the lock timeout left dirty={slot._dirty!r} owed={owed!r}, so nothing will re-read "
        "the record and the sweep's write is silently dropped"
    )


@pytest.mark.asyncio
async def test_a_refused_save_keeps_the_debt_it_did_not_persist(tmp_path, monkeypatch) -> None:
    """A refusal is not a failure and not a success: it writes nothing and must stay owed.

    The save reports ``False`` when it declines the write -- a permanently deleted session,
    a lost placement guard -- rather than raising, so the failure arm never runs. Treating
    that clean return as a landed write discharges debt nothing persisted, and the sweep
    then adopts a stale disk value over the edit that is still only in memory.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("refused", tags=["t_kept"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["refused"] = slot

    cp._remember_meta_retry_fields(slot, {"tags"})

    def _refuse(*_a, **_kw):
        return False

    monkeypatch.setattr(cp, "_save_slot_to_history", _refuse)
    applied = await cp.save_slot_off_loop(state, slot, force=True)

    assert applied is False, "fixture: the save must report its refusal, not raise"
    assert "tags" in (getattr(slot, "_meta_retry_fields", ()) or ()), (
        "a refused save discharged the debt it never persisted, so the tag edit that is "
        "still only in memory is recorded as flushed and the next sweep adopts the older "
        "disk list over it"
    )


@pytest.mark.asyncio
async def test_a_successful_force_save_discharges_the_debt_it_carried(
    tmp_path, monkeypatch
) -> None:
    """Debt outliving the save that discharged it suppresses every later adoption.

    A failed force save arms every slot-owned key as owed. The retry that succeeds writes
    those keys from memory, so the debt is settled -- but nothing on the success path says
    so, and the only other clearer is the periodic flush, which a vocabulary sweep can
    front-run. The sweep then reads stale debt, treats a guard refusal as "memory is
    newer", and declines to adopt a placement that really is newer on disk.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("retried", tags=["t_kept"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["retried"] = slot

    calls: list[str] = []

    def _explode(*_a, **_kw):
        calls.append("failed")
        raise OSError("lock timeout")

    monkeypatch.setattr(cp, "_save_slot_to_history", _explode)
    await cp.save_slot_off_loop(state, slot, force=True)
    assert set(getattr(slot, "_meta_retry_fields", ()) or ()), "fixture: the failure arms debt"

    def _succeed(*_a, **_kw):
        calls.append("ok")
        return True

    monkeypatch.setattr(cp, "_save_slot_to_history", _succeed)
    await cp.save_slot_off_loop(state, slot, force=True)

    assert calls == ["failed", "ok"], f"fixture: expected one failure then one success, got {calls}"
    assert not (getattr(slot, "_meta_retry_fields", ()) or ()), (
        f"the successful retry left {slot._meta_retry_fields!r} owed. A sweep reading that "
        "stale debt declines to adopt a newer disk placement, and the next full save then "
        "overwrites the concurrent edit that produced it"
    )


@pytest.mark.asyncio
async def test_debt_armed_while_a_save_is_in_flight_outlives_that_save(
    tmp_path, monkeypatch
) -> None:
    """The discharge must clear what the save CARRIED, not whatever is owed when it lands.

    A blanket clear on success passes the discharge test above and still loses data: a
    concurrent handler that arms a field after this save was dispatched had its value
    written by nobody, so dropping that marker silences the retry the field is owed.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("racing", tags=["t_kept"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["racing"] = slot

    # Owed at dispatch, so the discharge fires: without this the carried set is empty and
    # a blanket clear would short-circuit, making this test pass for the wrong reason.
    cp._remember_meta_retry_fields(slot, {"folder_id"})

    def _succeed_then_a_concurrent_writer_arms_tags(*_a, **_kw):
        # Stands in for another handler's failed tag write landing mid-save: this save
        # read its snapshot before the arming, so it cannot have persisted that value.
        cp._remember_meta_retry_fields(slot, {"tags"})
        return True

    monkeypatch.setattr(cp, "_save_slot_to_history", _succeed_then_a_concurrent_writer_arms_tags)
    await cp.save_slot_off_loop(state, slot, force=True)

    owed = set(getattr(slot, "_meta_retry_fields", ()) or ())
    assert "tags" in owed, (
        f"the save cleared debt armed after it was dispatched, leaving {owed!r}, so the tag "
        "write it never carried is recorded as flushed and its retry is silenced"
    )
    assert "folder_id" not in owed, "fixture: the field owed at dispatch should be discharged"


@pytest.mark.asyncio
async def test_a_swallowed_metadata_save_is_recorded_as_field_debt(tmp_path, monkeypatch) -> None:
    """A best-effort save that fails must record WHICH fields it failed to persist.

    ``save_slot_off_loop`` swallows a metadata write failure and arms ``_dirty`` so the
    flush retries. But ``_dirty`` means "messages changed" and the sweep's unflushed test
    keys on field names, so a failed tag PUT leaves no field debt: a tag delete then reads
    the slot as having nothing pending, treats the guard's refusal as "disk is newer", and
    adopts the older disk tags over the edit the user was told had been accepted.
    """
    from kiro_crew.dashboard import chat_persistence as cp

    state = _make_state(tmp_path)
    slot = _slot("acked", tags=["t_kept", "t_added"])
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["acked"] = slot

    def _explode(*_a, **_kw):
        raise OSError("lock timeout")

    monkeypatch.setattr(cp, "_save_slot_to_history", _explode)

    # The acknowledged tag edit: a force save carrying the new tag list, which fails.
    applied = await cp.save_slot_off_loop(state, slot, force=True)

    assert applied, "fixture: a best-effort save reports success even when it swallowed"
    owed = set(getattr(slot, "_meta_retry_fields", ()) or ())
    assert "tags" in owed, (
        f"the swallowed save recorded {owed!r}, so the sweep cannot tell that this slot's "
        "tags are unpersisted. It will read a guard refusal as 'disk is newer' and adopt "
        "the older disk list over the edit the user was told had landed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        SweepMergeOutcome.COMMITTED,
        SweepMergeOutcome.SUPERSEDED,
        SweepMergeOutcome.UNCONFIRMED,
        "raise",
        "not-ours",
    ],
)
async def test_every_sweep_disposition_releases_the_flush_hold(
    tmp_path, monkeypatch, outcome
) -> None:
    """A hold leaked on any one exit silences the flush for that slot for the process.

    Five dispositions reach the end of a sweep write -- three outcome arms, the exception
    path, and the slot-is-not-ours early return -- so the release cannot live at a single
    happy-path statement. Each arm is driven here and the counter checked back to zero.
    """

    state = _make_state(tmp_path)
    state._folders = [{"id": "f_doomed", "name": "Doomed", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    slot = _slot("busy", folder_id="f_doomed")
    slot.messages = [{"role": "user", "content": "hello"}]
    state._slots["busy"] = slot
    _log_session(state, "busy", {"folder_id": "f_doomed"})

    async def _merge_shim(_st, sl, _fields, **_kw):
        await asyncio.sleep(0)
        if outcome == "raise":
            raise RuntimeError("merge blew up")
        if outcome == "not-ours":
            # A concurrent recreate takes the key while the sweep is suspended.
            state._slots["busy"] = _slot("busy", folder_id="")
            return (SweepMergeOutcome.COMMITTED, {})
        return (outcome, {"folder_id": "f_other"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert slot._metadata_persist_inflight == 0, (
        f"the {outcome} exit left the flush hold at {slot._metadata_persist_inflight}. A "
        "leaked counter makes flush_slot_now return early forever, so this slot's "
        "messages stop being persisted for the life of the process"
    )


@pytest.mark.asyncio
async def test_a_flush_inside_the_sweep_window_cannot_erase_a_concurrent_close(
    tmp_path, monkeypatch
) -> None:
    """The sweep must hold the periodic flush off the slot it is writing.

    The flush full-rebuilds every slot-owned key from the in-memory object and returns
    early only while ``_metadata_persist_inflight`` is raised. A sweep write goes through
    ``update_metadata_if``, not ``save_slot_off_loop``, so nothing else raises it: a flush
    landing in the sweep's await writes a slot that still reads OPEN in memory, omits the
    ``closed`` a concurrent close just committed, and the dismissed tab comes back.
    """

    state = _make_state(tmp_path)
    state._folders = [{"id": "f_doomed", "name": "Doomed", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    slot = _slot("busy", folder_id="f_doomed")
    # Dirty WITH messages is what the flush acts on.
    slot.messages = [{"role": "user", "content": "hello"}]
    slot._dirty = True
    state._slots["busy"] = slot
    key = slot_history_key(slot)
    _log_session(state, "busy", {"folder_id": "f_doomed"})

    async def _merge_shim(_st, sl, _fields, **_kw):
        # A concurrent close commits the dismissal to disk...
        state.conversation_log.update_metadata(key, {"closed": True})
        # ...and the periodic flush fires while the sweep is still in flight.
        await asyncio.to_thread(state._flush_dirty_slots)
        return (SweepMergeOutcome.COMMITTED, {})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("closed"), (
        "the flush ran inside the sweep's window and rebuilt every slot-owned key from a "
        "slot that still reads open, dropping the closed flag a concurrent close had "
        "committed -- so the dismissed tab returns on the next restart"
    )


@pytest.mark.asyncio
async def test_the_adopt_runs_on_the_loop_thread_that_owns_the_slot(tmp_path) -> None:
    """The mutation must happen on the event loop, not on the worker holding the lock.

    Borrowing the record's lock from a worker thread makes the READ atomic, but mutating
    slot state there does not: the event loop can be running a folder edit at that instant,
    so the adopter's own unchanged-test passes before the edit and the assignment lands
    after it. The adopt path deliberately arms no dirty flag, so a later full save persists
    the stale value rather than repairing it. Marshalling the apply back onto that loop --
    and waiting for it before releasing the lock -- is what makes the pair atomic.
    """
    import threading

    from kiro_crew.dashboard.chat_persistence import _adopt_against_the_locked_record

    state = _make_state(tmp_path)
    slot = _slot("owned", folder_id="f_a")
    state._slots["owned"] = slot
    key = slot_history_key(slot)
    _log_session(state, "owned", {"folder_id": "f_a"})
    state.conversation_log.update_metadata(key, {"folder_id": "f_a"})

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _apply(current: dict) -> None:
        seen.append(threading.get_ident())

    applied = await _adopt_against_the_locked_record(state, key, _apply)

    assert applied, "fixture: the record must be readable, or the apply never ran"
    assert seen == [loop_thread], (
        f"the adopt ran on {seen} rather than exactly once on the loop's {loop_thread}, "
        "so a concurrent folder edit on the loop can interleave between the guard's read "
        "and this mutation, and the next full save makes the stale value durable"
    )


@pytest.mark.asyncio
async def test_a_successful_flush_clears_the_field_debt_it_persisted(tmp_path, monkeypatch) -> None:
    """A retry that lands must clear the marker, or it suppresses every later adopt.

    The marker exists to say "this field's newer value is only in memory". The periodic
    flush is what discharges that debt -- it full-saves every slot-owned key from the
    object -- so a marker that outlives the flush is a permanent lie: from then on the
    sweep reads the field as unflushed, keeps memory, and the next full save writes it
    over whatever an alias has since committed on the shared transcript.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_doomed", "name": "Doomed", "parent_id": "", "owner_app": ""},
        {"id": "f_alias", "name": "Alias", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)

    slot = _slot("settled", folder_id="f_doomed")
    slot.messages = [{"role": "user", "content": "hello"}]
    # An earlier transient failure recorded the debt, exactly as the sweep does.
    slot._meta_retry_fields = {"folder_id"}
    slot._dirty = True
    state._slots["settled"] = slot
    key = slot_history_key(slot)
    _log_session(state, "settled", {"folder_id": "f_doomed"})

    await asyncio.to_thread(state._flush_dirty_slots)

    assert not slot._meta_retry_fields, (
        f"the flush persisted this slot but left {slot._meta_retry_fields!r} owed. The "
        "debt is discharged, so the marker now permanently suppresses adoption and the "
        "next full save overwrites any newer value another writer commits"
    )

    # And the consequence the marker would otherwise cause: adoption still happens.
    state.conversation_log.update_metadata(key, {"folder_id": "f_alias"})

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_alias"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert slot.folder_id == "f_alias", (
        f"the slot kept {slot.folder_id!r}: a discharged marker still suppressed the "
        "adopt, so the alias's placement is contradicted in memory"
    )


@pytest.mark.asyncio
async def test_dirt_on_an_unrelated_field_does_not_suppress_the_swept_adoption(
    tmp_path, monkeypatch
) -> None:
    """Suppression must key on the SWEPT field, not on the slot's whole dirty flag.

    ``_dirty`` means "messages changed since last flush" and some twenty sites reuse it,
    so it says nothing about whether folder or tags have unflushed changes. Treating it
    as a proxy suppresses adoption for a slot that is merely carrying an unsent message,
    and the periodic flush then full-saves every slot-owned key from that stale object --
    overwriting a folder an alias committed on the shared transcript, with no recovery
    path. Only unflushed changes to the field being swept justify keeping memory.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f_doomed", "name": "Doomed", "parent_id": "", "owner_app": ""},
        {"id": "f_alias", "name": "Alias", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)

    slot = _slot("raced", folder_id="f_doomed")
    # Dirty for an UNRELATED reason: an unsent message, nothing to do with placement.
    slot.messages = [{"role": "user", "content": "unsent"}]
    slot._dirty = True
    state._slots["raced"] = slot
    key = slot_history_key(slot)
    _log_session(state, "raced", {"folder_id": "f_doomed"})
    state.conversation_log.update_metadata(key, {"folder_id": "f_alias"})

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_alias"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f_doomed")

    assert resp.status == 200
    assert slot.folder_id == "f_alias", (
        f"the slot kept {slot.folder_id!r} instead of adopting the folder the record "
        "holds, because an unrelated unsent message made the whole slot dirty. The next "
        "full save then writes that stale placement over the alias's, unrecoverably"
    )


@pytest.mark.asyncio
async def test_a_guard_refusal_against_stale_disk_keeps_the_unflushed_edit(
    tmp_path, monkeypatch
) -> None:
    """SUPERSEDED must not adopt disk when the slot holds edits disk never received.

    SUPERSEDED reads a guard refusal as "another writer owns this field, so disk is
    newer". A save that FAILED earlier falsifies that: disk kept the older value and
    the newer one lives only in memory, flagged by ``_dirty``. The submitted payload is
    captured from that same in-memory value, so the adopters' unchanged test compares
    EQUAL and cannot see the divergence -- it only detects an edit landing INSIDE the
    merge window, and this one predates it. Adopting then overwrites the newer edit,
    and because SUPERSEDED deliberately leaves ``_dirty`` unarmed no later flush
    restores it, so the loss is silent and permanent.
    """

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "t1", "name": "urgent", "color": "#ff0000"},
        {"id": "t_unsaved", "name": "unsaved", "color": "#0000ff"},
        {"id": "t_ondisk", "name": "ondisk", "color": "#00ff00"},
    ]
    unflushed = _slot("unflushed", tags=["t1", "t_unsaved"])
    # A prior write of THIS field failed, which is what the protocol records.
    unflushed._dirty = True
    unflushed._meta_retry_fields = {"tags"}
    state._slots["unflushed"] = unflushed
    state._slots["clean"] = _slot("clean", tags=["t1"])

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"tags": ["t_ondisk"]})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")

    assert resp.status == 200
    assert state._slots["clean"].tags == [
        "t_ondisk"
    ], "a slot with nothing unflushed must still adopt what the lock observed"
    assert state._slots["unflushed"].tags == ["t_unsaved"], (
        "an unflushed in-memory edit was replaced by the older on-disk list; a guard "
        "refusal proves disk DIFFERS from the caller's belief, not that disk is newer, "
        "so adoption must stand down when the slot carries unflushed edits"
    )
    assert state._slots["unflushed"]._dirty, (
        "the surviving edit was left unarmed for the flush, so it reaches disk only by "
        "luck; keeping the value is only half the repair"
    )


@pytest.mark.asyncio
async def test_default_filing_revalidates_a_folder_deleted_after_it_was_resolved(
    tmp_path, monkeypatch
) -> None:
    """A pending default-filed conversation must not publish a deleted folder.

    The sweep cannot cover this one, and not merely by bad luck -- by construction.
    ``reconcile_channel_slots`` resolves the channel folder with one await
    (``lookup_channel_folder``) and PERSISTS it with another before surfacing. If the
    delete commits inside that window while the conversation is still PENDING, then
    at sweep time NO live slot names the folder, so pass one finds nothing and the
    loop terminates at once on ``if not cleared: break``. The handler returns, and
    only afterwards does the reconciler publish the id it resolved before the delete.

    So the guarantee has to come from revalidating at the ASSIGNMENT, which is what
    the other three copy sites already do.

    The placement is also already ON DISK -- the caller persists it before surfacing
    -- so rejecting it in memory alone would leave the dead id to be read back and
    republished. The slot must therefore be armed for the flush to rewrite it.
    """
    from kiro_crew.dashboard import channel_slots
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    # load_folders() publishes this at boot, so True is the production state.
    _commit_vocabulary(state)

    # The channel's default folder, resolved by the reconciler BEFORE the delete.
    resolved_before_delete = "f1"

    # NO live slot names f1, which is the whole point: the sweep will find nothing.
    assert not [s for s in state._slots.values() if s.folder_id == "f1"]

    swept: list[int] = []

    async def _count_sweep_persists(_st, _sl, *_a, **_kw):
        swept.append(1)
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(mod, "save_slot_off_loop", _count_sweep_persists)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence._merge_slot_meta", _count_sweep_persists
    )

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"
    # NEGATIVE CONTROL on the premise -- the sweep really did nothing, so this test
    # exercises the hole rather than a path the sweep already covers.
    assert swept == [], "the sweep persisted something; this is not the zero-slot case"

    # Now the reconciler resumes and publishes, through the REAL helper, carrying the
    # id it resolved before the delete committed.
    late = channel_slots.surface_channel_session(
        state,
        {"key": "slack:9.9", "title": "", "modified": 1_700_000_000.0},
        {"agent": ""},  # no folder_id, no markers -> needs_default_filing() is True
        [{"role": "user", "content": "hi"}],
        folder_id=resolved_before_delete,
    )

    assert late is not None, "the slot was not surfaced; test proves nothing"
    # POSITIVE CONTROL on the path -- this really is the default-filing branch.
    assert getattr(
        late, "_channel_folder_filed", False
    ), "the arrival did not take the default-filing branch"
    assert late.folder_id == "", (
        "a pending conversation published a folder id that was deleted after the "
        "reconciler resolved it; the default-filing assignment must revalidate"
    )
    assert late._dirty is True, (
        "the rejected placement is already persisted on disk, so the slot must be "
        "armed for the flush to rewrite the record without the dead id"
    )


@pytest.mark.asyncio
async def test_default_filing_keeps_a_live_folder_and_does_not_arm_the_flush(
    tmp_path,
) -> None:
    """The revalidation must not break ordinary default filing.

    Companion control to the test above: with the folder still present, the same
    assignment path must keep the placement AND leave ``_dirty`` alone -- the record
    the caller just persisted is correct, so re-saving it is pure churn. A fix that
    simply stopped assigning, or that armed the flush unconditionally, passes the
    rejection test and fails this one.
    """
    from kiro_crew.dashboard import channel_slots

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    slot = channel_slots.surface_channel_session(
        state,
        {"key": "slack:8.8", "title": "", "modified": 1_700_000_000.0},
        {"agent": ""},
        [{"role": "user", "content": "hi"}],
        folder_id="f1",
    )

    assert slot is not None
    assert slot.folder_id == "f1", "a LIVE default folder must still be applied"
    assert getattr(slot, "_channel_folder_filed", False) is True
    assert slot._dirty is not True, (
        "nothing was rejected, so the flush must not be armed -- the record the "
        "caller persisted is already correct"
    )


@pytest.mark.asyncio
async def test_superseded_tag_adoption_validates_the_observed_list(tmp_path, monkeypatch) -> None:
    """The superseded tag adoption must not import invalid tag ids into live state.

    ``observed["tags"]`` is persisted metadata and was iterated directly. A ``str``
    persisted there is iterable, so ``for t in "t1"`` yields ``'t'`` and ``'1'`` --
    each a ``str``, each ``!= tid`` -- and single characters became live tag ids.
    Unknown ids survived too.

    The check is therefore type AND membership, mirroring the vocabulary prune the
    restore paths already perform (``chat_persistence.py``: gated on the committed
    snapshot, so an unreadable ``tags.json`` fails OPEN rather than wiping every
    assignment).
    """

    async def _run(observed_value, *, authoritative=True):
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "t1", "name": "urgent", "color": "#ff0000"},
            {"id": "t9", "name": "later", "color": "#00ff00"},
        ]
        # The adopt callback validates against the COMMITTED vocabulary, so the
        # fixture has to stand in for a confirmed write (or, when not authoritative,
        # for a loader that could not establish one). ``_tags`` is still seeded above
        # because the handler itself reads it; the two agree here, and the test below
        # named for the drift case is what pins them apart.
        state._committed_tag_ids = (
            frozenset(t["id"] for t in state._tags) if authoritative else None
        )
        state._slots["a"] = _slot("a", tags=["t1"])

        async def _merge_shim(_st, _sl, _fields, **_kw):
            await asyncio.sleep(0)
            return (SweepMergeOutcome.SUPERSEDED, {"tags": observed_value})

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
        async with TestClient(TestServer(_make_tags_app(state))) as client:
            resp = await client.delete("/api/chat/tags/t1")
        assert resp.status == 200
        return list(state._slots["a"].tags)

    # CONTROL -- a well-formed list of known ids is still adopted, minus the deleted
    # one. Without this arm a fix that returned [] always would pass every rejection.
    assert await _run(["t1", "t9"]) == ["t9"], (
        "a well-formed observed list of known ids must still be adopted, minus the "
        "tag being deleted"
    )

    # A STRING persisted as `tags` -- iterating it yields characters.
    kept_str = await _run("t9")
    assert kept_str == [], (
        f"a string persisted as tags was iterated character-by-character into "
        f"{kept_str!r}; the observed value must be a list before it is adopted"
    )

    # A DICT persisted as `tags` -- iterating it yields its KEYS.
    kept_dict = await _run({"t9": True})
    assert (
        kept_dict == []
    ), f"a dict persisted as tags contributed its keys as live tag ids ({kept_dict!r})"

    # UNKNOWN ids are pruned against the vocabulary.
    assert await _run(["t9", "t_ghost"]) == [
        "t9"
    ], "an observed tag id naming no known tag must be pruned, not adopted"

    # FAIL-OPEN control -- when the vocabulary is UNKNOWN the membership prune must
    # NOT run, or an unreadable tags.json would wipe every assignment. Type checking
    # still applies.
    assert await _run(["t9", "t_ghost"], authoritative=False) == [
        "t9",
        "t_ghost",
    ], "with the vocabulary unknown the membership prune must fail open"
    assert (
        await _run("t9", authoritative=False) == []
    ), "fail-open covers the VOCABULARY only -- a malformed type is still dropped"


@pytest.mark.asyncio
async def test_load_tags_publishes_the_committed_vocabulary_after_seeding_defaults(
    tmp_path, monkeypatch
) -> None:
    """Fresh install: the committed snapshot must hold the SEEDED ids, not an empty set.

    On a fresh install ``tags.json`` does not exist, so the parse branch never runs and
    the vocabulary is treated as known. The seed happens LATER in the same loader, after
    which ``save_tags`` persists five default tags. A snapshot published before that seed
    is an empty frozenset -- and empty is KNOWN-EMPTY, which PRUNES. A reconciliation
    landing in that window would strip every default tag from every slot that carries
    one, and the next slot save makes the loss durable.

    Both arms below are the harm, not a restatement of the ordering:

    (a) the snapshot equals the seeded id set;
    (b) a real tag delete on that fresh install leaves a slot's OTHER default tag
        intact -- the reconciliation prunes nothing it should not.

    Arm (b) drives the actual handler and its ``_adopt_observed_tags`` callback, so it
    fails for the user-visible reason rather than for the field's value.
    """

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # FIXTURE CONTROL: genuinely fresh, so the seed path is the one under test.
    assert not (tmp_path / state._TAGS_FILE).exists(), "fixture is not a fresh install"

    state.load_tags()
    seeded = frozenset(t["id"] for t in state._DEFAULT_TAGS)
    assert len(seeded) == 5, "the seed set changed; update this test deliberately"
    assert state._committed_tag_ids == seeded, (
        f"fresh install published {state._committed_tag_ids!r} as the committed tag "
        "vocabulary while disk holds the seeded defaults; an empty frozenset is "
        "KNOWN-EMPTY and prunes, so a reconciliation would strip every default tag"
    )

    # (b) the reconciliation itself must keep a surviving default tag.
    doomed, survivor = sorted(seeded)[0], sorted(seeded)[1]
    slot = _slot("a", tags=[doomed, survivor])
    state._slots["a"] = slot

    async def _merge_shim(_st, _sl, _fields, **_kw):
        await asyncio.sleep(0)
        return (SweepMergeOutcome.SUPERSEDED, {"tags": [doomed, survivor]})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete(f"/api/chat/tags/{doomed}")
    assert resp.status == 200
    assert list(state._slots["a"].tags) == [survivor], (
        f"deleting {doomed!r} left the slot holding {list(state._slots['a'].tags)!r}; the "
        f"surviving default {survivor!r} was pruned against an empty committed vocabulary"
    )


@pytest.mark.asyncio
async def test_an_unreadable_tags_file_leaves_the_vocabulary_unknown_until_a_write(
    tmp_path, monkeypatch
) -> None:
    """UNKNOWN must survive the LOADER, and end at the first confirmed write.

    ``None`` means UNKNOWN and fails open; ``frozenset()`` means known-empty and prunes.
    Publishing after the seed must not upgrade UNKNOWN to KNOWN -- the loader genuinely
    does not know what an unparsable file holds, so pruning against it would wipe every
    assignment.

    A WRITE is different, and the tempting reading is wrong about that. It is tempting to
    assert
    that a delete also leaves UNKNOWN in place, on the reasoning that a delete must not
    manufacture knowledge. But ``save_tags_snapshot`` rewrites the whole file, so once it
    returns, disk holds exactly what was written -- knowledge established by the write.
    Keeping UNKNOWN there left the field ``None`` for the life of the process, so every
    later restore failed open and a tag deleted after a malformed load was retained on
    resumed slots indefinitely. See
    ``test_a_confirmed_write_recovers_the_vocabulary_from_unknown``, and
    ``mutate_folders``, which has always published unconditionally for the same reason.

    Two independent UNKNOWN causes are covered because they take different code paths: a
    valid-JSON non-list (the ``vocab_ok = False`` branch) and a read failure (the
    ``except``). A fix that gated only the first would pass with the second still broken.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

    # CAUSE 1 -- valid JSON, not a list.
    (tmp_path / "tags.json").write_text("{}", encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_tags()
    assert state._committed_tag_ids is None, (
        "a non-list tags.json left a KNOWN vocabulary; UNKNOWN must stay None or "
        "restore-time pruning wipes every assignment"
    )
    # POSITIVE CONTROL: knownness now has exactly ONE encoding, so there is no second
    # flag left to agree or disagree with -- the assertion above IS the whole check.
    assert not hasattr(state, "_tags_authoritative"), (
        "a parallel knownness flag reappeared; two encodings of UNKNOWN is a standing "
        "sync obligation and the reason this one was retired"
    )

    # ... and a delete, being a confirmed WRITE, ends the ignorance rather than
    # preserving it: the vocabulary becomes exactly what the delete persisted.
    state._tags = [{"id": "t1", "name": "T1", "color": "#111111"}]
    state._slots["a"] = _slot("a", tags=["t1"])
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t1")
    assert resp.status == 200
    assert state._committed_tag_ids == frozenset(), (
        f"after a confirmed delete the vocabulary is {state._committed_tag_ids!r}; the "
        "write rewrote the whole file, so it must be published as KNOWN-empty -- leaving "
        "it None strands the process failing open and retains deleted ids on resume"
    )

    # CAUSE 2 -- the read itself fails, so the loader's ``except`` runs.
    import json as _json

    (tmp_path / "tags.json").write_text("[]", encoding="utf-8")
    state2 = _make_state(tmp_path)
    real_loads = _json.loads

    def _boom(*a, **kw):
        raise ValueError("simulated parse/IO failure")

    monkeypatch.setattr(_json, "loads", _boom)
    try:
        state2.load_tags()
    finally:
        monkeypatch.setattr(_json, "loads", real_loads)
    assert state2._committed_tag_ids is None, (
        "a failed tags.json read left a KNOWN vocabulary; the except path must leave "
        "UNKNOWN in place"
    )


@pytest.mark.asyncio
async def test_tag_adoption_validates_against_the_committed_vocabulary_not_the_live_list(
    tmp_path, monkeypatch
) -> None:
    """The adopted list must be checked against COMMITTED tag ids, not live ``_tags``.

    ``state._tags`` is a working copy, and it moves in both directions: a tag mutation
    applies to it in memory and only then persists, restoring the pre-mutation list if
    that write raises. So an id can be present in the live list while its bytes never
    landed. Adopting against that list imports a tag id that does not exist on disk,
    and the next save makes it durable.

    The two arms below are the two-way hazard:

    (a) DRIFT -- the live list carries an id the committed snapshot does not, exactly
        as it would mid-mutation or after a rollback. Adoption must prune it. This is
        the arm that fails against a validator reading ``state._tags``, because that
        read cannot tell the uncommitted id from a real one.
    (b) UNKNOWN -- no committed snapshot at all (``None``), which is what an
        unreadable ``tags.json`` leaves behind. Pruning there would wipe every
        assignment, so membership must fail open while the type check still applies.

    POSITIVE CONTROL in arm (a): ``t9`` is in BOTH lists and must survive, so a fix
    that simply pruned everything cannot pass.
    """

    async def _run(observed_value, *, committed):
        state = _make_state(tmp_path)
        # LIVE list is deliberately WIDER than the committed vocabulary: t_uncommitted
        # models an id a mutation applied in memory whose write has not landed.
        state._tags = [
            {"id": "t1", "name": "urgent", "color": "#ff0000"},
            {"id": "t9", "name": "later", "color": "#00ff00"},
            {"id": "t_uncommitted", "name": "in flight", "color": "#0000ff"},
        ]
        # The committed snapshot is now the ONLY thing the readers consult, so this
        # test fails for the committed-vocabulary reason and nothing else.
        state._committed_tag_ids = committed
        state._slots["a"] = _slot("a", tags=["t1"])

        async def _merge_shim(_st, _sl, _fields, **_kw):
            await asyncio.sleep(0)
            return (SweepMergeOutcome.SUPERSEDED, {"tags": observed_value})

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
        async with TestClient(TestServer(_make_tags_app(state))) as client:
            resp = await client.delete("/api/chat/tags/t1")
        assert resp.status == 200
        return list(state._slots["a"].tags)

    # (a) DRIFT -- committed vocabulary knows t1 and t9 only.
    drifted = await _run(["t9", "t_uncommitted"], committed=frozenset({"t1", "t9"}))
    assert drifted == ["t9"], (
        f"adoption kept {drifted!r}: an id present only in the LIVE tag list was "
        "imported onto a slot. The observed list must be validated against the "
        "committed vocabulary, which a rollback cannot leave ahead of disk"
    )

    # (b) UNKNOWN -- no committed snapshot, so membership must fail open.
    assert await _run(["t9", "t_ghost"], committed=None) == ["t9", "t_ghost"], (
        "with no committed vocabulary the membership prune must fail open; an "
        "unreadable tags.json would otherwise wipe every assignment"
    )
    assert (
        await _run("t9", committed=None) == []
    ), "fail-open covers the VOCABULARY only -- a malformed type is still dropped"


@pytest.mark.asyncio
async def test_a_confirmed_write_through_the_shared_helper_publishes_the_vocabulary(
    tmp_path,
) -> None:
    """Every confirmed tag write must leave ``_committed_tag_ids`` CURRENT.

    ``_mutate_tags_locked`` is a confirmed-write helper that persists a full snapshot
    and, before this test, published nothing -- so its first caller would leave the
    committed vocabulary STALE while disk had moved on. Staleness in the
    missing-a-live-id direction is the damaging one: the adopt and restore paths prune
    against this set, so an id that IS on disk but absent from the snapshot gets
    stripped off a user's slots and the next save makes that loss durable.

    Pinned through the SHARED helper rather than through one HTTP handler on purpose.
    A per-site convention cannot be tested into existence -- the next write path added
    would simply forget it again -- so what this asserts is that publication is reached
    by way of the funnel every confirmed write already passes through.

    POSITIVE CONTROL: the pre-existing id must still be present afterwards, so a
    "publish an empty set" regression cannot pass. And the frozenset is compared to the
    ids actually PERSISTED, not to the live list, so publishing ``state._tags`` by
    reference would not satisfy it either.
    """
    from kiro_crew.dashboard.chat_tags import _mutate_tags_locked

    state = _make_state(tmp_path)
    state._tags = [{"id": "keep_me", "name": "existing", "color": "#ff0000"}]
    state._committed_tag_ids = frozenset({"keep_me"})

    def _add_a_tag() -> str:
        state._tags.append({"id": "brand_new", "name": "added", "color": "#00ff00"})
        return "brand_new"

    assert await _mutate_tags_locked(state, _add_a_tag) == "brand_new"

    assert state._committed_tag_ids == frozenset({"keep_me", "brand_new"}), (
        f"a confirmed write through the shared helper left the committed vocabulary at "
        f"{state._committed_tag_ids!r}; the newly persisted id is missing, so the adopt "
        "and restore prunes would strip it off every slot that carries it"
    )


@pytest.mark.asyncio
async def test_a_failed_write_through_the_shared_helper_publishes_nothing(
    tmp_path, monkeypatch
) -> None:
    """The funnel must publish only AFTER the bytes land, never on the failure path.

    The counterpart to the test above, and the reason publication cannot simply be
    moved next to the snapshot capture: a vocabulary published before the write would
    advertise ids that never reached disk, which is precisely the uncommitted-state
    hazard reading ``_tags`` already has. Here the write raises, the helper rolls the
    live list back, and the committed snapshot must be exactly as it was.
    """
    from kiro_crew.dashboard import chat_tags as chat_tags_module
    from kiro_crew.dashboard.chat_tags import _mutate_tags_locked

    state = _make_state(tmp_path)
    state._tags = [{"id": "keep_me", "name": "existing", "color": "#ff0000"}]
    before = frozenset({"keep_me"})
    state._committed_tag_ids = before

    def _explode(_st, _snapshot):
        raise IOError("disk full")

    monkeypatch.setattr(chat_tags_module, "_write_tags_snapshot", _explode)

    with pytest.raises(IOError):
        await _mutate_tags_locked(
            state, lambda: state._tags.append({"id": "never_lands", "name": "x"})
        )

    assert state._committed_tag_ids == before, (
        f"a FAILED write published {state._committed_tag_ids!r}: the committed "
        "vocabulary now advertises an id whose bytes never landed"
    )
    assert [t["id"] for t in state._tags] == ["keep_me"], "the live list was not rolled back"


def _restore_prunes(tmp_path, committed, live_tags, persisted_tags):
    """Drive the vocabulary prune and return the surviving tags.

    Exercises ``validate_folder_tag_ids``, a reader that gates on the committed snapshot
    and is current by construction, so the arms below test the SOURCE the prune consults
    rather than any one caller's withholding rule. The boot readers deliberately adopt
    verbatim and so cannot express these arms. *committed* is the snapshot to install --
    ``None`` for UNKNOWN, a frozenset for KNOWN.
    """
    from kiro_crew.dashboard.chat_tags import validate_folder_tag_ids

    state = _make_state(tmp_path)
    state._tags = list(live_tags)
    state._committed_tag_ids = committed
    return list(validate_folder_tag_ids(list(persisted_tags), state))


def test_the_vocabulary_prune_reads_the_committed_snapshot_not_the_live_tag_list(
    tmp_path,
) -> None:
    """The prune must gate on the COMMITTED snapshot, not the live tag list.

    A load-time flag could only answer one boot-time question -- did ``tags.json``
    parse at startup -- and would go stale the moment any
    tag was created, renamed or deleted. It would also force the prune to compare against
    live ``state._tags``, which is a working copy that moves in BOTH directions: a
    mutation applies in memory before it persists, and is rolled back if that write
    raises. Pruning a user's durable filing state against that list is what this
    whole change exists to stop.

    THREE ARMS, because a fix that satisfies one alone is wrong:

    (a) UNKNOWN -- no committed snapshot. Must prune NOTHING. This is the FAIL-OPEN
        direction and the one that costs a user real data if it regresses: pruning
        against a vocabulary we could not read wipes every assignment, and the next
        save makes the loss durable. Absent knownness means DO NOT prune -- the
        opposite of what a truthy default would give.

    (b) DRIFT -- an id present in live ``_tags`` but absent from the committed
        snapshot, exactly as it stands mid-mutation or after a rollback. Must be
        pruned, because it is not on disk. This is the arm that fails against a
        reader consulting ``state._tags``, which cannot tell an uncommitted id from
        a real one.

    (c) KNOWN-EMPTY -- ``frozenset()``, the user deleted their last tag. Must PRUNE.
        ``None`` and ``frozenset()`` are NOT interchangeable: one fails open, the
        other prunes. A migration that collapses them is a regression, not a
        subtraction.

    POSITIVE CONTROL in (b): ``t_real`` is in both the live list and the committed
    snapshot and must survive, so a fix that simply pruned everything cannot pass.
    """
    live = [{"id": "t_real", "name": "real"}, {"id": "t_uncommitted", "name": "in flight"}]

    # (a) UNKNOWN -> fail open, nothing pruned.
    kept = _restore_prunes(tmp_path / "a", None, live, ["t_real", "t_uncommitted", "t_gone"])
    assert kept == ["t_real", "t_uncommitted", "t_gone"], (
        f"restore kept {kept!r} with an UNKNOWN vocabulary; pruning against a "
        "vocabulary that could not be read wipes assignments the user still owns"
    )

    # (b) DRIFT -- committed knows t_real only, so the in-flight id must go.
    kept = _restore_prunes(tmp_path / "b", frozenset({"t_real"}), live, ["t_real", "t_uncommitted"])
    assert kept == ["t_real"], (
        f"restore kept {kept!r}: an id present only in the LIVE tag list survived the "
        "prune, so the reader is still validating against the working copy"
    )

    # (c) KNOWN-EMPTY -> prunes.
    kept = _restore_prunes(tmp_path / "c", frozenset(), [], ["t_real"])
    assert kept == [], (
        f"restore kept {kept!r} against a KNOWN-EMPTY vocabulary; an empty frozenset "
        "is knowledge and must prune, or a crash mid-delete resurrects the id forever"
    )


@pytest.mark.asyncio
async def test_no_window_where_the_deleted_id_is_still_the_committed_vocabulary(
    tmp_path, monkeypatch
) -> None:
    """After the vocabulary write confirms, the deleted id must be gone from the
    committed snapshot IMMEDIATELY -- not at the end of the handler.

    The strip sweep covers the slots it captured plus the live view at its own moment. A
    slot RESUMED after that, while the handler is still awaiting its per-slot persists,
    is in neither -- and the restore prune it runs consults ``_committed_tag_ids``. While
    publication was deferred to the end of the handler, that field still advertised the
    deleted id for the whole strip, so such a resume KEPT the tag and the next save made
    it durable. A dangling id nobody sweeps is exactly the residual this change exists
    to remove.

    Measured at the moment of maximum exposure: the observer runs INSIDE the per-slot
    persist, i.e. after the vocabulary write has confirmed and while the strip is still
    in flight. That is the window a concurrent resume would land in.

    POSITIVE CONTROL: the surviving id must still be in the published set, so a fix that
    published an empty vocabulary -- which would prune every tag off every resumed slot
    -- cannot pass.
    """
    seen: list[object] = []

    async def _observing_persist(state, slot, fields, *, guard, adopt, label, **_kw):
        # Runs during the strip, after the vocabulary write returned.
        seen.append(getattr(state, "_committed_tag_ids", None))

    state = _make_state(tmp_path)
    state._tags = [
        {"id": "doomed", "name": "going", "color": "#ff0000"},
        {"id": "keeper", "name": "staying", "color": "#00ff00"},
    ]
    state._committed_tag_ids = frozenset({"doomed", "keeper"})
    state._slots["a"] = _slot("a", tags=["doomed"])

    monkeypatch.setattr("kiro_crew.dashboard.chat_tags.persist_swept_slot_meta", _observing_persist)
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/doomed")
    assert resp.status == 200

    assert seen, "the strip never ran, so the window was never sampled"
    during = seen[0]
    assert during is not None, (
        "the committed vocabulary went UNKNOWN mid-delete; readers would fail open and "
        "keep the deleted id"
    )
    assert "doomed" not in during, (
        f"during the strip the committed vocabulary was still {sorted(during)!r}: a slot "
        "resumed in this window prunes against a set that still admits the deleted id, "
        "keeps it, and persists it as a dangling tag"
    )
    assert "keeper" in during, (
        f"the surviving tag was dropped from the committed vocabulary ({sorted(during)!r}); "
        "a resume in this window would strip legitimate tags off the slot"
    )


@pytest.mark.asyncio
async def test_a_confirmed_write_recovers_the_vocabulary_from_unknown(
    tmp_path, monkeypatch
) -> None:
    """A malformed ``tags.json`` must not leave the vocabulary UNKNOWN forever.

    The loader is right to fail open on a file it could not parse: it does not know what
    is on disk, so pruning against nothing-in-particular would wipe every assignment.
    But the FIRST confirmed write ends that ignorance. ``save_tags_snapshot`` rewrites the
    whole file, so once it returns without raising, disk holds exactly the snapshot just
    written -- knowledge established BY the write, not assumed before it.

    Skipping publication there is what makes the damage permanent: the field stays ``None``
    for the life of the process, every restore keeps failing open, and a tag deleted after
    the malformed load is retained on resumed slots indefinitely. That is the residual --
    a dangling id no later sweep reaches, exactly the class this change exists to close.

    The folder side already works this way, and its own comment records the same bug being
    fixed there: publishing on every confirmed mutation because otherwise "a first run
    that created a folder and then deleted it left the restore validator disabled for the
    life of the process". This pins the tag side to that behaviour.

    THE PUBLISHED SET MUST MATCH DISK, which is the reason this is safe rather than a
    manufactured guess: the assertion compares against the ids actually persisted, so a
    fix that published the live list, or an empty set, cannot pass.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

    # A non-list document: valid JSON the loader cannot use, so it fails open.
    (tmp_path / "tags.json").write_text("{}", encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_tags()
    assert (
        state._committed_tag_ids is None
    ), "precondition: a non-list tags.json must leave the vocabulary UNKNOWN"

    # A confirmed write of a valid vocabulary. This is the moment ignorance ends.
    from kiro_crew.dashboard import chat_tags as mod

    state._tags = [
        {"id": "kept", "name": "Kept", "color": "#111111"},
        {"id": "doomed", "name": "Doomed", "color": "#222222"},
    ]
    await mod.persist_tags_snapshot_unlocked(state)

    assert state._committed_tag_ids is not None, (
        "the vocabulary is still UNKNOWN after a write that CONFIRMED; it can now never "
        "become known without a restart, so every later restore fails open and a tag "
        "deleted from here on is retained on resumed slots indefinitely"
    )
    assert state._committed_tag_ids == frozenset({"kept", "doomed"}), (
        f"published {sorted(state._committed_tag_ids)!r}, which is not what was persisted; "
        "the published set must equal the ids the write actually put on disk"
    )

    # And the recovered vocabulary must then behave as knowledge: a delete narrows it,
    # so a slot resumed afterwards prunes the dangling id instead of keeping it.
    state._slots["a"] = _slot("a", tags=["doomed"])
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/doomed")
    assert resp.status == 200
    assert state._committed_tag_ids == frozenset({"kept"}), (
        f"after the delete the committed vocabulary is {sorted(state._committed_tag_ids or [])!r}; "
        "it must no longer advertise the deleted id"
    )


def test_a_silently_failed_seed_write_does_not_prune_persisted_tag_assignments(
    tmp_path, monkeypatch
) -> None:
    """A seed the loader could not persist must not become authoritative vocabulary.

    THE HARM CHAIN, and every link is in the tree today. ``tags.json`` is absent, so
    ``file_existed`` is False and ``vocab_ok`` stays True -- the fresh-install path. The
    seed is written with ``save_tags``, which routes through ``_atomic_write_json``, and
    that helper SWALLOWS its exception and only logs. So a failed seed write returns
    normally, ``vocab_ok`` is untouched, and the five default ids are published as the
    COMMITTED vocabulary while no ``tags.json`` exists at all.

    Everything downstream then trusts that set. A slot restored from history has its tag
    ids pruned to committed membership, so a user's own tag -- persisted on the slot line,
    never one of the five defaults -- is stripped as unknown vocabulary. The next slot
    save writes the pruned list back, and the assignment is gone for good.

    Asserting the SURVIVING ASSIGNMENT rather than "publish was not called" is deliberate:
    the harm is data loss, and a test that only watches the publisher would still pass if
    some later reader pruned for a different reason.

    POSITIVE CONTROL at the end: with the same absent file and a WORKING write, the seeded
    vocabulary IS published and does prune a genuinely dangling id -- so a fix that simply
    disabled pruning, or left the vocabulary permanently unknown, cannot pass.
    """
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    assert not (tmp_path / "tags.json").exists(), "precondition: fresh install, no tags.json"

    # A slot already on disk carrying the user's OWN tag id.
    log = state.conversation_log
    log.append("dashboard:s1", "user", "hello")
    log.update_metadata("dashboard:s1", {"tags": ["my_own_tag"]})

    # The seed write fails the way production fails: the exception is swallowed by
    # ``_atomic_write_json``, so ``load_tags`` returns as if it had succeeded.
    real_write = state._atomic_write_json_strict

    def _failing_write(path, data):
        if path.name == "tags.json":
            raise OSError("simulated disk failure")
        return real_write(path, data)

    monkeypatch.setattr(
        "kiro_crew.dashboard.state.DashboardState._atomic_write_json_strict",
        staticmethod(_failing_write),
    )
    state.load_tags()

    assert not (tmp_path / "tags.json").exists(), (
        "the seed write was supposed to fail; if the file exists this test is not "
        "exercising the swallowed-failure path at all"
    )

    restored = _rehydrate_slot_from_history(state, "s1")
    assert restored is not None, "the slot failed to rehydrate"
    assert restored.tags == ["my_own_tag"], (
        f"the restored slot's tags are {restored.tags!r}: the seed was published as the "
        "committed vocabulary even though no tags.json was written, so the user's own tag "
        "was pruned as unknown -- and the next slot save makes that permanent"
    )

    # POSITIVE CONTROL: a seed write that SUCCEEDS must still publish and still prune.
    monkeypatch.setattr(
        "kiro_crew.dashboard.state.DashboardState._atomic_write_json_strict",
        staticmethod(real_write),
    )
    state2 = _make_state(tmp_path / "ok")
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path / "ok")
    (tmp_path / "ok").mkdir(exist_ok=True)
    log2 = state2.conversation_log
    log2.append("dashboard:s2", "user", "hello")
    log2.update_metadata("dashboard:s2", {"tags": ["definitely_not_a_default"]})
    state2.load_tags()
    assert (tmp_path / "ok" / "tags.json").exists(), "the control's seed write should succeed"
    assert state2._committed_tag_ids is not None, (
        "a CONFIRMED seed must publish; leaving the vocabulary unknown would disable "
        "pruning everywhere and is not the fix"
    )
    assert state2.tag_ids_for_restore(["definitely_not_a_default"]) == [], (
        "the control kept the id: with a confirmed seed the vocabulary is authoritative "
        "and every caller that consults it must still prune a dangling id"
    )
    restored2 = _rehydrate_slot_from_history(state2, "s2")
    assert restored2 is not None and restored2.tags == [], (
        f"the boot path returned {restored2.tags if restored2 else None!r}: with a CONFIRMED "
        "seed the vocabulary is authoritative, so a dangling id must be pruned -- a fix that "
        "simply stopped pruning, or left the vocabulary permanently unknown, would pass the "
        "first assertion and fail here"
    )


# ── The identity gate is a MECHANISM in the helper, not a caller convention ───


@pytest.mark.asyncio
async def test_the_sweep_helper_itself_refuses_a_slot_that_is_no_longer_live(
    tmp_path, monkeypatch
) -> None:
    """``persist_swept_slot_meta`` must withhold the write, without caller help.

    THE INVARIANT THIS BINDS. The re-check ``state._slots.get(slot.key) is not slot``
    lives INSIDE the helper, which already receives ``state`` and ``slot``, so a sweep
    site cannot omit it: there is one copy and every caller goes through it. That
    matters because a caller-side copy is omittable, and omitting it reintroduces the
    erase-a-close bug silently at the next sweep site.

    So this asserts the property directly rather than by inspecting source. The helper is
    called with a slot whose key has been REBOUND to a different object -- the shape a
    close-then-reopen produces -- and must not reach the merge at all.

    The ``_merge_slot_meta`` shim is the observation point: if it runs, the write was not
    withheld.
    """
    from kiro_crew.dashboard.chat_persistence import persist_swept_slot_meta

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    held = _slot("s1", folder_id="f1")
    state._slots["s1"] = held
    # A concurrent close popped the tab and a reopen rebound the key to a NEW object.
    # ``held`` is the pre-close object the sweep is still carrying.
    state._slots["s1"] = _slot("s1", folder_id="f1")
    held._dirty = False

    merge_calls: list[str] = []

    async def _merge_shim(_st, sl, _fields, **_kw):  # pragma: no cover - must not run
        merge_calls.append(sl.key)
        return (SweepMergeOutcome.COMMITTED, {})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    await persist_swept_slot_meta(
        state,
        held,
        {"folder_id": ""},
        guard=lambda meta: bool(meta),
        adopt=lambda _sl, _obs, _f: None,
        label="folder delete",
        expected_history_key=slot_history_key(held),
    )

    assert merge_calls == [], (
        f"the helper merged {merge_calls!r} for a slot that is no longer the live "
        "_slots entry. Writing the pre-close object back is exactly the erase-a-close "
        "defect: the gate must be inside the helper, not left to each caller"
    )
    assert held._dirty is True, (
        "the withheld write must arm _dirty so the periodic flush retries if the slot "
        "returns; the flag is inert while the key points elsewhere"
    )


@pytest.mark.asyncio
async def test_the_sweep_helper_still_persists_the_live_slot(tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL for the gate above: it must not refuse the ordinary case.

    Without this, moving the check into the helper could withhold EVERY write -- which
    would pass the subject test above while breaking the sweep entirely.
    """
    from kiro_crew.dashboard.chat_persistence import persist_swept_slot_meta

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    live = _slot("s1", folder_id="f1")
    state._slots["s1"] = live
    live._dirty = False

    merge_calls: list[str] = []

    async def _merge_shim(_st, sl, _fields, **_kw):
        merge_calls.append(sl.key)
        return (SweepMergeOutcome.COMMITTED, {})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    await persist_swept_slot_meta(
        state,
        live,
        {"folder_id": ""},
        guard=lambda meta: bool(meta),
        adopt=lambda _sl, _obs, _f: None,
        label="folder delete",
        expected_history_key=slot_history_key(live),
    )

    assert merge_calls == ["s1"], (
        "the live slot's write was withheld; the identity gate must refuse only a slot "
        "whose key no longer points at it"
    )
    assert live._dirty is False, "a successful write must not leave the slot dirty"


# ── STRUCTURAL tier: the tag-snapshot write choke point ───────────────────────

#: Each tag-snapshot writer mapped to the ONE function permitted to reach it.
#: The sanctioned chain is ``_commit_tags_snapshot`` -> ``_write_tags_snapshot``
#: -> ``state.save_tags_snapshot``, and pinning it link by link is stricter than
#: a flat allow-set: it stops a new caller being added to the middle of the chain
#: as well as one bypassing it entirely.
#: Functions permitted to ASSIGN ``_committed_folder_ids``. The publisher is the choke
#: point; ``load_folders`` may additionally reset it to ``None`` because UNKNOWN is not a
#: publication, and ``__init__`` establishes that initial UNKNOWN.
_FOLDER_COMMITTED_ASSIGNERS = frozenset(
    {"publish_committed_folder_ids", "load_folders", "__init__"}
)


def find_folder_publish_violations(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for off-choke-point publications.

    Matches the three ASSIGN forms and the ``setattr`` indirection. The indirection is
    included because a gate that only reads assignment statements can be walked around by
    spelling the same write as a call, and the meta-tests below pin all four shapes.
    """
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if scope.name in _FOLDER_COMMITTED_ASSIGNERS:
            continue
        for node in _scope_nodes(scope):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_committed_folder_ids":
                    out.append((path, node.lineno, scope.name))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "_committed_folder_ids"
            ):
                out.append((path, node.lineno, scope.name))
    return out


def test_no_folder_publish_bypasses_the_commit_choke_point() -> None:
    """Every ``_committed_folder_ids`` publication must go through the one publisher.

    THE FOLDER SIDE'S HALF OF THE PAIR, and it is a gate for the same reason the tag one
    is. The prune paths trust this set absolutely: a KNOWN set missing a live id unfiles
    that conversation on restore, and the next slot save makes the loss durable -- the
    silent, durable direction.

    Publication is currently reached from two places -- the repository load and
    ``mutate_folders``' post-commit hook -- and both go through the one publisher. That is
    a convention, not a mechanism: nothing stops a third site deriving the set inline, and
    the two former derivations had ALREADY drifted on whether an empty-string ``id``
    counts, which is precisely how a KNOWN set ends up missing a live id. This gate makes
    the single-derivation rule structural, so the next writer is told rather than trusted.
    """
    violations = collect_repo_violations(find_folder_publish_violations)
    if violations:
        detail = "\n".join(f"  {path}:{lineno} in {func}()" for path, lineno, func in violations)
        raise AssertionError(
            "a folder committed-vocabulary publication bypasses the choke point.\n\n"
            "``_committed_folder_ids`` must be derived in ONE place, "
            "``publish_committed_folder_ids``, and only after a write CONFIRMS -- every "
            "restore validator prunes against it, so a set published for a write that "
            "did not land unfiles conversations whose folder is really still there. "
            "Call the publisher instead of assigning:\n"
            "    self.publish_committed_folder_ids(snapshot)\n"
            f"{detail}"
        )


def test_the_folder_publish_gate_scanned_a_non_empty_tree() -> None:
    """Positive control: an empty scan would make the gate above pass vacuously.

    Asserts the scan REACHES the publisher and its sanctioned assigners, so a rename
    cannot silence the gate while a bypass ships green.
    """
    root = _src_root()
    state_src = (root / "dashboard" / "state.py").read_text(encoding="utf-8")
    assert "def publish_committed_folder_ids" in state_src, (
        "publish_committed_folder_ids is gone from state.py; the gate is scanning for a "
        "name that no longer exists and can no longer fail"
    )
    assert "_committed_folder_ids" in state_src
    assert len(list(root.rglob("*.py"))) > 100


# ── Meta-tests: prove the folder-publish detector FIRES, per bypass shape ─────
# A gate is worth only what it can DETECT; without these it could match nothing forever.


def test_folder_publish_detector_flags_a_direct_bypass() -> None:
    src = (
        "def api_folder_rename(state, snapshot):\n"
        "    state._committed_folder_ids = frozenset(f['id'] for f in snapshot)\n"
    )
    found = find_folder_publish_violations(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_folder_rename")], found


def test_folder_publish_detector_flags_an_augmented_bypass() -> None:
    """``|=`` publishes too, and a target-only match would miss it."""
    src = "def api_folder_rename(state, snapshot):\n    state._committed_folder_ids |= {'f1'}\n"
    found = find_folder_publish_violations(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_folder_rename")], found


def test_folder_publish_detector_flags_an_annotated_bypass() -> None:
    src = (
        "def api_folder_rename(state, snapshot):\n"
        "    state._committed_folder_ids: frozenset = frozenset()\n"
    )
    found = find_folder_publish_violations(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_folder_rename")], found


def test_folder_publish_detector_flags_a_setattr_bypass() -> None:
    """The same write spelled as a CALL. Measured missed before this was added.

    A gate reading only assignment statements is walked around by one line of
    ``setattr``, and the bypass ships green -- which is the shape that silently unfiles
    conversations, because every restore validator prunes against this set.
    """
    src = (
        "def api_folder_rename(state, snapshot):\n"
        "    setattr(state, '_committed_folder_ids', frozenset())\n"
    )
    found = find_folder_publish_violations(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_folder_rename")], (
        "the detector missed a setattr-spelled publication, so the choke-point gate can "
        f"be bypassed by one line: {found!r}"
    )


def test_folder_publish_detector_accepts_the_sanctioned_publisher() -> None:
    src = (
        "def publish_committed_folder_ids(self, snapshot):\n"
        "    self._committed_folder_ids = frozenset(\n"
        "        f['id'] for f in snapshot if isinstance(f.get('id'), str) and f['id']\n"
        "    )\n"
    )
    assert find_folder_publish_violations(src) == []


def test_folder_publish_detector_accepts_the_unknown_reset() -> None:
    """``load_folders`` may reset to ``None``: UNKNOWN is not a publication."""
    src = "def load_folders(self):\n    self._committed_folder_ids = None\n"
    assert find_folder_publish_violations(src) == []


# ── STRUCTURAL tier: the shielded-sweep cancellation contract ─────────────────

_SWEEP_DRAIN = "sweep_to_completion_despite_cancellation"
#: The shared ledger's sweep-capture context manager. A ``with`` block of it is the
#: canonical protection, because the ledger also owns the commit-before-sweep order.
_SWEEP_CAPTURE = "capturing_sweep"


def _catches_cancelled(handler: ast.ExceptHandler) -> bool:
    """Whether *handler* catches ``CancelledError`` (bare ``except`` counts)."""
    if handler.type is None:
        return True
    names = [handler.type] if not isinstance(handler.type, ast.Tuple) else list(handler.type.elts)
    for node in names:
        if isinstance(node, ast.Attribute) and node.attr == "CancelledError":
            return True
        if isinstance(node, ast.Name) and node.id == "CancelledError":
            return True
        if isinstance(node, ast.Name) and node.id == "BaseException":
            return True
    return False


def find_uncaptured_sweep_drains(source: str, path: str = "<source>") -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for un-CAPTURED sweep drains.

    The helper drains its coroutine and then RE-RAISES the cancellation it absorbed, so a
    bare ``await`` of it unwinds the caller on the spot. Every caller must instead capture
    that cancellation -- in a ``with`` block of the shared ledger's ``capturing_sweep``,
    which owns the ordering, or in a plain ``try``/``except asyncio.CancelledError`` -- and
    re-raise it only after its own durable consequences and its audit line.
    """
    tree = ast.parse(source)
    protected: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if not any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == _SWEEP_CAPTURE
                for item in node.items
            ):
                continue
        elif isinstance(node, ast.Try):
            if not any(_catches_cancelled(h) for h in node.handlers):
                continue
        else:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                protected.add(id(inner))

    out: list[tuple[str, int, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _scope_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name != _SWEEP_DRAIN:
                continue
            if id(node) not in protected:
                out.append((path, node.lineno, scope.name))
    return out


def test_no_sweep_drain_escapes_its_cancellation_capture() -> None:
    """Every shielded sweep must CAPTURE the cancellation it re-raises.

    THE CONTRACT THIS PINS, which two HTTP handlers now depend on: a client disconnect
    mid-delete must still finish the unfile/strip sweep AND still emit the operation's only
    audit line. ``sweep_to_completion_despite_cancellation`` drains the work and then
    re-raises by design, so a bare ``await`` of it is not a barrier later statements sit
    safely behind -- it unwinds the handler on the spot and the audit line never runs.

    That is not hypothetical: one of the two handlers shipped a bare-awaited second drain
    during this change's own development, and only a behavioural test caught it. Behavioural
    tests cover the paths that exist; this gate covers the one a future edit adds.
    """
    violations = collect_repo_violations(find_uncaptured_sweep_drains)
    if violations:
        detail = "\n".join(f"  {path}:{lineno} in {func}()" for path, lineno, func in violations)
        raise AssertionError(
            f"a {_SWEEP_DRAIN} call is not wrapped in a cancellation capture.\n\n"
            "The helper drains and then RE-RAISES, so awaiting it bare unwinds the handler "
            "before its audit line and any remaining durable step. Capture it in the "
            "shared ledger, which also owns the commit-before-sweep order (#8361):\n"
            "    cancels = VocabularyDeleteCancellations()\n"
            f"    with cancels.{_SWEEP_CAPTURE}():\n        await {_SWEEP_DRAIN}(...)\n"
            "    ...durable work, then the audit line...\n"
            "    cancels.reraise_in_order()\n"
            f"{detail}"
        )


def test_the_sweep_drain_gate_scanned_a_non_empty_tree() -> None:
    """Positive control: an empty scan would make the gate above pass vacuously."""
    root = _src_root()
    src = (root / "dashboard" / "snapshot_commit.py").read_text(encoding="utf-8")
    assert f"async def {_SWEEP_DRAIN}" in src, (
        f"{_SWEEP_DRAIN} is gone from snapshot_commit.py; the gate is scanning for a name "
        "that no longer exists and can no longer fail"
    )
    callers = collect_repo_violations(
        lambda s, p: [
            (p, n.lineno)
            for n in ast.walk(ast.parse(s))
            if isinstance(n, ast.Call)
            and (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
            == _SWEEP_DRAIN
        ]
    )
    assert len(callers) >= 3, (
        f"expected at least 3 {_SWEEP_DRAIN} call sites, found {len(callers)}; the gate is "
        "no longer reaching the handlers it protects"
    )


def test_the_delete_ledger_reraises_the_commit_cancellation_first() -> None:
    """The ORDER is the invariant the hoist exists to own, so it is asserted on the owner.

    A commit cancellation can predate any write, so it is the one carrying the caller's
    rollback decision; a sweep cancellation always follows a mutation that already landed.
    Re-raising the sweep first hands the caller the wrong one and silently changes which
    recovery arm runs. Measured before this test existed: reversing the two in
    ``reraise_in_order`` left every suite green, so the order was unpinned.
    """
    from kiro_crew.dashboard.snapshot_commit import VocabularyDeleteCancellations

    ledger = VocabularyDeleteCancellations()
    commit = asyncio.CancelledError("commit")
    sweep = asyncio.CancelledError("sweep")
    with ledger.capturing_commit():
        raise commit
    with ledger.capturing_sweep():
        raise sweep
    with pytest.raises(asyncio.CancelledError) as caught:
        ledger.reraise_in_order()
    assert caught.value is commit, "the sweep cancellation displaced the commit one"


def test_the_delete_ledger_keeps_the_first_sweep_cancellation() -> None:
    """A handler runs several sweeps after ONE commit; the earliest dates when it stopped.

    Also unpinned before this test: letting a later sweep overwrite the first kept every
    suite green, so the tag handler's two-sweep sequence had no guard on which one survives.
    """
    from kiro_crew.dashboard.snapshot_commit import VocabularyDeleteCancellations

    ledger = VocabularyDeleteCancellations()
    first = asyncio.CancelledError("first")
    second = asyncio.CancelledError("second")
    with ledger.capturing_sweep():
        raise first
    with ledger.capturing_sweep():
        raise second
    assert ledger.sweep is first, "a later sweep cancellation displaced the first"


@pytest.mark.asyncio
async def test_the_cleanup_restore_persists_the_prune_it_just_made(tmp_path, monkeypatch) -> None:
    """The cleanup restore's prune must reach DISK, not merely the in-memory slot.

    Its sibling above asserts the in-memory field, which holds whether or not the restore
    arms the flush, so it cannot see this gap. ``flush_slot_now`` returns before saving
    when ``_dirty`` is unset, so an unarmed restore leaves the in-memory prune lost and the
    deleted id on disk until some later pass removes it.
    """
    from test_slot_close_recreation_race import NAME, _make_stale, _Req

    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#222222", "order": 0}]
    state.publish_committed_tag_ids(state._tags)
    slot = state.get_or_create_slot(NAME)
    slot.tags = ["t-doomed"]
    slot.messages = [{"role": "user", "content": "hello"}]
    _make_stale(state, NAME)
    key = slot_history_key(slot)
    _log_session(state, key, {"tags": ["t-doomed"]})
    assert "t-doomed" in (state._committed_tag_ids or frozenset()), (
        "fixture: the tag must be COMMITTED before cleanup runs, or there is no "
        "transition for the restore to prune on"
    )

    async def _delete_the_tag_then_fail(*_a, **_kw):
        state._tags = []
        state.publish_committed_tag_ids(state._tags)
        raise RuntimeError("archive write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _delete_the_tag_then_fail)

    with contextlib.suppress(Exception):
        await mod.api_chat_slots_cleanup(_Req(state, NAME))

    restored = state._slots.get(NAME)
    assert restored is slot, (
        "fixture: the failed archive must have restored the popped slot, or the restore arm "
        "under test never ran"
    )
    assert restored.tags == [], (
        "fixture: the restore must have pruned in memory before this test can say anything "
        f"about durability; got {restored.tags!r}"
    )

    await asyncio.to_thread(state._flush_dirty_slots)

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("tags", []) == [], (
        f"the flush left tags={meta.get('tags')!r} on disk after the cleanup "
        "restore pruned them in memory. The restore owes a guarded metadata correction, "
        "since nothing else makes the pruned list durable"
    )


@pytest.mark.asyncio
async def test_an_unconfirmed_sweep_correction_reaches_disk_without_messages(
    tmp_path, monkeypatch
) -> None:
    """A message-less slot's correction must be WRITTEN, not queued for a flush that skips it.

    ``flush_slot_now`` returns early when a slot has no messages, so the ``_dirty`` the
    UNCONFIRMED arm arms is a retry that never runs: the correction lives in memory only
    and a restart reads the deleted id straight back. An empty slot is an ordinary shape
    here, because a session created and closed without a turn still carries a placement.
    """
    from kiro_crew.dashboard import chat_persistence as persistence

    state = _make_state(tmp_path)
    slot = _slot("a", folder_id="")
    slot.messages = []
    state._slots["a"] = slot
    key = slot_history_key(slot)
    _log_session(state, key, {"folder_id": "f-doomed"})
    assert state.conversation_log.get_metadata(key).get("folder_id") == "f-doomed", (
        "fixture: the deleted id must be on disk before the sweep, or the correction has "
        "nothing to remove and this test would pass vacuously"
    )

    async def _unconfirmed(_st, _sl, _fields, **_kw):
        return (persistence.SweepMergeOutcome.UNCONFIRMED, {})

    monkeypatch.setattr(persistence, "_merge_slot_meta", _unconfirmed)

    await persistence.persist_swept_slot_meta(
        state,
        slot,
        {"folder_id": ""},
        guard=lambda _meta: True,
        adopt=lambda *_a: None,
        label="folder delete",
        expected_history_key=key,
    )

    assert slot._dirty is True, (
        "fixture: the UNCONFIRMED arm must still arm _dirty, so this covers the "
        "message-less gap rather than replacing the retry a normal slot gets"
    )
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("folder_id", "") == "", (
        f"the correction left folder_id={meta.get('folder_id')!r} on disk. A slot with no "
        "messages is skipped by flush_slot_now, so the sweep must persist the "
        "metadata-only correction itself instead of arming a retry that never runs"
    )


@pytest.mark.asyncio
async def test_an_unconfirmed_correction_does_not_recreate_a_deleted_record(
    tmp_path, monkeypatch
) -> None:
    """The message-less correction must never UPSERT: a deleted session stays deleted.

    Its sibling above covers the record that still exists. This is the other half, and
    the reason the write is guarded: a plain metadata update CREATES the directory and a
    fresh line when the record is gone, so a correction arriving after a permanent
    delete would resurrect the history it belongs to. The sweep owes a correction, never
    a resurrection, so the guarded writer must fail closed on an absent record.
    """
    from kiro_crew.dashboard import chat_persistence as persistence

    state = _make_state(tmp_path)
    slot = _slot("a", folder_id="")
    slot.messages = []
    state._slots["a"] = slot
    key = slot_history_key(slot)
    assert not state.conversation_log.get_metadata(key), (
        "fixture: the record must be ABSENT -- deleted inside the sweep window -- or "
        "this exercises the ordinary update path and says nothing about the upsert"
    )

    async def _unconfirmed(_st, _sl, _fields, **_kw):
        return (persistence.SweepMergeOutcome.UNCONFIRMED, {})

    monkeypatch.setattr(persistence, "_merge_slot_meta", _unconfirmed)

    await persistence.persist_swept_slot_meta(
        state,
        slot,
        {"folder_id": ""},
        guard=lambda _meta: True,
        adopt=lambda *_a: None,
        label="folder delete",
        expected_history_key=key,
    )

    assert not state.conversation_log.get_metadata(key), (
        "the correction RECREATED a record that had been permanently deleted. The "
        "message-less write must route through the guarded update, which fails closed "
        "on an absent record, not the upserting one"
    )


async def _delete_with_a_broken_commit(tmp_path, monkeypatch, failure: BaseException):
    """Drive a tag delete whose vocabulary commit ends in *failure*, and return the state."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#111111", "order": 0}]
    state.publish_committed_tag_ids(state._tags)

    async def _break_the_commit(*_a, **_kw):
        raise failure

    from kiro_crew.dashboard import chat_tags as chat_tags_mod

    monkeypatch.setattr(chat_tags_mod, "_commit_tags_snapshot", _break_the_commit)

    with contextlib.suppress(BaseException):
        async with TestClient(TestServer(_make_tags_app(state))) as client:
            await client.delete("/api/chat/tags/t-doomed")
    return state


@pytest.mark.asyncio
async def test_a_worker_failure_outranks_a_cancellation_that_raced_it() -> None:
    """A cancelled config write must still surface the worker's own exception.

    The drain deliberately absorbs the cancellation and lets the worker thread finish,
    so on that path the completed task holds the REAL outcome — and if the worker broke,
    re-raising the cancellation instead discards it. The caller then sees an orderly
    cancellation where there was a disk failure, so a rollback keyed on that exception
    never runs and whatever the worker staged is orphaned with nothing left to report it.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import drained_to_thread

    class _SaveFailed(RuntimeError):
        pass

    started = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        started.set()
        release.wait(5)
        raise _SaveFailed("cfg.save could not write")

    task = asyncio.ensure_future(drained_to_thread(_worker))
    assert await asyncio.to_thread(started.wait, 5), "fixture: the worker never started"
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(_SaveFailed):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_correction_still_completes_the_close_rollback(
    tmp_path, monkeypatch
) -> None:
    """A cancellation during the restore must not abandon the rest of the rollback.

    The correction awaits a real suspension point inside an ``except Exception`` arm, and
    that arm does not catch ``CancelledError`` -- so an escaping cancellation skips the
    nudge-loop restore and the app's dismissal undo. What is left is the shape the arm's
    own comment names: the tab back, the clock retired, and the crew worker still paused,
    with no later pass that re-arms a loop. So the cancellation is captured, the rollback
    finishes, and only then does it travel.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    slot = _slot("a", folder_id="")
    slot.messages = []
    slot._app = "crew"
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": ""})

    async def _fail_the_close(*_a, **_kw):
        raise RuntimeError("history write failed")

    async def _cancel_the_correction(*_a, **_kw):
        raise asyncio.CancelledError()

    restored_loop: list[bool] = []
    undone: list[tuple] = []

    async def _restore_loop(_retired, _still_ours):
        restored_loop.append(True)
        return True

    async def _undo(app, name):
        undone.append((app, name))
        return True

    monkeypatch.setattr(mod, "save_slot_off_loop", _fail_the_close)
    monkeypatch.setattr(mod, "persist_meta_correction_without_messages", _cancel_the_correction)
    monkeypatch.setattr(mod, "_restore_slot_nudge_loop", _restore_loop)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_close_undone", _undo)

    with pytest.raises(asyncio.CancelledError):
        await mod.close_slot(state, slot, "a")

    assert restored_loop, (
        "the nudge loop was never restored: the cancellation escaped the rollback, so a "
        "restored session is left with no clock -- an abandoned unattended worker"
    )
    assert undone == [("crew", "a")], (
        f"the app dismissal was not taken back (undo calls={undone!r}); the tab came back "
        "while the crew worker stayed paused, which is the pair the rollback exists to "
        "keep in agreement"
    )


def test_sweep_drain_detector_flags_a_bare_await() -> None:
    src = (
        "async def api_thing_delete(request):\n"
        f"    await {_SWEEP_DRAIN}(_sweep())\n"
        "    sel().log_api_access(outcome='allowed')\n"
    )
    found = find_uncaptured_sweep_drains(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_thing_delete")], found


def test_sweep_drain_detector_accepts_the_shared_ledger_capture() -> None:
    """The hoisted shape must READ as protected, or the gate reddens every converted site.

    Paired with the bare-await control above: that one proves the detector still bites, this
    one proves the ledger's ``with`` block is what satisfies it. Without this, replacing the
    protection with a differently-named context manager would pass unnoticed.
    """
    protected = (
        "async def api_thing_delete(request):\n"
        f"    with cancels.{_SWEEP_CAPTURE}():\n        await {_SWEEP_DRAIN}(_sweep())\n"
        "    sel().log_api_access(outcome='allowed')\n"
    )
    assert find_uncaptured_sweep_drains(protected) == []
    unrelated = (
        "async def api_thing_delete(request):\n"
        f"    with cancels.some_other_helper():\n        await {_SWEEP_DRAIN}(_sweep())\n"
    )
    assert [v[1] for v in find_uncaptured_sweep_drains(unrelated)] == [3]


def test_sweep_drain_detector_accepts_a_captured_await() -> None:
    src = (
        "async def api_thing_delete(request):\n"
        "    cancelled = None\n"
        "    try:\n"
        f"        await {_SWEEP_DRAIN}(_sweep())\n"
        "    except asyncio.CancelledError as exc:\n"
        "        cancelled = exc\n"
        "    sel().log_api_access(outcome='allowed')\n"
        "    if cancelled is not None:\n"
        "        raise cancelled\n"
    )
    assert find_uncaptured_sweep_drains(src) == []


def test_sweep_drain_detector_rejects_a_capture_that_misses_cancellation() -> None:
    """``except Exception`` does NOT catch ``CancelledError``: it must still flag."""
    src = (
        "async def api_thing_delete(request):\n"
        "    try:\n"
        f"        await {_SWEEP_DRAIN}(_sweep())\n"
        "    except Exception:\n"
        "        pass\n"
    )
    found = find_uncaptured_sweep_drains(src)
    assert [(v[1], v[2]) for v in found] == [(3, "api_thing_delete")], (
        "a try/except Exception was accepted as a cancellation capture, but "
        "CancelledError derives from BaseException and is not caught by it: "
        f"{found!r}"
    )


@pytest.mark.asyncio
async def test_a_second_cancellation_still_outlives_the_config_worker() -> None:
    """The shared drain loops rather than draining once -- pinned from its other caller.

    A single drain is not enough: awaiting it is itself a suspension point, so a SECOND
    cancellation -- a graceful shutdown escalating after its timeout, which is exactly when a
    config write is most likely to be in flight -- would unwind while the worker thread is
    still inside its read-modify-write. The next writer would then enter the critical section
    against a file the previous one is still rewriting.

    Lives here rather than beside ``run_config_write``'s own tests for two reasons: the
    property under test belongs to ``drain_shielded``, whose other callers are covered in this
    file, and that test module is black-baselined, so adding to it would have made it
    black-clean and forced an unrelated baseline edit into this change.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import run_config_write

    release = threading.Event()
    finished = threading.Event()

    def _slow_write() -> str:
        release.wait(timeout=5)
        finished.set()
        return "written"

    async def _caller() -> None:
        await run_config_write(_slow_write)

    task = asyncio.create_task(_caller())
    await asyncio.sleep(0.05)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done(), (
        "the caller unwound while the worker was still writing -- a second cancellation "
        "escaped the drain, so the config lock does not outlive the thread"
    )

    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert finished.is_set(), "fixture: the worker never completed, so nothing was drained"


def _pass_one_loops() -> dict[str, ast.For]:
    """The pass-one sweep loop in each delete handler, found structurally.

    Keyed on the iterable's SHAPE -- a list of exactly two starred elements, the captured
    slots then the live view -- rather than on a variable name, so renaming ``closing`` or
    ``cleared`` cannot silently take the gate below out of service.
    """
    root = _src_root()
    found: dict[str, ast.For] = {}
    for rel in ("dashboard/chat_folders.py", "dashboard/chat_tags.py"):
        source = (root / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if (
                isinstance(it, ast.List)
                and len(it.elts) == 2
                and all(isinstance(e, ast.Starred) for e in it.elts)
            ):
                found[rel] = node
    return found


def test_pass_one_of_each_delete_sweep_stays_yield_free() -> None:
    """No ``await`` inside pass one -- the invariant the baselines depend on.

    Why a gate rather than the spec alone: the
    ordering rules here are stated in ``history.md`` and enforced by gates that a
    restructuring refactor can satisfy while breaking the ordering itself. This one is the
    load-bearing rule of the three, so it is now enforced rather than described.

    Pass one clears or strips in memory and captures its baseline -- the transcript key.
    That capture is atomic with the write ONLY because
    nothing yields between them. Introduce one ``await`` and every slot after the first
    reads a baseline that a previous slot's persist already let the user move.
    """
    loops = _pass_one_loops()
    assert set(loops) == {"dashboard/chat_folders.py", "dashboard/chat_tags.py"}, (
        f"the pass-one detector found {sorted(loops)}; it locates the loop by its "
        "``[*captured, *live_view]`` iterable, so a restructure that changed that shape has "
        "taken this gate out of service. Re-point it rather than deleting it."
    )
    for rel, loop in sorted(loops.items()):
        awaits = [n.lineno for n in ast.walk(loop) if isinstance(n, ast.Await)]
        assert not awaits, (
            f"{rel}: pass one now awaits at line(s) {awaits}. The baselines captured in this "
            "loop -- the transcript key, and the placement counter on the folder side -- are "
            "only atomic with the in-memory write while nothing yields. With an await here, "
            "every slot after the first reads a baseline taken after an earlier slot's "
            "persist, so a move landing in that window is invisible to the guard. Capture "
            "before the loop or move the awaiting work into pass two."
        )


def test_the_pass_one_gate_flags_a_loop_that_awaits() -> None:
    """Negative control: the detector must FAIL on a pass one that yields.

    Without this the gate above passes just as well when the detector is broken, which is
    the failure mode a gate over a structural shape is most prone to.
    """
    src = (
        "async def sweep(state, closing, tid):\n"
        "    cleared = []\n"
        "    for slot in [*closing, *state._slots.values()]:\n"
        "        await persist(slot)\n"
        "        cleared.append(slot)\n"
    )
    loops = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.List)
        and len(n.iter.elts) == 2
        and all(isinstance(e, ast.Starred) for e in n.iter.elts)
    ]
    assert len(loops) == 1, "fixture: the detector did not match the shape it exists to find"
    assert [n.lineno for n in ast.walk(loops[0]) if isinstance(n, ast.Await)] == [4], (
        "the await detector missed a plain ``await`` in the loop body, so the gate above "
        "would pass on a yielding pass one"
    )


def test_the_pass_one_gate_accepts_a_yield_free_loop() -> None:
    """Positive control: a compliant pass one must NOT be flagged."""
    src = (
        "def sweep(state, closing, tid):\n"
        "    cleared = []\n"
        "    for slot in [*closing, *state._slots.values()]:\n"
        "        slot.folder_id = ''\n"
        "        cleared.append((slot, key(slot)))\n"
    )
    loops = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.List)
        and len(n.iter.elts) == 2
        and all(isinstance(e, ast.Starred) for e in n.iter.elts)
    ]
    assert len(loops) == 1, "fixture: the detector did not match the compliant shape"
    assert [n.lineno for n in ast.walk(loops[0]) if isinstance(n, ast.Await)] == [], (
        "the detector reported an await in a loop that has none, so the gate would fire on "
        "compliant code and get disabled"
    )


_TAG_SNAPSHOT_WRITE_CHAIN = {
    "_write_tags_snapshot": "_commit_tags_snapshot",
    "save_tags_snapshot": "_write_tags_snapshot",
}


def _referenced_names(node: ast.AST):
    """Yield ``(name, lineno)`` for every name READ in *node*'s own scope.

    Deliberately not restricted to ``ast.Call``. ``_commit_tags_snapshot`` reaches
    its writer as ``asyncio.to_thread(_write_tags_snapshot, ...)`` -- a bare
    reference, not a call -- so a gate that only inspected call targets would miss
    both that sanctioned use and any bypass spelled the same way. Attribute access
    is matched on the final attribute, so ``state.save_tags_snapshot`` counts
    regardless of what the receiver local is called.
    """
    for child in _scope_nodes(node):
        if isinstance(child, ast.Name):
            yield child.id, child.lineno
        elif isinstance(child, ast.Attribute):
            yield child.attr, child.lineno


def find_tag_write_violations(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str, str]]:
    """Return ``(path, lineno, enclosing_function, writer)`` for off-chain writes."""
    tree = ast.parse(source)
    out: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for name, lineno in _referenced_names(node):
            allowed = _TAG_SNAPSHOT_WRITE_CHAIN.get(name)
            if allowed is None or node.name == allowed:
                continue
            # The writer's own definition is not a reference to itself.
            if node.name == name:
                continue
            out.append((path, lineno, node.name, name))
    return out


def test_no_tag_snapshot_write_bypasses_the_commit_choke_point() -> None:
    """Every tag-snapshot write must go through ``_commit_tags_snapshot``.

    WHY THIS IS A GATE RATHER THAN A CONVENTION. The prune paths trust
    ``_committed_tag_ids`` absolutely: a KNOWN set that is missing a live id strips
    that tag from a restored slot, and the next slot save makes the loss durable. What
    keeps the set truthful is that publication happens immediately after a CONFIRMED
    write, inside ``_commit_tags_snapshot``. The folder side reaches the same guarantee
    by a different route -- one derivation in ``publish_committed_folder_ids``, pinned by
    ``test_no_folder_publish_bypasses_the_commit_choke_point`` -- because every folder
    write already routes through ``mutate_folders``. Here the WRITE CHAIN is what needs
    pinning instead: a future
    caller reaching ``save_tags_snapshot`` or ``_write_tags_snapshot``
    directly would move disk while leaving the committed set stale in exactly the
    damaging direction: advertising a vocabulary that disagrees with the file.

    Nothing about the tag path makes that mistake hard to make, which is what this gate
    supplies. It is the same mechanism this file already uses for the live-view loop
    rule, deliberately so: a reader who learns one pattern here can read both.
    """
    violations = collect_repo_violations(find_tag_write_violations)
    if violations:
        detail = "\n".join(
            f"  {path}:{lineno} in {func}() reaches {writer}"
            for path, lineno, func, writer in violations
        )
        raise AssertionError(
            "a tag-snapshot write bypasses the commit choke point.\n\n"
            "``_committed_tag_ids`` is published from ``_commit_tags_snapshot`` "
            "immediately after the write confirms, and every prune path trusts it "
            "absolutely -- so a write that lands on disk without republishing leaves "
            "the committed set describing a vocabulary that no longer exists. A KNOWN "
            "set missing a live id strips that tag from restored slots and the next "
            "save makes it durable. Route the write through the choke point:\n"
            "    await _commit_tags_snapshot(state, snapshot)\n"
            f"{detail}"
        )


def test_the_tag_write_gate_scanned_a_non_empty_tree() -> None:
    """Positive control: an empty scan would make the gate above pass vacuously.

    Asserts the scan actually REACHES the sanctioned chain, not merely that it read
    some files -- if the writers were renamed, the gate would fall silent and a bypass
    would ship green.
    """
    root = _src_root()
    chain_src = (root / "dashboard" / "chat_tags.py").read_text(encoding="utf-8")
    for writer, allowed in _TAG_SNAPSHOT_WRITE_CHAIN.items():
        assert writer in chain_src or allowed in chain_src, (
            f"neither {writer} nor {allowed} appears in chat_tags.py; the gate is "
            "scanning for names that no longer exist and can no longer fail"
        )
    assert len(list(root.rglob("*.py"))) > 100


# ── Meta-tests: prove the tag-write detector fires and stays quiet ────────────


def test_tag_write_detector_flags_a_direct_bypass() -> None:
    src = (
        "async def api_tag_rename(state, snapshot):\n"
        "    await asyncio.to_thread(state.save_tags_snapshot, snapshot)\n"
    )
    found = find_tag_write_violations(src)
    assert [(v[1], v[2], v[3]) for v in found] == [
        (2, "api_tag_rename", "save_tags_snapshot")
    ], found


def test_tag_write_detector_flags_a_bare_reference_bypass() -> None:
    """A reference handed to ``to_thread`` is a write; matching only calls would miss it."""
    src = (
        "async def api_tag_rename(state, snapshot):\n"
        "    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)\n"
    )
    found = find_tag_write_violations(src)
    assert [(v[1], v[3]) for v in found] == [(2, "_write_tags_snapshot")], found


def test_tag_write_detector_accepts_the_sanctioned_chain() -> None:
    src = (
        "async def _commit_tags_snapshot(state, snapshot):\n"
        "    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)\n"
        "    state.publish_committed_tag_ids(snapshot)\n"
        "\n"
        "def _write_tags_snapshot(state, snapshot):\n"
        "    state.save_tags_snapshot(snapshot)\n"
    )
    assert find_tag_write_violations(src) == []


# ── The collapsed tag-prune reader keeps UNKNOWN and KNOWN-EMPTY distinct ─────


def test_tag_ids_for_restore_fails_open_on_an_unknown_vocabulary(tmp_path) -> None:
    """``None`` is UNKNOWN and must keep every id.

    THE ARM THAT LOSES DATA IF IT BREAKS. ``None`` means the vocabulary was never
    loaded, or failed to parse, or could not be read. Pruning against it would strip
    every tag from the slot, and the next slot save would make that loss durable -- so
    the absence of knowledge must never be read as knowledge of absence.
    """
    state = _make_state(tmp_path)
    state._committed_tag_ids = None

    kept = state.tag_ids_for_restore(["t1", "t_unknown"])

    assert kept == ["t1", "t_unknown"], (
        f"an UNKNOWN vocabulary pruned to {kept!r}. None must fail OPEN: pruning "
        "against a vocabulary that was never loaded wipes every assignment on the "
        "slot and the next save persists the loss"
    )


def test_tag_ids_for_restore_prunes_against_a_known_empty_vocabulary(tmp_path) -> None:
    """``frozenset()`` is KNOWN-EMPTY and must prune.

    The other arm, and the reason a bare ``set()`` default would be wrong. An empty
    committed set is positive knowledge that the user deleted the last tag. If it
    failed open instead, a crash mid-delete would resurrect the dangling id forever.
    """
    state = _make_state(tmp_path)
    state._committed_tag_ids = frozenset()

    kept = state.tag_ids_for_restore(["t1", "t_unknown"])

    assert kept == [], (
        f"a KNOWN-EMPTY vocabulary kept {kept!r}. frozenset() is knowledge, not "
        "ignorance: it must prune, or a crash mid-delete leaves the dangling id on "
        "disk permanently"
    )


def test_tag_ids_for_restore_prunes_only_the_unknown_ids(tmp_path) -> None:
    """A populated vocabulary keeps members, drops non-members, preserves order."""
    state = _make_state(tmp_path)
    state._committed_tag_ids = frozenset({"t1", "t9"})

    assert state.tag_ids_for_restore(["t9", "gone", "t1"]) == ["t9", "t1"]


def test_the_four_restore_sites_route_through_the_single_reader() -> None:
    """No restore path may carry its own copy of the fail-open rule.

    The prune stood at four sites, each restating the rule in its own prose. Four
    hand-synced spellings is how a rule drifts: whoever corrects one has no reason to
    look for the other three, and the direction of drift here is silent data loss. This
    pins the collapse so a fifth copy cannot be added quietly.

    Scoped to the RESTORE-time prune shape specifically, and to the modules that
    CONSUME the vocabulary. ``state.py`` is excluded because it OWNS the field: it
    declares it, publishes it, and reads it inside the single reader itself.
    ``api_chat_tag_delete`` captures ``_committed_tag_ids`` into
    ``pre_delete_committed`` for its adopt callback to compare against, which is a
    different operation -- a pre-delete snapshot, not a prune -- so it is deliberately
    not matched. The same handler also reads it into ``committed_after`` AFTER the commit,
    to prove the removal actually published before stripping anything; that is a
    post-commit confirmation rather than the fail-open rule, so it is exempt for the same
    reason. Comment and docstring prose is skipped: naming the field while
    explaining the rule is not a second implementation of it.
    """
    root = _src_root()
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py.name == "state.py":
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for lineno, line in enumerate(src.splitlines(), 1):
            if "_committed_tag_ids" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#") or "``_committed_tag_ids``" in line:
                continue
            if "pre_delete_committed" in line or "publish_committed_tag_ids" in line:
                continue
            if "committed_after" in line:
                continue
            offenders.append(f"  {py.name}:{lineno} {stripped}")

    assert not offenders, (
        "a restore path reads _committed_tag_ids directly instead of routing through "
        "state.tag_ids_for_restore. The fail-open rule (None keeps everything, "
        "frozenset() prunes) must have exactly one spelling:\n" + "\n".join(offenders)
    )


# ── A concurrent MOVE reattaching the folder the delete just removed. The sweep
# clears every slot it can SEE, but pass two awaits, and a move that resumes
# inside (or after) that window can put ``fid`` back on a live slot. The folder
# is already gone by then, so the value is dangling the moment it lands and the
# next save makes it durable -- the same erase/resurrect family as the rest of
# this file, arriving from the write side rather than the close side. ──


@pytest.mark.asyncio
async def test_a_cancelled_failing_write_still_rolls_back(tmp_path, monkeypatch) -> None:
    """Cancellation must not let a FAILED write skip the rollback.

    The hole the drain opened. Both transaction sites restore their pre-mutation copy on
    ``except Exception`` only, and the cancellation path re-raised ``CancelledError`` --
    so a write that failed WHILE being drained had its error discarded and no rollback
    ran, leaving memory holding a folder that never reached disk.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    in_write = asyncio.Event()
    attempted: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _slow_failing_write(func, /, *args, **kwargs):
        in_write.set()
        await asyncio.sleep(0.05)
        attempted.append("raised")
        raise OSError("disk full")

    monkeypatch.setattr(asyncio, "to_thread", _slow_failing_write)

    def _add(folders):
        folders.append({"id": "f2", "name": "Later", "parent_id": "", "owner_app": ""})
        return True, None

    task = asyncio.ensure_future(state.mutate_folders(_add))
    await in_write.wait()
    task.cancel()
    outcome: list[str] = []
    try:
        await task
    except OSError:
        outcome.append("write-error")
    except asyncio.CancelledError:
        outcome.append("cancelled")
    for _ in range(50):
        if attempted:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    monkeypatch.setattr(asyncio, "to_thread", real_to_thread)

    # NEGATIVE CONTROLS.
    assert attempted == ["raised"], f"the write never failed; premise broken: {attempted}"
    assert outcome, "the task neither raised nor cancelled"

    assert [f["id"] for f in state._folders] == ["f1"], (
        f"the live folder list still holds {[f['id'] for f in state._folders]!r} after a "
        "write that FAILED during the drain -- the rollback was skipped because the "
        "cancellation path discarded the write error, so memory diverges from disk"
    )
    assert state._committed_folder_ids == frozenset(
        {"f1"}
    ), "a failed write must not publish; the committed vocabulary moved anyway"


@pytest.mark.asyncio
async def test_a_cancelled_write_holds_the_lock_until_the_worker_finishes(
    tmp_path, monkeypatch
) -> None:
    """A cancelled transaction must not release the lock mid-write.

    The hole the shield opened. ``asyncio.shield`` re-raises immediately, so returning
    on cancellation lets ``async with lock`` exit while the worker is still writing:
    the NEXT mutation acquires the lock, writes, and is then overwritten by the older
    worker finishing last -- a lost update, worse than the publication staleness the
    shield was added to fix.

    Pinned by ORDERING, not by timing luck: the second mutation must not be able to
    ACQUIRE the lock until the first write has completed, so its own write is
    necessarily last and the store ends with its value.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    order: list[str] = []
    first_in_write = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def _slow_first_write(func, /, *args, **kwargs):
        # Only the FIRST write is slow; later ones run promptly.
        if "first-done" not in order:
            first_in_write.set()
            await asyncio.sleep(0.10)
            order.append("first-done")
            return func(*args, **kwargs)
        result = func(*args, **kwargs)
        order.append("second-done")
        return result

    monkeypatch.setattr(asyncio, "to_thread", _slow_first_write)

    def _add_a(folders):
        folders.append({"id": "fa", "name": "A", "parent_id": "", "owner_app": ""})
        return True, None

    def _add_b(folders):
        folders.append({"id": "fb", "name": "B", "parent_id": "", "owner_app": ""})
        return True, None

    first = asyncio.ensure_future(state.mutate_folders(_add_a))
    await first_in_write.wait()
    first.cancel()

    async def _second() -> None:
        order.append("second-acquiring")
        await state.mutate_folders(_add_b)

    second = asyncio.ensure_future(_second())
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await second
    monkeypatch.setattr(asyncio, "to_thread", real_to_thread)

    # NEGATIVE CONTROLS -- hold on unfixed code too.
    assert first.cancelled(), "the first transaction was not cancelled"
    assert "first-done" in order and "second-done" in order, f"both writes ran: {order}"

    assert order.index("first-done") < order.index("second-done"), (
        f"the first (cancelled) write finished AFTER the second, so it overwrote the "
        f"store with stale bytes: {order}. The lock has to outlive the worker"
    )
    # The store must end with the SECOND mutation's value, not be reverted by the
    # older worker landing last.
    assert [f["id"] for f in state._folders] == ["f1", "fa", "fb"], (
        f"final store is {[f['id'] for f in state._folders]!r}; the later mutation's "
        "write was lost to the cancelled one finishing last"
    )


@pytest.mark.asyncio
async def test_a_cancelled_tag_write_still_publishes_the_committed_vocabulary(
    tmp_path, monkeypatch
) -> None:
    """Cancelling mid-write must not leave the committed tag set behind disk.

    ``asyncio.to_thread`` cannot interrupt its worker, so a cancelled handler still
    lands the bytes. If the publication is skipped, disk holds the NEW vocabulary while
    ``_committed_tag_ids`` holds the OLD one -- and that is the damaging direction:
    ``tag_ids_for_restore`` and the fork's producer validation PRUNE against the
    committed set, so a valid assignment is stripped and the next save makes the loss
    durable.
    """
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "urgent", "color": "#ff0000"}]
    state.publish_committed_tag_ids(state._tags)
    new_snapshot = [
        {"id": "t1", "name": "urgent", "color": "#ff0000"},
        {"id": "t2", "name": "later", "color": "#00ff00"},
    ]

    in_write = asyncio.Event()
    landed: list[str] = []

    def _slow_write(_state, snapshot):
        # Runs in the worker thread. Signals that the write is in flight, then
        # completes -- exactly like a real write the cancellation cannot stop.
        loop.call_soon_threadsafe(in_write.set)
        time.sleep(0.05)
        landed.append("yes")

    monkeypatch.setattr(mod, "_write_tags_snapshot", _slow_write)
    loop = asyncio.get_running_loop()

    task = asyncio.ensure_future(mod._commit_tags_snapshot(state, new_snapshot))
    await in_write.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Let the shielded write finish and its done-callback run on the loop.
    for _ in range(50):
        if landed:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)

    # NEGATIVE CONTROLS -- both hold on unfixed code.
    assert task.cancelled(), "the handler was not actually cancelled"
    assert landed == ["yes"], "the write did not complete; premise broken"

    assert state._committed_tag_ids == frozenset({"t1", "t2"}), (
        f"the write landed t2 on disk but the committed vocabulary still reads "
        f"{state._committed_tag_ids!r}. Every restore and the fork's producer "
        "validation prunes against this set, so t2 would be stripped off any slot "
        "carrying it -- publication must survive cancellation of the awaiting handler"
    )


@pytest.mark.asyncio
async def test_a_cancelled_folder_write_still_publishes_and_does_not_roll_back(
    tmp_path, monkeypatch
) -> None:
    """The folder side loses TWO things on cancellation, so both are pinned.

    ``CancelledError`` is not an ``Exception``, so the rollback arm does not catch it:
    unfixed, the live list keeps the new folder while ``on_committed`` never fires, and
    live state disagrees with both disk and the committed snapshot.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    in_write = asyncio.Event()
    landed: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _slow_to_thread(func, /, *args, **kwargs):
        async def _run():
            in_write.set()
            await asyncio.sleep(0.05)
            landed.append("yes")
            return func(*args, **kwargs)

        return await _run()

    monkeypatch.setattr(asyncio, "to_thread", _slow_to_thread)

    def _add(folders):
        folders.append({"id": "f2", "name": "Later", "parent_id": "", "owner_app": ""})
        return True, None

    task = asyncio.ensure_future(state.mutate_folders(_add))
    await in_write.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    for _ in range(50):
        if landed:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    monkeypatch.setattr(asyncio, "to_thread", real_to_thread)

    # NEGATIVE CONTROLS.
    assert task.cancelled(), "the handler was not actually cancelled"
    assert landed == ["yes"], "the write did not complete; premise broken"

    assert state._committed_folder_ids == frozenset({"f1", "f2"}), (
        f"the write landed f2 but the committed folder vocabulary reads "
        f"{state._committed_folder_ids!r}; folder_id_for_restore and the fork's "
        "producer validation prune against it, so f2 would be stripped"
    )
    assert [f["id"] for f in state._folders] == ["f1", "f2"], (
        "the live folder list was rolled back on CANCELLATION, but the shielded write "
        "still landed f2 -- live state now disagrees with disk"
    )


@pytest.mark.asyncio
async def test_the_fork_still_inherits_when_the_vocabulary_is_unknown(
    tmp_path, monkeypatch
) -> None:
    """UNKNOWN vocabulary must FAIL OPEN, so validating the fork cannot lose data.

    The guard against the obvious regression in the test above: if the validators
    pruned on ``None`` too, every fork taken before the stores finished loading would
    silently drop its parent's folder and tags.
    """
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("dashboard:forkparent2", "user", "one")
    log.append("dashboard:forkparent2", "assistant", "two")
    parent = state.get_or_create_slot("forkparent2")
    parent.append("user", "one", "msg msg-u")
    parent.append("assistant", "two", "msg msg-a")
    parent.drain()
    parent.folder_id = "f1"
    parent.tags = ["t1"]
    state._committed_folder_ids = None
    state._committed_tag_ids = None

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/forkparent2/fork",
            json={"at_message_index": 1, "prompt": "forked"},
        )
        status = resp.status
        payload = await resp.json()
    assert status == 200, f"fork failed for another reason: {status} {payload}"

    child = state._slots[payload["key"]]
    assert child.folder_id == "f1", (
        "the fork dropped its parent's folder against an UNKNOWN vocabulary; the "
        "validator must fail open, or an early fork loses its placement"
    )
    assert child.tags == [
        "t1"
    ], "the fork dropped its parent's tags against an UNKNOWN vocabulary; same rule"


# ── The sweep-merge PROTOCOL, pinned independently of the two call sites ──────
# The behavioural tests above exercise the folder and tag sweeps. These pin the
# CONTRACT a THIRD sweep site would have to satisfy, so the protocol is covered
# even though no third site exists yet: every outcome must have a disposition,
# an unrecognised one must be REFUSED rather than silently treated as transient,
# and a guard must state a real boolean belief.


@pytest.mark.asyncio
async def test_the_sweep_merge_protocol_disposes_of_every_outcome(tmp_path, monkeypatch) -> None:
    """Each ``SweepMergeOutcome`` must get its own disposition, not a default.

    Parameterised over the enum ITSELF rather than over the two existing sweeps, so
    adding a member without giving it a disposition fails here. The dispositions are
    the three that must never be confused: COMMITTED owes nothing, SUPERSEDED
    reconciles via ``adopt``, UNCONFIRMED arms ``_dirty`` for the flush.
    """
    from kiro_crew.dashboard.chat_persistence import persist_swept_slot_meta

    expected = {
        SweepMergeOutcome.COMMITTED: (False, False),
        SweepMergeOutcome.SUPERSEDED: (True, False),
        SweepMergeOutcome.UNCONFIRMED: (False, True),
    }
    assert set(expected) == set(SweepMergeOutcome), (
        "a SweepMergeOutcome member has no disposition pinned here. Every member must "
        "name one: the two non-committed outcomes want OPPOSITE handling, so a new "
        "member falling through to the transient branch would silently arm _dirty and "
        "the periodic flush would write back the very value the guard refused"
    )

    for outcome, (want_adopt, want_dirty) in expected.items():
        state = _make_state(tmp_path)
        slot = _slot(f"s-{outcome.value}", folder_id="f1")
        state._slots[slot.key] = slot
        slot._dirty = False
        adopted: list[dict] = []

        async def _merge_shim(_st, _sl, _fields, _o=outcome, **_kw):
            return (_o, {"folder_id": "f_ondisk"})

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

        await persist_swept_slot_meta(
            state,
            slot,
            {"folder_id": ""},
            guard=lambda meta: True,
            adopt=lambda _sl, observed, _sub: adopted.append(observed),
            label="protocol probe",
            expected_history_key=slot_history_key(slot),
        )

        assert bool(adopted) is want_adopt, (
            f"{outcome.value}: adopt was "
            f"{'not called' if want_adopt else 'called'} when it should "
            f"{'have been' if want_adopt else 'not have been'}. Only SUPERSEDED means "
            "another writer owns the field, and only then is the observed value the "
            "newer truth to reconcile against"
        )
        assert slot._dirty is want_dirty, (
            f"{outcome.value}: _dirty is {slot._dirty}, expected {want_dirty}. Arming it "
            "on SUPERSEDED is the specific bug this protocol exists to prevent -- the "
            "flush full-saves from memory and would rewrite the refused value"
        )


# ── Gate 3: every vocabulary adopter validates, or says why it need not ────────

#: The two shared readers that make an adopted id safe. An assignment whose value
#: comes from one of these is sanctioned wherever it appears -- that is the whole
#: point of routing through them, so a NEW adopter that validates needs no edit here.
#: The shared restore-side helper, accepted in place of a direct validator call because
#: it IS that call, factored out. The gate re-checks its body; see its own docstring.
_PARKED_REVALIDATOR = "_revalidate_parked_vocabulary"

_VOCABULARY_VALIDATORS = frozenset({"folder_id_for_restore", "tag_ids_for_restore"})

#: Calls that MINT a slot, so it can carry no vocabulary from before that call. This is
#: what exempts session import from the parked-restore gate without naming it.
_SLOT_FACTORIES = frozenset({"get_or_create_slot", "create_slot", "put_slot"})

#: Receiver names that denote a chat slot. ``job.folder_id`` (cron) and ``art.tags``
#: (artifacts) are different objects with their own vocabularies and are deliberately
#: out of scope; keying on the receiver keeps them out without an exclusion list.
_SLOT_RECEIVERS = ("slot",)

#: Functions that assign a slot's ``folder_id``/``tags`` WITHOUT routing the value
#: through a validator, each with the structural reason it is safe anyway. This is the
#: allowlist a new adopter must either avoid (by validating) or join (by proving one of
#: these shapes and saying so here). It is deliberately keyed by function name and
#: deliberately verbose: the reason is the reviewable part.
#:
#: This allowlist is INTERIM, not permanent: it exists to bridge the deferred
#: merge-aware-save layer decision tracked at kirodotdev/KiroCrew#8361, and retires with
#: that fix alongside ``persist_swept_slot_meta`` and the force-save census gate. If you
#: are adding an entry, read that issue first -- a growing allowlist is the signal the
#: decision is overdue, not that the exemption list needs to be longer.
_UNVALIDATED_VOCABULARY_WRITERS: dict[str, str] = {
    # Raw assign then the shared validator, with no await between the two statements, so
    # the unvalidated value is never observable outside the frame.
    "_rehydrate_slot_from_history": "raw assign then the shared validator; no await between",
    "_apply_recent_session": "raw assign then the shared validator; no await between",
    # Two-step raw-then-validate, with NO await between the two statements, so the
    # unvalidated value is never observable outside the frame.
    "api_chat_slot_resume": "raw assign then validator, plus a clear-to-empty; no await between",
    # Holds the tags write lock for the whole read-modify-write, so a concurrent
    # vocabulary delete cannot interleave.
    "_auto_tag_inner": "holds tags_write_lock across the read-modify-write",
    "api_chat_slot_tags": "holds tags_write_lock across the read-modify-write",
    # REMOVAL only. Stripping an id, or clearing to "", cannot introduce a dangling
    # reference -- the failure mode this gate exists for is adopting one. Both are
    # nested helpers, so the exemption is keyed to the helper, not its handler.
    "api_chat_tag_delete": "removal only -- strips the deleted id, never adopts",
    "api_chat_folder_delete": "removal only -- clears folder_id to the empty string",
    "_adopt_observed_tags": (
        "prunes against pre_delete_committed (the committed vocabulary captured at "
        "transaction start) with the same None-fails-open rule, plus a list TYPE check; "
        "the capture rather than a live read because it cannot be moved by this "
        "handler's own publication, so the adopt's meaning does not depend on when "
        "that publication lands"
    ),
    # Vocabulary read and slot write in one synchronous run: no await sits between the
    # existence check and the assignment, so the check cannot be overtaken.
    "api_chat_slot_drop": "tag_index read and write with no await between",
    "api_chat_slot_folder": "target check and assign with no await between; revert validates",
    "api_chat_slot_create": "target check and assign with no await between; revert validates",
    # Inherits the parent's folder id verbatim. A folder deleted inside the fork's awaits
    # leaves the child rendering as Unfiled, which the spec rules a benign residue.
    "api_chat_slot_fork": "inherits folder_id verbatim; residue is render-as-Unfiled",
    "create_session": "existence confirmed under the folder-store lock, no await before assign",
    # The value assigned is itself the OUTPUT of a validator, bound one statement
    # earlier, so the call is present in the function but not on this line.
    "surface_channel_session": "assigns the result of folder_id_for_restore bound above",
}


def _assigns_slot_vocabulary(node: ast.Assign) -> str | None:
    """Return the attribute name when *node* assigns a slot's folder_id/tags."""
    for target in node.targets:
        if not isinstance(target, ast.Attribute) or target.attr not in ("folder_id", "tags"):
            continue
        recv = target.value
        if not isinstance(recv, ast.Name):
            continue
        # ``slot``, ``new_slot`` -- but not ``self`` (the dataclass's own field), and
        # not ``job``/``art``, which are other objects entirely.
        if recv.id == "self":
            continue
        if recv.id in _SLOT_RECEIVERS or recv.id.endswith("_slot") or recv.id.startswith("slot_"):
            return target.attr
    return None


def _validator_bound_locals(scope: ast.AST) -> set[str]:
    """Locals bound to a validator fetched by NAME, e.g. ``getattr(state, "...", None)``.

    Recognising this indirection rather than allowlisting the functions that use it: an
    entry would silence the whole function, including any genuinely unvalidated adopt it
    later grows, whereas this keeps the check on every assignment. The indirection is how
    a caller across an app boundary reaches a state method it cannot assume is present.
    """
    out: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        for sub in ast.walk(node.value):
            if (
                isinstance(sub, ast.Call)
                and getattr(sub.func, "id", "") == "getattr"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and sub.args[1].value in _VOCABULARY_VALIDATORS
            ):
                out.update(t.id for t in node.targets if isinstance(t, ast.Name))
            # A local bound DIRECTLY from a validator carries the same authority as the
            # call, so a later assignment from that local is validated too.
            if isinstance(sub, ast.Call):
                fn = sub.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if called in _VOCABULARY_VALIDATORS:
                    out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


def _routes_through_a_validator(node: ast.Assign, scope: ast.AST | None = None) -> bool:
    """True when the assigned value comes from one of the shared validators."""
    indirect = _validator_bound_locals(scope) if scope is not None else set()
    for sub in ast.walk(node.value):
        if isinstance(sub, ast.Name) and sub.id in indirect:
            return True
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _VOCABULARY_VALIDATORS or name in indirect:
                return True
    return False


def find_unvalidated_vocabulary_writes(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str, str]]:
    """Return ``(path, lineno, enclosing_function, attribute)`` for unsanctioned adopters.

    Attribution is to the INNERMOST enclosing function, via ``_scope_nodes``. A plain
    ``ast.walk`` from each function descends into nested defs, so an assignment inside a
    nested helper would be reported once per ancestor -- and the allowlist would have to
    name the outer handler to silence a decision the inner helper actually makes. Both
    real cases here are nested (``_remove`` inside the folder delete,
    ``_adopt_observed_tags`` inside the tag delete), so the exemption belongs to the
    helper that does the assigning.

    Retires with the allowlist it enforces, at the merge-aware-save layer decision tracked
    in kirodotdev/KiroCrew#8361 -- this detector is scaffolding, not a standing rule.
    """
    tree = ast.parse(source)
    out: list[tuple[str, int, str, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _scope_nodes(scope):
            if not isinstance(node, ast.Assign):
                continue
            attr = _assigns_slot_vocabulary(node)
            if attr is None:
                continue
            if _routes_through_a_validator(node, scope):
                continue
            if scope.name in _UNVALIDATED_VOCABULARY_WRITERS:
                continue
            out.append((path, node.lineno, scope.name, attr))
    return out


def test_a_parked_restore_persists_the_prune_it_just_made(tmp_path, monkeypatch) -> None:
    """The parked-teardown restore's prune must reach DISK too.

    Its sibling above asserts the in-memory field only. The parked slot re-enters the
    registry where the periodic flush is the writer that would carry the prune, and that
    flush skips a slot whose ``_dirty`` is unset -- so without the arm the revalidation is
    discarded and the id stays on disk until some later pass removes it.
    """
    from kiro_crew.apps.builtins.spec_builder.backend import repository as sb_repo
    from kiro_crew.apps.builtins.spec_builder.backend import runtime as sb_runtime

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "T1", "color": "#222222", "order": 0}]
    state.publish_committed_tag_ids(state._tags)

    slot_key = sb_repo._slot_key("probe")
    slot = state.get_or_create_slot(slot_key)
    slot._app = sb_repo.APP_NAME
    slot.tags = ["t1"]
    slot.messages = [{"role": "user", "content": "hello"}]
    key = slot_history_key(slot)
    _log_session(state, key, {"tags": ["t1"]})

    async def _delete_the_tag_then_fail(state_, slot_, **kwargs):
        state_._tags[:] = []
        state_.publish_committed_tag_ids(state_._tags)
        raise RuntimeError("archive failed")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
        _delete_the_tag_then_fail,
    )

    archived = asyncio.run(
        sb_runtime._teardown_worker_slot(state, "probe", only_slot=slot, require_archive=True)
    )
    assert archived is False, "a failed require_archive teardown must report failure"
    assert slot.tags == [], (
        "fixture: the parked restore must have pruned in memory before this test can say "
        f"anything about durability; got {slot.tags!r}"
    )

    state._flush_dirty_slots()

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("tags", []) == [], (
        f"the flush left tags={meta.get('tags')!r} on disk after the parked "
        "restore pruned them in memory. The revalidation must arm _dirty, or the periodic "
        "flush skips the slot and the in-memory prune is lost"
    )


def test_the_validator_indirection_rule_accepts_only_a_real_validator() -> None:
    """Accepting a validator fetched by name must not accept its neighbours.

    The adopter gate treats ``v = getattr(state, "folder_id_for_restore", None)`` followed
    by ``slot.folder_id = v(...)`` as validated. That acceptance is the kind that quietly
    widens: a getattr of any other attribute, or a call to a different local, reads almost
    identically. Both must still be flagged, or the gate stops seeing real adopters.
    """
    must_flag = {
        "plain adopt": "def f(slot, cand):\n    slot.folder_id = cand\n",
        "getattr of a non-validator": (
            "def f(state, slot):\n"
            '    g = getattr(state, "some_other_reader", None)\n'
            "    slot.folder_id = g(slot.folder_id)\n"
        ),
        "a different local is called": (
            "def f(state, slot, other):\n"
            '    v = getattr(state, "folder_id_for_restore", None)\n'
            "    slot.folder_id = other(slot.folder_id)\n"
        ),
    }
    for label, src in must_flag.items():
        assert find_unvalidated_vocabulary_writes(src), f"gate went blind to: {label}"

    must_pass = {
        "the validator via getattr": (
            "def f(state, slot):\n"
            '    v = getattr(state, "folder_id_for_restore", None)\n'
            "    slot.folder_id = v(slot.folder_id)\n"
        ),
        "the direct call": (
            "def f(state, slot):\n    slot.folder_id = state.folder_id_for_restore(slot.folder_id)\n"
        ),
        "tags via getattr": (
            "def f(state, slot):\n"
            '    t = getattr(state, "tag_ids_for_restore", None)\n'
            "    slot.tags = t(list(slot.tags))\n"
        ),
    }
    for label, src in must_pass.items():
        assert not find_unvalidated_vocabulary_writes(src), f"gate wrongly flags: {label}"


def _creates_its_own_slot(scope: ast.AST, name: str) -> bool:
    """True when *name* is bound in *scope* from a slot FACTORY rather than held.

    The discriminator that keeps this gate free of a maintained exemption list. A slot
    this function created cannot carry vocabulary from before the call, so putting it
    into the registry adopts nothing; a slot the function merely HELD across an await
    can carry ids a delete has since removed. Session import is the former, the two
    Spec Builder teardown restores the latter.
    """
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Call):
                fn = sub.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if called in _SLOT_FACTORIES:
                    return True
    return False


def find_unvalidated_parked_restores(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for parked slots restored unchecked.

    A SECOND entry path into the live registry, and the one the attribute-write gate
    structurally cannot see: ``state._slots[key] = slot`` re-registers a whole slot
    OBJECT, so its ``folder_id`` and ``tags`` arrive with no assignment to match on.
    That is why two such restores existed unnoticed while five gates passed.

    Flagged when a function puts a slot it did not create back into the registry with
    an await before that point and no validator call anywhere in the function. The
    await is what makes it a hazard: the vocabulary can be deleted while the slot is
    unreachable from both delete sweeps, whose two snapshots are a captured list and
    the live view -- a parked slot is in neither.
    """
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(sub, ast.Call)
            and (
                sub.func.attr
                if isinstance(sub.func, ast.Attribute)
                else getattr(sub.func, "id", "")
            )
            in (_VOCABULARY_VALIDATORS | {_PARKED_REVALIDATOR})
            for sub in ast.walk(scope)
        ):
            continue
        awaits = [n.lineno for n in ast.walk(scope) if isinstance(n, ast.Await)]
        for node in _scope_nodes(scope):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "_slots"
                ):
                    continue
                if not isinstance(node.value, ast.Name):
                    continue
                if _creates_its_own_slot(scope, node.value.id):
                    continue
                if not any(ln < node.lineno for ln in awaits):
                    continue
                out.append((path, node.lineno, scope.name))
    return out


def test_every_vocabulary_adopter_validates_or_is_allowlisted() -> None:
    """A new writer of ``slot.folder_id``/``slot.tags`` must validate the id it adopts.

    WHY THIS IS A GATE RATHER THAN A CONVENTION. The delete handlers do not re-sweep
    the live view after their awaits, so nothing catches an id adopted mid-window on
    whoever's behalf. Validating at the producer is what allows that pass to be absent,
    so correctness rests on every adopter either routing through
    ``folder_id_for_restore``/``tag_ids_for_restore`` or being structurally unable to
    adopt a deleted id. Nothing about ``slot.folder_id = folder_id`` looks wrong at the
    call site, and the regression it reintroduces is silent: a durable dangling id that
    only surfaces as a phantom folder or tag after a restart.

    The four sibling gates cannot see this. One pins loop iteration, one pins the
    tag-snapshot write chain, one pins the folder publication choke point, one pins the
    force-save census; none notices a
    new assignment. So this converts the
    producers-validate-at-source rule from prose into a check, in the same allowlist
    shape as the tag-write gate: validate, or join
    ``_UNVALIDATED_VOCABULARY_WRITERS`` with the structural reason you are exempt.

    A RENAME IS NOT A VIOLATION, and the companion
    ``test_the_vocabulary_allowlist_has_no_stale_entries`` exists so a rename says so.
    The allowlist is keyed on the function NAME, so renaming an exempt function drops its
    exemption and this gate would then report the renamed function as an unvalidated
    adopter -- true in letter, but it names the wrong cause and sends the reader looking
    for a validation bug that is not there. The companion test fails FIRST with the real
    instruction: update the key.
    """
    violations = collect_repo_violations(find_unvalidated_vocabulary_writes)
    if violations:
        detail = "\n".join(
            f"  {path}:{lineno} in {func}() assigns slot.{attr}"
            for path, lineno, func, attr in violations
        )
        raise AssertionError(
            "a slot vocabulary id is adopted without validation.\n\n"
            "Folder and tag deletes no longer re-sweep the live view afterwards, so "
            "nothing downstream will strip an id that was deleted while this handler "
            "was awaiting. An unvalidated adoption therefore becomes durable on the "
            "next save and shows up as a folder or tag that no longer exists.\n\n"
            "Either route the value through the shared reader:\n"
            "    slot.folder_id = state.folder_id_for_restore(candidate)\n"
            "    slot.tags = state.tag_ids_for_restore(candidates)\n"
            "or, if the site cannot adopt a stale id (removal only, lock held across "
            "the read-modify-write, or no await between the vocabulary check and the "
            "assignment), add it to _UNVALIDATED_VOCABULARY_WRITERS with that reason:\n"
            f"{detail}"
            "\n\nCLASSIFY THE SITE FIRST, because the two classes want OPPOSITE "
            "behaviour and only one of them is what this gate is asking for. A COLD-START "
            "adopter -- one reading a persisted value it did not author and cannot date -- "
            "adopts VERBATIM and must NOT route through the reader: absence there is "
            "ambiguous, so pruning would unfile filings made after a stale store's "
            "snapshot. A MID-SESSION adopter -- one holding a value it captured ITSELF "
            "before an await -- validates, because absence afterwards is real evidence of "
            "a delete. If the site is cold-start, the allowlist entry is the correct "
            "answer and routing through the reader is the wrong one.\n\n"
            "The full contract, and why the sweeps have no trailing re-sweep to fall "
            "back on, is in docs/system-specs/modules/history.md under "
            '"Vocabulary Deletes and Slot Metadata Persistence". This allowlist is INTERIM: '
            "it retires with the merge-aware-save layer decision TRACKED at "
            "kirodotdev/KiroCrew#8361, so prefer taking that decision over adding an entry."
        )


def test_the_vocabulary_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted name must still exist as a function in the tree.

    THE RENAME TRAP THIS CLOSES. ``_UNVALIDATED_VOCABULARY_WRITERS`` is keyed on the
    function NAME, which is the only key an AST scan can match cheaply -- but it means a
    rename for reasons entirely unrelated to vocabulary handling silently drops that
    function's exemption. The adopter gate would then fire on the renamed function and
    report it as adopting an id without validation. That report is literally true and
    diagnostically useless: it sends the reader hunting for a missing validator when the
    only thing that changed is a dict key.

    So this test fails first, and says what to do. It also catches the opposite drift --
    an exemption kept alive for a function that was DELETED, which otherwise sits in the
    allowlist forever asserting a structural reason about deleted code.
    """
    root = _src_root()
    defined: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)

    # Positive control: the scan must actually resolve names, or every entry below
    # would read as stale and this test would fail for the wrong reason.
    assert "persist_swept_slot_meta" in defined, (
        "the function scan found nothing recognisable; it is not reading the tree, so "
        "its verdict about the allowlist means nothing"
    )

    stale = sorted(name for name in _UNVALIDATED_VOCABULARY_WRITERS if name not in defined)
    if stale:
        detail = "\n".join(f"  {name}: {_UNVALIDATED_VOCABULARY_WRITERS[name]}" for name in stale)
        raise AssertionError(
            "_UNVALIDATED_VOCABULARY_WRITERS names function(s) that no longer exist.\n\n"
            "This is a BOOKKEEPING failure, not a validation failure. Either the "
            "function was renamed -- in which case update the key and keep the reason -- "
            "or it was deleted, in which case drop the entry. Leaving it stale silently "
            "un-exempts the renamed function, and the adopter gate will then blame it for "
            "a missing validator it never needed:\n"
            f"{detail}"
        )


def test_the_vocabulary_adopter_gate_scanned_a_non_empty_tree() -> None:
    """Positive control: prove the scan REACHES real adopters, not just some files.

    An empty walk, a renamed attribute or a receiver-name rule that stopped matching
    would each make the gate above pass while detecting nothing. Assert it still finds
    the known sanctioned sites by scanning with the allowlist emptied.
    """

    def _find_ignoring_the_allowlist(source: str, path: str = "<source>") -> list[tuple]:
        tree = ast.parse(source)
        found: list[tuple] = []
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign) and _assigns_slot_vocabulary(node):
                    found.append((path, node.lineno, scope.name))
        return found

    seen = collect_repo_violations(_find_ignoring_the_allowlist)
    assert len(seen) >= 20, (
        f"the adopter scan reached only {len(seen)} slot-vocabulary assignments; it "
        "should see every one in src/. A near-empty result means the walk, the "
        "attribute names or the receiver rule stopped matching, which would make the "
        "gate above pass vacuously"
    )
    functions = {func for _p, _l, func in seen}
    assert "api_chat_slot_fork" in functions, (
        "the scan did not reach api_chat_slot_fork, a producer validated at "
        "source -- so the gate is not looking where the invariant matters"
    )


def test_the_adopter_gate_flags_an_unvalidated_new_adopter() -> None:
    """Negative control: the shape the gate exists to catch must actually trip it."""
    src = (
        "async def api_chat_slot_something_new(request):\n"
        "    slot = state._slots.get(name)\n"
        "    body = await read_bounded_json(request)\n"
        "    slot.folder_id = body['folder_id']\n"
    )
    hits = find_unvalidated_vocabulary_writes(src, "m.py")
    assert [(h[2], h[3]) for h in hits] == [("api_chat_slot_something_new", "folder_id")], (
        "an unvalidated folder_id adoption in a brand-new handler was not flagged; " f"got {hits}"
    )


def test_the_adopter_gate_accepts_a_validated_new_adopter() -> None:
    """A new adopter that routes through a validator needs no allowlist edit."""
    src = (
        "async def api_chat_slot_something_new(request):\n"
        "    slot = state._slots.get(name)\n"
        "    body = await read_bounded_json(request)\n"
        "    slot.tags = state.tag_ids_for_restore(body['tags'])\n"
    )
    assert find_unvalidated_vocabulary_writes(src, "m.py") == [], (
        "a validated adoption was flagged; routing through the shared reader is "
        "exactly what the gate is asking for, so it must not require an allowlist entry"
    )


def test_the_adopter_gate_ignores_non_slot_receivers() -> None:
    """``job.folder_id`` and ``art.tags`` are other objects with their own vocabularies."""
    src = (
        "def update(job, art):\n"
        "    job.folder_id = kwargs['folder_id'] or ''\n"
        "    art.tags = _validate_tags(tags)\n"
        "    self.tags = incoming\n"
    )
    assert find_unvalidated_vocabulary_writes(src, "m.py") == [], (
        "a non-slot receiver was flagged; cron jobs and artifacts carry unrelated "
        "folder/tag fields and pulling them in would make the gate noisy enough to "
        "be disabled"
    )


#: The machine-checkable SHAPE each allowlist entry claims, so a prose reason cannot drift
#: away from the code it describes. Coverage in both directions is asserted below.
_ADOPTER_CLAIM_SHAPES: dict[str, str] = {
    "_rehydrate_slot_from_history": "VALIDATES_WITHOUT_AWAIT",
    "_apply_recent_session": "VALIDATES_WITHOUT_AWAIT",
    "api_chat_slot_resume": "VALIDATES_WITHOUT_AWAIT",
    "_auto_tag_inner": "HOLDS_A_LOCK",
    "api_chat_slot_tags": "HOLDS_A_LOCK",
    "api_chat_tag_delete": "REMOVAL_ONLY",
    "api_chat_folder_delete": "REMOVAL_ONLY",
    "_adopt_observed_tags": "PRUNES_AGAINST_CAPTURE",
    "api_chat_slot_drop": "GUARDED_WITHOUT_AWAIT",
    "api_chat_slot_folder": "GUARDED_WITHOUT_AWAIT",
    "api_chat_slot_create": "GUARDED_WITHOUT_AWAIT",
    "api_chat_slot_fork": "TAGS_VALIDATED_FOLDER_VERBATIM",
    "create_session": "GUARDED_WITHOUT_AWAIT",
    "surface_channel_session": "VALIDATES_WITHOUT_AWAIT",
}


def _src_function_index() -> dict[str, list[tuple[str, ast.AST]]]:
    """Map function name to every ``(path, node)`` defining it under ``src/``."""
    index: dict[str, list[tuple[str, ast.AST]]] = {}
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append((str(path), node))
    return index


def _calls_a_validator(scope: ast.AST) -> bool:
    """True when *scope* calls one of the shared vocabulary validators by name."""
    for node in _scope_nodes(scope):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in _VOCABULARY_VALIDATORS:
                    return True
    return False


def _await_free_between_assign_and_validator(scope: ast.AST) -> bool:
    """True when no ``await`` sits between an unvalidated adopt and a validator call.

    The claim these entries make is that the raw value is never observable outside the
    frame. That holds only while no suspension point separates the two statements, so
    the check is for an ``await`` in the statement range they span.
    """
    nodes = list(_scope_nodes(scope))
    adopt_lines = [
        n.lineno for n in nodes if isinstance(n, ast.Assign) and _assigns_slot_vocabulary(n)
    ]
    validator_lines: list[int] = []
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in _VOCABULARY_VALIDATORS:
                    validator_lines.append(sub.lineno)
    if not adopt_lines or not validator_lines:
        return False
    await_lines = {
        sub.lineno for node in nodes for sub in ast.walk(node) if isinstance(sub, ast.Await)
    }
    for adopt in adopt_lines:
        near = min(validator_lines, key=lambda v: abs(v - adopt))
        lo, hi = sorted((adopt, near))
        if any(lo < line < hi for line in await_lines):
            return False
    return True


def _adopt_is_removal_or_filter(scope: ast.AST) -> bool:
    """True when every unvalidated adopt in *scope* strips or clears rather than adopts."""
    seen = False
    for node in _scope_nodes(scope):
        if not isinstance(node, ast.Assign) or not _assigns_slot_vocabulary(node):
            continue
        seen = True
        value = node.value
        clears = isinstance(value, ast.Constant) and value.value == ""
        filters = isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp))
        if not (clears or filters):
            return False
    return seen


def _references_a_capture(scope: ast.AST) -> bool:
    """True when *scope* reads a captured committed vocabulary."""
    return any(isinstance(node, ast.Name) and "committed" in node.id for node in ast.walk(scope))


def _guarded_without_await(scope: ast.AST) -> bool:
    """True when each adopt sits under a guard that no ``await`` separates from it.

    These entries claim an existence or target check immediately ahead of the assign,
    rather than a validator call. What makes that safe is the absence of a suspension
    point between the guard and the write, so the check cannot be overtaken. The guard
    is the innermost enclosing ``if``/``with`` block, and the window is that block up to
    the adopt.
    """
    adopts = [
        node
        for node in _scope_nodes(scope)
        if isinstance(node, ast.Assign) and _assigns_slot_vocabulary(node)
    ]
    if not adopts:
        return False
    guards = [
        node for node in ast.walk(scope) if isinstance(node, (ast.If, ast.With, ast.AsyncWith))
    ]
    for adopt in adopts:
        enclosing = [
            g
            for g in guards
            if g.lineno < adopt.lineno and (getattr(g, "end_lineno", g.lineno) or 0) >= adopt.lineno
        ]
        if not enclosing:
            return False
        innermost = max(enclosing, key=lambda g: g.lineno)
        if any(
            isinstance(sub, ast.Await) and innermost.lineno < sub.lineno < adopt.lineno
            for sub in ast.walk(innermost)
        ):
            return False
    return True


def _shape_holds(shape: str, scope: ast.AST) -> bool:
    """Dispatch *shape* to its checker against the real function *scope*."""
    if shape == "ADOPTS_VERBATIM":
        return not _calls_a_validator(scope)
    if shape == "TAGS_VALIDATED_FOLDER_VERBATIM":
        # Stricter than either neighbour: it pins BOTH halves, so the entry fails if the
        # folder arm comes back OR if the tag arm goes away.
        names = {
            sub.func.attr if isinstance(sub.func, ast.Attribute) else getattr(sub.func, "id", "")
            for sub in ast.walk(scope)
            if isinstance(sub, ast.Call)
        }
        return "tag_ids_for_restore" in names and "folder_id_for_restore" not in names
    if shape == "VALIDATES_WITHOUT_AWAIT":
        return _calls_a_validator(scope) and _await_free_between_assign_and_validator(scope)
    if shape == "GUARDED_WITHOUT_AWAIT":
        return _guarded_without_await(scope)
    if shape == "HOLDS_A_LOCK":
        return any(
            isinstance(node, (ast.With, ast.AsyncWith))
            and "lock" in ast.dump(node.items[0].context_expr).lower()
            for node in ast.walk(scope)
        )
    if shape == "REMOVAL_ONLY":
        return _adopt_is_removal_or_filter(scope)
    if shape == "PRUNES_AGAINST_CAPTURE":
        return _references_a_capture(scope)
    raise AssertionError(f"unknown adopter claim shape: {shape}")


def test_every_allowlisted_adopter_declares_a_checkable_shape() -> None:
    """The allowlist and the shape map must name exactly the same functions.

    Without this the shape map is optional, so a new entry could carry a prose reason
    that nothing reads while the backstop below silently skips it -- which is the
    unbacked-allowlist shape this gate exists to close.
    """
    allowlisted = set(_UNVALIDATED_VOCABULARY_WRITERS)
    declared = set(_ADOPTER_CLAIM_SHAPES)
    assert allowlisted == declared, (
        "the adopter allowlist and its shape map disagree.\n"
        f"  allowlisted with no declared shape: {sorted(allowlisted - declared)}\n"
        f"  declared but not allowlisted:       {sorted(declared - allowlisted)}\n\n"
        "Every exemption must state a shape a checker can verify against the source. "
        "Adding a prose reason alone leaves the exemption unbacked."
    )


def test_every_allowlisted_adopter_still_matches_its_declared_shape() -> None:
    """The backstop: each exemption's stated shape is re-derived from the real source.

    A prose reason is written once and read never. This re-checks every entry against
    the function it exempts, so an exemption whose justification stops being true fails
    here instead of silently widening what the adopter gate lets through.
    """
    index = _src_function_index()
    broken: list[str] = []
    for name, shape in sorted(_ADOPTER_CLAIM_SHAPES.items()):
        definitions = index.get(name, [])
        assert definitions, (
            f"{name} is allowlisted but defines no function under src/; the "
            "bookkeeping gate above should have caught this first"
        )
        if not any(_shape_holds(shape, node) for _path, node in definitions):
            reason = _UNVALIDATED_VOCABULARY_WRITERS[name]
            broken.append(f"  {name} claims {shape}\n      recorded reason: {reason}")
    assert not broken, (
        "an allowlisted adopter no longer matches the shape its exemption claims:\n"
        + "\n".join(broken)
        + "\n\nFIX THE CODE OR THE CLAIM, not this gate. Either restore the shape the "
        "entry depends on, or -- if the change was deliberate -- correct the recorded "
        "reason AND the declared shape so the exemption states what the code does."
    )


def test_the_claim_backstop_rejects_a_reason_that_stopped_being_true() -> None:
    """Negative control: a VALIDATES_WITHOUT_AWAIT claim on a verbatim adopt must fail.

    This is the drift the backstop exists to catch, and it is not hypothetical: the two
    bulk restore paths are exempted here and adopt persisted tags verbatim by design, so
    a claim that they validate would be false while reading plausibly.
    """
    src = (
        "async def adopt_without_validating(request):\n"
        "    slot = state._slots.get(name)\n"
        "    slot.tags = [str(t) for t in meta['tags']]\n"
    )
    scope = next(
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert not _shape_holds("VALIDATES_WITHOUT_AWAIT", scope), (
        "the backstop accepted a validation claim for a function that calls no "
        "validator, so a stale exemption reason would survive it"
    )
    assert _shape_holds("ADOPTS_VERBATIM", scope), (
        "the verbatim shape did not recognise a verbatim adopt, so the two boot paths "
        "could not declare what they actually do"
    )


# ── The deferred layer decision, tracked in code rather than narrated ──────────

#: How many ``save_slot_off_loop(..., force=True)`` callers remain on the clobber
#: protocol. This count is AST-derived, not grepped: three of them span multiple
#: lines, so a single-line grep misses them, and both full-save spellings count --
#: see ``_count_force_saves``. The constant below is the ONE
#: spelling of this count; each remaining caller is annotated in place and
#: deliberately deferred behind ONE layer decision -- make the save merge-aware, so it
#: cannot clobber a field it did not author -- which retires the guard/adopt surface for
#: all of them at once instead of growing a bespoke closure per site. That decision is
#: TRACKED at kirodotdev/KiroCrew#8361, so this gate points at a destination rather than
#: only at a doc paragraph, and the issue records the two candidates already measured and
#: ruled out (persisting ``closed`` positively, and dropping it from the owned key set).
_FORCE_SAVE_CLOBBER_SITES = 10


_FORCE_SAVE_FULL_SAVE_CALLEES = frozenset({"save_slot_off_loop", "_save_slot_to_history"})


def _count_force_saves(source: str, path: str = "<source>") -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for each force=True full save.

    BOTH spellings count. ``save_slot_off_loop`` is the awaitable wrapper, but it
    delegates to ``_save_slot_to_history``, and a caller that reaches for the inner
    helper directly clobbers exactly the same keys. Counting only the wrapper left the
    census bypassable by dropping one level -- the shutdown save does precisely that --
    so the pin undercounted the real clobber surface.
    """
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _scope_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in _FORCE_SAVE_FULL_SAVE_CALLEES:
                continue
            for kw in node.keywords:
                if kw.arg == "force" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    out.append((path, node.lineno, scope.name))
    return out


def _find_sweep_persist_consumers(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for each sweep-persist caller."""
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _scope_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "persist_swept_slot_meta":
                out.append((path, node.lineno, scope.name))
    return out


_SWEEP_PERSIST_CONSUMERS = 2


def test_the_bridge_helper_has_not_grown_a_third_consumer() -> None:
    """The interim sweep-persist surface must not spread while its cause stays deferred.

    A TRACKED PRE-CONDITION rather than a sentence in a docstring, because the failure
    mode of any bridge is that it quietly becomes load-bearing for callers it was never
    sized for: each new consumer needs its own guard and adopt closure, earns an
    allowlist entry, and makes the protocol harder to retire than to keep.

    An UPPER BOUND, not an equality. Growth is the hazard and it fails here; SHRINKING is
    the outcome this scaffold exists to reach, so a change that removes a consumer must
    not red the build -- it lowers the constant instead. Non-vacuity is carried by the
    control below, so a drop to zero still surfaces rather than passing silently.
    """
    sites = collect_repo_violations(_find_sweep_persist_consumers)
    assert len(sites) <= _SWEEP_PERSIST_CONSUMERS, (
        f"{len(sites)} persist_swept_slot_meta callers now exist, above the bound of "
        f"{_SWEEP_PERSIST_CONSUMERS}. Do not add a third guard/adopt pair: take the "
        "merge-aware save decision the existing ones are waiting on, TRACKED at "
        "kirodotdev/KiroCrew#8361, which retires this "
        "helper for all of them at once instead of growing one closure per site.\n\n"
        + "\n".join(f"  {p}:{ln} in {fn}()" for p, ln, fn in sites)
    )


def _find_outcome_enum_references(
    source: str, path: str = "<source>"
) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for each ``SweepMergeOutcome`` use."""
    tree = ast.parse(source)
    seen: set[tuple[str, int, str]] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _scope_nodes(scope):
            named = ""
            if isinstance(node, ast.Attribute):
                named = getattr(node.value, "id", "")
            elif isinstance(node, ast.Name):
                named = node.id
            if named == "SweepMergeOutcome":
                # An attribute access matches twice, as the Attribute and its inner Name.
                seen.add((path, node.lineno, scope.name))
    return sorted(seen)


def test_the_interim_merge_protocol_stays_deletable_from_one_file() -> None:
    """Retirement must be a bounded delete, not a promise: keep the enum single-file.

    The bridging surface this change adds is interim by declaration, and a declaration is
    worth only what a reader can check. Its cross-module entry point is one function,
    pinned at two consumers by the census above. Its dispatch vocabulary --
    ``SweepMergeOutcome`` -- is consumed where it is produced, so retiring the protocol is
    a single-file delete plus two call-site conversions, and that is checkable here rather
    than taken on trust. A third module dispatching on the enum would spread the retirement
    across files and turn a mechanical delete into a survey.
    """
    strays = [
        (path, lineno, fn)
        for path, lineno, fn in collect_repo_violations(_find_outcome_enum_references)
        if pathlib.Path(path).name != "chat_persistence.py"
    ]
    assert not strays, (
        "SweepMergeOutcome is dispatched outside chat_persistence.py, so retiring the "
        "interim merge protocol (kirodotdev/KiroCrew#8361) is no longer a single-file "
        "delete. Consume the outcome where it is produced and hand the caller a plain "
        "result instead:\n\n" + "\n".join(f"  {p}:{ln} in {fn}()" for p, ln, fn in strays)
    )
    found = _find_outcome_enum_references(
        "def h():\n    return SweepMergeOutcome.COMMITTED\n", "<sample>"
    )
    assert len(found) == 1, f"the enum scan found nothing in a sample that dispatches: {found}"


def test_the_sweep_consumer_pin_is_not_vacuous() -> None:
    """Positive control: an upper bound passes at zero forever if the scan breaks.

    Also the retirement signal -- at zero this helper, its outcome enum, the census and
    the adopter allowlist are all removable and someone should say so deliberately.
    """
    live = collect_repo_violations(_find_sweep_persist_consumers)
    assert len(live) > 0, (
        "the scan found NO persist_swept_slot_meta callers, so the bound above passes "
        "vacuously. Either the detector stopped matching, or the last consumer is gone -- "
        "and if the latter, delete this helper, SweepMergeOutcome, the force-save census "
        "and the adopter allowlist rather than leaving them asserting nothing"
    )
    found = _find_sweep_persist_consumers(
        "async def h():\n    await persist_swept_slot_meta(s, sl, {})\n", "<sample>"
    )
    assert len(found) == 1, f"the consumer scan found nothing in a sample that calls it: {found}"


def test_the_deferred_force_save_layer_decision_has_not_grown() -> None:
    """The clobber deferral must not accrete new sites while the decision is open.

    WHY A GATE AND NOT A NOTE. ``force=True`` rebuilds every ``SLOT_OWNED_META_KEYS``
    entry from the in-memory slot, and in that set an ABSENT ``closed`` means "cleared"
    -- so a full save issued after a concurrent close committed erases that close and
    the dismissed tab returns on the next restart. This change closes that at the two
    delete sweeps by persisting metadata only. It does NOT close it at the other nine,
    because the honest fix is one layer decision (a merge-aware save) rather than ten
    bespoke guard/adopt closures. Persisting ``closed`` positively is NOT a second
    candidate -- see
    ``test_removing_closed_from_the_owned_set_requires_an_explicit_adopt_clear``.

    Deferring is a defensible call; letting the deferral GROW silently is not. So the count
    is an UPPER BOUND: adding a ``force=True`` save fails here and puts the
    layer decision in front of whoever is adding it, while REMOVING one does not red the
    build -- that is the change this scaffold exists to reach, and it lowers the constant
    instead. Non-vacuity is carried by the control below, so the bound cannot pass at zero
    while the scaffolding quietly stays.
    """
    sites = collect_repo_violations(_count_force_saves)
    assert len(sites) <= _FORCE_SAVE_CLOBBER_SITES, (
        f"{len(sites)} force=True full saves now exist (save_slot_off_loop or the "
        f"_save_slot_to_history helper it wraps) against the "
        f"pinned {_FORCE_SAVE_CLOBBER_SITES}. force=True rewrites every slot-owned "
        "metadata key from memory, so a save racing a concurrent close erases that "
        "close's `closed` flag and the dismissed tab comes back after a restart.\n\n"
        "If the count went UP: take the layer decision the existing sites are "
        "waiting on -- make the save merge-aware, TRACKED at kirodotdev/KiroCrew#8361. "
        "(Persisting `closed` positively does "
        "NOT work: a stale explicit `False` overwrites an on-disk `True` exactly as an "
        "absent key erases it.) That retires the per-site guard/adopt "
        "machinery instead of adding another copy of it.\n\n"
        "If the count went DOWN: good -- lower the constant, and check whether the "
        "remaining sites still justify `persist_swept_slot_meta`, its guard/adopt "
        "closures, this census and the adopter allowlist. At zero, delete all four.\n\n"
        + "\n".join(f"  {p}:{ln} in {fn}()" for p, ln, fn in sites)
    )


def test_the_force_save_census_is_not_vacuous() -> None:
    """Positive control: the AST census must actually find the annotated sites.

    A renamed helper or a keyword spelled differently would make the pin above pass
    while counting nothing, so assert it still sees the known population and that the
    detector can distinguish force=True from an ordinary save.
    """
    sites = collect_repo_violations(_count_force_saves)
    assert len(sites) > 0, (
        "the census found NO force=True sites, so the upper-bound gate above passes "
        "vacuously and would not notice another clobber site being added. Either the "
        "detector stopped matching, or every site is genuinely gone -- and if the latter, "
        "the layer decision landed and this gate plus the guard/adopt surface should be "
        "deleted outright rather than left asserting nothing"
    )
    # DELIBERATELY NOT an equality against the pin. An equality reddens the one PR the
    # ratchet most wants to encourage -- the one that DELETES a clobber site -- while
    # removing no hazard, since fewer sites is strictly safer. The upper bound above
    # carries the hazard; non-vacuity is carried here and by the two fixtures below,
    # which prove the detector discriminates without depending on the repo's count.
    assert _count_force_saves("await save_slot_off_loop(state, slot)\n", "m.py") == [], (
        "the detector counted a save with no force keyword; it must pin only the "
        "clobber-protocol callers"
    )
    flagged = _count_force_saves(
        "async def f():\n    await save_slot_off_loop(state, slot, force=True)\n", "m.py"
    )
    assert [f[2] for f in flagged] == ["f"], f"detector missed a force=True call: {flagged}"
    # The bypass fixture: the wrapper delegates to this helper, so counting only the
    # wrapper let a caller drop one level and clobber the same keys uncounted.
    inner = _count_force_saves(
        "def g():\n    _save_slot_to_history(state, slot, force=True)\n", "m.py"
    )
    assert [f[2] for f in inner] == ["g"], (
        "the detector missed a force=True call on the inner helper, so the census is "
        f"bypassable by calling it instead of the wrapper: {inner}"
    )


@pytest.mark.asyncio
async def test_publication_lands_before_the_cancellation_the_caller_can_observe():
    """A cancellation racing the write's completion must not leave publication queued.

    The delete handlers decide whether the sweep is owed by reading the committed set,
    and that decision runs in the same step the cancellation unwinds through them. A
    done-callback is scheduled with ``call_soon``, so it can still be pending at that
    point: the handler then sees the OLD committed set, concludes the removal never
    landed, and skips the sweep -- leaving durable metadata naming a folder or tag that
    is gone from disk. Publication therefore has to be a statement on the cancelled
    path, not a callback.

    The ordering here is the reachable one, not a contrivance: a client disconnect
    cancels the handler while the worker thread is finishing, so the cancellation is
    queued before the write's own callbacks.
    """
    from kiro_crew.dashboard.snapshot_commit import commit_snapshot_while_holding_the_lock

    loop = asyncio.get_running_loop()
    write: asyncio.Future[None] = loop.create_future()
    published: list[str] = []
    seen_by_the_caller: list[bool] = []

    async def caller() -> None:
        try:
            await commit_snapshot_while_holding_the_lock(
                write, lambda: published.append("committed")
            )
        except asyncio.CancelledError:
            # Exactly what a delete handler does next: read the committed set to
            # decide whether the sweep is owed.
            seen_by_the_caller.append(bool(published))
            raise

    task = asyncio.ensure_future(caller())
    await asyncio.sleep(0)
    task.cancel()
    write.set_result(None)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert seen_by_the_caller == [True], (
        "the caller unwound the cancellation before publication landed, so a delete "
        "handler would read the pre-removal committed set and skip the required sweep"
    )
    assert published == ["committed"], "the snapshot was published more than once or not at all"


@pytest.mark.asyncio
async def test_a_cancelled_folder_delete_that_committed_still_writes_its_audit_record(
    tmp_path, monkeypatch
) -> None:
    """A committed delete must be auditable even when the handler is cancelled.

    ``log_api_access`` is the ONLY SEL emission for ``chat.folder_delete``, so re-raising
    the captured cancellation before it leaves the mutation done and unrecorded. Nothing
    downstream backfills the entry, so the audit trail simply has a hole in it.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": "f1"})

    real_mutate = state.mutate_folders
    committed: list[bool] = []

    async def _commit_then_cancel(mutate):
        await real_mutate(mutate)
        committed.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(state, "mutate_folders", _commit_then_cancel)

    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/folders/f1")

    assert committed == [
        True
    ], "fixture: the commit never landed, so the cancellation window was never opened"
    assert not any(
        f["id"] == "f1" for f in state._folders
    ), "fixture: the folder removal must have committed"
    assert any(e.get("operation") == "chat.folder_delete" for e in audit), (
        "the folder removal committed but no SEL record was written, because the captured "
        "cancellation was re-raised before log_api_access. Emit the success audit first."
    )


@pytest.mark.asyncio
async def test_a_cancelled_tag_delete_that_committed_still_strips_folders_and_boards(
    tmp_path, monkeypatch
) -> None:
    """The tag delete's durable cleanup must survive the captured cancellation too.

    A folder can carry tags and a sidebar column holds tag ids, so re-raising before those
    strips run leaves the deleted id referenced on disk in two more places. Moving the
    re-raise alone does not help: the task is still cancelled, so the cleanup's own awaits
    raise again -- and its ``except Exception`` cannot catch that. The cleanup has to run
    through the shielded helper, as the slot strip already does.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "urgent", "color": "#ff0000"}]
    state.publish_committed_tag_ids(state._tags)
    state._committed_tag_ids = frozenset()
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": "", "tags": ["t1"]}
    ]
    state._tag_boards = [{"id": "c1", "name": "Board", "tag_ids": ["t1"]}]

    real_commit = mod._commit_tags_snapshot
    committed: list[bool] = []

    async def _commit_then_cancel(st, snapshot):
        await real_commit(st, snapshot)
        committed.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "_commit_tags_snapshot", _commit_then_cancel)
    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/tags/t1")

    assert committed == [
        True
    ], "fixture: the tag snapshot never committed, so the window was never opened"
    assert not any(
        t.get("id") == "t1" for t in state._tags
    ), "fixture: the tag removal must have committed"
    assert state._folders[0].get("tags") in (None, []), (
        f"the folder still carries the deleted tag id "
        f"(tags={state._folders[0].get('tags')!r}); the captured cancellation was "
        "re-raised before the folder strip, so the reference stays on disk"
    )
    assert state._tag_boards[0]["tag_ids"] == [], (
        f"the sidebar column still carries the deleted tag id "
        f"(tag_ids={state._tag_boards[0]['tag_ids']!r}); the board strip was skipped"
    )
    assert any(
        e.get("operation") == "chat.tag_delete" for e in audit
    ), "the tag removal committed but no SEL record was written"


@pytest.mark.asyncio
async def test_cancellation_during_the_tag_slot_sweep_still_dereferences_folders_and_boards(
    tmp_path, monkeypatch
) -> None:
    """The FIRST shielded call re-raises, so everything after it needs its own capture.

    ``sweep_to_completion_despite_cancellation`` drains the sweep and then re-raises by
    contract. A cancellation arriving while slot persistence runs therefore unwinds out of
    that call, and the folder/board dereference below it plus the audit never execute --
    leaving the deleted tag id durably referenced in two stores. Shielding the cleanup is
    not enough on its own: control has to reach it.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "urgent", "color": "#ff0000"}]
    state.publish_committed_tag_ids(state._tags)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": "", "tags": ["t1"]}
    ]
    state._tag_boards = [{"id": "c1", "name": "Board", "tag_ids": ["t1"]}]
    slot = _slot("a")
    slot.tags = ["t1"]
    state._slots["a"] = slot

    reached: list[str] = []

    async def _cancel_during_slot_persistence(*_a, **_kw):
        # Exactly a client disconnect landing while the slot strip is persisting.
        reached.append("persist")
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "persist_swept_slot_meta", _cancel_during_slot_persistence)
    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )

    statuses: list[int] = []
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        with contextlib.suppress(Exception):
            resp = await client.delete("/api/chat/tags/t1")
            statuses.append(resp.status)

    assert reached == ["persist"], (
        "fixture: the slot persistence was never reached, so the cancellation window "
        f"was never opened (reached={reached})"
    )
    assert state._folders[0].get("tags") in (None, []), (
        f"the folder still references the deleted tag id "
        f"(tags={state._folders[0].get('tags')!r}); the sweep helper re-raised and the "
        "folder dereference never ran"
    )
    assert state._tag_boards[0]["tag_ids"] == [], (
        f"the sidebar column still references the deleted tag id "
        f"(tag_ids={state._tag_boards[0]['tag_ids']!r})"
    )
    assert any(
        e.get("operation") == "chat.tag_delete" for e in audit
    ), "the tag removal committed but no SEL record was written"
    assert statuses == [], (
        f"the cancellation was swallowed -- the handler returned {statuses} instead of "
        "propagating CancelledError after finishing its durable work"
    )


@pytest.mark.asyncio
async def test_cancellation_during_the_folder_slot_sweep_still_writes_the_audit(
    tmp_path, monkeypatch
) -> None:
    """The folder handler carries the identical re-raise-before-audit shape.

    Fixed alongside the tag side deliberately: both route their slot sweep through the same
    helper, so a capture added to one leaves the other silently unaudited on the same race.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": "f1"})

    reached: list[str] = []

    async def _cancel_during_slot_persistence(*_a, **_kw):
        reached.append("persist")
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "persist_swept_slot_meta", _cancel_during_slot_persistence)
    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )

    statuses: list[int] = []
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        with contextlib.suppress(Exception):
            resp = await client.delete("/api/chat/folders/f1")
            statuses.append(resp.status)

    assert reached == ["persist"], f"fixture: slot persistence never ran (reached={reached})"
    assert any(e.get("operation") == "chat.folder_delete" for e in audit), (
        "the folder removal committed but no SEL record was written; the sweep helper "
        "re-raised before the audit"
    )
    assert statuses == [], f"the cancellation was swallowed -- the handler returned {statuses}"


@pytest.mark.asyncio
async def test_cancellation_in_the_tag_folder_board_cleanup_still_writes_the_audit(
    tmp_path, monkeypatch
) -> None:
    """The SECOND shielded sweep needs its own capture, exactly like the first.

    ``sweep_to_completion_despite_cancellation`` re-raises once drained, so awaiting it
    bare lets a cancellation escape before ``log_api_access`` -- the committed vocabulary
    deletion and both strips have landed and no SEL record exists for them. Nothing later
    re-emits the entry.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t1", "name": "urgent", "color": "#ff0000"}]
    state.publish_committed_tag_ids(state._tags)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": "", "tags": ["t1"]}
    ]
    state._tag_boards = [{"id": "c1", "name": "Board", "tag_ids": ["t1"]}]

    reached: list[str] = []

    async def _cancel_inside_the_cleanup(_cb):
        # A client disconnect landing while the folder strip persists. CancelledError is
        # not an Exception, so the cleanup's own ``except Exception`` cannot catch it.
        reached.append("cleanup")
        raise asyncio.CancelledError()

    monkeypatch.setattr(state, "mutate_folders", _cancel_inside_the_cleanup)
    audit: list[dict] = []
    monkeypatch.setattr(
        mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audit.append(kw))
    )

    statuses: list[int] = []
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        with contextlib.suppress(Exception):
            resp = await client.delete("/api/chat/tags/t1")
            statuses.append(resp.status)

    assert reached == ["cleanup"], (
        f"fixture: the folder/board cleanup never ran (reached={reached}), so the second "
        "sweep's cancellation window was never opened"
    )
    assert not any(
        t.get("id") == "t1" for t in state._tags
    ), "fixture: the tag removal must have committed before the cleanup was reached"
    assert any(e.get("operation") == "chat.tag_delete" for e in audit), (
        "the tag deletion committed but no SEL record was written: the second shielded "
        "sweep was awaited bare, so its re-raise escaped before log_api_access"
    )
    assert statuses == [], (
        f"the cancellation was swallowed -- the handler returned {statuses} instead of "
        "propagating it after the audit"
    )


@pytest.mark.asyncio
async def test_a_failed_vocabulary_write_does_not_unfile_the_folders_conversations(
    tmp_path, monkeypatch
) -> None:
    """A folder write that FAILS must leave every filing intact.

    The removal commits BEFORE the unfile sweep runs, so a failed write must mutate
    nothing: the folder is still in ``folders.json``, still committed, and every
    conversation keeps its placement in memory and on disk.
    """
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    state.publish_committed_folder_ids(state._folders)
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot
    _log_session(state, slot_history_key(slot), {"folder_id": "f1"})

    async def _write_fails(_mutate):
        raise OSError("no space left on device")

    monkeypatch.setattr(state, "mutate_folders", _write_fails)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        with contextlib.suppress(Exception):
            await client.delete("/api/chat/folders/f1")

    assert any(
        f["id"] == "f1" for f in state._folders
    ), "fixture: the folder must still exist, or the write did not fail"
    assert "f1" in (
        state._committed_folder_ids or frozenset()
    ), "the failed write left the folder out of the COMMITTED vocabulary"

    assert slot.folder_id == "f1", (
        "a failed folder write unfiled a live conversation in memory; the removal commits "
        "before the sweep precisely so a failed write mutates nothing"
    )
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("folder_id") == "f1", (
        f"a failed folder write unfiled the conversation DURABLY (metadata folder_id="
        f"{meta.get('folder_id')!r}), which the next save would make permanent"
    )
    assert (
        state.folder_id_for_restore("f1") == "f1"
    ), "the still-committed folder id did not survive validation after a failed write"


@pytest.mark.asyncio
async def test_a_commit_window_rebind_still_scrubs_the_original_transcript(
    tmp_path, monkeypatch
) -> None:
    """The pin must name the transcript the deleted id is ON, not the one routing moved to.

    ``closing`` is captured before the vocabulary write; pass one runs after it. A rebind
    landing inside the commit await changes the slot's routing, so a key read in pass one
    names the NEW transcript. Pinning that writes the blank to a record that never carried
    the id and leaves the original dangling -- durable, because the sweep never revisits.

    Fails on the pre-fix shape for the right reason: with the object-only capture the
    persisted metadata for the ORIGINAL key still carries the folder id.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f-doomed", "name": "Doomed", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)
    slot = _slot("a", folder_id="f-doomed")
    state._slots["a"] = slot

    original_key = slot_history_key(slot)
    _log_session(state, original_key, {"folder_id": "f-doomed"})

    pinned: list[str] = []

    async def _record_pin(_state, _slot, _fields, **kw):
        pinned.append(kw.get("expected_history_key"))
        return True

    monkeypatch.setattr(mod, "persist_swept_slot_meta", _record_pin)

    real_mutate = state.mutate_folders

    async def _rebind_inside_the_commit(fn):
        result = await real_mutate(fn)
        # The rebind: routing moves while the vocabulary write is in flight.
        slot.key = "a-rebound"
        return result

    monkeypatch.setattr(state, "mutate_folders", _rebind_inside_the_commit)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f-doomed")
    assert resp.status == 200, f"fixture: the delete failed ({resp.status})"

    assert (
        slot_history_key(slot) != original_key
    ), "fixture: the rebind never took, so the window under test does not exist"
    assert pinned, "fixture: the sweep never reached its persist, so no pin was recorded"
    assert original_key in pinned, (
        f"the sweep pinned {pinned!r}, none of which is the pre-commit transcript "
        f"{original_key!r}. A rebind inside the commit await moved the pin onto the new "
        "record, leaving the deleted folder id durable on the original"
    )


@pytest.mark.asyncio
async def test_a_commit_window_rebind_still_strips_the_original_transcript(
    tmp_path, monkeypatch
) -> None:
    """The tag side's pin, measured unpinned before this test existed.

    Reverting the tag capture to the object-only shape left the whole suite green, because the
    folder test cannot reach this handler. Two sweeps take the same capture and both must name
    the transcript the deleted id is on.
    """
    from kiro_crew.dashboard import chat_tags as mod

    state = _make_state(tmp_path)
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#ff0000", "order": 0}]
    state._committed_tag_ids = frozenset({"t-doomed"})
    slot = _slot("a", tags=["t-doomed"])
    state._slots["a"] = slot

    original_key = slot_history_key(slot)
    _log_session(state, original_key, {"tags": ["t-doomed"]})

    pinned: list[str] = []

    async def _record_pin(_state, _slot, _fields, **kw):
        pinned.append(kw.get("expected_history_key"))
        return True

    monkeypatch.setattr(mod, "persist_swept_slot_meta", _record_pin)

    real_commit = mod._commit_tags_snapshot

    async def _rebind_inside_the_commit(st, snapshot):
        result = await real_commit(st, snapshot)
        slot.key = "a-rebound"
        return result

    monkeypatch.setattr(mod, "_commit_tags_snapshot", _rebind_inside_the_commit)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t-doomed")
    assert resp.status == 200, f"fixture: the delete failed ({resp.status})"

    assert (
        slot_history_key(slot) != original_key
    ), "fixture: the rebind never took, so the window under test does not exist"
    assert pinned, "fixture: the sweep never reached its persist, so no pin was recorded"
    assert original_key in pinned, (
        f"the strip pinned {pinned!r}, none of which is the pre-commit transcript "
        f"{original_key!r}; the deleted tag id is left durable on the original record"
    )


def test_an_empty_id_never_enters_either_committed_vocabulary(tmp_path) -> None:
    """`""` is what UNFILED / untagged MEANS, so it must never validate as a filing.

    Both publishers state this rule and neither had a fixture for it: the suite pins the
    malformed-id drop but never published a row whose ``id`` is the empty string. Since
    restore-time pruning now validates against these sets on every boot, resume, fork and
    channel arrival, a derivation that admitted `""` would make "unfiled" a member of the
    vocabulary that unfiled slots are checked against -- the one value that must stay outside
    it.

    Asserted on the SETS rather than through a restore path, because the rule belongs to the
    derivation: this is the single spelling both call sites share.
    """
    state = _make_state(tmp_path)

    state.publish_committed_folder_ids(
        [
            {"id": "f-real", "name": "Real", "parent_id": "", "owner_app": ""},
            {"id": "", "name": "Empty", "parent_id": "", "owner_app": ""},
        ]
    )
    assert state._committed_folder_ids == frozenset({"f-real"}), (
        f"the folder vocabulary is {sorted(state._committed_folder_ids)!r}; an empty-string id "
        "was admitted, so an unfiled slot's folder_id would validate as a real filing"
    )

    state.publish_committed_tag_ids(
        [
            {"id": "t-real", "name": "Real", "color": "#111111", "order": 0},
            {"id": "", "name": "Empty", "color": "#222222", "order": 1},
        ]
    )
    assert state._committed_tag_ids == frozenset({"t-real"}), (
        f"the tag vocabulary is {sorted(state._committed_tag_ids)!r}; an empty-string id was "
        "admitted, so a blank tag id would survive every restore prune"
    )


#: The three popped/parked restores whose comments claim tags-only revalidation.
_TAG_ONLY_RESTORE_SITES = ("close_slot", "api_chat_slots_cleanup", "_revalidate_parked_vocabulary")


def test_the_popped_and_parked_restores_revalidate_tags_only() -> None:
    """The three restores must call the TAG validator and NOT the folder one.

    A folder id the sidebar cannot resolve renders as Unfiled, so that residue does not buy
    the surface; a stripped tag is user data. Asserted in both directions on purpose.
    Requiring the tag call keeps this from passing
    vacuously if a refactor drops revalidation altogether, and forbidding the folder call
    is what stops the asymmetry drifting back by symmetry-with-the-tag-arm reasoning -- the
    justification the review withdrew. A reader meets the claim in the comment at each
    site; this is what makes the claim checkable.
    """
    index = _src_function_index()
    problems: list[str] = []
    for name in _TAG_ONLY_RESTORE_SITES:
        definitions = index.get(name, [])
        assert definitions, f"{name} defines no function under src/; the gate cannot see it"
        for path, node in definitions:
            # Both spellings: a direct ``state.x(...)`` call and the dynamic
            # ``getattr(state, "x", None)`` the spec-builder runtime uses.
            called = {
                (
                    sub.func.attr
                    if isinstance(sub.func, ast.Attribute)
                    else getattr(sub.func, "id", "")
                )
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
            } | {
                sub.value
                for sub in ast.walk(node)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            }
            if "folder_id_for_restore" in called:
                problems.append(f"{path}::{name} revalidates folder_id")
            if "tag_ids_for_restore" not in called:
                problems.append(f"{path}::{name} no longer revalidates tags at all")
    assert not problems, (
        "the popped/parked restores no longer match the tag-only shape their comments "
        f"describe: {problems}"
    )


#: The fork inherits a folder id and must NOT revalidate it. The REVERTS are not in this
#: list -- see the docstring for why they differ.
_NO_FOLDER_ARM_SITES = ("api_chat_slot_fork",)


def test_the_fork_does_not_revalidate_the_folder_id_it_inherits() -> None:
    """A forked child keeps the parent's folder id; only the tag arm prunes.

    The REVERT paths do validate, and the difference is not inconsistency. A revert
    reattaches a placement the operation moved AWAY from, so if that folder was deleted
    inside the window the revert would write an id the user never chose -- pinned by
    ``test_refused_refile_does_not_restore_a_folder_deleted_in_the_window``. The fork copies
    what the parent legitimately holds, and a child rendering as Unfiled is the residue the
    spec rules benign.
    """
    index = _src_function_index()
    problems: list[str] = []
    for name in _NO_FOLDER_ARM_SITES:
        definitions = index.get(name, [])
        assert definitions, f"{name} defines no function under src/; the gate cannot see it"
        for path, node in definitions:
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                called = (
                    sub.func.attr
                    if isinstance(sub.func, ast.Attribute)
                    else getattr(sub.func, "id", "")
                )
                if called == "folder_id_for_restore":
                    problems.append(f"{path}::{name}")
    assert not problems, (
        "these sites revalidate a folder id whose only residue the spec rules benign: "
        f"{sorted(set(problems))}"
    )


@pytest.mark.asyncio
async def test_identity_loss_still_persists_a_message_less_correction(tmp_path) -> None:
    """A slot popped from the registry must still get its message-less correction to disk.

    THE HARM CHAIN. The sweep's persist begins by checking that ``state._slots`` still holds
    THIS object; a concurrent failed close pops and re-inserts, so the check can fail. That
    arm arms ``_dirty`` and returns, on the reasoning that the periodic flush will retry --
    but ``flush_slot_now`` returns before saving when the slot has no messages, so for a
    message-less slot the retry never happens and the swept field never reaches disk.

    Nothing recovers it afterwards. The two bulk restores assign ``meta["folder_id"]``
    verbatim, with no folder counterpart to ``tag_ids_for_restore``, so the deleted folder id
    survives every restart. The two sibling arms of this same function -- UNCONFIRMED and the
    exception handler -- already call ``persist_meta_correction_without_messages`` for exactly
    this reason; this arm is the one that did not.
    """
    from kiro_crew.dashboard.chat_persistence import persist_swept_slot_meta

    state = _make_state(tmp_path)
    state._folders = [{"id": "f-doomed", "name": "Doomed", "parent_id": "", "owner_app": ""}]
    _commit_vocabulary(state)

    slot = _slot("a", folder_id="f-doomed")
    slot.messages = []
    key = slot_history_key(slot)
    _log_session(state, key, {"folder_id": "f-doomed"})
    assert (
        state.conversation_log.get_metadata(key).get("folder_id") == "f-doomed"
    ), "fixture: the deleted folder id must be on disk before the sweep runs"

    # The delete commits, the sweep clears in memory, and a concurrent failed close has
    # left the registry holding a DIFFERENT object under this key.
    state._folders = []
    state.publish_committed_folder_ids(state._folders)
    slot.folder_id = ""
    state._slots["a"] = _slot("a", folder_id="")
    assert (
        state._slots.get(slot.key) is not slot
    ), "fixture: the identity check must fail, or this test exercises a different arm"

    await persist_swept_slot_meta(
        state,
        slot,
        {"folder_id": ""},
        guard=lambda meta: meta.get("folder_id") == "f-doomed",
        adopt=lambda _s, _o, _f: None,
        label="folder delete",
        expected_history_key=key,
    )

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("folder_id", "") == "", (
        f"the swept slot lost identity and its correction never reached disk (folder_id="
        f"{meta.get('folder_id')!r}). The slot has no messages, so the periodic flush skips "
        "it, and the bulk restores adopt folder_id verbatim -- nothing else will ever remove "
        "the deleted id"
    )


#: Words a spec sentence uses to restate the SIZE of a maintained list. Digits and the
#: spellings that have actually drifted here; not an exhaustive number vocabulary.
_SIZE_WORDS = r"(\d+|nine|ten|eleven|twelve|thirteen|fourteen|fifteen)"


def test_the_spec_does_not_restate_a_maintained_list_size() -> None:
    """`history.md` must not carry its own copy of either list's size.

    Both maintained lists are AST-derived and change when a caller lands or leaves, so a
    number written into prose is stale from the next such change -- which happened four
    times on this branch before this gate existed, once per review cycle. The constant and
    the dict are the only spellings; the spec describes what they mean, never how many.
    """
    import re

    spec = (
        pathlib.Path(__file__).resolve().parent.parent
        / "docs"
        / "system-specs"
        / "modules"
        / "history.md"
    )
    assert spec.exists(), f"the spec this gate reads is missing: {spec}"
    text = spec.read_text(encoding="utf-8")

    offenders: list[str] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        # A size claim about the allowlist ("13 entries", "**13** entries", "13-entry") ...
        if re.search(rf"{_SIZE_WORDS}[\s\-*_`]*entr(y|ies)", line, re.IGNORECASE):
            offenders.append(f"history.md:{lineno}: {line.strip()[:90]}")
        # ... or about the force-save census ("nine callers", "10 sites", "= 9").
        if re.search(rf"{_SIZE_WORDS}[\s*_`]+(callers?|sites?)\b", line, re.IGNORECASE):
            offenders.append(f"history.md:{lineno}: {line.strip()[:90]}")
        if re.search(r"_FORCE_SAVE_CLOBBER_SITES\s*=\s*\d+", line):
            offenders.append(f"history.md:{lineno}: {line.strip()[:90]}")

    assert not offenders, (
        "the spec restates a maintained list's size; delete the number and let the "
        "constant or the dict be the single spelling:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_a_malformed_board_column_does_not_crash_the_tag_delete(
    tmp_path, monkeypatch
) -> None:
    """A non-list ``tag_ids`` on a sidebar column must be skipped, not iterated.

    The column strip does ``col.get("tag_ids") or []``, which substitutes for None and for
    an empty list but hands a TRUTHY non-iterable straight to the comprehension -- so an
    ``int`` or ``bool`` raises ``TypeError`` and the whole delete 500s AFTER the vocabulary
    commit has already landed. That leaves disk self-inconsistent in exactly the window
    this change exists to close.

    The loader already has the right guard for the same field
    (``isinstance(tag_ids, list)`` in ``load_sidebar_columns``) and skips a malformed
    column rather than coercing it, but it does not REWRITE the bad value, so the shape
    survives into ``state._tag_boards`` and reaches this loop. Matching the loader's guard
    here is the fix; the malformed column is left exactly as found for a human to correct.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [{"id": "t-doomed", "name": "Doomed", "color": "#222222", "order": 0}]
    state.publish_committed_tag_ids(state._tags)

    # Hand-edited tag_boards.json: a valid column id with a truthy NON-iterable tag_ids.
    state._tag_boards = [
        {"id": "c-ok", "tag_ids": ["t-doomed", "t-keep"]},
        {"id": "c-broken", "tag_ids": 5},
    ]
    monkeypatch.setattr(state, "save_tag_boards_snapshot", lambda snap: None)

    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.delete("/api/chat/tags/t-doomed")

    assert resp.status == 200, (
        f"the delete returned {resp.status}: a malformed sidebar column crashed the "
        "handler after the vocabulary commit, which is the self-inconsistent window this "
        "change closes"
    )
    assert not any(
        t.get("id") == "t-doomed" for t in state._tags
    ), "fixture: the tag must actually have been deleted, or this test proves nothing"
    assert state._tag_boards[0]["tag_ids"] == [
        "t-keep"
    ], "the well-formed column was not stripped; the guard must skip only the bad one"
    assert (
        state._tag_boards[1]["tag_ids"] == 5
    ), "the malformed column was rewritten; it is left as found so a human can see it"


@pytest.mark.asyncio
async def test_run_config_write_also_surfaces_the_worker_failure() -> None:
    """The sibling helper must order the two exceptions the same way as its twin.

    Review asked whether every consumer of BOTH shared drain helpers tolerates receiving
    the worker's failure rather than the absorbed cancellation. They do, because that is
    the ordering the hand-rolled loop this replaced already had: it awaited
    ``asyncio.shield(task)`` inside ``except asyncio.CancelledError``, which never catches
    a worker exception. Its twin is pinned by
    ``test_a_worker_failure_outranks_a_cancellation_that_raced_it``; this pins the other
    one, so neither can drift to masking a failure without a red build.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import run_config_write

    class _WriteFailed(RuntimeError):
        pass

    started = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        started.set()
        release.wait(5)
        raise _WriteFailed("config write could not land")

    task = asyncio.ensure_future(run_config_write(_worker))
    assert await asyncio.to_thread(started.wait, 5), "fixture: the worker never started"
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(_WriteFailed):
        await task
