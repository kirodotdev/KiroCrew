"""Per-test reset of the process-global crewmate worker and Bot seat registries.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_crewmate_worker_slots():
    """A test that registers a session key as a crewmate worker must not leak it.

    ``_CREWMATE_WORKER_SLOTS``, ``_CREWMATE_WORKER_MEMBERS``,
    ``_CREWMATE_WORKER_PROMPT_IN_CONTEXT`` and ``_SESSION_BACKEND_PICKS`` are
    process-wide in ``kiro_crew.members_parts.member_sessions``, and so is the
    thread-born mark in ``kiro_crew.members_parts.worker_conducts`` and the Bot seat
    map in ``kiro_crew.dashboard.state_parts.bot_worker_cap``.
    ``note_crewmate_worker_session`` mutates them directly (not through a
    monkeypatch), so a test that calls ``resolve_session_agent_bindings`` or
    ``note_worker_session`` with a worker execution silently contaminates every
    later test on the same xdist worker that calls ``get_or_create`` on the same
    key: the allocation's crewmate-worker gate fires and raises
    ``CapabilityStartupError("capability_harness_unsupported")`` against a plain
    mock provider that never set ``mate_worker_kit_supported = True``.
    """

    def clear():
        module = sys.modules.get("kiro_crew.members_parts.member_sessions")
        if module is not None:
            module._CREWMATE_WORKER_SLOTS.clear()
            module._CREWMATE_WORKER_MEMBERS.clear()
            module._CREWMATE_WORKER_PROMPT_IN_CONTEXT.clear()
            module._SESSION_BACKEND_PICKS.clear()
        marks = sys.modules.get("kiro_crew.members_parts.worker_conducts")
        if marks is not None:
            marks._THREAD_BORN_WORKER_SLOTS.clear()
            marks._LOGGED_PLAIN.clear()
        seats = sys.modules.get("kiro_crew.dashboard.state_parts.bot_worker_cap")
        for table in ("_BOT_SESSIONS", "_RECORDS_READ"):
            getattr(seats, table, set()).clear()

    clear()
    try:
        yield
    finally:
        clear()
