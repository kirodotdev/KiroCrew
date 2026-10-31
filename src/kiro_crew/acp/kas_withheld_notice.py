"""The session-start notice for MCP servers a KAS session starts without part of their config.

KAS takes the agent spec over the ACP wire, so the projection
(:func:`kiro_crew.acp.kas_agents._project_mcp_servers`) withholds every ``env``
value it cannot prove is not a credential, and every ``headers`` block. The server
is still declared and still starts, so a server that needs one of those settings
fails with no visible cause; before this notice the only trace was an INFO log
line. The gateway rewriter leaves a credentialed server unwrapped when a shared
backend would withhold its env, expecting the session to apply it, which is the
case a KAS session cannot honour.

Names only: a key name passes through
:func:`kiro_crew.acp.kas_agents._loggable_key_names`, so a credential-shaped NAME is
counted rather than printed, and no value is ever read here.
"""

from __future__ import annotations

from kiro_crew.acp.kas_agents import WithheldOut, _loggable_key_names


def withheld_credentials_notice(withheld: WithheldOut) -> str:
    """The notice for *withheld*, or ``""`` when nothing was withheld.

    *withheld* is what :func:`kiro_crew.acp.kas_agents.build_kas_custom_agents`
    collected: per server, the withheld ``env`` key names and the fields
    (``env``, ``headers``) anything went from.
    """
    lines = []
    for name in sorted(withheld):
        env_keys, fields = withheld[name]
        parts = []
        if "env" in fields:
            parts.append(f"env {_loggable_key_names(list(env_keys))}" if env_keys else "its env")
        if "headers" in fields:
            parts.append("headers")
        if not parts:
            continue
        server = name if _loggable_key_names([name]) == name else "(unnamed)"
        lines.append(f"• {server}: {' and '.join(parts)}")
    if not lines:
        return ""
    return (
        "ℹ This session runs on the KAS backend, which receives MCP servers over the "
        "ACP connection, and Crew does not send credentials over it. These servers "
        "start without the settings named below, so they may fail to authenticate "
        "or miss a setting:\n"
        + "\n".join(lines)
        + "\nTo start a server with its full config, route it through the gateway: "
        "store each secret in the vault, reference it as `secret://` in `env`, and "
        "turn routing on for the server in Developer → MCP Management. The gateway "
        "then starts it on this machine with its whole declared env."
    )
