"""MCP server management handlers — probe, sync, toggle, remove."""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import re
import sys as _sys
import time
from collections.abc import Collection, Mapping
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from aiohttp import web

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import mcp_quarantine, platform_compat
from kiro_crew.agent import (
    AGENT_FILENAME,
    SharedAgentHomeRefused,
    _atomic_json_write,
    _declined_foreign_spec_write,
    entry_as_enabled,
    gateway_routed_names,
    kiro_agents_dir_path,
    rebuild_agent_config_reporting,
)
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    FORWARD_DECLARED_ENV_DEFAULT,
    KiroCrewConfig,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    read_config_text,
)
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.config.sections import MANAGED_STUB_FOLLOWERS
from kiro_crew.dashboard.chat_utils import run_to_completion
from kiro_crew.dashboard.handlers._shared import AGENT_SPEC_NOT_REWRITTEN, read_bounded_json
from kiro_crew.dashboard.handlers.mcp_agent_sync import (
    _agent_home_not_owned,
    _agent_home_not_owned_response,
    _mcp_hot_reload_active,
    _sync_mcp_to_agent,
    _sync_mcp_to_agent_batch,
)
from kiro_crew.dashboard.handlers.mcp_config_entries import (
    _find_server_spec_anywhere,
    _scope_has_entry,
    _set_kirocrew_entry,
    _set_scope_entry,
    _set_tool_overrides,
)
from kiro_crew.dashboard.handlers.mcp_config_files import (
    _GLOBAL_MCP_JSON,
    _KIRO_GLOBAL_SURFACE,
    _MCP_LOCK_PATH,
    AmbiguousServerKey,
    _config_key_for,
    _extra_mcp_scopes,
    _is_valid_mcp_name,
    _kirocrew_mcp_json,
    _mcp_config_corrupt,
    _mcp_server_not_found,
    _mcp_spec_invalid,
    _mcp_write_failed,
    _offload_config_write,
    _refuse_commented_config,
    _string_identifier,
    _user_mcp_config_paths,
)
from kiro_crew.dashboard.handlers.mcp_config_purge import (
    PreflightKeys,
    _forget_owner_servers,
    _preflight_change_keys,
    _purge_server_config,
    _sweep_dangling_uninstalls,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.env import emit_env
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.mcp_cleanup import mcp_entry_is_muted
from kiro_crew.mcp_discovery import (
    SCOPE_KIRO_GLOBAL,
    SCOPE_KIROCREW,
    McpServerInfo,
    cached_probe_is_current,
    managed_server_is_session_bound,
    probe_metadata,
    redact_mcp_error,
    redact_mcp_headers,
)
from kiro_crew.mcp_gateway import hazards, is_gateway_supported, launch_resolve
from kiro_crew.mcp_gateway.hashing import hash_command
from kiro_crew.mcp_gateway.launch_approval import (
    approve,
    display_launches,
    load_approvals,
    refused_servers,
    refused_shares,
    restore,
    revoke,
    target_stem,
)
from kiro_crew.mcp_gateway.rewriter import records_dir
from kiro_crew.mcp_gateway.shareability import ShareEvidence, ShareVerdict, assess
from kiro_crew.mcp_gateway.verdict_cache import load_cache
from kiro_crew.mcp_hot_reload import (  # noqa: F401 - live_sessions_hot_reload is a public name of this facade
    live_sessions_apply_mcp_live,
    live_sessions_hot_reload,
    mark_reinjecting_providers,
)
from kiro_crew.mcp_provenance import ABSENT, WITHHELD_KEY, resolve_write, stamp
from kiro_crew.mcp_utils import (
    INTERNAL_CLIENT_ID_KEY,
    INTERNAL_SCOPES_KEY,
    apply_kiro_oauth_hints,
    mcp_server_alias,
    without_mcp_refs,
)
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import process_shutdown_requested
from kiro_crew.user_json import (
    has_json_comments,
    loads_mcp_config,
    loads_user_json,
    loads_user_jsonc,
)

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


# Max per-request changes accepted by /api/mcp/apply. The capability-manager
# uninstalls run OFF the MCP file lock (deferred, bounded-concurrent, under one
# phase deadline), so the cap bounds request latency and memory (result list
# size) for a single apply call rather than lock-hold time. The
# dashboard applies at most one change per visible server, so this is generous.
_MCP_APPLY_MAX_CHANGES = 200

# Byte ceiling for one /api/mcp/apply body. The change count above bounds
# cardinality but runs only AFTER the body is decoded, so the read itself
# needs a pre-decode ceiling. Each change is scope flags plus a per-tool
# toolOverrides map; 1 MB comfortably covers _MCP_APPLY_MAX_CHANGES changes
# over even a tool-heavy server while still refusing an arbitrarily large
# body before it is buffered.
_MCP_APPLY_MAX_BODY_BYTES = 1024 * 1024


# Bounded concurrency for the deferred capability-manager uninstall phase, so a
# large batch neither serializes (timeout×N) nor floods the companion with N
# simultaneous subprocesses.
_MCP_DEFERRED_UNINSTALL_CONCURRENCY = 8

# Hard ceiling for the whole uninstall phase's deadline. The budget scales with
# the number of concurrency waves (so a legitimate multi-wave batch isn't
# cancelled mid-wave) but never exceeds this, bounding request latency.
_MCP_DEFERRED_UNINSTALL_MAX_BUDGET = 300


class _McpFileLock:
    """Async context manager wrapping a cross-platform file lock for mcp.json.

    Both failure modes are REPORTED here before they propagate, mirroring
    :func:`kiro_crew.agent.agents_spec_lock`, because several callers treat
    this lock as best-effort work and swallow the refusal at a level no
    operator reads. Without a report at WARNING a gateway that skipped its
    mcp.json write reads in the log exactly like one that completed it. An
    unwritable lock path (a read-only ``~/.kiro/settings`` mount, or a sidecar
    whose own mode denies write) refuses BEFORE any lock is attempted;
    ``platform_compat.acquire_lock`` bounds the acquire itself, so neither
    failure mode can present as a hang. Both reports cover the setup and the
    acquire alone -- the caller's body runs only after ``__aenter__`` returns,
    so a caller-body error can never be mislabelled as a lock failure.
    """

    async def __aenter__(self) -> None:
        try:
            _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
            _MCP_LOCK_PATH.touch(exist_ok=True)
            # Open the lock fd WRITABLE and non-truncating. Windows msvcrt.locking()
            # requires write access on the handle -- an "r" fd fails with EACCES and
            # platform_compat.acquire_lock swallows that (best-effort semantics),
            # silently degrading this to a no-op and letting concurrent
            # /api/mcp/toggle requests race the atomic-rename write of mcp.json
            # (one flip is lost). "r+" keeps the shared file present (no truncate);
            # see platform_compat.open_lock_file for the full Windows rationale
            # (GH-9248). Kept inline rather than routed through that helper: the fd
            # is stored on self._fd and released in __aexit__, so it must OUTLIVE
            # this method -- the with-scoped helper would close it at method return.
            fd = open(_MCP_LOCK_PATH, "r+")
        except OSError as exc:
            # Naming the path AND the errno is the point: "Read-only file
            # system" on this specific path is what tells the operator what to
            # change, and it is not something retrying can recover. No
            # KIRO_HOME remedy: _GLOBAL_MCP_JSON resolves from a fixed
            # Path.home() that ignores KIRO_HOME, so moving it cannot move
            # this lock -- naming the config the lock guards is what stays
            # true.
            logger.warning(
                "cannot open the mcp config lock %s (%s) -- writes to %s cannot be "
                "serialized, so this update is being skipped",
                _MCP_LOCK_PATH,
                exc.strerror or exc,
                _GLOBAL_MCP_JSON,
            )
            raise
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
        except BaseException as exc:
            fd.close()
            # Report the lock's OWN refusal only: acquire_lock fails closed
            # with an OSError, at once for a real fd defect or past its
            # bounded ceiling for a stuck holder. A CancelledError while
            # pending or an executor-shutdown RuntimeError is this caller's
            # lifecycle, not a lock failure, and reporting it would send an
            # operator after a holder that does not exist. A stuck holder also
            # calls for a DIFFERENT operator action (find the process still
            # holding the lock) than an unwritable path, so this carries no
            # remedy. No BlockingIOError case: this acquire is always a
            # WAITING one, so a refusal here is never a caller's own "do not
            # wait" choice.
            if isinstance(exc, OSError):
                logger.warning("mcp config lock %s: %s", _MCP_LOCK_PATH, exc)
            raise
        self._fd = fd

    async def __aexit__(self, *args: Any) -> None:
        try:
            platform_compat.release_lock(self._fd.fileno())
        finally:
            self._fd.close()


def _get_mcp_lock() -> _McpFileLock:
    """Return an MCP config file lock (compatible with bridges_mcp_config.py)."""
    return _McpFileLock()


# ── Process-wide /api/mcp/apply mutex ───────────────────────────────────
# The file lock (`_get_mcp_lock`) serializes individual filesystem WRITES, but
# an apply is a two-phase TRANSACTION (Phase 1: companion `uninstall_mcp` off the
# lock; Phase 2: config writes under the lock). Two concurrent applies can
# interleave across that boundary — A uninstalls a package and removes its
# config, B then re-adds the same server from a preserved spec — leaving config
# pointing at a removed package. This coarse async mutex spans BOTH phases so
# apply calls are fully serialized; the narrower file lock is retained inside for
# cross-process coordination with bridges_mcp_config.py. Loop-bound via the shared
# LoopBoundLock.
_apply_lock = LoopBoundLock()


def _get_apply_lock() -> LoopBoundLock:
    """Return the /api/mcp/apply mutex (loop-bound; rebinds per running loop)."""
    return _apply_lock


def _write_mcp_json(data: dict) -> None:
    """Atomically write global mcp.json to prevent partial reads.

    Raises :class:`McpConfigHasComments` when the file on disk holds JSONC.
    """
    _refuse_commented_config(_GLOBAL_MCP_JSON)
    _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(_GLOBAL_MCP_JSON, data)


async def api_mcp_sync(request: web.Request) -> web.Response:
    """POST /api/mcp/sync — apply MCP config changes to the running sessions.

    1. Discovers new MCP servers from mcp.json sources.
    2. Adds them to both kirocrew agent config AND global mcp.json.
    3. Makes the change reach the sessions. When every live process reconciles
       the agent file itself (:mod:`kiro_crew.mcp_hot_reload`) the write has
       already been picked up — no session is touched and the response reports
       ``sessions_reset: 0``. Otherwise every session and the warm pool are
       reset so the next message cold-starts on the new file.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_sync")
    if denied is not None:
        return denied
    from kiro_crew.mcp_discovery import (  # noqa: F811
        kirocrew_managed_names,
        sync_discovered_servers,
    )

    # Ownership BEFORE the first mutation: the discover pass below writes the
    # agent config itself, so a refusal reached inside it would leave this
    # request half applied.
    not_owned = await asyncio.to_thread(_agent_home_not_owned, "servers")
    if not_owned is not None:
        return not_owned
    # Refuse before ANY write: the global file is rewritten below as plain JSON.
    await asyncio.to_thread(_refuse_commented_config, _GLOBAL_MCP_JSON)

    # One serialized discover→write pass (agent config + CC sidecar), off the
    # event loop — the sync is blocking file I/O, and sync_discovered_servers'
    # mutex is what keeps this handler and the sessions-restart pre-sync from
    # interleaving their read-modify-writes of the same files.
    to_sync = await asyncio.to_thread(sync_discovered_servers)
    synced = len(to_sync)
    if to_sync:
        # Also add to global mcp.json (what ACP actually reads)
        async with _get_mcp_lock():
            try:
                gdata = loads_user_json(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                gdata = {"mcpServers": {}}
            gservers = gdata.setdefault("mcpServers", {})
            # The kiro-global mcp.json is NOT ours, and a name cannot say who
            # wrote an entry in it: the minimal ``{"url": ...}`` this emitter
            # produces for a hint-less managed server is also the smallest thing a
            # user can hand-type. Ownership is therefore the marker on the entry --
            # ``resolve_write`` rewrites what we provably wrote, and declines
            # everything else so a hand-authored collision survives untouched. A
            # present unmarked entry is never written, not even when its bytes
            # already match this emit: that state is also what reclaiming an entry
            # leaves behind, so stamping it would take the entry back.
            _managed = kirocrew_managed_names()
            for s in to_sync:
                if s.is_remote:
                    current = gservers.get(s.name)
                    entry: dict[str, Any] = dict(current) if isinstance(current, dict) else {}
                    for key in ("command", "args", "env", "type"):
                        entry.pop(key, None)
                    entry["url"] = s.url
                    # A headers map is scoped to the HOST it was typed for. The
                    # overlay below deliberately carries a credential the user put
                    # in their own global file across a sync -- but that copy was
                    # issued by, and for, the url already on disk. When the store
                    # moves a managed server to a different url, keeping the map
                    # stops being preservation and becomes forwarding: the user's
                    # Authorization value gets written beside an origin that never
                    # saw it, and the next session sends it there. So the map only
                    # survives when the url is unchanged, and the test sits ahead
                    # of BOTH write branches -- a header-less discovery never runs
                    # the overlay, so a guard inside it would miss the entry that
                    # simply rode along in ``dict(current)``. Dropping costs a
                    # re-auth, which the user can redo; a leaked bearer cannot be
                    # called back. An entry with no url on disk is treated the same
                    # way: nothing proves that map belongs to where we are pointing.
                    if not (isinstance(current, dict) and current.get("url") == s.url):
                        entry.pop("headers", None)
                    # Discovery coerces only a FALSY headers value, so a truthy
                    # non-dict from a hand-edited entry arrives here as-is. It
                    # carries no usable credential, so it reads as "discovery
                    # said nothing" and leaves the on-disk map alone rather than
                    # aborting a sync that covers every other server too.
                    _discovered = s.headers if isinstance(s.headers, dict) else {}
                    if _discovered:
                        # Overlay, never replace. Discovery reads the MERGED view,
                        # which takes the store's copy of the entry, so a header
                        # the user hand-added to their own global file is absent
                        # from the discovered map while still present in
                        # ``current``. Assigning wholesale would delete exactly the
                        # global-only credential the empty-map guard below exists
                        # to protect -- and this write path only became reachable
                        # for existing entries here, so it has to be as
                        # conservative as the base ref's create-only behaviour.
                        _prior = entry.get("headers")
                        entry["headers"] = {
                            **(_prior if isinstance(_prior, dict) else {}),
                            **_discovered,
                        }
                    # An empty discovered header map is NOT an instruction to
                    # delete. Discovery reads the dashboard's own mcp.json, so a
                    # server of the same name carrying an Authorization header in
                    # the user's global ~/.kiro/settings/mcp.json legitimately
                    # arrives here header-less. Popping on that would erase the
                    # only copy of a credential the user typed, silently and with
                    # nothing to restore it from.
                    #
                    # scopes/clientId below are handled the opposite way ON
                    # PURPOSE: those are written only by flows that own the whole
                    # OAuth request, so absent means the request was narrowed and
                    # the old grant must stop being asked for. Getting a stale
                    # scope wrong over-requests access; getting a header wrong
                    # destroys it.
                    #
                    # Both hints therefore always SPEAK here -- an empty list or
                    # id is a real "no longer requested", not silence -- while
                    # ``apply_kiro_oauth_hints`` edits ``oauth`` surgically, so a
                    # sub-key we do not own (``issuer``, ...) is not collateral.
                    entry.pop(INTERNAL_SCOPES_KEY, None)
                    entry.pop(INTERNAL_CLIENT_ID_KEY, None)
                    entry = apply_kiro_oauth_hints(
                        entry,
                        scopes=list(s.scopes),
                        client_id=s.client_id,
                        server=s.name,
                    )
                    resolved = resolve_write(
                        name=s.name,
                        # ``ABSENT``, not ``current``: a hand-edited file can hold
                        # ``null`` or a string under a name, and that value
                        # occupies the name -- it cannot carry a marker, so it is
                        # the user's, not a free slot to create into.
                        on_disk=gservers.get(s.name, ABSENT),
                        candidate=entry,
                        store_managed=s.name in _managed,
                        surface=_KIRO_GLOBAL_SURFACE,
                    )
                    if resolved is not None and current != resolved:
                        gservers[s.name] = resolved
                elif s.name not in gservers:
                    entry = {"command": s.command}
                    if s.args:
                        entry["args"] = s.args
                    if s.env:
                        # This file is consumed directly by the ACP runtime,
                        # which applies a declared env per key — emit through
                        # the shared normalization point (env.emit_env) so a
                        # declared PATH is complete. Create-only: an entry the
                        # user authored here is never rewritten, so their text
                        # stays theirs. Off the event loop: the PATH expansion
                        # scans Node-manager directories on a cold cache.
                        entry["env"] = await asyncio.to_thread(emit_env, s.env)
                    # Create-only here, as on the base ref, so there is no rewrite
                    # to gate -- but the entry is still ours, and marking it now is
                    # what lets a later slice re-sync it without guessing.
                    gservers[s.name] = stamp(entry) if s.name in _managed else entry
            _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
            _write_mcp_json(gdata)

            # The registry write, the spec sync and the rollback all under the
            # SAME mcp lock. The rollback rewrites the WHOLE registry file, so
            # releasing the lock first would let another request commit into
            # the window and have this stale snapshot erase that commit
            # silently. Order matches the toggle paths: mcp lock outside,
            # config lock in.
            #
            # Ensure newly-synced servers are added to tools/allowedTools so
            # the AI can actually use them (not just see them in mcpServers).
            from kiro_crew.dashboard.handlers.agents import (
                _get_config_lock,  # circular import: agents imports mcp
            )

            async with _get_config_lock():
                try:
                    await asyncio.to_thread(
                        _sync_mcp_to_agent_batch, [s.name for s in to_sync], enabled=True
                    )
                except SharedAgentHomeRefused:
                    return _agent_home_not_owned_response(
                        ", ".join(s.name for s in to_sync) or "servers"
                    )

    # The reset exists only to make kiro-cli re-read a file it may already
    # watch. Runs even with no new servers: an enable/disable toggle also wrote
    # kirocrew.json, and on a harness without live reconcile only a restart
    # applies it. Skipped only when EVERY process the reset would touch has
    # shown it reconciles on its own; ``sessions_reset: 0`` is then the
    # observable outcome.
    if _mcp_hot_reload_active(request):
        sessions_reset = 0
    else:
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
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_toggle")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    name, err = _string_identifier(body, "name")
    if err is not None:
        return err
    enabled = body.get("enabled", True)
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)

    # Ownership BEFORE the first mutation: the disabled flag lands in the
    # user-level registry ahead of the rendered spec, so a refusal found at
    # that last write shows the row off while the spec still mounts it.
    not_owned = await asyncio.to_thread(_agent_home_not_owned, name)
    if not_owned is not None:
        return not_owned

    async with _get_mcp_lock():
        _refuse_commented_config(_GLOBAL_MCP_JSON)
        # 1. Update global mcp.json
        try:
            data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return _mcp_config_corrupt()

        servers = data.setdefault("mcpServers", {})
        # By the ONE key the row stands for -- raw or alias: the table asks for
        # ``playwright-mcp``, the file may hold ``npm:@playwright/mcp``. A stub
        # written beside the raw entry would show the row Disabled while the
        # rebuild kept mounting the entry the flag never reached. A name that is
        # the alias of several raw keys is refused rather than guessed: the flag
        # would land on an entry the user did not name, or on a stub none of
        # them reads.
        try:
            key = _config_key_for(servers, name)
        except AmbiguousServerKey as exc:
            return web.json_response(
                {"error": str(exc), "code": "mcp_server_name_ambiguous"}, status=409
            )
        if key is None:
            # Server may exist in another scope (agent config, ~/.claude.json).
            # Create a stub so we can store disabled state here.
            from kiro_crew.mcp_discovery import (
                list_servers as _ls,  # circular import: mcp_discovery defers imports of kiro_crew.agent which shares state with this module
            )

            known = {s.name for s in _ls()}
            if name not in known:
                return _mcp_server_not_found(name)
            servers[name] = {}
            key = name

        spec = servers[key]
        if not isinstance(spec, dict):
            if isinstance(spec, str):
                servers[key] = spec = {"command": spec}
            else:
                return _mcp_spec_invalid(name, spec)
        if enabled:
            spec.pop("disabled", None)
        else:
            spec["disabled"] = True

        try:
            _write_mcp_json(data)
        except Exception:
            return _mcp_write_failed("mcp toggle")

        # 2. Sync to kirocrew.json tools/allowedTools (lock prevents lost updates vs agents.py)
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            try:
                await asyncio.to_thread(_sync_mcp_to_agent, name, enabled)
            except SharedAgentHomeRefused:
                # Ownership flipped after the preflight, so the flag above
                # landed in a registry whose spec will not follow it.
                return _agent_home_not_owned_response(name)

    return web.json_response({"ok": True, "name": name, "enabled": enabled, "applied": True})


async def api_mcp_toggle_tool(request: web.Request) -> web.Response:
    """POST /api/mcp/toggle-tool — enable or disable a specific tool in an MCP server.

    Updates ``disabledTools`` in ``~/.kiro/settings/mcp.json``.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_toggle_tool")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    server, err = _string_identifier(body, "server")
    if err is not None:
        return err
    tool, err = _string_identifier(body, "tool")
    if err is not None:
        return err
    enabled = body.get("enabled", True)
    if not server or not tool:
        return web.json_response(
            {"error": "server and tool are required", "code": "server_and_tool_required"},
            status=400,
        )

    async with _get_mcp_lock():
        _refuse_commented_config(_GLOBAL_MCP_JSON)
        try:
            data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return _mcp_config_corrupt()

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
                return _mcp_server_not_found(server)
            servers[server] = {}

        spec = servers[server]
        if not isinstance(spec, dict):
            if isinstance(spec, str):
                servers[server] = spec = {"command": spec}
            else:
                return _mcp_spec_invalid(server, spec)
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
        except Exception:
            return _mcp_write_failed("mcp toggle-tool")
    return web.json_response({"ok": True, "server": server, "tool": tool, "enabled": enabled})


async def api_mcp_toggle_all(request: web.Request) -> web.Response:
    """POST /api/mcp/toggle-all — enable or disable all MCP servers."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_toggle_all")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    enabled = body.get("enabled", True)

    # Ownership BEFORE the first mutation: every flag lands in the user-level
    # registry ahead of the rendered spec, so a refusal found at that last
    # write shows every row flipped while the spec is unchanged.
    not_owned = await asyncio.to_thread(_agent_home_not_owned, "servers")
    if not_owned is not None:
        return not_owned

    async with _get_mcp_lock():
        _refuse_commented_config(_GLOBAL_MCP_JSON)
        try:
            data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"mcpServers": {}}
        except json.JSONDecodeError:
            return _mcp_config_corrupt()

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
        except Exception:
            return _mcp_write_failed("mcp toggle-all")

        # Batch sync: single read-modify-write of kirocrew.json
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        async with _get_config_lock():
            try:
                await asyncio.to_thread(_sync_mcp_to_agent_batch, toggled, enabled)
            except SharedAgentHomeRefused:
                # Ownership flipped after the preflight, so the registry
                # change above landed with no spec change to match it.
                return _agent_home_not_owned_response(", ".join(toggled) or "servers")

    return web.json_response({"ok": True, "enabled": enabled, "count": len(servers)})


async def api_mcp_remove(request: web.Request) -> web.Response:
    """POST /api/mcp/remove — uninstall an MCP server.

    Removes the server from ``~/.kiro/settings/mcp.json`` and syncs
    kirocrew.json.  If the optional ``aim`` package manager happens to be
    on PATH it is also asked to uninstall (best-effort); on a vanilla
    machine ``aim`` is absent and that step is skipped gracefully.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_remove")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    name, err = _string_identifier(body, "name")
    if err is not None:
        return err
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)

    # Ownership BEFORE the first mutation: this handler writes the
    # user-level registry (and, on remove, asks the companion package
    # manager) ahead of the rendered spec, so a refusal found at that last
    # write leaves the two disagreeing with no path back.
    not_owned = await asyncio.to_thread(_agent_home_not_owned, name)
    if not_owned is not None:
        return not_owned

    logger.info("MCP remove: %s", name)

    # Optional capability-manager uninstall (best-effort). The edition's
    # capability manager owns the actual uninstall; only attempt when the seam
    # reports it available so the handler stays fully functional without it.
    from kiro_crew.dashboard.handlers._shared import _capability_manager

    # Before the uninstall, so a refused global file leaves nothing half done.
    await asyncio.to_thread(_refuse_commented_config, _GLOBAL_MCP_JSON)
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
            data = loads_user_json(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
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
            try:
                await asyncio.to_thread(lambda: _sync_mcp_to_agent(name, False, remove=True))
            except SharedAgentHomeRefused:
                # Ownership flipped after the preflight: another process claimed
                # the specs between that check and this write. Answered as the
                # refusal it is, like every other write failure in this handler.
                return _agent_home_not_owned_response(name)

    await _forget_owner_servers([name])
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

    Two caller classes, two authorizations. ``/api/mcp/servers`` is listed in
    ``server._STRICT_INTERNAL_API_PATHS``, so its designed caller is an internal
    loopback process presenting ``X-Internal-Secret`` -- the App Kit SDK's
    ``register_mcp_server`` / ``remove_mcp_server``
    (``packages/kirocrew-client-py``) is exactly that. ``token_auth`` grants such a
    request and marks it ``internal_auth``, and it deliberately leaves
    ``request["app"]`` ABSENT for the person-behind-the-secret case, which
    ``is_owner_dashboard_request`` reads as not-the-owner. So the owner predicate
    applies to the COOKIE caller only: a ``local_only=False`` deployment
    reclassifies strict paths as mixed and a browser session can then arrive here,
    and that session must be the owner, because a PUT writes a command later agent
    sessions execute. Gating the internal caller too would answer 403 to the one
    caller the route exists for.
    """
    # Only authentication middleware publishes ``internal_auth``, and only on a
    # constant-time ``X-Internal-Secret`` match -- a request header claiming to
    # carry a secret is not evidence, so this cannot be spoofed by a browser.
    if request.get("internal_auth") is not True:
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        denied = await require_owner_dashboard_request(request, "mcp_server_detail")
        if denied is not None:
            return denied
    name = request.match_info["name"]
    if not name or not name.strip():
        return web.json_response(
            {"error": "server name is required", "code": "name_required"}, status=400
        )
    name = name.strip()

    # Ownership BEFORE the first mutation: this handler writes the
    # user-level registry (and, on remove, asks the companion package
    # manager) ahead of the rendered spec, so a refusal found at that last
    # write leaves the two disagreeing with no path back.
    not_owned = await asyncio.to_thread(_agent_home_not_owned, name)
    if not_owned is not None:
        return not_owned

    if request.method == "DELETE":
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # Remove from global mcp.json
        async with _get_mcp_lock():
            _refuse_commented_config(_GLOBAL_MCP_JSON)
            try:
                data = loads_user_json(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"mcpServers": {}}
            removed = data.get("mcpServers", {}).pop(name, None) is not None
            if removed:
                _write_mcp_json(data)

            # Order matches the toggle paths: mcp lock outside, config lock in.
            # Hold the config lock across the offloaded read-modify-write: the
            # sync runs in a worker thread, so without the lock two concurrent
            # DELETE/PUT requests would both read kirocrew.json and the last
            # write would drop the other's change.
            async with _get_config_lock():
                try:
                    await asyncio.to_thread(lambda: _sync_mcp_to_agent(name, False, remove=True))
                except SharedAgentHomeRefused:
                    return _agent_home_not_owned_response(name)
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_server_remove",
            outcome="completed" if removed else "not_found",
            resources=name,
        )
        if not removed:
            return web.json_response(
                {"ok": False, "name": name, "removed": False, "code": "mcp_server_not_found"},
                status=404,
            )
        return web.json_response({"ok": True, "name": name, "removed": True})

    # PUT — register or update
    #
    # Validate the name on the WRITE path only. ``_is_valid_mcp_name`` is the
    # predicate the validation-dependent readers enforce — several call sites in
    # this module, plus ``mcp_custom``, ``mcp_discover`` and ``connections`` — so
    # a key written outside it is filtered out by those and the entry cannot be
    # managed through them. The listing, toggle and remove paths do not apply it,
    # which is what keeps such an entry visible and clearable. Checked before the
    # body is parsed so a malformed name costs no further work.
    #
    # The removal paths stay permissive: DELETE above and ``api_mcp_remove`` both
    # accept a name this guard would reject, which is how a junk key — including
    # one written before this guard existed — can still be cleared. Guarding a
    # remover would strand exactly what the writer guard is meant to prevent.
    # Same writer-validates / remover-does-not split that ``security_trusted_apps.py``'s
    # trusted-app grant and revoke pair documents.
    if not _is_valid_mcp_name(name):
        return web.json_response(
            {
                "error": f"invalid server name '{name[:64]}'",
                "code": "invalid_server_name",
            },
            status=400,
        )

    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    command = body.get("command", "")
    if not command:
        return web.json_response(
            {"error": "command is required", "code": "command_required"}, status=400
        )

    entry: dict[str, Any] = {"command": command}
    if body.get("args"):
        entry["args"] = body["args"]
    if body.get("env"):
        # This file is consumed directly by the ACP runtime (declared env is
        # applied per key), and this caller is programmatic (App Kit), not a
        # user hand-authoring their own file — emit through the shared
        # normalization point so a declared PATH is complete. Off the event
        # loop: emit_env's PATH expansion scans Node-manager directories on a
        # cold cache. See env.emit_env.
        if isinstance(body["env"], dict):
            entry["env"] = await asyncio.to_thread(emit_env, body["env"])
        else:
            entry["env"] = body["env"]

    # Write to global mcp.json
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_mcp_lock():
        try:
            data = loads_user_json(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"mcpServers": {}}
        data.setdefault("mcpServers", {})[name] = entry
        _GLOBAL_MCP_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_mcp_json(data)

        # Order matches the toggle paths: mcp lock outside, config lock in.
        # Config lock across the offloaded write -- see the DELETE branch
        # above for why the thread hop needs it.
        async with _get_config_lock():
            try:
                await asyncio.to_thread(_sync_mcp_to_agent, name, True)
            except SharedAgentHomeRefused:
                return _agent_home_not_owned_response(name)

    logger.info("MCP register via REST: %s command=%s", name, command)
    sel().log_api_access(
        caller="dashboard",
        operation="mcp_server_register",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name}, status=200)


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.mcp_agent_sync import (  # noqa: F401
        _mark_agent_entries_disabled,
        _shared_agent_home_declined,
        _sync_mcp_to_agent_batch_unlocked,
        _sync_mcp_to_agent_unlocked,
    )
    from kiro_crew.dashboard.handlers.mcp_config_entries import (  # noqa: F401
        _get_kirocrew_entry,
        _remove_from_agent_file,
        _remove_kirocrew_entry,
        _replace_kirocrew_spec,
        _rmw_remove_entry,
    )
    from kiro_crew.dashboard.handlers.mcp_config_files import (  # noqa: F401
        _KIROCREW_MCP_JSON,
        _MAX_MCP_NAME_LEN,
        _VALID_MCP_NAME_RE,
        MCP_CONFIG_HAS_COMMENTS,
        McpConfigHasComments,
        _atomic_write,
        _config_entry_for,
        _get_mcp_lock_sync,
        _has_json_comments_on_disk,
        _load_json_or_empty,
        _McpFileLockSync,
        _resolve_key_from_read,
        api_mcp_global_scopes,
    )
    from kiro_crew.dashboard.handlers.mcp_config_purge import (  # noqa: F401
        _scrub_preregistered_oauth_copies,
        _uninstall_scope_files,
    )
    from kiro_crew.dashboard.handlers.mcp_gateway_routes import (  # noqa: F401
        _MCP_GATEWAY_APPLY_LOCK,
        _MCP_RESOLVE_REFRESH_LOCK,
        _assess_server,
        _collect_server_rows,
        _env_pooling_outcome,
        _freeze_stub_servers,
        _launch_specs_for,
        _load_shareability_state,
        _local_overlay_section,
        _overlay_shadowed_keys,
        api_mcp_gateway_enable,
        api_mcp_gateway_metrics,
        api_mcp_gateway_server_launch,
        api_mcp_gateway_servers,
        api_mcp_gateway_status,
        api_mcp_resolve_refresh,
    )
    from kiro_crew.dashboard.handlers.mcp_gateway_stubs import (  # noqa: F401
        _MAX_STUB_BATCH,
        _record_stub_decisions,
        _stub_eligibility,
        api_mcp_gateway_server_launches,
        api_mcp_gateway_set_stub,
    )
    from kiro_crew.dashboard.handlers.mcp_probe import (  # noqa: F401
        _MCP_PROBE_CACHE_SECS,
        _agent_mcp_server_names,
        _annotate_owner_sign_in,
        _annotate_quarantine,
        _arm_reprobe,
        _bg_mcp_probe,
        _bg_measure_all,
        _edited_since_probe,
        _evaluate_shareability,
        _invalid_flag_reason,
        _kirocrew_mcp_servers,
        _mcp_probe_cache,
        _mcp_probe_in_progress,
        _mcp_probe_task,
        _mcp_probe_ts,
        _mcp_reprobe_armed_for,
        _measure_progress,
        _probe_currency,
        _quarantine_verdicts,
        _read_global_mcps,
        _recheck_routed,
        _record_probe_verdicts,
        _run_mcp_probe,
        _stamp_config_state,
        api_mcp_active,
        api_mcp_measure_progress,
        api_mcp_measure_start,
        api_mcp_probe,
        api_mcp_probe_cached,
        api_mcp_quarantine_clear,
        api_mcp_servers,
    )


async def api_mcp_apply(request: web.Request) -> web.Response:
    """POST /api/mcp/apply — serialize the two-phase apply under the apply mutex.

    Thin wrapper: the apply is a Phase-1 (companion uninstall, off the file lock)
    + Phase-2 (config writes, under the file lock) TRANSACTION, and the file lock
    only serializes individual writes — not the phase boundary. Without a mutex
    two concurrent applies can interleave so one re-adds a server from a preserved
    spec after another removed its package, leaving config pointing at a removed
    package. ``_get_apply_lock`` (a process-wide async mutex spanning BOTH phases)
    closes that; the narrower file lock is retained inside ``_do_mcp_apply`` for
    cross-process coordination with bridges_mcp_config.py.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_apply")
    if denied is not None:
        return denied
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
    body, body_err = await read_bounded_json(request, max_bytes=_MCP_APPLY_MAX_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    changes = body.get("changes")
    if not isinstance(changes, list):
        return web.json_response(
            {"error": "changes must be a list", "code": "invalid_changes"}, status=400
        )
    if len(changes) > _MCP_APPLY_MAX_CHANGES:
        return web.json_response(
            {
                "error": f"too many changes (max {_MCP_APPLY_MAX_CHANGES})",
                "code": "too_many_changes",
            },
            status=400,
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
    #      the lock and is last-writer-wins (TOCTOU fix).
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

    # ── Phase 0: read-only preflight of every change, before any side effect ──
    # Each scope is read once and every change's name resolved against it. A
    # name that is the alias of several raw keys (``team/foo`` beside
    # ``other:team/foo``, none keyed ``team-foo``) has no one entry to write and
    # is refused as a request failure the table shows while keeping the pending
    # change -- the only refusal an apply makes, taken before the companion
    # package is gone and before the first scope write. Nothing is logged as
    # applied. Presence is NOT carried forward from here: Phase 2's writers are
    # each one locked read-modify-write that decides from the read they write
    # back; the preflight's resolved key travels only as their tiebreaker.
    change_names = sorted(
        {
            n
            for c in changes
            if isinstance(c, dict)
            and (n := str(c.get("name", "")).strip())
            and _is_valid_mcp_name(n)
        }
    )
    change_keys: dict[str, PreflightKeys] = {}
    if change_names:
        # Ownership first, because an apply's own ordering puts the companion
        # package removal and every scope write ahead of the rendered spec: a
        # refusal discovered at that last write would leave the server purged
        # from all its sources and still mounted by the spec.
        not_owned = await asyncio.to_thread(_agent_home_not_owned, ", ".join(change_names))
        if not_owned is not None:
            return not_owned
        # A scope file holding JSONC cannot be written without dropping its
        # comments, so the batch is refused before its first write.
        await asyncio.to_thread(_refuse_commented_config, *_user_mcp_config_paths())
        change_keys, ambiguous = await asyncio.to_thread(_preflight_change_keys, change_names)
        if ambiguous:
            sel().log_api_access(
                caller="dashboard",
                operation="mcp_apply_rejected_ambiguous",
                outcome="denied",
                resources=",".join(sorted(ambiguous))[:64],
            )
            detail = "; ".join(
                f"{name!r} is the alias of several entries in {', '.join(scopes)} and none is "
                f"keyed {name!r}"
                for name, scopes in sorted(ambiguous.items())
            )
            return web.json_response(
                {
                    "error": f"apply refused: {detail}; edit the file directly",
                    "code": "mcp_server_name_ambiguous",
                    "ambiguous": ambiguous,
                },
                status=409,
            )
    uninstall_keys = {n: change_keys[n] for n in uninstall_names if n in change_keys}

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
                    # file idempotently -- exactly the entries Phase 0 pinned, so
                    # nothing is resolved (or refused) after the package is gone.
                    try:
                        outcome["actions"].update(
                            await _offload_config_write(
                                _purge_server_config, name, preferred=uninstall_keys.get(name, {})
                            )
                        )
                    except SharedAgentHomeRefused:
                        # Ownership was claimed after Phase 0's preflight. The
                        # purge strips the scopes FIRST and the rendered spec
                        # last, so the sources are already gone while the spec
                        # still mounts the server with its autoApprove -- and
                        # nothing reconciles it, because this instance's own
                        # rebuild is declined too. Recorded as purged so the
                        # guaranteed-cleanup sweep does not re-attempt the same
                        # refused write, then reported as the partial state it
                        # is rather than surfacing as a 500.
                        purged_names.add(name)
                        return _agent_home_not_owned_response(name)
                    purged_names.add(name)
                    await _forget_owner_servers([name])
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
                keys = change_keys.get(name, {})
                outcome["actions"]["kirocrew"] = await _offload_config_write(
                    _set_kirocrew_entry,
                    name,
                    enabled=desired_mc,
                    spec=preserved_spec,
                    preferred=keys.get(SCOPE_KIROCREW),
                )

                # Apply Kiro and CC (add/remove from their respective globals).
                # Resolve the spec ONCE before any scope mutation — otherwise
                # the kiro removal can vacate the only source that had the
                # spec, and the CC add would get "missing_spec" even though
                # the user clearly intended it to move over.
                resolved_spec = _find_server_spec_anywhere(name)
                outcome["actions"]["kiroGlobal"] = await _offload_config_write(
                    _set_scope_entry,
                    _GLOBAL_MCP_JSON,
                    name,
                    enabled=desired_kiro,
                    spec=resolved_spec,
                    preferred=keys.get(SCOPE_KIRO_GLOBAL),
                )
                for scope in extra_scopes:
                    outcome["actions"][f"{scope.id}Global"] = await _offload_config_write(
                        _set_scope_entry,
                        scope.global_json,
                        name,
                        enabled=desired_extra[scope.id],
                        spec=resolved_spec,
                        preferred=keys.get(f"{scope.id}Global"),
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
                        changed_tools = await _offload_config_write(
                            _set_tool_overrides, name, sanitized
                        )
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
            uninstall_keys,
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
        _spec_path, rebuild_ok = await asyncio.to_thread(rebuild_agent_config_reporting)
        if not rebuild_ok:
            # Declined, not failed: this instance may not write the shared agent
            # home, so the saved change does not reach new sessions.
            rebuild_error = AGENT_SPEC_NOT_REWRITTEN
            logger.warning("agent spec was not rewritten after apply: %s", rebuild_error)
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


# This module keeps the mcp.json file lock whose blocking open the sync-io fs
# baseline counts against this path, and the routes that take that lock: sync,
# toggle, remove, server detail and apply. It is also the facade of the MCP server
# management owners: every other name is reached through it as
# ``kiro_crew.dashboard.handlers.mcp.<name>``, a read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module, and each imports only the owners listed before it in ``_OWNERS``. New work
# goes to the owner of its responsibility:
#
#   ``mcp_config_files`` -- Config paths, name checks, JSONC refusal, atomic writes, key lookup.
#   ``mcp_config_entries`` -- Reading and editing one server's entry in each config scope.
#   ``mcp_config_purge`` -- Removing a server's config everywhere when its package is uninstalled.
#   ``mcp_agent_sync`` -- Copying servers into the Kiro Crew agent spec; the agent-home refusal.
#   ``mcp_probe`` -- The probe cache, quarantine, shareability measurement and the listing routes.
#   ``mcp_gateway_routes`` -- The shared gateway's status, enable, refresh and per-server routes.
#   ``mcp_gateway_stubs`` -- Which servers may be stubbed, the set-stub route, its launch preview.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.mcp_config_files",
    "kiro_crew.dashboard.handlers.mcp_config_entries",
    "kiro_crew.dashboard.handlers.mcp_config_purge",
    "kiro_crew.dashboard.handlers.mcp_agent_sync",
    "kiro_crew.dashboard.handlers.mcp_probe",
    "kiro_crew.dashboard.handlers.mcp_gateway_routes",
    "kiro_crew.dashboard.handlers.mcp_gateway_stubs",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
