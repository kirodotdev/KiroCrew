"""kiro-cli's ``mcp.json`` and ``cli.json`` settings, carried onto a KAS agent block.

A sibling of :mod:`kiro_crew.acp.kas_agents`. KAS reads the same global and
workspace ``.kiro/settings/mcp.json`` files kiro-cli does, but decides from them
differently, so a kiro-cli agent run on KAS would get more servers, fewer
restrictions and a shorter start budget than its author tested. Each function
here closes one of those gaps from the Crew side; KAS is not changed.

* :func:`include_mcp_json` -- kiro-cli accepts ``useLegacyMcpJson`` as well as
  ``includeMcpJson`` (``#[serde(alias)]``); KAS's wire knows only the second.
  Defined in :mod:`kiro_crew.mcp_cleanup` and re-exported here, so the gateway
  rewriter reads the opt-in through the same function.
* :func:`stamp_settings_mcp` reads the files in the projection thread, and
  :func:`finish_settings_mcp` applies them when the wire envelope is built: the
  ``mcp.json`` servers the agent does not declare are excluded unless the agent
  opted in, the glob ``disabledTools`` of the ones it reaches are carried as
  ``@server/<pattern>``, and every declared server without a ``timeout`` gets
  kiro-cli's 120 s default instead of KAS's 60 s, or 300 s for a remote one.

"Opted in" means the spec states the flag ``true``, under either spelling. The
wire never carries that ``true`` (:func:`kiro_crew.acp.kas_agents.to_client_custom_agent`),
so the opt-in travels in the stamp. An agent FILE that leaves it out reads
as ``false`` on kiro-cli: the field is ``#[serde(default, alias =
"includeMcpJson")] use_legacy_mcp_json: bool``, so a missing key is
``bool::default()``, and the mcp.json servers are loaded only behind
``if use_legacy_mcp_json`` (``chat-cli-v2/src/cli/agent/mod.rs``,
``agent/src/agent/agent_config/mod.rs``). Only kiro-cli's built-in default
agent, which is not read from a file, sets it ``true``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.kas_tool_limits import kas_globs, unmatched_disabled_globs
from kiro_crew.mcp_cleanup import (  # noqa: F401 - include_mcp_json is re-exported
    KIROCREW_BIN_MCP_SERVERS,
    include_mcp_json,
    mcp_entry_is_muted,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_mcp import NativeSettingsUnreadable

#: The key the active entry carries what this module read from the mcp.json /
#: cli.json files under, until the wire envelope applies it. Crew-only:
#: ``kas_permissions.CREW_ONLY_ENTRY_KEYS`` keeps it off the engine.
CREW_SETTINGS_MCP_KEY = "_crewSettingsMcp"

logger = logging.getLogger(__name__)

#: kiro-cli's per-server timeout when none is set (``default_timeout``), in ms.
#: KAS reads ``timeout`` in ms too and defaults it to 60 000.
KIRO_CLI_MCP_TIMEOUT_MS = 120_000
#: KAS clamps a larger ``timeout`` to this (``MAX_MCP_TIMEOUT_MS``).
KAS_MAX_MCP_TIMEOUT_MS = 600_000
#: A remote server's floor when none is set: KAS's own sign-in wait
#: (``LAZY_AUTH_TIMEOUT_MS``). KAS closes an OAuth sign-in's callback listener
#: when the server's ``timeout`` runs out, so this is the user's time to consent.
KAS_REMOTE_MCP_TIMEOUT_MS = 300_000

#: KAS's always-started servers (``ASBX_KIRO_MANDATORY_MCPS``). KAS starts them
#: whatever the agent says, so an exclusion would only hide their tools.
_MANDATORY_ENV = "ASBX_KIRO_MANDATORY_MCPS"


def _unreadable_line(exc: NativeSettingsUnreadable) -> str:
    """The session-start line for a settings file *exc* says could not be read."""
    from kiro_crew.acp.session_mcp import _named_path

    because = f" because {exc.reason}" if exc.reason else ""
    return (
        f"⚠ Kiro Crew could not read {exc.label} ({_named_path(exc.path)}){because}."
        " Its MCP server settings may not apply to this session."
    )


def _file_servers(work_dir: str | Path | None, notices: list[str]) -> dict[str, Any]:
    """The mcp.json servers KAS reads, the workspace entry winning a shared name.

    Each file that cannot be read adds one line to *notices*, naming it and why.
    """
    from kiro_crew.acp.session_mcp import NativeSettingsUnreadable, native_settings_sources

    try:
        sources = native_settings_sources(work_dir)
    except NativeSettingsUnreadable as exc:
        notices.append(_unreadable_line(exc))
        # An unreadable workspace file must not lift the global file's
        # exclusions: those servers are the ones the gateway pools.
        logger.warning("KAS: MCP settings unreadable; only the global file's servers are named")
        try:
            sources = native_settings_sources(None) if work_dir else []
        except NativeSettingsUnreadable as global_exc:
            if global_exc.path != exc.path:
                notices.append(_unreadable_line(global_exc))
            # KAS cannot use a global file Crew cannot read either; no servers to name.
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
    """*entry* with ``timeout`` set: its ``timeoutMs`` alias, else *default_ms*.

    For a server the engine reaches by url, *default_ms* is at least
    :data:`KAS_REMOTE_MCP_TIMEOUT_MS`.
    """
    if not isinstance(entry, dict):
        return entry
    timeout = entry.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        return entry
    if entry.get("url") and not entry.get("command"):
        default_ms = max(default_ms, KAS_REMOTE_MCP_TIMEOUT_MS)
    out = {k: v for k, v in entry.items() if k != "timeoutMs"}
    alias = entry.get("timeoutMs")
    valid_alias = isinstance(alias, int) and not isinstance(alias, bool) and alias >= 0
    out["timeout"] = alias if valid_alias else default_ms
    return out


def _glob_disabled_tools(entry: Any) -> list[str]:
    """A file server's ``disabledTools`` globs as KAS matches them (:func:`kas_globs`).

    Empty for a muted or malformed entry. A glob with no KAS spelling is kept as
    written, and :func:`stamp_settings_mcp` reports it for the session-start notice.
    """
    if not isinstance(entry, dict) or mcp_entry_is_muted(entry):
        return []
    tools = entry.get("disabledTools")
    if not isinstance(tools, list):
        return []
    return [
        glob
        for tool in tools
        if isinstance(tool, str) and ("*" in tool or "?" in tool)
        for glob in kas_globs(tool) or [tool]
    ]


def stamp_settings_mcp(
    custom_agents: list[dict[str, Any]],
    agent_id: str,
    work_dir: str | Path | None,
    declared: Iterable[str] = (),
    legacy_mcp_json: bool = False,
    notices: list[str] | None = None,
    unmatched: set[tuple[str, str]] | None = None,
    opted_in: bool = False,
) -> list[dict[str, Any]]:
    """The batch with what :func:`finish_settings_mcp` needs stamped on the ACTIVE entry.

    Blocking (settings reads): runs in the harness's projection thread, beside the
    session's own directory. The finish itself waits for the wire envelope,
    because only then are the block and the session-level array the ones sent.
    The stamp is a Crew-only key (``kas_permissions.CREW_ONLY_ENTRY_KEYS``).

    *declared* is the server names the agent's own spec lists, read before
    projection: a name there may reach KAS in the session-level array instead
    of the block, and it is still the agent's. *legacy_mcp_json* marks an agent
    file that keeps the earlier behaviour (:mod:`kiro_crew.acp.kas_mcp_json_legacy`).
    *notices* gets one session-start line per mcp.json file that could not be read.
    *unmatched* gets ``(server, pattern)`` for each file glob of a server this
    block carries that KAS cannot match (:func:`kas_tool_limits.unmatched_globs_notice`).
    *opted_in* is the spec's own :func:`include_mcp_json` ``true``, which the wire
    entry does not carry.
    """
    files = {
        n: e
        for n, e in _file_servers(work_dir, [] if notices is None else notices).items()
        if isinstance(n, str) and n
    }
    stamp = {
        "timeout": _default_timeout_ms(work_dir),
        "fileServers": {name: _glob_disabled_tools(entry) for name, entry in files.items()},
        "declared": sorted(n for n in declared if isinstance(n, str)),
        "legacy": legacy_mcp_json,
        "optedIn": opted_in,
    }
    stamped = [
        (
            {**a, CREW_SETTINGS_MCP_KEY: stamp}
            if isinstance(a, dict) and a.get("id") == agent_id
            else a
        )
        for a in custom_agents
    ]
    for entry in stamped:
        if unmatched is not None and isinstance(entry, dict):
            if entry.get(CREW_SETTINGS_MCP_KEY) is stamp:
                # Only the servers whose file globs finish_settings_mcp carries.
                skipped = _declared_names(entry) | excluded_whole(entry)
                kept = {n: e for n, e in files.items() if n not in skipped}
                unmatched.update(unmatched_disabled_globs(kept))
    return stamped


def finish_settings_mcp(agent: dict[str, Any]) -> dict[str, Any]:
    """*agent* finished against its stamp, for the wire envelope.

    Pure. Every server left in the block without a ``timeout`` gets the stamped
    default, at least 300 s for a remote one (after the hoist, so a managed
    server is not kept off the array).
    A mcp.json server the agent does not declare (:func:`excluded_whole`) is
    excluded whole unless the agent opted in: kiro-cli never loads it, and KAS
    would start it for ``tools: ["*"]``. A server KAS still starts has its
    glob ``disabledTools`` carried as ``@server/<pattern>``, because KAS matches
    those names exactly; a ``{a,b}`` glob is expanded per branch, and a glob
    KAS cannot match is carried as written (the session-start notice names it).
    An entry without a stamp is returned as is.

    Only Crew's own servers are hoisted into the session-level array, on
    ``session/new`` and ``session/load`` alike, so every user server the block
    keeps gets the ``timeout`` here (KAS's session-level element has no slot).
    """
    stamp = agent.get(CREW_SETTINGS_MCP_KEY)
    if not isinstance(stamp, dict):
        return agent
    out = dict(agent)
    block = out.get("mcpServers")
    block = block if isinstance(block, dict) else {}
    if block:
        out["mcpServers"] = {n: _with_timeout(e, stamp["timeout"]) for n, e in block.items()}
    own = _declared_names(agent)
    whole = excluded_whole(agent)
    exclusions = [f"@{name}" for name in stamp["fileServers"] if name in whole]
    for name, globs in stamp["fileServers"].items():
        if name in own or name in whole:
            continue
        exclusions += [f"@{name}/{g}" for g in globs]
    if exclusions:
        current = out.get("excludedTools")
        current = current if isinstance(current, list) else []
        out["excludedTools"] = list(dict.fromkeys([*current, *exclusions]))
    return out


def without_excluded_stubs(servers: list[Any], agents: Any) -> list[Any]:
    """*servers* without the pooled stub of a server every stamped agent excludes.

    The gateway also pools the settings files' own servers into the session-level
    array, which would mount one past the exclusion. That array is the whole
    batch's, so a stub stays while one stamped agent (the session's own or a
    sub-agent target) still loads its server; each agent that does not keeps its
    ``@server`` exclusion (:func:`finish_settings_mcp`). The runtime calls this
    before it binds the array every consumer reads (request, report, unresolved-ref
    guard), so they all describe what was sent. *servers* is returned as is when
    nothing is excluded.
    """
    batch = agents if isinstance(agents, list) else []
    stamped = [
        a for a in batch if isinstance(a, dict) and isinstance(a.get(CREW_SETTINGS_MCP_KEY), dict)
    ]
    dropped = set.intersection(*(excluded_whole(a) for a in stamped)) if stamped else set()
    if not dropped:
        return servers
    # Imported here: mcp_session_report reaches this module through kas_permissions.
    from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text

    for name in sorted(dropped):
        # The name comes from a settings file a checkout can supply.
        shown = sanitize_sink_text(str(name), NAME_CAP)
        logger.info("pooled stub %s not sent: the agent does not load mcp.json servers", shown)
    return [e for e in servers if not (isinstance(e, dict) and e.get("name") in dropped)]


def _declared_names(agent: Mapping[str, Any]) -> set[str]:
    """The server names *agent*'s block and its spec declare.

    Never the session-level array: it also carries the pooled stubs built from
    the settings files themselves, and a stub is not a declaration.
    """
    stamp = agent.get(CREW_SETTINGS_MCP_KEY)
    block = agent.get("mcpServers")
    declared = stamp.get("declared", ()) if isinstance(stamp, dict) else ()
    return set(block if isinstance(block, dict) else ()) | set(declared)


def excluded_whole(agent: Mapping[str, Any]) -> set[str]:
    """The stamped mcp.json servers a not-opted-in *agent* does not start.

    Judged from the agent's block and the names its spec lists, never from the
    session-level array: that array also carries the pooled stubs Crew builds
    from the settings file itself, and a stub is not a declaration
    (:func:`without_excluded_stubs` drops those).
    Crew's own servers and the mandatory ones are never excluded: a session
    without Crew's cannot report back at all, and KAS starts the mandatory ones
    whatever it is told. Empty for an unstamped, opted-in or legacy entry.
    """
    stamp = agent.get(CREW_SETTINGS_MCP_KEY)
    if not isinstance(stamp, dict) or stamp.get("optedIn") is True or stamp.get("legacy") is True:
        return set()
    own = _declared_names(agent)
    mandatory = {n.strip() for n in os.environ.get(_MANDATORY_ENV, "").split(",") if n.strip()}
    return {
        name
        for name in stamp["fileServers"]
        if name not in own and name not in mandatory and name not in KIROCREW_BIN_MCP_SERVERS
    }
