"""The shared MCP gateway's control routes.

Status and metrics, the enable toggle with its local overlay and stub freeze, resolve
refresh, and the per-server rows with their shareability assessment and launch.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection
from types import SimpleNamespace
from typing import Any

from aiohttp import web

from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.config.loader import KiroCrewConfig, _resolve_stub_roster, read_config_text
from kiro_crew.dashboard.chat_utils import run_to_completion
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.mcp_config_files import _is_valid_mcp_name, _offload_config_write
from kiro_crew.dashboard.handlers.mcp_probe import _probe_currency
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.mcp_discovery import managed_server_is_session_bound, probe_metadata
from kiro_crew.mcp_gateway import hazards, is_gateway_supported, launch_resolve
from kiro_crew.mcp_gateway.hashing import hash_command
from kiro_crew.mcp_gateway.launch_approval import (
    display_launches,
    refused_servers,
    refused_shares,
)
from kiro_crew.mcp_gateway.rewriter import records_dir
from kiro_crew.mcp_gateway.shareability import ShareEvidence, ShareVerdict, assess
from kiro_crew.mcp_gateway.verdict_cache import load_cache
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


# ─── Shared MCP gateway enable toggle ───────────────────────────────────


async def api_mcp_gateway_status(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/status — MCP gateway state.

    ``enabled`` is the persisted backend-sharing flag; ``running``/``ping_ok``
    reflect the live broker held by the gateway orchestrator. The broker runs iff
    something is stubbed, so ``stub_count == 0`` with ``running=false`` is the
    default install, not a fault. A freshly-flipped flag reads its new value with
    ``running`` still stale until the restart lands.
    """
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    state: DashboardState = request.app["state"]
    manager = getattr(state, "_mcp_gateway_manager", None)
    running = manager is not None and manager.is_running
    ping_ok = manager is not None and running and await manager.ping()
    cfg = KiroCrewConfig.load().mcp_gateway
    launch_refused = await asyncio.to_thread(refused_servers)
    return web.json_response(
        {
            "enabled": cfg.enabled,
            # The stub set is what the sharing switch acts on, so the UI needs
            # it to say what turning sharing on will affect. Sent as a count and
            # a list: the count drives the header line, the list drives each
            # row's own control without a second request.
            "stub": sorted(cfg.stub_servers),
            "stub_count": len(cfg.stub_servers),
            # Stubbed servers whose launch the last rewrite refused to run
            # outside the sandbox. Each resolved argv is paired with its
            # redacted declared environment for an informed re-approval.
            "launch_refused": launch_refused,
            # Share-list names (as configured) whose launch is refused, with the
            # reason. Matched here rather than in the client because a refusal is
            # recorded under the rewriter's alias for a slash-named server.
            "share_refused": refused_shares(cfg.stub_servers, launch_refused),
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


# Serializes in-process gateway apply operations (enable/disable + set-stub)
# so two concurrent dashboard requests cannot interleave broker start/stop and
# orphan a gatewayd process. The config write is guarded by _get_config_lock();
# this lock guards the apply() side effect that runs AFTER that lock is released.
_MCP_GATEWAY_APPLY_LOCK = LoopBoundLock()


def _local_overlay_section() -> dict:
    """Return ``mcp_gateway`` from ``config.local.json``, or ``{}``.

    That file is USER-OWNED and deep-merged OVER ``config.json`` (see
    ``KiroCrewConfig.load``), so ``config.json`` alone is not the effective
    config. Any read-modify-write on the base file has to account for it or it
    reasons about a view the runtime never sees.
    """
    from kiro_crew.config.loader import config_local_path

    path = config_local_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, json.JSONDecodeError):
        # Unreadable overlay: treat as absent. The loader logs and ignores it
        # too, so behaving otherwise here would diverge from the runtime.
        return {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("mcp_gateway")
    return section if isinstance(section, dict) else {}


def _overlay_shadowed_keys(overlay: dict, keys: Collection[str]) -> list[str]:
    """Which of *keys* the local overlay defines — i.e. writes that cannot land.

    The overlay wins the deep merge for every key it defines, so writing such a
    key into ``config.json`` changes nothing the runtime will read. Reporting
    that write as applied is the same class of lie as a 200 with
    ``applied: false``: the switch looks live and governs nothing.
    """
    return sorted(k for k in keys if k in overlay)


def _freeze_stub_servers(section: dict, overlay: dict | None = None) -> None:
    """Materialize the resolved stub set into ``stub_servers``. Call BEFORE any
    other mutation of *section*.

    Resolves from the MERGED effective view (base + ``config.local.json``), not
    from *section* alone: a legacy allowlist commonly lives in the user-owned
    overlay, and freezing from the base would write an EMPTY ``stub_servers``.
    Because key PRESENCE wins in ``_resolve_stub_servers``, that empty base value
    then beats the overlay's allowlist and silently unstubs servers the operator
    never touched. The frozen value is still written to the BASE section — the
    caller owns ``config.json`` — but it is computed from what the runtime reads.

    ``_resolve_stub_servers`` is deliberately conditional on ``enabled``: a legacy
    config carrying ``poolable_servers`` with ``enabled: false`` must resolve to an
    EMPTY stub set, so an upgrade never invents a daemon for an install whose
    gateway was off. The cost of that correctness is that the resolved value is
    UNSTABLE across a change to ``enabled`` — so a writer that leaves the file
    still riding the deprecated alias hands the NEXT read a different stub set
    than the operator was looking at when they clicked.

    Both directions were reachable through the sharing toggle alone:

    * ON, from ``enabled:false, poolable_servers:[X]`` — the page truthfully says
      "0 stubbed", and one click on *sharing* would stub every alias entry and
      share it, the unrequested-topology change this design exists to make opt-in.
    * OFF, from ``enabled:true, poolable_servers:[X]`` — the alias stops firing and
      the stub set empties, so "stubbed but private" becomes unreachable for
      exactly the migrated operator, and turning sharing off does more than narrow.

    Freezing on every write closes both: afterwards the file always carries an
    explicit ``stub_servers``, key presence wins in the resolver, and ``enabled``
    goes back to meaning only "share these backends". Ordering is load-bearing —
    freezing after ``enabled`` had been reassigned would resolve against the NEW
    value and bake in the very set this prevents.

    Absent-key test, not truthiness: an operator who wrote ``stub_servers: []``
    chose to stub nothing, and overwriting that from a stale ``poolable_servers``
    would re-stub servers they had just cleared.

    Deduplicates: the resolver preserves whatever the file held, and a
    ``poolable_servers`` carrying the same name twice would otherwise be frozen
    with the duplicate and make the dashboard's ``stub_count`` overcount.
    """
    if "stub_servers" not in section:
        effective = dict(section)
        effective.update(overlay or {})
        # The ROSTER resolver, not the effective one: this materializes the base
        # layer, and folding ``stub_overrides`` in here would write the operator's
        # deviations INTO the roster -- making them indistinguishable from a
        # shipped name and pinning them against every later roster change, which
        # is the shadowing the override map exists to prevent.
        section["stub_servers"] = sorted(set(_resolve_stub_roster(effective)))


#: Serializes explicit pre-resolve refreshes. Installs are registry-bound and
#: slow, so two overlapping presses would double the network work and race each
#: other's atomic commits. Deliberately NOT the gateway apply lock: a refresh
#: must not block an operator toggling sharing while it runs.
_MCP_RESOLVE_REFRESH_LOCK = LoopBoundLock()


async def api_mcp_resolve_refresh(request: web.Request) -> web.Response:
    """POST /api/mcp-gateway/resolve-refresh -- re-resolve npm MCP targets now.

    Pre-resolving lets a launch exec an already-installed tree, so session start
    performs no dependency resolution. An unpinned spec is refreshed on a timer;
    this is the operator asking for that check immediately, so it forces past the
    freshness window.

    Returns ``{ok, resolved, ready}`` where ``resolved`` maps each npm package to
    ``ready`` / ``unresolved`` / ``error``. A server that fails to resolve is not
    an error for the request: it simply keeps launching the way it does today.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_resolve_refresh")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    refresh = getattr(state, "_mcp_resolve_refresh", None)
    if refresh is None:
        return web.json_response(
            {
                "error": "Pre-resolve is not available in this process.",
                "code": "resolve_refresh_unavailable",
            },
            status=503,
        )
    if _MCP_RESOLVE_REFRESH_LOCK.locked():
        # Report the in-flight pass instead of queueing behind it: the caller is
        # a person who pressed a button, and a silent multi-minute wait reads as
        # a hang.
        return web.json_response(
            {"error": "A pre-resolve pass is already running.", "code": "resolve_in_progress"},
            status=409,
        )
    async with _MCP_RESOLVE_REFRESH_LOCK:
        try:
            result = await refresh()
        except Exception:
            logger.exception("mcp-gateway: explicit pre-resolve refresh failed")
            return web.json_response(
                {"error": "Could not pre-resolve.", "code": "resolve_refresh_failed"},
                status=500,
            )
    if not isinstance(result, dict):
        return web.json_response(
            {"error": "Could not pre-resolve.", "code": "resolve_refresh_failed"},
            status=500,
        )
    return web.json_response(result)


async def api_mcp_gateway_enable(request: web.Request) -> web.Response:
    """POST /api/mcp-gateway/enable — persist the flag and apply it in-process.

    Writes ``mcp_gateway.enabled`` to config.json then applies the change
    live: the broker is started/stopped and all agent sessions are dropped +
    relinked to the new stub set — without restarting the gateway process,
    so the dashboard session stays authenticated.  Returns the verified state
    ``{ok, enabled, running, ping_ok}``.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_gateway_enable")
    if denied is not None:
        return denied
    from kiro_crew.config.loader import (  # circular import
        ConfigReadError,
        config_path,
        update_config_locked,
    )
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # circular import

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
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

    async def _persist_and_apply() -> web.Response:
        # Serialize the whole persist+apply under the apply lock so two racing
        # toggles cannot interleave (write A, write B, apply B, apply A) and leave
        # persisted config.json diverged from live broker state. The config lock is
        # nested inside only for the read-modify-write of config.json itself.
        async with _MCP_GATEWAY_APPLY_LOCK:
            async with _get_config_lock():
                # The overlay is user-owned and read-only here; its verdict does not
                # depend on config.json, so it is taken before the locked write.
                overlay = await asyncio.to_thread(_local_overlay_section)
                shadowed = _overlay_shadowed_keys(overlay, ("enabled",))
                if shadowed:
                    return web.json_response(
                        {
                            "error": (
                                "config.local.json defines "
                                f"mcp_gateway.{', mcp_gateway.'.join(shadowed)}, which "
                                "overrides config.json. Edit that file instead — writing "
                                "here would not change anything the gateway reads."
                            ),
                            "code": "overlay_owns_enabled",
                        },
                        status=409,
                    )

                section_not_object = False

                def _set_enabled(fresh: dict) -> dict | None:
                    nonlocal section_not_object
                    section = fresh.setdefault("mcp_gateway", {})
                    if not isinstance(section, dict):
                        # ``None`` skips the write; the 500 is answered outside.
                        section_not_object = True
                        return None
                    # Freeze the alias BEFORE reassigning `enabled` — the resolver
                    # reads `enabled`, so doing this afterwards would resolve against
                    # the new value and bake in the stub set this call must not change.
                    _freeze_stub_servers(section, overlay)
                    section["enabled"] = enabled
                    return fresh

                # Through ``update_config_locked``: it holds the advisory lock on the
                # sidecar ``<path>.lock`` across the whole read-modify-write, so a
                # writer in ANOTHER PROCESS cannot land between the read and the
                # write, and the mutation is applied to the file as read under that
                # lock. Off-loop and drained: file IO that may wait on another holder,
                # and a cancelled request must not unwind past the apply below while
                # the thread is still persisting ``enabled``.
                try:
                    await _offload_config_write(update_config_locked, path, mutate=_set_enabled)
                except ConfigReadError:
                    return web.json_response({"error": "config.json is corrupt"}, status=500)
                if section_not_object:
                    return web.json_response({"error": "mcp_gateway is not an object"}, status=500)
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
                # The exception detail is in the SEL log above; the client body
                # (rendered verbatim into a localized UI) gets a generic message.
                return web.json_response(
                    {"error": "apply failed", "code": "mcp_apply_failed"}, status=500
                )

        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="mcp_gateway_enable",
            outcome="ok",
            source="dashboard",
            resources=f"enabled={enabled}",
        )
        return web.json_response({"ok": True, **result})

    # Persist + apply is one transaction: a cancelled request (client gone,
    # gateway shutting down) must not persist ``enabled`` and then skip the
    # live apply, leaving config.json and the broker disagreeing.
    return await run_to_completion(_persist_and_apply())


# ─── Per-server poolability management ──────────────────────────────────


def _collect_server_rows() -> dict[str, dict[str, Any]]:
    """Scan the agent specs into one row per distinct server. BLOCKING.

    Reads ``~/.kiro/agents/*.json`` — the clean source specs, which the rewriter
    never mutates. Shared by the rows endpoint and the batch stub write so both
    describe the same fleet from the same scan; a second copy of this loop would
    let the two disagree about which servers exist and what they declare.

    Values are never collected, only env NAMES: the verdict engine needs to know
    whether a per-session rotating credential is declared, never what it is.
    """
    rows: dict[str, dict[str, Any]] = {}
    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return rows
    for path in iter_agent_spec_files(agents_dir):
        spec = _read_agent_spec(
            path,
            operation="mcp_server_rows",
            source="dashboard",
        )
        if spec is None:
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
                    "env_names": set(),
                    # One hash per DISTINCT launch seen under this name. A
                    # measurement belongs to one execution identity, and the probe
                    # only ever runs the definition that won the merge, so a second
                    # distinct launch here means the row covers something nobody
                    # measured. Hashing opens no file the launch names.
                    "launch_ids": set(),
                }
                rows[name] = row
            row["agents"].add(str(agent_name))
            command = entry.get("command")
            if isinstance(command, str) and command:
                args = entry.get("args")
                row["launch_ids"].add(
                    hash_command(command, [str(a) for a in args] if isinstance(args, list) else [])
                )
            if entry.get("poolable") is True:
                row["entry_poolable"] = True
            declared_env = entry.get("env")
            if isinstance(declared_env, dict):
                row["env_names"].update(str(k) for k in declared_env)
    return rows


def _launch_specs_for(names: set[str]) -> dict[str, list[SimpleNamespace]]:
    """EVERY definition of each named server, for identity computation only.

    Every one, not the first: a name can be declared by several agents, and two
    declarations that differ only in env are different programs as far as pooling
    is concerned. Returning one of them would let the identity of whichever file
    sorts first stand in for the rest.

    Separate from :func:`_collect_server_rows` on purpose. That builder feeds the
    rows payload and collects env NAMES only, because a display path must never
    hold a credential value. Identity needs the VALUES -- ``hash_effective_env``
    hashes them, with the pool's own rotating-secret exclusions, so a credential
    rotation does not read as a different server -- and they are consumed by the
    hash and never returned, logged or rendered.

    Only the requested names are collected, so a batch of three does not
    fingerprint a fleet of thirty.
    """
    specs: dict[str, list[SimpleNamespace]] = {}
    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return specs
    for path in iter_agent_spec_files(agents_dir):
        spec = _read_agent_spec(
            path,
            operation="mcp_stub_eligibility",
            source="dashboard",
        )
        if spec is None:
            continue
        mcp_servers = spec.get("mcpServers")
        if not isinstance(mcp_servers, dict):
            continue
        for name, entry in mcp_servers.items():
            if name not in names or not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                continue
            args = entry.get("args")
            env = entry.get("env")
            specs.setdefault(name, []).append(
                SimpleNamespace(
                    command=command,
                    args=[str(a) for a in args] if isinstance(args, list) else [],
                    env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
                )
            )
    return specs


async def api_mcp_gateway_servers(request: web.Request) -> web.Response:
    """GET /api/mcp-gateway/servers — enumerate distinct MCP servers.

    Reads ``~/.kiro/agents/*.json`` (the clean source specs — the rewriter never
    mutates them) and returns one row per distinct server with its effective
    STUB state. The stub is opt-in: a stdio server is stubbed only when its name
    is in ``mcp_gateway.stub_servers`` — that list is the ONLY trigger. A per-spec
    ``poolable:true`` is retired and does NOT stub a server; it is still reported
    as ``entry_poolable`` for information, because a row that claimed ``stub`` on
    the strength of that key was describing a stub that did not exist.
    HTTP/SSE servers cannot be stubbed — there is no stdio pipe to interpose
    on — and denylisted servers (``UNPOOLABLE_SERVERS``) never are.

    Whether a stubbed server SHARES its backend is not per-row: that is the one
    global switch (``mcp_gateway.enabled``), reported by the status endpoint.
    """
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
    from kiro_crew.mcp_gateway.rewriter import (
        UNPOOLABLE_SERVERS,
        _withheld_env_count,
        pool_identity_env_keys,
    )

    gw_cfg = KiroCrewConfig.load().mcp_gateway
    stub_set = set(gw_cfg.stub_servers)
    forward_declared_env = bool(gw_cfg.forward_declared_env)
    # Resolved ONCE for the whole payload, same reason the shareability files are:
    # two rows in one response must not disagree about the operator's list.
    identity_keys = pool_identity_env_keys()

    rows = await asyncio.to_thread(_collect_server_rows)

    # Both shareability files are read ONCE, off the event loop, before the row
    # loop — and the row builder does no IO at all. Reading per row would put N
    # synchronous parses on the loop for an N-server config, stalling the
    # dashboard and every chat sharing it, and would also let two rows in one
    # payload disagree about the same file.
    observed, preflights = await asyncio.to_thread(_load_shareability_state)
    probe_current = await asyncio.to_thread(_probe_currency)

    result: list[dict[str, Any]] = []
    for name in sorted(rows):
        row = rows[name]
        is_stdio = row["transport"] == "stdio"
        denylisted = name in UNPOOLABLE_SERVERS
        # Separated on purpose: ``can_stub`` is a property of the server (is
        # there a stdio pipe, is it denylisted) while ``stub`` is the
        # operator's choice. The UI needs both — one disables the control, the
        # other sets it — and collapsing them would make an unstubbable server
        # look like one the operator declined.
        can_stub = is_stdio and not denylisted
        # ``stub_servers`` is the only thing that produces a stub, so it is the
        # only thing this row may report. ``entry_poolable`` is still returned
        # below as information — a spec-level ``poolable: true`` does not opt a
        # server in, and reading it as "stubbed" here would make the row claim a stub
        # the broker has not created.
        stubbed = can_stub and name in stub_set
        # Whether stubbing this server could actually produce a SHARED backend.
        # The rewriter leaves an env-declaring entry unwrapped when the pooled
        # spawn would withhold a declared key (every key with
        # ``forward_declared_env`` off; the rotating-secret and credential
        # classes with it on), because a backend that dies without that key
        # would crash-loop through fallback on every session. Reported here so a
        # batch action cannot enable something the rewriter will silently skip —
        # that is the same "intent reported as reality" the Running-as column
        # already risks, and a bulk gesture multiplies it.
        #
        # Key NAMES only, never values: the classifier is name-based, so the
        # row builder's value-free discipline holds.
        pooling_blocked = bool(
            gw_cfg.enabled
            and _withheld_env_count(
                {k: "" for k in row["env_names"]}, forward_declared_env, identity_keys
            )
        )
        result.append(
            {
                "name": name,
                "stub": stubbed,
                "can_stub": can_stub,
                "in_allowlist": name in stub_set,
                "entry_poolable": row["entry_poolable"],
                "pooling_blocked_by_env": pooling_blocked,
                "agents": sorted(row["agents"]),
                "transport": row["transport"],
                "denylisted": denylisted,
                # Advisory only. Never auto-applied: the evidence is weaker
                # than proof (the probe handshakes as a different client than
                # the gateway does), so the operator decides.
                "recommendation": _assess_server(
                    name,
                    is_stdio=is_stdio,
                    env_names=tuple(sorted(row["env_names"])),
                    observed_hazards=observed.get(name, ()),
                    # A measurement describes ONE execution identity. When this
                    # name merged more than one distinct launch, the probe
                    # measured whichever definition won the merge, so serving that
                    # result here would tell the operator it is safe to share a
                    # backend nobody ran. Same invariant the cache-side check
                    # applies, enforced at the other place the information exists.
                    preflight=(preflights.get(name) if len(row["launch_ids"]) <= 1 else None),
                    identity_keys=identity_keys,
                    probe_current=probe_current.get(name, True),
                ).to_dict(),
            }
        )
    return web.json_response({"servers": result})


def _load_shareability_state() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[bool, bool]]]:
    """Read both shareability records for one response. BLOCKING — call off-loop.

    Returns ``(hazards_by_name, preflight_by_name)`` where the preflight value is
    ``(ran, caller_sensitive)``.

    Absence is not an error and not a claim of safety: an empty map means nothing
    has been observed or measured yet, which is exactly how
    ``shareability.assess`` treats it.

    Preflight rows are keyed by server NAME — one server, one row — so this is a
    direct lookup. Identity is a field inside the row and is not checked here: this
    builder is deliberately IO-free and cannot resolve a binary fingerprint, so a
    server whose command just changed shows its previous measurement until the next
    probe overwrites the row. Ambiguity that DOES matter — one name covering two
    different launches in the merged agent config — is decided in the row loop,
    where those definitions are visible.
    """
    try:
        rt = records_dir(KiroCrewConfig.load().mcp_gateway.socket_path)
        observed = hazards.load_ledger(rt).as_dict()
        cache = load_cache(rt)
    except OSError:
        return {}, {}
    preflights: dict[str, tuple[bool, bool]] = {}
    for name in cache.server_names():
        row = cache.get_by_name(name)
        if row is not None:
            preflights[name] = (row.ran, row.caller_sensitive)
    return observed, preflights


def _assess_server(
    name: str,
    *,
    is_stdio: bool,
    env_names: tuple[str, ...],
    observed_hazards: tuple[str, ...],
    preflight: tuple[bool, bool] | None,
    identity_keys: Collection[str] = (),
    probe_current: bool = True,
) -> ShareVerdict:
    """Build evidence for one row and hand it to the verdict engine.

    Pure: no IO, no config read, no clock. All the judgement lives in
    ``shareability``; this function only gathers what the caller already loaded.
    Probe metadata comes from the in-memory discovery cache rather than a fresh
    probe — starting a server to render a table would spawn every configured MCP
    on every page load, and probing is deliberately an explicit user action.

    *probe_current* False means that cache entry was taken under a config other
    than the row's current one (see ``_probe_currency``), so it is read as no
    measurement: the same fail-closed answer as never having probed.
    """
    meta = probe_metadata(name) if probe_current else None
    return assess(
        ShareEvidence(
            name=name,
            is_stdio=is_stdio,
            # Not "is this one of ours". A managed server is session-bound only
            # when it declines the caller-identity extension: kirocrew-core
            # consumes the injected caller block and shares a backend safely,
            # while kirocrew-cron reads process identity. Asking the server's own
            # module answers this without a handshake, which the probe cannot
            # always provide (no spawn on Windows / macOS >= 26, and none at all
            # before the first probe cycle).
            session_bound_by_construction=managed_server_is_session_bound(name),
            probe_ok=bool(meta and meta.status == "ok"),
            capabilities=meta.capabilities if meta else None,
            protocol_version=meta.protocol_version if meta else "",
            tool_annotations=list(meta.tool_annotations) if meta else [],
            has_tools=bool(meta and meta.tools),
            declared_env_names=env_names,
            observed_hazards=observed_hazards,
            preflight_ran=preflight[0] if preflight else None,
            preflight_caller_sensitive=preflight[1] if preflight else False,
        ),
        identity_keys,
    )


async def api_mcp_gateway_server_launch(request: web.Request) -> web.Response:
    """GET one server's complete, display-safe launch and compare-and-set token."""
    name = str(request.query.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name is required", "code": "name_required"}, status=400)
    if not _is_valid_mcp_name(name):
        return web.json_response(
            {"error": "invalid server name", "code": "invalid_server_name"}, status=400
        )
    try:
        launches = await asyncio.to_thread(launch_resolve.resolve_launches, [name])
    except Exception as exc:
        logger.warning("mcp launch resolution failed: %s", exc)
        return web.json_response(
            {"error": "could not resolve the launch", "code": "launch_resolve_failed"},
            status=503,
        )
    if name in getattr(launches, "over_cap", ()):
        return web.json_response(
            {"error": "the launch set exceeds the approval limit", "code": "launch_over_cap"},
            status=409,
        )
    if not launches.get(name):
        return web.json_response(
            {"error": "no launch resolves for this server", "code": "launch_unresolved"},
            status=409,
        )
    return web.json_response(display_launches(name, launches[name]))
