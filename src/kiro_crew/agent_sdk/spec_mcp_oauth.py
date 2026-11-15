"""The remote MCP ``oauth`` blocks a session's agent spec sets that its harness drops.

Only kiro-cli honours the block (``ACP_BACKENDS_REMOTE_MCP_OAUTH``). KAS names what
it drops through :func:`kiro_crew.agent_sdk.spec_hooks.crew_fired_spec_hooks`; this
leaf answers the same question for every other harness, for the session-start
notice (``dashboard.chat_turn.mcp_oauth_notice``).
"""

from __future__ import annotations


def spec_mcp_oauth_dropped(
    agent_id: str, project_dir: str | None, *, backend: str, session_key: str = ""
) -> list[str]:
    """The ``mcpServers.<name>.oauth`` keys of the active agent spec a session on
    *backend* runs without, for ``spec_hooks.spec_keys_notice``.

    For a backend outside ``ACP_BACKENDS_REMOTE_MCP_OAUTH`` that is not KAS. The
    servers are those the KAS notice would name
    (``mcp_oauth_projection.servers_with_discarded_oauth``: declared, not muted, not
    withheld by registry mode), less any whose sign-in Crew serves through its own
    bridge (``kas_env_launch.crew_connection_slug``), since that one still signs in.
    Read through the spec reader the hooks use, so it is the spec the session runs,
    with the checkout spec's switch-offs layered on as the session's MCP projection
    layers them (``session_mcp._with_restrictions``), so a server the checkout mutes
    is not named. Blocking; raises when the spec cannot be read. *project_dir* is
    ``spec_hooks.spec_project_dir`` of the session. *session_key* reads a
    crewmate's worker as that session's merged spec, as the hooks do.
    """
    # Call-time imports: the ACP layer imports the config loader, which sits below
    # these modules. `_agent_spec`, `_registry_governed` and the restriction layer are
    # reached for on purpose, so this reads the spec and registry verdict the session acts on.
    from kiro_crew.acp.kas_env_launch import crew_connection_slug
    from kiro_crew.acp.kas_projection_reads import _registry_governed
    from kiro_crew.acp.session_mcp import (
        _project_restrictions,
        _with_restrictions,
        project_agent_spec,
    )
    from kiro_crew.agent_sdk.spec_hooks import _agent_spec
    from kiro_crew.mcp_oauth_projection import oauth_lost_keys, servers_with_discarded_oauth

    spec = _agent_spec(agent_id, project_dir, session_key)
    _declared, project = project_agent_spec(agent_id, project_dir)
    if project:
        # Only narrows: a mute or switched-off tools, never a server or a key that mounts.
        spec = _with_restrictions(spec, _project_restrictions(project))
    servers = spec.get("mcpServers") or {}
    named = set(servers_with_discarded_oauth(spec, _registry_governed))
    # Each name is read off the spec's own keys, in spec order, which is the order
    # `servers_with_discarded_oauth` keeps.
    return oauth_lost_keys(
        name
        for name, entry in servers.items()
        if name in named and not crew_connection_slug(name, entry, backend)
    )
