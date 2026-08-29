"""Session-scoped MCP server injection for the shared gateway.

kiro-cli's ACP ``session/new`` accepts an ``mcpServers`` array, and a
session-injected server takes precedence over the same-named entry in the
resolved agent spec: the spec's own copy is never launched. That makes pooling
a protocol-level operation. The broker stubs replace an agent's poolable
servers for the lifetime of one session, and nothing is written to the user's
project, to their ``~/.kiro/agents/``, or through a bind mount — so pooling
works with ``agent.sandbox`` set to ``off`` (the default) and on macOS and
Windows, neither of which can bind-mount.

The first-class Kiro path injects only broker stubs. Adapted harnesses that do
not read Kiro agent specs may additionally request a portable projection of
Kiro Crew's managed servers. That projection is intentionally narrower than the
whole spec: only a server with a full ``@server`` exposure and no disabled-tool
filter is representable without widening its tool set.

Precedence caveat: same-name override is verified against the shipped binary
(``test_mcp_gateway_session_inject.py`` pins it, including a live check when
kiro-cli is on PATH) but is NOT documented by kiro-cli. The documented
hierarchy covers only the three *file* tiers (agent config > workspace
``mcp.json`` > global ``mcp.json``). If a future release made injection purely
additive, an agent's own copy would launch alongside the stub, which is worse
than not pooling — hence the pinning test.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY

logger = logging.getLogger(__name__)

# Keys that are positional in the ACP element shape (``name``) or that we
# always re-derive (``env``), so they must not be copied verbatim.
_ACP_RESERVED = frozenset({"name", "env", _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY})
_PORTABLE_ACP_KEYS = ("name", "command", "args", "env")


def _acp_env(raw: Any) -> list[dict[str, str]]:
    """Convert a kiro-agent-JSON ``env`` mapping to ACP's array-of-pairs form.

    Stub entries always carry an empty mapping, so this normally returns ``[]``.
    It is still a faithful conversion rather than a hardcoded empty list so a
    future caller that injects a non-stub entry cannot silently drop its env.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def _acp_server_entry(
    name: str, entry: dict[str, Any], channel_id: str | None = None
) -> dict[str, Any] | None:
    """Shape one rewritten ``mcpServers`` entry into an ACP array element.

    Operator-set passthrough keys (``timeout``, ``type``, ``disabledTools``,
    ``autoApprove``, vendor keys) are preserved: kiro-cli tolerates them on the
    session-injected element, and dropping ``autoApprove`` in particular would
    re-prompt for tools the agent spec had already auto-approved.

    ``channel_id`` is APPENDED as ``--channel-id <value>`` rather than
    prepended: the overlay entry runs the interpreter, so ``args`` opens with
    ``-m kiro_crew.mcp_gateway.stub`` and anything inserted ahead of that would
    be eaten by the interpreter instead of the stub. argparse does not care
    about order. The channel value is known here, at the one place that runs
    per session, so the stub does not need to recover it by walking its
    ancestors' ``/proc/<pid>/environ`` from a bash launcher.
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # A stub without a command cannot be launched; injecting it would
        # shadow the agent's working entry with a broken one. Skip instead,
        # leaving the spec's own server in place.
        return None
    args = [
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
        for a in (entry.get("args") or [])
    ]
    if channel_id and "--channel-id" not in args:
        args.extend(["--channel-id", channel_id])
    shaped: dict[str, Any] = {
        k: v for k, v in entry.items() if k not in _ACP_RESERVED and k != "command"
    }
    shaped.update(
        {
            "name": name,
            "command": command,
            "args": args,
            "env": _acp_env(entry.get("env")),
        }
    )
    return shaped


def _portable_acp_server_entry(
    name: str, entry: dict[str, Any], channel_id: str | None = None
) -> dict[str, Any] | None:
    """Return only fields defined by the portable ACP local-server shape.

    Kiro tolerates extensions such as ``autoApprove`` and ``disabledTools`` in a
    session entry. Adapted harnesses may validate the SDK schema strictly, so
    those Kiro-only controls must not cross this boundary.
    """
    shaped = _acp_server_entry(name, entry, channel_id)
    if shaped is None:
        return None
    return {key: shaped[key] for key in _PORTABLE_ACP_KEYS}


def _managed_servers_from_spec(
    spec: dict[str, Any], managed_names: frozenset[str]
) -> list[dict[str, Any]]:
    """Project safely representable managed servers from one rendered spec.

    A whole ACP server exposes every tool it reports. Therefore an exact
    ``@server`` reference is required; a hand-narrowed ``@server/tool`` grant
    cannot be translated without widening it. Likewise, any disabled-tool
    filter causes the whole server to be withheld. Losing tools is preferable
    to silently restoring a tool the operator or governance layer removed.
    """
    servers = spec.get("mcpServers")
    tools = spec.get("tools")
    if not isinstance(servers, dict) or not isinstance(tools, list):
        return []
    exposed = {item for item in tools if isinstance(item, str)}
    out: list[dict[str, Any]] = []
    for name in sorted(managed_names):
        entry = servers.get(name)
        if not isinstance(entry, dict) or f"@{name}" not in exposed:
            continue
        if entry.get("disabled") is True or entry.get("enabled") is False:
            continue
        if entry.get("disabledTools"):
            continue
        shaped = _portable_acp_server_entry(name, entry)
        if shaped is not None:
            out.append(shaped)
    return out


def managed_session_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return portable managed MCP entries for an adapted ACP harness.

    The rendered agent spec remains the authority for whether a managed server
    exists and is fully exposed. When pooling rewrote an eligible server, its
    broker stub replaces the direct entry by name. User-installed servers are
    deliberately excluded: their per-tool narrowing, credentials, and remote
    transport extensions cannot all be represented by the portable local-server
    shape without broadening access.

    Fail-soft to an empty list, preserving a usable chat session when a spec is
    missing or malformed.
    """
    if not agent:
        return []
    # Lazy import avoids the agent -> MCP rewriter -> session-server cycle at
    # module import time. The agent module owns both the immutable name set and
    # the hardened materialized-spec reader.
    from kiro_crew.agent import MANAGED_MCP_SERVER_NAMES, materialized_agent_spec

    spec = materialized_agent_spec(agent)
    if not isinstance(spec, dict):
        return []
    direct = _managed_servers_from_spec(spec, MANAGED_MCP_SERVER_NAMES)
    by_name = {entry["name"]: entry for entry in direct}
    if not by_name:
        return []
    for pooled in pooled_session_servers(overlay_dir, agent, channel_id):
        name = pooled.get("name")
        if not isinstance(name, str) or name not in by_name:
            continue
        portable = _portable_acp_server_entry(name, pooled)
        if portable is not None:
            by_name[name] = portable
    return [by_name[name] for name in sorted(by_name)]


def _load_overlay_for_agent(overlay_dir: Path, agent: str) -> dict[str, Any] | None:
    """Locate the rewritten overlay spec for *agent*, or ``None``.

    Package-installed agents are written to the overlay directory under a
    package-qualified filename (e.g. ``Pkg-gpu-dev.json``) while the session
    requests them by bare name (``gpu-dev``). A filename-only lookup therefore
    silently misses and disables pooling for every packaged agent. Match the
    bare filename first (fast path for unprefixed agents), then fall back to a
    filename-qualified overlay (``*<agent>.json``) whose parsed ``name`` equals
    *agent*. (#925)

    Fail-soft: an unreadable/malformed overlay yields ``None`` (unpooled), never
    an exception.
    """
    direct = overlay_dir / f"{agent}.json"
    try:
        return json.loads(direct.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass  # not emitted under the bare name — try a name-field match below
    except (OSError, ValueError):
        logger.warning("MCP-gateway: cannot read overlay spec %s", direct, exc_info=True)
        return None
    # Fallback: a package-installed agent's overlay keeps its package-qualified
    # source filename (e.g. ``Pkg-gpu-dev.json``) while the session requests it
    # by bare name. Restrict the scan to filenames that END with the agent name
    # so at most a handful of plausible candidates are read on the async
    # session-creation path — never the whole directory — then confirm each
    # against the authoritative ``name`` field so a coincidental filename suffix
    # can't mismatch.
    try:
        candidates = sorted(overlay_dir.glob(f"*{agent}.json"))
    except (OSError, ValueError):
        # ValueError: an agent name carrying glob metacharacters (e.g. ``*`` ->
        # ``**.json``) is an invalid pattern; fail soft to unpooled rather than
        # aborting session creation.
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == agent:
            return data
    if candidates:
        # Distinguish "packaged agent not found" from "gateway disabled" / "agent
        # declared nothing poolable" for an operator debugging a low backend count.
        logger.debug(
            "MCP-gateway: no overlay with name %r among %d filename-qualified "
            "candidate(s) in %s; session runs unpooled",
            agent,
            len(candidates),
            overlay_dir,
        )
    return None


def pooled_session_servers(
    overlay_dir: str | Path | None,
    agent: str | None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return ACP ``session/new`` entries for *agent*'s broker stubs.

    ``overlay_dir`` is the rewriter's output directory (usually
    ``<config_dir>/mcp-gateway/agents/``); it is ``None`` when the shared
    gateway is disabled, which is the natural off switch — this returns ``[]``
    and the session runs entirely on the agent's own servers.

    ``channel_id`` reaches the stub so it can report the channel in its caller
    identity, which ``gatewayd`` stamps onto every forwarded ``tools/call`` as
    ``_meta.kirocrew.caller``. It is deliberately NOT a pool dimension (see
    :mod:`kiro_crew.mcp_gateway.pool`), so passing it does not split backends —
    two channels reaching the same agent still share one. It is passed here
    rather than baked into the overlay because the overlay is written once at
    gateway startup and is session-agnostic, while this function runs per
    session. ``None`` simply leaves the flag off and the channel unreported.

    Fail-soft by design: any unreadable or malformed overlay yields ``[]``, so a
    bad rewrite degrades to unpooled operation rather than breaking the spawn.
    """
    if not overlay_dir or not agent:
        return []
    spec = _load_overlay_for_agent(Path(overlay_dir), agent)
    if not isinstance(spec, dict):
        return []
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict) or not (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        ):
            # Not a broker stub: leave it to the agent spec entirely.
            continue
        shaped = _acp_server_entry(str(name), entry, channel_id)
        if shaped is not None:
            out.append(shaped)
    return out


def injection_server_names(
    overlay_dir: str | Path | None,
    agent: str | None,
) -> frozenset[str]:
    """Return the set of server names that WILL be injected for *agent*.

    Callers use this to detect an additive-injection regression: if a launched
    session reports MCP servers whose names overlap with this set, injection has
    become additive rather than overriding and every pooled server is running
    twice. See #927.

    This is deliberately cheap (one file read, no shaping) so it can be called
    as a post-launch health check without adding latency to the session path.
    """
    if not overlay_dir or not agent:
        return frozenset()
    spec = _load_overlay_for_agent(Path(overlay_dir), agent)
    if not isinstance(spec, dict):
        return frozenset()
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(
        name
        for name, entry in servers.items()
        if isinstance(entry, dict)
        and (entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY))
    )
