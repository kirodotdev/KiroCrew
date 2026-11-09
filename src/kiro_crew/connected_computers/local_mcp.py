"""Local machine: the local MCP servers a grant may expose, as the user confirmed them.

Only servers the user ticked in the native consent are exposed, each snapshotted
(command, arguments, environment) at that moment, so a later edit of the local
MCP config cannot widen what a remote gateway may start. Each one runs under the
same OS confinement as the workspace tools, on the same root (:mod:`.confinement`).

What is refused, with a reason the consent shows, rather than exposed (B14):

* a remote (HTTP) server -- it does not act on this computer, and its traffic
  would carry this computer's credentials to a third party for the gateway;
* a server whose environment reads from the vault (``secret://``) -- that secret
  would be handed to a process the remote gateway drives;
* Kiro Crew's own servers -- they act on the local gateway, not in the folder.

A server with some tools turned off locally (``disabledTools``) IS exposed: the
list is snapshotted too, and the connector keeps those tools off on this
computer (:mod:`.mcp_filter`), whatever the remote gateway sends. Each start
also re-reads the local config (:func:`live_limits`), which can only narrow.

Exposing nothing is the default. Refusing every server is not how the feature is
met: a plain stdio server the user ticks is exposed.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from kiro_crew.connected_computers.local_store import ExposedMcp
from kiro_crew.connected_computers.mcp_filter import MAX_DISABLED_TOOLS
from kiro_crew.connected_computers.protocol import MAX_EXPOSED_MCP, MCP_NAME_RE


class McpExposureRefused(ValueError):
    """A ticked server cannot be exposed; the message names it and says why."""


def _refusal(info: Any) -> str:
    name = str(getattr(info, "name", ""))
    if not MCP_NAME_RE.match(name):
        return "its name cannot be exposed"
    if name.startswith("kirocrew"):
        return "it is one of Kiro Crew's own servers"
    if getattr(info, "disabled", False) is not False:
        return "it is turned off on this computer"
    off = getattr(info, "disabled_tools", None) or []
    if not isinstance(off, (list, tuple)) or not all(isinstance(t, str) for t in off):
        return "its list of tools turned off on this computer is unreadable"
    if len(set(off)) > MAX_DISABLED_TOOLS:
        return f"it has more than {MAX_DISABLED_TOOLS} tools turned off on this computer"
    if getattr(info, "url", "") or not getattr(info, "command", ""):
        return "it is a remote server, not a program on this computer"
    env = getattr(info, "env", None) or {}
    if not isinstance(env, dict):
        return "its environment is unreadable"
    if any(isinstance(v, str) and v.startswith("secret://") for v in env.values()):
        return "it reads a saved secret, which would be handed to the remote gateway"
    return ""


def exposable(servers: Iterable[Any]) -> list[dict[str, str]]:
    """Every configured server with whether it can be exposed: what the consent lists."""
    out = []
    for info in servers:
        why = _refusal(info)
        out.append({"name": str(getattr(info, "name", "")), "refused": why})
    return out


def snapshot(names: Iterable[str], servers: Iterable[Any]) -> tuple[ExposedMcp, ...]:
    """The ticked ``names`` as :class:`ExposedMcp`; raise for any that cannot be exposed."""
    wanted = list(dict.fromkeys(names))
    if len(wanted) > MAX_EXPOSED_MCP:
        raise McpExposureRefused(f"at most {MAX_EXPOSED_MCP} local MCP servers can be shared")
    by_name = {str(getattr(s, "name", "")): s for s in servers}
    out = []
    for name in wanted:
        info = by_name.get(name)
        if info is None:
            raise McpExposureRefused(f"{name}: no local MCP server has that name")
        why = _refusal(info)
        if why:
            raise McpExposureRefused(f"{name}: {why}")
        env = getattr(info, "env", None) or {}
        out.append(
            ExposedMcp(
                name=name,
                command=str(info.command),
                args=tuple(str(a) for a in (getattr(info, "args", None) or ())),
                env=tuple(sorted((str(k), str(v)) for k, v in env.items())),
                disabled_tools=tuple(sorted(set(getattr(info, "disabled_tools", None) or ()))),
            )
        )
    return tuple(out)


def live_limits(exposed: ExposedMcp, servers: Iterable[Any]) -> Optional[tuple[str, ...]]:
    """The tools to keep off when ``exposed`` starts now; None when it must not start.

    Read at each start, so a later local change can only NARROW what was
    shared: a tool turned off locally since joins the snapshot's list, one
    turned back on stays off until the user shares again, and a server removed,
    turned off or not exposable here does not start at all.
    """
    info = {str(getattr(s, "name", "")): s for s in servers}.get(exposed.name)
    if info is None or _refusal(info):
        return None
    now_off = getattr(info, "disabled_tools", None) or ()
    merged = set(exposed.disabled_tools) | {str(t) for t in now_off}
    if len(merged) > MAX_DISABLED_TOOLS:
        return None
    return tuple(sorted(merged))


def configured_servers() -> list[Any]:
    """This machine's configured MCP servers (``mcp_discovery.list_servers``)."""
    from kiro_crew.mcp_discovery_config import list_servers

    return list(list_servers())
