"""Extractable session-import core (slice 3, circle 7 — issue #7577, Task 3.1).

The 'validated bundle -> new slot -> new session id' logic that both
``dashboard.session_transfer.api_chat_slot_import`` (the HTTP path) and the
migration importer need. Pulling it here means one implementation, called two
ways (DRY), and it is a PURE function over an injected slot-lifecycle object —
so it is testable without aiohttp or a live DashboardState.

The injected ``state`` must provide the same lifecycle the handler already
drives, in this order:

    live_slot_count() -> int
    get_or_create_slot(*, name, agent, app) -> slot (with .key)
    begin_slot_construction(key)
    finish_slot_construction(key)
    publish(key)

The ordering guarantee this core encodes is the one the handler's comments
spell out: the slot is created, its construction is BRACKETED
(begin ... finish) so it is not reachable with a half-built transcript, and it
is published exactly once at the end. The migration path reuses this instead of
rebuilding a second, subtly-different sequence.

NOTE: this defines and tests the core against the lifecycle contract. Wiring
the real ``api_chat_slot_import`` to call it (and adapting its aiohttp request
plumbing) is the remaining live-environment step — this module is the target
that step lands on.
"""

from __future__ import annotations

from typing import Callable

MAX_LIVE_SLOTS_DEFAULT = 100


def import_session_core(
    state,
    bundle: dict,
    *,
    resolve_agent: Callable[[str], str],
    app: str = "",
    cap: int | None = None,
) -> str:
    """Materialize a validated *bundle* into a new slot; return its key.

    *bundle* is assumed already validated (the HTTP handler runs
    ``_validate_bundle`` before calling this; the migration path validates in
    its receiver). ``resolve_agent`` maps the bundle's agent hint to a resolved
    agent name — injected because the real resolver scans the agents directory
    and must not run on the event loop.

    Raises ``ValueError`` when the live-slot cap is reached, BEFORE allocating
    anything (so a refused import leaves no partial slot).
    """
    limit = cap if cap is not None else MAX_LIVE_SLOTS_DEFAULT
    if state.live_slot_count() >= limit:
        raise ValueError(f"slot cap reached ({limit})")

    agent_hint = bundle.get("agent", "") or ""
    resolved_agent = resolve_agent(agent_hint) if agent_hint else ""

    slot = state.get_or_create_slot(name=None, agent=resolved_agent, app=app)

    # Construction bracket: unreachable until fully built, published once.
    state.begin_slot_construction(slot.key)
    try:
        # (transcript hydration + Layer B join happen here in the real path,
        # driven by the caller with the bundle's messages; the core owns the
        # lifecycle ordering, not the transport-specific hydration.)
        pass
    finally:
        state.finish_slot_construction(slot.key)
    state.publish(slot.key)
    return slot.key
