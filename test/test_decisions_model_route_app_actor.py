"""A dispatch that OBSERVED an app must NAME it, at every call site.

``_crew_log_actor`` resolves to ``user`` when a dispatch passes no ``_turn_actor``
(``chat_runner``: ``_turn_actor or ("autonudge" if _directive_self_wake else "user")``).
So a site that reads a request's ``app`` claim, uses it to decide
``_directive_user_origin``, and then stays silent about the actor records a person
who never typed anything -- which ``chat_handlers.api_chat`` already says in its own
comment, and already does.

That mislabelling is load-bearing for ``model.route``: the routing gate admits a
turn whose structural actor is ``user``, so an app-authored turn reaching the gate
as ``user`` gets an owner-scoped model decision spent on it. ``api_chat`` was the
only one of five such sites naming the actor, which is exactly the shape a
presence check passes and a COVERAGE check does not.

So this is an AST sweep rather than a behavioural test of one handler. The five
sites live in five modules and reach ``_run_chat`` through five different shapes (a
kwargs dict, a bare call, two nested dispatch closures, a ``wait_for``); a test per
handler would pin the four that exist and say nothing about the sixth. What every
site has in common is visible in the syntax: it derives a keyword from an ``app``
claim. That is the thing asserted.

TWO SHAPES, not one, and the second is why the population is pinned rather than
trusted. A send to a BUSY slot never reaches ``_run_chat`` from its handler at all:
it is appended to the slot queue and the DRAIN dispatches it later, so no keyword on
any call can carry the actor across the wait. It rides the entry's meta
(``TURN_ACTOR_META_KEY``), which ``_actor_for_queue_items`` reads, and an unstamped
entry drains as ``user``. A sweep looking only for ``_run_chat`` keywords reported
full coverage while both queue paths were silent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew.dashboard as dashboard_pkg

#: Spellings a dispatch uses to read the request's app claim. ``is_dashboard_caller``
#: is the inverted form ``openai_compat`` computes once and passes as the origin.
_APP_TOKENS = ("request_app", '"app"', "'app'", "is_dashboard_caller")

#: The keyword whose value tells us the site consulted the claim at all. A site that
#: never derives provenance from an app is not this rule's subject -- a cron or a
#: sub-agent names its own actor for its own reasons.
_ORIGIN_KW = "_directive_user_origin"


def _dashboard_sources() -> list[Path]:
    root = Path(dashboard_pkg.__file__).parent
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _run_chat_calls(tree: ast.AST, src: str):
    """Every ``_run_chat(...)`` call, with the source text of each keyword."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name != "_run_chat":
            continue
        kwargs = {}
        for kw in node.keywords:
            # ``arg is None`` is ``**{...}``, which is how a site spreads a
            # conditional actor; its own source text carries the keyword name.
            kwargs[kw.arg or "**"] = ast.get_source_segment(src, kw.value) or ""
        yield node, kwargs


def _app_derived_dispatches() -> list[tuple[str, int, dict]]:
    found = []
    for path in _dashboard_sources():
        src = path.read_text(encoding="utf-8")
        if "_run_chat" not in src:
            continue
        for node, kwargs in _run_chat_calls(ast.parse(src), src):
            origin = kwargs.get(_ORIGIN_KW, "")
            if any(token in origin for token in _APP_TOKENS):
                found.append((path.name, node.lineno, kwargs))
    return found


def test_the_sweep_finds_the_sites_it_is_meant_to_guard():
    """The guard's own witness. An AST sweep that matched nothing would pass the rule
    below forever, and that failure is silent -- so the population is pinned before
    anything is asserted about it.

    FOUR sites pass the claim as a literal keyword: the OpenAI-compatible route,
    rewind, regenerate and edit-and-resend. ``chat_handlers.api_chat`` is the fifth
    site of the same class and is deliberately NOT here: it builds its kwargs as a
    dict and spreads them, so no keyword is visible on the call. It has its own test
    below, because a sweep silently missing a site is the failure this file exists to
    prevent -- so the one site it cannot see is named rather than left to luck."""
    found = _app_derived_dispatches()

    assert len(found) == 4, f"expected 4 keyword-shaped app-derived dispatches, found {found}"
    assert {name for name, _, _ in found} == {
        "chat_regenerate.py",
        "chat_rewind.py",
        "openai_compat.py",
    }


def test_the_accepting_handler_builds_both_keys_into_its_kwargs():
    """``api_chat``'s dict form, asserted where the sweep cannot reach.

    It is the site the other four were measured against, so a regression here would
    remove the very precedent this rule cites. Read as SOURCE because the value is
    assembled across two statements (the dict literal, then a conditional insert),
    which is the shape that put it outside the keyword sweep."""
    path = Path(dashboard_pkg.__file__).parent / "chat_handlers.py"
    src = path.read_text(encoding="utf-8")

    assert '_turn_kwargs: dict = {"_directive_user_origin": not bool(request_app)}' in src
    assert '_turn_kwargs["_turn_actor"] = "app"' in src


@pytest.mark.parametrize("site", _app_derived_dispatches(), ids=lambda s: f"{s[0]}:{s[1]}")
def test_a_dispatch_that_read_the_app_claim_also_names_the_actor(site):
    """The rule. A site holding the claim must say ``app``, or the turn is filed as a
    person's and every consumer that asks "is a human watching this" is told yes."""
    name, lineno, kwargs = site
    rendered = " ".join(kwargs.values()) + " " + " ".join(kwargs)

    assert "_turn_actor" in rendered, (
        f"{name}:{lineno} derives {_ORIGIN_KW} from the request's app claim but names "
        f"no _turn_actor, so an app-authored turn is recorded as the user's"
    )
    assert (
        '"app"' in rendered or "'app'" in rendered
    ), f"{name}:{lineno} names an actor that is not 'app' while reading the app claim"


#: The queue producers a handler reaches INSTEAD of ``_run_chat`` when the slot is
#: busy. Both read the app claim the same way, and for both the entry's meta is the
#: only thing that survives to the drain.
_QUEUE_CALLS = ("queue_for_next_turn", "queue_append")


def _app_derived_queue_calls() -> list[tuple[str, int, str]]:
    """Every queue append whose ``directive_user_origin`` comes from an app claim,
    paired with the EVIDENCE that it stamps an actor -- or ``""`` when there is none.

    The evidence is deliberately narrow, and the narrowness is the whole point. An
    earlier version searched the enclosing function and was vacuous: ``api_chat``
    holds the direct-dispatch ``_turn_actor`` stamp too, so deleting the queue stamp
    left an unrelated line satisfying the rule and the guard passed on the defect it
    exists to catch. Only two things count now:

    * a ``turn_actor=`` keyword on the call itself, or
    * an assignment into the very dict the call passes as ``meta=``
      (``<that name>[TURN_ACTOR_META_KEY] = ...``), which is how a stamp assembled a
      few lines above the call is recognised without admitting the rest of the body.
    """
    found = []
    for path in _dashboard_sources():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name not in _QUEUE_CALLS:
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            # Most `queue_append` calls name no provenance at all (a cron banner, a
            # sub-agent completion, a runner requeue). Those are not this rule's
            # subject, and reading the keyword unconditionally raised on them at
            # COLLECTION -- which reads as a usage error rather than a failure.
            origin_node = kwargs.get("directive_user_origin")
            origin = ast.get_source_segment(text, origin_node) if origin_node else ""
            if not any(token in (origin or "") for token in _APP_TOKENS):
                continue

            evidence = ""
            if "turn_actor" in kwargs:
                evidence = ast.get_source_segment(text, kwargs["turn_actor"]) or ""
            else:
                meta_arg = kwargs.get("meta")
                meta_name = meta_arg.id if isinstance(meta_arg, ast.Name) else ""
                if meta_name:
                    scope = node
                    while scope in parents and not isinstance(
                        scope, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        scope = parents[scope]
                    for stmt in ast.walk(scope):
                        if not isinstance(stmt, ast.Assign):
                            continue
                        for target in stmt.targets:
                            if (
                                isinstance(target, ast.Subscript)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == meta_name
                                and "TURN_ACTOR_META_KEY"
                                in (ast.get_source_segment(text, target.slice) or "")
                            ):
                                evidence = ast.get_source_segment(text, stmt) or ""
            found.append((path.name, node.lineno, evidence))
    return found


def test_the_queue_sweep_finds_the_sites_it_is_meant_to_guard():
    """The second shape's witness. THREE app-derived queue appends: the busy-slot
    queue, the sub-agent hold, and the plan-approval queue in another module. A fourth
    is a new site and must be added deliberately -- and a sweep finding none would
    pass the rule below in silence, which is exactly how a `_run_chat`-only sweep
    reported full coverage while all three were unstamped.

    The third one is why the count is asserted rather than the module set trusted: it
    lives outside the handler module and was found by this sweep, not by reading the
    two the review named."""
    found = _app_derived_queue_calls()

    assert len(found) == 3, f"expected 3, found {[(n, ln) for n, ln, _ in found]}"
    assert {name for name, _, _ in found} == {"chat_handlers.py", "chat_orchestrator.py"}


@pytest.mark.parametrize("site", _app_derived_queue_calls(), ids=lambda s: f"{s[0]}:{s[1]}")
def test_a_queue_append_that_read_the_app_claim_also_stamps_the_actor(site):
    """The rule for the queue shape. The entry's meta is the only carrier across the
    wait, so an unstamped app entry drains as the person's and lands on the routing
    gate's ``user`` arm."""
    name, lineno, evidence = site

    assert evidence, (
        f"{name}:{lineno} queues a turn whose origin came from the app claim but stamps "
        f"no actor on the entry it queues, so the drain resolves that turn to the user"
    )
    assert (
        '"app"' in evidence
    ), f"{name}:{lineno} stamps an actor that is not 'app' while reading the app claim"


def test_a_restored_queue_entry_carries_no_actor():
    """A stamp on disk is worth what the file is worth, which is nothing.

    The durable queue line is an ordinary writable file in the crew home, and
    ``slot_queue_repository`` already drops ``_directive_user_origin`` and
    ``_directive_channel_origin`` on restore for exactly that reason -- its own
    docstring calls anything restored from that line attacker-supplied. The turn
    actor has to go the same way: it names WHO authored the entry, the drain turns
    that into what the turn may do, and ``model.route`` admits only ``user`` -- so
    an entry whose stamp a file editor removed drains as the person's and spends an
    owner-scoped model decision on an app's prompt.

    Driven through the real restore, with the stamp present on the line, so this
    fails if the key is ever added back to what the reader carries.
    """
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    on_disk = [
        {
            "id": "q1",
            "content": "a prompt an app queued",
            "meta": {TURN_ACTOR_META_KEY: "app", "sendId": "s1"},
        }
    ]

    restored = sanitize_restored_queue(on_disk)

    assert len(restored) == 1, f"the entry itself must survive: {restored}"
    meta = restored[0].get("meta") or {}
    assert TURN_ACTOR_META_KEY not in meta, (
        "a restored entry kept its actor stamp, so whoever can write the session "
        "file decides which turns reach the owner-scoped routing arm"
    )
    # The rest of the sender's own metadata still rides along -- dropping the
    # whole dict would lose the send id a caller proves its message landed with.
    assert meta.get("sendId") == "s1"
