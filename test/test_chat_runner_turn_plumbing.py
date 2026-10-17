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
* reading a value the turn never set raises ``UnboundLocalError``.

Each source rule has a self-test on synthetic source, so a rule that stopped seeing anything
fails here instead of passing.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator

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
    """Names a nested function writes on the turn."""
    out: set[str] = set()
    for _, fn in functions:
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


def test_the_closure_rule_sees_a_bare_use() -> None:
    outer = _fn(
        "async def p(turn):\n" "    def cut():\n" "        turn.text = ''\n" "    text = 'x'\n"
    )
    written = closure_written([("p", outer)])
    assert written == {"text"}
    assert bare_uses("p", outer, written) == ["p:4:text"]
