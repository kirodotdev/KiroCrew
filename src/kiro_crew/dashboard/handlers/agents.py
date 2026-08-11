"""Agent configuration, themes, AIM integration, and agent CRUD handlers."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import agent_state, model_registry
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.agent_discovery import (
    clear_list_agents_cache,
    list_agents,
    project_agent_names,
    spec_model,
    spec_str,
)
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    normalize_agent_model,
    read_config_for_update,
    resolve_agent_bindings,
    resolve_agent_config_path,
    resolve_effective_model,
    write_config_atomically,
)
from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    _history_key_for,
    is_deprecated_model,
)
from kiro_crew.dashboard.handlers._shared import (
    MAX_AGENT_SKILLS,
    _capability_manager,
    _read_session_key,
    active_project_dir,
    agent_skill_keys,
    agent_skill_views,
    apply_skill_mapping,
)
from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor, maintenance_executor, subprocess_executor
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
    wrap_argv,
)

_MODEL_LIST_STDERR_TAIL_CHARS = 1000

logger = logging.getLogger(__name__)


def _namespaced_agent_file_exists(agent_name: str) -> bool:
    """True when an app-registered agent file backs *agent_name*.

    App agents are materialized as ``<app>--<agent>.json`` (namespaced file
    names prevent two apps' same-named agents from clobbering each other), but
    kiro-cli resolves agents by the JSON ``name`` field, not the file name. A
    file-name-only existence check therefore reports a perfectly spawnable app
    agent as missing on every boot.
    """
    # Resolved per call, not read from a module constant: the agents dir tracks
    # the live data home (see config.md "Data Home"), and a frozen constant would
    # glob the real ~/.kiro from an isolated run.
    from kiro_crew.agent import kiro_agents_dir_path

    try:
        for path in kiro_agents_dir_path().glob(f"*--{agent_name}.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("name") == agent_name:
                return True
    except OSError:
        return False
    return False


def _err500(exc: BaseException) -> web.Response:
    """Return a generic 500 with a correlation id; log the detail server-side.

    Browser-facing 5xx bodies must not echo raw backend exception text
    (CWE-209). The short correlation id ties the sanitized client response to
    the full server-side log line (which retains the traceback).
    """
    corr = uuid.uuid4().hex[:12]
    logger.error("agents handler error [%s]", corr, exc_info=exc)
    return web.json_response({"error": "internal error", "id": corr}, status=500)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Agent Config ──


def _auto_install_agent() -> None:
    """Re-install agent config to kiro-cli so changes take effect immediately."""
    try:
        from kiro_crew.agent import install_agent  # noqa: F811

        install_agent()
        logger.info("Auto-applied agent config via dashboard")
    except Exception:
        logger.debug("Auto-apply agent config failed", exc_info=True)


def _find_agent_config() -> Path:
    """Find agents/defaults.json — delegates to centralized resolver."""
    return resolve_agent_config_path()


def _installed_agent_config() -> Path:
    """Return the installed agent config path (~/.kiro/agents/kirocrew.json).

    This is the live config that kiro-cli reads.  Dashboard MCP toggle
    and sync operations write here — NOT to agents/defaults.json.
    """
    from kiro_crew.agent import AGENT_FILENAME, kiro_agents_dir_path  # noqa: F811

    return kiro_agents_dir_path() / AGENT_FILENAME


async def api_agent_config(request: web.Request) -> web.Response:
    """GET/PUT /api/agent/config — read or write the installed agent config.

    Reads/writes ``~/.kiro/agents/kirocrew.json`` — the live config that
    kiro-cli actually uses at runtime.  Falls back to ``agents/defaults.json``
    if the installed config doesn't exist yet.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    installed_path = _h._installed_agent_config()
    defaults_path = _h._find_agent_config()
    # Prefer installed config (what kiro-cli reads); fall back to defaults
    agent_config_path = installed_path if installed_path.is_file() else defaults_path

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        config = body.get("config")
        if not isinstance(config, dict):
            return web.json_response({"error": "config must be an object"}, status=400)
        try:
            # Track tools the user intentionally removed from shipped defaults
            # so they don't reappear on upgrade.  Stored in ~/.kiro/crew/config.json
            # (NOT kirocrew.json — kiro-cli rejects unknown fields).
            # Per-key dict so removing from allowedTools only doesn't affect tools.
            from kiro_crew.agent import get_shipped_tools  # noqa: F811

            shipped = get_shipped_tools()
            removed_per_key: dict[str, list[str]] = {}
            for key in ("tools", "allowedTools"):
                diff = sorted(set(shipped.get(key, [])) - set(config.get(key, [])))
                if diff:
                    removed_per_key[key] = diff
            mc_cfg_path = _h.config_path()  # type: ignore[operator]
            # Fail closed: writing back a {} baseline would drop every other
            # setting just to record removedTools. See read_config_for_update.
            try:
                mc_cfg = read_config_for_update(mc_cfg_path)
            except ConfigReadError:
                logger.exception("Refusing to record removedTools: config unreadable")
                return web.json_response(
                    {"error": "failed to read config file", "code": "config_unreadable"},
                    status=500,
                )
            if removed_per_key:
                mc_cfg["removedTools"] = removed_per_key
            else:
                mc_cfg.pop("removedTools", None)
            write_config_atomically(mc_cfg_path, mc_cfg)
            # Governance floor on the WHOLE-object write path: this handler
            # persists the request's config verbatim, so a dashboard PUT could
            # otherwise restore a ceiling-governed @denied grant or a governed
            # server's autoApprove that the per-ref writers strip. Filter the map
            # here too (no-op on an ungoverned host).
            from kiro_crew.platform.governance import sanitize_agent_config_governance

            # Offloaded: the filter resolves the ceiling AND scans the profile
            # directory per ref, which is synchronous filesystem work — running it
            # inline would stall the aiohttp event loop for the duration.
            await asyncio.to_thread(sanitize_agent_config_governance, config)
            installed_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            # Restart kiro-cli sessions so new config takes effect
            await _h._reset_all_sessions(request)
            return web.json_response({"ok": True, "applied": True})
        except Exception as exc:
            return _err500(exc)
    # GET
    try:
        data = json.loads(agent_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    return web.json_response(data)


async def api_default_agent(request: web.Request) -> web.Response:
    """GET/PUT /api/config/default-agent — read or set the default agent."""
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        name = body.get("agent", "")
        # Reject non-strings before any use: a JSON list/object here would make
        # the membership check below raise (unhashable) into a 500, and a
        # non-string must never reach the config write either.
        if not isinstance(name, str):
            return web.json_response(
                {"error": "agent must be a string", "code": "invalid_agent_type"}, status=400
            )
        # Only a config alias may become the default: the default is resolved
        # from cfg.agents on every dispatch, so persisting any other name (a
        # project-scope discovery, an app agent, a typo) writes a default that
        # silently resolves to something else. Guarded server-side so EVERY
        # caller is covered, not just whichever picker currently hides the
        # action — project-scope rows carry scope="project" in /api/agents
        # precisely so UIs can disable this, but the config file is the last
        # line of defense.
        try:
            # Config load is stat/read/validation filesystem work; off-loop so
            # slow storage cannot freeze chat and the liveness heartbeat.
            known = set((await asyncio.to_thread(KiroCrewConfig.load)).agents.keys())
        except Exception:
            known = set()
        # Fail CLOSED: an unreadable config yields an empty `known`, and that is
        # precisely when validation is impossible — a non-empty name must be
        # rejected, not waved through. A valid config always has at least one
        # agent (load() guarantees default_agent exists in agents), so an empty
        # set never rejects a legitimate alias.
        if name and name not in known:
            return web.json_response(
                {
                    "error": f"agent {name!r} is not a configured agent alias",
                    "code": "default_agent_not_alias",
                },
                status=400,
            )
        path = _h.config_path()
        try:
            data = read_config_for_update(path)
        except ConfigReadError:
            # Fail closed: writing back a {} baseline would drop every other setting.
            logger.exception("Refusing to set default agent: config unreadable")
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"}, status=500
            )
        data["default_agent"] = name
        write_config_atomically(path, data)
        return web.json_response({"ok": True, "default_agent": name})
    cfg = KiroCrewConfig.load()
    return web.json_response({"default_agent": cfg.default_agent})


# ── Config Schema ──


async def api_config_schema(request: web.Request) -> web.Response:
    """GET /api/config/schema — return config schema entries."""
    entries = SCHEMA_REGISTRY

    # Filter by tags (comma-separated, intersection)
    tags_param = request.query.get("tags", "").strip()
    if tags_param:
        requested_tags = {t.strip() for t in tags_param.split(",") if t.strip()}
        entries = [e for e in entries if set(e.tags) & requested_tags]

    # Filter out deprecated entries when deprecated=false
    dep_param = request.query.get("deprecated", "").strip().lower()
    if dep_param == "false":
        entries = [e for e in entries if not e.deprecated]

    # Serialize, masking sensitive defaultValues and converting dataclass
    # defaults to None (they aren't JSON-serializable).
    result = []
    for entry in entries:
        d = config_entry_to_dict(entry)
        if entry.sensitive or dataclasses.is_dataclass(d.get("defaultValue")):
            d["defaultValue"] = None
        result.append(d)

    return web.json_response({"entries": result})


_CAPABILITY_UNAVAILABLE = "capability manager not available"

#: Upper bound on a capability package name. Generous for a real package id, but
#: it stops an unbounded string from reaching an edition's argv or a path join.
_MAX_CAPABILITY_PACKAGE_LEN = 200
#: Package-name charset. Deliberately permissive enough for the real shapes
#: (scoped npm ids, ``Pkg-1.0``, ``package/skill`` paths) while excluding
#: whitespace and every shell metacharacter.
#:
#: A leading ``@`` is allowed so a bare scoped npm id (``@scope/pkg``) is accepted,
#: but it must be FOLLOWED by an alphanumeric: what excluding ``-`` at position 0
#: buys is that a flag-shaped value can never be read as an option, and ``@-evil``
#: would hand ``-evil`` to an installer that strips the scope prefix.
_VALID_CAPABILITY_PACKAGE_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._@:/-]*$")


def _is_valid_capability_package(name: str) -> bool:
    """Return True if *name* is a well-formed, non-traversal package name.

    The structural twin of ``mcp._is_valid_mcp_name``, for the package-shaped ids
    the capability seam takes. Anchoring the first character to alphanumeric is
    what makes flag injection (``--force``, ``-o``) impossible, and ``..`` is
    rejected explicitly even though the charset would admit it.
    """
    if not name or len(name) > _MAX_CAPABILITY_PACKAGE_LEN:
        return False
    if ".." in name:  # reject path traversal even if it matches the charset
        return False
    return bool(_VALID_CAPABILITY_PACKAGE_RE.match(name))


def _audit_capability(operation: str, outcome: str, resource: str) -> None:
    """Emit a SEL line naming the package a capability mutation touched.

    ``sel_audit_middleware`` already logs every mutating request, but only with
    ``resources=request.path`` — it never reads the body. That records "an agent
    package was installed" and not WHICH one. Installing an agent package
    materializes new spawnable agent configs (persisted into ``config.json`` by
    ``_do_agents_sync`` and treated as the spawn allowlist by
    ``subagent._validate_agent``) plus new skills and prompt sources, so the
    package name is the one fact an incident responder needs. Mirrors
    ``mcp_discover``'s explicit per-outcome audit.
    """
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=f"capability:{resource}",
        )
    except Exception:  # audit must never change the outcome
        logger.debug("capability audit emit failed", exc_info=True)


async def api_capability_mcp_list(request: web.Request) -> web.Response:
    """GET /api/capability/mcp — list installed MCP servers (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_mcp())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_install(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/install — install an MCP server via the capability manager."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_mcp(server_id)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            # Off the loop: _sync_mcp_to_agent acquires bridges' synchronous
            # _mcp_lock and does a full RMW of kirocrew.json. If a concurrent app
            # registration holds that lock, a direct call would block the gateway
            # loop until it releases. Every other caller offloads — match it.
            await asyncio.to_thread(_sync_mcp_to_agent, server_id, True)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/uninstall — uninstall an MCP server via the capability manager."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_mcp(server_id)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            # Off the loop for the same reason as install: the synchronous
            # _mcp_lock RMW must not block the gateway if app registration holds it.
            await asyncio.to_thread(lambda: _sync_mcp_to_agent(server_id, False, remove=True))
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_list(request: web.Request) -> web.Response:
    """GET /api/capability/skills — list installed skill packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_skills())
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_install(request: web.Request) -> web.Response:
    """POST /api/capability/skills/install — install a skill package.

    Takes only ``package``; any version/source resolution is owned by the
    edition's capability manager (no Amazon-internal version-set field is
    exposed on the public API).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_skill(package)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        # Regenerate agent config to pick up new skill paths. install_agent()
        # does filesystem-heavy config rebuilding — offload it so it never
        # blocks the asyncio event loop (chat/heartbeat) under a slow FS.
        from kiro_crew.agent import install_agent  # noqa: F811

        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/skills/uninstall — uninstall a skill package."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_skill(package)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        from kiro_crew.agent import install_agent  # noqa: F811

        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def _mutate_agent_package(request: web.Request, *, install: bool) -> web.Response:
    """Shared body for the agent-package install/uninstall handlers.

    The two differ only in which seam op they call and the failure noun, and both
    must rebuild the agent config afterwards: an agent package carries agents plus
    its own skills and prompt sources, so the on-disk catalog and the generated
    agent config are both stale until ``install_agent()`` re-runs.

    Mirrors the guards ``mcp_discover.api_mcp_discover_install`` applies to the
    same seam: an allowlist on the name BEFORE it leaves core, ``_redact_external``
    on the manager's message, and an explicit SEL line naming the package.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # A body that is valid JSON but not an object (or carries a non-string
    # ``package``) must be a 400, not an unhandled AttributeError -> 500.
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    package = body.get("package")
    if not isinstance(package, str):
        return web.json_response({"error": "package required"}, status=400)
    package = package.strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    # The name crosses into the edition manager verbatim, and that manager owns
    # its own invocation grammar — so bound it here rather than trusting every
    # edition to reject a traversal or a shell metacharacter. Same allowlist the
    # MCP mutation endpoints use.
    if not _is_valid_capability_package(package):
        return web.json_response({"error": f"Invalid package name '{package[:64]}'"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    verb = "install" if install else "uninstall"
    try:
        res = await (mgr.install_agent(package) if install else mgr.uninstall_agent(package))
        if not res.ok:
            _audit_capability(f"capability_agent_{verb}", "error", package)
            # The message is edition subprocess output in all but name: it can
            # echo a registry URL with an embedded token, so it is redacted (both
            # scans) and length-bounded before reaching the dashboard.
            message = (res.message or f"{verb} failed")[:500]
            return web.json_response({"error": _redact_external(message)}, status=500)
        # Filesystem-heavy config rebuild — offload so it never blocks the asyncio
        # event loop (chat turn + liveness heartbeat) on a slow FS.
        from kiro_crew.agent import install_agent  # noqa: F811

        await asyncio.to_thread(install_agent)
        # list_agents() caches on a (count, newest-mtime-ns) signature, so a
        # mutation landing inside one mtime tick would otherwise serve a stale
        # catalog until some unrelated write bumped the signature.
        clear_list_agents_cache()
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        _audit_capability(f"capability_agent_{verb}", "ok", package)
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_capability_agents_install(request: web.Request) -> web.Response:
    """POST /api/capability/agents/install — install an agent package."""
    return await _mutate_agent_package(request, install=True)


async def api_capability_agents_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/agents/uninstall — uninstall an agent package."""
    return await _mutate_agent_package(request, install=False)


async def api_capability_plugins_list(request: web.Request) -> web.Response:
    """GET /api/capability/plugins — installed plugin packages + the drift set.

    Returns the installed rows AND ``out_of_sync`` in one response: the dashboard
    renders them together (a list plus a "reconcile N packages" affordance), and
    splitting them would mean two polls that can disagree mid-install.

    The two reads are independent, so they run CONCURRENTLY rather than in
    sequence. Each carries its own ``CAPABILITY_READ_TIMEOUT`` bound, and that
    bound is sized tight precisely because the dashboard POLLS the list endpoints
    — awaiting them one after the other would let a single request pend for twice
    the designed budget and accumulate pending gateway tasks per poll, which is
    the wedge class the bound exists to prevent. Gathering caps the endpoint at
    one read bound and halves its latency.
    """
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        plugins, out_of_sync = await asyncio.gather(mgr.list_plugins(), mgr.plugins_out_of_sync())
        return web.json_response({"plugins": plugins, "out_of_sync": out_of_sync})
    except Exception as exc:
        return _err500(exc)


async def api_capability_plugins_sync(request: web.Request) -> web.Response:
    """POST /api/capability/plugins/sync — reconcile plugins with agent packages."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.sync_plugins()
        if not res.ok:
            _audit_capability("capability_plugins_sync", "error", "*")
            message = (res.message or "sync failed")[:500]
            return web.json_response({"error": _redact_external(message)}, status=500)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        _audit_capability("capability_plugins_sync", "ok", "*")
        # Redacted AND length-bounded on the success path too: this message names
        # what was reconciled and can carry edition subprocess output, so it gets
        # the same treatment as the failure path rather than passing through raw.
        return web.json_response(
            {"ok": True, "message": _redact_external((res.message or "")[:500])}
        )
    except Exception as exc:
        return _err500(exc)


async def api_agents_installed(request: web.Request) -> web.Response:
    """GET /api/agents/installed — list all installed kiro-cli agents.

    kirocrew is always first; kirocrew-lite is excluded.

    Deliberately GLOBAL-only (no project scope): every frontend consumer of this
    endpoint is an agent CRUD/editor surface (Agents page, template editor) whose
    actions persist into the global configuration — "Set as default" writes the
    selected name into ``cfg.agents``. A project-scope row here would let that
    action persist a name that exists only inside one checkout, producing a
    default agent the config cannot resolve. Project-scope discovery instead
    reaches the surfaces that DISPATCH agents: per-turn resolution
    (``resolve_agent_bindings(..., project_dir=...)``), spawn validation, and
    Slack — see ``agent_discovery.project_agent_names``.
    """
    # list_agents() does glob + per-file resolve(strict=True) + read_bytes +
    # json.loads over ~/.kiro/agents — blocking filesystem work that, on a large
    # agents dir (network home, many project-registry agents), can stall the
    # event loop past the loop-stall watchdog when a browser loads the dashboard.
    # Offload to the discovery pool, same as /api/skills.
    def _collect() -> list[Any]:
        agents = list(list_agents())
        agents.sort(key=lambda a: (0 if a.name == "kirocrew" else 1, a.name))
        return agents

    agents = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _collect)
    return web.json_response([a.to_dict() for a in agents])


def _normalize_model_key(name: str) -> str:
    """Canonical key for de-duping CC model ids across spelling variants.

    The claude-agent-acp adapter advertises dashed ids (``claude-opus-4-7``)
    while curated/config entries may use dotted versions (``claude-opus-4.7``);
    case can also differ. Without normalization the same model surfaces twice in
    the dropdown (one curated row + one advertised row). Lowercase and fold
    ``.``→``-`` so equivalent ids collapse to one entry. ``default`` and ``auto``
    both mean "let the backend pick", so they map to a single key too.
    """
    key = (name or "").strip().lower().replace(".", "-")
    if key in ("default", "auto"):
        return "auto"
    return key


def _advertised_cc_models(request: web.Request) -> list[dict]:
    """Map the first active CC provider's advertised models to the API shape.

    claude-agent-acp captures its real versioned list at session init (see
    AcpClient._capture_available_models). Backend provider ids are mapped back to
    canonical registry keys (``from_provider_id``) so they dedup cleanly against
    the registry rows in :func:`_cc_models` and the wire value stays canonical.
    A provider id with no registry entry passes through unchanged (forward-compat
    for models the registry doesn't list yet). Returns ``[]`` when no session has
    initialized or the backend advertised nothing.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in providers:
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            advertised = getter()
        except Exception:
            continue
        if advertised:
            return [
                {
                    "model_name": model_registry.from_provider_id(
                        m.get("modelId", ""), "claude_code"
                    ),
                    "display_name": m.get("name", "") or m.get("modelId", ""),
                    "description": m.get("description", ""),
                }
                for m in advertised
                if m.get("modelId")
            ]
    return []


def _entitled_kiro_models(request: web.Request, models: list[dict]) -> list[dict]:
    """Narrow the ``--list-models`` catalog to what a live session advertises.

    ``kiro chat --list-models`` is a CATALOG, not an entitlement: it returns the
    same rows whatever the account's tier, so after a downgrade the picker kept
    offering (and kept SHOWING as selected) a model no turn can run. The
    per-session ``session/new`` ``availableModels`` list is the tier-aware one —
    the same signal ``model_is_unusable`` pre-flights against before the wire —
    so when a live session has one, it wins here too. Same rule #1549 applied to
    the claude_code branch in :func:`_cc_models`: advertised is authoritative
    when present.

    The keep/drop decision delegates to ``model_is_unusable`` rather than
    comparing ids here, so the picker cannot disagree with the wire about what
    "this account can run" means. A local comparison would be a second spelling
    of that question — the exact drift that predicate exists to prevent — and any
    difference in how the two fold spelling variants shows up as a row the picker
    offers and the wire then withholds.

    The ``auto`` sentinel is never filtered: it means "inherit whatever the
    session already resolved", so it stays selectable even on a backend that does
    not advertise it by name.

    Fails open in every unknowable case — no live session, a backend that
    advertises nothing, or an advertised set that does not intersect the catalog
    at all (a namespace mismatch rather than an entitlement, e.g. the claude
    backend's bare ids). Filtering on any of those would empty the picker, which
    is worse than listing one model too many.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return models
    advertised: list[str] = []
    # Newest session first. `active_providers()` walks a dict of live sessions, so
    # forward order is creation order — and a session that started BEFORE a plan
    # change still holds the advertised list it captured at its own session/new.
    # Reading the oldest one would narrow the catalog to pre-downgrade
    # entitlements, i.e. keep offering exactly the models this narrowing exists to
    # hide. The most recently started session carries the most recent snapshot.
    for provider in reversed(providers):
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
            advertised = ids
            break
    if not advertised:
        return models
    advertises_auto = any(_normalize_model_key(i) == "auto" for i in advertised)
    kept = [
        m
        for m in models
        if _normalize_model_key(m.get("model_name", "")) == "auto"
        or not model_is_unusable(m.get("model_name", ""), advertised)
    ]
    # Tell "not comparable" apart from "entitled to almost nothing". A backend
    # that advertises `auto` shares a namespace with the catalog by definition, so
    # `auto` alone is a real answer — the most restricted tier there is — and must
    # narrow the picker to it. Only when nothing at all lines up, `auto` included,
    # is this a namespace mismatch (bare vs prefixed provider ids) where showing
    # the whole catalog beats emptying the picker. `auto` is always kept, so it can
    # never serve as the evidence that the two sides are comparable.
    if not advertises_auto and not any(
        _normalize_model_key(m.get("model_name", "")) != "auto" for m in kept
    ):
        return models
    return kept


def _cc_models(request: web.Request, configured_default: str = "") -> list[dict]:
    """Assemble the CC model dropdown, scoped to what the account can actually use.

    The live backend's advertised set is AUTHORITATIVE when present. It is the
    only source that reflects entitlement: claude-agent-acp captures it at session
    init from what the signed-in account is actually served. The registry is a
    static catalog of everything KiroCrew knows how to name, so a free-tier user
    used to be offered the full flagship list and only discovered the truth when a
    prompt failed.

    So when anything is advertised, registry rows are FILTERED DOWN to it (keeping
    the registry's cleaner display names for the survivors), and advertised models
    the registry does not list are appended for forward-compat.

    When NOTHING is advertised the registry is shown unfiltered. That is not a
    fallback to the old behaviour by preference -- an empty advertised set means
    "no session has initialized yet", which is indistinguishable from "this account
    gets nothing", and showing an empty picker on a cold dashboard would be worse
    than showing a superset.

    ``auto`` is always present and always FIRST. It is the configured default
    (``config.agent.model``) and a sentinel rather than a real model, so it is
    never filtered by entitlement. It leads the list because the registry's own
    ``default: true`` flag sorts the current flagship to the top, which presented
    a specific paid model as the default in the picker.
    """
    advertised = _advertised_cc_models(request)
    registry_rows = model_registry.display_list("claude_code")

    if advertised:
        advertised_keys = {
            _normalize_model_key(e.get("model_name", ""))
            for e in advertised
            if _normalize_model_key(e.get("model_name", ""))
        }
        # Keep registry rows only when the backend also advertises them; "auto" is
        # a sentinel, not an entitlement, so it survives regardless.
        registry_rows = [
            e
            for e in registry_rows
            if _normalize_model_key(e.get("model_name", "")) in advertised_keys
            or _normalize_model_key(e.get("model_name", "")) == "auto"
        ]

    merged: list[dict] = []
    seen: set[str] = set()
    for entry in (*registry_rows, *advertised):
        name = entry.get("model_name", "")
        key = _normalize_model_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    # "auto" leads. It may be absent entirely if a future registry drops the row,
    # so synthesize it rather than assuming the filter above preserved one.
    merged = [e for e in merged if _normalize_model_key(e.get("model_name", "")) == "auto"] + [
        e for e in merged if _normalize_model_key(e.get("model_name", "")) != "auto"
    ]
    if not any(_normalize_model_key(e.get("model_name", "")) == "auto" for e in merged):
        merged.insert(0, {"model_name": "auto", "display_name": "Auto", "description": ""})
        seen.add("auto")
    # Guarantee the configured default is present (e.g. a custom cc_model the
    # backend doesn't advertise) so the selected model never vanishes. Resolve it
    # to its canonical key first (it may be stored as a provider id or alias) so a
    # default that already maps to a registry row does NOT produce a duplicate.
    if configured_default:
        canonical_default = model_registry.from_provider_id(
            model_registry.to_provider_id(configured_default, "claude_code"), "claude_code"
        )
        # Skip a blank canonical key: cc_model="auto" round-trips to "" (auto's
        # provider id is empty), and _normalize_model_key("")=="" is never in
        # `seen` (which holds "auto"), so without the `if key` guard — the same
        # one the merge loop above uses — a blank-named row would be inserted as
        # the first/selected dropdown option. The "auto" registry row already
        # covers this case.
        key = _normalize_model_key(canonical_default)
        # Only resurrect the configured default when entitlement cannot contradict
        # it: either nothing was advertised (unknown, so trust config) or it WAS
        # advertised but the registry lacked a row. Force-including a model the
        # backend did not advertise would reintroduce exactly the unusable option
        # this filter removes -- a stale config pick outliving the entitlement.
        may_include = not advertised or key in {
            _normalize_model_key(e.get("model_name", "")) for e in advertised
        }
        if key and key not in seen and may_include:
            # After "auto", never before it: "auto" is the configured default in
            # the general case and leads the list.
            merged.insert(
                1 if merged and _normalize_model_key(merged[0].get("model_name", "")) == "auto"
                else 0,
                {
                    "model_name": canonical_default,
                    "display_name": canonical_default,
                    "description": "Configured default",
                },
            )
    # Enrich every row with a context_window via the central authority so the CC
    # dropdown carries the same field the kiro branch does (the frontend picker
    # + tooltip read it uniformly). None -> reference (never a silent 200k).
    for entry in merged:
        if "context_window" not in entry:
            name = entry.get("model_name", "")
            entry["context_window"] = (
                model_registry.model_window(name) or model_registry.REFERENCE_WINDOW_TOKENS
            )
    return merged


def _wrap_list_models_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """Sandbox-wrap the ``--list-models`` argv at the configured tier.

    Runs in an executor, never on the loop: :func:`configured_sandbox_mode` stats
    (and on a cache miss re-reads and revalidates) ``config.json``, and
    ``wrap_argv`` -> ``detect_backend`` can cold-probe the sandbox backend with a
    synchronous ``subprocess.run(..., timeout=5)``. Resolving the mode here rather
    than passing it in keeps BOTH blocking reads in the worker thread.

    ``is_kiro_cli=True`` is explicit because ``_spawns_kiro_cli``'s basename test
    only matches a literal ``kiro-cli``: a Windows ``kiro-cli.exe``, a wrapper
    shim, or a ``KIROCREW_KIRO_BIN`` pointing at a nonstandard launch path all
    read as "not kiro-cli". On macOS with ``agent.sandbox="off"`` that
    misclassification skips the delegation branch — and with it the credential-env
    scrub — so the child would inherit the sensitive environment. Both ACP spawn
    paths pass this flag for the same reason.
    """
    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)


async def api_models(request: web.Request) -> web.Response:
    """GET /api/models — list available models from the live kiro-cli ACP session."""
    # Signed-out gateways must never reach the spawn below. kiro-cli auto-opens
    # an interactive browser login for ANY subcommand run unauthenticated
    # (--no-interactive does not suppress it, and there is no opt-out env var),
    # and the frontend polls this endpoint every 8s while the model list is
    # degraded — which is exactly the signed-out state. Ungated, that pairing
    # opened a browser window every 8s indefinitely. The 503 is the same
    # degraded response the timeout/unresolved branches already return, so the
    # client contract is unchanged; only the subprocess is skipped.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    kiro_bin: str | None = None
    try:
        from kiro_crew.acp.client import (  # noqa: F811
            _resolve_kiro_bin_for_spawn,
            _resolve_ssh_auth_sock,
        )
        from kiro_crew.env import augmented_path  # noqa: F811

        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # Degraded (binary not resolved yet), NOT a genuine "zero models"
            # result. Return 503 so the client retries instead of caching an
            # empty list — a cached [] renders an empty picker that only a
            # manual page refresh recovers from.
            return web.json_response({"error": "kiro binary not resolved"}, status=503)
        argv = [kiro_bin, "chat", "--list-models", "--format", "json", "--no-interactive"]
        # Mirror AcpClient._spawn() sandbox: wrap_argv + env + process isolation.
        # Note: AcpClient._spawn() is for interactive ACP sessions (stdin/stdout
        # pipes); this is a one-shot read-only command, so we replicate the
        # sandbox setup directly.  See the security-controls rule.
        #
        # The configured tier is passed EXPLICITLY rather than left to
        # wrap_argv's "auto" parameter default, so this endpoint can never ask
        # for stricter isolation than the chat spawn of the same binary. It
        # matters wherever the operator set agent.sandbox="off" (deferring
        # isolation to kiro-cli's own internal sandbox) on a host with no backend
        # — any Windows host, macOS >= 26: chat runs, while the default-mode wrap
        # here fail-closed and answered 503 on every 8s poll. The frontend reads
        # that as "degraded" and serves its auto-only fallback list, so the picker
        # showed exactly one entry. Same fix, same reason as the `_bg` session in
        # session.py.
        #
        # OFF the loop: `configured_sandbox_mode()` stats (and on a cache miss
        # re-reads + revalidates) config.json, and `wrap_argv` -> `detect_backend`
        # can cold-probe the backend with a synchronous
        # `subprocess.run(..., timeout=5)`. This endpoint is polled every 8s while
        # the model list is degraded, so leaving either on the loop stalls chat,
        # cron and the liveness heartbeat on exactly the host where the probe is
        # slowest. Both reads run in the worker, so the mode is resolved there
        # too rather than passed in.
        argv, cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _wrap_list_models_argv, argv
        )
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
        try:
            env = {**os.environ}
            env["PATH"] = augmented_path(env.get("PATH", ""))
            _resolve_ssh_auth_sock(env)
            proc = await create_subprocess_limited(
                *argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()
                # A cold CLI spawn exceeded the timeout. This is the common
                # cause of the "picker is empty until I refresh" symptom: a
                # slow first `--list-models` spawn returning [] (HTTP 200) would
                # be cached by the client as a successful empty result. Return
                # 503 instead so React Query retries with backoff and the
                # picker self-heals without a manual refresh.
                logger.warning("api_models: --list-models timed out; returning 503")
                return web.json_response({"error": "model list timed out"}, status=503)
        finally:
            if cleanup and callable(cleanup):
                cleanup()

        if proc.returncode != 0:
            from kiro_crew.platform import redact_via_context  # noqa: F811

            stderr_tail = stderr.decode(errors="replace").strip()
            stderr_tail = redact_via_context(stderr_tail)[-_MODEL_LIST_STDERR_TAIL_CHARS:]
            logger.warning(
                "api_models: --list-models exited %s: %s; returning 503",
                proc.returncode,
                stderr_tail or "<no stderr>",
            )
            return web.json_response({"error": "model list command failed"}, status=503)

        if not stdout.strip():
            logger.warning("api_models: --list-models returned empty output; returning 503")
            return web.json_response({"error": "model list returned empty output"}, status=503)

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "api_models: --list-models returned invalid JSON (%s); returning 503",
                exc,
            )
            return web.json_response({"error": "model list returned invalid JSON"}, status=503)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            logger.warning("api_models: --list-models returned an invalid payload; returning 503")
            return web.json_response(
                {"error": "model list returned an invalid payload"}, status=503
            )
        models = data["models"]
        # Seed the central window authority from kiro's authoritative structured
        # 'context_window_tokens' field (keyed by model_id/model_name). This is
        # the ONE place these rows enter the system; every other consumer (the
        # ACP backfill, the context-budget scaler, the live meter) then resolves
        # through model_registry.model_window() rather than re-reading kiro. The
        # in-memory update is synchronous (cheap dict mutation); only the disk
        # persist is offloaded to an executor so the event loop never blocks on
        # filesystem I/O (no blocking call on the event loop).
        #
        # This fork keeps kiro's bare-dotted ids as the picker WIRE FORMAT
        # (guarded by _model_rejected_reason / api_chat_slot_model, which rejects
        # canonical registry keys the ACP CLI can't accept). The upstream
        # registry-key canonicalization is deliberately NOT ported — it is
        # incompatible with this fork's _model_rejected_reason guard. The window
        # seeding above uses kiro's authoritative context_window_tokens to give
        # the backfill real GPT/DeepSeek/Qwen windows, independent of the
        # wire-format choice.
        if model_registry.refresh_kiro_windows(models):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), model_registry.persist_kiro_windows
            )
        models = [m for m in models if not is_deprecated_model(m.get("model_name", ""))]
        models = _entitled_kiro_models(request, models)
        return web.json_response(models)
    except SandboxUnavailableError as exc:
        # Narrower than the generic clause below, and BEFORE it: this is the one
        # degraded cause that no amount of retrying fixes, so it must not be
        # reported as an anonymous "model list unavailable". Reached only when the
        # tier resolved to "auto" (the shipped default) on a host with no
        # backend, where a configured "off" passes through
        # configured_sandbox_mode() above and never lands here.
        #
        # Still a 503: the client contract for "degraded, keep the last-good list
        # and poll" is what keeps the picker from caching an empty result, and a
        # 4xx here would make the frontend treat a host-capability problem as a
        # bad request. The `code` is what lets the UI tell this apart from a
        # timeout, and the log carries the sandbox layer's own remedy text (which
        # names the agent.sandbox_allow_unsandboxed_exec opt-in).
        logger.warning(
            "api_models: sandbox refused the --list-models spawn (kind=%s, detail=%s); "
            "returning 503. Retrying will not clear this — %s",
            exc.kind,
            exc.detail,
            exc,
        )
        return web.json_response(
            {"error": "model list unavailable", "code": "model_list_sandbox_unavailable"},
            status=503,
        )
    except Exception:
        # Spawn failure, JSON parse error, etc. — degraded, not "zero models".
        # 503 so the client retries instead of caching an empty picker.
        logger.warning("api_models failed; returning 503 for client retry", exc_info=True)
        return web.json_response({"error": "model list unavailable"}, status=503)


async def api_effort_levels(request: web.Request) -> web.Response:
    """GET /api/effort-levels — list available reasoning effort levels.

    Per-slot: when a ``?slot=`` query param resolves to a live ACP provider,
    return the levels that slot's CURRENT model reported (ACP escalation order),
    so concurrent slots on different models/backends each see their own set and
    a model switch is reflected immediately. Falls back to the process-global
    ordered list (cold start / no live provider / provider without the getter).
    """
    slot = request.query.get("slot")
    if slot:
        try:
            state: DashboardState = request.app["state"]
            provider = state.sessions.get_provider(_history_key_for(slot))
            getter = getattr(provider, "get_valid_effort_levels", None) if provider else None
            if callable(getter):
                levels = getter()
                if levels:
                    return web.json_response(levels)
        except (KeyError, AttributeError):
            pass
    return web.json_response(get_reasoning_effort_ordered())


async def api_slash_commands(request: web.Request) -> web.Response:
    """GET /api/slash-commands — list available slash commands (provider-aware)."""
    cfg = KiroCrewConfig.load()
    if cfg.agent.provider == "claude_code":
        state: DashboardState = request.app["state"]
        cc_commands: list[str] = []
        for provider in state.sessions.active_providers():
            cmds = getattr(provider, "_slash_commands", [])
            if cmds:
                cc_commands = cmds
                break
        if not cc_commands:
            cc_commands = [
                "compact",
                "clear",
                "context",
                "help",
                "init",
                "review",
                "security-review",
                "usage",
            ]
        result = [
            {"name": f"/{c}", "description": SLASH_COMMAND_DESCRIPTIONS.get(f"/{c}", "")}
            for c in cc_commands
            if f"/{c}" not in _BLOCKED_SLASH_COMMANDS
        ]
        result.append({"name": "/side", "description": SLASH_COMMAND_DESCRIPTIONS.get("/side", "")})
        return web.json_response(result)

    # Blocked commands stay in _SLASH_COMMANDS (typing one still gets the
    # explicit "not available in the dashboard" rejection in chat_runner), but
    # the suggestion payload must not advertise them: a menu entry that only
    # ever produces a warning is an inert affordance.
    return web.json_response(
        [
            {"name": c, "description": SLASH_COMMAND_DESCRIPTIONS.get(c, "")}
            for c in sorted(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)
        ]
    )


async def api_agent_detail(request: web.Request) -> web.Response:
    """GET/DELETE/PATCH /api/agents/detail/{name} — view, delete, or update agent config."""
    name = request.match_info["name"]
    from kiro_crew.agent import kiro_agents_dir_path  # noqa: F811

    # Parse body early so JSONDecodeError returns 400, not 404 from the file loop.
    patch_body = None
    if request.method == "PATCH":
        try:
            patch_body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        # Valid JSON is not necessarily an object. A top-level array makes
        # ``"skills" in patch_body`` a LIST-membership test (true for
        # ``["skills"]``), and the subscript that follows then raises TypeError
        # -> HTTP 500. Reject the shape once, here, rather than per-field.
        if not isinstance(patch_body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

    state: DashboardState = request.app["state"]
    for f in kiro_agents_dir_path().glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("name") == name or f.stem == name:
                if request.method == "DELETE":
                    if f.name in (
                        "kirocrew.json",
                        "kirocrew-lite.json",
                    ):
                        return web.json_response({"error": "cannot delete kirocrew"}, status=400)
                    # The dashboard withholds delete for a template that is the
                    # kiro-cli fallback or is bound to a crew, but a client-side
                    # check cannot be race-free: its view of the config is a
                    # cached snapshot, so a crew bound (or the fallback moved)
                    # between render and click still reaches this handler. Only
                    # a check that reads config UNDER THE SAME LOCK the writers
                    # take makes the invariant hold, so this is the authority
                    # and the UI is now just the early, friendlier signal.
                    async with _get_config_lock():
                        # Off-thread: this runs while the config lock is HELD, so a
                        # synchronous read stalls the event loop AND queues every
                        # other config writer behind a disk read. `to_thread` is the
                        # idiom already used for this in core.py / files.py.
                        cfg = await asyncio.to_thread(KiroCrewConfig.load)
                        # Every identifier this file answers to. The match above
                        # accepts EITHER the JSON's own "name" or the filename
                        # stem, so a request naming one leaves the other out of
                        # `name` -- and config may well record the one omitted.
                        #
                        # `declared` goes through `spec_str`, the helper this file
                        # already uses on the GET path: `~/.kiro/agents` is a
                        # SHARED directory and a structured `name` (an ACP-style
                        # `{"id": ...}`) is observed in the wild. It is reachable
                        # here via the `f.stem == name` arm of the match, and an
                        # unhashable set member turns this into a TypeError --
                        # which the `except (JSONDecodeError, OSError)` below does
                        # NOT catch, so it escapes as HTTP 500 on a delete that
                        # should have succeeded or returned a 409.
                        declared = spec_str(data, "name")
                        aliases = {name, f.stem}
                        if declared:
                            aliases.add(declared)
                        # Both fields are checked because the two readers disagree:
                        # `agent.default_agent` is the kiro-cli fallback, while
                        # top-level `default_agent` is what /api/config/default-agent
                        # (the picker on this very page) writes. Guarding only one
                        # leaves the other free to dangle.
                        if aliases & {cfg.agent.default_agent, cfg.default_agent}:
                            return web.json_response(
                                {
                                    "error": (
                                        f"Cannot delete '{name}': it is the default agent used "
                                        "when nothing names a template. Change the default first."
                                    ),
                                    "code": "agent_is_default",
                                },
                                status=409,
                            )
                        bound = sorted(
                            crew
                            for crew, crew_cfg in cfg.agents.items()
                            if crew_cfg.kiro_agent in aliases
                        )
                        if bound:
                            return web.json_response(
                                {
                                    "error": (
                                        f"Cannot delete '{name}': still used by "
                                        f"{', '.join(bound)}. Repoint or remove them first."
                                    ),
                                    "code": "agent_in_use",
                                },
                                status=409,
                            )
                        f.unlink()
                    clear_list_agents_cache()
                    # Same reason `declared` goes through `spec_str` above:
                    # `prune` is typed `str` and a structured `name` would reach
                    # it through the `or`.
                    agent_state.prune(declared or name)
                    state.push_refresh("agents")
                    return web.json_response({"ok": True})
                if request.method == "PATCH" and patch_body is not None:
                    if "skills" in patch_body:
                        raw_skills = patch_body["skills"]
                        if not isinstance(raw_skills, list) or not all(
                            isinstance(s, str) for s in raw_skills
                        ):
                            return web.json_response(
                                {"error": "skills must be a list of strings"}, status=400
                            )
                        if len(raw_skills) > MAX_AGENT_SKILLS:
                            return web.json_response(
                                {"error": f"at most {MAX_AGENT_SKILLS} skills per agent"},
                                status=400,
                            )
                    mapped: list[str] = []
                    loop = asyncio.get_running_loop()
                    async with _get_config_lock():
                        # Re-read under the lock: the copy above was read before
                        # the lock and a concurrent PATCH may have superseded it.
                        data = json.loads(f.read_text(encoding="utf-8"))
                        agent_name = data.get("name") or name
                        # Skills FIRST, before any state mutation. The mapping can
                        # reject the request (unknown key -> 400) and the model
                        # branch below writes the agent_state sidecar; doing model
                        # first meant a rejected combined PATCH still froze the
                        # model against future shipped-default bumps.
                        #
                        # Offloaded to the discovery pool: the mapping enumerates
                        # the skill roots (see enumerate_skill_catalog), which on a
                        # large or network-backed catalog is enough filesystem work
                        # to stall the event loop — the same reason /api/skills and
                        # /api/agents/installed run off the loop.
                        if "skills" in patch_body:
                            mapped, unknown = await loop.run_in_executor(
                                discovery_executor(),
                                apply_skill_mapping,
                                data,
                                f,
                                state,
                                list(patch_body["skills"]),
                                _read_session_key(request),
                            )
                            if unknown:
                                return web.json_response(
                                    {"error": "unknown skills", "skills": unknown[:20]},
                                    status=400,
                                )
                        else:
                            mapped = await loop.run_in_executor(
                                discovery_executor(),
                                agent_skill_keys,
                                data,
                                f,
                                state,
                                _read_session_key(request),
                            )
                        if "model" in patch_body:
                            # Stored verbatim (canonical key); translated to a
                            # provider id at the config.loader factory boundary.
                            data["model"] = patch_body["model"] or None
                            if data["model"] is None:
                                data.pop("model", None)
                                # Cleared/auto: resume tracking the shipped
                                # default (re-synced by _refresh_dynamic_fields).
                                agent_state.set_model_managed(agent_name, True)
                            else:
                                # Explicit pick: freeze it against default bumps.
                                agent_state.set_model_managed(agent_name, False)
                        # Never persist KiroCrew bookkeeping into the kiro spec —
                        # kiro-cli rejects unknown fields and drops the agent.
                        data.pop("model_managed", None)
                        data.pop("cc_model", None)
                        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    # The list_agents() cache keys on a (count, newest-mtime-ns)
                    # signature; two writes inside the same mtime granularity
                    # would otherwise serve a stale skill list.
                    clear_list_agents_cache()
                    state.push_refresh("agents")
                    return web.json_response(
                        {"ok": True, "model": data.get("model", ""), "skills": mapped}
                    )
                # ``skills`` / ``unmanaged_skills`` are computed, response-only
                # views of ``resources`` — never written back into the spec
                # (kiro-cli rejects unknown fields and drops the agent). One
                # catalog walk for both, off the event loop (filesystem-heavy).
                keys, unmanaged_uris = await asyncio.get_running_loop().run_in_executor(
                    discovery_executor(),
                    agent_skill_views,
                    data,
                    f,
                    state,
                    _read_session_key(request),
                )
                return web.json_response(
                    {
                        **data,
                        # The rest of the spec is passed through verbatim, but
                        # these two are CONSUMED as display text by the detail
                        # panel. A foreign spec's structured value rendered as a
                        # React child throws error #31 and blanks the whole tab,
                        # so both are coerced on the same "non-string means
                        # absent" rule list_agents() uses.
                        "description": spec_str(data, "description"),
                        "model": spec_model(data),
                        "skills": keys,
                        "unmanaged_skills": unmanaged_uris,
                    }
                )
        except (json.JSONDecodeError, OSError):
            continue
    # "default" is the built-in agent with no config file
    if name == "default":
        if request.method != "GET":
            return web.json_response({"error": "cannot modify built-in default agent"}, status=400)
        return web.json_response({"name": "default", "model": ""})

    return web.json_response({"error": "not found"}, status=404)


async def api_capability_agents_list(request: web.Request) -> web.Response:
    """GET /api/capability/agents — list installed agent packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_agents())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_registry(request: web.Request) -> web.Response:
    """GET /api/capability/mcp/registry — browse available MCP servers from the registry.

    The capability manager owns registry-output parsing and returns entries
    directly (conventional keys: id, installed, title, tier, description); the
    core passes them through verbatim.
    """
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response({"servers": await mgr.registry()})
    except Exception as exc:
        return _err500(exc)


# ── KiroCrew Agent CRUD API ──


async def api_kirocrew_agents(request: web.Request) -> web.Response:
    """GET /api/agents — list all Kiro Crew agent definitions, most-used first.

    Also surfaces the requesting session's project-scope agents
    (``<project>/.kiro/agents``, resolved via ``X-Session-Key``) tagged
    ``scope="project"`` — these dispatch from that slot because kiro-cli runs
    with the slot's project as cwd, so the picker must offer them (#1684's
    headline). A config alias of the same name is listed once, as the alias:
    dispatch resolves aliases first, so the alias is what would answer.
    """
    cfg = KiroCrewConfig.load()
    agents = [
        {"name": name, "scope": "global", **dataclasses.asdict(agent_cfg)}
        for name, agent_cfg in cfg.agents.items()
    ]

    state: DashboardState | None = request.app.get("state")

    # Project rows come from a directory scan, so it runs on the discovery
    # pool — same rule as every other agent listing: no filesystem I/O on the
    # event loop. Failure costs only the project rows, never the roster.
    project_dir = active_project_dir(state, _read_session_key(request)) if state else ""
    if project_dir:
        try:
            project_names = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(), project_agent_names, project_dir
            )
        except Exception:
            logger.warning("Failed to list project agents for %s", project_dir, exc_info=True)
            project_names = frozenset()
        base = dataclasses.asdict(KiroCrewAgentConfig())
        agents.extend(
            {"name": name, "scope": "project", **base}
            for name in sorted(project_names - set(cfg.agents.keys()))
        )

    # Reorder by usage frequency (most-used first). Derived read-only from chat
    # history; degrade to config-insertion order on any failure so the dropdown
    # never breaks or drops agents when history is unreadable.
    conversation_log = state.conversation_log if state else None
    if conversation_log:
        try:
            usage = await asyncio.to_thread(conversation_log.agent_usage)
            # Default missing agents to (0, 0) — keeps the sort key total and
            # deterministic (never negates None); never-used agents collapse to
            # their config-insertion index and form a stable bottom block.
            sorted_agents = sorted(
                enumerate(agents),
                key=lambda item: (
                    -usage.get(item[1]["name"], (0, 0.0))[0],
                    -usage.get(item[1]["name"], (0, 0.0))[1],
                    item[0],
                ),
            )
            agents = [a for _, a in sorted_agents]
        except Exception:
            logger.warning("Failed to sort agents by usage; using config order", exc_info=True)

    return web.json_response(
        {
            "agents": agents,
            "default_agent": cfg.default_agent,
        }
    )


_config_lock: asyncio.Lock | None = None
_config_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_config_lock() -> asyncio.Lock:
    """Return a config lock bound to the current event loop (Python 3.10 compat)."""
    global _config_lock, _config_lock_loop
    loop = asyncio.get_running_loop()
    if _config_lock is None or _config_lock_loop is not loop:
        _config_lock = asyncio.Lock()
        _config_lock_loop = loop
    return _config_lock


async def api_kirocrew_agents_sync(request: web.Request) -> web.Response:
    """POST /api/agents/sync — auto-sync AIM-installed agents into config.json."""
    async with _get_config_lock():
        return await _do_agents_sync(request)


async def _do_agents_sync(request: web.Request) -> web.Response:

    cfg = KiroCrewConfig.load()
    synced: list[str] = []
    pruned: list[str] = []
    try:
        discovered_agents = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), lambda: list(list_agents())
        )
        discovered_names = {a.name for a in discovered_agents}

        # Add new agents
        from kiro_crew.agent import kiro_agents_dir_path  # noqa: F811

        mc_kiro_agents = {a.kiro_agent for a in cfg.agents.values()}
        for disc in discovered_agents:
            if (
                disc.name not in mc_kiro_agents
                and disc.name not in cfg.agents
                and disc.source != "kirocrew"
            ):
                # EXECUTABLE INVARIANT enforcement (mirrors the seam-boundary
                # LIVENESS bound in platform.capability_bound —
                # BoundedCapabilityManager): a builtin_agents() row MUST be
                # spawnable. The core can only verify the on-disk case
                # (~/.kiro/agents/<name>.json); an edition may also make a
                # row ACP-resolvable WITHOUT an on-disk file, so we WARN rather
                # than hard-drop — dropping a legitimately ACP-resolvable agent
                # would itself be a correctness bug. The warning turns an
                # otherwise silent spawn-time (ACP session/set_mode) failure into
                # an actionable log line pointing at the offending seam row.
                # Upstream resolves the agents dir per call (data-home safety);
                # the namespaced check is for app-provided agents, which live as
                # `<app>--<agent>.json` and would otherwise look "missing".
                # Off the loop: both the stat and the namespaced glob touch the
                # filesystem, and on a populated agents directory this per-agent
                # check (in a loop) would stall the gateway loop and heartbeat.
                _dn = disc.name
                _has_on_disk = await asyncio.to_thread(
                    lambda: (kiro_agents_dir_path() / f"{_dn}.json").exists()
                    or _namespaced_agent_file_exists(_dn)
                )
                if not _has_on_disk:
                    logger.warning(
                        "syncing agent %r (source=%s) with no on-disk config at "
                        "%s — if it is not ACP-resolvable it will persist into "
                        "config.json and fail at spawn (builtin_agents EXECUTABLE "
                        "INVARIANT)",
                        disc.name,
                        disc.source,
                        kiro_agents_dir_path() / f"{disc.name}.json",
                    )
                cfg.agents[disc.name] = KiroCrewAgentConfig(
                    kiro_agent=disc.name,
                    description=disc.description,
                    source=disc.source,
                )
                synced.append(disc.name)

        # Prune agents whose kiro_agent file no longer exists on disk.
        # Only prune package-installed agents (never user-created or kirocrew-owned).
        # Skip pruning if scan returned nothing -- likely a transient issue.
        # Invariant: for package-sourced entries, kiro_agent == dict key == agent name.
        # ("aim" is also accepted for backward-compat with older configs.)
        if discovered_names:
            for name, agent_cfg in list(cfg.agents.items()):
                if agent_cfg.source in ("package", "aim") and (
                    agent_cfg.kiro_agent not in discovered_names
                ):
                    del cfg.agents[name]
                    pruned.append(name)
    except Exception:
        logger.warning("Failed to scan installed agents", exc_info=True)
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="failure",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync failure", exc_info=True)
        return web.json_response({"ok": False, "error": "sync failed", "synced": []}, status=500)

    if synced or pruned:
        try:
            cfg.save()
        except Exception:
            logger.warning("Failed to save config after agent sync", exc_info=True)
            try:
                _sel().log_api_access(
                    caller=request.get("user", "dashboard"),
                    operation="agent.auto_sync",
                    outcome="failure",
                    source="agent_sync",
                )
            except Exception:
                logger.warning("SEL logging failed for config save failure", exc_info=True)
            return web.json_response(
                {"ok": False, "error": "config save failed", "synced": []}, status=500
            )
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="success",
                source="agent_sync",
                resources=", ".join(synced + [f"-{p}" for p in pruned]),
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync success", exc_info=True)
    else:
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="noop",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync noop", exc_info=True)

    if pruned:
        logger.info("Pruned %d stale package agents: %s", len(pruned), ", ".join(pruned))

    return web.json_response({"ok": True, "synced": synced, "pruned": pruned})


async def api_kirocrew_agent_resolved_model(request: web.Request) -> web.Response:
    """GET /api/agents/resolved-model?agent=NAME — the model a new session uses.

    Serves the one backend resolver so the dashboard's model chip does not have
    to re-derive the precedence client-side (and drift from it). ``agent`` is a
    KiroCrew agent name; omitted falls back to the configured default agent.
    ``model`` is "" when every tier defers to the backend's own choice.
    """
    agent_name = request.query.get("agent", "").strip()
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # Globs ~/.kiro/agents and may read the installed agent file — keep the
    # filesystem work off the event loop.
    model = await asyncio.to_thread(resolve_effective_model, cfg, agent_name or None)
    bindings = resolve_agent_bindings(cfg, agent_name or None)
    return web.json_response(
        {
            "model": model,
            "agent": agent_name,
            "kiro_agent": bindings.kiro_agent,
            # Whether the agent itself pins the model, vs inheriting it.
            "pinned": bool(bindings.model),
        }
    )


async def api_kirocrew_agents_create(request: web.Request) -> web.Response:
    """POST /api/agents — create a new KiroCrew agent."""

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "Agent name is required"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)
        cfg.agents[name] = KiroCrewAgentConfig(
            kiro_agent=body.get("kiro_agent", "kirocrew"),
            workspace=body.get("workspace", "default"),
            memory_store=body.get("memory_store", "default"),
            # Passed RAW, not str()-coerced: normalize_agent_model is total and
            # maps a non-string to "" (inherit). Wrapping in str() first would
            # turn {"model": 123} into the literal "123", which normalizes to a
            # string the backend then rejects as an unknown model id.
            model=normalize_agent_model(body.get("model")),
            description=body.get("description", ""),
            triggers=body.get("triggers", ""),
            source=body.get("source", "kirocrew"),
        )
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_kirocrew_agent_update(request: web.Request) -> web.Response:
    """PUT /api/agents/{name} — update a KiroCrew agent."""

    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        agent = cfg.agents[name]
        changed: list[str] = []
        if "kiro_agent" in body:
            agent.kiro_agent = body["kiro_agent"]
            changed.append("kiro_agent")
        if "workspace" in body:
            agent.workspace = body["workspace"]
            changed.append("workspace")
        if "memory_store" in body:
            agent.memory_store = body["memory_store"]
            changed.append("memory_store")
        if "model" in body:
            # "auto"/"" both mean inherit; store the single "" spelling so the
            # agent keeps deferring to the kiro pin / global fallback. Raw, not
            # str()-coerced — see the create path for why.
            agent.model = normalize_agent_model(body["model"])
            changed.append("model")
        if "description" in body:
            agent.description = body["description"]
            changed.append("description")
        if "triggers" in body:
            agent.triggers = body["triggers"]
            changed.append("triggers")
        if "source" in body:
            agent.source = body["source"]
            changed.append("source")
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.update",
        outcome="success",
        source="dashboard",
        resources=f"{name} ({','.join(changed)})",
    )
    return web.json_response({"ok": True, "name": name})


async def api_kirocrew_agent_delete(request: web.Request) -> web.Response:
    """DELETE /api/agents/{name} — delete a KiroCrew agent."""

    name = request.match_info["name"]
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if name == cfg.default_agent:
            return web.json_response(
                {"error": f"Cannot delete default agent '{name}'. Change default_agent first."},
                status=409,
            )
        del cfg.agents[name]
        cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})


# ── Conductor skill regeneration ────────────────────────────────────


def _regen_conductor() -> None:
    """Regenerate conductor skill after metadata or agent roster changes."""
    try:
        cfg = KiroCrewConfig.load()
        if not cfg.agent.conductor_skill:
            return
        from kiro_crew.conductor_skill import generate_conductor_skill  # noqa: F811
        from kiro_crew.skills import SkillsLoader  # noqa: F811

        generate_conductor_skill(SkillsLoader())
    except Exception:
        logger.exception("Failed to regenerate conductor skill")
