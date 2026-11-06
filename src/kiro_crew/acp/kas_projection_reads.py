"""Config reads the KAS agent projection makes (:mod:`kiro_crew.acp.kas_agents`).

Split out of that module for its line cap; it imports both names back, so
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


def _http_loopback_only() -> bool:
    """``agent.kas_mcp_http_loopback_only``: bridge a plain ``http`` url only to a
    loopback host. Off by default, so a header-bearing ``http`` server on another
    host keeps running through the bridge as it does today.

    Read live, and only for a plain ``http`` url to another host, so most
    projections never pay for it. An unreadable or degraded config reads as ON:
    an owner who turned the refusal on must not lose it to a broken file.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001
        return True
    degraded = cfg.degraded_sections
    if (
        cfg._base_unreadable
        or cfg._overlay_unreadable
        or "agent" in degraded
        or DEGRADED_WHOLE_CONFIG in degraded
    ):
        return True
    return cfg.agent.kas_mcp_http_loopback_only is True
