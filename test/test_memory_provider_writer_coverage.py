"""Every reader and writer of Crew's memory asks the provider resolver first.

A missed writer would keep writing Crew's own stores while a provider answers reads,
which leaves two truths after a switch back. This test names each chokepoint and
secondary writer, by module and function, and fails when that function does not
reach the resolver. A new writer of Crew's memory joins the table.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: (module path under src/kiro_crew, function name, the resolver entry it calls)
WRITERS = [
    ("context_assembly/session_start.py", "build_session_context", "session_route"),
    ("context_assembly/store_admission.py", "turn_lessons_block", "session_route"),
    ("context_assembly/store_admission.py", "compaction_lessons_block", "session_route"),
    ("context_assembly/store_admission.py", "embed_startup_query", "session_route"),
    ("context_assembly/store_admission.py", "prefetch_session_memory", "session_route"),
    ("dashboard/handlers/memory_member.py", "api_memory_recall", "session_route"),
    ("dashboard/handlers/memory_member.py", "api_memory_seed", "provider_active_refusal"),
    ("dashboard/handlers/cron_lessons.py", "api_lessons_create", "session_route"),
    ("dashboard/handlers/cron_lessons.py", "api_lessons_delete", "session_route"),
    ("dashboard/handlers/cron_lessons.py", "api_lessons_update", "session_route"),
    ("dashboard/handlers/cron_lessons.py", "api_lessons", "session_route"),
    ("dashboard/handlers/memory.py", "_memory_write_gate", "provider_active_refusal"),
    ("dashboard/handlers/memory_admin.py", "api_memory_retired_restore", "provider_active_refusal"),
    ("history_consolidation_pass.py", "_consolidate", "session_route"),
    ("dashboard/handlers/change_cards.py", "_write", "builtin_writes_allowed"),
    ("taskrunner_parts/run_reporting.py", None, "session_route"),
    ("onboarding_import.py", "apply_import", "route_for"),
    ("cli_commands_learn.py", "_learn", "provider_replaces"),
    ("cli_commands_memory.py", "_memory_verb", "provider_replaces"),
    ("heartbeat.py", None, "builtin_background_allowed"),
    (
        "slack/gateway_runtime/memory_lifecycle.py",
        "_repair_member_memory",
        "builtin_background_allowed",
    ),
    (
        "slack/gateway_runtime/memory_lifecycle.py",
        "_auto_migrate_memory",
        "builtin_background_allowed",
    ),
    (
        "apps/builtins/ops_mission_control/backend/ledger_index.py",
        "import_pending",
        "builtin_writes_allowed",
    ),
]


def _function_sources(path: Path) -> dict[str, list[str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, []).append(ast.get_source_segment(source, node) or "")
    found[""] = [source]
    return found


@pytest.mark.parametrize(
    "module, function, entry", WRITERS, ids=[f"{m}:{f or '*'}" for m, f, _ in WRITERS]
)
def test_the_writer_reaches_the_resolver(module: str, function: str | None, entry: str) -> None:
    sources = _function_sources(SRC / module)
    bodies = sources.get(function or "", [])
    assert bodies, f"{module} has no function {function}: update this table"
    assert any(entry in body for body in bodies), (
        f"{module}:{function} no longer calls {entry}; a memory writer that skips the "
        "resolver keeps writing Crew's own stores while a provider replaces them"
    )
