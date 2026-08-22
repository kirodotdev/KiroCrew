"""Agent configuration, themes, AIM integration, and agent CRUD handlers."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from aiohttp import web

from kiro_crew import agent_state, model_registry
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.agent import AGENT_FILENAME, get_shipped_tools, install_agent, kiro_agents_dir_path
from kiro_crew.agent_discovery import (
    _read_agent_spec,
    clear_list_agents_cache,
    list_agents,
    project_agent_names,
    spec_model,
    spec_str,
)
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
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
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor, maintenance_executor, subprocess_executor
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.platform_compat import restrict_to_owner
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
    wrap_argv,
)
from kiro_crew.security import ENV_VAR_REFERENCE_RE as _ENV_VAR_REFERENCE_RE
from kiro_crew.security import env_key_is_credential_like as _env_key_is_credential_like
from kiro_crew.security import is_sensitive_path
from kiro_crew.security import mcp_env_value_is_credential_like as _mcp_env_value_is_credential_like
from kiro_crew.security import path_contains_sensitive
from kiro_crew.validation import _AGENT_NAME_RE

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
            # Offloaded: the filter resolves the ceiling AND scans the profile
            # directory per ref, which is synchronous filesystem work — running it
            # inline would stall the aiohttp event loop for the duration.
            await asyncio.to_thread(sanitize_agent_config_governance, config)
            # Never persist Kiro Crew bookkeeping into the kiro spec —
            # kiro-cli rejects unknown fields and drops the agent (#2570).
            # Offloaded like the governance filter above: this reads/writes the
            # agent_model_state.json sidecar, the same class of synchronous
            # filesystem work that would otherwise stall the event loop.
            # Only trust a submitted name when it is a non-empty string — any
            # other JSON type (list, dict, number) would flow into the sidecar
            # helper as a dict key and crash the endpoint with a 500.
            raw_name = config.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name.strip() else installed_path.stem
            changed = await asyncio.to_thread(agent_state.lift_and_strip_bookkeeping, config, name)
            if changed:
                logger.info(
                    "Stripped Kiro Crew bookkeeping keys from a PUT to agent config for %r",
                    name,
                )
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
    # ``managed`` is derived from the SAME predicate the PUT handler enforces, so
    # the editor cannot offer an action the endpoint will refuse. Deriving it here
    # rather than restating the filename list in the frontend keeps one source of
    # truth: adding a managed spec to OWNED_KIRO_AGENT_FILES updates both at once.
    payload = []
    for a in agents:
        row = a.to_dict()
        row["managed"] = _template_is_writable(str(row.get("filename") or "")) is not None
        payload.append(row)
    return web.json_response(payload)


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


def _live_advertised_model_ids(request: web.Request) -> list[str]:
    """Model ids the most recently started live session advertises, or ``[]``.

    Newest session first. ``active_providers()`` walks a dict of live sessions, so
    forward order is creation order — and a session that started BEFORE a plan
    change still holds the advertised list it captured at its own ``session/new``.
    Reading the oldest one would narrow to pre-downgrade entitlements, i.e. keep
    offering exactly the models the narrowing exists to hide.

    ``[]`` means "entitlement unknown", which every caller must treat as allow:
    :func:`model_is_unusable` fails open on an empty set for the same reason.
    Shared by the picker and by template validation so the two cannot disagree
    about what a live session advertises.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in reversed(providers):
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
            return ids
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
    # An unresolvable advertised set (no live session, a backend that omits
    # `models`, or a surprising payload) means entitlement is unknown, and the
    # catalog is returned unfiltered rather than emptied.
    advertised = _live_advertised_model_ids(request)
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
                        # `spec_str` for the same reason as `declared` above: a
                        # hand-edited spec can carry a structured (non-string)
                        # "name", which would crash the sidecar helper's dict
                        # lookup with an unhashable key.
                        agent_name = spec_str(data, "name") or name
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
                        # Never persist Kiro Crew bookkeeping into the kiro spec —
                        # kiro-cli rejects unknown fields and drops the agent. Same
                        # shared helper as the PUT handler and migrate_agent_specs(),
                        # so this fourth writer can't drift from the other three
                        # (#2570). The model branch above may have just set the
                        # sidecar explicitly; the helper only lifts a stale key out
                        # of `data` when the sidecar is still unset, so it can't
                        # clobber that just-written value. Offloaded like the PUT
                        # handler: the helper does synchronous sidecar read/write
                        # filesystem work that would stall the event loop.
                        await asyncio.to_thread(
                            agent_state.lift_and_strip_bookkeeping, data, agent_name
                        )
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
    # The template pointer must be EXPLICIT. It used to default to "kirocrew",
    # which made every crew created without naming a template an alias for the
    # DEFAULT agent: dispatch flattens an alias to its `kiro_agent`
    # (config.loader.resolve_agent_bindings), so the crew was offered in the chat
    # picker and then the default answered — the "picker reverts to default"
    # report behind #1684. "kirocrew" is still a perfectly valid CHOICE here (a
    # crew booting the built-in agent against its own workspace/memory store is
    # the common case); only the silent default is refused.
    kiro_agent = str(body.get("kiro_agent") or "").strip()
    if not kiro_agent:
        return web.json_response(
            {
                "error": "kiro_agent is required — name the agent this crew boots "
                "from (pass 'kirocrew' for the built-in agent)",
                "code": "kiro_agent_required",
            },
            status=400,
        )
    # Grammar-checked before the name is persisted or used to look anything up.
    # This is the one shared agent-name grammar every other boundary uses, so a
    # value that cannot name an agent (path separators, traversal, wildcards,
    # over-length) is refused here rather than stored as a dangling pointer.
    if not _AGENT_NAME_RE.match(kiro_agent):
        return web.json_response(
            {"error": "invalid kiro_agent name", "code": "invalid_kiro_agent_name"},
            status=400,
        )
    # Existence is resolved through `list_agents()`, which reads every spec via the
    # hardened reader: it resolves symlinks, refuses a spec whose REAL target is
    # sensitive, and goes through the same gate as every other dashboard file read.
    # A direct filename probe here called `Path.read_text()` itself, so a namespaced
    # agent file symlinked at a credentials path would have been read outside that
    # gate. `list_agents()` is also the broader and more accurate notion of
    # existence: it includes edition-provided rows that are ACP-resolvable with no
    # on-disk file, which is what "will this actually dispatch" means.
    # Off the loop: it scans and parses the agent directories.
    known_agents = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: {a.name for a in list_agents()}
    )
    template_missing = kiro_agent not in known_agents
    # Unknown-but-accepted: an edition may resolve a row this listing cannot see,
    # so refusing here would break a legitimate crew. WARN instead — the same
    # posture, and for the same reason, as the sync path's EXECUTABLE INVARIANT
    # check — so a crew that will fail at spawn leaves a trace rather than
    # failing silently later.
    if template_missing:
        logger.warning(
            "creating crew %r against template %r, which is not in the installed "
            "agent listing — if it is not ACP-resolvable the crew will fail at spawn",
            name,
            kiro_agent,
        )
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)
        cfg.agents[name] = KiroCrewAgentConfig(
            kiro_agent=kiro_agent,
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


# ── Agent Template Authoring (CREATE / UPDATE) ──────────────────────

_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TEMPLATE_MAX_PROMPT_CHARS = 100_000
_TEMPLATE_MAX_DESCRIPTION_CHARS = 2000
_TEMPLATE_MAX_MCP_SERVERS = 50
_TEMPLATE_MAX_TOOLS = 500
_TEMPLATE_MAX_RESOURCES = 200

#: Spec keys the authoring form owns. A PUT replaces exactly these; anything else
#: in the existing spec is carried forward untouched (see the update handler).
_EDITOR_OWNED_SPEC_KEYS = frozenset(
    {
        "name",
        "description",
        "model",
        "prompt",
        "tools",
        "allowedTools",
        "mcpServers",
        "resources",
    }
)


def _reserved_template_names() -> set[str]:
    """Names a template may not take.

    The managed specs' own stems, plus ``default`` — the built-in agent that has
    no config file and is special-cased by :func:`api_agent_detail`.
    """
    return {Path(f).stem for f in OWNED_KIRO_AGENT_FILES} | {"default"}


def _template_is_writable(filename: str) -> str | None:
    """Return a refusal reason for a spec this endpoint must not overwrite.

    Two classes are refused:

    * ``<app>--<agent>.json`` — owned by an installed app.
    * ``OWNED_KIRO_AGENT_FILES`` — Kiro Crew rebuilds these from shipped defaults
      on every install and at boot self-heal. A full-replace write here drops
      ``hooks`` (including the bash audit hook), ``includeMcpJson`` and the
      managed MCP server block, breaking the running install until the next
      rebuild. A ``--``-only check misses every one of them, since none contains
      a double dash.
    """
    if "--" in filename:
        return "Cannot edit app-managed agent templates"
    if filename in OWNED_KIRO_AGENT_FILES:
        return (
            f"'{Path(filename).stem}' is managed by Kiro Crew and is rebuilt on "
            "every install; edits here would be silently reverted. Clone it to "
            "get an editable copy"
        )
    return None


def _name_already_claimed(agents_dir: Path, name: str) -> bool:
    """True when any existing spec already answers to *name*.

    kiro-cli resolves an agent by the ``name`` FIELD, not the filename, so a
    package spec at ``<pkg>-reviewer.json`` declaring ``"name": "reviewer"``
    makes a new ``reviewer.json`` a coin flip over which one wins. A filename
    check alone does not catch it.

    Reads go through the shared :func:`_read_agent_spec`, not ``read_text``: the
    agents directory is user-writable and shared with other tools, so a spec may
    be a symlink to a character device or an arbitrarily large file. That reader
    already caps the size, resolves the link and refuses a sensitive target, so
    scanning for a name collision cannot be turned into an unbounded read.
    """
    try:
        candidates = sorted(agents_dir.glob("*.json"))
    except OSError:
        return False
    for path in candidates:
        if path.stem == name:
            return True
        data = _read_agent_spec(path)
        if isinstance(data, dict) and spec_str(data, "name") == name:
            return True
    return False


def _carry_forward_unowned(spec: dict, existing: dict, body: dict) -> None:
    """Copy keys from *existing* that this request did not author into *spec*.

    Ownership is decided by what the REQUEST actually carried, not by a static
    key list. A static list is wrong in both directions and cost a data-loss bug:
    ``resources`` and ``mcpServers`` are nominally editor-owned, but the dialog
    omits ``resources`` entirely and can submit args-only servers, so treating
    them as owned regardless deleted hand-authored ``file://`` steering globs and
    stripped ``command`` / ``env`` off stdio MCP entries on an edit that only
    touched the description.

    The rule that holds: a key present in the body is replaced, a key absent from
    the body is preserved.
    """
    for key, value in existing.items():
        if key in _EDITOR_OWNED_SPEC_KEYS and key in body:
            continue  # the request authored this one; the built spec wins
        spec.setdefault(key, value)


async def _persist_spec(
    spec: dict,
    target: Path,
    *,
    exclusive: bool,
    expect_version: tuple[int, int, str] | None = None,
) -> str | None:
    """Write *spec* to *target* off the event loop. Returns an error code or None.

    ``sanitize_agent_config_governance`` runs here rather than at the call sites
    so no future writer can reach the file without it: this handler assigns
    ``allowedTools`` and ``mcpServers`` wholesale, which is exactly the shape that
    reopens the auto-approve bypass its docstring describes. Serialisation and the
    write are both filesystem/CPU work and belong off the loop with the scans.

    ``lift_and_strip_bookkeeping`` runs here for the same chokepoint reason. Its
    own docstring requires EVERY writer of a kiro agent spec to call it, and this
    is the fourth such writer (#2570 named the other three). ``model_managed`` and
    ``cc_model`` are Kiro Crew's, not kiro-cli's: carry-forward copies unowned keys
    verbatim, so one acquired after boot would survive an edit, and kiro-cli
    rejects the ENTIRE spec on an unknown field and silently falls the session back
    to the default agent. Already off the loop here, so the sync helper is called
    directly rather than through another executor hop.
    """

    def _work() -> str | None:
        raw_name = spec.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name.strip() else target.stem
        if agent_state.lift_and_strip_bookkeeping(spec, name):
            logger.info(
                "Stripped Kiro Crew bookkeeping keys from an agent template write for %r",
                name,
            )
        sanitize_agent_config_governance(spec)
        payload = json.dumps(spec, indent=2) + "\n"
        if exclusive:
            return _write_spec_exclusive(target, payload)
        return _replace_spec_atomically(target, payload, expect_version)

    return await asyncio.get_running_loop().run_in_executor(discovery_executor(), _work)


def _spec_version(target: Path) -> tuple[int, int, str] | None:
    """An identity for the bytes currently at *target*, or None if absent.

    ``(st_mtime_ns, st_size, sha256)``. The digest is load-bearing, not belt-and-
    braces: ``st_mtime_ns`` reports nanoseconds but many filesystems only STORE
    coarse timestamps (ext3 and some network mounts at one second, FAT at two), so a
    same-size save inside one tick compares equal on mtime and size alone and the
    conflict check waves the overwrite through. Hashing costs one extra read of a
    file this handler already reads, off the event loop, which is cheap next to
    silently losing someone's edit.
    """
    try:
        st = target.stat()
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, digest)


def _replace_spec_atomically(
    target: Path, content: str, expect_version: tuple[int, int, str] | None = None
) -> str | None:
    """Overwrite *target* via a temp file + rename. Returns an error code or None.

    When *expect_version* is given, the file is replaced only while it still matches
    the version the caller read. The config lock serializes Kiro Crew's own writers,
    but kiro-cli, an editor, or any other tool writes the same file outside it, so
    without this check a save landing after the read would be replaced wholesale and
    silently lost. Checked as late as possible, immediately before the rename.
    """
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".json.tmp")
    except OSError as exc:
        logger.warning("Failed to stage agent template %s: %s", target, exc)
        return "agent_template_write_failed"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # The replacement inherits the STAGED file's permissions, so restricting it
        # here is what keeps an edited spec owner-only. Without this a PUT silently
        # widens access on a spec that was restricted when created.
        restrict_to_owner(tmp_path)
        if expect_version is not None and _spec_version(target) != expect_version:
            # Someone else wrote between the read and now. Refusing loses this
            # request; replacing would lose theirs, and theirs is already on disk.
            os.unlink(tmp_path)
            return "agent_template_conflict"
        os.replace(tmp_path, str(target))
    except OSError as exc:
        logger.warning("Failed to write agent template %s: %s", target, exc)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return "agent_template_write_failed"
    return None


def _write_spec_exclusive(target: Path, content: str) -> str | None:
    """Create *target* only if absent, publishing complete content. Error code or None.

    Two properties are needed at once, and neither alone is sufficient:

    * **Exclusive.** ``exists()`` then ``os.replace`` is two steps and ``os.replace``
      overwrites, so a concurrent POST -- or any other tool writing that path --
      silently clobbers the loser. Only the kernel can make "create only if absent"
      atomic.
    * **Complete.** Opening the target with ``O_EXCL`` and then writing into it
      publishes the path before the bytes are there, so a crash mid-write leaves
      truncated JSON visible under a name kiro-cli will try to load, and an unparsable
      spec takes the whole agent down rather than failing this one request.

    ``os.link`` gives both: the content is staged in a temp file first, and the link
    is a single atomic operation that fails outright when the name already exists. The
    file is therefore never visible in a partial state.
    """
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".json.tmp")
    except OSError as exc:
        logger.warning("Failed to stage agent template %s: %s", target, exc)
        return "agent_template_write_failed"

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Owner-only BEFORE publication, and via platform_compat rather than a bare
        # chmod: a spec can name a credential-bearing MCP env, and on Windows a
        # mkstemp file inherits the parent directory's ACL, which chmod does not
        # touch. Restricting the staged file means the published spec is never
        # readable by another principal, not even briefly.
        restrict_to_owner(tmp_path)
        try:
            os.link(tmp_path, str(target))
        except FileExistsError:
            return "agent_template_exists"
        except OSError as exc:
            # A filesystem without hardlinks (some FAT/exFAT mounts, a few network
            # filesystems). REFUSE rather than fall back to writing into the target:
            # that fallback keeps exclusivity but publishes the name before the bytes
            # are there, so an interrupted write leaves truncated JSON that kiro-cli
            # rejects — trading a failed request for a broken template. A refusal is
            # recoverable; a corrupt spec on disk is not.
            if exc.errno not in (errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
                raise
            logger.warning(
                "Cannot publish agent template atomically on %s (%s); refusing rather "
                "than risking a partially written spec",
                target.parent,
                exc.strerror,
            )
            return "agent_template_write_failed"
    except OSError as exc:
        logger.warning("Failed to create agent template %s: %s", target, exc)
        return "agent_template_write_failed"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return None


def _validate_mcp_env(
    env: dict[str, str],
    server_name: str,
    field: str = "env",
    stored: dict | None = None,
    exemptions: list[tuple[tuple[str, ...], object]] | None = None,
) -> str | None:
    """Screen an mcpServers[] ``env`` or ``headers`` block for literal secrets.

    One screen for both blocks, parameterised only by the field name used in the
    error message. They are the same problem -- a key/value map on an MCP server
    whose values are frequently credentials -- and a remote server's bearer token
    lives in ``headers`` exactly as a stdio server's lives in ``env``. A second
    copy for headers would be a second place to forget a token.

    Returns an error message string on violation, or None if clean.
    """
    # What the stored spec holds at this exact location, if anything. A value the
    # request leaves byte-identical was not introduced by this request, so screening
    # it again only blocks editing an unrelated field on a template that already
    # carries a secret. Anything NEW or CHANGED is still screened.
    stored_block = stored.get(field) if isinstance(stored, dict) else None
    if not isinstance(stored_block, dict):
        stored_block = {}

    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return f"mcpServers.{server_name}.{field}: keys and values must be strings"
        # Allow env-var references ($VAR or ${VAR})
        if _ENV_VAR_REFERENCE_RE.match(value):
            continue
        if stored_block.get(key) == value:
            # Recorded so the caller can re-check it against the authoritative
            # in-lock read: this exemption asserts "already stored", and the read it
            # was judged against is older than the one being replaced.
            if exemptions is not None:
                exemptions.append((("mcpServers", server_name, field, key), value))
            continue
        # Check key name for credential-like patterns
        if _env_key_is_credential_like(key):
            return (
                f"mcpServers.{server_name}.{field}.{key}: looks like a credential. "
                f"Use an environment variable reference (${{VAR}}) instead of a literal value"
            )
        # Check value for known secret patterns. Uses the full validator rather
        # than the pattern alone: two credential shapes cannot be expressed as a
        # prefix pattern and are only reachable through it -- a base64-wrapped
        # credential, and a bare 40-char AWS secret access key, which carries no
        # prefix at all.
        if _mcp_env_value_is_credential_like(value):
            return (
                f"mcpServers.{server_name}.{field}.{key}: value matches a known secret pattern. "
                f"Use an environment variable reference (${{VAR}}) instead"
            )
    return None


_RESOURCE_GLOB_MAGIC = re.compile(r"[*?\[]")


_ABSENT = object()


def _mcp_entry_credential_error(
    srv_name: str,
    srv_spec: dict,
    stored: dict | None = None,
    exemptions: list[tuple[tuple[str, ...], object]] | None = None,
) -> str | None:
    """Screen EVERY string value in an MCP server entry for a literal credential.

    Field-by-field screening is what produced this function. `env` was screened,
    then `headers` had to be added, then `url` and `args`: four rounds of the same
    defect, because each new credential-carrying field was a separate thing to
    remember. This walks the entry's values instead, so a field nobody has thought
    of yet -- or one kiro-cli adds later -- is covered on arrival.

    ``command`` is excluded: it is a filesystem path, screened for sensitivity by
    its own check, and a path is not a credential.

    A URL embedding userinfo (``https://user:secret@host``) needs no separate rule
    here -- the shared credential patterns already recognise that shape, which a
    revert-verify proved by showing a dedicated regex was unreachable. Screening it
    twice would be a second spelling of the same question, free to drift from the
    canonical patterns.
    """

    def _stored_at(path: str) -> object:
        """The stored value at a dotted/indexed path, or a sentinel when absent."""
        node: object = stored
        for part in path.lstrip(".").replace("[", ".").replace("]", "").split("."):
            if not part:
                continue
            if isinstance(node, dict):
                if part not in node:
                    return _ABSENT
                node = node[part]
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return _ABSENT
            else:
                return _ABSENT
        return node

    def _walk(value: object, path: str) -> str | None:
        if isinstance(value, str):
            # Unchanged from the stored spec -> not introduced by this request.
            if stored is not None and _stored_at(path) == value:
                if exemptions is not None:
                    parts = tuple(
                        x
                        for x in path.lstrip(".").replace("[", ".").replace("]", "").split(".")
                        if x
                    )
                    exemptions.append((("mcpServers", srv_name) + parts, value))
                return None
            # An env-var reference is the SANCTIONED form and must stay allowed
            # wherever it appears -- including inside args, which is how a stdio
            # server is normally handed a token.
            if _ENV_VAR_REFERENCE_RE.match(value):
                return None
            if _mcp_env_value_is_credential_like(value):
                return (
                    f"mcpServers.{srv_name}{path}: value matches a known secret "
                    "pattern. Use an environment variable reference (${VAR}) instead"
                )
            return None
        if isinstance(value, list):
            for i, item in enumerate(value):
                err = _walk(item, f"{path}[{i}]")
                if err:
                    return err
            return None
        if isinstance(value, dict):
            for key, item in value.items():
                err = _walk(item, f"{path}.{key}")
                if err:
                    return err
        return None

    for field, value in srv_spec.items():
        if field == "command":
            continue
        err = _walk(value, f".{field}")
        if err:
            return err
    return None


def _resource_target_is_sensitive(uri: str) -> bool:
    """True when a resource URI addresses a credential file or the trust root.

    ``resources`` entries are handed to kiro-cli, which READS each match into the
    agent's context, so an entry like ``file://~/.ssh/id_rsa`` turns a template
    into a credential disclosure path. Only ``file://`` is screened; other schemes
    (``skill://``) address no filesystem location.

    A glob is screened by the last complete directory segment before the first
    wildcard, in the CONTAINS direction as well as the IS direction: a glob
    matches many files, so ``file://~/**`` never equals a sensitive path while
    sweeping every one of them. A precise glob like
    ``file://.kiro/steering/**/*.md`` stays allowed, which is the shape templates
    legitimately use.

    A glob must be ANCHORED to a concrete root. ``file://.*/*`` and ``file://**``
    name no directory at all, so what they match is decided entirely by the
    agent's working directory -- which this validator, running in the gateway,
    does not know. Under a home-directory workspace they sweep dotfile
    directories including ``.ssh``. Refusing them is the same rule already applied
    to parent traversal: when the base is unknowable here, do not resolve it,
    refuse it.
    """
    if not uri.startswith("file://"):
        return False
    raw = uri[len("file://") :]
    if not raw:
        return False

    magic = _RESOURCE_GLOB_MAGIC.search(raw)
    if magic is None:
        return _path_escapes_or_is_sensitive(raw)

    prefix = raw[: magic.start()]
    # The text immediately before the wildcard is a partial filename, not a
    # directory, so cut back to the last complete segment.
    root = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
    if not root:
        return True  # unanchored: no concrete root to screen
    if _path_escapes_or_is_sensitive(root):
        return True
    return _screen_both_bases(os.path.expanduser(root), path_contains_sensitive)


def _screen_both_bases(expanded: str, check: Callable[..., bool]) -> bool:
    """Apply *check* to a resource path under every base it could resolve against.

    A relative resource path is resolved by the AGENT against its workspace at
    prompt time, while this validator runs in the gateway process, so a bare
    ``.kube/config`` resolves against the gateway's cwd here and against the
    workspace there. Every screen in this module therefore applies its check
    TWICE for a relative path: once with HOME as the base -- the worst case the
    workspace can be, and the scenario these screens exist to defend -- and once
    with the process default.

    BOTH directions (is-sensitive and contains-sensitive) go through this single
    helper deliberately. Letting each call site pass its own base is what allowed
    the contains-direction to keep resolving against the gateway cwd after the
    is-direction was given a HOME base: the two checks sat adjacent, looked
    symmetric, and disagreed about the base. Centralising the choice means a new
    screening direction cannot reintroduce the gap by omission.
    """
    if not os.path.isabs(expanded) and check(expanded, base_dir=os.path.expanduser("~")):
        return True
    return check(expanded)


def _path_escapes_or_is_sensitive(raw: str) -> bool:
    """True when a resource path is sensitive OR climbs out of its own tree.

    Parent traversal is refused outright rather than resolved: a template
    resource has no legitimate reason to climb above the tree it is scoped to,
    and refusing is the only answer that does not depend on a working directory
    this process does not know.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        # PureWindowsPath, deliberately, on every platform: it treats BOTH "/" and
        # "\" as separators, so one parse covers a POSIX-style URI path and a
        # Windows-style one without hand-splitting on a hardcoded separator.
        if ".." in PureWindowsPath(expanded).parts:
            return True
    return _screen_both_bases(expanded, is_sensitive_path)


def _reserved_namespace_error(name: str) -> str | None:
    """Refuse a template name inside the app-owned agent namespace.

    `_safe_link_name` maps `app/agent` to `app--agent.json`, and
    `_deregister_agents` deletes by that PREFIX alone -- it checks neither
    ownership nor whether the entry is the app's own symlink -- so a user template
    named `calendar--assistant` is silently unlinked when the `calendar` app is
    disabled. `spawn_sdk` reads the same prefix as proof of which agents an app may
    run, so a name in that namespace is also an authority claim.

    Refused at the point the name is chosen rather than made safe downstream: three
    separate readers already treat the prefix as ownership, and a rule at the entry
    point cannot be missed by a fourth.
    """
    if "--" in name:
        return (
            "name may not contain '--': that separator is reserved for app-owned "
            "agents (app--agent) and a template using it would be deleted when "
            "that app is disabled"
        )
    return None


def _build_template_spec(
    body: dict,
    advertised: list[str] | None = None,
    stored: dict | None = None,
    exemptions: list[tuple[tuple[str, ...], object]] | None = None,
) -> tuple[dict, str | None]:
    """Validate and build a kiro-cli agent spec dict from a request body.

    Returns (spec, None) on success or ({}, error_message) on validation failure.
    """
    name = body.get("name", "")
    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.match(name):
        return {}, (
            "name must match ^[a-z0-9][a-z0-9._-]{0,63}$ "
            "(lowercase, start with alphanumeric, max 64 chars)"
        )
    # A template name must ALSO satisfy the repository's shared agent-name grammar,
    # because the name this endpoint writes to disk is the same string that is later
    # supplied as an `agent` value and validated against `_AGENT_NAME_RE`. The two
    # grammars are not identical -- this one is deliberately narrower on case (files
    # are lowercase) but was wider on separators, so `release.bot`, `my.agent` and
    # any trailing `-`/`_`/`.` were creatable here and then rejected everywhere the
    # shared grammar is enforced: a template that exists but cannot be selected.
    # Requiring both keeps this endpoint from minting names the rest of the system
    # refuses, rather than restating what a valid agent name is.
    if not _AGENT_NAME_RE.match(name):
        return {}, (
            "name must also match the agent-name grammar shared with the rest of "
            "Kiro Crew: no dots, and it must end in a letter or digit"
        )
    # `--` is deliberately NOT rejected here. It is a name-CHOICE rule enforced by
    # `_reserved_namespace_error` on the CREATE path only: update cannot rename (a
    # body name differing from the URL is refused), and for an app-owned file the
    # authoritative refusal is the managed 403, which carries its own audit event
    # and states the real reason. Enforcing it in this shared builder, which runs
    # before that check, made the name rule pre-empt the 403.

    spec: dict = {"name": name}

    # Scalar fields are type-checked on PRESENCE, before any truthiness test. A
    # falsy non-string (``false``, ``0``, ``[]``) would otherwise skip validation
    # AND skip assignment, so the key would be present in the request -- which
    # carry-forward reads as "the user authored this" -- while absent from the
    # built spec, erasing the stored value and returning 200.
    for scalar in ("description", "model", "prompt"):
        if scalar in body and not isinstance(body[scalar], str):
            return {}, f"{scalar} must be a string"

    # Description (optional)
    description = body.get("description", "")
    if description:
        if len(description) > _TEMPLATE_MAX_DESCRIPTION_CHARS:
            return {}, f"description exceeds {_TEMPLATE_MAX_DESCRIPTION_CHARS} characters"
        spec["description"] = description

    # Model (optional)
    model = body.get("model", "")
    if model:
        # A spec naming a model the account cannot run is not a harmless
        # aspiration: session startup withholds it and the agent silently runs the
        # backend default, so the user believes the template pinned a model when
        # it did not. Delegated to ``model_is_unusable`` rather than compared here,
        # so this cannot become a second spelling of "can this account run it" and
        # disagree with the picker or the wire. It fails OPEN on an unknown or
        # empty advertised set, so a host that advertises nothing is unaffected.
        if model_is_unusable(model, advertised):
            return {}, (
                f"model '{model}' is not available to this account; pick one the "
                "picker offers, or leave it on auto"
            )
        spec["model"] = model

    # Prompt (optional). kiro-cli validates these files with serde
    # ``deny_unknown_fields`` and rejects the ENTIRE spec on any unknown key, then
    # silently falls back to the default agent, so the key written is always the
    # spec's own ``prompt``.
    prompt = body.get("prompt", "")
    if prompt:
        if len(prompt) > _TEMPLATE_MAX_PROMPT_CHARS:
            return {}, f"prompt exceeds {_TEMPLATE_MAX_PROMPT_CHARS} characters"
        # ``prompt`` accepts a ``file://`` URI and kiro-cli READS it -- the repo's
        # own docs document `"prompt": "file:///path/to/prompt.md"`, and Kiro Crew
        # writes that form for its managed agents. So an unscreened prompt is the
        # same credential-disclosure path as an unscreened resource, and gets the
        # same screen.
        if _resource_target_is_sensitive(prompt):
            return {}, (
                "prompt points at a credential or protected location and cannot "
                "be attached to a template"
            )
        spec["prompt"] = prompt

    # tools (list of tool URIs)
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            return {}, "tools must be a list"
        if len(tools) > _TEMPLATE_MAX_TOOLS:
            return {}, f"tools exceeds {_TEMPLATE_MAX_TOOLS} entries"
        if not all(isinstance(t, str) for t in tools):
            return {}, "tools entries must be strings"
        spec["tools"] = tools

    # allowedTools (list of tool name patterns)
    allowed_tools = body.get("allowedTools")
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list):
            return {}, "allowedTools must be a list"
        if len(allowed_tools) > _TEMPLATE_MAX_TOOLS:
            return {}, f"allowedTools exceeds {_TEMPLATE_MAX_TOOLS} entries"
        if not all(isinstance(t, str) for t in allowed_tools):
            return {}, "allowedTools entries must be strings"
        # A glob must not be auto-approvable. The governance sanitizer decides
        # per entry via ``may_skip_gate_now``, which answers True for "*", "@*"
        # and "@builtin/*" while answering False for the concrete tools those
        # globs cover (measured: execute_bash and fs_write are both withheld).
        # So a wildcard is a strictly BROADER grant that passes the gate the
        # specific names fail, and it would auto-approve shell and filesystem
        # tools with the sensitive-path and denied-command checks never running.
        # Auto-approval is granted by exact tool name only.
        for entry in allowed_tools:
            if "*" in entry or "?" in entry:
                return {}, (
                    f"allowedTools entry '{entry}' is a wildcard. Auto-approval must "
                    "name each tool exactly, so the governance ceiling can screen it"
                )
        spec["allowedTools"] = allowed_tools

    # mcpServers
    mcp_servers = body.get("mcpServers")
    if mcp_servers is not None:
        if not isinstance(mcp_servers, dict):
            return {}, "mcpServers must be an object"
        if len(mcp_servers) > _TEMPLATE_MAX_MCP_SERVERS:
            return {}, f"mcpServers exceeds {_TEMPLATE_MAX_MCP_SERVERS} entries"
        for srv_name, srv_spec in mcp_servers.items():
            if not isinstance(srv_name, str):
                return {}, "mcpServers keys must be strings"
            if not isinstance(srv_spec, dict):
                return {}, f"mcpServers.{srv_name} must be an object"
            # The same server's stored entry, when this is an edit of a spec that
            # already had one. None on create, and None for a server being added,
            # so a new entry is screened in full.
            stored_servers = stored.get("mcpServers") if isinstance(stored, dict) else None
            stored_entry = (
                stored_servers.get(srv_name) if isinstance(stored_servers, dict) else None
            )
            if not isinstance(stored_entry, dict):
                stored_entry = None
            # Validate the whole entry shape, not just the fields being screened.
            # kiro-cli rejects the ENTIRE spec on a malformed server and falls the
            # session back to the default agent, so persisting a non-string
            # ``command`` or a non-list ``args`` breaks the template silently.
            cmd_raw = srv_spec.get("command")
            if cmd_raw is not None and not isinstance(cmd_raw, str):
                return {}, f"mcpServers.{srv_name}.command must be a string"
            url_raw = srv_spec.get("url")
            if url_raw is not None and not isinstance(url_raw, str):
                return {}, f"mcpServers.{srv_name}.url must be a string"
            args_raw = srv_spec.get("args")
            if args_raw is not None:
                if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
                    return {}, f"mcpServers.{srv_name}.args must be a list of strings"
            # A server needs EXACTLY ONE launchable transport. Neither means it cannot
            # start; both means a shape the MCP schema rejects outright, since stdio
            # and http are alternatives rather than a pair. Either way kiro-cli
            # refuses the whole spec on an unusable server and silently falls the
            # session back to the default agent. "At least one" was the earlier rule
            # and let the both-set case through.
            if bool(cmd_raw) == bool(url_raw):
                return {}, (
                    f"mcpServers.{srv_name} needs exactly one transport: a command "
                    "(stdio) or a url (http), not both"
                )
            # Validate command path is not sensitive
            cmd = srv_spec.get("command", "")
            if cmd and isinstance(cmd, str) and is_sensitive_path(cmd):
                return {}, f"mcpServers.{srv_name}.command: path is sensitive"
            # Screen env whenever the key is PRESENT, not only when truthy: a
            # falsy non-dict (``[]``, ``0``) would otherwise skip the type check
            # and be persisted as an invalid block.
            if "env" in srv_spec:
                env = srv_spec.get("env")
                if not isinstance(env, dict):
                    return {}, f"mcpServers.{srv_name}.env must be an object"
                err = _validate_mcp_env(env, srv_name, stored=stored_entry)
                if err:
                    return {}, err
            # `headers` is the remote (http) transport's equivalent of `env`: it
            # is where a bearer token or api key is passed, so it gets the SAME
            # screen rather than a parallel one. Screened on presence for the same
            # reason env is -- a falsy non-dict would otherwise skip the type
            # check and be persisted as a block kiro-cli rejects.
            if "headers" in srv_spec:
                headers = srv_spec.get("headers")
                if not isinstance(headers, dict):
                    return {}, f"mcpServers.{srv_name}.headers must be an object"
                err = _validate_mcp_env(
                    headers,
                    srv_name,
                    field="headers",
                    stored=stored_entry,
                    exemptions=exemptions,
                )
                if err:
                    return {}, err
            # Whole-entry sweep, AFTER the per-field key screens. The key screens
            # catch a credential named by its key (`env.GITHUB_TOKEN`); this
            # catches one recognisable by its value in ANY field, including `url`
            # and `args`, which had no screen at all.
            err = _mcp_entry_credential_error(srv_name, srv_spec, stored_entry, exemptions)
            if err:
                return {}, err
        spec["mcpServers"] = mcp_servers

    # resources (list of resource URIs)
    resources = body.get("resources")
    if resources is not None:
        if not isinstance(resources, list):
            return {}, "resources must be a list"
        if len(resources) > _TEMPLATE_MAX_RESOURCES:
            return {}, f"resources exceeds {_TEMPLATE_MAX_RESOURCES} entries"
        if not all(isinstance(r, str) for r in resources):
            return {}, "resources entries must be strings"
        for entry in resources:
            if _resource_target_is_sensitive(entry):
                return {}, (
                    f"resources entry '{entry}' points at a credential or "
                    "protected location and cannot be attached to a template"
                )
        spec["resources"] = resources

    # deniedCommands is deliberately NOT accepted.
    #
    # Two independent reasons, either of which is disqualifying:
    #
    # 1. At the top level it is an unknown key, so kiro-cli's
    #    ``deny_unknown_fields`` rejects the whole spec and falls back to the
    #    default agent — the template would silently not exist.
    # 2. Relocating it to ``toolsSettings.execute_bash.deniedCommands`` (the only
    #    place kiro-cli reads it) would resurrect a RETIRED mechanism. Command
    #    denial moved to Kiro Crew's own hooks.py PreToolUse gate, and
    #    ``agent.py:_strip_legacy_denied_commands`` actively removes that key on
    #    every refresh precisely so a stale spec rule cannot outrank the gate.
    #    Offering it in an editor would let a user author a rule that either
    #    disappears or shadows Settings > Security.
    #
    # A body carrying it is rejected loudly rather than ignored: silently
    # dropping a field a user believes restricts the agent is the worse failure.
    if body.get("deniedCommands") is not None:
        return {}, (
            "deniedCommands is not settable on an agent template. Command denial "
            "is enforced by Kiro Crew's tool gate; configure it in Settings > Security"
        )

    return spec, None


async def api_agents_installed_create(request: web.Request) -> web.Response:
    """POST /api/agents/installed — create a new agent template (kiro-cli spec).

    Writes ``~/.kiro/agents/<name>.json`` atomically. Rejects names that
    collide with existing files.
    """
    # These handlers install agent tools and MCP server COMMANDS into
    # ~/.kiro/agents, which every session on this machine resolves against, so the
    # write surface is global even though the request is per-session. Identity comes
    # from the token-auth middleware, never a client-set header. Refused before the
    # body is parsed so a non-owner cannot even exercise the validators.
    if not is_owner_dashboard_request(request):
        _sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation="agent_template.create",
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
        return web.json_response(
            {"error": "owner authorization required", "code": "owner_required"},
            status=403,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # Valid JSON is not necessarily an object. A top-level array reaches
    # ``body.get(...)`` as a list and raises AttributeError -> HTTP 500, so the
    # shape is rejected once here rather than per field.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )

    # Off the loop: the builder screens resource, prompt and MCP command paths,
    # and those screens resolve realpaths. On a network-backed home that is slow
    # enough filesystem work to stall the loop and the heartbeat with it.
    spec, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _build_template_spec, body, _live_advertised_model_ids(request)
    )
    if err:
        # A validation refusal here is frequently a SECURITY decision -- a
        # sensitive resource path, a literal credential in an MCP env block, a
        # wildcard auto-approve grant. Those are exactly the denials the audit log
        # exists to record, and they were previously invisible to it.
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent_template.validate",
            outcome="denied",
            source="dashboard",
            resources=str(body.get("name") or "")[:120],
            error=err[:200],
        )
        return web.json_response({"error": err, "code": "validation_failed"}, status=400)

    name = spec["name"]

    ns_err = _reserved_namespace_error(name)
    if ns_err:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent_template.validate",
            outcome="denied",
            source="dashboard",
            resources=name[:120],
            error=ns_err[:200],
        )
        return web.json_response({"error": ns_err, "code": "agent_name_reserved"}, status=400)

    if name in _reserved_template_names():
        return web.json_response(
            {"error": f"'{name}' is reserved", "code": "agent_name_reserved"}, status=400
        )

    skill_keys = body.get("skills")
    if skill_keys is not None:
        if not isinstance(skill_keys, list) or not all(isinstance(k, str) for k in skill_keys):
            return web.json_response(
                {"error": "skills must be a list of strings", "code": "invalid_skills"},
                status=400,
            )
        if len(skill_keys) > MAX_AGENT_SKILLS:
            return web.json_response(
                {
                    "error": f"skills exceeds {MAX_AGENT_SKILLS} entries",
                    "code": "too_many_skills",
                },
                status=400,
            )

    agents_dir = kiro_agents_dir_path()
    target = agents_dir / f"{name}.json"
    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()

    def _prepare() -> tuple[list[str], str]:
        """Collision scan + skill mapping. Both enumerate and READ the agents and
        skill directories, which on a large or network-backed catalog is enough
        filesystem work to stall the gateway loop -- the same reason
        /api/agents/installed and /api/skills already run off it."""
        agents_dir.mkdir(parents=True, exist_ok=True)
        if _name_already_claimed(agents_dir, name):
            return [], "agent_template_exists"
        if skill_keys is not None:
            _applied, unknown = apply_skill_mapping(spec, target, state, list(skill_keys))
            if unknown:
                return list(unknown), "unknown_skills"
        return [], ""

    unknown_keys, prepare_err = await loop.run_in_executor(discovery_executor(), _prepare)
    if prepare_err == "agent_template_exists":
        return web.json_response(
            {
                "error": f"Agent template '{name}' already exists",
                "code": "agent_template_exists",
            },
            status=409,
        )
    if prepare_err == "unknown_skills":
        return web.json_response(
            {
                "error": f"unknown skill keys: {', '.join(unknown_keys[:20])}",
                "code": "unknown_skills",
            },
            status=400,
        )

    write_err = await _persist_spec(spec, target, exclusive=True)
    if write_err == "agent_template_exists":
        # Lost the race against a concurrent create between the scan and the write.
        return web.json_response(
            {
                "error": f"Agent template '{name}' already exists",
                "code": "agent_template_exists",
            },
            status=409,
        )
    if write_err:
        return web.json_response(
            {"error": "could not write agent template", "code": write_err}, status=500
        )

    clear_list_agents_cache()
    state.push_refresh("agents")

    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent_template.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name}, status=201)


async def api_agents_installed_update(request: web.Request) -> web.Response:
    """PUT /api/agents/installed/{name} — update an existing agent template.

    Overwrites ``~/.kiro/agents/<name>.json`` atomically. Only user-owned
    templates (not app-namespaced ``<app>--<agent>.json`` files) are writable.
    """
    # These handlers install agent tools and MCP server COMMANDS into
    # ~/.kiro/agents, which every session on this machine resolves against, so the
    # write surface is global even though the request is per-session. Identity comes
    # from the token-auth middleware, never a client-set header. Refused before the
    # body is parsed so a non-owner cannot even exercise the validators.
    if not is_owner_dashboard_request(request):
        _sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation="agent_template.update",
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
        return web.json_response(
            {"error": "owner authorization required", "code": "owner_required"},
            status=403,
        )

    url_name = request.match_info["name"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )

    # Body name must match URL name
    body_name = body.get("name", "")
    if body_name and body_name != url_name:
        return web.json_response(
            {
                "error": "body.name must match the URL name",
                "code": "name_mismatch",
            },
            status=400,
        )
    # Inject URL name if body omits it
    body["name"] = url_name

    # Resolved once per request: the screening read below and the authoritative
    # read inside the lock must agree on which file they are talking about.
    agents_dir = kiro_agents_dir_path()
    target = agents_dir / f"{url_name}.json"

    # Off the loop: the builder screens resource, prompt and MCP command paths,
    # and those screens resolve realpaths. On a network-backed home that is slow
    # enough filesystem work to stall the loop and the heartbeat with it.
    #
    # The stored spec is read here only to SCOPE the credential screen -- a value
    # the request leaves byte-identical is not one this request introduces, and
    # re-judging it made a template that already carries a literal token
    # uneditable, description-only edits included. The AUTHORITATIVE read for
    # carry-forward still happens inside the config lock in `_prepare`; this read
    # is deliberately not load-bearing, so a concurrent write between the two
    # cannot lose an update. A stale view can only exempt a value that was on disk
    # when it was read, which is the value carry-forward would preserve anyway.
    # Resolved HERE, on the loop. `_live_advertised_model_ids` walks the live
    # session map, which the loop owns and mutates; iterating it from a worker
    # thread races a concurrent session removal into a RuntimeError and a 500.
    advertised_ids = _live_advertised_model_ids(request)

    # The version of the file as read INSIDE the config lock. The write requires it
    # to still match, so a save landing during the write window is refused rather
    # than overwritten. Stamped in the lock, not at the earlier screening read:
    # stamping the screening read would make two lock-serialized dashboard PUTs
    # conflict with each other, when the lock plus the in-lock re-read is exactly
    # what lets both of them survive. The earlier window is covered instead by
    # re-validating the credential exemptions below, which is the only thing the
    # older read is relied upon for.
    read_version: list[tuple[int, int, str] | None] = [None]

    # Credential values the pre-lock screen exempted as "already stored".
    # `_prepare` re-checks each against the authoritative read before writing.
    exemptions: list[tuple[tuple[str, ...], object]] = []

    def _read_then_build() -> tuple[dict, str | None]:
        stored = None if target.is_symlink() else _read_agent_spec(target)
        return _build_template_spec(
            body,
            advertised_ids,
            stored if isinstance(stored, dict) else None,
            exemptions,
        )

    spec, err = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_then_build
    )
    if err:
        # A validation refusal here is frequently a SECURITY decision -- a
        # sensitive resource path, a literal credential in an MCP env block, a
        # wildcard auto-approve grant. Those are exactly the denials the audit log
        # exists to record, and they were previously invisible to it.
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent_template.validate",
            outcome="denied",
            source="dashboard",
            resources=str(body.get("name") or "")[:120],
            error=err[:200],
        )
        return web.json_response({"error": err, "code": "validation_failed"}, status=400)

    skill_keys = body.get("skills")
    if skill_keys is not None:
        if not isinstance(skill_keys, list) or not all(isinstance(k, str) for k in skill_keys):
            return web.json_response(
                {"error": "skills must be a list of strings", "code": "invalid_skills"},
                status=400,
            )
        if len(skill_keys) > MAX_AGENT_SKILLS:
            return web.json_response(
                {
                    "error": f"skills exceeds {MAX_AGENT_SKILLS} entries",
                    "code": "too_many_skills",
                },
                status=400,
            )

    state: DashboardState = request.app["state"]
    loop = asyncio.get_running_loop()

    def _prepare() -> tuple[list[str], str]:
        """Read the existing spec, carry unowned keys forward, map skills. Reads
        and enumerates the agents and skill directories, so it stays off the loop."""
        # A spec that cannot be read or is not an object must NOT be treated as
        # empty: carry-forward would then preserve nothing and the write would
        # replace a file whose contents were never understood -- the same silent
        # erase this handler exists to prevent, just triggered by a parse failure
        # instead of a partial request. A symlink is refused for the same reason
        # plus one more: following it would copy whatever it points at into a
        # world-readable agent spec.
        # Read through the shared hardened reader, not read_text: it caps the size,
        # resolves the link and refuses a sensitive target, so an oversized or
        # symlinked spec cannot become an unbounded allocation in the gateway. A
        # None result covers every unusable case at once (missing, oversized,
        # symlink to a sensitive target, non-UTF-8, or JSON that is not an object)
        # and must be refused rather than treated as empty -- carry-forward would
        # then preserve nothing and the write would replace a file whose contents
        # were never understood.
        if target.is_symlink():
            return [], "spec_unreadable"
        # Stamped here, inside the lock, immediately before the authoritative read,
        # so a write landing between this and the rename is caught by the write's own
        # comparison.
        read_version[0] = _spec_version(target)
        existing = _read_agent_spec(target)
        if not isinstance(existing, dict):
            return [], "spec_unreadable"

        # The editor forces the spec's ``name`` to the URL stem, so a stored
        # spec whose declared name differs would be silently renamed by an
        # otherwise unrelated edit. kiro-cli resolves an agent by the name
        # FIELD, so that rename unregisters the agent under the name every
        # caller uses -- a package spec at <pkg>-reviewer.json declaring
        # "reviewer" stops answering to "reviewer". The dialog cannot express
        # the divergence, so refusing is the only non-destructive answer.
        # Re-validate every credential value the pre-lock screen EXEMPTED against
        # the authoritative read. The exemption means "identical to what is stored",
        # and the screening read is older than this one, so a value that changed in
        # between would otherwise be written unscreened. Only exempted values are
        # re-checked: an edit that exempted nothing (a description change) cannot
        # conflict, which is what keeps two serialized dashboard PUTs both working.
        for path_parts, exempted_value in exemptions:
            node: object = existing
            for part in path_parts:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    node = _ABSENT
                    break
            if node != exempted_value:
                return [], "agent_template_conflict"

        declared = existing.get("name")
        if isinstance(declared, str) and declared and declared != url_name:
            return [declared], "declared_name_mismatch"
        _carry_forward_unowned(spec, existing, body)
        if skill_keys is not None:
            _applied, unknown = apply_skill_mapping(spec, target, state, list(skill_keys))
            if unknown:
                return list(unknown), "unknown_skills"
        return [], ""

    async def _read_modify_write() -> tuple[list[str], str]:
        """Existence check, read, carry-forward and write as ONE serialized step.

        Held under the config lock because this is a read-modify-write: two
        disjoint PUTs that each read the same version would both carry forward
        that version's fields, and whichever wrote second would restore the
        other's stale values -- silently losing an update that reported success.
        The lock is an ``asyncio.Lock``, so awaiting the executor hops inside it
        suspends this handler without blocking the event loop; only another
        config writer waits.
        """

        # Both the existence stat and the writability check are filesystem work
        # and belong off the loop with the read: on a network-backed or unavailable
        # home a synchronous `is_file()` stat blocks every other gateway task,
        # including the heartbeat. Folded into ONE executor hop rather than two so
        # the round-trip cost does not double.
        def _precheck() -> tuple[str | None, str]:
            if not target.is_file():
                return None, "not_found"
            return _template_is_writable(target.name), ""

        managed, pre_err = await loop.run_in_executor(discovery_executor(), _precheck)
        if pre_err:
            return [], pre_err
        if managed:
            return [managed], "managed"
        keys, err = await loop.run_in_executor(discovery_executor(), _prepare)
        if err:
            return keys, err
        return [], await _persist_spec(
            spec, target, exclusive=False, expect_version=read_version[0]
        ) or ""

    async with _get_config_lock():
        unknown_keys, prepare_err = await _read_modify_write()

    if prepare_err == "not_found":
        return web.json_response(
            {"error": f"Agent template '{url_name}' not found", "code": "not_found"},
            status=404,
        )
    if prepare_err == "managed":
        # A managed-template refusal is a permission denial, so it belongs in the
        # audit log for the same reason the validation refusals do. Fixing the
        # validate branch last round and leaving this one is the same
        # audit-every-path gap, one branch over.
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent_template.update",
            outcome="denied",
            source="dashboard",
            resources=url_name,
            error="managed template is not editable",
        )
        return web.json_response(
            {"error": unknown_keys[0], "code": "agent_template_managed"}, status=403
        )
    if prepare_err == "spec_unreadable":
        return web.json_response(
            {
                "error": (
                    f"'{url_name}.json' could not be read as an agent spec (unreadable, "
                    "not valid JSON, not an object, or a symlink). Refusing to overwrite it"
                ),
                "code": "agent_template_unreadable",
            },
            status=409,
        )
    if prepare_err == "declared_name_mismatch":
        declared = unknown_keys[0] if unknown_keys else ""
        return web.json_response(
            {
                "error": (
                    f"'{url_name}.json' declares the agent name '{declared}'. Editing it "
                    f"here would rename it to '{url_name}' and unregister '{declared}'. "
                    "Clone it to get an editable copy"
                ),
                "code": "agent_template_name_mismatch",
            },
            status=409,
        )
    if prepare_err == "unknown_skills":
        return web.json_response(
            {
                "error": f"unknown skill keys: {', '.join(unknown_keys[:20])}",
                "code": "unknown_skills",
            },
            status=400,
        )

    # A conflict is not a server fault: the file changed under us, and the caller
    # can re-read and retry. 409 rather than 500 so a client can tell the two apart.
    if prepare_err == "agent_template_conflict":
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent_template.update",
            outcome="denied",
            source="dashboard",
            resources=url_name,
            error="spec changed on disk between read and write",
        )
        return web.json_response(
            {
                "error": (
                    "this template changed on disk while you were editing; "
                    "reopen it to see the current version"
                ),
                "code": "agent_template_conflict",
            },
            status=409,
        )

    # Any remaining code is the write error surfaced by _read_modify_write, which
    # performed the write itself under the lock.
    if prepare_err:
        return web.json_response(
            {"error": "could not write agent template", "code": prepare_err}, status=500
        )

    clear_list_agents_cache()
    state.push_refresh("agents")

    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent_template.update",
        outcome="success",
        source="dashboard",
        resources=url_name,
    )
    return web.json_response({"ok": True, "name": url_name})
