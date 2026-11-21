"""Every engine that lands a model turn reports one ``interaction`` event.

``record_interaction_event`` (``llm_helpers_parts/turn_usage.py``) is the one
per-turn telemetry call the ``PlatformContext`` telemetry seam receives. Only
the dashboard chat runner and the legacy Slack handler made it, so a landed
turn on any other engine (a sub-agent, a cron job, a task run, a webhook, a
heartbeat task, Telegram, the channel dispatcher, the Slack transport path, a
parent turn that consumes a sub-agent report) never reached the seam, and a
provider counting interactions saw only part of the usage.

This test reads the source rather than driving each engine: the engines differ
in how a turn lands, and what must hold is the same for all of them, the call
sits beside the landed-turn bookkeeping. The second test is the ratchet: a new
module that books a landed turn fails until it either reports the event or is
listed below with the reason it does not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew

_SRC = Path(kiro_crew.__file__).resolve().parent

#: module (relative to ``kiro_crew``) -> function that books the landed turn,
#: and the ``surface`` literal it reports (``None``: computed, e.g. a channel).
_REPORTING_SITES: dict[str, tuple[str, str | None]] = {
    "dashboard/chat_turn/turn_stats.py": ("_turn_context_usage", "dashboard"),
    "slack/handler_runtime/turn.py": ("book_success", "slack"),
    "slack/transport_dispatch.py": ("handle_message_transport", "slack"),
    "subagent_manager/run_turn.py": ("_count_success", "subagent"),
    "dashboard/handlers/hooks.py": ("_run_hook_inner", "webhook"),
    "telegram/dispatch/turn_engine.py": ("_run_turn", "telegram"),
    "task_executor.py": ("execute_task", "task"),
    "messaging/dispatch.py": ("_run", None),
    "slack/gateway_runtime/cron_turn.py": ("_cron_run_sequence", "cron"),
    "slack/gateway_runtime/heartbeat_turns.py": ("_heartbeat_task", "heartbeat"),
    "slack/gateway_runtime/runtime_charge.py": ("_inject_with_retry", "subagent_report"),
}

#: A second function in the same module that also books a landed turn.
_EXTRA_SITES: dict[str, tuple[str, str | None]] = {
    "slack/gateway_runtime/cron_turn.py": ("_cron_run_single", "cron"),
}

#: Modules that book a landed turn but must NOT report, with the reason.
_NOT_REPORTING: dict[str, str] = {
    "session_parts/claims.py": "defines SessionManager.record_success / note_turn_landed",
    "parent_turns.py": "defines note_turn_landed",
    "messaging/dispatch_recovery.py": "the _record_success helper; messaging/dispatch.py reports",
    "messaging/turn_bracket.py": "TurnBracket.landed(); the engine that owns the bracket reports",
}


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _reported_surfaces(fn: ast.AST) -> list[str | None]:
    out: list[str | None] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "record_interaction_event" or len(node.args) < 3:
            continue
        arg = node.args[2]
        out.append(arg.value if isinstance(arg, ast.Constant) else None)
    return out


def _books_a_landed_turn(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "note_turn_landed":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "record_success":
            if ast.unparse(func.value).split(".")[-1] in {"sessions", "_sessions"}:
                return True
    return False


def test_every_landed_turn_engine_reports_one_interaction_event() -> None:
    missing = []
    for table in (_REPORTING_SITES, _EXTRA_SITES):
        for rel, (fn_name, surface) in table.items():
            tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
            fn = _function(tree, fn_name)
            surfaces = _reported_surfaces(fn)
            if surfaces.count(surface) != 1:
                missing.append(f"{rel}:{fn_name} reports {surfaces}, wants [{surface!r}]")
    assert not missing, "\n".join(missing)


#: Sites whose engine returns normally for a turn that did not land (a Stop, a
#: stream with no completion, a failed completion), so the call must sit under
#: that engine's landed verdict rather than its delivery or success branch.
_LANDED_GUARDED = (
    "slack/transport_dispatch.py",
    "telegram/dispatch/turn_engine.py",
    "messaging/dispatch.py",
    "dashboard/handlers/hooks.py",
    "slack/gateway_runtime/heartbeat_turns.py",
    "slack/gateway_runtime/runtime_charge.py",
)


def test_channel_and_webhook_sites_report_only_a_landed_turn() -> None:
    unguarded = []
    for rel in _LANDED_GUARDED:
        fn_name = _REPORTING_SITES[rel][0]
        fn = _function(ast.parse((_SRC / rel).read_text(encoding="utf-8")), fn_name)
        guarded = [
            call
            for node in ast.walk(fn)
            if isinstance(node, ast.If) and "landed" in ast.unparse(node.test)
            for stmt in node.body
            for call in ast.walk(stmt)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", getattr(call.func, "attr", ""))
            == "record_interaction_event"
        ]
        if len(guarded) != 1:
            unguarded.append(f"{rel}:{fn_name}")
    assert not unguarded, f"not under a landed check: {unguarded}"


def test_no_engine_books_a_landed_turn_without_a_decision() -> None:
    unlisted = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _REPORTING_SITES or rel in _NOT_REPORTING:
            continue
        text = path.read_text(encoding="utf-8")
        # Cheap text screen first: parsing every module costs seconds.
        if "note_turn_landed" not in text and "record_success" not in text:
            continue
        if _books_a_landed_turn(ast.parse(text)):
            unlisted.append(rel)
    assert not unlisted, (
        "these modules book a landed turn but are in neither _REPORTING_SITES nor "
        f"_NOT_REPORTING: {unlisted}"
    )
