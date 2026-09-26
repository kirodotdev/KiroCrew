"""The resume pin is threaded, not re-derived: a structural inventory.

A message that reaches a Discord, Telegram or Teams conversation is pinned ONCE at
admission to the session it runs in (``session_resume.ResumeBinding``), and every
later step that could route it takes that pin -- the hand-off to a dashboard turn,
the steer-or-queue busy path, the queue entry, the retry after a false enqueue, the
turn itself, the drain's replay. Three heads in a row carried a variant of the same
defect (a stale binding check, a check that ran before the awaits, a retry that
re-routed), each fixed at its site; this pin exists so that no call site CAN route
unpinned, and this test is what keeps it that way: it reads the dispatchers' source
and asserts, for every call site of the pinned helpers, that the pin is what they
receive. A new call site fails here until it is classified below.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew.discord.transport_dispatch as discord_mod
import kiro_crew.messaging.session_resume as session_resume_mod
import kiro_crew.teams.transport_dispatch as teams_mod
import kiro_crew.telegram.transport_dispatch as telegram_mod

#: Helpers on the replay path. Each takes the pin, and every call site passes it.
PINNED_HELPERS = {
    "discord": {"_handle_busy", "_enqueue_with_receipt", "_hand_to_dashboard_turn"},
    "telegram": {"_handle_busy", "_enqueue_with_receipt", "_hand_to_dashboard_turn"},
    "teams": {"_handle_busy", "_enqueue_with_receipt", "_hand_to_dashboard_turn", "_run_turn"},
}

#: ``self.handle_message(...)`` re-entries that MUST carry the pin: the busy path's
#: retry after a false enqueue (``_handle_busy``) and the drain's replay
#: (``_pump_queue``). Re-routing either is how a rebind that landed while the
#: message waited carries it into another session.
PINNED_REENTRIES = {"_handle_busy", "_pump_queue"}

#: ``self.handle_message(...)`` re-entries that are FRESH messages by design and
#: must not carry a pin: an option-chip press (routed by its ``origin_tag``, or a
#: card action) and a slash-command interaction re-dispatched as a synthetic
#: message. Anything not listed here or above is unclassified and fails.
FRESH_REENTRIES = {
    "discord": {"on_interaction", "_on_command_interaction"},
    "telegram": {"on_callback"},
    "teams": {"_handle_card_action"},
}

#: The loose spellings the pin replaced. Two callees legitimately still take one:
#: the shared restricted-session check, which is handed the pin's key, and the
#: ``RoutingDecision`` constructor, which is the routing decision itself (the pin is
#: built FROM it). Telegram's ``/link`` command handler takes the resolved key as a
#: command input, not as a routing decision -- it runs before any busy path and
#: replays nothing -- so it is listed rather than converted.
LOOSE_KWARGS = {"resumed_key", "resumed_session_key"}
LOOSE_KWARG_ALLOWED_CALLEES = {"refused_resume_is_restricted", "RoutingDecision", "_handle_link"}

MODULES = {"discord": discord_mod, "telegram": telegram_mod, "teams": teams_mod}


def _tree(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _self_calls(fn, names: set[str]):
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in names
        ):
            yield node


def _passes_binding(call: ast.Call) -> bool:
    positional = any(isinstance(a, ast.Name) and a.id == "binding" for a in call.args)
    keyword = any(k.arg == "binding" for k in call.keywords)
    return positional or keyword


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


@pytest.mark.parametrize("channel", sorted(MODULES))
def test_every_pinned_helper_call_site_receives_the_binding(channel: str) -> None:
    tree = _tree(MODULES[channel])
    seen: list[str] = []
    unpinned: list[str] = []
    for fn in _functions(tree):
        for call in _self_calls(fn, PINNED_HELPERS[channel]):
            site = f"{fn.name}:{call.lineno} self.{_callee_name(call)}(...)"
            seen.append(site)
            if not _passes_binding(call):
                unpinned.append(site)
    assert seen, f"{channel}: no call sites found -- the helper set is stale"
    assert unpinned == [], f"{channel}: helper call sites without the pin: {unpinned}"


@pytest.mark.parametrize("channel", sorted(MODULES))
def test_every_handle_message_reentry_is_classified_and_pinned_where_it_must_be(
    channel: str,
) -> None:
    tree = _tree(MODULES[channel])
    unpinned: list[str] = []
    unexpected_pin: list[str] = []
    unclassified: list[str] = []
    seen_pinned = 0
    for fn in _functions(tree):
        for call in _self_calls(fn, {"handle_message"}):
            site = f"{fn.name}:{call.lineno}"
            if fn.name in PINNED_REENTRIES:
                seen_pinned += 1
                if not _passes_binding(call):
                    unpinned.append(site)
            elif fn.name in FRESH_REENTRIES[channel]:
                if _passes_binding(call):
                    unexpected_pin.append(site)
            else:
                unclassified.append(site)
    assert seen_pinned >= 2, f"{channel}: expected the retry and the replay re-entries"
    assert unpinned == [], f"{channel}: re-entries that route unpinned: {unpinned}"
    assert unexpected_pin == [], f"{channel}: fresh re-entries carrying a pin: {unexpected_pin}"
    assert unclassified == [], (
        f"{channel}: handle_message re-entries not classified as pinned or fresh: "
        f"{unclassified} -- add them to PINNED_REENTRIES or FRESH_REENTRIES deliberately"
    )


@pytest.mark.parametrize("channel", sorted(MODULES))
def test_the_pinned_helpers_take_the_binding_and_no_loose_key(channel: str) -> None:
    tree = _tree(MODULES[channel])
    wanted = PINNED_HELPERS[channel] | {"handle_message"}
    found: dict[str, ast.AST] = {}
    for fn in _functions(tree):
        if fn.name in wanted and fn.name not in found:
            found[fn.name] = fn
    assert (
        set(found) == wanted
    ), f"{channel}: helpers missing from the module: {wanted - set(found)}"
    for name, fn in found.items():
        params = {a.arg: a for a in fn.args.args + fn.args.kwonlyargs}
        assert "binding" in params, f"{channel}.{name} has no `binding` parameter"
        annotation = ast.unparse(params["binding"].annotation or ast.Name(id="?"))
        assert "ResumeBinding" in annotation, f"{channel}.{name}: binding is {annotation}"
        loose = LOOSE_KWARGS & set(params)
        assert not loose, f"{channel}.{name} still takes a loose key: {loose}"


@pytest.mark.parametrize("channel", sorted(MODULES))
def test_no_call_passes_a_loose_resumed_key(channel: str) -> None:
    tree = _tree(MODULES[channel])
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node) in LOOSE_KWARG_ALLOWED_CALLEES:
            continue
        for kw in node.keywords:
            if kw.arg in LOOSE_KWARGS:
                offenders.append(f"{node.lineno}: {_callee_name(node)}({kw.arg}=...)")
    assert offenders == [], f"{channel}: loose resumed-key keywords: {offenders}"


def test_the_loose_check_helper_is_gone() -> None:
    """The pin's own ``check`` replaced the module-level ``check_replay_binding``:
    a check that takes two loose keys is a check a caller can hand the wrong keys."""
    assert not hasattr(session_resume_mod, "check_replay_binding")
    assert callable(getattr(session_resume_mod.ResumeBinding, "check"))
