"""Registration bridges — wire app resources into KiroCrew's runtime.

When an app is installed or enabled, its agents, skills, and cron jobs need
to be registered with KiroCrew's existing systems.  This module provides
``register_app`` and ``deregister_app`` which handle the namespacing and
symlink/copy operations.

Namespace convention: ``{app_name}/{resource_name}`` to avoid collisions
between apps.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse, urlunparse

from kiro_crew import platform_compat
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.manager import app_dir, get_app, get_app_manifest
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Where kiro-cli looks for agent definitions
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"

# Where KiroCrew loads skills from
SKILLS_DIR_NAME = "skills"


def _skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def _namespace(app_name: str, resource_name: str) -> str:
    """Build a namespaced resource name: ``app_name/resource_name``."""
    return f"{app_name}/{resource_name}"


def _safe_link_name(namespaced: str) -> str:
    """Convert ``app/resource`` to a safe filename for symlinks: ``app--resource``."""
    return namespaced.replace("/", "--")


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

def _register_agents(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Symlink app agent JSONs into ~/.kiro/agents/ with namespaced names.

    Returns list of registered agent names (namespaced).
    """
    registered: list[str] = []
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    for agent_path_str in manifest.agents:
        agent_path = app_root / agent_path_str
        # Path containment check — reject paths that escape the app root
        if not agent_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: agent path escapes app root: %s", app_name, agent_path)
            continue
        if not agent_path.is_file():
            logger.warning("App %s: agent file not found: %s", app_name, agent_path)
            continue

        # Read agent JSON to get the agent name
        try:
            agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
            agent_name = agent_data.get("name", agent_path.stem)
        except (json.JSONDecodeError, OSError):
            agent_name = agent_path.stem

        # Namespaced link name: app-name--agent-name.json
        link_name = _safe_link_name(_namespace(app_name, agent_name)) + ".json"
        link_path = KIRO_AGENTS_DIR / link_name

        # Remove existing link if present
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()

        try:
            os.symlink(str(agent_path), str(link_path))
            registered.append(_namespace(app_name, agent_name))
            logger.info("Registered agent: %s -> %s", link_name, agent_path)
        except OSError as exc:
            logger.warning("Failed to symlink agent %s: %s", link_name, exc)

    return registered


def _deregister_agents(app_name: str) -> int:
    """Remove all agent symlinks for an app from ~/.kiro/agents/."""
    prefix = _safe_link_name(app_name + "/")
    removed = 0
    if not KIRO_AGENTS_DIR.is_dir():
        return 0
    for entry in KIRO_AGENTS_DIR.iterdir():
        if entry.name.startswith(prefix) and entry.name.endswith(".json"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Deregistered %d agent(s) for app %s", removed, app_name)
    return removed


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

_RESERVED_SKILL_DIRS = {"auto"}


def _register_skills(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Symlink app skill directories into ~/.kirocrew/skills/.

    Creates both a namespaced link (``skills/{app_name}/{skill_name}``) and a
    flat link (``skills/{skill_name}``) so the skill scanner finds the skill
    regardless of whether it walks subdirectories or only checks the top level.

    Returns list of registered skill names (namespaced).
    """
    registered: list[str] = []
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    app_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_path_str in manifest.skills:
        skill_path = app_root / skill_path_str
        if not skill_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: skill path escapes app root: %s", app_name, skill_path)
            continue
        if not skill_path.is_dir():
            logger.warning("App %s: skill directory not found: %s", app_name, skill_path)
            continue

        skill_name = skill_path.name

        # Namespaced link: ~/.kirocrew/skills/{app_name}/{skill_name}
        link_path = app_skills_dir / skill_name
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink():
                link_path.unlink()
            else:
                shutil.rmtree(link_path)

        # Flat link: ~/.kirocrew/skills/{skill_name} (for skill scanner)
        if skill_name in _RESERVED_SKILL_DIRS:
            logger.info("App %s: skipping flat link for reserved name %s", app_name, skill_name)
            flat_link = None
        else:
            flat_link = skills_root / skill_name
            if flat_link.exists() or flat_link.is_symlink():
                if flat_link.is_symlink():
                    flat_link.unlink()
                else:
                    logger.info(
                        "App %s: skipping flat link for %s — non-symlink dir exists",
                        app_name, skill_name,
                    )
                    flat_link = None  # type: ignore[assignment]

        try:
            os.symlink(str(skill_path), str(link_path))
            if flat_link is not None:
                os.symlink(str(skill_path), str(flat_link))
            namespaced = _namespace(app_name, skill_name)
            registered.append(namespaced)
            logger.info("Registered skill: %s -> %s", namespaced, skill_path)
        except OSError as exc:
            logger.warning("Failed to symlink skill %s: %s", skill_name, exc)

    if registered:
        sel().log_tool_invocation(
            session_key="", agent="kirocrew", source="app_bridge",
            tool_name="register_skills", tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={registered}",
        )
    else:
        sel().log_tool_invocation(
            session_key="", agent="kirocrew", source="app_bridge",
            tool_name="register_skills", tool_kind="permission_change",
            outcome="no_op",
            resources=f"app={app_name} skills=[]",
        )
    return registered


def _deregister_skills(app_name: str) -> int:
    """Remove the app's skill symlinks from ~/.kirocrew/skills/."""
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if not app_skills_dir.exists():
        return 0
    try:
        removed_skills = [item.name for item in app_skills_dir.iterdir() if item.is_symlink()]
        for item in app_skills_dir.iterdir():
            if item.is_symlink():
                if item.name in _RESERVED_SKILL_DIRS:
                    continue
                target = item.resolve()
                flat_link = skills_root / item.name
                if flat_link.is_symlink() and flat_link.resolve() == target:
                    flat_link.unlink()
        shutil.rmtree(app_skills_dir)
        logger.info("Deregistered skills for app %s", app_name)
        sel().log_tool_invocation(
            session_key="", agent="kirocrew", source="app_bridge",
            tool_name="deregister_skills", tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={removed_skills}",
        )
        return 1
    except OSError:
        sel().log_tool_invocation(
            session_key="", agent="kirocrew", source="app_bridge",
            tool_name="deregister_skills", tool_kind="permission_change",
            outcome="failed",
            resources=f"app={app_name}",
        )
        return 0


# ---------------------------------------------------------------------------
# Skill reconcile (startup — ensures manifest-declared skills are linked)
# ---------------------------------------------------------------------------


def reconcile_app_skills(app_name: str) -> list[str]:
    """Reconcile skill symlinks for an enabled app at gateway startup.

    Ensures manifest-declared skills are registered (idempotent: existing
    correct symlinks are overwritten by _register_skills, missing ones are
    created).  Also removes stale symlinks for skills that were removed from
    the manifest since the last registration.

    Called from start_enabled_app_backends() so that an app upgraded in-place
    (new manifest declaring new skills) gets its symlinks without needing a
    disable/enable cycle.

    Returns list of currently-registered namespaced skill names.
    """
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        # Self-managed apps own their registration lifecycle -- never touch
        # their symlinks here, even when the manifest declares no skills
        # (dynamically managed skills are not manifest-declared).
        return []

    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.skills:
        # No skills declared — remove any stale symlinks left from a prior version
        _deregister_skills(app_name)
        return []

    app_root = app_dir(app_name)

    # _register_skills is already idempotent (overwrites existing symlinks)
    registered = _register_skills(app_name, manifest, app_root)

    # Clean stale links: skills present as symlinks but no longer in manifest
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if app_skills_dir.is_dir():
        manifest_skill_names = {Path(s).name for s in manifest.skills}
        for entry in list(app_skills_dir.iterdir()):
            if entry.is_symlink() and entry.name not in manifest_skill_names:
                # Stale link — skill was removed from manifest
                target = entry.resolve()
                entry.unlink()
                # Also remove the flat link if it points to the same target
                flat_link = skills_root / entry.name
                if flat_link.is_symlink():
                    try:
                        if flat_link.resolve() == target:
                            flat_link.unlink()
                    except OSError:
                        pass
                logger.info(
                    "Reconcile: removed stale skill link %s/%s for app %s",
                    app_name, entry.name, app_name,
                )

    return registered


# ---------------------------------------------------------------------------
# Cron registration (deferred — writes a manifest for the CronService)
# ---------------------------------------------------------------------------

_CRON_MANIFEST_NAME = "app-crons.json"


def _app_crons_path(app_name: str) -> Path:
    """Path to the app's cron manifest within its install directory."""
    return app_dir(app_name) / _CRON_MANIFEST_NAME


def _register_crons(app_name: str, manifest: AppManifest) -> list[str]:
    """Write app cron definitions to a manifest file for later CronService pickup.

    The actual CronService registration happens at enable time via
    ``register_app_crons_with_service()``.  This just persists the
    definitions so they survive restarts.

    Returns list of namespaced cron names.
    """
    if not manifest.crons:
        return []

    cron_defs: list[dict[str, Any]] = []
    registered: list[str] = []
    for cron in manifest.crons:
        namespaced = _namespace(app_name, cron.name)
        cron_defs.append({
            "name": namespaced,
            "every": cron.every,
            "cron_expr": cron.cron_expr,
            "agent": cron.agent,
            "message": cron.message,
            "command": cron.command,
            "script": cron.script,
            "app": app_name,
            "agent_sequence": cron.agent_sequence,
            "env": cron.env,
            "persistent_session": cron.persistent_session,
            "silent": cron.silent,
        })
        registered.append(namespaced)

    path = _app_crons_path(app_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cron_defs, indent=2), encoding="utf-8")
    logger.info("Wrote %d cron definition(s) for app %s", len(cron_defs), app_name)
    return registered


def _deregister_crons(app_name: str) -> int:
    """Remove the app's cron manifest."""
    path = _app_crons_path(app_name)
    if path.is_file():
        path.unlink()
        logger.info("Removed cron manifest for app %s", app_name)
        return 1
    return 0


def load_app_cron_defs(app_name: str) -> list[dict[str, Any]]:
    """Load persisted cron definitions for an app (used by CronService bridge)."""
    path = _app_crons_path(app_name)
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def register_app_crons_with_service(app_name: str, cron_service: Any) -> list[str]:
    """Promote persisted app cron defs into the running CronService.

    Reads ``app-crons.json`` and registers each job via :class:`CronSDK`,
    which tags ownership as ``created_by="app:{app_name}"``.

    Idempotent — jobs already present (by name) are skipped.
    """
    if cron_service is None:
        return []

    defs = load_app_cron_defs(app_name)
    if not defs:
        return []

    sdk = CronSDK(app_name, cron_service)
    existing_names = {j.name for j in sdk.list_jobs()}

    # circular import: mcp_cron → security → ... → hooks_integration → bridges
    from kiro_crew.mcp_cron import _vet_script_file, _vet_shell_command

    newly_registered: list[str] = []
    for d in defs:
        name = d.get("name", "")
        if not name or name in existing_names:
            continue
        command = d.get("command") or ""
        script = d.get("script") or ""
        # Security vetting: same checks as the MCP cron_add path
        if command:
            err = _vet_shell_command(command)
            if err:
                logger.warning("App %s: cron %r command rejected: %s", app_name, name, err)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_command_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=err,
                )
                continue
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_command_vetted",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        if script:
            try:
                file_path, _ = resolve_script_path(script)
                err = _vet_script_file(file_path)
                if err:
                    logger.warning("App %s: cron %r script rejected: %s", app_name, name, err)
                    sel().log_api_access(
                        caller="app_bridge",
                        operation="app_cron_script_vetted",
                        outcome="denied",
                        resources=f"app={app_name} cron={name}",
                        error=err,
                    )
                    continue
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="allowed",
                    resources=f"app={app_name} cron={name}",
                )
            except (PermissionError, FileNotFoundError, ValueError) as exc:
                logger.warning("App %s: cron %r script path rejected: %s", app_name, name, exc)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=str(exc),
                )
                continue
        try:
            sdk.add_job(
                name=name,
                message=d.get("message", ""),
                every_secs=d.get("every"),  # JSON "every" → Python "every_secs"
                cron_expr=d.get("cron_expr"),
                agent=d.get("agent") or "",
                command=command,
                script=script,
                agent_sequence=d.get("agent_sequence") or None,
                env=d.get("env") or None,
                persistent_session=d.get("persistent_session", False),
                silent=bool(d.get("silent", False)),
            )
            newly_registered.append(name)
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        except Exception as exc:
            logger.warning(
                "App %s: failed to register cron %r (%s): %s",
                app_name, name, type(exc).__name__, exc,
            )
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="failed",
                resources=f"app={app_name} cron={name}",
                error=str(exc),
            )

    if newly_registered:
        logger.info(
            "App %s: registered %d cron job(s) with scheduler: %s",
            app_name, len(newly_registered), ", ".join(newly_registered),
        )
    return newly_registered


def deregister_app_crons_from_service(app_name: str, cron_service: Any) -> int:
    """Remove app-owned cron jobs from the running CronService.

    Mirrors :func:`register_app_crons_with_service`. Uses :class:`CronSDK`,
    which only removes jobs tagged ``created_by="app:{app_name}"`` — other
    apps' jobs are unaffected.

    Idempotent — safe to call when no jobs are registered (returns ``0``).
    Returns the number of jobs removed.
    """
    if cron_service is None:
        return 0
    sdk = CronSDK(app_name, cron_service)
    try:
        return sdk.remove_all()
    except Exception as exc:
        logger.warning(
            "App %s: failed to remove crons from scheduler (%s): %s",
            app_name, type(exc).__name__, exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        return 0


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------

_MCP_JSON_PATH = Path.home() / ".kiro" / "settings" / "mcp.json"


@contextmanager
def _mcp_lock(*, exclusive: bool = True) -> Iterator[None]:
    """Acquire a lock on mcp.json for the duration of the block.

    Uses a single ``.lock`` sidecar file for both shared and exclusive
    locks so that readers and writers coordinate properly.
    """
    lock_path = _MCP_JSON_PATH.with_suffix(".lock")
    _MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    # "r+" (not "r"): Windows msvcrt.locking requires write access on the fd —
    # a read-only handle fails with EACCES and platform_compat.file_lock
    # swallows it (best-effort), silently degrading this to a no-op and letting
    # concurrent writers race the atomic mcp.json rename.
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=exclusive):
            yield


def _read_mcp_json_unlocked() -> dict[str, Any]:
    """Read mcp.json without acquiring a lock (caller must hold lock)."""
    if not _MCP_JSON_PATH.is_file():
        return {}
    try:
        return json.loads(_MCP_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read mcp.json: %s", exc)
        return {}


def _write_mcp_json_unlocked(data: dict[str, Any]) -> None:
    """Write mcp.json without acquiring a lock (caller must hold lock)."""
    _MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_MCP_JSON_PATH, json.dumps(data, indent=2) + "\n")


def _read_mcp_json() -> dict[str, Any]:
    """Read mcp.json with a shared lock."""
    with _mcp_lock(exclusive=False):
        return _read_mcp_json_unlocked()


def _resolve_live_mcp_url(app_name: str, url: str, live_port: int | None = None) -> str:
    """Rewrite a manifest HTTP MCP url's port to the backend's ACTUALLY-allocated port.

    Gateway-managed backends declare ``backend.port:"auto"`` and get a free port at
    spawn time (``backend.py:_find_free_port`` — 9100 if free, else 9101, …). The
    manifest's ``mcpServers.<name>.url`` carries an illustrative fixed port (e.g.
    ``http://localhost:9100/mcp``). Registering that verbatim is a latent bug: whenever
    the backend lands on a different port, the registered MCP server points at the wrong
    one and every agent tool call to the app silently fails. Here we substitute the live
    port (preserving scheme/host/path) so the registration always matches the running
    backend. Non-HTTP transports and apps with no resolvable port are passed through
    unchanged.

    ``live_port`` may be passed explicitly by a caller that knows the just-allocated
    port (the boot/enable path, where the backend isn't marked *healthy* yet so the
    tracked-port lookup would still return None). When omitted we fall back to the
    health-gated ``get_app_backend_port``.
    """
    if not url or not url.startswith("http"):
        return url
    try:
        if live_port is None:
            # circular import: backend.py imports from bridges (reregister_app_mcp_servers
            # in its boot path), so bridges can't import backend at module load — defer it.
            from kiro_crew.apps.backend import get_app_backend_port
            live_port = get_app_backend_port(app_name)
        if not live_port:
            return url  # backend not running yet — keep the manifest default
        p = urlparse(url)
        if p.port == live_port:
            return url  # already correct
        host = p.hostname or "127.0.0.1"
        netloc = f"{host}:{live_port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:  # noqa: BLE001 — registration must never crash on URL rewrite
        return url


def _live_port_for(app_name: str, live_port: int | None) -> int | None:
    """The backend's actually-allocated port, or None if it isn't running yet.

    ``live_port`` (passed by the boot/enable path that just spawned the backend) wins;
    otherwise fall back to the health-gated tracked-port lookup. Never raises — a failure
    to resolve is treated as "not live" so registration can fail safe."""
    if live_port:
        return live_port
    try:
        # circular import: backend.py imports from bridges in its boot path — defer.
        from kiro_crew.apps.backend import get_app_backend_port

        return get_app_backend_port(app_name)
    except Exception:  # noqa: BLE001 — registration must never crash on a port lookup
        return None


def _register_mcp_servers(app_name: str, manifest: AppManifest, live_port: int | None = None) -> list[str]:
    """Register app-provided MCP servers into global mcp.json.

    Uses ``{app_name}:{server_name}`` namespace to avoid collisions. HTTP MCP urls have
    their port rewritten to the backend's live allocated port (see
    :func:`_resolve_live_mcp_url`) so a ``backend.port:"auto"`` app whose backend landed
    on a non-default port is still reachable by agents. ``live_port`` lets the boot/enable
    path pass the just-allocated port directly (health not yet confirmed).

    FAIL-SAFE for ``backend.port:"auto"`` HTTP servers (regression fix):
    a manifest's ``mcpServers.<name>.url`` carries an ILLUSTRATIVE
    fixed port (e.g. ``:9100``). If we wrote that verbatim while the backend is NOT
    running (app disabled / down / registered before the port is known), the entry is a
    reachable-LOOKING but dead URL. kiro-cli merges global mcp.json into EVERY session and
    tries to connect to it on every request; a connect failure surfaces as a "transient
    HTTP 5xx / backend hiccup", gets retried 3× by the transient-retry path, then shown as
    a hard error — breaking ALL kiro requests, not just this app's. (An alternate ACP
    backend reads a different config file, so it was unaffected — the asymmetry in the
    report.)
    So: an HTTP server with NO resolvable LIVE port is NOT written at all (and any stale
    entry for it is scrubbed) — never a dead URL the kiro binary might still dial whether
    or not it honours a ``disabled`` flag. The boot/enable path calls
    :func:`reregister_app_mcp_servers` with the real ``live_port`` once the backend is up,
    which writes the entry with the correct, reachable port. stdio/command servers (no
    ``url``) are always registered — they have no port to be dead.
    """
    if not manifest.mcpServers:
        return []
    resolved_port = _live_port_for(app_name, live_port)
    registered: list[str] = []
    skipped: list[str] = []
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked()
        servers = mcp_data.setdefault("mcpServers", {})
        for server_name, server_config in manifest.mcpServers.items():
            namespaced = f"{app_name}:{server_name}"
            cfg = dict(server_config) if isinstance(server_config, dict) else server_config
            is_http = isinstance(cfg, dict) and bool(cfg.get("url"))
            if is_http and not resolved_port:
                # No live backend → registering the manifest's dead default-port URL would
                # break every kiro session. Skip it AND scrub any stale entry so a prior
                # (now-dead) registration can't keep poisoning the provider path.
                servers.pop(namespaced, None)
                skipped.append(namespaced)
                continue
            if is_http:
                cfg["url"] = _resolve_live_mcp_url(app_name, cfg["url"], live_port=resolved_port)
                cfg.pop("disabled", None)  # backend is live — ensure enabled
            servers[namespaced] = cfg
            registered.append(namespaced)
        _write_mcp_json_unlocked(mcp_data)
    logger.info(
        "Registered %d MCP server(s) for app %s (live_port=%s); skipped %d HTTP server(s) "
        "with no live backend: %s",
        len(registered),
        app_name,
        resolved_port,
        len(skipped),
        skipped or "none",
    )
    return registered


def reregister_app_mcp_servers(app_name: str, live_port: int | None = None) -> list[str]:
    """Re-register an app's MCP servers AFTER its backend has started, so an HTTP MCP
    url with ``backend.port:"auto"`` is rewritten to the live allocated port. Called
    from the enable + boot paths once the backend is up (the first register_app ran
    before the port was known). ``live_port`` should be the just-allocated port from the
    spawn result — at boot the backend isn't marked *healthy* yet, so the health-gated
    tracked-port lookup would return None and the rewrite would be skipped. Safe to call
    repeatedly — it overwrites the namespaced entries. No-op for apps with no MCP servers."""
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.mcpServers:
        return []
    return _register_mcp_servers(app_name, manifest, live_port=live_port)


def _deregister_mcp_servers(app_name: str) -> int:
    """Remove app MCP servers from global mcp.json."""
    prefix = f"{app_name}:"
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked()
        servers = mcp_data.get("mcpServers", {})
        to_remove = [k for k in servers if k.startswith(prefix)]
        for k in to_remove:
            del servers[k]
        if to_remove:
            _write_mcp_json_unlocked(mcp_data)
    if to_remove:
        logger.info("Deregistered %d MCP server(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------

@dataclass
class RegistrationResult:
    """Summary of what was registered/deregistered for an app."""

    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    crons: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "skills": self.skills,
            "crons": self.crons,
            "mcp_servers": self.mcp_servers,
            "errors": self.errors,
        }


def register_app(app_name: str) -> RegistrationResult:
    """Register all resources for an installed app.

    Reads the app's manifest from its install directory and creates
    symlinks/manifests for agents, skills, crons, and MCP servers.

    Apps with ``resources="app"`` manage their own resource registration
    (agents, skills, MCP servers via SDK).  Bridge registration is skipped
    entirely to avoid creating duplicates that confuse kiro-cli.
    """
    result = RegistrationResult()
    manifest = get_app_manifest(app_name)
    if not manifest:
        result.errors.append(f"app {app_name!r} not found or has invalid manifest")
        return result

    # Self-managed apps handle their own registration — skip all bridge work.
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        logger.debug(
            "Skipping bridge registration for %s (resources=app)", app_name,
        )
        return result

    app_root = app_dir(app_name)

    try:
        result.agents = _register_agents(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"agent registration failed: {exc}")

    try:
        result.skills = _register_skills(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"skill registration failed: {exc}")

    try:
        result.crons = _register_crons(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"cron registration failed: {exc}")

    try:
        result.mcp_servers = _register_mcp_servers(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"MCP server registration failed: {exc}")

    logger.info(
        "Registered app %s: %d agents, %d skills, %d crons, %d mcp, %d errors",
        app_name, len(result.agents), len(result.skills),
        len(result.crons), len(result.mcp_servers), len(result.errors),
    )
    return result


def deregister_app(app_name: str) -> RegistrationResult:
    """Deregister all resources for an app.

    Removes symlinks and cron manifests.  Does not remove the app directory.
    """
    result = RegistrationResult()

    try:
        n = _deregister_agents(app_name)
        result.agents = [f"removed {n} agent(s)"]
    except Exception as exc:
        result.errors.append(f"agent deregistration failed: {exc}")

    try:
        _deregister_skills(app_name)
        result.skills = ["removed"]
    except Exception as exc:
        result.errors.append(f"skill deregistration failed: {exc}")

    try:
        _deregister_crons(app_name)
        result.crons = ["removed"]
    except Exception as exc:
        result.errors.append(f"cron deregistration failed: {exc}")

    try:
        n = _deregister_mcp_servers(app_name)
        result.mcp_servers = [f"removed {n} MCP server(s)"]
    except Exception as exc:
        result.errors.append(f"MCP server deregistration failed: {exc}")

    logger.info("Deregistered app %s", app_name)
    return result
