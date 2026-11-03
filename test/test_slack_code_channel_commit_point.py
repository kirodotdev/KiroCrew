"""Every code-channel side effect after an await goes through the Stop commit point.

A Stop can land in any await. So in the handler modules, every side effect
that follows an await (a raw Slack send, a store or record write, a status
hold, a change to module state) needs ``surface.commit_if_not_stopped(...)``
before it, with no await between them. The start of an async function counts
as following an await, since its caller awaited to get there. Slack calls made
through ``SurfaceClient`` are covered by ``SurfaceClient._call``, which this
test also pins.

The allow-list holds only two kinds of write, each with its reason: undoing this
call's own earlier write, and recording a Slack write that already landed
(skipping it would lose track of what Slack shows).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src/kiro_crew/slack/code_channels"
MODULES = ("tools.py", "wrapup.py", "commands.py", "workspace/service.py")
COMMIT = {"commit_if_not_stopped", "_commit"}  # tools._commit raises on a refusal

#: Calls that change channel state or Slack: store and record writes, the status
#: hold, the wrap-up bookkeeping, and the raw Slack client's sends.
WRITES = {
    "surface_update",
    "update_async",
    "retire_async",
    "update_binding",
    "replace_binding",
    "put_binding",
    "hold_after_turn",
    "_mark_wrapped",
    "_mark_archived",
    "_stamp_archived",
    "_forget_request",
    "_finish_archive",
    "post_blocks",
    "post_message",
    "update_message",
    "post_ephemeral",
    "chat_update",
    "chat_postMessage",
    # the workspace service's record writes and session pointing
    "put",
    "_set_project",
    "_point_once",
}

_SETUP = (
    "workspace setup: records a worktree, session or folder that already exists, "
    "so a Stop never orphans it; nothing reaches Slack, and the next turn's "
    "ensure would do the same"
)
_PUBLISHED = (
    "records what a publish did: a Stop refuses the publish itself "
    "(SurfaceClient._call), which then changes neither hash nor slack_off"
)
MUTATORS = {"pop", "add", "discard", "clear", "update", "setdefault", "append"}

#: (module, function, written name): why the write needs no commit check.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("tools.py", "_create_canvas", "_unsaved_canvas"): (
        "records a canvas Slack already created, so the next call stores it "
        "instead of making a second one"
    ),
    ("tools.py", "_publish_canvas_locked", "_unsaved_canvas"): (
        "records a canvas Slack already created (see _create_canvas)"
    ),
    ("tools.py", "_publish_canvas_locked", "plan_canvas_id"): (
        "stores the id of a canvas Slack already created; dropping it would make "
        "the next publish create or refuse a second canvas"
    ),
    ("tools.py", "_publish_canvas_locked", "plan_view_id"): (
        "stores the id of a Plan tab Slack already attached"
    ),
    ("tools.py", "_drop_create_intent", "_maybe_created"): (
        "undoes this call's own create intent after Slack refused the create"
    ),
    ("tools.py", "_drop_create_intent", "canvas_create_pending_since"): (
        "undoes this call's own create intent after Slack refused the create"
    ),
    ("tools.py", "_publish_canvas_locked", "_maybe_created"): (
        "clears the create intent once the landed canvas's id is stored"
    ),
    ("tools.py", "_publish_plan_canvas", "_canvas_locks"): (
        "takes the channel's publish lock (an asyncio.Lock), not channel state"
    ),
    ("tools.py", "_set_context_bar", "_bar_locks"): (
        "takes the channel's bar lock (an asyncio.Lock), not channel state"
    ),
    ("tools.py", "_rollback_bar", "context_bar"): (
        "undoes this call's own stored bar items when their publish failed or " "was stopped"
    ),
    ("workspace/service.py", "_set", "_set_project"): _SETUP,
    ("workspace/service.py", "ensure", "_point_once"): _SETUP,
    ("workspace/service.py", "ensure", "put"): _PUBLISHED,
    ("workspace/service.py", "_bind_and_store", "put"): _SETUP,
    ("workspace/service.py", "_bind_and_store", "_point_once"): _SETUP,
    ("workspace/service.py", "_point_once", "put"): _SETUP,
    ("workspace/service.py", "_point_session", "_set_project"): _SETUP,
    ("workspace/service.py", "plan_session", "put"): _SETUP,
    ("workspace/service.py", "_recover_folder", "put"): _SETUP,
    ("workspace/service.py", "republish_bar", "put"): _PUBLISHED,
    ("workspace/service.py", "_refresh", "put"): _PUBLISHED,
    ("commands.py", "_gh_signed_in", "_gh_cache"): (
        "caches a gh sign-in probe for the process; not channel state"
    ),
    ("wrapup.py", "_post_wrap_up", "_forget_request"): (
        "the wrap-up message is already up: it settles the request it answers"
    ),
    ("wrapup.py", "_post_wrap_up", "update_async"): (
        "stores the ts of a wrap-up Slack already posted; without it its live "
        "Archive button answers 'out of date' forever"
    ),
    ("wrapup.py", "_post_wrap_up", "_mark_wrapped"): (
        "stamps a wrap-up Slack already posted (with its ts above)"
    ),
}

#: Functions never run as admitted work: a person's own click or command, or a
#: turn hook. The commit point lets their writes through unconditionally
#: (``commit_if_not_stopped`` is True outside an admission), so a check there
#: would be a no-op.
UNADMITTED: dict[tuple[str, str], str] = {
    ("wrapup.py", "request_wrap_up"): "a person's /wrap-up",
    ("wrapup.py", "cancel_wrap_up"): "settles a person's /wrap-up request",
    ("wrapup.py", "on_turn_start"): "turn hook",
    ("wrapup.py", "on_turn_end"): "turn hook; its post goes through post_wrap_up",
    ("wrapup.py", "handle_archive_click"): "a person's Archive click",
    ("wrapup.py", "_recover_archive"): "background retry of a person's Archive click",
    ("wrapup.py", "_finish_archive"): "completes a person's Archive click",
    ("wrapup.py", "_stamp_archived"): "the Archive click's own stamp",
    ("commands.py", "handle_slash"): "a person's slash command",
}


def _pos(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _end(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "end_lineno", 0), getattr(node, "end_col_offset", 0))


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _module_state(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and isinstance(node.value, (ast.Dict, ast.Set, ast.Call)):
                names.add(t.id)
    return names


def _writes(fn: ast.AST, state: set[str]) -> list[tuple[ast.AST, str]]:
    """``(node, written name)`` for every side effect in *fn* (not nested defs)."""
    out: list[tuple[ast.AST, str]] = []
    for node in _own_nodes(fn):
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name in WRITES:
                fields = [k.arg for k in node.keywords if k.arg]
                out.append((node, fields[0] if name == "surface_update" and fields else name))
            elif name == "to_thread" and node.args:
                inner = node.args[0]
                inner_name = (
                    inner.attr if isinstance(inner, ast.Attribute) else getattr(inner, "id", "")
                )
                if inner_name in WRITES:
                    out.append((node, inner_name))
            elif (
                name in MUTATORS
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in state
            ):
                out.append((node, node.func.value.id))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and t.value.id in state
                ):
                    out.append((node, t.value.id))
    # A surface_update writing a plan id is named by that field (see ALLOWED).
    return out


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        nodes.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return nodes


def _loops_holding(fn: ast.AST, node: ast.AST) -> list[ast.AST]:
    p = _pos(node)
    return [
        n
        for n in _own_nodes(fn)
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)) and _pos(n) <= p <= _end(n)
    ]


def _suspends(n: ast.AST) -> bool:
    return isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))


def _parents(fn: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}


def _block_spans(node: ast.AST, parents: dict[ast.AST, ast.AST], fn: ast.AST) -> list[tuple]:
    """The statement lists *node* sits in (an ``if``'s body, a loop's body, ...).

    A check counts only for writes in the same lists: a check in one branch
    proves nothing about a write after the branch.
    """
    spans: list[tuple] = []
    child, parent = node, parents.get(node)
    while parent is not None and child is not fn:
        for field in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and child in block and parent is not fn:
                spans.append((_pos(block[0]), _end(block[-1])))
        child, parent = parent, parents.get(parent)
    return spans


def _counts(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """A check whose answer is used: ``_commit`` raises; the other must be tested."""
    return _callee(call) == "_commit" or not isinstance(parents.get(call), ast.Expr)


def _unchecked(fn: ast.AST, state: set[str]) -> list[tuple[int, str]]:
    """Writes in async *fn* with no commit check since the last await."""
    own = _own_nodes(fn)
    parents = _parents(fn)
    commits = [
        n for n in own if isinstance(n, ast.Call) and _callee(n) in COMMIT and _counts(n, parents)
    ]
    bad: list[tuple[int, str]] = []
    for node, name in _writes(fn, state):
        p = _pos(node)

        def between(a: ast.AST, start: tuple[int, int]) -> bool:
            # A suspension after the check and before the write. The write's own
            # await (``await surface_update(...)``) wraps the write and does not
            # count; an ``async with`` or ``async for`` entered after the check
            # does, even with the write inside it (its lock wait comes first).
            if not _suspends(a) or not start < _pos(a) < p:
                return False
            return not (isinstance(a, ast.Await) and _pos(a) <= p <= _end(a))

        reaching = [
            c
            for c in commits
            if _pos(c) < p and all(lo <= p <= hi for lo, hi in _block_spans(c, parents, fn))
        ]
        last = max((_pos(c) for c in reaching), default=None)
        ok = last is not None and not any(between(a, last) for a in own)
        if ok:
            # Inside a loop, an await later in the loop body runs before the
            # next pass's write.
            for loop in _loops_holding(fn, node):
                if not (last >= _pos(loop)) or any(
                    _suspends(a) and _pos(a) > p and _end(a) <= _end(loop) for a in _own_nodes(loop)
                ):
                    ok = False
        if not ok:
            bad.append((p[0], name))
    return bad


def _scan(source: str, module: str) -> list[str]:
    tree = ast.parse(source)
    state = _module_state(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if (module, fn.name) in UNADMITTED:
            continue
        for line, name in _unchecked(fn, state):
            if (module, fn.name, name) not in ALLOWED:
                found.append(f"{module}:{line} {fn.name} writes {name} with no commit check")
    return found


def test_every_post_await_write_goes_through_the_commit_point() -> None:
    found: list[str] = []
    for module in MODULES:
        found += _scan((ROOT / module).read_text(encoding="utf-8"), module)
    assert found == [], "\n".join(found)


def test_every_allow_list_entry_still_names_a_real_write() -> None:
    """A stale entry would let a new write of that name through unexamined."""
    live: set[tuple[str, str, str]] = set()
    functions: set[tuple[str, str]] = set()
    for module in MODULES:
        tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
        state = _module_state(tree)
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                functions.add((module, fn.name))
                live |= {(module, fn.name, name) for _, name in _writes(fn, state)}
    assert set(ALLOWED) <= live, set(ALLOWED) - live
    assert set(UNADMITTED) <= functions, set(UNADMITTED) - functions


def test_the_scan_catches_a_planted_bypass() -> None:
    planted = """
_held: dict[str, str] = {}

async def handler(call):
    await call.client.set_status("C1", "suspended")
    hold_after_turn("C1", "suspended")
    _held["C1"] = "x"
    surface.commit_if_not_stopped("C1")
    await asyncio.sleep(0)
    await surface_update("C1", context_bar=())

async def guarded(call):
    await call.client.set_status("C1", "suspended")
    if not surface.commit_if_not_stopped("C1"):
        return
    hold_after_turn("C1", "suspended")

async def looped(call):
    if not surface.commit_if_not_stopped("C1"):
        return
    for _ in range(2):
        _held["C1"] = "y"
        await asyncio.sleep(0)

async def lock_wait(call):
    _commit("C1")
    async with _lock("C1"):
        hold_after_turn("C1", "suspended")

async def discarded(call):
    surface.commit_if_not_stopped("C1")
    hold_after_turn("C1", "suspended")

async def branch(call, flag):
    if flag:
        _commit("C1")
    hold_after_turn("C1", "suspended")

async def tested_in_branch(call, flag):
    if flag:
        _commit("C1")
        hold_after_turn("C1", "suspended")
    if flag and surface.commit_if_not_stopped("C1"):
        await surface_update("C1", commands_digest="d")
"""
    found = sorted(_scan(planted, "planted.py"), key=lambda s: int(s.split()[0].split(":")[1]))
    assert found == [
        "planted.py:6 handler writes hold_after_turn with no commit check",
        "planted.py:7 handler writes _held with no commit check",
        "planted.py:10 handler writes context_bar with no commit check",
        "planted.py:22 looped writes _held with no commit check",
        "planted.py:28 lock_wait writes hold_after_turn with no commit check",
        "planted.py:32 discarded writes hold_after_turn with no commit check",
        "planted.py:37 branch writes hold_after_turn with no commit check",
    ], found


def test_every_surface_client_send_passes_the_commit_point() -> None:
    """``SurfaceClient._call`` checks right before its send, with no await between."""
    tree = ast.parse((ROOT / "surface_client.py").read_text(encoding="utf-8"))
    call = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call"
    )
    own = _own_nodes(call)
    send = max(
        (n for n in own if isinstance(n, ast.Await)), key=_pos
    )  # ``return await super()._call(...)``
    check = max(
        (_pos(n) for n in own if isinstance(n, ast.Call) and _callee(n) in COMMIT),
        default=None,
    )
    assert check is not None and check < _pos(send)
    assert not any(
        isinstance(a, ast.Await) and check < _pos(a) < _pos(send) for a in own
    ), "an await sits between the commit check and the send"
