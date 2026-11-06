"""The session-start notice for MCP servers a KAS session starts without part of their config.

KAS takes the agent spec over the ACP wire, so the projection
(:func:`kiro_crew.acp.kas_agents._project_mcp_servers`) withholds every ``env``
value it cannot prove is not a credential, and every ``headers`` block. A stdio
server's env and a streamable-HTTP server's headers then reach it through Crew's
local launcher, so only what that launcher cannot carry is named. The server
is still declared and still starts, so a server that needs one of those settings
fails with no visible cause; before this notice the only trace was an INFO log
line. The gateway rewriter leaves a credentialed server unwrapped when a shared
backend would withhold its env, expecting the session to apply it, which is the
case a KAS session cannot honour.

The same notice names servers that will not run at all: in MCP registry mode,
every server Crew withholds, and Crew's own, which KAS drops when the Kiro profile
is registry-governed too (KAS keeps only admin-catalog servers then); and a plain
``http`` url to another host (KAS refuses it as ``insecure-url``).

Names only: a key name passes through
:func:`kiro_crew.acp.kas_env_literals._loggable_key_names`, so a credential-shaped NAME is
counted rather than printed, and no value is ever read here.
"""

from __future__ import annotations

from kiro_crew.acp.kas_agents import (
    NOT_RUN_INSECURE_URL,
    NOT_RUN_REGISTRY,
    NOT_RUN_REGISTRY_IF_GOVERNED,
    WithheldOut,
)
from kiro_crew.acp.kas_env_literals import _loggable_key_names


def withheld_credentials_notice(withheld: WithheldOut) -> str:
    """The notice for *withheld*, or ``""`` when nothing was withheld.

    *withheld* is what :func:`kiro_crew.acp.kas_agents.build_kas_custom_agents`
    collected: per server, the withheld ``env`` key names and the fields
    (``env``, ``headers``) anything went from.
    """
    not_run = _not_run_notice(withheld)
    lines = []
    routable = remote = False
    for name in sorted(withheld):
        env_keys, fields, stdio = withheld[name]
        if _NOT_RUN.intersection(fields):
            continue
        routable = routable or (stdio and "env" in fields)
        remote = remote or not stdio
        parts = []
        if "env" in fields:
            parts.append(f"env {_loggable_key_names(list(env_keys))}" if env_keys else "its env")
        if "headers" in fields:
            parts.append("headers")
        if not parts:
            continue
        server = _server_label(name)
        lines.append(f"• {server}: {' and '.join(parts)}")
    if not lines:
        return not_run
    return "\n\n".join(
        part
        for part in (
            not_run,
            "ℹ This session runs on the KAS backend, which receives MCP servers over the "
            "ACP connection, and Crew does not send credentials over it. These servers "
            "start without the settings named below, so they may fail to authenticate "
            "or miss a setting:\n"
            + "\n".join(lines)
            + (_STDIO_REMEDY if routable else "")
            + (_REMOTE_LIMIT if remote else ""),
        )
        if part
    )


_NOT_RUN = frozenset({NOT_RUN_REGISTRY, NOT_RUN_REGISTRY_IF_GOVERNED, NOT_RUN_INSECURE_URL})


def _server_label(name: str) -> str:
    return name if _loggable_key_names([name]) == name else "(unnamed)"


def _not_run_notice(withheld: WithheldOut) -> str:
    """The part naming servers the KAS backend will not run at all, or ``""``."""

    def named(reason: str) -> list[str]:
        return sorted(n for n, (_, f, _s) in withheld.items() if reason in f)

    registry = named(NOT_RUN_REGISTRY)
    own = named(NOT_RUN_REGISTRY_IF_GOVERNED)
    insecure = named(NOT_RUN_INSECURE_URL)
    parts = []
    if registry or own:
        lines = ["⚠ MCP registry mode is on (`agent.mcp_registry_mode`)."]
        if registry:
            lines.append(
                "Crew does not send these servers to the KAS backend, so they will not "
                "run in this session:\n" + "\n".join(f"• {_server_label(n)}" for n in registry)
            )
        if own:
            lines.append(
                "If your Kiro profile is registry-governed, KAS runs only servers from "
                "your admin's registry, so Crew's own servers will not run either:\n"
                + "\n".join(f"• {_server_label(n)}" for n in own)
            )
        lines.append(
            "If your Kiro profile is not registry-governed, run "
            "`kirocrew config set agent.mcp_registry_mode false` and start a new session."
        )
        parts.append("\n".join(lines))
    if insecure:
        parts.append(
            "⚠ The KAS backend connects only to `https://` urls or `http://` on this "
            "machine (localhost), so these servers will not run in this session:\n"
            + "\n".join(f"• {_server_label(n)}" for n in insecure)
            + "\nChange each url to `https://` to use it here."
        )
    return "\n\n".join(parts)


#: The way out for a stdio server that lost env: the gateway starts a routed server
#: on this machine with its whole declared env, so nothing crosses the wire.
_STDIO_REMEDY = (
    "\nTo start a local (command) server with its full env, route it through the "
    "gateway: store each secret in the vault, reference it as `secret://` in `env`, "
    "and turn routing on for the server in Developer → MCP Management. The gateway "
    "then starts it on this machine with its whole declared env."
)

#: A remote server named here is one the local bridge does not carry (an ``sse``
#: entry, :func:`kiro_crew.acp.kas_env_launch.bridgeable`), and it has no gateway
#: route either: the gateway routes only servers it launches, and ``secret://`` is
#: resolved only in a stdio server's ``env``.
_REMOTE_LIMIT = (
    "\nA remote (url) server the local bridge cannot carry, such as an `sse` one, "
    "cannot be routed through the gateway either, so on this backend it starts "
    "without the headers and env named above, and nothing on Crew's side can "
    "supply them."
)
