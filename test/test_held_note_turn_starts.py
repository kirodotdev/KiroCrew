"""Every turn start delivers a held ``/note`` above its own row, or says why it does not.

A ``/note`` posted while a turn runs is held (``visibleDeferred: true``) and written
at a seam: the queue drain above the successor's row, the queue cycle's end, or a
direct send above its own row (``flush_restored_hold``) for a hold a restart
restored onto an idle slot. The seams are placed by hand, so what is worth pinning
is that a NEW way of starting a turn cannot skip them without turning this red. No
recorded turn can show that, because a path that does not exist yet cannot be run
through ``turn_harness``; so this module reads the package's source, and it is the
package's one scan for the property.

A turn start is a function or method that names ``_run_chat``, calls
``spawn_guarded_turn`` or ``enqueue_or_run_prompt``, or drains the queue
(``_dequeue_next_message`` / ``_dequeue_next_system_message``). It flushes when a
``flush_deferred_notes`` or ``flush_restored_hold`` call sits above the first row
it writes itself: a user-row append, a drain (which appends the successor's row)
or an ``enqueue_or_run_prompt`` call. A flush below that row lands the note under
the message it should precede, where the prompt build's ``exclude_last_n=1`` drops
the note instead of the request, and a turn start that writes no row of its own
cannot flush above it. Every other turn start is a row of :data:`_DOES_NOT_FLUSH`,
with the reason.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterator

import pytest
import source_corpus

pytestmark = pytest.mark.xdist_group(name="tree_scan_held_note_turn_starts")

_RUNNER = "_run_chat"
_DRAINS = frozenset({"_dequeue_next_message", "_dequeue_next_system_message"})
_ROW_WRITERS = _DRAINS | {"enqueue_or_run_prompt"}
_TURN_STARTS = _ROW_WRITERS | {"spawn_guarded_turn"}
_FLUSHES = frozenset({"flush_deferred_notes", "flush_restored_hold"})

_AUTOMATIC = (
    "a note is owed to the next turn a person starts; the hold waits for this turn's cycle end"
)
_RESTORED_HOLD_MISSED = (
    "a person's turn that does not deliver a hold a restart restored onto an idle slot, "
    "so the note lands at its cycle end, below its reply (open in #32240)"
)

#: Turn starts that do not flush the hold above their anchor, keyed by path under
#: the package and function. A row that names no such turn start fails, so an
#: exemption cannot outlive the code it describes.
_DOES_NOT_FLUSH: dict[tuple[str, str], str] = {
    ("dashboard/chat_turn/agent_started.py", "_on_agent_turn"): (
        f"a turn the agent started between prompts: {_AUTOMATIC}"
    ),
    ("dashboard/chat_turn/queue_cycle.py", "_run_pending_synthesis"): (
        f"sub-agent result synthesis: {_AUTOMATIC}"
    ),
    ("dashboard/mate_welcome.py", "dispatch_hidden_turn"): (
        "a gateway-authored hidden turn (every caller is a crewmate welcome, goal "
        f"setup or reminder, or thread wake): {_AUTOMATIC}"
    ),
    ("slack/gateway_runtime/nudge_dashboard_fire.py", "_start_dashboard_wake_turn"): (
        f"a monitor loop wake: {_AUTOMATIC}"
    ),
    ("slack/gateway_runtime/result_egress.py", "_deliver_result"): (
        f"a heartbeat result: {_AUTOMATIC}"
    ),
    ("dashboard/session_deliver.py", "_deliver_peer"): (
        f"another session's message, delivered with user_origin=False: {_AUTOMATIC}"
    ),
    ("dashboard/server_runtime/workflow_startup.py", "_initialize_workflow_service"): (
        f"a finished workflow's result: {_AUTOMATIC}"
    ),
    ("apps/builtins/issue_radar/backend/crew_runtime_session.py", "dispatch_crew_turn"): (
        f"an Issue Radar crew turn: {_AUTOMATIC}"
    ),
    ("apps/builtins/issue_radar/backend/crew_runtime_session.py", "_capped_run_chat"): (
        "the body of the crew turn dispatch_crew_turn starts, which writes no row"
    ),
    ("dashboard/handlers/taskrunner.py", "api_taskrunner_to_chat"): (
        "starts a turn only on the fresh slot it just created, which holds no note"
    ),
    ("dashboard/chat_turn/slash_commands.py", "_turn_slash_gate"): (
        "re-enters the turn already running with the expanded prompt; that turn's "
        "start decided the hold"
    ),
    ("cli.py", "main"): "the terminal chat's own _run_chat, which drives no dashboard slot",
    ("dashboard/chat_regenerate.py", "api_chat_slot_regenerate"): (
        f"Regenerate is {_RESTORED_HOLD_MISSED}"
    ),
    ("dashboard/chat_regenerate.py", "api_chat_slot_edit_resend"): (
        f"Edit & Resend is {_RESTORED_HOLD_MISSED}"
    ),
    ("dashboard/chat_rewind.py", "api_chat_slot_rewind"): f"rewind is {_RESTORED_HOLD_MISSED}",
    ("slack/handler_runtime/inbound.py", "route_linked_thread"): (
        f"a message in a linked channel thread is {_RESTORED_HOLD_MISSED}"
    ),
    ("apps/builtins/spec_builder/backend/runtime.py", "_dispatch_turn"): (
        f"a Spec Builder chat turn is {_RESTORED_HOLD_MISSED}"
    ),
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _is_user_row(call: ast.Call) -> bool:
    """``<x>.append("user", ...)`` or ``<x>.append(role="user", ...)``."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "append"):
        return False
    role = call.args[0] if call.args else None
    if role is None:
        role = next((kw.value for kw in call.keywords if kw.arg == "role"), None)
    return isinstance(role, ast.Constant) and role.value == "user"


def _flushes_above_its_row(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool | None:
    """``None`` when *fn* starts no turn, else whether a flush sits above the first row it writes."""
    starts = False
    rows: list[int] = []
    flushes: list[int] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == _RUNNER:
            starts = True
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            starts = starts or name in _TURN_STARTS
            if name in _FLUSHES:
                flushes.append(node.lineno)
            elif name in _ROW_WRITERS or _is_user_row(node):
                rows.append(node.lineno)
    if not starts:
        return None
    return bool(rows) and any(line < min(rows) for line in flushes)


def _functions(
    node: ast.AST, prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every def not nested in another def, at any statement depth; a method is ``Class.name``."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{child.name}", child
        elif isinstance(child, ast.ClassDef):
            yield from _functions(child, f"{prefix}{child.name}.")
        elif isinstance(child, (ast.stmt, ast.excepthandler)):
            yield from _functions(child, prefix)


def _scan(tree: ast.Module) -> dict[str, bool]:
    """Each function or method of *tree* that starts a turn, and whether it flushes."""
    found: dict[str, bool] = {}
    for name, fn in _functions(tree):
        flushes = _flushes_above_its_row(fn)
        if flushes is not None:
            found[name] = found.get(name, True) and flushes
    return found


@functools.lru_cache(maxsize=1)
def _package_turn_starts() -> dict[tuple[str, str], bool]:
    root = source_corpus.src_root()
    found: dict[tuple[str, str], bool] = {}
    for path, _text, tree in source_corpus.parsed_candidates(
        require_any=(_RUNNER, *_TURN_STARTS), skip_syntax_errors=False
    ):
        rel = path.relative_to(root).as_posix()
        for name, flushes in _scan(tree).items():
            found[(rel, name)] = flushes
    return found


def test_every_turn_start_flushes_the_hold_or_is_listed() -> None:
    """A NEW turn start that does not flush above its own row turns this red."""
    found = _package_turn_starts()
    assert any(found.values()), "no turn start flushes; the scan itself is broken"
    offenders = sorted(
        site for site, flushes in found.items() if not flushes and site not in _DOES_NOT_FLUSH
    )
    assert not offenders, f"turn starts that skip the held-note flush: {offenders}"


def test_every_listed_turn_start_still_skips_the_flush() -> None:
    found = _package_turn_starts()
    stale = sorted(site for site in _DOES_NOT_FLUSH if found.get(site) is not False)
    assert not stale, f"rows naming no turn start that skips the flush: {stale}"


def test_the_scan_can_fail() -> None:
    """Negative controls, one per way a turn start goes wrong, and the passing shapes."""

    def scan(source: str) -> dict[str, bool]:
        return _scan(ast.parse(source))

    missing = (
        "def start(state, slot):\n"
        "    slot.append('user', 'x')\n"
        "    spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
    )
    assert scan(missing) == {"start": False}

    below_the_row = (
        "async def start(state, slot):\n"
        "    slot.append(role='user', content='x')\n"
        "    flush_restored_hold(slot, logger=None)\n"
        "    slot.task = asyncio.create_task(_run_chat(state, slot, 'x'))\n"
    )
    assert scan(below_the_row) == {"start": False}

    below_the_drain = (
        "def start(state, slot):\n"
        "    nxt, consumed = _dequeue_next_message(slot, merge_enabled=False)\n"
        "    slot.flush_deferred_notes()\n"
        "    spawn_guarded_turn(state, slot, _run_chat(state, slot, nxt))\n"
    )
    assert scan(below_the_drain) == {"start": False}

    no_row_of_its_own = (
        "def start(state, slot):\n"
        "    slot.flush_deferred_notes()\n"
        "    spawn_guarded_turn(state, slot, _run_chat(state, slot, 'x'))\n"
    )
    assert scan(no_row_of_its_own) == {"start": False}

    flushes = (
        "class Slot:\n"
        "    def start(self, state):\n"
        "        flush_restored_hold(self, logger=None)\n"
        "        return self.enqueue_or_run_prompt('x', _run_chat, state)\n"
        "    def steer(self):\n"
        "        self.append('user', 'x')\n"
        "if True:\n"
        "    def drain(state, slot):\n"
        "        slot.flush_deferred_notes()\n"
        "        nxt, consumed = _dequeue_next_message(slot, merge_enabled=False)\n"
        "        spawn_guarded_turn(state, slot, _run_chat(state, slot, nxt))\n"
    )
    assert scan(flushes) == {"Slot.start": True, "drain": True}
