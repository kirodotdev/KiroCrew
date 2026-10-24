"""Every opener of a named slot key waits for a pending create.

The source scan that finds each named open and checks the wait dominates
it, and ``open_named_slot``, the helper that owns the wait then open.
The shared setup is in ``slot_create_publish_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import slot_create_publish_helpers
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from slot_create_publish_helpers import WAIT, _pending

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import slot_create_transaction as txn

#: The shared fixture, bound here so pytest finds it for this module's tests.
state = slot_create_publish_helpers.state


#: Openers that wait for a pending create inside, so calling one counts as waiting.
#: Each is checked to call ``wait_for_pending_create`` (or another entry) itself.
_WAITING_OPENERS = {
    "rehydrate_slot_from_history_async": "wait_for_pending_create",
    "resume_slot_from_history": "wait_for_pending_create",
    "ensure_cron_slot": "wait_for_pending_create",
    "_restore_worker_transcript": "rehydrate_slot_from_history_async",
    "open_named_slot": "wait_for_pending_create",
}

#: Calls that open a named key: a key a pending create holds makes each refuse
#: with ValueError ("still being built").
_OPENING_CALLS = ("get_or_create_slot", "_rehydrate_slot_from_history", "_bind_cron_slot")

#: Single opens past a suspension that no create can meet, keyed by the function
#: and the key expression the open names, each with the reason.
_OPENS_NO_CREATE_CAN_HOLD = {
    ("kiro_crew/dashboard/openai_compat.py:api_completions", "slot_name"): (
        "the ephemeral branch's key is oai-<uuid4>, minted on this request"
    ),
    ("kiro_crew/dashboard/chat_api/resume.py:resume_slot_from_history", "state._slots[slot.key]"): (
        "publishes the slot this resume built under its own construction mark, "
        "which a create's mint refuses (DashboardState.prepare_slot)"
    ),
}

#: Opens a wait reaches on every path although no wait dominates them, keyed like
#: ``_OPENS_NO_CREATE_CAN_HOLD``. The scan judges dominance by statement lists,
#: so a branch reached only after a wait in an earlier branch is named here.
_OPENS_WAITED_ON_EVERY_PATH = {
    ("kiro_crew/apps/builtins/spec_builder/backend/runtime.py:_ensure_worker_slot", "slot_key"): (
        "the else branch runs only when the `existing is None` branch above ran the waiting "
        "_restore_worker_transcript, with no suspension after it"
    ),
}

#: A close's put-back of its own slot after a failed archive, keyed like
#: ``_OPENS_NO_CREATE_CAN_HOLD``. A create or an opener that minted the key between
#: the close's pop and this write loses its slot to it. That race spans every
#: opener and every register-then-pop site; the session spec tracks it as a
#: follow-up.
_CLOSE_PUT_BACKS = {
    (
        "kiro_crew/apps/builtins/spec_builder/backend/orchestration/execution_state.py"
        ":_remove_orphaned_executions_with_service",
        "state._slots[slot_key]",
    ),
    (
        "kiro_crew/apps/builtins/spec_builder/backend/runtime.py:_teardown_worker_slot",
        "state._slots[slot_key]",
    ),
    ("kiro_crew/dashboard/chat_api/slot_lifecycle.py:_close_slot", "state._slots[name]"),
    ("kiro_crew/dashboard/chat_api/slot_lifecycle.py:api_chat_slots_cleanup", "state._slots[name]"),
}

#: Openers of a named key that neither wait nor sit behind one that does, each
#: with the reason it cannot meet a pending create's key, or what it does instead.
_OPENERS_THAT_DO_NOT_WAIT = {
    "kiro_crew/dashboard/chat_persistence_parts/slot_builders.py:_apply_restored_open_slot": (
        "boot restore, awaited in on_startup before the dashboard serves a create"
    ),
    "kiro_crew/dashboard/chat_persistence_parts/slot_builders.py:_apply_recent_session": (
        "boot restore, awaited in on_startup before the dashboard serves a create"
    ),
    "kiro_crew/dashboard/handlers/taskrunner.py:_open_result_slot": (
        "the key carries a fresh uuid4 token minted on this call"
    ),
    "kiro_crew/dashboard/slot_registry.py:put_slot": (
        "the registry's own write, reached only through the state's publishers"
    ),
    "kiro_crew/dashboard/session_transfer_install.py:_install_arrived_bundle": (
        "import mints its key and holds it under its own construction mark"
    ),
}


#: Statements whose body may run zero times, or stop part way: a wait in it
#: does not dominate what follows the statement.
_BRANCHING = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.ExceptHandler,
    ast.Match,
    ast.match_case,
)


class _OpenSites:
    """Every place in ``kiro_crew`` that opens a named key, with what precedes it.

    An open is a call in ``_OPENING_CALLS`` that names a key, or a direct
    ``<x>._slots[<k>] = ...`` write. For each one this records, in its own
    function body and before it in source order, whether the function waited
    (``wait_for_pending_create`` or a waiting opener), and whether the open sits
    in a ``try`` whose handler catches ValueError (the named fallback).
    """

    def __init__(self) -> None:
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).resolve().parent
        self.waiting = {"wait_for_pending_create", *_WAITING_OPENERS}
        #: ``rel:function`` -> its open sites, as ``(what, line, satisfied)``.
        self.sites: dict[str, list[tuple[str, int, bool]]] = {}
        #: called name -> every ``(rel:function, line, satisfied, call)`` calling it.
        self.calls: dict[str, list[tuple[str, int, bool, Any]]] = {}
        #: ``rel:function`` -> every name it calls.
        self.calls_in: dict[str, set[str]] = {}
        #: The ``_OPENS_NO_CREATE_CAN_HOLD`` entries met, so a stale one fails.
        self.exempt_seen: set[tuple[str, str]] = set()
        needles = (*_OPENING_CALLS, "._slots[", *_WAITING_OPENERS)
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root.parent).as_posix()
            if "/tests/" in rel:
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                self._scan(rel, ast.parse(text))

    @staticmethod
    def _catches_value_error(handler: Any) -> bool:
        import ast

        kind = handler.type
        names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
        return any(isinstance(n, ast.Name) and n.id == "ValueError" for n in names)

    @staticmethod
    def _call_name(call: Any) -> str:
        import ast

        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        return func.id if isinstance(func, ast.Name) else ""

    @staticmethod
    def _checks_construction_mark(test: Any) -> bool:
        """Whether an ``if`` test reads ``_slots_under_construction``: the synchronous fallback."""
        import ast

        return any(
            (isinstance(n, ast.Attribute) and n.attr == "_slots_under_construction")
            or (isinstance(n, ast.Constant) and n.value == "_slots_under_construction")
            for n in ast.walk(test)
        )

    def _scan(self, rel: str, tree: Any) -> None:
        import ast

        def walk_function(fn: Any) -> None:
            owner = f"{rel}:{fn.name}"
            self.sites.setdefault(owner, [])
            self.calls_in.setdefault(owner, set())
            #: Each wait, as its line and the chain of statement lists it sits in.
            waits: list[tuple[int, tuple[int, ...]]] = []
            #: Every other suspension point: a create can start during any of them.
            suspends: list[int] = []
            found: list[tuple[str, int, bool, Any, tuple[int, ...]]] = []
            counter = [0]

            def inner(blocks: tuple[int, ...]) -> tuple[int, ...]:
                """A new statement list nested in *blocks*: it may run or not."""
                counter[0] += 1
                return (*blocks, counter[0])

            def visit(node: Any, guarded: bool, blocks: tuple[int, ...]) -> None:
                for child in ast.iter_child_nodes(node):
                    visit_one(child, guarded, blocks)

            def visit_one(child: Any, guarded: bool, blocks: tuple[int, ...]) -> None:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk_function(child)
                    return
                # A ``try`` or ``if`` at any depth, nested in another block too.
                if isinstance(child, ast.Try):
                    catches = any(self._catches_value_error(h) for h in child.handlers)
                    body = inner(blocks)
                    for stmt in child.body:
                        visit_one(stmt, guarded or catches, body)
                    for handler in child.handlers:
                        visit_one(handler, guarded, blocks)
                    for part in (child.orelse, child.finalbody):
                        branch = inner(blocks)
                        for stmt in part:
                            visit_one(stmt, guarded, branch)
                    return
                if isinstance(child, ast.If):
                    # The fallback counts only when a marked key leaves the function.
                    if (
                        self._checks_construction_mark(child.test)
                        and child.body
                        and isinstance(child.body[-1], (ast.Return, ast.Raise))
                    ):
                        waits.append((child.lineno, blocks))
                    visit_one(child.test, guarded, blocks)
                    for part in (child.body, child.orelse):
                        branch = inner(blocks)
                        for stmt in part:
                            visit_one(stmt, guarded, branch)
                    return
                if isinstance(child, (ast.AsyncWith, ast.AsyncFor)) or (
                    isinstance(child, ast.Await)
                    and not (
                        isinstance(child.value, ast.Call)
                        and self._call_name(child.value) in self.waiting
                    )
                ):
                    suspends.append(child.lineno)
                if isinstance(child, ast.Call):
                    name = self._call_name(child)
                    self.calls_in[owner].add(name)
                    if name in self.waiting:
                        waits.append((child.lineno, blocks))
                    found.append((name, child.lineno, guarded, child, blocks))
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and target.value.attr == "_slots"
                        ):
                            found.append(("_slots[]=", child.lineno, guarded, target, blocks))
                visit(child, guarded, inner(blocks) if isinstance(child, _BRANCHING) else blocks)

            visit(fn, False, ())
            for name, line, guarded, node, blocks in found:
                call = None if name == "_slots[]=" else node
                # A wait counts only when it dominates the open (it sits in the
                # open's statement list or one enclosing it) and nothing suspends
                # between them: a create that starts during that await holds the
                # key again.
                ok = guarded or any(
                    w < line
                    and blocks[: len(chain)] == chain
                    and not any(w < a < line for a in suspends)
                    for w, chain in waits
                )
                if not ok and (name == "_slots[]=" or name in _OPENING_CALLS):
                    key = (owner, ast.unparse(node if name == "_slots[]=" else _key_arg(node)))
                    if (
                        key in _OPENS_NO_CREATE_CAN_HOLD
                        or key in _OPENS_WAITED_ON_EVERY_PATH
                        or key in _CLOSE_PUT_BACKS
                    ):
                        self.exempt_seen.add(key)
                        ok = True
                if name == "_slots[]=" or (
                    name in _OPENING_CALLS and (name != "get_or_create_slot" or _names_a_key(call))
                ):
                    self.sites[owner].append((name, line, ok))
                if call is not None:
                    self.calls.setdefault(name, []).append((owner, line, ok, call))

        def outer(node: Any) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk_function(child)
                elif not isinstance(child, ast.expr):
                    outer(child)

        outer(tree)


def _key_arg(call: Any) -> Any:
    """The key an opening call names: its first argument or its ``name=``."""
    import ast

    arg = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "name":
            arg = keyword.value
    return arg if arg is not None else ast.Constant(None)


def _names_a_key(call: Any) -> bool:
    import ast

    arg = _key_arg(call)
    return not (isinstance(arg, ast.Constant) and arg.value is None)


def _passes_no_name(call: Any) -> bool:
    """``name=None``: the call mints a fresh key, which no create can hold."""
    import ast

    return any(
        keyword.arg == "name"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is None
        for keyword in call.keywords
    )


@pytest.mark.asyncio
async def test_a_member_thread_open_waits_for_a_create_begun_after_its_rehydrate(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create of the member key that begins after the rehydrate's wait is waited on.

    The create starts in the window between ``rehydrate_slot_from_history_async``
    (which waits at its top) and the open, where the workspace resolution
    awaits. The open waits for it right before the open, and once the create
    gives the key up, mints the member's slot: 200, never the bare "still being
    built" ValueError as a 500.

    Mutation pin: drop the wait before the open (or put an await back between
    it and the open) and this answers 500; the opener scan fails too.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.handlers import members

    # No private memory: the open under test is the slot's, not the store's.
    cfg = KiroCrewConfig()
    cfg.agents = {"code-reviewer": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "code-reviewer"
    cfg.save()
    monkeypatch.setattr(members, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    real_rehydrate = members.rehydrate_slot_from_history_async
    real_wait = txn.wait_for_pending_create
    held: dict[str, Any] = {}

    async def rehydrate_then_create(st: Any, key: str, **kwargs: Any) -> Any:
        restored = await real_rehydrate(st, key, **kwargs)
        assert restored is None
        held["create"] = _pending(st, key, agent="code-reviewer", mode=members_mod.DM_SLOT_MODE)
        return None

    async def observed_wait(st: Any, key: str) -> None:
        create = held.get("create")
        if create is None or held.get("waited"):
            return await real_wait(st, key)
        held["waited"] = key
        waiting = asyncio.ensure_future(real_wait(st, key))
        await asyncio.sleep(0)
        assert not waiting.done()
        await create.settle()
        await asyncio.wait_for(asyncio.shield(waiting), WAIT)

    monkeypatch.setattr(members, "rehydrate_slot_from_history_async", rehydrate_then_create)
    monkeypatch.setattr(txn, "wait_for_pending_create", observed_wait)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/members/{slug}/thread", members.api_member_thread)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await asyncio.wait_for(client.post("/api/members/code-reviewer/thread"), WAIT)
        assert resp.status == 200, await resp.text()
    key = held["waited"]
    assert state._slots[key].mode == members_mod.DM_SLOT_MODE
    assert key not in state._slots_under_construction


@pytest.mark.parametrize(
    ("between", "safe"),
    [
        ("", True),
        ("    await asyncio.to_thread(read)\n", False),
        ("    async with lock:\n        pass\n", False),
        ("    if key in state._slots_under_construction:\n        return None\n", True),
    ],
)
def test_the_opener_scan_refuses_a_suspension_between_the_wait_and_the_open(
    between: str, safe: bool
) -> None:
    """A wait followed by an await, before the open, does not count as waiting.

    A synchronous ``_slots_under_construction`` check after the last await is the
    named fallback and does count.
    """
    source = (
        "async def opener(state, key):\n"
        "    await wait_for_pending_create(state, key)\n"
        f"{between}"
        "    return state.get_or_create_slot(key)\n"
    )
    assert _synthetic_open_is_safe(source) is safe


def _synthetic_open_is_safe(source: str) -> bool:
    """The opener scan's verdict on the one open in *source*'s ``opener``."""
    scan = object.__new__(_OpenSites)
    scan.waiting = {"wait_for_pending_create", *_WAITING_OPENERS}
    scan.sites, scan.calls, scan.calls_in, scan.exempt_seen = {}, {}, {}, set()
    scan._scan("kiro_crew/synthetic.py", ast.parse(source))
    [(what, _line, ok)] = scan.sites["kiro_crew/synthetic.py:opener"]
    assert what == "get_or_create_slot"
    return ok


@pytest.mark.parametrize(
    ("body", "safe"),
    [
        # A wait in a branch that may not run does not cover the open after it.
        (
            "    if flag:\n"
            "        await wait_for_pending_create(state, key)\n"
            "    return state.get_or_create_slot(key)\n",
            False,
        ),
        # A wait in a loop body may run zero times.
        (
            "    for _ in range(n):\n"
            "        await wait_for_pending_create(state, key)\n"
            "    return state.get_or_create_slot(key)\n",
            False,
        ),
        # A wait in the enclosing statement list covers an open nested below it.
        (
            "    await wait_for_pending_create(state, key)\n"
            "    if flag:\n"
            "        return state.get_or_create_slot(key)\n",
            True,
        ),
        # A construction-mark test whose body goes on to the open is no fallback.
        (
            "    if key in state._slots_under_construction:\n"
            "        log(key)\n"
            "    return state.get_or_create_slot(key)\n",
            False,
        ),
        # One whose body leaves the function is.
        (
            "    if key in state._slots_under_construction:\n"
            "        raise LookupError(key)\n"
            "    return state.get_or_create_slot(key)\n",
            True,
        ),
    ],
)
def test_the_opener_scan_counts_only_a_wait_that_dominates_the_open(body: str, safe: bool) -> None:
    """A wait counts only when every path to the open passes it.

    The scan approximates that by statement lists: the wait must sit in the
    open's list or one enclosing it, and a construction-mark ``if`` counts only
    when its body returns or raises.

    Mutation pins: compare line numbers alone, or count every construction-mark
    ``if``, and the unsafe cases are judged safe.
    """
    assert _synthetic_open_is_safe(f"async def opener(state, key, flag, n):\n{body}") is safe


def test_every_opener_of_a_named_key_waits_for_a_pending_create() -> None:
    """Found, not listed: every open of a named key in ``kiro_crew`` meets a pending
    create safely, sync and async alike, and direct ``_slots[k] = ...`` writes too.

    Safe means, BEFORE the open in its own function body: a wait for the pending
    create, or the named fallback (a ``try`` that catches ValueError around it).
    Otherwise every call reaching the function must be safe in the same sense, or
    the function is named in
    ``_OPENERS_THAT_DO_NOT_WAIT`` with its reason.

    A wait followed by any other ``await`` (or ``async with`` / ``async for``)
    before the open does not count: a create can start during that suspension.

    A new opener fails here until it waits, falls back, or says why it need not.
    Mutation pins: move a wait after its open, put an await between a wait and
    its open, or drop ``ensure_cron_slot``'s fallback together with its wait, and
    the opener is listed.
    """
    scan = _OpenSites()
    for opener, inner in _WAITING_OPENERS.items():
        defined = [names for key, names in scan.calls_in.items() if key.endswith(f":{opener}")]
        assert defined and all(inner in names for names in defined), opener

    def reached_safely(owner: str, seen: frozenset[str] = frozenset()) -> bool:
        if owner in _OPENERS_THAT_DO_NOT_WAIT:
            return True
        if owner in seen:
            return False
        name = owner.rsplit(":", 1)[1]
        callers = scan.calls.get(name, [])
        return bool(callers) and all(
            ok or _passes_no_name(call) or reached_safely(caller, seen | {owner})
            for caller, _line, ok, call in callers
        )

    unsafe = sorted(
        f"{owner}:{line} {what}"
        for owner, sites in scan.sites.items()
        for what, line, ok in sites
        if not ok and not reached_safely(owner)
    )
    assert unsafe == []
    # Every reason still names a real opener.
    openers = {owner for owner, sites in scan.sites.items() if sites}
    assert sorted(set(_OPENERS_THAT_DO_NOT_WAIT) - openers) == []
    assert sorted(set(_OPENS_NO_CREATE_CAN_HOLD) - scan.exempt_seen) == []
    assert sorted(set(_OPENS_WAITED_ON_EVERY_PATH) - scan.exempt_seen) == []
    assert sorted(_CLOSE_PUT_BACKS - scan.exempt_seen) == []
    # The direct writers the review named are all seen, and each is guarded.
    writers = {
        owner.split(":")[0]
        for owner, sites in scan.sites.items()
        for what, _line, _ok in sites
        if what == "_slots[]="
    }
    assert {
        "kiro_crew/dashboard/chat_api/slot_lifecycle.py",
        "kiro_crew/dashboard/chat_api/resume.py",
        "kiro_crew/apps/builtins/spec_builder/backend/runtime.py",
        "kiro_crew/apps/builtins/spec_builder/backend/orchestration/execution_state.py",
    } <= writers


# -- open_named_slot: the wait and the open in one step --------------------------


@pytest.mark.asyncio
async def test_open_named_slot_opens_the_slot_a_pending_create_publishes(state: Any) -> None:
    """The open waits for the create holding its key, then returns the slot it published.

    Mutation pin: drop the wait from ``open_named_slot`` and the open meets the
    construction mark and raises "still being built".
    """
    create = _pending(state, "demo", agent="from-create")
    opening = asyncio.ensure_future(txn.open_named_slot(state, "demo", agent="from-opener"))
    await asyncio.sleep(0)
    assert not opening.done()
    published = create.publish()
    await create.settle()
    slot = await asyncio.wait_for(opening, WAIT)
    assert slot is published is state._slots["demo"]
    assert slot.agent == "from-create"


@pytest.mark.asyncio
async def test_open_named_slot_mints_the_key_a_create_gave_up(state: Any) -> None:
    create = _pending(state, "demo", agent="from-create")
    opening = asyncio.ensure_future(txn.open_named_slot(state, "demo", agent="from-opener"))
    await asyncio.sleep(0)
    assert not opening.done()
    await create.settle()
    slot = await asyncio.wait_for(opening, WAIT)
    assert slot is state._slots["demo"] and slot.agent == "from-opener"
    assert "demo" not in state._slots_under_construction


def test_open_named_slot_always_waits() -> None:
    """The helper takes no way to skip its wait, so every call to it is a wait.

    The opener scan counts any ``open_named_slot`` call as waiting; a ``wait=False``
    escape would let a new opener pass the scan without a wait or a fallback.
    Mutation pin: give ``open_named_slot`` a ``wait`` keyword again and this fails.
    """
    import inspect

    assert "wait" not in inspect.signature(txn.open_named_slot).parameters


@pytest.mark.asyncio
async def test_open_named_slot_refuses_a_wait_keyword(state: Any) -> None:
    """``wait=False`` is not an option: it reaches ``prepare_slot`` and raises TypeError."""
    with pytest.raises(TypeError):
        await txn.open_named_slot(state, "demo", wait=False)
    with pytest.raises(TypeError):
        await txn.open_named_slot(state, "demo", wait=False, on_create=lambda _slot: None)
    assert "demo" not in state._slots


@pytest.mark.asyncio
async def test_open_named_slot_runs_on_create_only_for_a_slot_it_created(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_create`` runs once, on a new slot, before any broadcast carries it."""
    seen_at_broadcast: list[str] = []
    broadcast = state._do_slots_broadcast

    def recording_broadcast(*args: Any, **kwargs: Any) -> Any:
        if "demo" in state._slots:
            seen_at_broadcast.append(state._slots["demo"].project)
        return broadcast(*args, **kwargs)

    monkeypatch.setattr(state, "_do_slots_broadcast", recording_broadcast)
    created: list[Any] = []

    def setup(slot: Any) -> None:
        slot.project = "/configured"
        created.append(slot)

    first = await txn.open_named_slot(state, "demo", on_create=setup)
    again = await txn.open_named_slot(state, "demo", on_create=setup)
    assert again is first and created == [first]
    assert seen_at_broadcast and all(project == "/configured" for project in seen_at_broadcast)


def _hand_written_pairs(rel: str, tree: Any) -> list[str]:
    """Each ``await wait_for_pending_create(...)`` in *tree* followed by a named
    ``get_or_create_slot`` before the next suspension point, as ``rel:fn:line``.
    """
    import ast

    def is_wait(stmt: Any) -> bool:
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Await)
            and isinstance(stmt.value.value, ast.Call)
            and _OpenSites._call_name(stmt.value.value) == "wait_for_pending_create"
        )

    def first_open_and_suspension(stmt: Any) -> tuple[Any, Any]:
        """Source positions of the first named open and the first suspension in *stmt*.

        A create can begin at any suspension, so the pair ends there, inside a
        compound statement as well as between statements.
        """
        opens, suspends = [], []
        for node in ast.walk(stmt):
            where = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
            if (
                isinstance(node, ast.Call)
                and _OpenSites._call_name(node) == "get_or_create_slot"
                and _names_a_key(node)
            ):
                opens.append(where)
            if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor, ast.Yield, ast.YieldFrom)):
                suspends.append(where)
        return min(opens, default=None), min(suspends, default=None)

    pairs: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef) or fn.name == "open_named_slot":
            continue
        for node in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if not isinstance(stmts, list):
                    continue
                for index, first in enumerate(stmts):
                    if not is_wait(first):
                        continue
                    # Forward from the wait to the next suspension point: an
                    # open anywhere in that run is the pair spelled by hand.
                    for later in stmts[index + 1 :]:
                        opened, suspended = first_open_and_suspension(later)
                        if opened is not None and (suspended is None or opened < suspended):
                            pairs.append(f"{rel}:{fn.name}:{first.lineno}")
                            break
                        if suspended is not None:
                            break
    return pairs


@pytest.mark.parametrize(
    ("between", "listed"),
    [
        ("", True),
        ("    _x = 0\n", True),
        ("    if key:\n        _x = 0\n", True),
        ("    await asyncio.sleep(0)\n", False),
    ],
)
def test_the_pair_scan_looks_forward_to_the_next_suspension(between: str, listed: bool) -> None:
    """The pair is found across synchronous statements, and ends at an await."""
    import ast

    source = (
        "async def opener(state, key):\n"
        "    await wait_for_pending_create(state, key)\n"
        f"{between}"
        "    return state.get_or_create_slot(key)\n"
    )
    assert bool(_hand_written_pairs("kiro_crew/synthetic.py", ast.parse(source))) is listed


def test_no_opener_repeats_the_wait_then_open_that_open_named_slot_owns() -> None:
    """An ``await wait_for_pending_create(...)`` followed by a named
    ``get_or_create_slot`` before the next suspension point is the step
    :func:`open_named_slot` owns: no async function outside it spells the two out
    by hand, adjacent or with synchronous statements between them.

    Mutation pins: put one routed site back to the wait and the open, or put a
    synchronous statement between them, and it is listed.
    """
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    by_hand: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if "/tests/" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        if "wait_for_pending_create" in text:
            by_hand.extend(_hand_written_pairs(rel, ast.parse(text)))
    assert by_hand == []
