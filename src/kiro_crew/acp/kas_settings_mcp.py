"""kiro-cli's ``mcp.json`` and ``cli.json`` settings, carried onto a KAS agent block.

A sibling of :mod:`kiro_crew.acp.kas_agents`. KAS reads the same global and
workspace ``.kiro/settings/mcp.json`` files kiro-cli does, but decides from them
differently, so a kiro-cli agent run on KAS would get more servers, fewer
restrictions and a shorter start budget than its author tested. Each function
here closes one of those gaps from the Crew side; KAS is not changed.

* :func:`include_mcp_json` -- kiro-cli accepts ``useLegacyMcpJson`` as well as
  ``includeMcpJson`` (``#[serde(alias)]``); KAS's wire knows only the second.
* :func:`with_legacy_mcp_json` -- kiro-cli falls back to the workspace
  ``.amazonq/mcp.json`` when the workspace has no ``.kiro``; KAS never reads it.
* :func:`stamp_settings_mcp` reads the files in the projection thread, and
  :func:`finish_settings_mcp` applies them when the wire envelope is built: the
  glob ``disabledTools`` of the ``mcp.json`` servers KAS starts are carried as
  ``@server/<pattern>``, and every declared server without a ``timeout`` gets
  kiro-cli's 120 s default instead of KAS's 60 s.

"Opted in" means the flag is ``true``. An agent FILE that leaves it out reads
as ``false`` on kiro-cli: the field is ``#[serde(default, alias =
"includeMcpJson")] use_legacy_mcp_json: bool``, so a missing key is
``bool::default()``, and the mcp.json servers are loaded only behind
``if use_legacy_mcp_json`` (``chat-cli-v2/src/cli/agent/mod.rs``,
``agent/src/agent/agent_config/mod.rs``). Only kiro-cli's built-in default
agent, which is not read from a file, sets it ``true``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.mcp_cleanup import mcp_entry_is_muted

logger = logging.getLogger(__name__)

#: kiro-cli's per-server timeout when none is set (``default_timeout``), in ms.
#: KAS reads ``timeout`` in ms too and defaults it to 60 000.
KIRO_CLI_MCP_TIMEOUT_MS = 120_000
#: KAS clamps a larger ``timeout`` to this (``MAX_MCP_TIMEOUT_MS``).
KAS_MAX_MCP_TIMEOUT_MS = 600_000


def include_mcp_json(spec: Mapping[str, Any]) -> bool | None:
    """The spec's mcp.json opt-in under either spelling, ``None`` when unstated.

    ``includeMcpJson`` wins when both are set; kiro-cli refuses such a spec as a
    duplicate field, so either answer is a guess and the canonical one is kept.
    A non-bool is unstated: the wire's ``z.boolean()`` would drop the whole agent.
    """
    for key in ("includeMcpJson", "useLegacyMcpJson"):
        value = spec.get(key)
        if isinstance(value, bool):
            return value
    return None


def with_legacy_mcp_json(spec: dict[str, Any], work_dir: str | Path | None) -> dict[str, Any]:
    """*spec* with the workspace ``.amazonq/mcp.json`` servers merged in, as kiro-cli does.

    kiro-cli reads ``<cwd>/.amazonq/mcp.json`` only when the agent opts in to
    mcp.json and ``<cwd>/.kiro`` does not exist (``resolve_local_migrated_path``).
    Its global ``~/.aws/amazonq`` fallback needs ``~/.kiro`` absent, which never
    holds on a Crew host, so it is not read. The merged servers become agent
    servers, so the projection's credential, mute and registry filters apply to
    them; a server the spec already declares keeps the spec's entry.
    """
    if include_mcp_json(spec) is not True or not work_dir:
        return spec
    root = Path(work_dir)
    legacy = root / ".amazonq" / "mcp.json"
    if (root / ".kiro").exists() or not legacy.is_file():
        return spec
    from kiro_crew.acp.session_mcp import _read_mcp_settings

    try:
        servers = _read_mcp_settings(legacy).get("mcpServers", {})
    except (OSError, ValueError):
        logger.warning("KAS: legacy workspace .amazonq/mcp.json is unreadable; not merged")
        return spec
    declared = spec.get("mcpServers")
    declared = declared if isinstance(declared, dict) else {}
    # A repository's file must not pre-approve tools past Crew's gate: its
    # ``autoApprove`` is dropped, so those calls ask.
    added = {
        n: ({k: v for k, v in e.items() if k != "autoApprove"} if isinstance(e, dict) else e)
        for n, e in servers.items()
        if n not in declared
    }
    if not added:
        return spec
    logger.info("KAS: merging legacy .amazonq/mcp.json servers %s", sorted(added))
    return {**spec, "mcpServers": {**added, **declared}}


def _file_servers(work_dir: str | Path | None) -> dict[str, Any]:
    """The mcp.json servers KAS reads, the workspace entry winning a shared name."""
    from kiro_crew.acp.session_mcp import NativeSettingsUnreadable, native_settings_sources

    try:
        sources = native_settings_sources(work_dir)
    except NativeSettingsUnreadable:
        # KAS cannot use a file Crew cannot read either; nothing to name.
        logger.warning("KAS: MCP settings unreadable; their servers are not excluded")
        return {}
    servers: dict[str, Any] = {}
    for source in sources:  # global first, so the project file overrides
        servers.update(source.servers)
    return servers


def _init_timeout_ms(work_dir: str | Path | None) -> int | None:
    """kiro-cli's ``mcp.initTimeout`` from ``cli.json``, workspace over global.

    Read through the same credential-gated reader as the mcp.json files: the
    workspace file is checked out with the repository, so a link there must not
    make the gateway read what it points at.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp.session_mcp import _read_mcp_settings

    # Beside the global mcp.json: kiro-cli keeps both in ``~/.kiro/settings``.
    paths = [agent_mod._KIRO_MCP_JSON.parent / "cli.json"]
    if work_dir:
        paths.append(Path(work_dir) / ".kiro" / "settings" / "cli.json")
    found: int | None = None
    for path in paths:
        try:
            value = _read_mcp_settings(path).get("mcp.initTimeout")
        except (OSError, ValueError):
            continue
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            found = value
    return found


def _default_timeout_ms(work_dir: str | Path | None) -> int:
    """kiro-cli's 120 s, raised to ``mcp.initTimeout`` when that is longer.

    shortcut: kiro-cli's ``mcp.initTimeout`` is how long the first turn waits for
    every server, which KAS has no slot for; it is mapped onto the per-server
    budget only as a floor, so a server it was meant to wait for is not cut off.
    """
    init = _init_timeout_ms(work_dir) or 0
    return min(max(KIRO_CLI_MCP_TIMEOUT_MS, init), KAS_MAX_MCP_TIMEOUT_MS)


def _with_timeout(entry: Any, default_ms: int) -> Any:
    """*entry* with ``timeout`` set: its ``timeoutMs`` alias, else *default_ms*."""
    if not isinstance(entry, dict):
        return entry
    timeout = entry.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        return entry
    out = {k: v for k, v in entry.items() if k != "timeoutMs"}
    alias = entry.get("timeoutMs")
    valid_alias = isinstance(alias, int) and not isinstance(alias, bool) and alias >= 0
    out["timeout"] = alias if valid_alias else default_ms
    return out


def _glob_disabled_tools(entry: Any) -> list[str]:
    """A file server's ``disabledTools`` globs; none for a muted or malformed entry."""
    if not isinstance(entry, dict) or mcp_entry_is_muted(entry):
        return []
    tools = entry.get("disabledTools")
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, str) and ("*" in t or "?" in t)]


def stamp_settings_mcp(
    custom_agents: list[dict[str, Any]],
    agent_id: str,
    work_dir: str | Path | None,
) -> list[dict[str, Any]]:
    """The batch with what :func:`finish_settings_mcp` needs stamped on the ACTIVE entry.

    Blocking (settings reads): runs in the harness's projection thread, beside the
    session's own directory. The finish itself waits for the wire envelope,
    because only then are the block and the session-level array the ones sent.
    The stamp is a Crew-only key (``kas_permissions.CREW_ONLY_ENTRY_KEYS``).
    """
    from kiro_crew.acp.kas_permissions import CREW_SETTINGS_MCP_KEY

    files = {n: e for n, e in _file_servers(work_dir).items() if isinstance(n, str) and n}
    stamp = {
        "timeout": _default_timeout_ms(work_dir),
        "fileServers": {name: _glob_disabled_tools(entry) for name, entry in files.items()},
    }
    return [
        (
            {**a, CREW_SETTINGS_MCP_KEY: stamp}
            if isinstance(a, dict) and a.get("id") == agent_id
            else a
        )
        for a in custom_agents
    ]


def finish_settings_mcp(agent: dict[str, Any], session_servers: Any) -> dict[str, Any]:
    """*agent* finished against its stamp and the session-level array it is sent with.

    Pure. Every server left in the block without a ``timeout`` gets the stamped
    default (after the hoist, so a managed server is not kept off the array).
    A mcp.json server neither the block nor the array names reaches the agent
    only through the files, and KAS starts it: its glob ``disabledTools`` are
    carried as ``@server/<pattern>``, because KAS matches those names exactly.
    An entry without a stamp is returned as is.

    shortcut: a server hoisted into the session-level array (every user server on
    ``session/load``) keeps KAS's 60 s, because KAS's client-server element has
    no ``timeout`` slot (``mcp/client-servers.ts`` reads only ``_meta.kiro``'s
    ``disabledTools``/``waitForReady``/``versionNegotiation``); upgrade when it
    gains one.
    """
    from kiro_crew.acp.kas_permissions import CREW_SETTINGS_MCP_KEY

    stamp = agent.get(CREW_SETTINGS_MCP_KEY)
    if not isinstance(stamp, dict):
        return agent
    out = dict(agent)
    block = out.get("mcpServers")
    block = block if isinstance(block, dict) else {}
    if block:
        out["mcpServers"] = {n: _with_timeout(e, stamp["timeout"]) for n, e in block.items()}
    own = _own_names(agent, session_servers)
    exclusions = [
        f"@{name}/{g}"
        for name, globs in stamp["fileServers"].items()
        if name not in own
        for g in globs
    ]
    if exclusions:
        current = out.get("excludedTools")
        current = current if isinstance(current, list) else []
        out["excludedTools"] = list(dict.fromkeys([*current, *exclusions]))
    return out


def _own_names(agent: Mapping[str, Any], session_servers: Any) -> set[str]:
    """The server names *agent*'s block and the session-level array declare."""
    block = agent.get("mcpServers")
    servers = session_servers if isinstance(session_servers, list) else []
    return set(block if isinstance(block, dict) else ()) | {
        str(e.get("name")) for e in servers if isinstance(e, dict) and e.get("name")
    }
