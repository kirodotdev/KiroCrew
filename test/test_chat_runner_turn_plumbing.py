"""The turn object's plumbing keeps one value per name across ``_run_chat`` and its phases.

``_run_chat`` and the phase functions cut from it share their state through one
:class:`~kiro_crew.dashboard.chat_turn.turn_locals._ChatTurn` per turn. A phase reads a
name the turn shares under its own name (``x = turn.x`` once, at its start, or right
after a phase it calls returns) and writes it back the moment it changes
(``x = ...`` then ``turn.x = x``, with nothing between), so a write that happens before
an ``await`` is already on the turn when a cancel lands there and the turn's except
arms and ``finally`` read it. A name a closure writes (the steer cut runs on another
task) is never held under its own name at all: every reader and writer spells
``turn.x``, one storage cell, as the closure cell it replaces was.

What is pinned, over ``_run_chat`` and every owner function whose first parameter is
``turn`` (each function's own statements; a nested closure keeps its own scope):

* every store of a name some function reads off the turn (``turn.x``) is followed at once
  by its copy onto the turn, unless the store is itself that read (``x = turn.x``);
* a name a closure writes is never bare, so it is never aliased either;
* no closure of the turn declares a ``nonlocal``;
* after a function awaits a phase (a ``_turn_*`` or ``_on_*`` coroutine), a name the phase may have
  changed on the turn is read bare only after its reload (``x = turn.x``), on every path to
  the read, an exception's included;
* reading a value the turn never set raises ``UnboundLocalError``.

Each source rule has a self-test on synthetic source, so a rule that stopped seeing anything
fails here instead of passing.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.dashboard import chat_turn

#: Phase-local temporaries that share a spelling with a name read off the turn: the
#: phase's own value never leaves it, so its store needs no copy. ``(function, name)``.
_LOCAL_SPELLINGS: frozenset[tuple[str, str]] = frozenset()


def _turn_attr(node: ast.AST, name: str | None = None) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "turn"
        and (name is None or node.attr == name)
    )


def _functions() -> Iterator[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    """``_run_chat`` and every owner function that takes the turn first."""
    runner = ast.parse(inspect.getsource(cr))
    for node in runner.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_chat":
            yield "_run_chat", node
    for owner in chat_turn.owner_modules():
        for node in ast.parse(inspect.getsource(owner)).body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.args.args
                and node.args.args[0].arg == "turn"
            ):
                yield node.name, node


def _own_blocks(fn: ast.AST) -> Iterator[list[ast.stmt]]:
    """Every statement list of *fn*'s own scope (not nested defs or classes)."""
    stack = [fn]
    while stack:
        node = stack.pop()
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block
                stack.extend(
                    s
                    for s in block
                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                )
        for handler in getattr(node, "handlers", []) or []:
            yield handler.body
            stack.extend(handler.body)


def _header_nodes(stmt: ast.stmt) -> list[ast.AST]:
    """*stmt*'s own expression nodes: not the statements of its blocks."""
    fields = [
        value
        for field, value in ast.iter_fields(stmt)
        if field not in ("body", "orelse", "finalbody", "handlers")
    ]
    nodes: list[ast.AST] = []
    for value in fields:
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, ast.AST):
                nodes.extend(ast.walk(item))
    return nodes


def _stored(stmt: ast.stmt) -> list[str]:
    """Every name *stmt* itself binds: an assignment's targets, a ``def``/``class`` name,
    an import's alias, and a walrus anywhere in the statement's own expressions."""
    targets: list[ast.AST] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif (
        isinstance(stmt, (ast.AugAssign, ast.AnnAssign))
        and getattr(stmt, "value", True) is not None
    ):
        targets = [stmt.target]
    names = [
        n.id
        for t in targets
        for n in ast.walk(t)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    ]
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        names += [(alias.asname or alias.name).split(".")[0] for alias in stmt.names]
    names += [
        node.target.id
        for node in _header_nodes(stmt)
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name)
    ]
    return names


def _is_copy(stmt: ast.stmt | None, name: str) -> bool:
    return (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and _turn_attr(stmt.targets[0], name)
        and isinstance(stmt.value, ast.Name)
        and stmt.value.id == name
    )


def _is_read_of_turn(stmt: ast.stmt, name: str) -> bool:
    return isinstance(stmt, ast.Assign) and _turn_attr(stmt.value, name)


def uncopied_stores(fn_name: str, fn: ast.AST, declared: set[str]) -> list[str]:
    found = []
    for block in _own_blocks(fn):
        for i, stmt in enumerate(block):
            # A store of several names is followed by a run of copies, one per name.
            copies = []
            for later in block[i + 1 :]:
                if not (
                    isinstance(later, ast.Assign)
                    and len(later.targets) == 1
                    and _turn_attr(later.targets[0])
                ):
                    break
                copies.append(later)
            for name in _stored(stmt):
                if name not in declared or (fn_name, name) in _LOCAL_SPELLINGS:
                    continue
                if _is_read_of_turn(stmt, name) or any(_is_copy(c, name) for c in copies):
                    continue
                found.append(f"{fn_name}:{stmt.lineno}:{name}")
            if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
                for handler in stmt.handlers:
                    if (
                        handler.name in declared
                        and (fn_name, handler.name) not in _LOCAL_SPELLINGS
                        and not _is_copy(handler.body[0], handler.name)
                    ):
                        found.append(f"{fn_name}:{handler.lineno}:{handler.name}")
            if isinstance(stmt, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                bound = []
                if isinstance(stmt, (ast.For, ast.AsyncFor)):
                    bound = [n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name)]
                else:
                    for item in stmt.items:
                        if item.optional_vars is not None:
                            bound += [
                                n.id
                                for n in ast.walk(item.optional_vars)
                                if isinstance(n, ast.Name)
                            ]
                for name in bound:
                    if (
                        name in declared
                        and (fn_name, name) not in _LOCAL_SPELLINGS
                        and not _is_copy(stmt.body[0], name)
                    ):
                        found.append(f"{fn_name}:{stmt.lineno}:{name}")
    return found


def read_off_the_turn(functions: list[tuple[str, ast.AST]]) -> set[str]:
    """Names some function reads as ``turn.<name>``: a closure or a later phase reads the
    turn's value, so a bare store of one must reach the turn at once."""
    return {
        node.attr
        for _, fn in functions
        for node in ast.walk(fn)
        if _turn_attr(node) and isinstance(node.ctx, ast.Load)
    }


def closure_written(functions: list[tuple[str, ast.AST]]) -> set[str]:
    """Names a nested function, or an owner function that takes the turn, writes on it.

    In a function that is not one of ``_run_chat``'s cut closures (a phase cut in line), a
    copy ``turn.x = x`` of a local of the same name is not such a write: it is how a phase
    hands a value it bound to the rest of the turn (the copy rule above)."""
    out: set[str] = set()
    for name, fn in functions:
        if name != "_run_chat":
            out |= {
                target.attr
                for node in ast.walk(fn)
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Attribute)
                and _turn_attr(target)
                and not (
                    name not in _CUT_NAMES
                    and isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == target.attr
                )
            }
        for node in ast.walk(fn):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and node is not fn
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Attribute)
                        and _turn_attr(sub)
                        and isinstance(sub.ctx, ast.Store)
                    ):
                        out.add(sub.attr)
    return out


def nonlocals(fn_name: str, fn: ast.AST) -> list[str]:
    """Every ``nonlocal`` in *fn* or a scope nested in it."""
    return [f"{fn_name}:{n.lineno}" for n in ast.walk(fn) if isinstance(n, ast.Nonlocal)]


def bare_uses(fn_name: str, fn: ast.AST, names: set[str]) -> list[str]:
    return [
        f"{fn_name}:{n.lineno}:{n.id}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and n.id in names
    ]


# ── the rules over the real code ──────────────────────────────────────────────


def test_every_store_of_a_shared_name_is_copied_onto_the_turn() -> None:
    functions = list(_functions())
    assert any(name == "_run_chat" for name, _ in functions)
    declared = read_off_the_turn(functions)
    assert "message" in declared, "the recovery requeue reads the turn's message; this went stale"
    assert [loc for name, fn in functions for loc in uncopied_stores(name, fn, declared)] == []


def test_a_name_a_closure_writes_is_never_held_under_its_own_name() -> None:
    functions = list(_functions())
    written = closure_written(functions)
    assert "assistant_text" in written, "the steer cut writes the reply text; this check went stale"
    assert [loc for name, fn in functions for loc in bare_uses(name, fn, written)] == []


def test_a_value_the_turn_never_set_reads_as_the_unbound_local_did() -> None:
    """A phase reading a name the turn has not bound fails as the inline read did."""
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

    turn = _ChatTurn.__new__(_ChatTurn)
    with pytest.raises(UnboundLocalError, match="'assistant_text'"):
        turn.assistant_text
    turn.assistant_text = "partial"
    assert turn.assistant_text == "partial"
    with pytest.raises(AttributeError):
        turn.not_a_turn_name


def test_no_closure_of_the_turn_declares_a_nonlocal() -> None:
    """A closure shares the turn's state through the turn, never through a closure cell
    it rebinds: a phase cut out of ``_run_chat`` would take a copy of the cell's value.
    """
    functions = list(_functions())
    assert [loc for name, fn in functions for loc in nonlocals(name, fn)] == []


# ── each rule sees what it checks ─────────────────────────────────────────────


def _fn(source: str) -> ast.AST:
    return ast.parse(source).body[0]


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("async def p(turn):\n    x = 1\n    turn.x = x\n", []),
        ("async def p(turn):\n    x = 1\n    other()\n", ["p:2:x"]),
        ("async def p(turn):\n    x = turn.x\n    use(x)\n", []),
        ("async def p(turn):\n    for x in y:\n        turn.x = x\n", []),
        ("async def p(turn):\n    for x in y:\n        pass\n", ["p:2:x"]),
        ("async def p(turn):\n    if a:\n        x += 1\n", ["p:3:x"]),
        ("async def p(turn):\n    def inner():\n        x = 1\n", []),
        ("async def p(turn):\n    z = 1\n", []),
        ("async def p(turn):\n    x, z = f()\n    turn.z = z\n    turn.x = x\n", []),
        ("async def p(turn):\n    x, z = f()\n    turn.z = z\n", ["p:2:x"]),
        ("async def p(turn):\n    def x():\n        pass\n    other()\n", ["p:2:x"]),
        ("async def p(turn):\n    def x():\n        pass\n    turn.x = x\n", []),
        ("async def p(turn):\n    import x\n    other()\n", ["p:2:x"]),
        ("async def p(turn):\n    from m import y as x\n    turn.x = x\n", []),
        ("async def p(turn):\n    if (x := f()):\n        pass\n", ["p:2:x"]),
        (
            "async def p(turn):\n    try:\n        pass\n    except E as x:\n        pass\n",
            ["p:4:x"],
        ),
        (
            "async def p(turn):\n    try:\n        pass\n    except E as x:\n        turn.x = x\n",
            [],
        ),
    ],
)
def test_the_copy_rule_sees_every_store_shape(source: str, found: list[str]) -> None:
    assert uncopied_stores("p", _fn(source), {"x"}) == found


def test_the_shared_set_is_what_is_read_off_the_turn() -> None:
    fn = _fn("async def p(turn):\n    use(turn.a)\n    turn.b = 1\n    turn.c += 1\n")
    assert read_off_the_turn([("p", fn)]) == {"a"}


def test_the_nonlocal_rule_sees_one_in_a_nested_scope() -> None:
    fn = _fn("async def p(turn):\n    def cut():\n        nonlocal text\n        text = ''\n")
    assert nonlocals("p", fn) == ["p:3"]
    assert nonlocals("p", _fn("async def p(turn):\n    turn.text = ''\n")) == []


def test_the_closure_rule_reads_an_owner_functions_writes() -> None:
    """A closure hoisted to an owner writes the turn: its names are then never bare. A phase
    cut in line from ``_run_chat`` hands a value it bound on with a copy, which is no write."""
    hoisted = _fn(
        "def _steer_segment_cut(turn):\n    turn.text = ''\n    seen = 1\n    turn.seen = seen\n"
    )
    phase = _fn("def _a_phase(turn):\n    kept = 1\n    turn.kept = kept\n")
    runner = _fn("async def _run_chat(turn):\n    text = 'x'\n    seen = 2\n    kept = 3\n")
    functions = [("_run_chat", runner), ("_steer_segment_cut", hoisted), ("_a_phase", phase)]
    written = closure_written(functions)
    assert written == {"text", "seen"}
    assert sorted(loc for name, fn in functions for loc in bare_uses(name, fn, written)) == [
        "_run_chat:2:text",
        "_run_chat:3:seen",
        "_steer_segment_cut:3:seen",
        "_steer_segment_cut:4:seen",
    ]


def test_an_owner_function_never_rebinds_a_name_it_read_off_the_turn() -> None:
    """A store to the alias of a turn value goes to a stale local unless it is copied back."""
    stale = _fn("def f(turn):\n    x = turn.x\n    x = 2\n    use(x)\n")
    assert uncopied_stores("f", stale, {"x"}) == ["f:3:x"]
    copied = _fn("def f(turn):\n    x = turn.x\n    x = 2\n    turn.x = x\n")
    assert uncopied_stores("f", copied, {"x"}) == []


def test_the_closure_rule_sees_a_bare_use() -> None:
    outer = _fn(
        "async def p(turn):\n" "    def cut():\n" "        turn.text = ''\n" "    text = 'x'\n"
    )
    written = closure_written([("p", outer)])
    assert written == {"text"}
    assert bare_uses("p", outer, written) == ["p:4:text"]


# The functions cut from ``_run_chat``'s closures, by owner. Each takes the turn first: that is
# what the async-path guards key their reach on (``_takes_the_turn`` in
# test_no_config_dir_in_async.py, the inline-resolve scan in test_prompts.py).
_CUT_FROM_RUN_CHAT: dict[str, tuple[str, ...]] = {
    "turn_entry": (
        "_session_stop_generation",
        "_directive_producer_is_channel",
        "_stop_pressed",
    ),
    "turn_context": ("_restore_skills_context_after_compaction",),
    "turn_spec_hooks": ("_fire",),
    "turn_reply_seams": (
        "_persist_partial_reply",
        "_record_abnormal_turn_usage",
        "_attach_abnormal_turn_stats",
        "_flush_text_stream",
        "_flush_thinking_stream",
        "_steer_segment_cut",
        "_emit_stale",
        "_emit_stall",
        "_emit_error",
    ),
    "turn_ledgers": (
        "_report_consumed",
        "_queue_recovery",
        "_replay_owes_delivery",
        "_requeue_owed_completion",
        "_record_terminal_directive",
        "_wt_maybe_cadence",
        "_wt_note_approved",
        "_wt_collect",
    ),
    "slash_commands": ("_refuse_blocked_slash",),
    "turn_admission": (
        "_current_binding",
        "_require_current_binding",
        "_check_owner",
        "_release_dispatch_lock",
    ),
    "model_fallback": (
        "_record_refused_cycle",
        "_refusal_fallback_retry",
    ),
    "acp_recovery": ("_binding_rebuild_allowed",),
}
_CUT_NAMES = frozenset(name for names in _CUT_FROM_RUN_CHAT.values() for name in names)


def test_every_function_cut_from_run_chat_takes_the_turn_first() -> None:
    wrong = []
    for stem, names in _CUT_FROM_RUN_CHAT.items():
        owner = importlib.import_module(f"{chat_turn.__name__}.{stem}")
        for name in names:
            fn = getattr(owner, name, None)
            params = list(inspect.signature(fn).parameters) if fn is not None else []
            if params[:1] != ["turn"]:
                wrong.append(f"{stem}.{name}")
    assert wrong == []


def test_the_async_guards_reach_every_function_cut_from_run_chat() -> None:
    """Each cut function is one the async rule reads, in a file it scans (from the ASTs
    ``_functions`` parses, so no second read of the turn's source)."""
    from test_no_config_dir_in_async import _ASYNC_CHECKED_FILES, _takes_the_turn

    parsed = dict(_functions())
    unreached = []
    for stem, names in _CUT_FROM_RUN_CHAT.items():
        if f"dashboard/chat_turn/{stem}.py" not in _ASYNC_CHECKED_FILES:
            unreached.append(stem)
        for name in names:
            node = parsed.get(name)
            if node is None or not (
                isinstance(node, ast.AsyncFunctionDef) or _takes_the_turn(node)
            ):
                unreached.append(f"{stem}.{name}")
    assert unreached == []


def test_the_config_dir_guard_reads_a_function_that_takes_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_no_config_dir_in_async as guard

    (tmp_path / "platform").mkdir()
    layout = "def release_channel():\n    return data_home()\n"
    (tmp_path / "platform" / "update_layout.py").write_text(layout, encoding="utf-8")
    planted = "def _cut(turn):\n    config_dir()\n\n\ndef _not_cut(slot):\n    config_dir()\n"
    (tmp_path / "planted.py").write_text(planted, encoding="utf-8")
    monkeypatch.setattr(guard, "SRC", tmp_path)
    monkeypatch.setattr(guard, "_ASYNC_CHECKED_FILES", ["planted.py"])
    with pytest.raises(AssertionError) as caught:
        guard.TestNoConfigDirInAsync().test_update_layout_channel_helpers_never_maintain()
    assert "planted.py:2 in async _cut()" in str(caught.value)
    assert "_not_cut" not in str(caught.value)


# ── a phase's writes reach the reads after it ─────────────────────────────────

#: The paths a statement list leaves by other than falling through, each the set of stale names
#: on it (absent: no such path).
_Leaving = dict[str, frozenset[str]]


def _join(a: frozenset[str] | None, b: frozenset[str] | None) -> frozenset[str] | None:
    return b if a is None else a if b is None else a | b


def _leave(leaving: _Leaving, kind: str, stale: frozenset[str] | None) -> None:
    if stale is not None:
        leaving[kind] = leaving.get(kind, frozenset()) | stale


def _bare_reads(nodes: list[ast.AST]) -> Iterator[ast.Name]:
    """The bare reads in *nodes*, not those of a nested function, lambda or class."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def phase_writes(functions: list[tuple[str, ast.AST]]) -> dict[str, frozenset[str]]:
    """What each phase (an async ``_turn_*`` or ``_on_*`` owner function) may leave changed on
    the turn: its own ``turn.x`` stores, copies included, and those of every phase it awaits."""
    phases = {
        name: fn
        for name, fn in functions
        if isinstance(fn, ast.AsyncFunctionDef) and name.startswith(("_turn_", "_on_"))
    }
    own = {
        name: frozenset(
            node.attr
            for block in _own_blocks(fn)
            for stmt in block
            for node in _header_nodes(stmt)
            if _turn_attr(node) and isinstance(node.ctx, ast.Store)
        )
        for name, fn in phases.items()
    }
    calls = {
        name: {
            node.func.id
            for block in _own_blocks(fn)
            for stmt in block
            for node in _header_nodes(stmt)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in phases
        }
        for name, fn in phases.items()
    }
    writes = dict(own)
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            grown = writes[name].union(*(writes[c] for c in callees))
            if grown != writes[name]:
                writes[name], changed = grown, True
    return writes


class StaleReads:
    """Bare reads in one function that can see a value an awaited phase replaced on the turn.

    A may-analysis over the function's own statements. ``[_flow =] await <phase>(turn, ...)``
    makes every name the phase writes on the turn stale; a bare store of the name (the reload
    ``x = turn.x``, or any other binding) makes it fresh. Both arms of an ``if`` join, a loop
    runs to its fixpoint, a ``try``'s handlers and ``finally`` start from every state its body
    passed through (an exception out of a phase leaves before the reload after it), and a
    ``break``, ``continue`` or ``return`` passes through each enclosing ``finally``. A
    ``match`` or ``except*``, which the turn's code does not use, fails the rule until it is
    modelled.
    """

    def __init__(self, writes: dict[str, frozenset[str]]) -> None:
        self.writes = writes
        self.found: set[tuple[int, str]] = set()
        self.calls: list[str] = []

    def run(self, fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
        self.block(fn.body, frozenset(), {})
        return [f"{line}:{name}" for line, name in sorted(self.found)]

    def _phase(self, stmt: ast.stmt) -> str | None:
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.Expr)) else None
        if (
            isinstance(value, ast.Await)
            and isinstance(value.value, ast.Call)
            and isinstance(value.value.func, ast.Name)
            and value.value.func.id in self.writes
        ):
            return value.value.func.id
        return None

    def _read(self, nodes: list[ast.AST], stale: frozenset[str]) -> None:
        self.found |= {(n.lineno, n.id) for n in _bare_reads(nodes) if n.id in stale}

    def block(
        self, stmts: list[ast.stmt], stale: frozenset[str], leaving: _Leaving
    ) -> frozenset[str] | None:
        """Run *stmts*; the stale names where they fall through, or None if no path does."""
        current: frozenset[str] | None = stale
        for stmt in stmts:
            if current is None:
                return None
            _leave(leaving, "raise", current)
            current = self.stmt(stmt, current, leaving)
            _leave(leaving, "raise", current)
        return current

    def stmt(
        self, stmt: ast.stmt, stale: frozenset[str], leaving: _Leaving
    ) -> frozenset[str] | None:
        phase = self._phase(stmt)
        if phase is not None:
            self.calls.append(phase)
            return (stale | self.writes[phase]) - set(_stored(stmt))
        header = _header_nodes(stmt)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return stale - {stmt.name}
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            self._read(header, stale)
            _leave(leaving, type(stmt).__name__.lower(), stale)
            return None
        if isinstance(stmt, ast.If):
            self._read([stmt.test], stale)
            stale -= set(_stored(stmt))
            orelse = self.block(stmt.orelse, stale, leaving) if stmt.orelse else stale
            return _join(self.block(stmt.body, stale, leaving), orelse)
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(stmt, stale, leaving)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            self._read(header, stale)
            bound: set[str] = set()
            for item in stmt.items:
                if item.optional_vars is not None:
                    bound |= {n.id for n in ast.walk(item.optional_vars) if isinstance(n, ast.Name)}
            return self.block(stmt.body, stale - bound - set(_stored(stmt)), leaving)
        if isinstance(stmt, ast.Try):
            return self._try(stmt, stale, leaving)
        if isinstance(stmt, ast.Match) or type(stmt).__name__ == "TryStar":
            raise AssertionError(
                f"line {stmt.lineno}: the reload rule does not model this; extend it"
            )
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            self._read([ast.Name(id=stmt.target.id, ctx=ast.Load(), lineno=stmt.lineno)], stale)
        if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
            return stale  # a bare declaration binds nothing
        self._read(header, stale)
        return stale - set(_stored(stmt))

    def _loop(
        self, stmt: ast.For | ast.AsyncFor | ast.While, stale: frozenset[str], leaving: _Leaving
    ) -> frozenset[str] | None:
        is_for = isinstance(stmt, (ast.For, ast.AsyncFor))
        if is_for:
            self._read([stmt.iter], stale)
        target = (
            {n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name)} if is_for else set()
        )
        entry = stale
        while True:
            if not is_for:
                self._read([stmt.test], entry)
            inner: _Leaving = {}
            body = self.block(stmt.body, entry - target - set(_stored(stmt)), inner)
            grown = entry | (body or frozenset()) | inner.get("continue", frozenset())
            if grown == entry:
                break
            entry = grown
        for kind in ("raise", "return"):
            _leave(leaving, kind, inner.get(kind))
        endless = not is_for and isinstance(stmt.test, ast.Constant) and bool(stmt.test.value)
        done = None if endless else entry - set(_stored(stmt))
        if done is not None and stmt.orelse:
            done = self.block(stmt.orelse, done, leaving)
        return _join(done, inner.get("break"))

    def _try(
        self, stmt: ast.Try, stale: frozenset[str], leaving: _Leaving
    ) -> frozenset[str] | None:
        inner: _Leaving = {}
        body = self.block(stmt.body, stale, inner)
        raised = stale | inner.get("raise", frozenset())
        after: _Leaving = {}
        out: frozenset[str] | None = None
        for handler in stmt.handlers:
            if handler.type is not None:
                self._read([handler.type], raised)
            caught = raised - {handler.name} if handler.name else raised
            out = _join(out, self.block(handler.body, caught, after))
        if body is not None:
            out = _join(out, self.block(stmt.orelse, body, after) if stmt.orelse else body)
        exits: _Leaving = {"raise": raised}  # a handler that does not match lets it through
        for kind, names in inner.items():
            if kind != "raise":  # the body's raises are in ``raised``
                _leave(exits, kind, names)
        for kind, names in after.items():
            _leave(exits, kind, names)
        if not stmt.finalbody:
            for kind, names in exits.items():
                _leave(leaving, kind, names)
            return out
        # The finally runs on every way out; each way leaves the same way after it.
        through = frozenset().union(*exits.values())
        left = self.block(stmt.finalbody, through, leaving)
        for kind in exits:
            _leave(leaving, kind, left)
        return self.block(stmt.finalbody, out, leaving) if out is not None else None


def test_no_read_after_a_phase_sees_a_value_the_phase_replaced() -> None:
    """``_run_chat`` holds a phase's inputs as plain locals; after it awaits a phase, a name the
    phase may have changed on the turn is read only after its reload (``x = turn.x``), on every
    path, an exception's included. The copy rule above is the other half: a store reaches the
    turn at once. Every function that takes the turn is checked, so a phase awaiting a phase is
    held to the same rule."""
    functions = list(_functions())
    writes = phase_writes(functions)
    assert "_turn_open" in writes, "the turn's opening phase moved; this check went stale"
    arms = {
        "_on_tool_call",
        "_on_tool_call_update",
        "_on_tool_result",
        "_on_permission_request",
        "_on_permission_prompt",
    }
    assert arms <= set(writes), sorted(arms - set(writes))
    found, calls = [], []
    for name, fn in functions:
        rule = StaleReads(writes)
        found += [f"{name}:{loc}" for loc in rule.run(fn)]
        calls += rule.calls
    assert set(writes) <= set(calls), sorted(set(writes) - set(calls))
    assert found == []


_PHASE = "async def _turn_p(turn):\n    turn.x = 1\n"


@pytest.mark.parametrize(
    ("body", "found"),
    [
        ("    await _turn_p(turn)\n    use(x)\n", ["3:x"]),
        ("    await _turn_p(turn)\n    x = turn.x\n    use(x)\n", []),
        ("    await _turn_p(turn)\n    use(turn.x)\n", []),
        ("    _flow = await _turn_p(turn)\n    if a:\n        x = turn.x\n    use(x)\n", ["5:x"]),
        ("    if a:\n        await _turn_p(turn)\n    use(x)\n", ["4:x"]),
        ("    for i in y:\n        use(x)\n        await _turn_p(turn)\n", ["3:x"]),
        ("    for i in y:\n        use(x)\n        await _turn_p(turn)\n        x = turn.x\n", []),
        ("    while True:\n        await _turn_p(turn)\n        break\n    use(x)\n", ["5:x"]),
        (
            "    try:\n        await _turn_p(turn)\n        x = turn.x\n"
            "    except E:\n        use(x)\n",
            ["6:x"],
        ),
        (
            "    try:\n        await _turn_p(turn)\n        x = turn.x\n"
            "    except E:\n        x = turn.x\n        use(x)\n",
            [],
        ),
        (
            "    try:\n        await _turn_p(turn)\n        x = turn.x\n"
            "    finally:\n        use(x)\n",
            ["6:x"],
        ),
        (
            "    for i in y:\n        try:\n            await _turn_p(turn)\n            continue\n"
            "        finally:\n            pass\n    use(x)\n",
            ["8:x"],
        ),
        (
            "    for i in y:\n        try:\n            await _turn_p(turn)\n            continue\n"
            "        finally:\n            x = turn.x\n    use(x)\n",
            [],
        ),
        ("    def later():\n        use(x)\n    await _turn_p(turn)\n", []),
        ("    await _turn_p(turn)\n    x: int\n    use(x)\n", ["4:x"]),
        ("    await _turn_p(turn)\n    x += 1\n", ["3:x"]),
        ("    await _turn_q(turn)\n    use(x)\n", ["3:x"]),
    ],
)
def test_the_reload_rule_sees_every_path(body: str, found: list[str]) -> None:
    source = _PHASE + "async def _turn_q(turn):\n    await _turn_p(turn)\n"
    source += "async def _run_chat(turn):\n" + body
    functions = [(fn.name, fn) for fn in ast.parse(source).body]
    writes = phase_writes(functions)
    assert writes["_turn_q"] == {"x"}
    rule = StaleReads(writes)
    offset = source.count("\n") - body.count("\n") - 1
    assert [f"{int(loc.split(':')[0]) - offset}:x" for loc in rule.run(functions[-1][1])] == found
