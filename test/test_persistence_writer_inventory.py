"""Every automatic persistent-memory writer is gated by the persistence switch.

``memory.persistence_enabled: false`` promises "no automatic memory writes".
The read side has a chokepoint (``_config_scoped_groups`` inside
``build_session_context``), but the write side cannot have one: the gate must
sit at the *service boundary* rather than in the storage layer, because
explicit dashboard edits and deletions have to keep working while the switch is
off — a store-layer refusal would take away the right to forget along with the
automatic capture.

The cost of that shape is drift: a future automatic writer can call
``write_lesson()`` / ``write_episodic()`` / ``write_preferences()`` directly and
persist while the operator's config says memory is off, silently breaking the
promise. This module is the ratchet that makes such a writer go red instead of
shipping: it walks the source for calls to the persistent-write primitives and
requires every enclosing function to be classified below. A new call site lands
in no bucket and fails, forcing an explicit decision about whether it needs the
gate.

It pins the INVENTORY, not the gate mechanism — that each gate actually refuses
is asserted in ``test_memory_context_toggles.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: Methods that commit something to a persistent memory store.
_WRITE_PRIMITIVES = frozenset(
    {
        "write_lesson",
        "write_episodic",
        "set_semantic",
        "set_semantic_if_absent",
        "write_preferences",
        "write_projects",
        "append_history",
        "save_or_enrich",
    }
)

#: Automatic writers that MUST be behind the persistence switch, as
#: ``<module path relative to src/kiro_crew>::<enclosing function>``. Each is
#: gated at its entry point; see test_memory_context_toggles.py for the
#: behavioural assertions.
GATED_AUTOMATIC = {
    # One consolidation pass writes lessons, semantic, episodic, preferences and
    # projects. Gated in _consolidate plus all three automatic entry points.
    "history_consolidation.py::_save_lessons",
    "history_consolidation.py::_write_structured_memory",
    "history_consolidation.py::_consolidate",
    # Distils a lesson from a repeatedly failing task; gated before the LLM call.
    "taskrunner.py::_extract_lesson",
    # The enforcement point behind the learn_add MCP tool: every transport that
    # posts a lesson (tool, dashboard, direct HTTP) is refused by this one check.
    "dashboard/handlers/cron.py::api_lessons_create",
}

#: Writers that are deliberately NOT gated, each with the reason it is exempt.
#: An entry here is a decision, not an oversight — changing one means changing
#: what the switch promises.
EXEMPT = {
    # The user's own explicit edits and the right to forget survive the switch.
    "dashboard/handlers/memory.py::api_memory_preferences": "explicit user edit",
    "dashboard/handlers/memory.py::api_memory_projects": "explicit user edit",
    "dashboard/handlers/memory.py::write_preferences": "explicit user edit",
    "dashboard/handlers/memory.py::write_projects": "explicit user edit",
    # An explicit semantic-memory write from the dashboard editor, like the
    # preference/project edits above.
    "dashboard/handlers/memory.py::api_memory_semantic_write": "explicit user edit",
    # Internal helpers: they hold no policy and are reached through the callers
    # above, which are classified.
    "memory.py::add_preference": "internal helper, caller decides",
    "memory.py::append_history": "storage facade, caller decides",
    "memory.py::write": "internal helper, caller decides",
    # Store-internal writes. The storage layer is deliberately NOT the gate (see
    # the module docstring): these are reached from the classified entry points
    # or from an explicit migration/import the user asked for.
    "vector_memory.py::write_lesson": "storage primitive",
    "vector_memory.py::seed_item_if_absent": "storage primitive",
    "vector_memory.py::import_memory": "explicit user-initiated import",
    "vector_memory.py::migrate_from_markdown": "explicit one-shot migration",
    "vector_memory.py::promote_episodic_patterns": "explicit dashboard action",
    # A one-shot, user-initiated import (merge-only, never tombstones).
    "onboarding_import.py::_write_instruction": "user-initiated one-shot import",
    "onboarding_import.py::_write_memory": "user-initiated one-shot import",
    # Harnesses that seed their own isolated workspace, never the live store.
    "eval/runner.py::_seed_profile": "eval harness, isolated workspace",
    "eval/bench/ingest.py::ingest_instance": "bench harness, isolated workspace",
    "eval/bench/member_v2.py::_insert": "bench harness, isolated workspace",
    "eval/bench/member_v2.py::_edge_report": "bench harness, isolated workspace",
    # The CLI's own explicit `kirocrew learn add`, which carries its own check
    # (a refusal message rather than a silent skip) — see cli_commands.
    "cli_commands.py::_learn": "carries its own refusal",
    # KNOWN GAP, flagged for a maintainer ruling rather than silently widened:
    # an installed app's sweep projects its ops ledger into the shared episodic
    # store, so it writes while the switch is off. It is app-scoped and reached
    # only through that app's own trigger, but "no automatic memory writes" does
    # not currently hold for it.
    "apps/builtins/ops_mission_control/backend/ledger_index.py::import_pending": (
        "app-scoped sweep; ungated pending a ruling"
    ),
}


def _writer_sites() -> set[str]:
    """Every ``<module>::<function>`` in the package that reaches a write primitive.

    Matches an attribute *reference*, not only a call: the offloading writers
    hand the bound method to a pool (``run_in_embed_pool(store.write_lesson,
    ...)``, ``asyncio.to_thread(store.save, ...)``) rather than calling it
    inline, and a scanner that only looked at call targets would miss exactly
    the paths most likely to skip a gate.
    """
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("_vendor/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in _WRITE_PRIMITIVES:
                    found.add(f"{rel}::{node.name}")
    return found


def test_every_persistent_writer_is_classified() -> None:
    """A new write site must be gated or explicitly exempted, never neither."""
    classified = GATED_AUTOMATIC | set(EXEMPT)
    unclassified = sorted(site for site in _writer_sites() if site not in classified)
    assert not unclassified, (
        "New persistent-memory write site(s) that are neither gated by "
        "memory.persistence_enabled nor listed as exempt:\n  "
        + "\n  ".join(unclassified)
        + "\n\nAn automatic writer must check the switch (see "
        "history_consolidation._persistence_disabled or "
        "taskrunner._extract_lesson) and be added to GATED_AUTOMATIC. A writer "
        "that legitimately runs while persistence is off (an explicit user "
        "edit, a storage primitive, a test harness) goes in EXEMPT with its "
        "reason. Do NOT add an entry to silence this without deciding which "
        "one it is."
    )


def test_the_register_has_no_stale_entries() -> None:
    """Every classified site must still exist in the source.

    Without this the register accumulates dead names, and a genuinely new writer
    can then collide with one and be waved through.
    """
    sites = _writer_sites()
    stale = sorted(site for site in (GATED_AUTOMATIC | set(EXEMPT)) if site not in sites)
    assert (
        not stale
    ), "Register entries with no matching write site (renamed or deleted):\n  " + "\n  ".join(stale)


@pytest.mark.parametrize("site", sorted(GATED_AUTOMATIC))
def test_a_gated_writer_names_the_switch(site: str) -> None:
    """Each gated writer's module reads the config key, so the gate is real.

    A module-level check keeps this cheap and stable: proving the refusal
    behaviour is test_memory_context_toggles.py's job, but a module that stopped
    mentioning the key at all has lost its gate.
    """
    module = SRC / site.split("::", 1)[0]
    assert "persistence_enabled" in module.read_text(encoding="utf-8"), (
        f"{site} is registered as gated, but {module.name} never reads "
        "memory.persistence_enabled"
    )
