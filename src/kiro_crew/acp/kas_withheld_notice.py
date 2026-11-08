"""The session-start notice for MCP servers a KAS session starts without part of their config.

KAS takes the agent spec over the ACP wire, so the projection
(:func:`kiro_crew.acp.kas_agents._project_mcp_servers`) withholds every ``env``
value it cannot prove is not a credential, and every ``headers`` block. A stdio
server's env and a streamable-HTTP server's headers then reach it through Crew's
local launcher, so what is left is a server that launcher did not carry. Every
such server keeps the projection's INFO log line, which names the field.

The same notice names servers that will not run at all: in MCP registry mode,
every server Crew withholds, and Crew's own, which KAS drops when the Kiro profile
is registry-governed too (KAS keeps only admin-catalog servers then); and a plain
``http`` url to another host (KAS refuses it as ``insecure-url``).

For a server that does run, the chat gets a notice only when the user can fix it:
a stdio server Crew does not ship that lost env. Turning on Share for it in MCP
Management hands it to the gateway, which starts it on this machine with its whole
env. A server Share alone would not fix (a lost key the gateway does not forward),
a remote server the bridge cannot carry (an ``sse`` one), and a server Crew or the
edition ships are left to the log line, so the chat never asks for a step its user
cannot take there. That line names no config term: the server, the Share switch
and where it is.

Names only: a server name passes through
:func:`kiro_crew.acp.kas_env_literals._loggable_key_names`, so a credential-shaped
NAME is never printed, and no value is ever read here.
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
    """The notice for *withheld*, or ``""`` when the user has nothing to act on.

    *withheld* is what :func:`kiro_crew.acp.kas_agents.build_kas_custom_agents`
    collected: per server, the withheld ``env`` key names, the fields (``env``,
    ``headers``, or a not-run reason) anything went from, and whether it is a
    stdio server.
    """
    return "\n\n".join(
        part for part in (_not_run_notice(withheld), _share_notice(withheld)) if part
    )


def _share_notice(withheld: WithheldOut) -> str:
    """The one line naming servers that turning on Share would fix, or ``""``."""
    from kiro_crew.agent import crew_owned_mcp_servers

    shipped = crew_owned_mcp_servers()
    names = sorted(
        name
        for name, (keys, fields, stdio) in withheld.items()
        if stdio and "env" in fields and name not in shipped and not _share_withholds(keys)
    )
    if not names:
        return ""
    shown = [n for n in names if _loggable_key_names([n]) == n]
    hidden = len(names) - len(shown)
    label = ", ".join(shown + ([f"{hidden} more"] if hidden else []))
    return (
        f"ℹ {label} may be missing some of its settings in this chat. To fix it, "
        "turn on Share for it in Customize → Connections → MCP Management."
    )


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


def _share_withholds(env_keys: tuple[str, ...]) -> bool:
    """Whether Share alone would still leave a server without part of *env_keys*.

    A server whose declared env a shared backend would not fully receive stays
    unwrapped unless the value is a vault reference. The rewriter's own count
    answers that (``mcp_gateway.rewriter_env._withheld_env_count``), with the same
    forwarding setting and pool-identity keys it reads, so the notice adds the
    Secrets step exactly when Share needs it. No key names known (a malformed env)
    also gets that step.
    """
    from kiro_crew.mcp_gateway.rewriter_env import (
        _withheld_env_count,
        forward_declared_env_enabled,
        pool_identity_env_keys,
    )

    if not env_keys:
        return True
    env = dict.fromkeys(env_keys, "")
    return _withheld_env_count(env, forward_declared_env_enabled(), pool_identity_env_keys()) > 0
