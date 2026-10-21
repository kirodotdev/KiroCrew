"""Probing the configured servers, and what the listing routes report about them.

The probe cache and its single-flight background run, quarantine verdicts and re-probe
arming, stamping each row with its config state, the shareability measurement, and the
``GET /api/mcp``, ``/api/mcp/active``, ``/api/mcp/probe``, ``/api/mcp/measure`` and
``/api/mcp/quarantine/clear`` routes.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew import mcp_quarantine
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.mcp_config_files import (
    _GLOBAL_MCP_JSON,
    _KIRO_GLOBAL_SURFACE,
    _config_entry_for,
    _kirocrew_mcp_json,
    _load_json_or_empty,
    _string_identifier,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.mcp_cleanup import mcp_entry_is_muted
from kiro_crew.mcp_discovery import (
    McpServerInfo,
    cached_probe_is_current,
    probe_metadata,
    redact_mcp_error,
    redact_mcp_headers,
)
from kiro_crew.mcp_gateway.rewriter import records_dir
from kiro_crew.sel import sel
from kiro_crew.session import process_shutdown_requested
from kiro_crew.user_json import loads_mcp_config

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


# ── MCP Servers ──


_mcp_probe_cache: list[dict] = []
_mcp_probe_ts: float = 0.0
_MCP_PROBE_CACHE_SECS = 600  # 10 min
_mcp_probe_in_progress = False
# Handle on the one probe allowed to be in flight. `_mcp_probe_in_progress` is
# the flag the request handlers below consult to avoid STACKING a re-probe; this
# is the joinable object that makes `_bg_mcp_probe` single-flight, which the flag
# alone cannot do (a caller cannot await a bool).
_mcp_probe_task: asyncio.Task[None] | None = None


def _quarantine_verdicts(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Extract ``(name, status, error)`` triples from probe rows.

    Every server is counted. An earlier revision filtered this to the servers an
    unmount could safely touch, so no badge could claim an unmount that did not
    happen -- but nothing is unmounted now, so the count is a plain diagnostic and
    withholding it from some servers would only hide information.

    A ``declared`` row is DROPPED, though, because its status is not a verdict.
    When a managed server cannot be probed under the sandbox, discovery lists the
    tools the package declares and reports ``ok`` with ``probeMode: "declared"``
    -- its own comment says "nothing verified the server can START". Passing that
    ``ok`` through would delete a real failure streak without a single successful
    handshake, so a server broken for a week would look healthy the moment the
    sandbox went unavailable. Dropping it also means such a round cannot ADD to
    the count: no handshake was attempted, so there is no outcome either way.
    This is the same rule that excludes ``needs_auth`` -- only a status that
    actually reports a handshake attempt may move the counter.
    """
    return [
        (str(r.get("name") or ""), str(r.get("status") or ""), str(r.get("error") or ""))
        for r in rows
        if str(r.get("name") or "") and str(r.get("probeMode") or "") != "declared"
    ]


def _arm_reprobe(request: web.Request) -> None:
    """Create the background re-probe task and keep a strong reference to it.

    Call this LAST in a handler, after every ``await`` it performs. The task can
    finish quickly, and its done-callback removes itself from
    ``state._background_tasks`` -- so a handler that creates it and then awaits
    anything before returning can hand over the loop, let the task complete, and
    return having erased the only evidence that a reprobe was armed. The caller
    sets ``_mcp_probe_in_progress`` at its decision point instead, which is what
    actually prevents a second concurrent probe.
    """
    state: DashboardState = request.app["state"]
    task = asyncio.create_task(_bg_mcp_probe(state.sessions))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


# Server name -> the cached identity a config-edit re-probe was last armed
# against. See ``_edited_since_probe``.
_mcp_reprobe_armed_for: dict[str, str] = {}


def _edited_since_probe(servers: list[McpServerInfo]) -> dict[str, str]:
    """Rows whose cached probe predates a config edit, not yet re-probed for it.

    A config edit makes the discovery cache refuse its entry, so the row reads
    ``unknown`` with no tools. The name is still in the handler cache, so the
    unseen-name check never fires, and without this the row stays empty until
    the handler TTL lapses.

    Returned rather than recorded: the caller records them only once it has
    actually armed a probe, so a request that finds one already in flight does
    not mark the edit handled by a probe that read the config before it.

    Keyed on the CACHED identity, so each edit arms at most once. Some rows are
    never re-probed by ``probe_all`` (a quarantined server is left out of the
    spawn set), so their entry stays mismatched indefinitely; comparing against
    the current config instead would re-arm a full fan-out on every request for
    as long as that lasts.
    """
    live = {s.name for s in servers if not s.disabled}
    for name in [n for n in _mcp_reprobe_armed_for if n not in live]:
        del _mcp_reprobe_armed_for[name]
    targets: dict[str, str] = {}
    for srv in servers:
        if srv.disabled or cached_probe_is_current(srv):
            continue
        meta = probe_metadata(srv.name)
        if meta is not None and _mcp_reprobe_armed_for.get(srv.name) != meta.identity:
            targets[srv.name] = meta.identity
    return targets


def _probe_currency() -> dict[str, bool]:
    """Name -> whether the discovery cache's entry fits the current config. BLOCKING.

    For readers that take probe metadata by NAME alone, such as
    ``_assess_server``: without it they ground a verdict about the configured
    target in a handshake with the previous one. A name absent here has no row
    in ``list_servers()`` to compare against, and callers keep reading it.
    """
    from kiro_crew.mcp_discovery import list_servers

    return {s.name: cached_probe_is_current(s) for s in list_servers()}


def _annotate_quarantine(rows: list[dict[str, Any]]) -> None:
    """Stamp ``probeFailures`` / ``probeFailing`` onto rows that have a record.

    Applied at RESPONSE time rather than baked into the cached rows, so
    resetting a server's count shows up on the next poll instead of waiting for a
    re-probe (a reset the UI cannot see reads as a broken button).

    Servers with no failures on file get neither key, so a healthy fleet's wire
    shape is byte-identical to before this feature.

    Reads the store, so every caller runs it OFF the event loop -- see the
    ``asyncio.to_thread`` at each call site, which callers skip entirely for an
    empty row list.
    """
    if not rows:
        return
    try:
        snap = mcp_quarantine.snapshot()
    except Exception:
        logger.debug("cannot read MCP quarantine state", exc_info=True)
        return
    for row in rows:
        state = snap.get(str(row.get("name") or ""))
        if state:
            row["probeFailures"] = state["fails"]
            row["probeFailing"] = state["failing"]


def _record_probe_verdicts(rows: list[dict[str, Any]]) -> None:
    """Filter probe rows to eligible servers and fold them into the store.

    One function so ONE ``to_thread`` covers both halves. Passing
    ``_quarantine_verdicts(rows)`` as an argument to ``to_thread`` evaluated it on
    the event loop, and that filter reads up to three MCP scope files to decide
    eligibility -- so the loop paid for those reads on every probe round.
    """
    mcp_quarantine.record_verdicts(_quarantine_verdicts(rows))


def _kirocrew_mcp_servers() -> dict[str, Any]:
    """The Kiro Crew store's ``mcpServers`` map, or ``{}`` when it is not a map.

    Blocking file I/O: the request handlers call it through ``asyncio.to_thread``.
    A hand-edited store can hold a list or a string under ``mcpServers``; the
    empty map keeps every reader on the ``.get`` path instead of raising.
    """
    servers = _load_json_or_empty(_kirocrew_mcp_json()).get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def _stamp_config_state(d: dict[str, Any], global_mcps: Any, kirocrew_mcps: Any) -> None:
    """Stamp the config-derived fields onto one serialized server row.

    ``enabled`` folds every place a disable can live -- the Kiro-global
    ``mcp.json``, the Kiro Crew store, and the row's own aggregate ``disabled``
    flag (``list_servers`` sets it from every scope) -- and a disabled row reads
    ``status: "disabled"``; ``kirocrewManaged`` says whether the store holds the
    entry, which is what gates the table's Edit action.

    ``disabledIn`` says WHICH scope switched the row off, because the table's
    two disabled states need opposite controls and cannot be told apart from
    ``enabled`` + ``kirocrewManaged``: ``"kirocrew"`` is a disable in Kiro
    Crew's own store, which the Kiro Crew scope badge + Apply lifts (the consent
    step); ``"shared"`` is a disable in a config this panel does not write for
    enable -- the shared Kiro ``mcp.json`` the IDE edits, or a provider global
    -- so the row is inert here. A row disabled in BOTH reads ``"shared"``:
    lifting the store's flag would leave the shared flag standing, so offering
    the consent step would offer a re-enable that cannot land. The same rule
    holds when the shared flag sits in a provider global this helper never
    reads: ``list_servers`` records that verdict on the row (``disabledInShared``,
    consumed here), so a store disable never masks it. ``None`` when
    enabled. ``disabledInFile`` names the file to edit (``~/.kiro/settings/mcp.json``,
    the constant display form -- never resolved here) when the shared flag is
    the Kiro-global one; ``None`` otherwise, including a disable only the
    aggregate flag reports (a provider global), which the table words as "the
    shared MCP config".

    Both maps are read by the ONE key the row stands for
    (:func:`_config_entry_for`): its exact key, else the single raw key whose
    alias it is (``npm:@playwright/mcp`` for the row ``playwright-mcp``). An
    ambiguous name stamps nothing from that map; the row's own aggregate flags
    still carry the disable.

    Pure: no filesystem call -- not a stat, not a home-directory resolution --
    because this runs on the event loop for every row of every listing and probe
    response. The pin is ``test_stamp_config_state_never_touches_the_filesystem``.

    Every "is it off" read here is :func:`mcp_entry_is_muted`, the launch
    predicate the gateway rewriter, the session projections and
    ``list_servers`` share -- FAIL-CLOSED, so ``"disabled": "false"`` (a string)
    is a disable, not a switch that failed to take. A row muted by such a value
    also carries ``disabledReason: "invalid"`` (``None`` otherwise): for the
    Kiro-global and store scopes it is read off the value in hand; for a disable
    only the aggregate flag reports, it is ``list_servers``' own verdict over
    the scopes it read. The table then says "invalid value in <file>", because
    the operator's fix is to repair the value, not to flip a switch.

    One helper for ``GET /api/mcp`` AND both probe paths, because the probe
    response REPLACES the table's list on the client rather than overlaying it.
    Stamped from the global file alone, a probe row for a store-disabled server
    came back ``enabled: true`` and flipped the row live until the next GET, and
    a row without ``kirocrewManaged`` lost its Edit action for the same interval.

    Either map may come from a hand-edited file whose ``mcpServers`` is not a
    mapping; a non-dict reads as an empty map rather than failing the whole
    response after the probe fan-out already ran.
    """
    if not isinstance(global_mcps, dict):
        global_mcps = {}
    if not isinstance(kirocrew_mcps, dict):
        kirocrew_mcps = {}
    name = str(d.get("name") or "")
    # By raw key OR alias: the row is canonical, the files are not.
    spec = _config_entry_for(global_mcps, name) or {}
    kc_spec = _config_entry_for(kirocrew_mcps, name)
    d["kirocrewManaged"] = kc_spec is not None
    shared_off = mcp_entry_is_muted(spec)
    store_off = mcp_entry_is_muted(kc_spec)
    is_disabled = shared_off or store_off or d.get("disabled") is True
    # ``list_servers``' verdict for a disable neither map in hand explains.
    row_reason = d.get("disabledReason") if d.get("disabledReason") == "invalid" else None
    # ``list_servers``' verdict that a scope this panel does not write for enable
    # -- the Kiro-global file or a PROVIDER global -- mutes the row. Consumed
    # here: the table reads ``disabledIn``, and this is its one input the two
    # maps in hand cannot supply. Without it a server disabled in a provider
    # global AND the store read ``"kirocrew"``, so Apply lifted the store flag
    # and the provider-global flag stood: a re-enable that could not land.
    row_shared = d.pop("disabledInShared", None) is True
    d["enabled"] = not is_disabled
    d["disabledIn"] = None
    d["disabledInFile"] = None
    d["disabledReason"] = None
    if not is_disabled:
        return
    d["status"] = "disabled"
    if store_off and not shared_off and not row_shared:
        d["disabledIn"] = "kirocrew"
        d["disabledReason"] = _invalid_flag_reason(kc_spec)
        return
    d["disabledIn"] = "shared"
    if shared_off:
        # The constant, not ``display_path(_GLOBAL_MCP_JSON)``: this runs on the
        # event loop for every row of every listing and probe response, and the
        # resolver stats the home directory (``Path.home().resolve()``) -- a slow
        # home mount would stall the gateway and its heartbeat. The file is the
        # Kiro-global one by construction, so its display form is a constant.
        d["disabledInFile"] = _KIRO_GLOBAL_SURFACE
        d["disabledReason"] = _invalid_flag_reason(spec)
    else:
        d["disabledReason"] = row_reason


def _invalid_flag_reason(entry: Any) -> str | None:
    """``"invalid"`` when a MUTED *entry* is muted by a non-boolean ``disabled``.

    Only called for an entry :func:`mcp_entry_is_muted` already answered True
    for, so a boolean here can only be ``True`` -- a real switch, no reason to
    add -- and anything else (``"false"``, ``1``, ``null``) is the config error
    the row should name.
    """
    flag = entry.get("disabled") if isinstance(entry, dict) else None
    return None if isinstance(flag, bool) else "invalid"


async def _bg_mcp_probe(sessions: SessionManager | None) -> None:
    """Populate the MCP probe cache — SINGLE-FLIGHT.

    Two independent boot paths reach this: ``dashboard/server.py`` fires it as a
    background task once the port is bound, and ``slack/gateway_runtime/service_boot.py`` awaits it
    before warming sessions (kiro-cli reads mcp.json at spawn time). Without a
    join, boot spawns and handshakes EVERY enabled MCP server twice — doubling
    the subprocess churn, doubling occupancy of probe_all()'s concurrency
    semaphore (so the first URL waits longer), and giving each server two
    chances to trip a rate limit or an auth prompt.

    ``_mcp_probe_in_progress`` could not close this on its own: it was written
    but never read here, and a bool cannot be awaited, so the second caller had
    nothing to wait on. The task handle can be, so both callers get one fan-out
    and both still return only once the cache is populated.

    The join is SHIELDED so a caller giving up (gateway wraps this in
    ``wait_for`` with a timeout) abandons its own wait without cancelling the
    probe mid-handshake — the fan-out completes and the cache is populated for
    whoever asks next, which is what the boot path's
    "continuing without full probe" message already implies.
    """
    global _mcp_probe_task

    inflight = _mcp_probe_task
    if inflight is not None and not inflight.done():
        await asyncio.shield(inflight)
        return

    task = asyncio.ensure_future(_run_mcp_probe(sessions))
    _mcp_probe_task = task
    await asyncio.shield(task)


async def _run_mcp_probe(sessions: SessionManager | None) -> None:
    """The probe fan-out itself. Reached only through `_bg_mcp_probe`."""
    global _mcp_probe_ts, _mcp_probe_in_progress
    _mcp_probe_in_progress = True
    try:
        # circular import: mcp_discovery defers imports of kiro_crew.agent
        # which shares state with this module, so importing it at module top
        # would cycle. Kept in-function like the other function-local mcp_discovery
        # imports in this module; noqa: F811 for their same-named imports.
        from kiro_crew.mcp_discovery import probe_all  # noqa: F811

        global_mcps: dict[str, Any] = {}
        try:
            data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
            global_mcps = data.get("mcpServers", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        kirocrew_mcps = await asyncio.to_thread(_kirocrew_mcp_servers)

        # Route through probe_all() so the fan-out is bounded by its
        # PROBE_MAX_CONCURRENCY semaphore. An
        # unbounded gather here floods the loop's default executor during a
        # network blip and can starve the heartbeat into a watchdog _exit.
        probed = await probe_all()
        overlapped_shutdown = process_shutdown_requested(sessions)
        result: list[dict[str, Any]] = []
        for s in probed:
            d = s.to_dict()
            spec = global_mcps.get(s.name, {})
            _stamp_config_state(d, global_mcps, kirocrew_mcps)
            if isinstance(spec, dict) and spec.get("disabledTools"):
                d["disabledTools"] = spec["disabledTools"]
            result.append(d)
        if result:
            if not overlapped_shutdown:
                await asyncio.to_thread(_record_probe_verdicts, result)
            await asyncio.to_thread(_annotate_quarantine, result)
        _mcp_probe_cache[:] = result
        _mcp_probe_ts = time.time()
        logger.info("MCP probe complete: %d servers", len(result))
    except Exception:
        logger.debug("Background MCP probe failed", exc_info=True)
    finally:
        _mcp_probe_in_progress = False


async def api_mcp_servers(request: web.Request) -> web.Response:
    """GET /api/mcp — list configured MCP servers with enabled state.

    Inventory comes from ``list_servers()``, which merges the agent config's
    ``mcpServers``, the scope-tagged ``mcp.json`` files (Kiro Crew data home
    and ``~/.kiro/settings/mcp.json``), and provider-global entries. This
    handler describes what the DASHBOARD shows; it makes no claim about which
    of these sources kiro-cli itself loads at session time — that is backend
    behaviour this repo cannot verify: agent-level and disabled entries have
    been seen initializing there anyway.
    """
    global _mcp_probe_in_progress
    from kiro_crew.mcp_discovery import list_servers  # circular import

    # Kick off a background re-probe if the handler cache is stale,
    # so the next request gets fresh results.
    now = time.time()
    stale = now - _mcp_probe_ts > _MCP_PROBE_CACHE_SECS

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
    # Only consider rows probe_all() would actually PROBE. It never spawns a
    # consent-disabled server, so a disabled row enters _mcp_probe_cache only
    # as an unprobed ``status: "disabled"`` placeholder (or not at all, from a
    # cache filled before the server was disabled) -- its presence says nothing
    # about freshness, and comparing the unfiltered list against the cache would
    # treat a disabled server as "new" on EVERY request, bypassing the cache TTL
    # and leaving a full spawn fan-out permanently in flight for anyone with one
    # disabled server. Applying probe_all's own spawn filter here keeps the
    # freshness check and the cache contents talking about the same set.
    if not stale:
        for srv in servers:
            if srv.disabled:
                continue
            cached = cached_by_name.get(srv.name)
            # A ``disabled`` placeholder is not a probe: the server was withheld
            # from the spawn set when the cache was filled. An enabled row behind
            # one -- the operator re-enabled the server inside the TTL -- has never
            # been probed, so it re-arms the fan-out exactly like an absent row.
            if cached is None or cached.get("status") == "disabled":
                stale = True
                break

    # NOTE: the reprobe is decided AND armed at the very end of this handler, not
    # here. See the block above the return.

    # Read global mcp.json for disabled state
    global_mcps: dict[str, Any] = {}
    try:
        data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Kiro Crew-scope entries: disabled state (consent-disabled installs and
    # custom adds live only here) + which rows the JSON editor can manage.
    # Off the loop, like the probe and probe-cache sites: the helper is blocking
    # file I/O (``read_text`` + ``json.loads``), and this handler runs for every
    # page load -- a slow data-home mount would stall the loop and its heartbeat.
    kirocrew_mcps = await asyncio.to_thread(_kirocrew_mcp_servers)
    result: list[dict] = []
    for s in servers:
        d = s.to_dict()
        # Prefer handler cache status over discovery cache "outdated". A cached
        # ``disabled`` placeholder carries no observation, so it never overlays a
        # row that is enabled now; a row still disabled reads ``disabled`` from
        # its own config state below.
        #
        # Also gated on the discovery cache's own identity check. This cache is
        # keyed on the server NAME, same as that one, and it outlives the
        # discovery TTL on purpose — so on a config edit it is the path that would
        # put the PREVIOUS target's tools back onto the row, one layer above the
        # check that just refused them. "unknown" is exactly what a refused entry
        # reports, so without the gate the edit is invisible here for as long as
        # this cache holds, and the row renders "ok" with no probedAt: an undated
        # Online, which is the state ``probed_at`` exists to rule out.
        cached = cached_by_name.get(s.name)
        if (
            cached
            and cached.get("status") != "disabled"
            and d["status"] in ("outdated", "unknown")
            and cached_probe_is_current(s)
        ):
            d["status"] = cached.get("status", d["status"])
            d["tools"] = cached.get("tools", d["tools"])
            d["error"] = cached.get("error", d["error"])
        _stamp_config_state(d, global_mcps, kirocrew_mcps)
        err = d.get("error")
        if err:
            from kiro_crew.security import redact

            err = redact(err)
            d["error"] = err
        result.append(d)
    # Annotated HERE too, not only on the probe endpoints. This is the endpoint
    # the MCP table loads from, so without it a quarantined server rendered as a
    # plain failing row until the user happened to press Probe -- the badge
    # reporting the failure streak, and the only control that resets it, were
    # both absent on the surface a user actually lands on.
    if result:
        await asyncio.to_thread(_annotate_quarantine, result)
        await asyncio.to_thread(_annotate_owner_sign_in, result)

    # Decide AND arm the re-probe here, at the very end, all synchronously.
    #
    # Three constraints have to hold at once and this is the only ordering that
    # satisfies all three:
    #   * nothing may await between the flag TEST and the flag SET, or two
    #     concurrent requests both arm a probe and a full spawn fan-out runs
    #     twice,
    #   * nothing may await between the task being CREATED and the handler
    #     returning, or a fast probe completes and its done-callback drops it
    #     from ``_background_tasks`` before the caller can see it was armed,
    #   * the flag must not be set on a path that fails to create the task, or it
    #     stays True for the life of the process and no re-probe ever runs again.
    # Placing the whole decision after the last await makes the test/set/create
    # sequence atomic on a single-threaded loop, so all three hold by
    # construction rather than by bookkeeping.
    edited = _edited_since_probe(servers)
    if (stale or edited) and not _mcp_probe_in_progress:
        _mcp_probe_in_progress = True
        _arm_reprobe(request)
        _mcp_reprobe_armed_for.update(edited)
    return web.json_response(result)


def _agent_mcp_server_names(agent: str) -> list[str] | None:
    """The sorted ``mcpServers`` names of the user-level spec declaring *agent*.

    ``None`` when no spec declares that name. A thread-side read: it walks the
    agents directory and parses specs (both forms) until the first match.
    """
    for f in iter_agent_spec_files(kiro_agents_dir_path(), ordered=False):
        spec = _read_agent_spec(
            f,
            operation="api_mcp_active",
            source="dashboard",
        )
        if spec is None:
            continue
        if spec.get("name") == agent:
            # ``mcpServers: null`` (or any non-mapping) declares no servers; it
            # must answer an empty list, not a 500 from ``sorted(None)``.
            servers = spec.get("mcpServers")
            return sorted(servers) if isinstance(servers, dict) else []
    return None


async def api_mcp_active(request: web.Request) -> web.Response:
    """GET /api/mcp/active — return MCP servers for the current agent.

    For non-kirocrew agents, reads ``mcpServers`` from the agent's config
    in ``~/.kiro/agents/`` — these are the only servers kiro-cli loads
    when ``--agent <name>`` is passed.  For kirocrew (or no agent),
    reads from global ``~/.kiro/settings/mcp.json`` as before.
    """
    agent = request.query.get("agent", "")

    # Resolve Kiro Crew agent name → kiro agent name so "default" → "kirocrew"
    if agent:
        try:
            from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings  # noqa: F811

            cfg = KiroCrewConfig.load()
            bindings = resolve_agent_bindings(cfg, agent)
            if bindings.kiro_agent:
                agent = bindings.kiro_agent
        except Exception:
            pass

    # Non-kirocrew agent: read from agent config. The lookup walks the agents
    # directory and reads specs until the name matches, so it runs in a thread:
    # a large directory must not stall every other request on the loop.
    if agent and agent != "kirocrew":
        agent_mcps = await asyncio.to_thread(_agent_mcp_server_names, agent)
        if agent_mcps is None:
            return web.json_response([])
        return web.json_response([{"name": n, "enabled": True} for n in agent_mcps])

    # Kiro Crew / default: every scope, through the one launch predicate.
    from kiro_crew.mcp_discovery import list_servers  # noqa: F811

    global_mcps: dict[str, Any] = {}
    try:
        data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    kirocrew_mcps = await asyncio.to_thread(_kirocrew_mcp_servers)
    servers = list_servers()
    result: list[dict] = []
    for s in servers:
        # The launch predicate (fail-closed) over the Kiro-global file AND the
        # Kiro Crew store, plus the row's own aggregate flag (every scope
        # ``list_servers`` read, provider globals included), so this listing and
        # the sessions agree on what is off. Read from the shared file alone, a
        # registry install awaiting consent -- ``disabled: true`` in the store,
        # absent from the shared file -- answered ``enabled: true``.
        enabled = not (
            s.disabled
            or mcp_entry_is_muted(_config_entry_for(global_mcps, s.name))
            or mcp_entry_is_muted(_config_entry_for(kirocrew_mcps, s.name))
        )
        result.append({"name": s.name, "enabled": enabled})
    # Also include the managed Kiro Crew servers (always enabled). NOTE for
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
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_probe")
    if denied is not None:
        return denied
    global _mcp_probe_ts
    from kiro_crew.mcp_discovery import probe_all  # noqa: F811

    servers = await probe_all()
    overlapped_shutdown = process_shutdown_requested(request.app["state"].sessions)
    # The operator just asked us to spawn every configured server, which is the
    # only moment the shareability pre-flight is affordable. Evaluate the ones
    # whose execution identity has no cached measurement; failures here must not
    # cost the probe its response, since status and tools are what was asked for.
    try:
        await _evaluate_shareability(servers)
    except Exception:
        logger.debug("shareability evaluation failed; probe result unaffected", exc_info=True)
    # Read global mcp.json for enabled/disabledTools state
    global_mcps: dict[str, Any] = {}
    try:
        data = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
        global_mcps = data.get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    kirocrew_mcps = await asyncio.to_thread(_kirocrew_mcp_servers)
    result: list[dict[str, Any]] = []
    for s in servers:
        d = s.to_dict()
        spec = global_mcps.get(s.name, {})
        _stamp_config_state(d, global_mcps, kirocrew_mcps)
        if isinstance(spec, dict) and spec.get("disabledTools"):
            d["disabledTools"] = spec["disabledTools"]
        result.append(d)
    if result:
        if not overlapped_shutdown:
            await asyncio.to_thread(_record_probe_verdicts, result)
        await asyncio.to_thread(_annotate_quarantine, result)
        await asyncio.to_thread(_annotate_owner_sign_in, result)
    _mcp_probe_cache[:] = result
    _mcp_probe_ts = time.time()
    return web.json_response(result)


async def _evaluate_shareability(servers: list[Any]) -> None:
    """Pre-flight any server whose execution identity has no cached measurement.

    Separated from the endpoint so the probe's own contract — status and tools —
    cannot be changed by a shareability failure.
    """
    # Imported HERE, not at module scope, and not because of a cycle: this module
    # is on the gateway's boot path, and ``evaluate`` pulls in ``preflight`` ->
    # ``mcp_discovery`` and ``stub`` (the stub PROCESS entry point). Measured on
    # this tree, hoisting it put 8 extra modules on that path — enough to push a
    # startup loop-responsiveness ceiling over on Windows. Nothing needs it until
    # an operator explicitly probes.
    from kiro_crew.mcp_gateway.evaluate import evaluate_new_servers

    # Deliberately NOT gated on a configured broker: a machine that has never
    # enabled stubbing is exactly the one that needs to learn whether it could.
    await evaluate_new_servers(
        list(servers), records_dir(KiroCrewConfig.load().mcp_gateway.socket_path)
    )


#: Progress of the operator-requested measurement pass. One pass at a time per
#: gateway: the work is bounded by the configured server count, and a second
#: concurrent pass would double the spawn load for no new measurements, since
#: both would pick the same unmeasured set.
_measure_progress: dict[str, Any] = {
    "running": False,
    # ``done`` is servers ATTEMPTED, which is what a progress bar advances on.
    # ``measured`` is how many of those produced a verdict. They disagree whenever
    # a pre-flight could not run, and only ``measured`` can carry a claim about
    # the outcome: a pass that reached nothing must not report that it measured
    # everything it tried.
    "done": 0,
    "measured": 0,
    "total": 0,
    "error": "",
}


async def _bg_measure_all() -> None:
    """Measure every server that has no current verdict, reporting progress.

    Runs uncapped, which is safe here and is not on the request path: an operator
    asked for it and is watching a progress readout, so the cost is expected
    rather than paid by somebody loading a page. The per-pass fan-out ceiling
    still applies inside the evaluator, so this is a longer pass and not a
    heavier one.
    """
    from kiro_crew.mcp_discovery import probe_all  # noqa: F811
    from kiro_crew.mcp_gateway.evaluate import evaluate_new_servers

    def report(measured: int, attempted: int, total: int) -> None:
        _measure_progress["measured"] = measured
        _measure_progress["done"] = attempted
        _measure_progress["total"] = total

    try:
        servers = await probe_all()
        await evaluate_new_servers(
            list(servers),
            records_dir(KiroCrewConfig.load().mcp_gateway.socket_path),
            budget=None,
            on_progress=report,
        )
    except Exception as exc:
        # Surfaced in the progress payload rather than only logged: the operator
        # is watching this readout, and a pass that silently stops looks
        # identical to one that finished with nothing to do.
        logger.warning("shareability: measurement pass failed: %s", exc)
        _measure_progress["error"] = type(exc).__name__
    finally:
        _measure_progress["running"] = False


async def api_mcp_measure_start(request: web.Request) -> web.Response:
    """POST /api/mcp/measure — measure every server with no current verdict.

    Returns immediately. The pass spawns two processes per unmeasured server and
    can take minutes on a large configuration, so it must not be awaited by a
    request: poll ``GET /api/mcp/measure`` for progress.

    A second call while a pass is running is reported rather than queued, because
    both passes would select the same unmeasured set and simply double the spawns.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_measure")
    if denied is not None:
        return denied
    if _measure_progress["running"]:
        return web.json_response({"ok": False, "running": True, **_measure_progress})
    _measure_progress.update(running=True, done=0, measured=0, total=0, error="")
    state: DashboardState = request.app["state"]
    task = asyncio.create_task(_bg_measure_all())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "running": True, **_measure_progress})


async def api_mcp_measure_progress(request: web.Request) -> web.Response:
    """GET /api/mcp/measure — where the current or last measurement pass got to."""
    return web.json_response({"ok": True, **_measure_progress})


async def api_mcp_probe_cached(request: web.Request) -> web.Response:
    """GET /api/mcp/probe — return cached probe results (non-blocking)."""
    global _mcp_probe_in_progress
    stale = time.time() - _mcp_probe_ts > _MCP_PROBE_CACHE_SECS

    # Read only when there is something to gate, so an empty cache adds no
    # yield point (see the quarantine read below).
    servers: list[McpServerInfo] = []
    if _mcp_probe_cache:
        from kiro_crew.mcp_discovery import list_servers

        servers = await asyncio.to_thread(list_servers)
    by_name = {s.name: s for s in servers}

    result: list[dict] = []
    for cached in _mcp_probe_cache:
        # Same name-keyed copy ``api_mcp_servers`` gates, served whole here. An
        # entry probed under a config other than its row's current one describes a
        # different target, so it is left out, which is how this endpoint
        # already reports a server with no probe result.
        srv = by_name.get(str(cached.get("name") or ""))
        if srv is not None and not cached_probe_is_current(srv):
            continue
        item = dict(cached)
        # The cache is populated from to_dict(), which already redacts headers
        # and errors — this pass is defense-in-depth for any future cache
        # population path that stores raw output, not the primary boundary.
        cached_headers = item.get("headers")
        if "error" in item:
            item["error"] = redact_mcp_error(item["error"], cached_headers)
        if "headers" in item:
            item["headers"] = redact_mcp_headers(cached_headers)
        result.append(item)
    # Skipped for an empty cache: the store read is not free, and the await it
    # would need is a yield point that lets a reprobe task armed just above run
    # to completion before this handler returns.
    if result:
        await asyncio.to_thread(_annotate_quarantine, result)
        await asyncio.to_thread(_annotate_owner_sign_in, result)
    # Decided and armed after the last await -- same three constraints as the
    # servers endpoint.
    edited = _edited_since_probe(servers)
    if (stale or edited) and not _mcp_probe_in_progress:
        _mcp_probe_in_progress = True
        _arm_reprobe(request)
        _mcp_reprobe_armed_for.update(edited)
    return web.json_response(result)


async def api_mcp_quarantine_clear(request: web.Request) -> web.Response:
    """POST /api/mcp/quarantine/clear — release an auto-quarantined MCP server.

    Clears the consecutive-failure COUNTER as well as the quarantine flag.
    Releasing a server but leaving it one failure short of re-quarantine would
    make the button look broken -- the user would press it, the server would
    fail once, and it would vanish again.

    Deliberately does NOT touch ``disabled`` in any config file: this clears only
    the count Kiro Crew accumulated on its own, so a server the user had switched
    off by hand stays off. It does not mount or unmount anything either -- the
    server was never unmounted.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_quarantine_clear")
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

    try:
        removed = await asyncio.to_thread(mcp_quarantine.clear, name)
    except (OSError, ValueError):
        # The store could not be read or could not be written, so nothing was
        # reset. Reporting success here would tell the user the count is clear
        # while it is still on disk. ValueError covers a payload ``json.dumps``
        # refuses; records are sanitized on read so that should be unreachable,
        # and a coded 500 beats an unhandled traceback if it is not.
        logger.warning("failure-count store unavailable resetting %s", name, exc_info=True)
        return web.json_response(
            {
                "error": "cannot update the probe-failure store",
                "code": "quarantine_store_write_failed",
            },
            status=500,
        )
    released = removed is not None
    if released:
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_probe_failures_reset",
            outcome="ok",
            source="dashboard",
            resources=f"{name} consecutive probe-failure count reset",
        )
        # The cached probe rows carry the old annotation; drop the row's keys so
        # a poll that lands before the next probe does not re-render the badge.
        for row in _mcp_probe_cache:
            if row.get("name") == name:
                row.pop("probeFailing", None)
                row.pop("probeFailures", None)
    return web.json_response({"ok": True, "name": name, "released": released})


def _annotate_owner_sign_in(rows: list[dict]) -> None:
    """Stamp ``ownerSignIn`` on each row the owner recorded for the dashboard sign-in.

    True only when the row's URL equals the URL recorded for its name in the
    sealed owner record (:mod:`kiro_crew.connections.owner_servers`). The mint
    re-checks the same record against the agent spec, so this flag only decides
    which rows the table offers the control on. Every row with a URL gets an
    explicit boolean, so a row served from a cache never keeps a stale True. An
    unreadable record clears any flag already on a row and stamps nothing, which
    leaves every row on the chat guidance.
    """
    from kiro_crew.config.loader import read_config_for_update
    from kiro_crew.connections.owner_servers import is_owner_server, is_signable_name

    for row in rows:
        if "ownerSignIn" in row:
            row["ownerSignIn"] = False
    try:
        config = read_config_for_update()
    except Exception:  # noqa: BLE001 -- no record read means no row is offered
        return
    for row in rows:
        name, url = row.get("name"), row.get("url")
        if not isinstance(url, str) or not url:
            continue
        row["ownerSignIn"] = (
            isinstance(name, str)
            and is_signable_name(name)
            and is_owner_server(config, name, {"url": url})
        )
