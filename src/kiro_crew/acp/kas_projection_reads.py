"""Config reads the KAS agent projection makes (:mod:`kiro_crew.acp.kas_agents`).

Split out of that module for its line cap; it imports every name back, so
``kas_agents._registry_governed`` stays the name callers and tests patch.
"""

from __future__ import annotations


def _registry_governed() -> bool:
    """Whether the operator declared this install registry-governed.

    Deferred, and reaching for a private name on purpose: this has to be the SAME
    answer :mod:`kiro_crew.acp.session_mcp` acts on rather than a second reading
    of the same config, because two readings can disagree and a ceiling that
    disagrees with itself is not a ceiling. That module's wrapper also fixes the
    direction an unreadable config is read in (governed, not ungoverned), which is
    the half most easily got backwards. The import is call-time because it pulls
    :mod:`kiro_crew.agent` and the config loader in behind it, which this
    projection leaf stays off (see :data:`MANAGED_MCP_SERVER_NAMES`) — the same
    reason ``_MEMBER_DASHBOARD_GRANTS`` is imported where it is used.

    Not on the event loop, and not by luck: the one production route into this
    module is ``KasHarness.session_extras``, which builds the whole payload inside
    ``await asyncio.to_thread(_build)``. A config read here costs that thread, not
    every other session's turn — the same place ``require_fresh_derived_spec`` and
    the ceiling's ``may_skip_gate_now`` already read from.
    """
    from kiro_crew.acp.session_mcp import _registry_mode

    return _registry_mode()


def _gated_off_managed_servers() -> frozenset[str]:
    """Kiro Crew's managed servers whose ``spec_gate`` is closed right now.

    The snapshot both spec writers take (``agent._gated_off_servers``), so the
    projection withholds exactly what a fresh build would, and a gate that raises
    reads as closed here too: declaring the entry is what makes the engine launch
    the backend. Deferred for the reason :func:`_registry_governed` gives, and
    read on the same off-loop route.
    """
    from kiro_crew.agent import _gated_off_servers

    return _gated_off_servers()


def _relayed_env_references() -> frozenset[str]:
    """The variable names the edition lets a KAS MCP entry reference.

    Read through ``getattr`` so an edition provider that predates the method
    keeps working and relays nothing. Non-string members are dropped.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    def _read() -> frozenset[str]:
        reader = getattr(current_context().mcp_tooling, "kas_relayed_env_references", None)
        names = reader() if callable(reader) else ()
        return frozenset(n for n in names if isinstance(n, str))

    return safe_context_call(
        _read,
        fallback_factory=frozenset,
        log_message="kas_relayed_env_references lookup failed; relaying no env references",
    )
