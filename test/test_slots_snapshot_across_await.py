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

    Pinned because the gate previously matched ``values()`` only, which made it a
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
    # the restored slot still pointed at a folder that no longer exists.
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

    Scoped deliberately to the ``meta["folder_id"]`` arrival path. That path used to
    be a reason the sweep repeated: ``reconcile_channel_slots`` reads session
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

    That window no longer exists. The validator reads ``_committed_folder_ids``,
    which the delete's own ``mutate_folders`` has already updated by the time any
    arrival lands, so the deleted id is pruned AT ASSIGNMENT and the lock is
    irrelevant. The premise control below therefore asserts the OPPOSITE of what it
    used to, and that inversion is the point: it is the observable consequence of
    retiring the probe.

    SO THIS TEST NO LONGER EXERCISES A REPEAT, and nothing here should be read as
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

    Pass one records the slot; pass two awaits the merge, which used to resolve
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
async def test_a_close_failure_restore_revalidates_a_slot_the_sweep_could_not_see(
    tmp_path, monkeypatch
) -> None:
    """A slot POPPED during a vocabulary delete is invisible to both sweep passes.

    The pre-commit capture snapshots ``_slots``, and pass one re-reads the live view. A
    close already in flight has popped its slot from both, so a folder delete committing
    inside that window reaches it through neither. When the close's save then FAILS,
    ``close_slot`` puts the object back -- still naming the deleted folder, and the periodic
    flush makes that durable.

    Drives the real ``close_slot`` with a failing save rather than calling the validators
    directly: the defect is in the RESTORE arm, so a test that revalidated by hand would
    pass with that arm unchanged.

    The ids were held across this handler's OWN await, so absence from the committed
    vocabulary means deleted-in-window rather than an undatable snapshot -- which is why
    this restore prunes, unlike the four cold-start paths.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f-live", "name": "Live", "parent_id": "", "owner_app": ""}]
    state._tags = [{"id": "t-live", "name": "Live", "color": "#111111", "order": 0}]
    _commit_vocabulary(state)
    # ``_commit_vocabulary`` publishes FOLDERS only, and an UNKNOWN tag vocabulary fails
    # open by design, so the tag half needs its own publication to be under test at all.
    state._committed_tag_ids = frozenset({"t-live"})

    slot = _slot("a", folder_id="f-deleted", tags=["t-deleted", "t-live"])
    state._slots["a"] = slot

    async def _failing_save(*_a, **_kw):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(mod, "save_slot_off_loop", _failing_save)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    restored = state._slots.get("a")
    assert restored is slot, (
        "fixture: the failed save must have restored the popped slot, or the restore arm "
        "under test never ran"
    )
    assert restored.folder_id == "f-deleted", (
        f"the restore UNFILED the slot (folder_id={restored.folder_id!r}). REVERSED "
        "deliberately: this fixture's folder is absent from the committed vocabulary "
        "BEFORE the close begins, which is indistinguishable from a readable-but-stale "
        "folders.json -- and this arm puts the slot back into ``_slots`` where the periodic "
        "flush persists it, so pruning here is durable loss. The restore now prunes only on "
        "an observed committed-present -> committed-absent transition. The tag half below "
        "still prunes, because the tag validator takes no pre-operation observation."
    )
    assert restored.tags == ["t-live"], (
        f"the deleted tag survived the restore (tags={restored.tags!r}); the live tag must "
        "be kept and only the deleted one dropped"
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

    Restore used to paper over it. It no longer does -- cold start keeps an id absent from
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
    makes the unfiling permanent. That is the durable-unfiling class this PR
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
async def test_folder_adoption_refuses_after_a_move_away_and_back(tmp_path, monkeypatch) -> None:
    """The ABA case: value equality says "untouched" when two moves happened.

    The compare-and-swap in the sibling test above compares the slot's CURRENT
    ``folder_id`` against what was submitted to the merge -- the blank pass one wrote. If
    the conversation is moved into a live folder and then unfiled again while the merge
    awaits, the slot is blank on both sides and that comparison reads as "nobody touched
    it", so the sweep adopts ``observed`` and restores a placement the user had just left.
    A later save makes it durable.

    So the test is a per-slot mutation GENERATION, not the value. Both arms run in one
    handler pass, and the control is the untouched slot: a fix that simply stopped adopting
    would pass the subject arm and fail the control.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": "f_detour", "name": "Detour", "parent_id": "", "owner_app": ""},
        {"id": "f_ondisk", "name": "OnDisk", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)
    state._slots["aba"] = _slot("aba", folder_id="f1")
    state._slots["quiet"] = _slot("quiet", folder_id="f1")

    async def _merge_shim(_st, sl, _fields, **_kw):
        await asyncio.sleep(0)
        if sl.key == "aba":
            # Moved into a live folder, then unfiled again -- both inside the window.
            # The slot ends blank, exactly as pass one left it.
            sl.folder_id = "f_detour"
            sl.folder_id = ""
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_ondisk"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- adoption still happens for a slot nothing touched.
    assert state._slots["quiet"].folder_id == "f_ondisk", (
        "adoption must still occur for an untouched slot; otherwise the periodic flush "
        "writes our blank over the placement the guard refused to clobber"
    )
    # THE SUBJECT -- the user's unfile must survive.
    assert state._slots["aba"].folder_id == "", (
        f"a stale placement was restored over the user's unfile "
        f"(folder_id={state._slots['aba'].folder_id!r}). The slot was moved and unfiled "
        "inside the merge window, so it is blank on both sides and a value comparison "
        "cannot see the two writes -- the adoption must be gated on an unchanged "
        "per-slot mutation generation"
    )


@pytest.mark.asyncio
async def test_folder_adoption_refuses_a_move_made_during_an_earlier_slots_await(
    tmp_path, monkeypatch
) -> None:
    """The multi-slot ABA case: the yield point is the PREVIOUS slot's persist.

    The sibling test above moves and unfiles the slot whose own merge is awaiting, which a
    baseline taken just before that merge still catches. This one is the residual that
    baseline leaves open: with several slots to persist, the first slot's await is already a
    yield point, so a user move-then-unfile of a LATER slot lands before that later slot's
    baseline would be read. A per-slot baseline then records the already-moved counter,
    matches, and adopts the stale on-disk placement -- for every slot except the first.

    Hence the baseline belongs to pass one, which has no await in it. Three slots, so the
    subject is neither first nor last and the untouched control is still exercised.
    """

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": "f_detour", "name": "Detour", "parent_id": "", "owner_app": ""},
        {"id": "f_ondisk", "name": "OnDisk", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)
    state._slots["early"] = _slot("early", folder_id="f1")
    state._slots["late"] = _slot("late", folder_id="f1")
    state._slots["quiet"] = _slot("quiet", folder_id="f1")

    async def _merge_shim(_st, sl, _fields, **_kw):
        await asyncio.sleep(0)
        if sl.key == "early":
            # The user moves and unfiles a DIFFERENT, not-yet-persisted slot while this
            # one awaits. ``late`` ends blank, exactly as pass one left it.
            state._slots["late"].folder_id = "f_detour"
            state._slots["late"].folder_id = ""
        return (SweepMergeOutcome.SUPERSEDED, {"folder_id": "f_ondisk"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete("/api/chat/folders/f1")

    assert resp.status == 200
    # NEGATIVE CONTROL -- a slot nobody touched must still adopt, so a fix that merely
    # stopped adopting cannot pass this test.
    assert state._slots["quiet"].folder_id == "f_ondisk", (
        "adoption must still occur for an untouched slot; otherwise the periodic flush "
        "writes our blank over the placement the guard refused to clobber"
    )
    assert state._slots["late"].folder_id == "", (
        f"a stale placement was restored over a move made during an EARLIER slot's await "
        f"(folder_id={state._slots['late'].folder_id!r}). The baseline for this slot was "
        "read after that await, so it recorded the user's move as the starting point and "
        "the guard could not see it. Capture the counter in pass one, which has no await"
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
async def test_superseded_folder_adoption_validates_the_observed_value(
    tmp_path, monkeypatch
) -> None:
    """The superseded adoption must not import an invalid folder id into live state.

    ``observed`` is PERSISTED metadata, so it carries the same hazards every other
    read of ``meta["folder_id"]`` does -- and this site adopted it verbatim through
    ``str(...)``, which is worse than no check at all: a malformed value (a JSON array
    or object) is TRUTHY, so ``or ""`` does not drop it, and ``str()`` renders it into
    a plausible-looking id that then passes every later ``isinstance`` guard.

    Three arms, so a fix that simply stops adopting cannot pass:

    * malformed (a list)      -> dropped
    * well-formed but UNKNOWN -> pruned against the vocabulary
    * well-formed and KNOWN   -> still adopted (the control)
    """

    async def _run(observed_value):
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
            {"id": "f_live", "name": "Live", "parent_id": "", "owner_app": ""},
        ]
        _commit_vocabulary(state)
        state._slots["a"] = _slot("a", folder_id="f1")

        async def _merge_shim(_st, _sl, _fields, **_kw):
            await asyncio.sleep(0)
            return (SweepMergeOutcome.SUPERSEDED, {"folder_id": observed_value})

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence._merge_slot_meta", _merge_shim)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.delete("/api/chat/folders/f1")
        assert resp.status == 200
        return state._slots["a"].folder_id

    # CONTROL -- a live, known folder is still adopted. Without this arm a fix that
    # dropped every observed value would pass the two rejection arms.
    assert (
        await _run("f_live") == "f_live"
    ), "a known folder observed under the record's lock must still be adopted"

    # MALFORMED -- a JSON array. Truthy, so `or ""` keeps it, and str() would render
    # it as the literal "['f_live']".
    kept_malformed = await _run(["f_live"])
    assert kept_malformed == "", (
        f"a malformed persisted folder_id entered live state as {kept_malformed!r}; "
        "the observed value must go through folder_id_for_restore"
    )

    # UNKNOWN -- well-formed but naming no current folder. PRESERVED, see the message.
    assert await _run("f_ghost") == "f_ghost", (
        "a well-formed observed folder_id must survive when no transition can be proven. "
        "REVERSED deliberately: ``observed`` is read from disk inside the sweep, never "
        "captured before an await, so no committed-present -> committed-absent transition "
        "is provable and absence is indistinguishable from a readable-but-stale store. "
        "This site passes was_committed=None -- observed, nothing proven -- which is why the "
        "malformed arm above still rejects. A dangling id self-corrects on the next folder "
        "operation; unfiling a validly-filed conversation does not."
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
    """Drive the restore-time vocabulary prune and return the slot's surviving tags.

    Exercises ``_rehydrate_slot_from_history``, one of the readers that gates on the
    committed snapshot. *committed* is the snapshot to install --
    ``None`` for UNKNOWN, a frozenset for KNOWN.
    """
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    state = _make_state(tmp_path)
    state._tags = list(live_tags)
    state._committed_tag_ids = committed
    log = state.conversation_log
    log.append("dashboard:s1", "user", "hello")
    log.update_metadata("dashboard:s1", {"tags": list(persisted_tags)})
    restored = _rehydrate_slot_from_history(state, "s1")
    assert restored is not None, "the slot failed to rehydrate at all"
    return list(restored.tags)


def test_restore_prune_reads_the_committed_vocabulary_not_the_live_tag_list(tmp_path) -> None:
    """The restore prune must gate on the COMMITTED snapshot, not the live tag list.

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
    restored2 = _rehydrate_slot_from_history(state2, "s2")
    assert restored2 is not None and restored2.tags == [], (
        f"the control kept {restored2.tags if restored2 else None!r}: with a confirmed "
        "seed the vocabulary is authoritative and a dangling id must still be pruned"
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
    that cancellation in a ``try``/``except asyncio.CancelledError`` and re-raise it only
    after its own durable consequences and its audit line.
    """
    tree = ast.parse(source)
    protected: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_catches_cancelled(h) for h in node.handlers):
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
            "before its audit line and any remaining durable step. Capture and re-raise "
            "last:\n"
            "    cancelled: BaseException | None = None\n"
            f"    try:\n        await {_SWEEP_DRAIN}(...)\n"
            "    except asyncio.CancelledError as exc:\n        cancelled = exc\n"
            "    ...durable work, then the audit line...\n"
            "    if cancelled is not None:\n        raise cancelled\n"
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


def test_sweep_drain_detector_flags_a_bare_await() -> None:
    src = (
        "async def api_thing_delete(request):\n"
        f"    await {_SWEEP_DRAIN}(_sweep())\n"
        "    sel().log_api_access(outcome='allowed')\n"
    )
    found = find_uncaptured_sweep_drains(src)
    assert [(v[1], v[2]) for v in found] == [(2, "api_thing_delete")], found


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


def test_every_shield_drain_loop_is_cross_referenced() -> None:
    """The shield-drain-reraise loops must be findable from each other.

    A reviewer's point, and a fair one: this protocol is now spelled in three places, and
    it is subtle enough that a correction applied to one copy and not the others is the
    likely failure. They are deliberately NOT unified -- the two in ``snapshot_commit``
    add publish-on-both-exits and failure-supersedes-cancellation, and the third guards a
    config write -- so the mitigation is that each is reachable from the others by a grep.

    This fails if the cross-reference goes stale because the sibling was renamed or moved.
    It deliberately does NOT try to count drain loops repo-wide: a shielded re-await inside
    a cancellation handler appears in 14 files, most of them ordinary single-shield uses, so
    a census would either sit red or need its own maintained allowlist -- the very tax both
    reviewers object to. It pins the pointer, which is the part that rots silently.
    """
    root = _src_root()
    sibling = "run_config_write"
    utils = (root / "dashboard" / "chat_utils.py").read_text(encoding="utf-8")
    assert f"def {sibling}" in utils, (
        f"{sibling} is gone from chat_utils.py, so snapshot_commit's cross-reference to it "
        "is stale. Name wherever that loop now lives, or drop the reference if it is gone."
    )
    commit_src = (root / "dashboard" / "snapshot_commit.py").read_text(encoding="utf-8")
    assert sibling in commit_src, (
        f"snapshot_commit.py no longer names the sibling drain loop ({sibling}), so a future "
        "correction to this protocol would reach two of the three copies."
    )
    assert "asyncio.shield" in commit_src, (
        "snapshot_commit.py no longer shields anything, so the protocol this cross-reference "
        "describes is gone; delete the reference and this gate with it"
    )


def test_the_placement_counter_retires_with_its_only_reader() -> None:
    """``_placement_seq`` must not outlive the sweep adoption it exists for.

    A reviewer's objection, and a fair one: a counter bumped on every ``folder_id`` write
    with exactly ONE reader is a generalized primitive paying for a single caller. It stays
    because a review graded that caller's residual a blocking defect — a stale placement
    restored over a move the user made and undid, which value comparison cannot see.

    What this gate refuses is the OTHER failure: the reader going away and the machinery
    staying. When the merge-aware-save layer retires the guard/adopt surface, this fails and
    says to delete the property, the slot, and the capture with it.
    """
    readers = collect_repo_violations(
        lambda s, p: [
            (p, n.lineno)
            for n in ast.walk(ast.parse(s))
            if isinstance(n, ast.Attribute) and n.attr == "_placement_seq"
        ]
    )
    consumers = [(p, ln) for p, ln in readers if "state.py" not in p]
    assert consumers, (
        "``_placement_seq`` has no reader outside state.py, so the property, the "
        "``__slots__`` entry and the setter's bump are now dead weight. Delete all three "
        "and this gate with them -- the sweep adoption it served has retired."
    )
    detail = "\n".join(f"  {p}:{ln}" for p, ln in consumers)
    assert len(consumers) <= 2, (
        f"{len(consumers)} consumers of ``_placement_seq`` now exist, up from the one this "
        "counter was justified for. A second reader means it has become the general-purpose "
        "primitive a reviewer warned against; either justify it as such in its docstring or "
        f"give the new caller its own narrower test.\n{detail}"
    )


def test_the_tag_prune_asymmetry_self_retires_when_it_is_closed() -> None:
    """The accepted data-loss deferral must self-retire the moment it is closed.

    `folder_id_for_restore` can withhold its prune via ``was_committed``;
    `tag_ids_for_restore` has no withholding channel at all, so a readable-but-stale
    ``tags.json`` still strips tags at boot. That is a deliberate deferral, and the moment
    someone gives the tag reader such a channel this gate FAILS -- forcing the now-false
    declaration and its spec paragraph to be deleted in the same change rather than left
    behind contradicting the code.

    Keyed on the SIGNATURE only, and on EITHER spelling a future author might reach for, so
    the gate cannot be bypassed by choosing the other name. An earlier revision also
    asserted two docstring substrings, which made rewording a comment red the build for no
    correctness reason.
    """
    src = (_src_root() / "dashboard" / "state.py").read_text(encoding="utf-8")
    start = src.index("def tag_ids_for_restore")
    signature = src[start : src.index("\n", src.index(")", start))]

    withholding = [name for name in ("prune_unknown", "was_committed") if name in signature]
    assert not withholding, (
        f"tag_ids_for_restore now takes {withholding}, so the asymmetry is CLOSED. Delete "
        "the declaration in its docstring and the matching paragraph in "
        "docs/system-specs/modules/history.md, then delete this gate -- a declaration left "
        "standing over closed work misleads the next reader."
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
    damaging direction: advertising a vocabulary that no longer matches the file.

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
async def test_the_fork_validates_the_vocabularies_it_inherits(tmp_path, monkeypatch) -> None:
    """The fork must not copy a parent's DELETED folder or tag onto its child.

    This is the ONLY guard for that producer, not one half of a pair.
    ``api_chat_slot_fork`` resolves its parent from ``state._slots`` and then copies
    that parent's ``folder_id`` and ``tags`` onto a brand-new record, so a parent
    holding an id whose vocabulary entry is already gone makes the dangling
    reference durable on a slot no sweep ever saw. Validating at the producer is
    what let both delete handlers drop their post-await live-view sweep, so nothing
    downstream will catch this if the validation regresses.

    The committed vocabularies here are KNOWN and EMPTY -- ``frozenset()`` -- which
    is the state that prunes. ``None`` would be UNKNOWN and must fail open, which the
    sibling test below pins.
    """
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("dashboard:forkparent", "user", "one")
    log.append("dashboard:forkparent", "assistant", "two")
    parent = state.get_or_create_slot("forkparent")
    parent.append("user", "one", "msg msg-u")
    parent.append("assistant", "two", "msg msg-a")
    parent.drain()
    # The parent still names a folder and a tag whose vocabulary rows are gone.
    parent.folder_id = "deleted-folder"
    parent.tags = ["deleted-tag"]
    state._folders = []
    state._tags = []
    state._committed_folder_ids = frozenset()
    state.publish_committed_tag_ids([])

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/forkparent/fork",
            json={"at_message_index": 1, "prompt": "forked"},
        )
        status = resp.status
        payload = await resp.json()
    assert status == 200, f"fork failed for another reason: {status} {payload}"

    child = state._slots[payload["key"]]
    # NEGATIVE CONTROLS -- true on unfixed code, so a vacuous pass is visible.
    assert child is not parent, "the fork returned the parent"
    assert parent.folder_id == "deleted-folder", "the parent was mutated; premise broken"
    assert parent.tags == ["deleted-tag"], "the parent's tags were mutated"

    assert child.folder_id == "deleted-folder", (
        f"the fork UNFILED the child (folder_id={child.folder_id!r}). REVERSED "
        "deliberately: this fixture's folder id is absent from the committed vocabulary "
        "BEFORE the fork begins, which is indistinguishable from a readable-but-stale "
        "folders.json -- so pruning it durably unfiles a validly-filed conversation on a "
        "record no sweep can reach. The producer now prunes only on an observed "
        "committed-present -> committed-absent transition; preserving a dangling id is "
        "self-correcting on the next folder operation, and cold-start restore already "
        "keeps it on purpose. The tag half below still prunes, because the tag validator "
        "takes no pre-operation observation."
    )
    assert child.tags == [], (
        f"the fork copied a deleted tag id onto a NEW slot (tags={child.tags!r}); same "
        "reason -- validate at the producer, not in a later sweep"
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


@pytest.mark.asyncio
async def test_a_refused_move_does_not_restore_the_deleted_folder(tmp_path, monkeypatch) -> None:
    """A refused move must not revert a slot onto a folder deleted meanwhile.

    ``api_chat_slot_folder`` reads ``previous = slot.folder_id`` BEFORE awaiting
    ``_unhide_folder``, then restores it verbatim when that await reports the
    target gone. When a delete of the PREVIOUS folder lands inside the window,
    that restore reattaches a folder that no longer exists -- and unlike the
    mid-persist case, it can resume after the delete handler has already
    returned, so no sweep inside the delete can reach it.
    """
    from kiro_crew.dashboard import chat_folders as mod

    state = _make_state(tmp_path)
    state._folders = [
        {"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": "f2", "name": "Archive", "parent_id": "", "owner_app": ""},
    ]
    _commit_vocabulary(state)
    slot_a = _slot("a", folder_id="f1")
    state._slots["a"] = slot_a

    delete_done = asyncio.Event()
    refused: list[str] = []
    real_unhide = mod._unhide_folder

    async def _unhide_waits_behind_the_delete(st, folder_id):
        # The move's own pre-check passed while f2 was still present. Hold here
        # until the delete of f1 has fully returned, then drop f2 so the REAL
        # existence check -- the one taken under the store lock -- reports gone.
        await delete_done.wait()
        state._folders = [f for f in state._folders if f["id"] != folder_id]
        _commit_vocabulary(state)
        verdict = await real_unhide(st, folder_id)
        refused.append(folder_id)
        return verdict

    async def _noop_persist(_state, _slot_arg, *args, **kwargs):
        return None

    monkeypatch.setattr(mod, "save_slot_off_loop", _noop_persist)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        move = asyncio.ensure_future(
            client.patch("/api/chat/slots/a/folder", json={"folder_id": "f2"})
        )
        await asyncio.sleep(0)
        monkeypatch.setattr(mod, "_unhide_folder", _unhide_waits_behind_the_delete)
        resp = await client.delete("/api/chat/folders/f1")
        delete_done.set()
        move_resp = await move

    assert resp.status == 200
    # NEGATIVE CONTROLS -- true on unfixed code as well: the move really was
    # refused under the lock, and the delete of f1 really committed.
    assert refused == ["f2"], "the move was never refused; the revert path did not run"
    assert move_resp.status == 400, "a move into a deleted folder must be refused"
    assert not any(f["id"] == "f1" for f in state._folders), "the folder delete committed"

    assert slot_a.folder_id != "f1", (
        "the refused move restored the folder id it captured before the await, but that "
        "folder was deleted inside the window -- so the slot now names a folder that "
        "does not exist and the next save makes it durable. Validate the restored value "
        "through folder_id_for_restore like every other adoption site"
    )


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
    # Two-step raw-then-validate, with NO await between the two statements, so the
    # unvalidated value is never observable outside the frame.
    "_rehydrate_slot_from_history": "raw assign then tag_ids_for_restore, no await between",
    "_apply_recent_session": "raw assign then tag_ids_for_restore, no await between",
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
    return out


def _routes_through_a_validator(node: ast.Assign, scope: ast.AST | None = None) -> bool:
    """True when the assigned value comes from one of the shared validators."""
    indirect = _validator_bound_locals(scope) if scope is not None else set()
    for sub in ast.walk(node.value):
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


def test_a_parked_worker_slot_does_not_bring_back_a_deleted_folder(tmp_path, monkeypatch) -> None:
    """The interleaving the sweep cannot reach: parked, folder deleted, then restored.

    Drives the REAL teardown path rather than re-implementing it. The slot is popped
    before the archiving await, the committed folder vocabulary loses the folder DURING
    that await -- exactly where a delete handler commits -- and the failed archive then
    puts the slot back. Neither sweep pass ever saw this slot: it was absent from the
    captured list and from the live view for the whole delete.

    Without revalidation on that restore the slot returns holding the deleted id and the
    next save makes it durable, which is the phantom-folder-after-restart regression.
    """
    from kiro_crew.apps.builtins.spec_builder.backend import repository as sb_repo
    from kiro_crew.apps.builtins.spec_builder.backend import runtime as sb_runtime

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "F1", "parent_id": ""}]
    state.publish_committed_folder_ids(state._folders)

    slot_key = sb_repo._slot_key("probe")
    slot = state.get_or_create_slot(slot_key)
    slot._app = sb_repo.APP_NAME
    slot.folder_id = "f1"
    slot.tags = ["t1"]
    assert state.get_slot(slot_key) is slot

    async def _delete_the_folder_then_fail(state_, slot_, **kwargs):
        assert state_.get_slot(slot_key) is None, (
            "precondition: the teardown must have popped the slot before this await, "
            "otherwise the parked window under test does not exist"
        )
        state_._folders[:] = []
        state_.publish_committed_folder_ids(state_._folders)
        raise RuntimeError("archive failed")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
        _delete_the_folder_then_fail,
    )

    archived = asyncio.run(
        sb_runtime._teardown_worker_slot(state, "probe", only_slot=slot, require_archive=True)
    )

    assert archived is False, "a failed require_archive teardown must report failure"
    assert state.get_slot(slot_key) is slot, (
        "the failed archive must still restore the slot -- the transcript is the user's "
        "data and the caller retries against it"
    )
    assert slot.folder_id == "", (
        "the restored slot still names the deleted folder. A parked slot is invisible to "
        "both sweep passes, so the restore is the last place this can be caught; route "
        "the id through folder_id_for_restore with the membership captured before the await"
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


def test_a_parked_slot_is_revalidated_before_it_re_enters_the_registry() -> None:
    """Restoring a popped slot must revalidate its ids, like every other adopter.

    THE HOLE THIS CLOSES, and why four sibling gates missed it. They all key on an
    assignment to ``slot.folder_id`` or ``slot.tags``. Re-registering the whole slot
    object performs neither, so a slot parked by a teardown could come back carrying a
    folder id the delete had already committed away -- durably, because the restore is
    followed by a save. The sweep cannot reach such a slot by construction: it iterates
    a list captured before the commit plus the live view, and a parked slot is absent
    from both, which is why the check belongs at the restore.

    Deliberately NO allowlist: a slot the function created itself is exempt
    structurally, via the factory binding, so session import needs no entry and cannot
    silently acquire one.
    """
    offenders = collect_repo_violations(find_unvalidated_parked_restores)
    detail = "\n".join(f"  {p}:{ln} in {fn}()" for p, ln, fn in offenders)
    assert not offenders, (
        "a slot is put back into the live registry after an await without revalidating "
        "its vocabulary. While it was popped, a folder or tag delete could commit, and "
        "neither sweep pass can see a parked slot -- so the restore is the only place "
        "left to catch it. Route the ids through folder_id_for_restore / "
        f"tag_ids_for_restore first, capturing the committed membership BEFORE the "
        f"await:\n{detail}"
    )

    # Delegating to the shared helper satisfies the rule above, so the helper itself
    # has to keep both validators or that acceptance becomes the hole.
    helper_src = ""
    for py in sorted(_src_root().rglob("*.py")):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if f"def {_PARKED_REVALIDATOR}" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef) and node.name == _PARKED_REVALIDATOR:
                helper_src = ast.dump(node)
    assert helper_src, (
        f"{_PARKED_REVALIDATOR} is gone, but the gate still accepts a call to it as "
        "proof of validation -- so every parked restore delegating to it is now "
        "unchecked. Remove the acceptance too, or restore the helper."
    )
    for validator in sorted(_VOCABULARY_VALIDATORS):
        assert validator in helper_src, (
            f"{_PARKED_REVALIDATOR} no longer calls {validator}, so a restore that "
            "delegates to it is only apparently validated"
        )


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
    allowlist forever asserting a structural reason about code that no longer exists.
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
        "the scan did not reach api_chat_slot_fork, the producer this PR validates at "
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


# ── The deferred layer decision, tracked in code rather than narrated ──────────

#: How many ``save_slot_off_loop(..., force=True)`` callers remain on the clobber
#: protocol. This count is AST-derived, not grepped: three of them span multiple
#: lines, so the single-line ``save_slot_off_loop(.*force=True`` grep everyone
#: reached for finds only 7 calls and misses them. The base has 13; this change
#: removed 3 by routing the two delete sweeps through
#: ``persist_swept_slot_meta``. The remaining 10 are annotated in place and are
#: deliberately deferred behind ONE layer decision -- make the save merge-aware, so it
#: cannot clobber a field it did not author -- which retires the guard/adopt surface for
#: all of them at once instead of growing a bespoke closure per site. That decision is
#: TRACKED at kirodotdev/KiroCrew#8361, so this gate points at a destination rather than
#: only at a doc paragraph, and the issue records the two candidates already measured and
#: ruled out (persisting ``closed`` positively, and dropping it from the owned key set).
_FORCE_SAVE_CLOBBER_SITES = 10


def _count_force_saves(source: str, path: str = "<source>") -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, enclosing_function)`` for each force=True full save."""
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
            if name != "save_slot_off_loop":
                continue
            for kw in node.keywords:
                if kw.arg == "force" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    out.append((path, node.lineno, scope.name))
    return out


def test_the_deferred_force_save_layer_decision_has_not_grown() -> None:
    """The clobber deferral must not accrete new sites while the decision is open.

    WHY A GATE AND NOT A NOTE. ``force=True`` rebuilds every ``SLOT_OWNED_META_KEYS``
    entry from the in-memory slot, and in that set an ABSENT ``closed`` means "cleared"
    -- so a full save issued after a concurrent close committed erases that close and
    the dismissed tab returns on the next restart. This change closes that at the two
    delete sweeps by persisting metadata only. It does NOT close it at the other ten,
    because the honest fix is one layer decision (a merge-aware save) rather than ten
    bespoke guard/adopt closures. Persisting ``closed`` positively is NOT a second
    candidate -- see
    ``test_removing_closed_from_the_owned_set_requires_an_explicit_adopt_clear``.

    Deferring is a defensible call; letting the deferral GROW silently is not, and neither
    is letting the SCAFFOLDING outlive the sites it was built for. An eleventh caller would
    make the interim surface accrete exactly as it is meant to be retired. So the count is
    pinned EXACTLY, in both directions: adding a ``force=True`` save fails here and puts the
    layer decision in front of whoever is adding it, and REMOVING one also fails here --
    which is the point, because that is the moment the guard/adopt machinery, this census
    and the adopter allowlist become removable and someone has to say so out loud. An upper
    bound would have let the interim surface be retired site-by-site while its scaffolding
    quietly stayed forever.
    """
    sites = collect_repo_violations(_count_force_saves)
    assert len(sites) == _FORCE_SAVE_CLOBBER_SITES, (
        f"{len(sites)} save_slot_off_loop(..., force=True) callers now exist against the "
        f"pinned {_FORCE_SAVE_CLOBBER_SITES}. force=True rewrites every slot-owned "
        "metadata key from memory, so a save racing a concurrent close erases that "
        "close's `closed` flag and the dismissed tab comes back after a restart.\n\n"
        "If the count went UP: take the layer decision the existing sites are "
        "waiting on -- make the save merge-aware. (Persisting `closed` positively does "
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
        "vacuously and would not notice an eleventh clobber site being added. Either the "
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


def test_a_stale_vocabulary_preserves_a_filing_it_never_carried(tmp_path) -> None:
    """Prune only on an observed committed-present -> committed-absent TRANSITION.

    The discriminating pair: both halves validate against the IDENTICAL committed
    vocabulary (``{"f2"}``), so only the pre-operation OBSERVATION differs. A
    readable-but-stale ``folders.json`` never carried ``f1``, so the operation observes it
    already absent and the filing must survive; a real delete observes it present and then
    finds it gone, and that must still prune. Bare absence cannot tell those apart, which
    is exactly why absence is the wrong test.

    Without the transition rule the stale half unfiles ``f1``, and because the await-window
    callers run mid-session the next ordinary save makes that unfiling durable -- the
    data-loss the anchor names.
    """
    state = _make_state(tmp_path)
    # A readable-but-stale store: parses fine, simply predates the folder f1 was filed into.
    state.publish_committed_folder_ids([{"id": "f2", "name": "Other"}])
    assert state._committed_folder_ids == frozenset({"f2"}), "fixture: vocabulary not published"

    stale_observation = state.committed_folder_membership("f1")
    assert stale_observation is False, (
        "fixture: the stale store must observe f1 as already absent, or the pair does not "
        f"discriminate (observed {stale_observation!r})"
    )
    assert state.folder_id_for_restore("f1", was_committed=stale_observation) == "f1", (
        "a filing the vocabulary NEVER carried was pruned; absence against a "
        "readable-but-stale folders.json is not a delete, and the next save makes the "
        "unfiling durable"
    )

    # Now the real delete: f1 IS observed committed, then the delete republishes without it.
    state.publish_committed_folder_ids(
        [{"id": "f1", "name": "Work"}, {"id": "f2", "name": "Other"}]
    )
    delete_observation = state.committed_folder_membership("f1")
    assert delete_observation is True, "fixture: f1 must be observed present before the delete"
    state.publish_committed_folder_ids([{"id": "f2", "name": "Other"}])
    assert state._committed_folder_ids == frozenset({"f2"}), (
        "fixture: both halves must validate against the SAME committed set, or the pair "
        "does not isolate the observation as the only variable"
    )

    assert state.folder_id_for_restore("f1", was_committed=delete_observation) == "", (
        "a folder observed committed-present and now committed-absent must still be pruned "
        "-- otherwise a crash mid-delete strands a dangling id forever"
    )

    # No observation supplied keeps the pre-existing behaviour, so untouched callers and the
    # KNOWN-empty prune rule are unaffected.
    assert state.folder_id_for_restore("f1") == "", "an unobserved caller must still prune"
    # UNKNOWN still fails open, and a malformed value is still dropped unconditionally.
    state._committed_folder_ids = None
    assert state.committed_folder_membership("f1") is None, "UNKNOWN must carry no claim"
    assert state.folder_id_for_restore("f1") == "f1", "UNKNOWN must fail open"
    state.publish_committed_folder_ids([{"id": "f2", "name": "Other"}])
    assert (
        state.folder_id_for_restore(["not", "a", "str"], was_committed=False) == ""
    ), "the malformed rejection must stay unconditional"


@pytest.mark.asyncio
async def test_the_fork_preserves_a_filing_a_stale_vocabulary_never_carried(
    tmp_path, monkeypatch
) -> None:
    """End-to-end at one of the two sites the finding names.

    The fork copies the parent's ``folder_id`` onto a NEW record, so a prune here is
    immediately durable -- the new slot is saved with ``folder_id`` cleared and no sweep
    can reach a record that did not exist when any snapshot was taken.
    """
    from kiro_crew.dashboard import chat_fork as mod

    state = _make_state(tmp_path)
    # The live folder list still knows f1; the COMMITTED snapshot is stale and does not.
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    state.publish_committed_folder_ids([{"id": "f2", "name": "Other"}])
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot

    observed = state.committed_folder_membership("f1")
    assert observed is False, f"fixture: f1 must read already-absent (observed {observed!r})"

    forked = state.get_or_create_slot("chat-forked")
    forked.folder_id = state.folder_id_for_restore("f1", was_committed=observed)
    assert forked.folder_id == "f1", (
        "the fork unfiled the child against a vocabulary that never carried the parent's "
        "folder; the copy lands on a new record outside every delete sweep, so it is "
        "durable immediately"
    )
    assert mod.api_chat_slot_fork is not None, "fixture: the fork handler must be importable"


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
async def test_a_close_failure_restore_keeps_a_filing_a_stale_vocabulary_never_carried(
    tmp_path, monkeypatch
) -> None:
    """The close-failure restore must not unfile against a stale snapshot.

    This site puts the slot back into ``_slots`` after a failed save, and the periodic
    flush then persists it -- so clearing ``folder_id`` here against a readable-but-stale
    ``folders.json`` is durable loss on a record the delete sweeps cannot reach.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    # Live list still knows f1; the COMMITTED snapshot is stale and predates the filing.
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    state.publish_committed_folder_ids([{"id": "f2", "name": "Other"}])
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot

    observed = state.committed_folder_membership("f1")
    assert observed is False, f"fixture: f1 must read already-absent (observed {observed!r})"

    saves: list[str] = []

    async def _failing_save(*_a, **_kw):
        saves.append("attempted")
        raise OSError("disk full")

    monkeypatch.setattr(mod, "save_slot_off_loop", _failing_save)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    # POSITIVE CONTROL: without this a wrong signature or an early return passes vacuously.
    assert saves == ["attempted"], (
        f"fixture: the failing save was never reached (saves={saves}), so the restore "
        "revalidation this test targets never ran"
    )
    assert (
        state._slots.get("a") is slot
    ), "fixture: the failed save must have restored the slot into _slots"
    assert slot.folder_id == "f1", (
        f"the close-failure restore unfiled the slot (folder_id={slot.folder_id!r}) against "
        "a vocabulary that never carried f1; the next periodic flush makes that durable"
    )


@pytest.mark.asyncio
async def test_a_folder_deleted_during_the_post_pop_await_is_still_pruned_on_restore(
    tmp_path, monkeypatch
) -> None:
    """The observation must be taken at the POP, not after the awaits that follow it.

    A running-slot close pops the slot and then awaits task cancellation and the handover
    tail. A folder delete committing inside THAT window is a genuine
    committed-present -> committed-absent transition, but an observation taken after those
    awaits already reads absent -- so the transition looks like a stale store and the failed
    archive reattaches a folder that is really gone, which the next save makes durable.

    The discriminating pair with
    ``test_a_close_failure_restore_keeps_a_filing_a_stale_vocabulary_never_carried``: there
    the id was never committed and must survive; here it WAS committed at the pop and must
    be pruned. Only the observation's position separates them.
    """
    from kiro_crew.dashboard import chat_handlers as mod

    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "parent_id": "", "owner_app": ""}]
    state.publish_committed_folder_ids(state._folders)
    slot = _slot("a", folder_id="f1")
    state._slots["a"] = slot

    assert state.committed_folder_membership("f1") is True, (
        "fixture: f1 must be committed-present at the pop, or this pair does not "
        "discriminate the delete from a stale store"
    )

    deleted_during_window: list[str] = []
    saves: list[str] = []

    async def _turn_that_deletes_while_being_cancelled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # THE window the finding names: close_slot has popped the slot and is awaiting
            # this cancellation. The folder delete commits right here.
            state._folders = []
            state.publish_committed_folder_ids([])
            deleted_during_window.append("f1")
            raise

    turn = asyncio.ensure_future(_turn_that_deletes_while_being_cancelled())
    await asyncio.sleep(0)
    # ``running`` is derived from ``task``, so assigning the task is what opens the window.
    slot.task = turn
    assert slot.running is True, "fixture: the slot must look running, or the cancel is skipped"

    async def _failing_save(*_a, **_kw):
        saves.append("attempted")
        raise OSError("disk full")

    monkeypatch.setattr(mod, "save_slot_off_loop", _failing_save)

    with contextlib.suppress(Exception):
        await mod.close_slot(state, slot, "a")

    assert deleted_during_window == ["f1"], (
        f"fixture: the delete never landed in the post-pop cancellation window (got "
        f"{deleted_during_window}); without it the observation's POSITION is not tested"
    )
    assert saves == [
        "attempted"
    ], f"fixture: the failing save was never reached (saves={saves}), so the restore never ran"
    assert state._slots.get("a") is slot, "fixture: the failed save must have restored the slot"
    assert slot.folder_id == "", (
        f"the restore reattached a folder deleted inside the post-pop window "
        f"(folder_id={slot.folder_id!r}). f1 was committed-present at the pop and absent "
        "after, so this IS a transition -- the observation must be taken at the pop, before "
        "the cancellation and handover awaits, or a real delete reads as a stale store"
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
        state.folder_id_for_restore("f1", was_committed=None) == "f1"
    ), "the still-committed folder id did not survive validation after a failed write"


def test_an_observed_unknown_vocabulary_is_not_treated_as_no_observation(tmp_path) -> None:
    """``was_committed=None`` means OBSERVED-UNKNOWN and must never license a prune.

    A transition caller captures membership before its awaits. If the vocabulary was
    UNKNOWN at that moment it captures ``None`` -- it looked, and learned nothing. A
    concurrent write can then publish a vocabulary omitting the folder, so by the time the
    caller revalidates the id is absent from a KNOWN set. Treating that ``None`` as "no
    observation" makes bare absence read as a delete and saves the slot durably unfiled.

    Three-way discrimination, because collapsing any two of these is the defect.
    """
    state = _make_state(tmp_path)
    # The vocabulary published mid-operation, and it omits the folder the caller captured.
    state.publish_committed_folder_ids([{"id": "other", "name": "O", "parent_id": ""}])

    assert state.folder_id_for_restore("f1", was_committed=None) == "f1", (
        "an OBSERVED-UNKNOWN capture was treated as a provable delete. The caller looked "
        "before its awaits and learned nothing, so a vocabulary published since cannot "
        "turn bare absence into a transition -- the slot is saved unfiled"
    )
    assert (
        state.folder_id_for_restore("f1", was_committed=False) == "f1"
    ), "an id observed ABSENT before the operation must be kept: absence now is not new"
    assert state.folder_id_for_restore("f1", was_committed=True) == "", (
        "a real committed-present -> committed-absent TRANSITION must still prune, or a "
        "genuine delete strands a dangling id"
    )
    # NO observation at all keeps the pre-existing absence-only behaviour.
    assert state.folder_id_for_restore("f1") == "", (
        "a caller that took no observation must keep pruning on absence, which is what "
        "the observed-but-unproven callers rely on being separate from"
    )
