"""GET /api/mcp-gateway/footprint: what the open chats' MCP servers cost now.

Feeds the MCP page's hero numbers and its "open chats still use their own
copies" banner. Every figure is a reading of this host
(:mod:`kiro_crew.mcp_gateway.footprint`); a figure with any unreadable input is
``null`` and the page hides it. Read only: nothing here starts, stops or
reloads anything.

Owner only, on top of the dashboard's own credential gate: the payload names
the owner's chats. It carries numbers and chat slot names, never a command
line, an env name or value, or a path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway import footprint
from kiro_crew.mcp_gateway.launch_approval import (
    refused_servers,
    target_env_stem,
    target_stem,
)
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


def _settings_rows(path: Path) -> dict[str, dict[str, Any]]:
    """Stdio servers declared in ``~/.kiro/settings/mcp.json``. BLOCKING.

    kiro-cli merges that file into every agent that reads it, so its servers
    run in session trees without appearing in any agent spec. The filter is
    the rewriter's own for that file (``_injectable_settings_servers``): a
    muted or catalog-governed entry is not launched, and an HTTP/SSE entry has
    no process to measure. Env NAMES only, never a value.
    """
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed
    from kiro_crew.mcp_gateway.launch_approval import env_fingerprint
    from kiro_crew.user_json import loads_user_json

    try:
        loaded = loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = loaded.get("mcpServers") if isinstance(loaded, dict) else None
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if mcp_entry_is_muted(entry) or mcp_entry_is_registry_governed(entry):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        args = entry.get("args")
        argv = [str(a) for a in args] if isinstance(args, list) else []
        env = entry.get("env")
        out[str(name)] = {
            "transport": "stdio",
            "env_names": {str(k) for k in env} if isinstance(env, dict) else set(),
            "launches": {(command, *argv)},
            "env_ids": {env_fingerprint(env if isinstance(env, dict) else {})},
        }
    return out


def _specs(
    enabled: bool, refused: dict[str, Any], routed: frozenset[str] = frozenset()
) -> dict[str, footprint.ServerSpec]:
    """One :class:`footprint.ServerSpec` per declared server. BLOCKING.

    ``shared_now`` reads the RUNNING routing set (*routed*: the target stems
    the live daemon was started with), not the saved stub list: a server
    enabled since the last gateway start is saved intent only, and reloading
    a chat cannot move it onto a backend the daemon does not serve.

    The agent specs first, then ``settings/mcp.json`` for every server no agent
    spec declares (a spec's own entry wins, as it does in kiro-cli's merge). An
    unshareable server counts on both sides of the reading, so a server added
    here can only make ``with_sharing`` more complete, never smaller by itself.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.config.loader import KiroCrewConfig

    # Through the facade: this module is not one of the split ``mcp_*`` owners.
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers
    from kiro_crew.mcp_gateway.rewriter import (
        UNPOOLABLE_SERVERS,
        _withheld_env_count,
        pool_identity_env_keys,
    )

    gw_cfg = KiroCrewConfig.load().mcp_gateway
    forward = bool(gw_cfg.forward_declared_env)
    identity_keys = pool_identity_env_keys()
    rows = mcp_handlers._collect_server_rows()
    # Union, not override: an agent spec that redeclares a server overrides
    # the settings entry for THAT agent only, and every other agent still
    # inherits the settings launch, so both launches can run at once.
    for name, row in _settings_rows(
        kiro_agents_dir_path().parent / "settings" / "mcp.json"
    ).items():
        have = rows.get(name)
        if have is None:
            rows[name] = row
            continue
        have["launches"] = set(have.get("launches", ())) | row["launches"]
        have["env_ids"] = set(have.get("env_ids", ())) | row["env_ids"]
        have["env_names"] = set(have.get("env_names", ())) | row["env_names"]
    out: dict[str, footprint.ServerSpec] = {}
    for name, row in rows.items():
        can_stub = row["transport"] == "stdio" and name not in UNPOOLABLE_SERVERS
        # The same withheld-env test the servers endpoint applies, asked as
        # "would it pool once sharing is on", whatever the switch reads now.
        env_blocks = bool(
            _withheld_env_count({k: "" for k in row["env_names"]}, forward, identity_keys)
        )
        shareable = can_stub and not env_blocks
        out[name] = footprint.ServerSpec(
            launches=tuple(sorted(row.get("launches", ()))),
            shareable=shareable,
            one_identity=len(row.get("env_ids", ())) <= 1,
            shared_now=bool(
                enabled
                and shareable
                and target_stem(mcp_server_alias(name)) in routed
                and name not in refused
            ),
        )
    return out


async def api_mcp_gateway_footprint(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/footprint.

    ``{using_now_bytes, with_sharing_bytes, open_chats, stale_chats: [slot]}``.
    ``open_chats`` counts dashboard chats with a live agent process;
    ``stale_chats`` names the chats whose process still runs its own copy of a
    server that is shared for new chats now -- the ones a reload would move
    onto the shared copy. Both byte figures are ``null`` when not every process
    they need could be read.
    """
    # An app's credential, an internal (agent-tool) caller and a non-owner
    # dashboard subject are all refused here, before anything is read.
    denied = await require_owner_dashboard_request(request, "mcp_gateway.footprint")
    if denied is not None:
        return denied

    from kiro_crew.config.loader import KiroCrewConfig

    state: DashboardState = request.app["state"]
    enabled = bool(KiroCrewConfig.load().mcp_gateway.enabled)
    manager = getattr(state, "_mcp_gateway_manager", None)
    running = manager is not None and manager.is_running
    # What the live daemon routes, from the spec it was started with.
    routed: frozenset[str] = frozenset()
    if running:
        spec = getattr(manager, "_spec", None)
        routed = frozenset(
            stem
            for key in getattr(spec, "mcp_target_env", None) or ()
            if (stem := target_env_stem(str(key)))
        )
    refused = await asyncio.to_thread(refused_servers)
    specs = await asyncio.to_thread(_specs, enabled, refused, routed)

    rows = state.sessions.runtime_pids()
    sessions: list[tuple[str, int]] = []
    for row in rows:
        key, pid = row.get("key"), row.get("pid")
        if isinstance(pid, int) and isinstance(key, str):
            sessions.append((key, pid))
    # ``None`` = the pool could not be asked, so its backends are unread and
    # both figures come back null; ``[]`` = it answered with no backends.
    backends: list[tuple[str, int]] | None = []
    if manager is not None and running:
        try:
            snap = await manager.stats()
        except Exception:  # a broker that cannot answer has no backends to read
            logger.debug("mcp footprint: pool stats unavailable", exc_info=True)
            snap = {}
        listed = snap.get("backends") if isinstance(snap, dict) else None
        # The snapshot lists pooled backends only. A connection-private or
        # draining backend is running but unlisted (and not in any chat's
        # tree), so with any of them the reading would be short: no figures.
        unlisted = 0
        if isinstance(snap, dict):
            for field in ("exclusive", "draining"):
                value = snap.get(field)
                unlisted += value if isinstance(value, int) else 0
        if not isinstance(listed, list) or unlisted > 0:
            backends = None
        else:
            backends = [
                (str(b["server"]), b["pid"])
                for b in listed
                if isinstance(b, dict) and isinstance(b.get("pid"), int) and b.get("server")
            ]

    reading = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), lambda: footprint.measure(sessions, specs, backends)
    )

    live_keys = {key for key, _ in sessions}
    slot_by_key: dict[str, list[str]] = {}
    for name, slot in list(state._slots.items()):
        slot_by_key.setdefault(effective_session_key(slot), []).append(name)
    open_chats = sorted(n for key in live_keys for n in slot_by_key.get(key, ()))
    stale = (
        sorted({n for key in reading.stale_session_keys for n in slot_by_key.get(key, ())})
        if reading is not None
        else []
    )
    return web.json_response(
        {
            "using_now_bytes": reading.using_now_bytes if reading else None,
            "with_sharing_bytes": reading.with_sharing_bytes if reading else None,
            "open_chats": len(open_chats),
            "stale_chats": stale,
        }
    )
