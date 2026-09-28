"""One serving loop, resolved through one accessor.

The dashboard latched "the loop to hand cross-thread work to" in four separate
places under four names: a field for the coalesced slots broadcast, a second for
off-loop websocket sends, a third inside the ring-log handler, and a startup
closure in ``server.py`` that marshalled the skill-staging hooks. They held the
same loop, so nothing was broken -- but they are four answers to one question,
they can be updated independently, and a caller that finds ITS copy unset drops
the work silently rather than raising.

These tests pin the collapsed shape: ``DashboardState.serving_loop`` is the single
resolver, and the three ratchets at the bottom fail if a second latched copy
reappears -- as an instance attribute, as a loop held across a closure, or as a
second writer of the field that would clobber the startup bind.
"""
from __future__ import annotations

import ast
import asyncio
import collections
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import state as state_mod
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


@pytest.mark.asyncio
async def test_bind_wins_over_lazy_latching(tmp_path) -> None:
    """A bound loop is authoritative: the accessor never re-derives one."""
    state = _make_state(tmp_path)
    sentinel = MagicMock(name="loop-bound-at-startup")
    state.bind_serving_loop(sentinel)

    # Read from a real running loop; the bound value must still win, otherwise
    # startup's authoritative answer could be overwritten by whoever reads first.
    assert state.serving_loop is sentinel


@pytest.mark.asyncio
async def test_lazy_latch_when_nothing_bound(tmp_path) -> None:
    """A state whose startup never ran still resolves a loop when read from one."""
    state = _make_state(tmp_path)
    state._serving_loop = None

    assert state.serving_loop is asyncio.get_running_loop()
    # And it sticks, so a later off-loop caller has a target.
    assert state._serving_loop is asyncio.get_running_loop()


def test_unknowable_loop_is_none_not_a_guess(tmp_path) -> None:
    """Off the loop with nothing bound, the accessor admits it does not know."""
    state = _make_state(tmp_path)
    state._serving_loop = None

    assert state.serving_loop is None


@pytest.mark.asyncio
async def test_ws_hop_and_slots_coalescer_share_one_loop(tmp_path) -> None:
    """The two in-state consumers resolve the SAME object, not two copies."""
    state = _make_state(tmp_path)
    state._serving_loop = None
    ws = MagicMock(closed=False)
    ws.send_str = AsyncMock()
    ws.get = MagicMock(return_value=True)

    state.register_ws(ws)  # on the loop
    latched = state._serving_loop
    assert latched is asyncio.get_running_loop()

    # The slots coalescer must read that same field rather than latch its own.
    state.push_slots_update()
    assert state._serving_loop is latched


@pytest.mark.asyncio
async def test_log_handler_uses_the_states_loop(tmp_path) -> None:
    """The ring-log handler keeps no loop; it asks the state at emit time.

    emit() runs on arbitrary threads, so this is the surface most likely to
    re-grow its own copy.
    """
    ring: collections.deque[str] = collections.deque(maxlen=4)
    handler = updates._RingLogHandler(ring)
    handler.setFormatter(logging.Formatter("%(message)s"))
    ws = MagicMock()
    ws.send_str = AsyncMock()
    state = MagicMock()
    state._ws_log_subscribers = {ws}
    state.serving_loop = MagicMock()
    handler.set_state(state)

    handler.emit(
        logging.LogRecord("kiro_crew", logging.INFO, __file__, 1, "hello", None, None)
    )

    assert state.serving_loop.call_soon_threadsafe.called, (
        "the handler did not route its fan-out through the state's serving loop"
    )


@pytest.mark.asyncio
async def test_register_and_send_do_not_clobber_the_startup_bind(tmp_path) -> None:
    """The two on-loop consumers must not overwrite the authoritative bind.

    Both ``register_ws`` and the on-loop branch of ``_spawn_ws_send`` run while a
    loop IS current, so a direct ``self._serving_loop = loop`` there would silently
    replace whatever startup bound -- making "bind wins" true only until the first
    websocket connects. Reading the accessor instead latches only when nothing is
    bound, which is what keeps the invariant a property of the code rather than a
    claim in a docstring.
    """
    state = _make_state(tmp_path)
    sentinel = MagicMock(name="loop-bound-at-startup")
    state.bind_serving_loop(sentinel)
    ws = MagicMock(closed=False)
    ws.send_str = AsyncMock()
    ws.get = MagicMock(return_value=True)

    state.register_ws(ws)
    assert state.serving_loop is sentinel, "register_ws clobbered the startup bind"

    state._spawn_ws_send(ws, "{}")
    assert state.serving_loop is sentinel, "_spawn_ws_send clobbered the startup bind"

    pending = list(state._background_tasks)
    if pending:
        await asyncio.gather(*pending)


def test_only_bind_and_the_accessor_write_the_field() -> None:
    """Ratchet: "bind wins" must be structural, not merely asserted.

    Any other writer of ``_serving_loop`` overwrites the loop bound at startup, so
    the accessor's promise would hold only until whichever call site got there
    first. Exactly two writers are permitted: ``bind_serving_loop``, the
    authoritative one, and the lazy latch inside the ``serving_loop`` property.
    A third reintroduces the many-writers shape this change removed -- a reader
    that wants the loop recorded should READ the accessor, which latches for it.
    """
    tree = ast.parse(Path(state_mod.__file__).read_text(encoding="utf-8"))
    allowed = {"bind_serving_loop", "serving_loop"}
    offenders: list[str] = []

    def scan(node: ast.AST, fn: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else fn
            )
            if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_serving_loop"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and inner not in allowed
                    ):
                        offenders.append(f"{inner or '<module>'}:{child.lineno}")
            scan(child, inner)

    scan(tree, None)
    assert not offenders, (
        "something other than bind_serving_loop and the accessor writes "
        f"_serving_loop: {offenders} -- read DashboardState.serving_loop instead, "
        "which latches without overwriting a loop bound at startup"
    )


def test_only_one_latched_serving_loop_field_remains() -> None:
    """Ratchet: a second latched copy of the loop must not reappear.

    Matches assignments of a running loop into an instance attribute across the
    two modules that had them. `_serving_loop` is the one permitted sink; anything
    else means the duplication this collapsed has grown back, which is invisible
    until two copies disagree at runtime.

    The match must END at the loop call: ``self._x = get_running_loop()`` stores
    the loop, whereas ``self._timer = get_running_loop().call_later(...)`` stores
    a timer handle and is not a second copy of the loop.
    """
    pattern = re.compile(
        r"self\.(_[A-Za-z0-9_]+)\s*=\s*(?:self\._running_loop\(\)"
        r"|asyncio\.get_running_loop\(\)"
        r"|asyncio\.get_event_loop\(\))\s*(?:#.*)?$",
        re.MULTILINE,
    )
    offenders: dict[str, set[str]] = {}
    for mod in (state_mod, updates):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        names = {m.group(1) for m in pattern.finditer(src)} - {"_serving_loop"}
        if names:
            offenders[Path(mod.__file__).name] = names

    assert not offenders, (
        "a second latched serving-loop field reappeared: "
        f"{offenders} -- route it through DashboardState.serving_loop instead"
    )


def test_no_latched_loop_held_across_a_closure() -> None:
    """Ratchet: a loop latched into a LOCAL and held for later must not reappear.

    The instance-attribute ratchet above cannot see this shape, which is exactly
    how one copy survived the first pass: a startup closure captured the loop in a
    local, then marshalled onto it from hook callbacks that fire much later.

    The tell is the annotation. A transient local reads
    ``loop = asyncio.get_running_loop()`` and is used immediately; a copy meant to
    outlive its statement is annotated Optional so the ``except RuntimeError``
    branch can set it to None. Matching only the annotated form keeps ordinary
    transient locals free while pinning the smell.

    That makes this ratchet deliberately incomplete: an UNANNOTATED captured local
    slips past it. It is kept anyway because neither other ratchet can see a local
    at all -- one matches ``self.<attr> =``, the other only writes of
    ``_serving_loop`` -- so dropping this one would leave the exact shape that
    escaped the first pass with no guard. Widening it to every
    ``get_running_loop()`` assignment would flag dozens of legitimate transient
    locals and get muted, which is worse than a narrow guard that fires rarely.
    """
    annotated = re.compile(
        r"[A-Za-z_][A-Za-z0-9_]*\s*:\s*\"?asyncio\.AbstractEventLoop\s*\|\s*None\"?\s*"
        r"=\s*asyncio\.get_running_loop\(\)"
    )
    from kiro_crew.dashboard import server as server_mod

    offenders: dict[str, list[str]] = {}
    for mod in (server_mod, state_mod, updates):
        name = Path(mod.__file__).name
        hits = [
            line.strip()
            for line in Path(mod.__file__).read_text(encoding="utf-8").splitlines()
            if annotated.search(line)
        ]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "a loop was latched into a local and held for later use: "
        f"{offenders} -- read DashboardState.serving_loop at dispatch time instead"
    )
