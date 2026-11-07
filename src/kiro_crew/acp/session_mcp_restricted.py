"""Which servers a backend with no per-tool deny channel must withhold.

Imported by :mod:`kiro_crew.acp.session_mcp` under its own name, so callers reach it
as ``session_mcp.session_mcp_restricted_servers``.
"""

from __future__ import annotations

from collections.abc import Collection

from kiro_crew.mcp_cleanup import CONTROL_PLANE_SERVERS


def session_mcp_restricted_servers(disabled_tools: Collection[tuple[str, str]]) -> frozenset[str]:
    """Servers whose per-TOOL narrowing no transport can carry as an element.

    The sibling of :func:`session_mcp_deny_rules`, for a backend that has no file
    to put deny rules in. Same input, same reason to exist: ``disabledTools`` is a
    RESTRICTION, and a backend that forwards the server while dropping it widens
    the session's tool surface behind the user's back -- the dashboard writes that
    key on an ordinary tool-off action, and this repo already treats losing it as a
    defect ("dropping ``disabledTools`` on a save would silently widen the agent's
    tool surface").

    Claude re-applies the restriction as ``permissions.deny`` rules and so may keep
    the server. A backend with no deny channel has only one faithful option, which
    is to not mount the server at all -- so this returns the NAMES and lets that
    backend omit them. Withholding a server is an availability cost; forwarding an
    un-narrowed one is a capability the user switched off.

    **Crew's own control plane is exempt from WITHHOLDING, not from the
    restriction.** ``kirocrew-core`` / ``kirocrew-cron`` are re-derived from
    ``managed_mcp_spec_entry``, which emits only command/args/env, so a
    ``disabledTools`` on their spec entry never reaches the element -- and
    withholding the whole server on the strength of it would leave the session
    unable to report back to its channel at all, which is the exact defect this
    module exists to fix. The restriction itself is still honoured, on the one
    channel this transport does have: every call to one of these servers reaches
    Crew as a ``session/request_permission`` (their tools carry no annotations, so
    codex prompts for each), and the client answers a call to a switched-off tool
    with the adapter's reject option before anything runs. The pairs it checks
    come from :func:`session_mcp_disabled_tools`, on the same parse as this set.
    That channel is complete for the control plane and NOT for a third-party
    server -- a tool annotated ``readOnlyHint`` is auto-approved inside codex and
    never prompts -- which is why the two are treated differently here rather
    than both being mounted.

    ``disabled`` is deliberately NOT part of this. A disabled server is already
    absent from the array by a different mechanism: ``agent.build_agent_config``
    strips its ``@alias`` from ``tools``, and the allowlist filter in
    :func:`session_mcp_servers` mounts nothing ``tools`` does not name. Re-checking
    it here would be a second spelling of a rule that already holds.

    Derived from the ``(server, tool)`` PAIRS :func:`session_mcp_disabled_tools`
    resolved, never from the spec on its own, so the two decisions a restriction
    drives -- withhold the server, or refuse the call -- read the same sources. The
    dashboard's tool-off action writes to the global settings file and not to the
    spec, so a withhold set read from the spec alone would miss a third-party server
    narrowed the ordinary way; that server would mount, and the per-call refusal
    cannot reach it (a tool annotated ``readOnlyHint`` is approved inside codex
    without asking), so the switched-off tool would run. Its one caller is
    :func:`session_mcp_projection`, which hands in the pairs it already resolved
    from one read of each source. A server named only in the global file and not in
    the spec lands here too and is harmless: nothing mounts it. Free of I/O.
    """
    return frozenset(server for server, _tool in disabled_tools) - frozenset(CONTROL_PLANE_SERVERS)
