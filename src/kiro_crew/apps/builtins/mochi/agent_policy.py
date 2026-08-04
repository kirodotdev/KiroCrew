"""Mochi's MCP reach policy — what its agents may and may not call.

kiro-cli loads every server in the GLOBAL ``~/.kiro/settings/mcp.json`` into an
agent regardless of that agent's own config; there is no "off" switch. The only
way to keep a server out of an agent's reach is to re-declare it in the agent's
own ``mcpServers`` with its tools disabled. The original standalone Mochi did
exactly this with hand-maintained tool lists per server.

This module produces the same effect from live data instead of hardcoded lists:

* ``mochi.extraMcpServers`` (what the user turned on in Settings -> MCP) becomes
  the GRANT list, with per-server ``autoApprove`` / ``disabledTools`` and
  per-agent scoping (``chat`` -> the foreground agent, ``bg`` -> the background
  one).
* every other ambient server becomes a NEUTRALIZE entry carrying its real tool
  names, discovered from the MCP probe cache.

**Fail-closed on unknown tools.** Every ungranted ambient server becomes a
NEUTRALIZE entry, INCLUDING one whose tools have not been probed yet (empty
list). The bridge disables a neutralized server at the SERVER level
(``disabled: true``), so an empty tool list still fully denies it — there is no
"hollow deny" to fear, and deferring the unprobed case to an audit-only list
would be a fail-open that lets Mochi retain ambient access to that server.

**Fail-closed on unknown SERVERS too.** The ambient set is unioned from the probe
cache AND the global config, because the cache both can fail and can lag. Taking
it from the cache alone made an enumeration failure look like "nothing to deny"
and silently restored Mochi's ambient reach — see :func:`_ambient_servers`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.mochi.soul_loader import rendered_bg_prompt_path, rendered_prompt_path
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Filename the framework reads from the app's data dir. Must match
#: ``kiro_crew.apps.bridges.AGENT_MCP_POLICY_FILE``.
POLICY_FILENAME = "agent_mcp_policy.json"

#: The two agents this app ships. ``chat`` / ``bg`` are the audience labels the
#: user-facing settings use; these are the real agent names they map to.
CHAT_AGENT = "mochi"
BG_AGENT = "mochi-bg"
_AUDIENCE_TO_AGENT = {"chat": CHAT_AGENT, "bg": BG_AGENT}

#: Never neutralize the app's own server: it is the pet's whole reason to exist,
#: and it is declared by the manifest rather than by the user.
_OWN_SERVER_PREFIX = "mochi"


def policy_path(data_dir: Path) -> Path:
    return data_dir / POLICY_FILENAME


def _normalise_entries(raw: Any) -> list[dict[str, Any]]:
    """Accept both wire shapes for ``extraMcpServers``.

    Older settings stored plain strings ("just enable this server"); the current
    UI stores objects with per-server policy. Both must keep working — a user's
    stored settings are not migrated on read.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item:
            out.append({"name": item})
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item)
    return out


def _configured_server_names() -> list[str]:
    """Ambient server NAMES straight from the user's global MCP config.

    Reads exactly the file kiro-cli itself loads ambient servers from, which is
    what makes it authoritative here.

    Read-only and best-effort. An unreadable or malformed file yields ``[]``, and
    that is NOT a fail-open: it is the same file kiro-cli reads to decide which
    ambient servers to load, so a file it cannot parse loads nothing for Mochi to
    deny either. Contrast the probe cache, whose failure says nothing about what
    kiro-cli loaded — which is the case :func:`_ambient_servers` exists to cover.
    """
    config = Path.home() / ".kiro" / "settings" / "mcp.json"
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []  # no global servers configured at all
    except Exception as exc:  # noqa: BLE001 — a malformed global file is not our error
        logger.warning("Mochi policy: cannot read global mcp.json: %s", exc)
        return []
    specs = raw.get("mcpServers")
    if not isinstance(specs, dict):
        return []
    return [name for name in specs if isinstance(name, str) and name]


def _ambient_servers() -> dict[str, list[str]]:
    """Ambient server name -> known tool names, from TWO independent sources.

    * the MCP probe cache (``mcp_discovery``) — names AND real tool names;
    * the global MCP config (:func:`_configured_server_names`) — names only.

    Unioned, not tried in order, and that is the security-relevant part. Building
    the map from the probe cache ALONE was a fail-open with two triggers: the
    cache can be unavailable (import or listing error), and it also LAGS — a
    server the user just added to the global config is already ambient because
    kiro-cli loads it, before anything has probed it. Either way the map came
    back empty, every ``neutralize`` map was empty, and an empty map is
    indistinguishable from "nothing to deny": Mochi silently kept ambient reach
    to servers the user never granted it.

    A name with no tools denies completely — the bridge writes ``disabled: true``
    at the SERVER level — so the config-only half is a full denial, not a partial
    one. Tools from the probe cache are an enrichment, not a requirement.
    """
    tools: dict[str, list[str]] = {}
    try:
        from kiro_crew.mcp_discovery import list_servers

        for server in list_servers():
            tools[server.name] = list(getattr(server, "tools", []) or [])
    except Exception as exc:  # noqa: BLE001 — the probe cache is an enrichment
        logger.warning("Mochi policy: MCP discovery unavailable: %s", exc)

    for name in _configured_server_names():
        tools.setdefault(name, [])
    return tools


def build_policy(settings: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    """Compute the policy document from Mochi's settings + live MCP discovery.

    When *data_dir* is given, each agent also gets its system prompt pinned to the
    rendered prompt file in that directory. The prompt is GENERATED (it carries the
    user's pet name and the persona of the chosen appearance), so the path can only
    be stated at runtime — the packaged agent template cannot name it.
    """
    entries = _normalise_entries(settings.get("extraMcpServers"))
    ambient = _ambient_servers()

    granted: dict[str, dict[str, dict[str, Any]]] = {CHAT_AGENT: {}, BG_AGENT: {}}
    for entry in entries:
        name = entry["name"]
        audiences = entry.get("agents") or ["chat"]
        spec = {
            "autoApprove": list(entry.get("autoApprove") or []),
            "disabledTools": list(entry.get("disabledTools") or []),
        }
        for audience in audiences:
            agent = _AUDIENCE_TO_AGENT.get(str(audience))
            if agent is not None:
                granted[agent][name] = dict(spec)

    agents: dict[str, dict[str, Any]] = {}
    for agent, servers in granted.items():
        neutralize: dict[str, list[str]] = {}
        for name, tools in ambient.items():
            if name in servers:
                continue
            if name == _OWN_SERVER_PREFIX or name.startswith(_OWN_SERVER_PREFIX + ":"):
                continue
            # Neutralize EVERY ungranted ambient server, including one whose
            # tools have not been probed yet (empty list). The bridge writes
            # ``disabled: true`` at the SERVER level (not just disabledTools),
            # so an empty tool list still fully denies the server. Deferring the
            # unprobed case to an audit-only list was a fail-open: it let Mochi
            # retain ambient access to any not-yet-probed global MCP server.
            neutralize[name] = tools
        agents[agent] = {
            "servers": servers,
            "neutralize": neutralize,
        }

    if data_dir is not None:
        # Per-agent, NOT one shared document. The background agent is a spawned
        # subagent with a different tool set and a different output contract;
        # pointing it at the chat prompt told it to spawn subagents, save
        # lessons, and reply in plain text — none of which it can do.
        prompts = {
            CHAT_AGENT: rendered_prompt_path(data_dir),
            BG_AGENT: rendered_bg_prompt_path(data_dir),
        }
        for agent in agents:
            path = prompts.get(agent)
            if path is not None:
                agents[agent]["prompt"] = f"file://{path}"

    return {"version": 1, "agents": agents}


def write_policy(data_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Persist the policy where the framework's agent materializer reads it."""
    policy = build_policy(settings, data_dir)
    atomic_write(policy_path(data_dir), json.dumps(policy, indent=2) + "\n")
    return policy


class PolicyNotMaterialized(RuntimeError):
    """The deny policy was written but did NOT reach the agent configs.

    A distinct type because the caller must treat it as fail-closed rather than as
    a warning: kiro-cli loads every server in the global MCP config into an agent
    regardless of that agent's own config, so until the ``neutralize`` entries are
    materialized the agent HAS ambient reach to every ungranted server. On a first
    enable there is no earlier materialization to fall back on, so starting anyway
    is exactly the access this whole module exists to remove.
    """


def apply_policy(data_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Write the policy AND re-materialize the app's agent configs.

    The policy is written to disk first, unconditionally: it is what the gateway's
    startup reconcile picks up, so persisting it is useful even when this
    materialization fails.

    Raises :class:`PolicyNotMaterialized` when the refresh does not land. This
    used to be swallowed with a warning — including the case where
    ``refresh_app_agents`` returned an EMPTY list, which was logged as "applied to
    0 agent config(s)" and read as success. Both left the caller believing the
    deny policy was in force when it was not.

    Not raised when the app has NO registered agent configs: there is then nothing
    for kiro-cli to load either, so nothing holds ambient reach.
    """
    policy = write_policy(data_dir, settings)
    try:
        from kiro_crew.apps.bridges import get_app_manifest, refresh_app_agents

        refreshed = refresh_app_agents("mochi")
    except Exception as exc:
        raise PolicyNotMaterialized(f"agent refresh raised: {exc}") from exc

    # Compared against what the MANIFEST declares, not a hardcoded count and not
    # the policy's own agent list. `refresh_app_agents` returns an empty list both
    # when it fails AND when there is simply nothing registered to write to (no
    # manifest agents, or an app that owns its own resources) — and the second is
    # not a fail-open, because then no Mochi agent config exists for kiro-cli to
    # load in the first place. Treating every empty result as a failure fired in
    # every environment where the app is not registry-installed.
    declared = getattr(get_app_manifest("mochi"), "agents", None) or []
    if not declared:
        logger.warning("Mochi policy: no registered agent configs to apply it to")
        return policy
    if len(refreshed) < len(declared):
        # A PARTIAL refresh is the dangerous one: the agent that got skipped is
        # the one left holding ambient reach.
        raise PolicyNotMaterialized(
            f"refreshed {len(refreshed)} of {len(declared)} agent config(s)"
        )
    logger.info("Mochi MCP policy applied to %d agent config(s)", len(refreshed))
    return policy
