"""Build gates for the invariants the folder and tag delete sweeps rely on.

Each gate scans every module under ``src/kiro_crew`` and has a planted violation
below that proves it fires. None takes an inline suppression marker; the only
exemptions are the write-chain tables and census bounds below, each with its reason
beside it. The ``# loop-ok:`` marker belongs to ``test_no_blocking_call_on_loop.py``
and means something else.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator

from source_corpus import parsed_candidates, src_root

_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SUSPENDS = (ast.Await, ast.AsyncFor, ast.AsyncWith)


def _own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Every node under *node* that runs in its frame (a nested def or class is another)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        yield child
        yield from _own_scope(child)


def _in_frame(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node of the statement block *stmts* that runs in the block's own frame."""
    for stmt in stmts:
        if not isinstance(stmt, _NESTED_SCOPES):
            yield stmt
            yield from _own_scope(stmt)


def _callee(node: ast.AST) -> str:
    """The called name of a call, ``""`` for anything else."""
    if not isinstance(node, ast.Call):
        return ""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", "")


def _scan(find: Callable[[ast.Module], Iterator[tuple]], *needles: str) -> list[tuple]:
    """``(path, *finding)`` for every finding *find* reports in the package."""
    return [
        (path.relative_to(src_root().parent).as_posix(), *finding)
        for path, _text, tree in parsed_candidates(require_any=needles)
        for finding in find(tree)
    ]


# ── 1. No await inside a loop over a live ``_slots`` view ─────────────────────


def _is_live_slots(node: ast.expr) -> bool:
    """``x._slots``, ``getattr(x, "_slots", ...)``, or a ``.values/.items/.keys()`` of one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"values", "items", "keys"}
    ):
        node = node.func.value
    if isinstance(node, ast.Attribute):
        return node.attr == "_slots"
    return (
        isinstance(node, ast.Call)
        and _callee(node) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "_slots"
    )


def find_live_view_loops(tree: ast.Module) -> Iterator[tuple[int, bool]]:
    """``(lineno, suspends)`` for each ``for`` over a live ``_slots`` view."""
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _is_live_slots(node.iter):
            yield node.lineno, any(isinstance(n, _SUSPENDS) for n in _in_frame(node.body))


def test_no_loop_over_a_live_slots_view_awaits() -> None:
    """Another coroutine pops a slot during the await, and the loop then raises
    ``RuntimeError: dictionary changed size during iteration`` half-way through."""
    loops = _scan(find_live_view_loops, "_slots")
    assert loops, "the scan found no loop over _slots at all, so this gate checks nothing"
    found = [(path, line) for path, line, suspends in loops if suspends]
    assert not found, (
        "a loop over a live _slots view awaits; iterate a snapshot instead, "
        "e.g. `for slot in list(state._slots.values()):`\n"
        + "\n".join(f"  {path}:{line}" for path, line in found)
    )


def test_the_live_view_gate_fires_on_each_view_and_spares_a_snapshot() -> None:
    planted = (
        "async def f(state, self):\n"
        "    for s in state._slots.values():\n"
        "        await save(s)\n"
        "    for k, s in self._slots.items():\n"
        "        async with lock:\n"
        "            pass\n"
        "    for k in state._slots:\n"
        "        await save(k)\n"
        "    for s in getattr(state, '_slots', {}).values():\n"
        "        await save(s)\n"
        "    for s in list(state._slots.values()):\n"
        "        await save(s)\n"
        "    for s in state._slots.values():\n"
        "        save(s)\n"
        "    for s in state._slots.values():\n"
        "        async def later():\n"
        "            await save(s)\n"
    )
    flagged = sorted(
        line for line, suspends in find_live_view_loops(ast.parse(planted)) if suspends
    )
    assert flagged == [2, 4, 7, 9]


# ── 2 and 3. Store writes reach disk only through their commit choke point ────

#: Each folder-store writer, mapped to the only functions that may reach it. The
#: chain ends in ``FolderRepository.mutate``, which holds the store lock and fires
#: ``on_committed`` only once the write is proven on disk. ``save_folders`` writes
#: around that lock and has no production caller, so it may not gain one here.
_FOLDER_WRITE_CHAIN: dict[str, frozenset[str]] = {
    "_write_folders_confirmed": frozenset({"mutate_folders"}),
    "write_confirmed": frozenset({"mutate", "_write_folders_confirmed"}),
    "save_folders": frozenset(),
}

#: The same for the tag vocabulary: ``_commit_tags_snapshot`` waits the write out and
#: confirms it, so a cancelled delete can still tell a landed write from a lost one.
#: ``save_tags`` writes around the tag lock; only the load's own back-fill may call it.
_TAG_WRITE_CHAIN: dict[str, frozenset[str]] = {
    "_write_tags_snapshot": frozenset({"_commit_tags_snapshot"}),
    "save_tags_snapshot": frozenset({"_write_tags_snapshot"}),
    "save_tags": frozenset({"load_tags"}),
}


def _chain_bypasses(chain: dict[str, frozenset[str]]):
    """A detector reporting ``(lineno, function, writer)`` for each off-chain reference.

    Any read of the name counts, not only a call: the sanctioned links hand the
    writer to ``asyncio.to_thread`` as a bare reference, and a bypass can too.
    """

    def find(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
        for fn in ast.walk(tree):
            if not isinstance(fn, _FUNCTIONS):
                continue
            for node in _own_scope(fn):
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                else:
                    continue
                if name in chain and fn.name not in chain[name]:
                    yield node.lineno, fn.name, name

    return find


def _assert_no_bypass(store: str, chain: dict[str, frozenset[str]], entry: str) -> None:
    found = _scan(_chain_bypasses(chain), *chain)
    assert not found, (
        f"a {store} write bypasses its commit choke point; go through {entry}, which "
        "holds the store lock and confirms the write before anyone acts on it\n"
        + "\n".join(f"  {path}:{line} {fn}() reaches {name}" for path, line, fn, name in found)
    )


def test_no_folder_write_bypasses_mutate_folders() -> None:
    _assert_no_bypass("folder-store", _FOLDER_WRITE_CHAIN, "state.mutate_folders(...)")


def test_no_tag_write_bypasses_commit_tags_snapshot() -> None:
    _assert_no_bypass("tag-vocabulary", _TAG_WRITE_CHAIN, "_commit_tags_snapshot(...)")


def test_the_write_chain_gates_fire_on_a_bypass_and_spare_the_chain() -> None:
    planted = (
        "async def api_folder_rename(state, snapshot):\n"
        "    state.save_folders()\n"
        "    await asyncio.to_thread(state._write_folders_confirmed, path, snapshot)\n"
        "async def api_tag_rename(state, snapshot):\n"
        "    await asyncio.to_thread(lambda: state.save_tags_snapshot(snapshot))\n"
        "    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)\n"
        "    state.save_tags()\n"
        "async def _commit_tags_snapshot(state, snapshot):\n"
        "    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)\n"
        "def _write_tags_snapshot(state, snapshot):\n"
        "    state.save_tags_snapshot(snapshot)\n"
    )
    tree = ast.parse(planted)
    assert sorted(_chain_bypasses(_FOLDER_WRITE_CHAIN)(tree)) == [
        (2, "api_folder_rename", "save_folders"),
        (3, "api_folder_rename", "_write_folders_confirmed"),
    ]
    assert sorted(_chain_bypasses(_TAG_WRITE_CHAIN)(tree)) == [
        (5, "api_tag_rename", "save_tags_snapshot"),
        (6, "api_tag_rename", "_write_tags_snapshot"),
        (7, "api_tag_rename", "save_tags"),
    ]


def test_the_write_chains_name_functions_that_exist() -> None:
    """A renamed link would leave its gate matching nothing, and passing forever."""
    defined = {
        node.name
        for chain in (_FOLDER_WRITE_CHAIN, _TAG_WRITE_CHAIN)
        for _path, _text, tree in parsed_candidates(require_any=tuple(chain))
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTIONS)
    }
    for chain in (_FOLDER_WRITE_CHAIN, _TAG_WRITE_CHAIN):
        for writer, callers in chain.items():
            assert {writer, *callers} <= defined, (writer, {writer, *callers} - defined)


# ── 4. The ``force=True`` full-save census does not grow ──────────────────────

#: A forced full save rewrites every slot-owned metadata key from memory, so one
#: that races a concurrent close can erase that close. The fix is one layer
#: decision, a merge-aware save (kirodotdev/KiroCrew#8361); until it lands the
#: surface must not grow. UPPER bounds, so a change that removes a site does not
#: fail; it lowers the number in the same change. "Pinned" means the call names the transcript it
#: expects with ``expected_history_key``, which lets the save refuse a stale one.
_FORCED_FULL_SAVES = 26
_FORCED_FULL_SAVES_WITHOUT_TRANSCRIPT_PIN = 4
_FULL_SAVES = frozenset({"save_slot_off_loop", "_save_slot_to_history"})


def find_forced_saves(tree: ast.Module) -> Iterator[tuple[int, bool]]:
    """``(lineno, pinned)`` for each ``force=True`` full save."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee(node) not in _FULL_SAVES:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        force = keywords.get("force")
        if isinstance(force, ast.Constant) and force.value is True:
            yield node.lineno, "expected_history_key" in keywords


def test_the_forced_full_save_census_does_not_grow() -> None:
    sites = _scan(find_forced_saves, *_FULL_SAVES)
    unpinned = [(path, line) for path, line, pinned in sites if not pinned]
    listing = "\n".join(
        f"  {path}:{line}{'' if pinned else ' (unpinned)'}" for path, line, pinned in sites
    )
    assert sites, "the census found no forced full save, so its bounds assert nothing"
    assert len(sites) <= _FORCED_FULL_SAVES, (
        f"{len(sites)} force=True full saves, above {_FORCED_FULL_SAVES}. Each one can erase "
        "a concurrent close; make the save merge-aware (kirodotdev/KiroCrew#8361) instead "
        f"of adding another.\n{listing}"
    )
    assert len(unpinned) <= _FORCED_FULL_SAVES_WITHOUT_TRANSCRIPT_PIN, (
        f"{len(unpinned)} force=True full saves without expected_history_key, above "
        f"{_FORCED_FULL_SAVES_WITHOUT_TRANSCRIPT_PIN}. Pass the transcript key the slot was "
        f"read under so the save can refuse a stale write.\n{listing}"
    )


def test_the_census_counts_both_spellings_and_reads_the_pin() -> None:
    planted = (
        "async def f(state, slot, key):\n"
        "    await save_slot_off_loop(state, slot, force=True, expected_history_key=key)\n"
        "    await persistence.save_slot_off_loop(state, slot, force=True)\n"
        "    _save_slot_to_history(state, slot, force=True)\n"
        "    await save_slot_off_loop(state, slot)\n"
        "    await save_slot_off_loop(state, slot, force=False)\n"
    )
    assert sorted(find_forced_saves(ast.parse(planted))) == [(2, True), (3, False), (4, False)]


# ── 5. A delete's sweep drain stays inside its cancellation capture ───────────

#: The ledger's two capture blocks. ``run_to_completion`` drains its work and then
#: RE-RAISES the cancellation it absorbed, so awaited bare in a delete handler it
#: unwinds the handler before the audit line and ``reraise_in_order()``.
_CAPTURES = frozenset({"capturing_commit", "capturing_sweep"})
_LEDGER = "VocabularyDeleteCancellations"


def _ledger_holders(
    tree: ast.Module,
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.AST]]]:
    """``(function, its own nodes)`` for each function that builds the delete ledger."""
    for fn in ast.walk(tree):
        if isinstance(fn, _FUNCTIONS):
            scope = list(_own_scope(fn))
            if any(_callee(n) == _LEDGER for n in scope):
                yield fn, scope


def find_uncaptured_drains(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """``(lineno, function)`` for a ``run_to_completion`` outside a capture block,
    in any function that builds a ``VocabularyDeleteCancellations`` ledger."""
    for fn, scope in _ledger_holders(tree):
        captured = {
            id(n)
            for block in scope
            if isinstance(block, ast.With)
            and any(_callee(item.context_expr) in _CAPTURES for item in block.items)
            for n in _in_frame(block.body)
        }
        for n in scope:
            if _callee(n) == "run_to_completion" and id(n) not in captured:
                yield n.lineno, fn.name


def test_no_sweep_drain_escapes_its_cancellation_capture() -> None:
    holders = {
        fn for _path, fn in _scan(lambda t: ((f.name,) for f, _ in _ledger_holders(t)), _LEDGER)
    }
    assert holders >= {
        "api_chat_folder_delete",
        "api_chat_tag_delete",
    }, f"a delete handler no longer builds the ledger, so this gate stopped checking it: {holders}"
    found = _scan(find_uncaptured_drains, _LEDGER)
    assert not found, (
        "a delete handler awaits run_to_completion outside its ledger's capture, so a "
        "client disconnect skips the audit line; wrap it in "
        "`with cancels.capturing_sweep():`\n"
        + "\n".join(f"  {path}:{line} in {fn}()" for path, line, fn in found)
    )


def test_the_drain_gate_fires_on_a_bare_drain_and_spares_a_captured_one() -> None:
    planted = (
        "async def api_delete(state):\n"
        "    cancels = VocabularyDeleteCancellations()\n"
        "    with cancels.capturing_sweep():\n"
        "        await run_to_completion(sweep())\n"
        "    await run_to_completion(sweep())\n"
        "    cancels.reraise_in_order()\n"
        "async def unrelated():\n"
        "    await run_to_completion(work())\n"
    )
    assert sorted(find_uncaptured_drains(ast.parse(planted))) == [(5, "api_delete")]
