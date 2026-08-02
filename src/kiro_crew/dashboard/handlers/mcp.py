"""MCP server management handlers — probe, sync, toggle, remove."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.mcp_gateway import is_gateway_supported
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Allowlist pattern for MCP server names.  Matches the convention used
# in AIM / kiro-cli (alphanumerics, dashes, underscores, slashes, dots,
# and ``@`` for scoped names like ``@org/server``) and defends against
# command-injection into subprocess calls that pass the name as an argv
# element (e.g. a capability-manager `uninstall <name>` argv).
#
# The leading char must be alphanumeric or ``@`` so a name can't begin
# with ``.`` or ``/``.  Path-traversal sequences (``..``) are rejected
# separately at validation time below.
_VALID_MCP_NAME_RE = re.compile(r"^[@a-zA-Z0-9][@a-zA-Z0-9/_.-]*$")
_MAX_MCP_NAME_LEN = 128


def _is_valid_mcp_name(name: str) -> bool:
    """Return True if ``name`` is a well-formed, non-traversal MCP name."""
    if not name or len(name) > _MAX_MCP_NAME_LEN:
        return False
    if ".." in name:  # reject path traversal even if it matches the charset
        return False
    return bool(_VALID_MCP_NAME_RE.match(name))


_GLOBAL_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"

# Max per-request changes accepted by /api/mcp/apply. The capability-manager
# uninstalls run OFF the MCP file lock (deferred, bounded-concurrent, under one
# phase deadline), so the cap no longer guards lock-hold time — it bounds
# request latency and memory (result list size) for a single apply call. The
# dashboard applies at most one change per visible server, so this is generous.
_MCP_APPLY_MAX_CHANGES = 200

# Bounded concurrency for the deferred capability-manager uninstall phase, so a
# large batch neither serializes (timeout×N) nor floods the companion with N
# simultaneous subprocesses.
_MCP_DEFERRED_UNINSTALL_CONCURRENCY = 8

# Hard ceiling for the whole uninstall phase's deadline. The budget scales with
# the number of concurrency waves (so a legitimate multi-wave batch isn't
# cancelled mid-wave) but never exceeds this, bounding request latency.
_MCP_DEFERRED_UNINSTALL_MAX_BUDGET = 300

# File-based lock for mcp.json — shared with bridges.py so that app
# registration and dashboard MCP handlers coordinate properly.
# Uses fcntl.flock on a sidecar .lock file (works cross-process too).
_MCP_LOCK_PATH = _GLOBAL_MCP_JSON.with_suffix(".lock")


class _McpFileLock:
    """Async context manager wrapping a cross-platform file lock for mcp.json."""

    async def __aenter__(self) -> None:
        _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
        _MCP_LOCK_PATH.touch(exist_ok=True)
        # Open the lock fd WRITABLE. Windows msvcrt.locking() requires write
        # access on the handle — an "r" fd fails with EACCES and
        # platform_compat.acquire_lock swallows that (best-effort semantics),
        # silently degrading this to a no-op and letting concurrent
        # /api/mcp/toggle requests race the atomic-rename write of mcp.json
        # (one flip is lost). "r+" keeps the shared file present (no truncate).
        fd = open(_MCP_LOCK_PATH, "r+")
        # Run blocking lock acquire in a thread to avoid blocking the event
        # loop. Bind self._fd ONLY AFTER a successful acquire — otherwise a
        # raise inside run_in_executor (executor shutdown RuntimeError,
        # fcntl.flock EINTR on POSIX, CancelledError while pending) would
        # abort __aenter__ and Python's async-CM protocol would skip
        # __aexit__, leaking the fd. Close it in the except.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: platform_compat.acquire_lock(fd.fileno(), exclusive=True),
            )
        except BaseException:
            fd.close()
            raise
        self._fd = fd

    async def __aexit__(self, *args: Any) -> None:
        try:
            platform_compat.release_lock(self._fd.fileno())
        finally:
            self._fd.close()


def _get_mcp_lock() -> _McpFileLock:
    """Return an MCP config file lock (compatible with bridges.py)."""
    return _McpFileLock()


class _McpFileLockSync:
    """Synchronous sibling of :class:`_McpFileLock` for the guaranteed-cleanup
    sweep.

    The sweep is dispatched to a worker thread via ``run_in_executor`` (see the
    ``api_mcp_apply`` finally), so this blocking ``acquire_lock`` runs OFF the
    event loop — it must not, and does not, block the loop thread. Running it on
    a worker thread is what makes the cleanup both deadlock-free and
    run-to-completion: (a) the event loop stays free, so any other task that
    currently holds the MCP lock can resume and release it (a loop-blocking
    acquire here would wedge that release → deadlock); and (b) a worker thread is
    not cancelled when the request task is, so the purge finishes even when the
    apply was cancelled mid-flight.
    """

    def __enter__(self) -> None:
        _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
        _MCP_LOCK_PATH.touch(exist_ok=True)
        fd = open(_MCP_LOCK_PATH, "r+")
        try:
            platform_compat.acquire_lock(fd.fileno(), exclusive=True)
        except BaseException:
            fd.close()
            raise
        self._fd = fd

    def __exit__(self, *args: Any) -> None:
        try:
            platform_compat.release_lock(self._fd.fileno())
        finally:
            self._fd.close()


def _get_mcp_lock_sync() -> _McpFileLockSync:
    """Return a SYNCHRONOUS MCP config file lock (used by the cleanup sweep)."""
    return _McpFileLockSync()


# ── Process-wide /api/mcp/apply mutex ───────────────────────────────────
# The file lock (`_get_mcp_lock`) serializes individual filesystem WRITES, but
# an apply is a two-phase TRANSACTION (Phase 1: companion `uninstall_mcp` off the
# lock; Phase 2: config writes under the lock). Two concurrent applies can
# interleave across that boundary — A uninstalls a package and removes its
# config, B then re-adds the same server from a preserved spec — leaving config
# pointing at a removed package. This coarse async mutex spans BOTH phases so
# apply calls are fully serialized; the narrower file lock is retained inside for
# cross-process coordination with bridges.py. Bound to the running loop
# (Python 3.10 compat), mirroring agents.py::_get_config_lock.
_apply_lock: asyncio.Lock | None = None
_apply_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_apply_lock() -> asyncio.Lock:
    """Return the /api/mcp/apply mutex bound to the current event loop."""
    global _apply_lock, _apply_lock_loop
    loop = asyncio.get_running_loop()
    if _apply_lock is None or _apply_lock_loop is not loop:
        _apply_lock = asyncio.Lock()
        _apply_lock_loop = loop
    return _apply_lock


def _write_mcp_json(data: dict) -> None:
    """Atomically write global mcp.json to prevent partial reads."""
    from kiro_crew.agent import (  # noqa: F811  # circular import: agent imports handlers
        _atomic_json_write,
    )

    _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(_GLOBAL_MCP_JSON, data)


# ── MCP Servers ──


_mcp_probe_cache: list[dict] = []
_mcp_probe_ts: float = 0.0
_MCP_PROBE_CACHE_SECS = 600  # 10 min
_mcp_probe_in_progress = False


def _sync_mcp_to_agent(name: str, enabled: bool, *, remove: bool = False) -> None:
    """Serialize the kirocrew.json read-modify-write with bridges' app-MCP
    registration. Both do a FULL RMW of the same file; bridges holds `_mcp_lock`
    while this dashboard path held only the in-process `_get_config_lock` — a
    DIFFERENT lock — so a concurrent app (re)registration and a dashboard toggle
    could each read the file and the last write drop the other's change. Lock the
    file actually being written (== `_mcp_json_path()` in production, the patched
    tmp path under test) so the two paths serialize on one lock.
    """
    from kiro_crew.apps.bridges import _mcp_lock
    from kiro_crew.dashboard.handlers.agents import _installed_agent_config

    with _mcp_lock(target=_installed_agent_config()):
        _sync_mcp_to_agent_unlocked(name, enabled, remove=remove)


def _sync_mcp_to_agent_unlocked(name: str, enabled: bool, *, remove: bool = False) -> None:
    """Sync MCP server state to kirocrew.json mcpServers (not tools/allowedTools)."""
    from kiro_crew.dashboard.handlers.agents import (  # noqa: F811 circular: agents imports mcp
        _installed_agent_config,
    )

    path = _installed_agent_config()
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read agent config %s, skipping sync: %s", path, exc)
        return

    alias = mcp_server_alias(name)
    if enabled and not remove:
        # Ensure server exists in kirocrew.json mcpServers when enabled
        mcp_servers = cfg.setdefault("mcpServers", {})
        tool_ref = f"@{alias}"
        changed = False
        if alias not in mcp_servers:
            # Copy spec from global mcp.json (looked up by original name)
            try:
                gdata = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
                spec = gdata.get("mcpServers", {}).get(name, {})
                if isinstance(spec, dict) and spec:
                    entry = {k: v for k, v in spec.items() if k != "disabled"}
                    # Strip a governed `autoApprove`: kiro-cli honours it on the
                    # copied entry and auto-approves the server WITHOUT reaching the
                    # PreToolUse gate — the same exemption `allowedTools` is
                    # withheld for below. Drop it when the ceiling constrains this
                    # server.
                    if "autoApprove" in entry and not may_skip_gate_now(tool_ref):
                        entry.pop("autoApprove", None)
                    mcp_servers[alias] = entry
                    changed = True
                else:
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                return
        # Strip a governed `autoApprove` from the entry REGARDLESS of whether the
        # alias was just copied or already existed: a re-enable, or a spec written
        # before the ceiling arrived, skips the copy branch above and would keep
        # its gate exemption otherwise.
        _existing = mcp_servers.get(alias)
        if (
            isinstance(_existing, dict)
            and "autoApprove" in _existing
            and not may_skip_gate_now(tool_ref)
        ):
            _existing.pop("autoApprove", None)
            changed = True
        # Ensure @server-name in tools, and in allowedTools only if the
        # governance ceiling has nothing to say about this server. `tools` MOUNTS
        # it; `allowedTools` additionally auto-approves it, and auto-approve is
        # the one path that never reaches the PreToolUse gate — so granting it
        # unconditionally here made the ceiling un-enforceable for any server a
        # user enables from the dashboard, which is the common case.
        keys = ("tools", "allowedTools") if may_skip_gate_now(tool_ref) else ("tools",)
        for key in keys:
            lst = cfg.setdefault(key, [])
            if tool_ref not in lst:
                lst.append(tool_ref)
                changed = True
        if "allowedTools" not in keys:
            stale = cfg.get("allowedTools")
            if isinstance(stale, list) and tool_ref in stale:
                # A grant written before the ceiling arrived must not survive it.
                stale.remove(tool_ref)
                changed = True
        if not changed:
            return
        if "allowedTools" not in keys:
            # Governed: the ref was mounted in `tools` but auto-approve was
            # WITHHELD from allowedTools (and any stale grant/autoApprove
            # removed) because the ceiling constrains this server. Record the
            # withheld decision — emitting mcp_tools_added here would falsely
            # report that auto-approve was granted.
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="dashboard",
                resources=f"{tool_ref} mounted in tools; auto-approve withheld (ceiling)",
            )
        else:
            sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="dashboard",
                resources=f"{tool_ref} added to tools/allowedTools",
            )
    # On disable/remove, clean up any @server-name refs the user may have added
    if not enabled or remove:
        stale_refs = {f"@{alias}", f"@{name}"}
        tool_ref = f"@{alias}"
        cfg["tools"] = [t for t in cfg.get("tools", []) if t not in stale_refs]
        cfg["allowedTools"] = [t for t in cfg.get("allowedTools", []) if t not in stale_refs]
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="dashboard",
            resources=f"{tool_ref} removed from tools/allowedTools",
        )
    if remove:
        cfg.get("mcpServers", {}).pop(alias, None)
        cfg.get("mcpServers", {}).pop(name, None)
    try:
        from kiro_crew.agent import (  # noqa: F811 circular: agent imports handlers
            _atomic_json_write,
        )

        _atomic_json_write(path, cfg)
    except OSError as exc:
        logger.warning("Cannot write agent config %s: %s", path, exc)


def _sync_mcp_to_agent_batch(names: list[str], enabled: bool) -> None:
    """See :func:`_sync_mcp_to_agent` — the same file lock around the batch RMW."""
    from kiro_crew.apps.bridges import _mcp_lock
    from kiro_crew.dashboard.handlers.agents import _installed_agent_config

    with _mcp_lock(target=_installed_agent_config()):
        _sync_mcp_to_agent_batch_unlocked(names, enabled)


def _sync_mcp_to_agent_batch_unlocked(names: list[str], enabled: bool) -> None:
    """Batch sync multiple MCP servers to kirocrew.json in a single read-modify-write."""
    from kiro_crew.dashboard.handlers.agents import (  # noqa: F811 circular: agents imports mcp
        _installed_agent_config,
    )

    path = _installed_agent_config()
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read agent config %s, skipping batch sync: %s", path, exc)
        return

    changed = False
    if enabled:
        # Ensure all servers exist in kirocrew.json mcpServers
        mcp_servers = cfg.setdefault("mcpServers", {})
        try:
            gdata = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            gdata = {}
        granted_refs: list[str] = []
        withheld_refs: list[str] = []
        for name in names:
            alias = mcp_server_alias(name)
            if alias not in mcp_servers:
                spec = gdata.get("mcpServers", {}).get(name, {})
                if not isinstance(spec, dict) or not spec:
                    continue
                _entry = {k: v for k, v in spec.items() if k != "disabled"}
                # Strip a governed autoApprove — see the single-server path.
                if "autoApprove" in _entry and not may_skip_gate_now(f"@{alias}"):
                    _entry.pop("autoApprove", None)
                mcp_servers[alias] = _entry
                changed = True
            # Strip a governed autoApprove REGARDLESS of whether the alias was just
            # copied or already existed — see the single-server path.
            _existing = mcp_servers.get(alias)
            if (
                isinstance(_existing, dict)
                and "autoApprove" in _existing
                and not may_skip_gate_now(f"@{alias}")
            ):
                _existing.pop("autoApprove", None)
                changed = True
            # Same split as the single-server path above: mount always,
            # auto-approve only when the ceiling is silent about this server.
            tool_ref = f"@{alias}"
            keys = ("tools", "allowedTools") if may_skip_gate_now(tool_ref) else ("tools",)
            if "allowedTools" in keys:
                granted_refs.append(tool_ref)
            else:
                withheld_refs.append(tool_ref)
            for key in keys:
                lst = cfg.setdefault(key, [])
                if tool_ref not in lst:
                    lst.append(tool_ref)
                    changed = True
            if "allowedTools" not in keys:
                stale = cfg.get("allowedTools")
                if isinstance(stale, list) and tool_ref in stale:
                    stale.remove(tool_ref)
                    changed = True
        if changed:
            if granted_refs:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_tools_added",
                    outcome="ok",
                    source="dashboard",
                    resources=f"{', '.join(granted_refs)} added to tools/allowedTools",
                )
            if withheld_refs:
                # Governed refs: mounted in `tools` but auto-approve withheld
                # from allowedTools. Record the withheld decision rather than a
                # grant, matching the single-server path.
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="dashboard",
                    resources=(
                        f"{', '.join(withheld_refs)} mounted in tools; "
                        "auto-approve withheld (ceiling)"
                    ),
                )
    else:
        # Remove both the alias ref and any legacy slash ref the user may have.
        refs_to_remove = {f"@{name}" for name in names} | {
            f"@{mcp_server_alias(name)}" for name in names
        }
        cfg["tools"] = [t for t in cfg.get("tools", []) if t not in refs_to_remove]
        cfg["allowedTools"] = [t for t in cfg.get("allowedTools", []) if t not in refs_to_remove]
        changed = True
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="dashboard",
            resources=f"{', '.join(sorted(refs_to_remove))} removed from tools/allowedTools",
        )
    if not changed:
        return
    try:
        from kiro_crew.agent import (  # noqa: F811 circular: agent imports handlers
            _atomic_json_write,
        )

        _atomic_json_write(path, cfg)
    except OSError as exc:
        logger.warning("Cannot write agent config %s: %s", path, exc)


async def _bg_mcp_probe() -> None:
    """Background MCP probe — populates cache at startup."""
    global _mcp_probe_ts, _mcp_probe_in_progress
    try:
        # circular import: mcp_discovery defers imports of kiro_crew.agent
        # which shares state with this module, so importing it at module top
        # would cycle. Kept in-function like every other mcp_discovery import
        # in this file. noqa: F811 for the same-named import at mcp.py:426.
        from kiro_crew.mcp_discovery import probe_all  # noqa: F811

        global_mcps: dict[str, Any] = {}
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
            global_mcps = data.get("mcpServers", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Route through probe_all() so the fan-out is bounded by its
        # _PROBE_MAX_CONCURRENCY semaphore. An
        # unbounded gather here floods the loop's default executor during a
        # network blip and can starve the heartbeat into a watchdog _exit.
        probed = await probe_all()
        result: list[dict[str, Any]] = []
        for s in probed:
            d = s.to_dict()
            spec = global_mcps.get(s.name, {})
            d["enabled"] = not (isinstance(spec, dict) and spec.get("disabled"))
            if isinstance(spec, dict) and spec.get("disabledTools"):
                d["disabledTools"] = spec["disabledTools"]
            result.append(d)
        _mcp_probe_cache[:] = result
        _mcp_probe_ts = time.time()
        logger.info("MCP probe complete: %d servers", len(result))
    except Exception:
        logger.debug("Background MCP probe failed", exc_info=True)
    finally:
        _mcp_probe_in_progress = False


async def api_mcp_servers(request: web.Request) -> web.Response:
    """GET /api/mcp — list configured MCP servers with enabled state.

    Reads from ``~/.kiro/settings/mcp.json`` — the global MCP config that
    kiro-cli ACP actually loads at runtime.  Agent-level ``mcpServers``
    and ``includeMcpJson`` are ignored by kiro-cli in ACP mode.
    """
    global _mcp_probe_in_progress
    from kiro_crew.mcp_discovery import list_servers  # circular import

    # Kick off a background re-probe if the handler cache is stale,
    # so the next request gets fresh results.
    now = time.time()
    should_reprobe = now - _mcp_probe_ts > _MCP_PROBE_CACHE_SECS and not _mcp_probe_in_progress

    servers = list_servers()

    # Overlay handler-level probe cache (last successful probe results)
    # so that "outdated" from the expired discovery cache is replaced with
    # the actual last-known status.  Without this, every page load after
    # 30 min shows "Outdated" even though the servers are healthy.
    cached_by_name: dict[str, dict] = {s["name"]: s for s in _mcp_probe_cache}

    # Also re-probe if a new server appeared (e.g. fresh install from AIM
    # Browse) so status transitions from "Unknown" to "ok"/"error" on the
    # next page refresh without waiting out the cache TTL.
    #
    # Only consider rows probe_all() would actually probe. It excludes
    # consent-disabled servers on purpose (probing spawns the process), so a
    # disabled row can never enter _mcp_probe_cache — while list_servers()
    # deliberately returns it so the UI can render the row. Comparing the
    # unfiltered list against the cache therefore treated every disabled
    # server as "new" on EVERY request, bypassing the cache TTL and leaving a
    # full spawn fan-out permanently in flight for anyone with one disabled
    # server. Applying probe_all's own filter here keeps the freshness check
    # and the cache contents talking about the same set.
    if not should_reprobe and not _mcp_probe_in_progress:
        for srv in servers:
            if srv.disabled:
                continue
            if srv.name not in cached_by_name:
                should_reprobe = True
                break

    if should_reprobe:
        _mcp_probe_in_progress = True
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_bg_mcp_probe())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

    # Read global mcp.json for disabled state
    global_mcps: dict[str, Any] = {}
    try:
        data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # KiroCrew-scope entries: disabled state (consent-disabled installs and
    # custom adds live only here) + which rows the JSON editor can manage.
    kirocrew_mcps = _load_json_or_empty(_kirocrew_mcp_json()).get("mcpServers", {})
    result: list[dict] = []
    for s in servers:
        d = s.to_dict()
        # Prefer handler cache status over discovery cache "outdated"
        cached = cached_by_name.get(s.name)
        if cached and d["status"] in ("outdated", "unknown"):
            d["status"] = cached.get("status", d["status"])
            d["tools"] = cached.get("tools", d["tools"])
            d["error"] = cached.get("error", d["error"])
        spec = global_mcps.get(s.name, {})
        kc_spec = kirocrew_mcps.get(s.name)
        d["kirocrewManaged"] = isinstance(kc_spec, dict)
        is_disabled = (
            (isinstance(spec, dict) and spec.get("disabled"))
            or (isinstance(kc_spec, dict) and kc_spec.get("disabled"))
            or s.disabled
        )
        d["enabled"] = not is_disabled
        if is_disabled:
            d["status"] = "disabled"
        err = d.get("error")
        if err:
            err, _ = redact_credentials(err)
            err, _ = redact_exfiltration_urls(err)
            d["error"] = err
        result.append(d)
    return web.json_response(result)


async def api_mcp_active(request: web.Request) -> web.Response:
    """GET /api/mcp/active — return MCP servers for the current agent.

    For non-kirocrew agents, reads ``mcpServers`` from the agent's config
    in ``~/.kiro/agents/`` — these are the only servers kiro-cli loads
    when ``--agent <name>`` is passed.  For kirocrew (or no agent),
    reads from global ``~/.kiro/settings/mcp.json`` as before.
    """
    from kiro_crew.agent import kiro_agents_dir_path  # noqa: F811

    agent = request.query.get("agent", "")

    # Resolve KiroCrew agent name → kiro agent name so "default" → "kirocrew"
    if agent:
        try:
            from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings  # noqa: F811

            cfg = KiroCrewConfig.load()
            bindings = resolve_agent_bindings(cfg, agent)
            if bindings.kiro_agent:
                agent = bindings.kiro_agent
        except Exception:
            pass

    # Non-kirocrew agent: read from agent config
    if agent and agent != "kirocrew":
        for f in kiro_agents_dir_path().glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("name") == agent:
                    agent_mcps = data.get("mcpServers", {})
                    return web.json_response(
                        [{"name": n, "enabled": True} for n in sorted(agent_mcps)]
                    )
            except (json.JSONDecodeError, OSError):
                continue
        return web.json_response([])

    # Kirocrew / default: read from global mcp.json
    from kiro_crew.mcp_discovery import list_servers  # noqa: F811

    global_mcps: dict[str, Any] = {}
    try:
        data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    servers = list_servers()
    result: list[dict] = []
    for s in servers:
        spec = global_mcps.get(s.name, {})
        enabled = not (isinstance(spec, dict) and spec.get("disabled"))
        result.append({"name": s.name, "enabled": enabled})
    # Also include the managed KiroCrew servers (always enabled). NOTE for
    # ``kirocrew-computer``: "enabled" here means the SERVER is registered, not
    # that computer use is on — the feature's primary enable lives on the keystone
    # (Settings -> Computer Use) and its shim advertises zero tools while off.
    names = {r["name"] for r in result}
    for builtin in ("kirocrew-cron", "kirocrew-core", "kirocrew-computer"):
        if builtin not in names:
            result.insert(0, {"name": builtin, "enabled": True})
    return web.json_response(result)


async def api_mcp_probe(request: web.Request) -> web.Response:
    """POST /api/mcp/probe — probe all MCP servers and return live status.

    Merges ``enabled`` and ``disabledTools`` from global mcp.json so
    probe results don't reset user's previous enable/disable choices.
    """
    global _mcp_probe_ts
    from kiro_crew.mcp_discovery import probe_all  # noqa: F811

    servers = await probe_all()
    # Read global mcp.json for enabled/disabledTools state
    global_mcps: dict[str, Any] = {}
    try:
        data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    result: list[dict[str, Any]] = []
    for s in servers:
        d = s.to_dict()
        spec = global_mcps.get(s.name, {})
        d["enabled"] = not (isinstance(spec, dict) and spec.get("disabled"))
        if isinstance(spec, dict) and spec.get("disabledTools"):
            d["disabledTools"] = spec["disabledTools"]
        result.append(d)
    _mcp_probe_cache[:] = result
    _mcp_probe_ts = time.time()
    return web.json_response(result)


async def api_mcp_probe_cached(request: web.Request) -> web.Response:
    """GET /api/mcp/probe — return cached probe results (non-blocking)."""
    global _mcp_probe_in_progress
    now = time.time()
    if now - _mcp_probe_ts > _MCP_PROBE_CACHE_SECS and not _mcp_probe_in_progress:
        _mcp_probe_in_progress = True
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_bg_mcp_probe())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    return web.json_response(_mcp_probe_cache)


async def api_mcp_sync(request: web.Request) -> web.Response:
    """POST /api/mcp/sync — apply MCP config changes and restart sessions.

    1. Discovers new MCP servers from mcp.json sources.
    2. Adds them to both kirocrew agent config AND global mcp.json
       (kiro-cli ACP only reads the global config).
    3. Resets all sessions so changes take effect.
    """
    from kiro_crew.mcp_discovery import (  # noqa: F811
        discover_servers_to_sync,
        register_servers_for_cc,
        sync_to_agent_config,
    )

    to_sync = discover_servers_to_sync()
    synced = 0
    if to_sync:
        ok = sync_to_agent_config(to_sync)
        if ok:
            synced = len(to_sync)
        register_servers_for_cc(to_sync)
        # Also add to global mcp.json (what ACP actually reads)
        async with _get_mcp_lock():
            try:
                gdata = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                gdata = {"mcpServers": {}}
            gservers = gdata.setdefault("mcpServers", {})
            for s in to_sync:
                if s.name not in gservers:
                    entry: dict[str, Any] = {"command": s.command}
                    if s.args:
                        entry["args"] = s.args
                    if s.env:
                        entry["env"] = s.env
                    gservers[s.name] = entry
            _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
            _write_mcp_json(gdata)

        # Ensure newly-synced servers are added to tools/allowedTools so
        # the AI can actually use them (not just see them in mcpServers).
        from kiro_crew.dashboard.handlers.agents import (
            _get_config_lock,  # circular import: agents imports mcp
        )

        async with _get_config_lock():
            await asyncio.to_thread(
                _sync_mcp_to_agent_batch, [s.name for s in to_sync], enabled=True
            )

    # Always reset sessions — even with no new servers, the user may have
    # toggled enable/disable which writes to kirocrew.json but requires
    # a session restart for kiro-cli to pick up the change.
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions  # noqa: F811

    sessions_reset = await _reset_all_sessions(request)
    return web.json_response(
        {
            "ok": True,
            "synced": synced,
            "servers": [s.name for s in to_sync],
            "sessions_reset": sessions_reset,
        }
    )


async def api_mcp_toggle(request: web.Request) -> web.Response:
    """POST /api/mcp/toggle — enable or disable an MCP server globally.

    1. Sets ``disabled`` in ``~/.kiro/settings/mcp.json`` (ACP runtime).
    2. Syncs ``tools``/``allowedTools`` in ``kirocrew.json`` (non-ACP mode).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    enabled = body.get("enabled", True)
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    async with _get_mcp_lock():
        # 1. Update global mcp.json
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return web.json_response({"error": "cannot parse global mcp.json"}, status=500)

        servers = data.setdefault("mcpServers", {})
        if name not in servers:
            # Server may exist in another scope (agent config, ~/.claude.json).
            # Create a stub so we can store disabled state here.
            from kiro_crew.mcp_discovery import (
                list_servers as _ls,  # circular import: mcp_discovery defers imports of kiro_crew.agent which shares state with this module
            )

            known = {s.name for s in _ls()}
            if name not in known:
                return web.json_response({"error": f"server {name!r} not found"}, status=404)
            servers[name] = {}

        spec = servers[name]
        if not isinstance(spec, dict):
            if isinstance(spec, str):
                servers[name] = spec = {"command": spec}
            else:
                return web.json_response(
                    {"error": f"server {name!r} has invalid config type: {type(spec).__name__}"},
                    status=500,
                )
        if enabled:
            spec.pop("disabled", None)
        else:
            spec["disabled"] = True

        try:
            _write_mcp_json(data)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        # 2. Sync to kirocrew.json tools/allowedTools (lock prevents lost updates vs agents.py)
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            await asyncio.to_thread(_sync_mcp_to_agent, name, enabled)

    return web.json_response({"ok": True, "name": name, "enabled": enabled, "applied": True})


async def api_mcp_toggle_tool(request: web.Request) -> web.Response:
    """POST /api/mcp/toggle-tool — enable or disable a specific tool in an MCP server.

    Updates ``disabledTools`` in ``~/.kiro/settings/mcp.json``.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    server = body.get("server", "").strip()
    tool = body.get("tool", "").strip()
    enabled = body.get("enabled", True)
    if not server or not tool:
        return web.json_response({"error": "server and tool are required"}, status=400)

    async with _get_mcp_lock():
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return web.json_response({"error": "cannot parse global mcp.json"}, status=500)

        servers = data.setdefault("mcpServers", {})
        if server not in servers:
            # Server may exist in another scope (agent config, ~/.claude.json)
            # but not in kiro global mcp.json. Create a stub entry to hold
            # disabledTools state — kiro-cli reads this file for enforcement.
            from kiro_crew.mcp_discovery import (
                list_servers as _ls,  # circular import: mcp_discovery defers imports of kiro_crew.agent which shares state with this module
            )

            known = {s.name for s in _ls()}
            if server not in known:
                return web.json_response({"error": f"server {server!r} not found"}, status=404)
            servers[server] = {}

        spec = servers[server]
        if not isinstance(spec, dict):
            if isinstance(spec, str):
                servers[server] = spec = {"command": spec}
            else:
                return web.json_response(
                    {"error": f"server {server!r} has invalid config type: {type(spec).__name__}"},
                    status=500,
                )
        disabled_tools: list[str] = spec.get("disabledTools", [])
        if enabled:
            disabled_tools = [t for t in disabled_tools if t != tool]
        else:
            if tool not in disabled_tools:
                disabled_tools.append(tool)
        if disabled_tools:
            spec["disabledTools"] = disabled_tools
        else:
            spec.pop("disabledTools", None)

        try:
            _write_mcp_json(data)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "server": server, "tool": tool, "enabled": enabled})


async def api_mcp_toggle_all(request: web.Request) -> web.Response:
    """POST /api/mcp/toggle-all — enable or disable all MCP servers."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled", True)

    async with _get_mcp_lock():
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return web.json_response({"error": "cannot parse global mcp.json"}, status=500)

        servers = data.get("mcpServers", {})
        toggled: list[str] = []
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            if enabled:
                spec.pop("disabled", None)
            else:
                spec["disabled"] = True
            toggled.append(name)

        try:
            _write_mcp_json(data)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        # Batch sync: single read-modify-write of kirocrew.json
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            await asyncio.to_thread(_sync_mcp_to_agent_batch, toggled, enabled)

    return web.json_response({"ok": True, "enabled": enabled, "count": len(servers)})


async def api_mcp_remove(request: web.Request) -> web.Response:
    """POST /api/mcp/remove — uninstall an MCP server.

    Removes the server from ``~/.kiro/settings/mcp.json`` and syncs
    kirocrew.json.  If the optional ``aim`` package manager happens to be
    on PATH it is also asked to uninstall (best-effort); on a vanilla
    machine ``aim`` is absent and that step is skipped gracefully.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    logger.info("MCP remove: %s", name)

    # Optional capability-manager uninstall (best-effort). The edition's
    # capability manager owns the actual uninstall; only attempt when the seam
    # reports it available so the handler stays fully functional without it.
    from kiro_crew.dashboard.handlers._shared import _capability_manager

    mgr = _capability_manager()
    if mgr.available():
        try:
            res = await mgr.uninstall_mcp(name)
            logger.info(
                "MCP uninstall via capability manager: ok=%s msg=%s", res.ok, res.message[:100]
            )
        except Exception as exc:
            logger.warning("capability-manager mcp uninstall failed for %s: %s", name, exc)

    # Remove from global mcp.json
    async with _get_mcp_lock():
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"mcpServers": {}}
        removed = data.get("mcpServers", {}).pop(name, None) is not None
        if removed:
            _write_mcp_json(data)
            logger.info("MCP remove: removed %s from global mcp.json", name)
        else:
            logger.warning("MCP remove: %s not found in global mcp.json", name)

        # Sync kirocrew.json
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            await asyncio.to_thread(lambda: _sync_mcp_to_agent(name, False, remove=True))

    return web.json_response({"ok": True, "name": name, "removed": removed})


# ---------------------------------------------------------------------------
# MCP server registration (generic REST)
# ---------------------------------------------------------------------------


async def api_mcp_server_detail(request: web.Request) -> web.Response:
    """PUT/DELETE /api/mcp/servers/{name} — register or remove an MCP server.

    PUT registers (or updates) an MCP server definition in the global
    ``~/.kiro/settings/mcp.json`` config.  Requires localhost + X-Internal-Secret.

    Body (PUT)::

        { "command": "node", "args": ["server.js"], "env": {"KEY": "val"} }

    DELETE removes the server from the config.
    """
    name = request.match_info["name"]
    if not name or not name.strip():
        return web.json_response({"error": "server name is required"}, status=400)
    name = name.strip()

    if request.method == "DELETE":
        # Remove from global mcp.json
        async with _get_mcp_lock():
            try:
                data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"mcpServers": {}}
            removed = data.get("mcpServers", {}).pop(name, None) is not None
            if removed:
                _write_mcp_json(data)
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # Hold the config lock across the offloaded read-modify-write: since the
        # sync now runs in a worker thread, two concurrent DELETE/PUT requests
        # would otherwise both read kirocrew.json and the last write would drop
        # the other's change (the event loop used to serialize this for free).
        async with _get_config_lock():
            await asyncio.to_thread(lambda: _sync_mcp_to_agent(name, False, remove=True))
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_server_remove",
            outcome="completed" if removed else "not_found",
            resources=name,
        )
        status = 200 if removed else 404
        return web.json_response({"ok": removed, "name": name, "removed": removed}, status=status)

    # PUT — register or update
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    command = body.get("command", "")
    if not command:
        return web.json_response({"error": "command is required"}, status=400)

    entry: dict[str, Any] = {"command": command}
    if body.get("args"):
        entry["args"] = body["args"]
    if body.get("env"):
        entry["env"] = body["env"]

    # Write to global mcp.json
    async with _get_mcp_lock():
        try:
            data = json.loads(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"mcpServers": {}}
        data.setdefault("mcpServers", {})[name] = entry
        _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_mcp_json(data)

    # Sync to kirocrew.json (enable by default). Config lock across the offloaded
    # write — see the DELETE branch above for why the thread hop needs it.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        await asyncio.to_thread(_sync_mcp_to_agent, name, True)

    logger.info("MCP register via REST: %s command=%s", name, command)
    sel().log_api_access(
        caller="dashboard",
        operation="mcp_server_register",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name}, status=200)


# ─── Batched scope apply ────────────────────────────────────────────────

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home" and
# issue #874; dashboard/handlers/usage.py is the reference implementation.
_KIROCREW_MCP_JSON: Path | None = None


def _kirocrew_mcp_json() -> Path:
    """KiroCrew-scope ``mcp.json`` path, resolved against the live data home."""
    return _KIROCREW_MCP_JSON if _KIROCREW_MCP_JSON is not None else data_home() / "mcp.json"


def _extra_mcp_scopes() -> list:
    """Edition-contributed provider MCP config scopes (CPP seam).

    Public Default returns ``[]`` so the core writes the Kiro global only; a
    companion contributes additional provider scopes (e.g. Claude Code's
    ``~/.claude.json``). Deferred context read so this module never imports the
    platform package at load; fails closed to no extra scopes.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; using none",
    )


async def api_mcp_global_scopes(request: web.Request) -> web.Response:
    """GET /api/mcp/scopes — extra provider global MCP scopes (CPP seam).

    Returns the provider-specific global scopes an edition contributes via
    ``extra_mcp_scopes()``, so the dashboard's Installed-Integrations "Globals"
    column can render a badge per scope instead of hardcoding a provider name.
    The public Default returns ``[]`` (the core manages only the Kiro global,
    which the UI renders unconditionally); a companion returns e.g.
    ``[{"id": "ccGlobal", "label": "Claude"}]``. ``id`` is the presence/apply
    key (``f"{scope.id}Global"``) the UI uses for toggle + apply.
    """
    scopes = [{"id": f"{s.id}Global", "label": s.label or s.id} for s in _extra_mcp_scopes()]
    return web.json_response({"scopes": scopes})


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    """Load JSON from a path; return empty dict on missing/malformed/unreadable.

    Catches the broad ``OSError`` (not just ``FileNotFoundError``) so a
    ``PermissionError`` or ``IsADirectoryError`` on a user-owned file like
    ``~/.claude.json`` won't crash ``api_mcp_apply`` mid-batch and leave
    partially-applied changes without a rebuild.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    """Atomic JSON write; reuses the agent helper."""
    from kiro_crew.agent import (  # noqa: F811  # circular: agent imports dashboard handlers
        _atomic_json_write,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(path, data)


def _find_server_spec_anywhere(name: str) -> dict | None:
    """Locate a server's full spec from any known source.

    Search order matches the KiroCrew merge: agent config → <data home>/mcp.json
    (``config_dir()/mcp.json``, i.e. ~/.kiro/crew/mcp.json) → kiro global →
    edition-contributed provider scopes.  Returns a shallow copy with
    ``disabled`` stripped (the caller decides whether to disable in its target
    scope).
    """
    candidates = [
        kiro_agents_dir() / "kirocrew.json",
        _kirocrew_mcp_json(),
        _GLOBAL_MCP_JSON,
        *[s.global_json for s in _extra_mcp_scopes()],
    ]
    for p in candidates:
        spec = _load_json_or_empty(p).get("mcpServers", {}).get(name)
        if isinstance(spec, dict) and (spec.get("command") or spec.get("url")):
            return {k: v for k, v in spec.items() if k != "disabled"}
    return None


def _scope_has_entry(name: str, path: Path) -> bool:
    return isinstance(_load_json_or_empty(path).get("mcpServers", {}).get(name), dict)


def _set_kirocrew_entry(name: str, *, enabled: bool, spec: dict | None = None) -> str:
    """Set the server's ``disabled`` state in ``<data home>/mcp.json``.

    When ``enabled`` is True and ``spec`` is provided, upserts the full spec
    (used for preservation copies).  When enabled is False, adds/updates the
    entry to carry ``disabled: true`` — preserves existing command/args/env
    if already present; otherwise uses ``spec`` as the seed.

    Returns a short label describing what happened: ``"added"``, ``"enabled"``,
    ``"disabled"``, or ``"noop"``.
    """
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(name)
    existing = existing if isinstance(existing, dict) else None

    if enabled:
        if existing is None and spec is None:
            return "noop"
        if existing is None:
            servers[name] = {k: v for k, v in (spec or {}).items() if k != "disabled"}
            action = "added"
        else:
            # Remove disabled flag if set; otherwise no change needed.
            if existing.get("disabled") is True:
                existing.pop("disabled", None)
                action = "enabled"
            else:
                return "noop"
    else:
        if existing is None:
            base = spec or _find_server_spec_anywhere(name) or {}
            entry = {k: v for k, v in base.items() if k != "disabled"}
            entry["disabled"] = True
            servers[name] = entry
            action = "disabled"
        elif existing.get("disabled") is True:
            return "noop"
        else:
            existing["disabled"] = True
            action = "disabled"

    _atomic_write(_kirocrew_mcp_json(), data)
    return action


def _get_kirocrew_entry(name: str) -> dict | None:
    """The raw entry for ``name`` from ``<data home>/mcp.json`` (or None)."""
    data = _load_json_or_empty(_kirocrew_mcp_json())
    entry = data.get("mcpServers", {}).get(name)
    return dict(entry) if isinstance(entry, dict) else None


def _replace_kirocrew_spec(name: str, spec: dict) -> bool:
    """Replace the full spec of an existing KiroCrew-managed entry.

    Preserves the entry's ``disabled`` state -- editing a spec is not
    consent to run it (mirrors the discover-install consent stance).
    Returns False when the name is not in ``<data home>/mcp.json`` (the
    caller decides how to report servers managed elsewhere).
    """
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(name)
    if not isinstance(existing, dict):
        return False
    entry = {k: v for k, v in spec.items() if k != "disabled"}
    if existing.get("disabled") is True:
        entry["disabled"] = True
    servers[name] = entry
    _atomic_write(_kirocrew_mcp_json(), data)
    return True


def _remove_kirocrew_entry(name: str) -> bool:
    """Delete the server from ``<data home>/mcp.json`` entirely.  Returns True on change."""
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.get("mcpServers", {})
    if name not in servers:
        return False
    del servers[name]
    _atomic_write(_kirocrew_mcp_json(), data)
    return True


def _remove_from_agent_file(path: Path, name: str) -> bool:
    """Delete a server entry from a rendered agent file.

    Used by the uninstall path so the entry doesn't linger in
    ``~/.kiro/agents/kirocrew.json`` / ``~/.claude/agents/kirocrew.mcp.json``
    — the rebuild uses the existing agent file as its merge base, so without
    this targeted delete, additive merging would keep the entry alive.
    Returns True when the file was modified.
    """
    if not path.is_file():
        return False
    data = _load_json_or_empty(path)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    _atomic_write(path, data)
    return True


def _set_scope_entry(path: Path, name: str, *, enabled: bool, spec: dict | None = None) -> str:
    """Add/remove a server from a provider global file (kiro or CC).

    When enabled=True and the server is absent, adds the spec.  When
    enabled=False, removes the entry entirely (NOT soft-disable — the
    dashboard badge treats absent and disabled identically).
    """
    data = _load_json_or_empty(path)
    servers = data.setdefault("mcpServers", {})
    present = name in servers and isinstance(servers[name], dict)

    if enabled:
        if present:
            # Already enabled; if the entry had disabled:true, clear it.
            s = servers[name]
            if isinstance(s, dict) and s.get("disabled") is True:
                s.pop("disabled", None)
                _atomic_write(path, data)
                return "enabled"
            return "noop"
        if spec is None:
            spec = _find_server_spec_anywhere(name)
        if spec is None:
            return "missing_spec"
        servers[name] = {k: v for k, v in spec.items() if k != "disabled"}
        _atomic_write(path, data)
        return "added"
    # enabled=False — hard remove.
    if not present:
        return "noop"
    del servers[name]
    _atomic_write(path, data)
    return "removed"


def _purge_server_config(name: str) -> dict[str, str]:
    """Remove a server's config from EVERY scope + rendered agent file.

    The config-side half of an uninstall, factored out so the normal
    per-change path and the guaranteed-cleanup sweep (see ``api_mcp_apply``)
    run byte-identical removal logic. Idempotent: each helper it calls is a
    read-modify-write that no-ops when the entry is already absent, so running
    it a second time (e.g. the sweep re-purging a name the loop already handled)
    changes nothing. MUST be called under the MCP file lock. Returns the
    per-scope action labels for the response outcome.
    """
    actions: dict[str, str] = {}
    actions["kirocrew"] = "removed" if _remove_kirocrew_entry(name) else "noop"
    actions["kiroGlobal"] = _set_scope_entry(_GLOBAL_MCP_JSON, name, enabled=False)
    for scope in _extra_mcp_scopes():
        actions[f"{scope.id}Global"] = _set_scope_entry(scope.global_json, name, enabled=False)
    # Also strip the entry directly from the rendered agent files so the next
    # rebuild doesn't resurrect it via the "start from existing agent config"
    # base. Without this the additive merge keeps the entry around.
    _remove_from_agent_file(kiro_agents_dir() / "kirocrew.json", name)
    for scope in _extra_mcp_scopes():
        if scope.agent_mcp_file is not None:
            _remove_from_agent_file(scope.agent_mcp_file, name)
    return actions


def _sweep_dangling_uninstalls(
    uninstall_names: list[str],
    purged_names: set[str],
) -> None:
    """Purge the config of every REQUESTED uninstall the apply loop did not reach.

    The compensation for package-first ordering: Phase 1 removes the companion
    package before Phase 2 removes its persisted config, so if the apply is
    interrupted between them (an earlier change's write raised, OR the request
    task was cancelled — e.g. gateway shutdown / client disconnect — during
    Phase 1, before the Phase-2 lock is ever taken) the config would dangle at
    an already-removed package.

    Sweeps by REQUEST, not by companion outcome. Phase 2's own uninstall branch
    removes config unconditionally for every ``uninstall`` change (it does not
    consult ``capability_results``), so the sweep mirrors that: any requested
    uninstall the loop did not reach (not in ``purged_names``) gets its config
    removed. Keying on the request — rather than on a ``capability_results`` entry
    that a cancellation could leave unset the instant AFTER the companion removed
    the package — closes the "package gone, result unrecorded, config kept"
    window at the root. Removing config for a requested uninstall is the user's
    intent anyway, and errs toward the BENIGN failure direction (config gone,
    package possibly orphaned-but-harmless — recoverable by reinstall) rather
    than the harmful one (config kept, package gone → broken at every session).

    SYNCHRONOUS by design and dispatched to a worker thread by the caller (the
    ``api_mcp_apply`` finally does ``run_in_executor`` + awaits the future to
    completion, shielding it across cancellation). Running the whole
    acquire→purge→release off the event loop is what makes it both
    deadlock-free (the loop stays free so a task holding the MCP lock can release
    it) and run-to-completion (a worker thread is not cancelled when the request
    task is). It uses its OWN synchronous lock, so it is correct whether or not
    the caller still holds the async lock (the Phase-2 ``async with`` has already
    exited by the time we reach here, and on a Phase-1 cancel it was never taken).
    Idempotent (``_purge_server_config`` no-ops on absent entries), so a name
    already purged by the loop (in ``purged_names``) is skipped and a re-run is
    cheap.
    """
    to_sweep = [n for n in uninstall_names if n not in purged_names]
    if not to_sweep:
        return
    # Best-effort: this runs from the apply's finally, so it must NEVER raise —
    # a failure here (even the lock acquire) would replace a successful apply's
    # response with a 500, or mask the original exception being unwound. Guard
    # the WHOLE body (lock acquire included), not just each purge; a miss self-
    # heals on the next idempotent apply.
    try:
        with _get_mcp_lock_sync():
            for uname in to_sweep:
                _purge_server_config(uname)
                logger.warning(
                    "mcp apply interrupted before purging config for requested "
                    "uninstall %r; swept it in guaranteed cleanup to avoid a "
                    "dangling config→removed-package reference",
                    uname,
                )
    except Exception:
        logger.exception(
            "guaranteed-cleanup sweep failed (%s); config may reference a removed "
            "package until the next apply",
            to_sweep,
        )


def _set_tool_overrides(name: str, tool_overrides: dict[str, bool]) -> list[str]:
    """Apply per-tool enable/disable overrides to a server's entry in
    ``<data home>/mcp.json``.

    ``tool_overrides`` maps tool name → desired enabled state.  Disabled
    tools are added to the entry's ``disabledTools`` list; re-enabling
    removes them.  Creates the entry if absent (sourcing full spec from
    any scope so the server keeps loading).

    Returns a list of tool names whose state changed.
    """
    if not tool_overrides:
        return []
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    entry = servers.get(name)
    if not isinstance(entry, dict):
        # Seed from the best-available spec so the server keeps its config.
        base = _find_server_spec_anywhere(name) or {}
        entry = {k: v for k, v in base.items() if k != "disabled"}
        servers[name] = entry

    disabled = list(entry.get("disabledTools") or [])
    changed: list[str] = []
    for tool, tool_enabled in tool_overrides.items():
        if tool_enabled and tool in disabled:
            disabled.remove(tool)
            changed.append(tool)
        elif (not tool_enabled) and tool not in disabled:
            disabled.append(tool)
            changed.append(tool)

    if disabled:
        entry["disabledTools"] = disabled
    else:
        entry.pop("disabledTools", None)

    if changed:
        _atomic_write(_kirocrew_mcp_json(), data)
    return changed


async def api_mcp_apply(request: web.Request) -> web.Response:
    """POST /api/mcp/apply — serialize the two-phase apply under the apply mutex.

    Thin wrapper: the apply is a Phase-1 (companion uninstall, off the file lock)
    + Phase-2 (config writes, under the file lock) TRANSACTION, and the file lock
    only serializes individual writes — not the phase boundary. Without a mutex
    two concurrent applies can interleave so one re-adds a server from a preserved
    spec after another removed its package, leaving config pointing at a removed
    package. ``_get_apply_lock`` (a process-wide async mutex spanning BOTH phases)
    closes that; the narrower file lock is retained inside ``_do_mcp_apply`` for
    cross-process coordination with bridges.py.
    """
    async with _get_apply_lock():
        return await _do_mcp_apply(request)


async def _do_mcp_apply(request: web.Request) -> web.Response:
    """The batched per-scope apply body (runs under the apply mutex).

    Request body::

        {
          "changes": [
            {
              "name": "slack-mcp",
              "kirocrew": true,     // desired MC visibility
              "kiroGlobal": true,   // desired presence in ~/.kiro/settings/mcp.json
              "ccGlobal": false,    // desired presence in ~/.claude.json
              "uninstall": false,   // optional: remove from all scopes + aim
              "toolOverrides": {    // optional: per-tool enable/disable
                "SkillsTool": false,
                "ReadFile": true
              }
            }
          ]
        }

    Each change is processed in the order MC → Kiro → CC, with a
    preservation step first: if the user is removing the server from its
    only source AND MC is desired on, the full spec is copied into
    ``<data home>/mcp.json`` before the removal so MC keeps its config.

    After all changes are written, ``rebuild_agent_config`` is called once
    so the provider-native agent files (``~/.kiro/agents/kirocrew.json`` and
    ``~/.claude/agents/kirocrew.md`` + ``kirocrew.mcp.json``) reflect the
    new merged state.  Returns a summary with per-change outcomes.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    changes = body.get("changes")
    if not isinstance(changes, list):
        return web.json_response({"error": "changes must be a list"}, status=400)
    if len(changes) > _MCP_APPLY_MAX_CHANGES:
        return web.json_response(
            {"error": f"too many changes (max {_MCP_APPLY_MAX_CHANGES})"}, status=400
        )

    results: list[dict] = []

    # ── Phase 1 (BEFORE the MCP file lock): companion package uninstalls ──
    # Run capability-manager uninstalls FIRST, off the lock, so:
    #  (a) a slow companion op never holds the process-wide lock (no timeout×N
    #      stall of every other MCP handler), and
    #  (b) the package is removed BEFORE its scope-file config entries are (config
    #      removal is the LAST step, under the lock below). If a concurrent apply
    #      re-adds the same server, it can't leave persisted MCP config pointing
    #      at an already-removed package — the config write is serialized under
    #      the lock and is last-writer-wins (TOCTOU fix, GPT 5.6 HIGH).
    # Package-first flips the partial-failure DIRECTION toward the harmful one
    # (package gone, config persists → the server fails at every subsequent
    # session start), so BOTH phases run inside one outer try/finally: on ANY
    # exit — a Phase-2 write error, OR a CancelledError raised during Phase 1
    # itself (gateway shutdown / client disconnect, before the Phase-2 lock is
    # ever taken) — the finally sweeps (shielded, re-acquiring the lock) the
    # config of every package Phase 1 CONFIRMED removed, closing the window down
    # to the irreducible hard-kill case, which self-heals idempotently on the
    # next apply. See platform-context.md → "uninstall ordering & the crash
    # window".
    # Deduped, bounded-concurrent, under ONE phase-level deadline; results are
    # merged into each uninstall change's outcome inside the locked loop.
    capability_results: dict[str, dict[str, str]] = {}
    uninstall_names = sorted(
        {
            n
            for c in changes
            if isinstance(c, dict)
            and c.get("uninstall")
            and (n := str(c.get("name", "")).strip())
            and _is_valid_mcp_name(n)
        }
    )
    # Names whose config the locked loop has already purged — so the
    # guaranteed-cleanup sweep does not redundantly re-purge them.
    purged_names: set[str] = set()

    try:
        if uninstall_names:
            from kiro_crew.dashboard.handlers._shared import _capability_manager
            from kiro_crew.platform.capability_bound import (
                CAPABILITY_UNINSTALL_TIMEOUT as _CAPABILITY_UNINSTALL_TIMEOUT,
            )

            mgr = _capability_manager()
            if mgr.available():
                sem = asyncio.Semaphore(_MCP_DEFERRED_UNINSTALL_CONCURRENCY)

                async def _run_one_uninstall(uname: str) -> None:
                    async with sem:
                        try:
                            res = await mgr.uninstall_mcp(uname)
                            capability_results[uname] = {
                                "capability": "uninstalled" if res.ok else "uninstall_failed"
                            }
                        except asyncio.CancelledError:
                            # Never mask cancellation as a companion error: the op
                            # may or may not have completed, so leave the result
                            # unset (the outer finally sweeps only CONFIRMED
                            # "uninstalled" names) and let cancellation propagate.
                            raise
                        except Exception as exc:
                            # Error strings may include env vars / AWS keys / URLs
                            # surfaced by failing operations; scrub before returning.
                            _urls_clean, _ = redact_exfiltration_urls(str(exc))
                            _redacted, _ = redact_credentials(_urls_clean)
                            capability_results[uname] = {"capability_error": _redacted}

                waves = (
                    len(uninstall_names) + _MCP_DEFERRED_UNINSTALL_CONCURRENCY - 1
                ) // _MCP_DEFERRED_UNINSTALL_CONCURRENCY
                budget = min(
                    _CAPABILITY_UNINSTALL_TIMEOUT * max(waves, 1),
                    _MCP_DEFERRED_UNINSTALL_MAX_BUDGET,
                )
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(_run_one_uninstall(n) for n in uninstall_names)),
                        timeout=budget,
                    )
                except asyncio.TimeoutError:
                    # Don't silently drop the status: stamp every uninstall that
                    # didn't finish as "timed_out" so the API response signals the
                    # core↔companion drift (config removed, package maybe not) rather
                    # than reporting a clean success with no capability key.
                    logger.warning(
                        "capability uninstalls exceeded the %ss phase budget; marking "
                        "unfinished as timed_out and proceeding to scope writes",
                        budget,
                    )
                    for uname in uninstall_names:
                        capability_results.setdefault(uname, {"capability": "timed_out"})

        async with _get_mcp_lock():
            for change in changes:
                name = str(change.get("name", "")).strip()
                if not name:
                    results.append({"error": "empty name", "change": change})
                    continue
                # Defense-in-depth: name flows into subprocess argv (capability
                # uninstall) and filesystem paths via scope helpers.  Even
                # though we use list-form subprocess (no shell), reject names
                # that contain argv-injection chars or path traversal.
                if not _is_valid_mcp_name(name):
                    results.append({"error": "invalid name", "name": name})
                    sel().log_api_access(
                        caller="dashboard",
                        operation="mcp_apply_rejected_name",
                        outcome="denied",
                        resources=name[:64],
                    )
                    continue

                outcome: dict[str, Any] = {"name": name, "actions": {}}

                # ── Uninstall path: wipe from all scopes and (best-effort) AIM ──
                if change.get("uninstall"):
                    # Config removal is the LAST mutation (package-then-config
                    # ordering); _purge_server_config strips every scope + agent
                    # file idempotently.
                    outcome["actions"].update(_purge_server_config(name))
                    purged_names.add(name)
                    # Companion package removal already ran in Phase 1 (before the
                    # lock); merge its recorded result here.
                    if name in capability_results:
                        outcome["actions"].update(capability_results[name])
                    sel().log_api_access(
                        caller="dashboard",
                        operation="mcp_uninstall",
                        outcome="ok",
                        resources=name,
                    )
                    results.append(outcome)
                    continue

                # ── Scope toggles: compute desired + apply preservation ──
                desired_mc = bool(change.get("kirocrew", True))
                desired_kiro = bool(change.get("kiroGlobal", False))
                extra_scopes = _extra_mcp_scopes()
                # An omitted ``f"{id}Global"`` field means "preserve current presence"
                # — the OSS frontend never sends it, so defaulting to False would
                # delete a companion-contributed scope's entry on any unrelated apply
                # (a Kiro/MC toggle or a tool-override change). Only an explicit value
                # from an edition frontend changes a contributed scope.
                desired_extra = {
                    s.id: bool(change.get(f"{s.id}Global", _scope_has_entry(name, s.global_json)))
                    for s in extra_scopes
                }

                # Preservation rule: if MC is desired ON and both globals are
                # going to lose the entry (or never had it), copy the spec into
                # <data home>/mcp.json so MC keeps its config via the merge.
                preserved_spec: dict | None = None
                if desired_mc and not desired_kiro and not any(desired_extra.values()):
                    has_mc = _scope_has_entry(name, _kirocrew_mcp_json())
                    if not has_mc:
                        preserved_spec = _find_server_spec_anywhere(name)

                # Apply MC first — flipping MC green needs the entry to exist or
                # the disabled override removed.  Flipping MC gray writes
                # disabled:true, preserving config for later re-enable.
                outcome["actions"]["kirocrew"] = _set_kirocrew_entry(
                    name,
                    enabled=desired_mc,
                    spec=preserved_spec,
                )

                # Apply Kiro and CC (add/remove from their respective globals).
                # Resolve the spec ONCE before any scope mutation — otherwise
                # the kiro removal can vacate the only source that had the
                # spec, and the CC add would get "missing_spec" even though
                # the user clearly intended it to move over.
                resolved_spec = _find_server_spec_anywhere(name)
                outcome["actions"]["kiroGlobal"] = _set_scope_entry(
                    _GLOBAL_MCP_JSON,
                    name,
                    enabled=desired_kiro,
                    spec=resolved_spec,
                )
                for scope in extra_scopes:
                    outcome["actions"][f"{scope.id}Global"] = _set_scope_entry(
                        scope.global_json,
                        name,
                        enabled=desired_extra[scope.id],
                        spec=resolved_spec,
                    )

                # ── Per-tool overrides (disabledTools in <data home>/mcp.json) ──
                tool_overrides = change.get("toolOverrides")
                if isinstance(tool_overrides, dict) and tool_overrides:
                    # Apply the same allowlist as server names — tool names are
                    # persisted to <data home>/mcp.json and later consumed by
                    # kiro-cli / other components, so reject anything that
                    # could smuggle argv-injection chars or path traversal
                    # into downstream reads.  Invalid names are filtered out
                    # silently and audited separately.
                    sanitized: dict[str, bool] = {}
                    rejected: list[str] = []
                    for k, v in tool_overrides.items():
                        tool_name = str(k)
                        if _is_valid_mcp_name(tool_name):
                            sanitized[tool_name] = bool(v)
                        else:
                            rejected.append(tool_name[:64])
                    if rejected:
                        outcome["actions"]["tools_rejected"] = rejected
                        sel().log_api_access(
                            caller="dashboard",
                            operation="mcp_apply_rejected_tool_name",
                            outcome="denied",
                            resources=f"{name}:{','.join(rejected)[:128]}",
                        )
                    if sanitized:
                        changed_tools = _set_tool_overrides(name, sanitized)
                        if changed_tools:
                            outcome["actions"]["tools"] = changed_tools

                # Audit the scope-toggle decision.  Changing scope presence
                # controls which MCP servers (and therefore tools) are
                # reachable from KiroCrew sessions — a permission-shaping
                # event that belongs in the SEL log alongside uninstalls.
                sel().log_api_access(
                    caller="dashboard",
                    operation="mcp_scope_apply",
                    outcome="ok",
                    resources=(
                        f"{name} "
                        f"mc={'on' if desired_mc else 'off'} "
                        f"kiro={'on' if desired_kiro else 'off'}"
                        + "".join(
                            f" {sid}={'on' if on else 'off'}" for sid, on in desired_extra.items()
                        )
                    ),
                )

                results.append(outcome)
    finally:
        # Guaranteed cleanup for the package-first crash window: sweep the config
        # of every REQUESTED uninstall the loop did NOT purge. This finally pairs
        # with the OUTER try that wraps BOTH phases, so it runs even when a
        # CancelledError is raised during Phase 1 — before the Phase-2 lock is
        # ever taken (the gateway-shutdown / client-disconnect case) — not only on
        # a Phase-2 write error. It sweeps by REQUEST (not by companion outcome),
        # matching Phase 2's own unconditional config removal, so a cancellation
        # that lands the instant after the companion removed a package but before
        # its result was recorded still gets that config purged.
        #
        # The sweep (a blocking acquire→purge→release) runs in a WORKER THREAD via
        # run_in_executor, which satisfies both constraints at once:
        #   - deadlock-free: the blocking file-lock acquire is OFF the event loop,
        #     so if another aiohttp task currently holds the MCP lock the loop
        #     stays free to resume it and let it release (a loop-blocking acquire
        #     here would wedge that release — the no-blocking-call-on-event-loop
        #     rule and a real deadlock);
        #   - runs-to-completion: a worker thread is not cancelled when this
        #     request task is, and we await the future to completion before
        #     re-raising, so the config is made consistent even on cancellation
        #     (an un-awaited/shield-only future would run orphaned and loop
        #     teardown could destroy it mid-write).
        _loop = asyncio.get_running_loop()
        _sweep_future = _loop.run_in_executor(
            None,
            _sweep_dangling_uninstalls,
            uninstall_names,
            purged_names,
        )
        try:
            await asyncio.shield(_sweep_future)
        except asyncio.CancelledError:
            # We're unwinding a cancellation: the executor thread is already
            # running and cannot be cancelled, so wait for it to finish the purge
            # before propagating (shield stopped the await from cancelling the
            # future; this second await blocks until the thread returns).
            await _sweep_future
            raise

    # ── Rebuild agent artifacts once all scope writes complete ──
    rebuild_ok = False
    rebuild_error: str | None = None
    try:
        # circular import: kiro_crew.agent imports dashboard handlers, so
        # this is delayed to runtime to break the cycle at module load.
        from kiro_crew.agent import rebuild_agent_config  # noqa: F811

        await asyncio.to_thread(rebuild_agent_config)
        rebuild_ok = True
    except Exception as exc:
        # Rebuild failures can surface file paths, env var contents, or
        # credential fragments (e.g. JSON decode errors that echo file
        # contents).  Apply the same redaction pipeline we use for the
        # capability-manager uninstall error before handing it to the dashboard.
        _urls_clean, _ = redact_exfiltration_urls(str(exc))
        rebuild_error, _ = redact_credentials(_urls_clean)
        logger.warning("rebuild_agent_config failed after apply: %s", exc)

    return web.json_response(
        {
            "ok": True,
            "applied": len(results),
            "results": results,
            "rebuild": {"ok": rebuild_ok, "error": rebuild_error},
        }
    )


# ─── Shared MCP gateway enable toggle ───────────────────────────────────


async def api_mcp_gateway_status(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/status — shared MCP gateway state.

    ``enabled`` reflects the persisted config flag; ``running``/``ping_ok``
    reflect the live broker held by the gateway orchestrator.  The broker is
    only spawned at startup when the flag is on, so a freshly-flipped flag
    reads ``enabled=true`` with ``running=false`` until the restart lands.
    """
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    state: DashboardState = request.app["state"]
    manager = getattr(state, "_mcp_gateway_manager", None)
    running = manager is not None and manager.is_running
    ping_ok = manager is not None and running and await manager.ping()
    return web.json_response(
        {
            "enabled": KiroCrewConfig.load().mcp_gateway.enabled,
            "running": bool(running),
            "ping_ok": bool(ping_ok),
            # Whether the broker can run on this OS at all. The UI reads this to
            # disable the toggle (and explain why) on unsupported platforms
            # (Windows) instead of surfacing a generic "could not apply" failure.
            "supported": is_gateway_supported(),
        }
    )


async def api_mcp_gateway_metrics(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/metrics — live broker pool snapshot.

    Returns ``{running, size, max_backends, backends:[{server, pid, alive,
    sessions, idle_s, rss_kb}]}``.  ``running=false`` (empty backends) when
    the broker isn't up.
    """
    state: DashboardState = request.app["state"]
    manager = getattr(state, "_mcp_gateway_manager", None)
    if manager is None or not manager.is_running:
        return web.json_response({"running": False, "backends": []})
    snap = await manager.stats()
    snap.pop("type", None)
    return web.json_response({"running": True, **snap})


# Serializes in-process gateway apply operations (enable/disable + set-poolable)
# so two concurrent dashboard requests cannot interleave broker start/stop and
# orphan a gatewayd process. The config write is guarded by _get_config_lock();
# this lock guards the apply() side effect that runs AFTER that lock is released.
_MCP_GATEWAY_APPLY_LOCK = asyncio.Lock()


async def api_mcp_gateway_enable(request: web.Request) -> web.Response:
    """POST /api/mcp-gateway/enable — persist the flag and apply it in-process.

    Writes ``mcp_gateway.enabled`` to config.json then applies the change
    live: the broker is started/stopped and all agent sessions are dropped +
    relinked to the new MCP routing — without restarting the gateway process,
    so the dashboard session stays authenticated.  Returns the verified state
    ``{ok, enabled, running, ping_ok}``.
    """
    from kiro_crew.agent import _atomic_json_write  # circular import
    from kiro_crew.config.loader import config_path  # circular import
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # circular import

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)

    # Fail closed on platforms the broker can't run on (Windows): never persist
    # enabled=true where the broker will never start — that would leave config
    # diverged from reality and surface as a generic apply failure. Disabling
    # is always allowed.
    if enabled and not is_gateway_supported():
        return web.json_response(
            {
                "error": "The shared MCP gateway is not supported on this platform.",
                "code": "mcp_gateway_platform_unsupported",
            },
            status=400,
        )

    path = config_path()
    state: DashboardState = request.app["state"]
    apply = getattr(state, "_mcp_gateway_apply", None)
    if apply is None:
        return web.json_response({"error": "gateway apply unavailable"}, status=503)

    # Serialize the whole persist+apply under the apply lock so two racing
    # toggles cannot interleave (write A, write B, apply B, apply A) and leave
    # persisted config.json diverged from live broker state. The config lock is
    # nested inside only for the read-modify-write of config.json itself.
    async with _MCP_GATEWAY_APPLY_LOCK:
        async with _get_config_lock():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                return web.json_response({"error": "config.json is corrupt"}, status=500)
            section = data.setdefault("mcp_gateway", {})
            if not isinstance(section, dict):
                return web.json_response({"error": "mcp_gateway is not an object"}, status=500)
            section["enabled"] = enabled
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json_write(path, data)
        try:
            result = await apply(enabled)
        except Exception as exc:
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="mcp_gateway_enable",
                outcome="error",
                source="dashboard",
                resources=f"enabled={enabled} error={exc}",
            )
            return web.json_response({"error": f"apply failed: {exc}"}, status=500)

    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="mcp_gateway_enable",
        outcome="ok",
        source="dashboard",
        resources=f"enabled={enabled}",
    )
    return web.json_response({"ok": True, **result})


# ─── Per-server poolability management ──────────────────────────────────


async def api_mcp_gateway_servers(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/servers — enumerate distinct MCP servers.

    Reads ``~/.kiro/agents/*.json`` (the clean source specs — the rewriter
    never mutates them) and returns one row per distinct server with its
    effective poolable state.  Pooling is opt-in: a stdio server is pooled
    only when its name is in the config allowlist
    (``mcp_gateway.poolable_servers``) OR its agent-JSON entry sets
    ``poolable:true``.  HTTP/SSE servers are shared by nature (not poolable);
    denylisted servers (``UNPOOLABLE_SERVERS``) can never be pooled.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
    from kiro_crew.mcp_gateway.rewriter import UNPOOLABLE_SERVERS

    allowlist = set(KiroCrewConfig.load().mcp_gateway.poolable_servers)

    rows: dict[str, dict[str, Any]] = {}
    agents_dir = kiro_agents_dir_path()
    if agents_dir.is_dir():
        for path in sorted(agents_dir.glob("*.json")):
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(spec, dict):
                continue
            agent_name = spec.get("name") or path.stem
            mcp_servers = spec.get("mcpServers")
            if not isinstance(mcp_servers, dict):
                continue
            for name, entry in mcp_servers.items():
                if not isinstance(entry, dict):
                    continue
                row = rows.get(name)
                if row is None:
                    row = {
                        "agents": set(),
                        "transport": "stdio" if "command" in entry else "http",
                        "entry_poolable": False,
                    }
                    rows[name] = row
                row["agents"].add(str(agent_name))
                if entry.get("poolable") is True:
                    row["entry_poolable"] = True

    result: list[dict[str, Any]] = []
    for name in sorted(rows):
        row = rows[name]
        is_stdio = row["transport"] == "stdio"
        denylisted = name in UNPOOLABLE_SERVERS
        effective = is_stdio and not denylisted and (name in allowlist or row["entry_poolable"])
        result.append(
            {
                "name": name,
                "poolable": effective,
                "in_allowlist": name in allowlist,
                "entry_poolable": row["entry_poolable"],
                "agents": sorted(row["agents"]),
                "transport": row["transport"],
                "denylisted": denylisted,
            }
        )
    return web.json_response({"servers": result})


async def api_mcp_gateway_set_poolable(request: web.Request) -> web.Response:
    """POST /api/mcp-gateway/servers/poolable — toggle a server's poolable flag.

    Body ``{"name": "slack-mcp", "poolable": true}``.  Adds/removes ``name``
    from ``mcp_gateway.poolable_servers`` in config.json (same config lock +
    atomic write as the enable toggle), then re-applies the change in-process
    so new sessions pick up the new MCP routing without a restart.  When the
    gateway is disabled, the allowlist is persisted only (it takes effect when
    the gateway is enabled).  Returns ``{ok, name, poolable, ...}``.
    """
    from kiro_crew.agent import _atomic_json_write
    from kiro_crew.config.loader import config_path  # noqa: F811
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # circular: agents imports mcp

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = str(body.get("name", "")).strip()
    poolable = body.get("poolable")
    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if not _is_valid_mcp_name(name):
        return web.json_response({"error": "invalid server name"}, status=400)
    if not isinstance(poolable, bool):
        return web.json_response({"error": "poolable must be a boolean"}, status=400)

    path = config_path()
    async with _get_config_lock():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        section = data.setdefault("mcp_gateway", {})
        if not isinstance(section, dict):
            return web.json_response({"error": "mcp_gateway is not an object"}, status=500)
        current = section.get("poolable_servers")
        servers_list = (
            [s for s in current if isinstance(s, str)] if isinstance(current, list) else []
        )
        if poolable and name not in servers_list:
            servers_list.append(name)
        elif not poolable and name in servers_list:
            servers_list = [s for s in servers_list if s != name]
        section["poolable_servers"] = sorted(set(servers_list))
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, data)

    state: DashboardState = request.app["state"]
    apply = getattr(state, "_mcp_gateway_apply_poolable", None)
    applied: dict[str, Any] = {"applied": False}
    if apply is not None:
        try:
            async with _MCP_GATEWAY_APPLY_LOCK:
                applied = await apply()
        except Exception as exc:
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="mcp_gateway_set_poolable",
                outcome="error",
                source="dashboard",
                resources=f"name={name} poolable={poolable} error={exc}",
            )
            return web.json_response({"error": f"apply failed: {exc}"}, status=500)

    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="mcp_gateway_set_poolable",
        outcome="ok",
        source="dashboard",
        resources=f"name={name} poolable={poolable}",
    )
    return web.json_response({"ok": True, "name": name, "poolable": poolable, **applied})
