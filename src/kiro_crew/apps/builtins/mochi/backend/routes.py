"""Mochi — backend routes (browser-facing, same-origin authed).

Registered at gateway startup via the manifest's ``backend.routes`` field,
the same pattern as issue-radar. Handlers reach the live service graph
through :func:`kiro_crew.apps.builtins.mochi.hooks` — the runtime the
lifecycle hook built — so every WRITE goes through
``WatchlistService.enqueue_write`` (the serialization primitive the port
preserved) instead of racing the guard/poller loops on the files.

First slice (drives the Watchlist panel + stats + pinned files UI):

  GET  /api/apps/mochi/watchlist            -> {"items": [...]}
  POST /api/apps/mochi/watchlist/update     {add?, cancel?, update?}
                                            -> {"updated", "items", "warning"?}
  GET  /api/apps/mochi/stats                -> CompanionStats
  GET  /api/apps/mochi/pinned               -> {"pins": [...]}
  POST /api/apps/mochi/pinned/unpin         {"path"} -> {"ok": bool}
  POST /api/apps/mochi/pinned/mark-seen     {"path"} -> {"ok": true}
  GET  /api/apps/mochi/soul                 -> {"soul", "petName", "isDefault"}
  GET  /api/apps/mochi/pet-state            -> {"state", "mood"}
  POST /api/apps/mochi/pet-event            -> {"ok", "state"}
  GET  /api/apps/mochi/plan                 -> MochiQueue (planner's queue)
  GET  /api/apps/mochi/activity             -> {"entries": [...]}

Deny-by-default: every route 403s while the app is disabled (routes are
registered once at gateway startup, so a default-disabled app would otherwise
stay callable) and 503s if the runtime is not up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from contextlib import AbstractContextManager
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import mcp_discovery
from kiro_crew.apps.builtins.mochi import activity_log, hooks
from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.activity_log import (
    LOG_FILE,
    YESTERDAY_LOG_FILE,
    activity_mutation,
)
from kiro_crew.apps.builtins.mochi.agent_policy import PolicyNotMaterialized, apply_policy
from kiro_crew.apps.builtins.mochi.appearance_store import (
    MAX_BUNDLE_BYTES,
    PackError,
    delete_pack,
    export_pack,
    get_pack_detail,
    import_bundle,
    list_packs,
    read_pack_file,
    save_pack,
    save_sprite_pack,
)
from kiro_crew.apps.builtins.mochi.petdex_import import (
    PetdexError,
    fetch_pet,
    list_installed,
    read_installed,
)
from kiro_crew.apps.builtins.mochi.pinned_files_service import DATA_FILE_NAME, pins_mutation
from kiro_crew.apps.builtins.mochi.queue_file import QUEUE_FILE as _QUEUE_FILE
from kiro_crew.apps.builtins.mochi.queue_file import queue_mutation
from kiro_crew.apps.builtins.mochi.redact import redact_tree
from kiro_crew.apps.builtins.mochi.settings import (
    _base_defaults,
    load_settings,
    save_settings,
    settings_mutation,
    settings_path,
)
from kiro_crew.apps.builtins.mochi.stats_service import STATS_FILE_NAME
from kiro_crew.apps.builtins.mochi.watchlist_file import watchlist_mutation
from kiro_crew.apps.builtins.mochi.watchlist_service import _ARCHIVE_FILE, _WATCHLIST_FILE
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.hooks import safe_read_file
from kiro_crew.mcp_discovery import list_servers, probe_server
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

APP_NAME = "mochi"
_BASE = f"/api/apps/{APP_NAME}"

#: Where the shell-reported monitor list is cached for the MCP query action.
DISPLAYS_FILE = "mochi-displays.json"

Handler = Callable[[web.Request], Awaitable[web.Response]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _runtime() -> Any | None:
    # Deliberately function-local: the mochi package __init__ eagerly imports
    # this module (backend.routes) to expose register_routes, so a top-level
    # `import hooks` here would be a circular import at package load (hooks →
    # queue_poller/soul_loader/etc.). Deferring it to call time breaks the cycle
    # — the same pattern the sibling builtin uses (issue_radar/backend/routes.py).

    return hooks._runtime


def _rt() -> Any:
    """The live runtime. Handlers only run behind _require_enabled, which has
    already 503'd when it is None — assert for the type checker."""
    rt = _runtime()
    assert rt is not None
    return rt


def _require_enabled(handler: Handler) -> Handler:
    """403 while disabled, 503 while the lifecycle hook has not built the
    runtime yet. ``is_app_enabled`` is a sync installed.json read — run it off
    the event loop (same as issue-radar's guard)."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": "mochi is disabled", "code": "app_disabled"}, status=403
            )
        if _runtime() is None:
            return web.json_response(
                {"error": "mochi runtime not started", "code": "runtime_not_started"}, status=503
            )
        return await handler(request)

    return _wrapped


# ── Watchlist ───────────────────────────────────────────────────────────────


async def _handle_watchlist_get(request: web.Request) -> web.Response:
    rt = _rt()
    wl = await asyncio.to_thread(wf.read_watchlist, rt._watchlist_path, now_ms=_now_ms())
    # Watchlist items are agent-authored (MCP update_watchlist) and served to the
    # dashboard, so redact credentials/exfiltration URLs before they reach the
    # browser — same sink as the plan queue and activity log.
    return web.json_response({"items": _redact_plan_tree(wl.get("items", []))})


async def _handle_watchlist_update(request: web.Request) -> web.Response:
    try:
        params = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(params, dict) or not (
        params.keys() & {"add", "cancel", "update", "remove"}
    ):
        return web.json_response(
            {
                "error": "body must contain add, cancel, update and/or remove",
                "code": "invalid_watchlist_op",
            },
            status=400,
        )

    rt = _rt()
    result_box: dict[str, Any] = {}

    def do_update() -> None:
        now = _now_ms()
        # enqueue_write serializes writers WITHIN this process; the lock covers the
        # MCP server, which is a separate process and cannot see that queue.
        with wf.watchlist_mutation(rt._watchlist_path):
            wl = wf.read_watchlist(rt._watchlist_path, now_ms=now)
            result = wf.apply_watchlist_update(wl, params, now_ms=now)
            wf.write_atomic(rt._watchlist_path, {"version": 1, "items": result["items"]})
        result_box.update(result)

    try:
        await rt.watchlist.enqueue_write(do_update)
    except ValueError as err:
        # apply_watchlist_update rejects a malformed add/update entry. That is the
        # CLIENT's mistake, so it must not surface as a 500 (which reads as "the
        # server broke" and hides the actual reason from the UI).
        return web.json_response({"error": str(err), "code": "invalid_do_update"}, status=400)
    except OSError as err:
        logger.exception("[mochi] watchlist update write failed")
        return web.json_response({"error": str(err), "code": "do_update_failed"}, status=500)
    # result_box echoes the persisted (agent-authored) items — redact before it
    # reaches the browser, same as the watchlist GET / plan sinks.
    return web.json_response(_redact_plan_tree(result_box))


async def _handle_watchlist_clear_completed(request: web.Request) -> web.Response:
    """Archive then remove all terminal items — ported from the original
    ``watchlist:clear-completed`` IPC handler: archive written FIRST, then the
    active file, so a crash duplicates (dedup on next merge) rather than
    loses. Serialized via enqueue_write like every watchlist mutation."""
    rt = _rt()
    cleared_box = {"cleared": 0}

    def do_clear() -> None:
        now = _now_ms()
        with wf.watchlist_mutation(rt._watchlist_path):
            wl = wf.read_watchlist(rt._watchlist_path, now_ms=now)
            items = wl.get("items", [])
            terminal = [i for i in items if i.get("status") in wf.TERMINAL_STATUSES]
            remaining = [i for i in items if i.get("status") not in wf.TERMINAL_STATUSES]
            if not terminal:
                return
            archive_path = rt._watchlist_path.replace(
                "mochi-watchlist.json", "mochi-watchlist-archive.json"
            )
            archive = wf.read_archive(archive_path, now_ms=now)
            updated = wf.merge_into_archive(archive, terminal, now_ms=now)
            wf.write_atomic(archive_path, updated)
            wf.write_atomic(rt._watchlist_path, {"version": 1, "items": remaining})
            cleared_box["cleared"] = len(terminal)

    try:
        await rt.watchlist.enqueue_write(do_clear)
    except OSError as err:
        logger.exception("[mochi] clear-completed write failed")
        return web.json_response({"error": str(err), "code": "do_clear_failed"}, status=500)
    return web.json_response(cleared_box)


# ── Stats ───────────────────────────────────────────────────────────────────


async def _handle_stats_get(request: web.Request) -> web.Response:
    return web.json_response(_rt().stats.get_stats())


async def _handle_presence(request: web.Request) -> web.Response:
    """Pet-window heartbeat (every 30s while the Electron shell runs).

    The beat itself means the shell is running (gates autonomous work);
    ``visible`` means the pet is on screen (gates companion time). A hidden
    pet beats with visible=false; a closed shell stops beating entirely.
    """
    visible = True
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("visible"), bool):
            visible = body["visible"]
    except Exception:  # noqa: BLE001 — an empty body means a legacy visible beat
        pass
    _rt().presence_beat(visible=visible)
    return web.json_response({"ok": True})


async def _handle_bg_usage(request: web.Request) -> web.Response:
    """Background-agent usage for the Settings display.

    Counts come from the SAME ledger the hourly cap enforces, so the number a
    user sees can never disagree with the number that limits them. The current
    tier's contract is included so the UI shows live numbers next to live
    limits (``budget: null`` = the unlimited tier, nothing enforced).
    """
    rt = _rt()
    budget = rt.activity_budget()
    return web.json_response(
        {
            "usage": rt.spawn_ledger.usage_summary(),
            "budget": (
                None
                if budget is None
                else {
                    "tier": budget.tier,
                    "maxSpawnsPerHour": budget.max_spawns_per_hour,
                    "watchMinIntervalMins": budget.watch_min_interval_ms // 60_000,
                    "maxWatchBatch": budget.max_watch_batch,
                }
            ),
        }
    )


# ── Pinned files ────────────────────────────────────────────────────────────


async def _handle_pinned_get(request: web.Request) -> web.Response:
    # Pin labels are agent-authored (pin_file) — redact before the browser.
    return web.json_response({"pins": _redact_plan_tree(_rt().pinned.get_pins())})


async def _handle_pinned_unpin(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        path = body["path"]
    except Exception:  # noqa: BLE001
        return web.json_response(
            {"error": "body must contain path", "code": "path_required"}, status=400
        )
    # Off the loop: the pin list is mutated under a CROSS-PROCESS lock (the MCP
    # server writes the same file from its own process), so a concurrent holder
    # makes this a blocking wait — on the loop that would stall chat streaming and
    # the heartbeat for as long as the other side holds it. Same reason the pack
    # and queue writes are offloaded.
    ok = await asyncio.to_thread(_rt().pinned.remove_pin, path)
    return web.json_response({"ok": ok})


async def _handle_pinned_mark_seen(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        path = body["path"]
    except Exception:  # noqa: BLE001
        return web.json_response(
            {"error": "body must contain path", "code": "path_required"}, status=400
        )
    # Same cross-process lock as unpin above — see there.
    await asyncio.to_thread(_rt().pinned.mark_seen, path)
    return web.json_response({"ok": True})


# ── Soul ────────────────────────────────────────────────────────────────────


async def _handle_soul_get(request: web.Request) -> web.Response:

    soul = _rt().soul
    return web.json_response(
        {
            "soul": soul.get(),
            "petName": soul.pet_name,
            "isDefault": soul.is_default,
        }
    )


# ── Pet state ────────────────────────────────────────────────────────────────


async def _handle_pet_state_get(request: web.Request) -> web.Response:
    """Current behaviour state + mood.

    The initial pull the panel title bar and the pet overlay read on mount; live
    updates arrive over the WS (``pet:state-change`` / ``mochi:mood``). The
    backend PetStateManager is the source of truth, so both readers converge on
    it instead of seeding a stale local default.
    """
    sm = _rt().state_manager
    return web.json_response(
        {
            "state": sm.current,
            "mood": sm.current_mood,
            # Quiet-mode expiry (ms epoch, 0 = not quiet). Read by the pet
            # context menu on open; live changes also broadcast as
            # ``mochi:quiet``.
            "silentUntil": _rt().notify_gate.silent_until,
        }
    )


async def _handle_quiet(request: web.Request) -> web.Response:
    """Enter or leave notification quiet mode.

    Body ``{"minutes": 60}`` silences non-critical notifications for that long;
    ``{"minutes": 0}`` resumes and flushes the held backlog merged. Bounded so a
    client cannot silence the pet for a year — the menu offers one hour, and
    anything above a day is more plausibly a bug than an intent.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error
        return web.json_response(
            {"error": "expected a JSON object", "code": "invalid_json"}, status=400
        )
    minutes = body.get("minutes") if isinstance(body, dict) else None
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= 1440:
        return web.json_response(
            {"error": "minutes must be an integer between 0 and 1440", "code": "invalid_minutes"},
            status=400,
        )
    until = _rt().set_quiet(minutes)
    return web.json_response({"ok": True, "silentUntil": until})


#: Events the chat surface may report.
#:
#: The transition table knows more events than this, and the extra ones are
#: deliberately NOT reachable from a page: ``connect``/``disconnect`` are the
#: runtime's own statements about the gateway, ``walk_start``/``walk_done``
#: belong to the walk routes that also move the pet, and ``timeout`` is the
#: error-state deadline the manager fires itself. Accepting those here would let
#: a renderer contradict the runtime.
_CHAT_EVENTS = frozenset(
    {
        "user_input",
        "task_start",
        "tool_call",
        "task_complete",
        "approval_required",
        "approval_granted",
        "approval_rejected",
        "error",
    }
)


async def _handle_pet_event(request: web.Request) -> web.Response:
    """Report a chat-lifecycle event into the pet state machine.

    **Why a route exists at all.** Upstream ran the chat controller and
    ``petStateManager`` in one process, so sending a message moved the pet to
    `thinking`, a tool call to `working`, and completion back to `idle` by direct
    call. Here the state machine lives in the gateway while the conversation is
    driven from the chat surface, and no gateway seam publishes chat lifecycle to
    an app — so without this route ``apply_event`` was only ever reached with
    ``connect`` and ``disconnect``, the broadcast never carried anything but
    `idle`/`offline`, and every appearance pack showed exactly one clip forever.

    Unknown or non-chat events are refused rather than ignored, so a typo in a
    caller surfaces as a 400 instead of a pet that quietly never animates again.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error
        return web.json_response(
            {"error": "expected a JSON object", "code": "invalid_json"}, status=400
        )
    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(event, str) or event not in _CHAT_EVENTS:
        return web.json_response(
            {"error": f"unknown chat event: {event!r}", "code": "unknown_chat_event"}, status=400
        )
    sm = _rt().state_manager
    sm.apply_event(event, _now())
    # The runtime tracks foreground chat-turn activity from this SAME signal so
    # an ambient pet push is not interleaved into a live turn (see
    # MochiRuntime.note_chat_lifecycle). Using the route (foreground-only) rather
    # than state_manager.current is deliberate: background spawns also drive the
    # state machine, and must not count as the user conversing.
    _rt().note_chat_lifecycle(event, _now())
    return web.json_response({"ok": True, "state": sm.current})


# ── Plan / activity (dashboard page) ─────────────────────────────────────────


def _redact_plan_tree(value: object) -> object:
    """Recursively redact credentials + exfiltration URLs in a JSON-like value.

    The plan queue is agent-authored (``update_plan``), persisted verbatim, and
    served to the dashboard by ``_handle_plan_get`` (and echoed by the
    ``get_plan`` MCP tool). An AKIA key or webhook URL that an LLM wrote into a
    narrative / task / planner-note string would otherwise reach the browser raw.
    Delegates to the shared :func:`redact_tree` so this HTTP path and the
    in-process ``hooks`` notify/pin path apply the SAME two-way redaction.
    """
    return redact_tree(value)


async def _handle_plan_get(request: web.Request) -> web.Response:
    """The planner's current queue: ``{tasks, narrative?, mood?, ...}``.

    Same payload as the ``get_plan`` MCP tool, deliberately: the dashboard and
    the agent must not read the plan through two different shapes. An absent or
    unreadable file reports an empty plan rather than an error — "no plan yet" is
    a normal state (the planner writes the first one on its first run).
    """

    queue = await asyncio.to_thread(qf.read_queue, str(_rt().data_dir / _QUEUE_FILE))
    if queue is None:
        return web.json_response({"tasks": [], "note": "no plan yet"})
    # Redact before returning: the queue is agent-authored and served to the
    # dashboard, so a credential/URL an LLM wrote into it must not reach the
    # browser verbatim (mirrors the activity-log redaction sink).
    return web.json_response(_redact_plan_tree(queue))


async def _handle_activity_get(request: web.Request) -> web.Response:
    """Today + yesterday's activity entries, newest first."""

    entries = await asyncio.to_thread(activity_log.read_recent, _rt().data_dir)
    return web.json_response({"entries": entries})


@_require_enabled
async def _handle_settings_get(request: web.Request) -> web.Response:

    # Off the loop like every other file read on a request path.
    settings = await asyncio.to_thread(load_settings, _rt().data_dir)
    return web.json_response(settings)


# Settings keys that already-open Mochi windows must learn about immediately.
# Most change how the pet LOOKS (and, for appearance/name, how it describes
# itself);
# `language` is here because the pet and panel pick their i18n bundle from it.
# Without a broadcast the user had to close and reopen the pet for any of these
# to take effect. Keys consumed only by the backend (silentSubagents) are
# deliberately absent — no window renders them.
#: Keys whose change must reach open windows immediately. `colorMaps` and
#: `customPresets` were missing, so recolouring a pack in the Avatars window
#: persisted but the pet kept its old colours until it was reopened.
_LIVE_KEYS = (
    "activeAppearance",
    "catPreset",
    "petName",
    "language",
    "colorMaps",
    "customPresets",
)


@_require_enabled
async def _handle_settings_update(request: web.Request) -> web.Response:

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    try:
        # Blocking file write, but a settings save is a single small atomic
        # write and the endpoint is user-driven (not on any hot path).
        updated = await asyncio.to_thread(save_settings, _rt().data_dir, body)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_settings_update"}, status=400)
    if any(key in body for key in _LIVE_KEYS):
        # Off the loop: _appearance_changed re-renders the prompt and
        # re-materializes every agent config (scans + atomic writes). On the
        # event loop that stalls chat streaming and the heartbeat for the whole
        # scan — same reason the pin/stats ticks and pack writes are offloaded.
        await asyncio.to_thread(_appearance_changed, updated)
    policy_applied = True
    if "extraMcpServers" in body:
        # The grant list IS the agent's tool boundary, so it has to reach the
        # materialized agent config -- storing it in settings alone would leave
        # the Settings -> MCP toggles as inert decoration.

        try:
            # Same blocking re-materialization as above; keep it off the loop.
            await asyncio.to_thread(apply_policy, _rt().data_dir, updated)
        except PolicyNotMaterialized:
            # The SAVE stands (it is the user's data), but the boundary did not
            # move. ERROR, not warning: for a REVOKE this is fail-open — settings
            # now say "not granted" while the agent config still grants it, until
            # the gateway's next startup reconcile. Reported to the caller too, so
            # the API does not answer "saved" for a change that is not in force.
            logger.exception("Mochi: MCP grant list saved but NOT applied to the agent")
            policy_applied = False
        except Exception as exc:  # noqa: BLE001 - never fail the user's save
            logger.warning("Mochi: MCP policy update failed: %s", exc)
            policy_applied = False
    return web.json_response({**updated, "policyApplied": policy_applied})


def _appearance_changed(settings: dict[str, Any]) -> None:
    """Re-apply the persona and tell open windows the look changed.

    Two halves, both from the original: it broadcast ``color-map-changed`` so
    live windows re-rendered, AND it re-installed the agent prompt with the new
    appearance description — so a recolour also changed how the pet described
    itself. Re-applying the appearance/name to the SoulLoader is that second half
    here; the persona is otherwise only chosen once at runtime construction.
    """
    rt = _rt()
    try:
        # Re-resolve from the runtime rather than from this payload: a custom
        # pack's persona comes from the PACK's description, which the payload
        # does not carry.
        rt.soul.set_appearance(*rt._stored_appearance())
        name = settings.get("petName")
        if isinstance(name, str) and name:
            rt.soul.set_pet_name(name)
    except Exception:  # pragma: no cover - persona refresh must not fail a save
        logger.warning("mochi: could not re-apply persona after settings change")
    # The persona now lives in the SoulLoader, but the AGENT reads a rendered file.
    # Without this the rename only changed the UI: the agent kept answering to its
    # old name because its prompt file still said so. apply_policy re-materializes
    # the agent configs, which is what makes the new file take effect.
    try:

        rt._write_agent_prompt()
        apply_policy(rt.data_dir, load_settings(rt.data_dir))
    except PolicyNotMaterialized:
        # Separated from the cosmetic failure below: this one means the agent's
        # MCP boundary did not move either, which is not "the pet answers to its
        # old name" — it belongs at ERROR next to the other policy failures.
        logger.exception("Mochi: agent prompt rewritten but the MCP policy did NOT apply")
    except Exception:  # pragma: no cover - never fail the user's save
        logger.warning("mochi: could not re-install the agent prompt")
    rt._broadcast("mochi:color-map-changed", settings)


# ── Appearance packs (the Avatars surface) ─────────────────────────────────


@_require_enabled
async def _handle_packs_list(request: web.Request) -> web.Response:

    # Off the event loop like every other pack operation: listing stats every
    # pack directory, and the write paths were already moved for this reason —
    # a read of the same tree blocks the gateway exactly as much as a write.
    packs = await asyncio.to_thread(list_packs, _rt().data_dir)
    return web.json_response({"packs": packs})


@_require_enabled
async def _handle_pack_detail(request: web.Request) -> web.Response:

    try:
        detail = await asyncio.to_thread(
            get_pack_detail, _rt().data_dir, request.match_info["pack_id"]
        )
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_detail"}, status=400)
    if detail is None:
        return web.json_response({"error": "pack not found", "code": "pack_not_found"}, status=404)
    return web.json_response(detail)


#: Names currently being probed, so click-spam on "discover tools" cannot spawn
#: one server process per click. Core guards its whole-inventory probe the same
#: way (`handlers/mcp.py::_mcp_probe_in_progress`); this is the per-name form,
#: because here the name comes from the request rather than from the config.
_mcp_probe_inflight: set[str] = set()


def _mcp_scope_specs_strict() -> list[dict[str, Any]]:
    """Every MCP scope's ``mcpServers`` map, or raise if ANY scope is unusable.

    Deliberately NOT ``mcp_discovery._load_mcp_json_by_source``: that helper
    logs and ``continue``s past a file it cannot read or parse, so a scope
    holding ``disabled: true`` simply vanishes from its result. A consent check
    built on it fails OPEN — the flag is missing, the row looks enabled, and the
    server the user switched off gets spawned. Here a per-source failure is
    propagated so the caller can refuse.

    Paths are composed exactly as discovery composes them (core sources plus any
    platform-seam scopes) so the two cannot drift into scanning different files.
    Reads go through ``safe_read_file``, which re-validates the RESOLVED target
    and refuses a symlink into a credential path.
    """
    paths = list(mcp_discovery._mcp_json_paths())
    paths += [p for p, _ in mcp_discovery._extra_scope_sources()]
    out: list[dict[str, Any]] = []
    for p in paths:
        if not p.is_file():
            continue
        # Let OSError / JSONDecodeError propagate: unreadable is NOT "empty".
        data = json.loads(safe_read_file(str(p)))
        # A malformed SHAPE is unreadable too. Skipping it silently here would
        # reintroduce the very fail-open this function exists to close:
        # ``{"mcpServers": []}`` parses fine, carries no server map, and would
        # drop a scope that may hold the ``disabled: true`` — so the caller
        # would see no disable and spawn the server. Only an ABSENT
        # ``mcpServers`` is legitimately empty (a config with no servers).
        if not isinstance(data, dict):
            raise ValueError(f"{p}: top level is {type(data).__name__}, not an object")
        servers = data.get("mcpServers")
        if servers is None:
            continue
        if not isinstance(servers, dict):
            raise ValueError(f"{p}: mcpServers is {type(servers).__name__}, not an object")
        out.append(servers)
    return out


def _mcp_effectively_disabled(name: str, server: Any) -> bool:
    """True when ``name`` is disabled in ANY MCP scope (or on the merged row).

    Consent lives per scope, so the single flag on the merged row is not the
    answer: ``list_servers`` only sets ``disabled`` from an entry in the Kiro
    Crew scope, while ``/api/mcp/toggle`` writes ``disabled: true`` into the
    KIRO-GLOBAL ``mcp.json``. Reading only the row therefore MISSES a server the
    user switched off in the dashboard whenever a retained agent entry
    introduced the row first — and probing it would spawn the process consent
    exists to gate. ``api_mcp_servers`` reports the same OR as its ``enabled``
    field.

    Scope keys are matched through ``mcp_server_alias`` because ``list_servers``
    CANONICALIZES row names (step 3b): a server configured as
    ``npm:@playwright/mcp`` is reported as ``playwright-mcp``, so a raw-keyed
    ``disabled: true`` would never be found by an exact lookup — and the
    canonical row can be retained from the agent config, which is the bypass.

    Blocking file I/O — call from a thread.

    Fails CLOSED, per source: a scope that cannot be read or parsed means the
    consent state is UNKNOWN, not absent, so the probe is refused. A refused
    discover is visible and recoverable; spawning a server the user turned off
    is neither.
    """
    if getattr(server, "disabled", False):
        return True
    try:
        scopes = _mcp_scope_specs_strict()
    except Exception:
        logger.warning(
            "MCP scope read failed while checking %r; refusing the probe", name
        )
        return True
    target = mcp_server_alias(name)
    for specs in scopes:
        for key, spec in specs.items():
            if not isinstance(spec, dict) or not spec.get("disabled"):
                continue
            if key == name or mcp_server_alias(key) == target:
                return True
    return False


async def _handle_mcp_tools_probe(request: web.Request) -> web.Response:
    """POST /api/apps/mochi/mcp-tools/{name} — tools for ONE MCP server.

    Backs the settings panel's "discover tools" action. Core exposes the whole
    inventory as ``GET /api/mcp`` and register/remove as PUT/DELETE on
    ``/api/mcp/servers/{name}``, but never a per-server read — so the panel's
    fetch resolved that path, missed on method, and took a 405. Both the api
    helper and the click handler swallow failures, so the button did nothing at
    all, visibly or in a log.

    POST, not GET, because this SPAWNS A PROCESS. The dashboard's CSRF
    middleware exempts ``{"GET", "HEAD", "OPTIONS"}`` from the Origin check, so
    as a GET this would be reachable by cross-site top-level navigation carrying
    the Lax auth cookie — a foreign page could make the dashboard start any
    configured MCP server. Side effect => unsafe method => Origin enforced. It
    also matches core's own split, where probing is a POST and only the cached
    read is a GET.

    Probing lives here rather than in a new core route because the inventory is
    already reachable from the app: ``mcp_discovery`` is public API, and
    ``probe_server`` writes through to the same cache ``GET /api/mcp`` reads, so
    a discover here also freshens the core view.
    """
    name = (request.match_info.get("name") or "").strip()
    if not name:
        return web.json_response(
            {"error": "server name is required", "code": "invalid_name"}, status=400
        )

    # Config read touches the filesystem across every MCP scope — off the loop.
    servers = await asyncio.to_thread(list_servers)
    server = next((s for s in servers if s.name == name), None)
    if server is None:
        return web.json_response(
            {"error": "unknown MCP server", "code": "server_not_found"}, status=404
        )

    # A consent-disabled row must NEVER be probed: probing SPAWNS the server, and
    # the user has not agreed to run it. ``probe_all`` filters these out before
    # it ever calls ``probe_server`` (see mcp_discovery.probe_all's docstring),
    # and ``probe_server`` itself does NOT enforce it — so this per-server entry
    # point has to repeat the check or it becomes a way around the consent gate.
    #
    # ``McpServerInfo.disabled`` alone is NOT that check. list_servers() only
    # sets it for an entry in the Kiro Crew scope, but /api/mcp/toggle writes
    # ``disabled: true`` into the KIRO-GLOBAL mcp.json — so a server the user
    # switched off in the UI still arrives with ``disabled = False`` whenever a
    # retained agent entry introduced the row first. The effective state is the
    # OR across every scope, which is what api_mcp_servers reports as
    # ``enabled``.
    if await asyncio.to_thread(_mcp_effectively_disabled, name, server):
        return web.json_response(
            {"error": "MCP server is disabled", "code": "server_disabled"}, status=409
        )

    if name in _mcp_probe_inflight:
        return web.json_response(
            {"error": "probe already running", "code": "probe_in_progress"}, status=409
        )
    _mcp_probe_inflight.add(name)
    try:
        probed = await probe_server(server)
    finally:
        _mcp_probe_inflight.discard(name)

    # ``McpServerInfo.tools`` is a list of NAMES; the panel's row renderer takes
    # objects so a description can be added later without a shape change.
    #
    # ``probed.error`` is deliberately NOT returned. It is the server's own
    # stderr/exception text, so it can carry a credential (a token in a URL an
    # MCP server echoed back, for instance) and this response reaches the
    # dashboard. Redacting it would still ship best-effort-scrubbed remote text
    # for no benefit: the panel renders a translated message keyed off ``code`` /
    # ``status`` and never the prose, and ``probe_server`` already logs the real
    # reason gateway-side for operators.
    # ``tools`` is filtered to non-empty STRINGS. Both extraction paths in
    # ``mcp_discovery`` keep whatever a server put under ``name`` — the
    # comprehension guards the element with ``isinstance(t, dict)`` and then
    # binds ``name := t.get("name", "")`` on truthiness alone — so a server
    # answering ``{"name": {"x": 1}}`` or ``{"name": ["a"]}`` lands a dict/list
    # in the list. Serialized as-is it reaches the panel, which renders each
    # name as a React child, and a non-primitive child throws and blanks the
    # settings tree. A hostile or merely broken MCP server must not be able to
    # do that, so the untrusted shape is narrowed at this boundary rather than
    # trusted from upstream.
    return web.json_response(
        {
            "name": probed.name,
            "tools": [{"name": t} for t in probed.tools if isinstance(t, str) and t],
            "status": probed.status,
            "cached": False,
        }
    )


#: Content types for the file kinds a pack may hold. Keys must stay in step with
#: ``appearance_store._ALLOWED_SUFFIXES`` — that is what may be IN a pack, this
#: is how it is served back. A hardcoded image/png here mislabelled every
#: non-PNG asset (webp sprite sheets and Lottie JSON both ship in packs), and a
#: browser handed a webp as image/png simply fails to decode it.
_PACK_CONTENT_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    # SVG is served as text/plain, NOT image/svg+xml: a pack is user-imported and
    # an SVG rendered as a same-origin DOCUMENT (direct navigation / iframe) runs
    # any inline <script> under the dashboard origin, reaching authenticated APIs.
    # The renderer never loads it as image/svg+xml anyway — it fetches the bytes
    # and reads res.text() (packDetail.packFileContent), so text/plain is
    # transparent to legitimate use and neutralizes the document-XSS vector. The
    # nosniff header below stops a browser sniffing it back into an SVG document.
    ".svg": "text/plain",
    ".json": "application/json",
}


@_require_enabled
async def _handle_pack_file(request: web.Request) -> web.Response:
    """Serve one image out of a pack.

    Served through the app's own route rather than a static mount so the
    enabled-gate and the path confinement in ``read_pack_file`` both apply; a
    static mount over the data dir would expose every other Mochi file too.
    """

    filename = request.match_info["filename"]
    try:
        blob = await asyncio.to_thread(
            read_pack_file, _rt().data_dir, request.match_info["pack_id"], filename
        )
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_file"}, status=400)
    if blob is None:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    suffix = os.path.splitext(filename)[1].lower()
    return web.Response(
        body=blob,
        content_type=_PACK_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        # nosniff so a text/plain SVG (see _PACK_CONTENT_TYPES) cannot be
        # content-sniffed back into an executable image/svg+xml document, and no
        # other pack asset is re-typed by the browser either.
        headers={"X-Content-Type-Options": "nosniff"},
    )


@_require_enabled
async def _handle_pack_save(request: web.Request) -> web.Response:

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    try:
        pack_id = await asyncio.to_thread(save_sprite_pack, _rt().data_dir, body)
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_save"}, status=400)
    # The original emitted gallery:packs-changed on every save; without it an
    # open Avatars window (and the pet) only saw the new pack after a reopen.
    _rt()._broadcast("mochi:gallery-packs-changed", {"packId": pack_id})
    return web.json_response({"ok": True, "packId": pack_id})


@_require_enabled
async def _handle_pack_delete(request: web.Request) -> web.Response:

    pack_id = request.match_info["pack_id"]
    try:
        removed = await asyncio.to_thread(delete_pack, _rt().data_dir, pack_id)
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_delete"}, status=400)
    # Deleting the ACTIVE pack must also clear the pointer, or the pet keeps
    # trying to render a pack that no longer exists.
    active = (await asyncio.to_thread(load_settings, _rt().data_dir)).get("activeAppearance")
    if removed and active == pack_id:
        updated = await asyncio.to_thread(save_settings, _rt().data_dir, {"activeAppearance": ""})
        # Off the loop — see the settings-save handler for why.
        await asyncio.to_thread(_appearance_changed, updated)
    if removed:
        _rt()._broadcast("mochi:gallery-packs-changed", {"packId": pack_id, "deleted": True})
    return web.json_response({"ok": removed})


@_require_enabled
async def _handle_petdex_installed(request: web.Request) -> web.Response:
    """List pets the petdex CLI already installed. No network, never fails."""

    # Directory scan + N small reads: off the event loop.
    pets = await asyncio.to_thread(list_installed)
    return web.json_response({"pets": pets})


@_require_enabled
async def _handle_petdex_import(request: web.Request) -> web.Response:
    """Obtain one petdex pet, from the local install dir or from petdex.dev.

    ``{"slug": "boba", "source": "local" | "remote"}``. The remote branch makes
    a user-initiated outbound request to petdex.dev; it is fenced inside
    :mod:`petdex_import` (HTTPS + host allow-list + byte caps + timeout) and
    only ever sends the slug.
    """

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    slug = body.get("slug")
    if not isinstance(slug, str):
        return web.json_response({"error": "slug is required", "code": "slug_required"}, status=400)

    try:
        if body.get("source") == "local":
            payload = await asyncio.to_thread(read_installed, slug)
        else:
            payload = await fetch_pet(slug)
    except PetdexError as exc:
        # 400, not 502: every failure here is something the user can act on
        # (wrong name, no network, unexpected format) and the UI shows the text.
        return web.json_response({"error": str(exc), "code": "invalid_petdex_import"}, status=400)
    return web.json_response({"ok": True, **payload})


def register_routes(app: web.Application) -> None:
    """Register on the gateway's aiohttp Application (single-arg convention,
    same as every builtin — see issue_radar/backend/routes.py)."""
    app.router.add_get(f"{_BASE}/watchlist", _require_enabled(_handle_watchlist_get))
    app.router.add_post(f"{_BASE}/watchlist/update", _require_enabled(_handle_watchlist_update))
    app.router.add_post(
        f"{_BASE}/watchlist/clear-completed",
        _require_enabled(_handle_watchlist_clear_completed),
    )
    app.router.add_get(f"{_BASE}/stats", _require_enabled(_handle_stats_get))
    app.router.add_get(f"{_BASE}/bg-usage", _require_enabled(_handle_bg_usage))
    app.router.add_post(f"{_BASE}/presence", _require_enabled(_handle_presence))
    app.router.add_get(f"{_BASE}/pinned", _require_enabled(_handle_pinned_get))
    app.router.add_get(f"{_BASE}/packs/{{pack_id}}/export", _require_enabled(_handle_pack_export))
    app.router.add_post(f"{_BASE}/packs/content", _require_enabled(_handle_pack_save_content))
    app.router.add_post(f"{_BASE}/packs/import", _require_enabled(_handle_pack_import))
    app.router.add_post(f"{_BASE}/reset", _require_enabled(_handle_reset))
    app.router.add_post(f"{_BASE}/walk-done", _require_enabled(_handle_walk_done))
    app.router.add_post(f"{_BASE}/walk-distance", _require_enabled(_handle_walk_distance))
    app.router.add_post(f"{_BASE}/peeking", _require_enabled(_handle_peeking))
    app.router.add_post(f"{_BASE}/stat", _require_enabled(_handle_stat))
    app.router.add_post(f"{_BASE}/displays", _require_enabled(_handle_displays))
    app.router.add_post(f"{_BASE}/pinned/unpin", _require_enabled(_handle_pinned_unpin))
    app.router.add_post(f"{_BASE}/pinned/mark-seen", _require_enabled(_handle_pinned_mark_seen))
    app.router.add_get(f"{_BASE}/soul", _require_enabled(_handle_soul_get))
    app.router.add_get(f"{_BASE}/pet-state", _require_enabled(_handle_pet_state_get))
    app.router.add_post(f"{_BASE}/pet-event", _require_enabled(_handle_pet_event))
    app.router.add_post(f"{_BASE}/quiet", _require_enabled(_handle_quiet))
    app.router.add_get(f"{_BASE}/plan", _require_enabled(_handle_plan_get))
    app.router.add_get(f"{_BASE}/activity", _require_enabled(_handle_activity_get))
    app.router.add_get(f"{_BASE}/settings", _handle_settings_get)
    app.router.add_post(f"{_BASE}/settings", _handle_settings_update)
    app.router.add_get(f"{_BASE}/packs", _handle_packs_list)
    app.router.add_post(f"{_BASE}/packs", _handle_pack_save)
    app.router.add_get(f"{_BASE}/packs/{{pack_id}}", _handle_pack_detail)
    app.router.add_delete(f"{_BASE}/packs/{{pack_id}}", _handle_pack_delete)
    app.router.add_get(f"{_BASE}/packs/{{pack_id}}/file/{{filename}}", _handle_pack_file)
    app.router.add_get(f"{_BASE}/petdex/installed", _handle_petdex_installed)
    app.router.add_post(f"{_BASE}/petdex/import", _handle_petdex_import)
    # POST, not GET, even though this reads: it SPAWNS the configured server
    # process. The dashboard's CSRF Origin check only guards mutating methods
    # (see dashboard/origin.py -- ``check_host`` runs for every method, the CSRF
    # boundary does not), and the auth cookie is SameSite=Lax, which a browser
    # still attaches to a cross-site TOP-LEVEL navigation. As a GET this was
    # therefore reachable by pointing a malicious page's location at it: no
    # CSRF check, cookie attached, and a configured MCP server gets executed.
    # A side-effecting endpoint has to be an unsafe method to inherit that gate.
    app.router.add_post(
        f"{_BASE}/mcp-tools/{{name}}", _require_enabled(_handle_mcp_tools_probe)
    )


# ── Movement reports ───────────────────────────────────────────────────────
#
# Upstream these were ipcMain handlers in the pet's own main process. As a
# builtin the pet is a page on this origin, so the same three reports arrive as
# posts. They exist for the same reasons they did upstream: pet STATE and the
# stats file are backend-owned, and a walk that never reports completion leaves
# the state machine stuck in "walking".


def _now() -> int:
    return int(time.time() * 1000)


async def _handle_walk_done(_request: web.Request) -> web.Response:
    _rt().state_manager.finish_walking(_now())
    return web.json_response({"ok": True})


async def _handle_walk_distance(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    pixels = body.get("pixels")
    # isfinite rejects NaN/±inf: they pass an isinstance+`<= 0` guard (nan/inf
    # compare False) but crash round() with ValueError -> HTTP 500. Order the
    # isinstance check first so isfinite only ever sees a number.
    if not isinstance(pixels, (int, float)) or not math.isfinite(pixels) or pixels <= 0:
        return web.json_response(
            {"error": "pixels must be a positive number", "code": "invalid_pixels"}, status=400
        )
    # 64px per step, as upstream: the count is a friendly stat, not telemetry.
    steps = max(1, round(pixels / 64))
    _rt().stats.record_walk(_now(), steps)
    return web.json_response({"ok": True, "steps": steps})


async def _handle_stat(request: web.Request) -> web.Response:
    """Report a countable companion event.

    One endpoint rather than one route per counter: these are all
    fire-and-forget friendly stats with the same shape. The recorders already
    existed in stats_service but had NO callers, so the Memories view showed
    only the counters the backend happened to own (walks, peeks, time) and
    read as broken.
    """
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    kind = body.get("kind")
    now = _now()
    stats = _rt().stats
    if kind == "message_sent":
        stats.record_message_sent(now)
    elif kind == "message_received":
        stats.record_message_received(now)
    elif kind == "screenshot":
        stats.record_screenshot(now)
    elif kind == "drag":
        stats.record_drag(now)
    else:
        return web.json_response(
            {"error": f"unknown stat kind {kind!r}", "code": "unknown_stat_kind"}, status=400
        )
    return web.json_response({"ok": True})


async def _handle_peeking(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    peeking = body.get("peeking")
    if not isinstance(peeking, bool):
        return web.json_response(
            {"error": "peeking must be a boolean", "code": "invalid_peeking"}, status=400
        )
    if peeking:
        _rt().stats.record_peek(_now())
    # Re-broadcast so every Mochi surface agrees the pet is tucked away — the
    # panel dims its header, and a second pet window would follow suit.
    _rt().publish("mochi:peeking", {"peeking": peeking})
    return web.json_response({"ok": True})


async def _handle_displays(request: web.Request) -> web.Response:
    """Cache the monitor list the shell knows and the backend does not.

    `perform_pet_action({action:"query"})` has to answer "which displays exist,
    and where is the pet" — questions only the Electron shell can see. The pet
    window posts its display list on every displays-info event, so the answer is
    at most one display change old.
    """
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    displays = body.get("displays")
    if not isinstance(displays, list):
        return web.json_response(
            {"error": "displays must be an array", "code": "invalid_displays"}, status=400
        )
    await asyncio.to_thread(_write_displays_cache, _rt().data_dir, displays, body.get("activeId"))
    return web.json_response({"ok": True})


def _write_displays_cache(data_dir: Any, displays: list[Any], active_id: Any) -> None:

    # Keep the GEOMETRY, not just the size. This projection used to reduce each
    # monitor to {id, width, height}, which left the pet unable to answer the one
    # question the cache exists for: which screen am I on, and where is it. With
    # no ordinal, no primary flag and no origin, an agent asked "which display?"
    # has nothing to reason from and guesses — usually "display 1". `index` is
    # 1-based so it matches how the user counts screens, and `active` is written
    # onto the entry as well as the top level so the two cannot be mis-paired.
    entries: list[dict[str, Any]] = []
    for d in displays:
        if not isinstance(d, dict):
            continue
        entry: dict[str, Any] = {
            "id": d.get("id"),
            "index": d.get("index"),
            "primary": bool(d.get("primary")),
            "x": d.get("x"),
            "y": d.get("y"),
            "width": d.get("width"),
            "height": d.get("height"),
            "active": d.get("id") == active_id,
        }
        work_area = d.get("workArea")
        if isinstance(work_area, dict):
            entry["workArea"] = {k: work_area.get(k) for k in ("x", "y", "width", "height")}
        entries.append(entry)

    payload = {
        "displays": entries,
        "activeId": active_id,
        "at": _now(),
    }
    try:
        atomic_write(
            data_dir / DISPLAYS_FILE,
            json.dumps(payload, indent=2),
            mode=0o600,
        )
    except OSError:
        # A failed cache write must not fail the pet's display handshake.
        logger.warning("mochi: could not cache the display list")


# ── Appearance bundles (.mochipack.zip) ────────────────────────────────────


async def _handle_pack_export(request: web.Request) -> web.Response:

    pack_id = request.match_info["pack_id"]
    try:
        # Off the event loop for the same reason as the import handler: a
        # sprite pack zips megabytes of sheets synchronously.
        blob = await asyncio.to_thread(export_pack, _rt().data_dir, pack_id)
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_export"}, status=400)
    return web.Response(
        body=blob,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pack_id}.mochipack.zip"'},
    )


async def _handle_pack_import(request: web.Request) -> web.Response:

    # Read with a ceiling rather than `await request.read()`: an unbounded read of
    # an untrusted upload is the one failure the store's own guards cannot catch,
    # because it happens before they run.
    blob = bytearray()
    while True:
        chunk = await request.content.read(64 * 1024)
        if not chunk:
            break
        blob.extend(chunk)
        if len(blob) > MAX_BUNDLE_BYTES:
            return web.json_response(
                {"error": "Bundle is too large", "code": "bundle_too_large"}, status=413
            )
    try:
        # Off the event loop: a bundle at the 32 MiB cap means seconds of ZIP
        # inflation and filesystem writes, which would stall every other
        # request (and the heartbeats) for the duration.
        meta = await asyncio.to_thread(import_bundle, _rt().data_dir, bytes(blob))
    except PackError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_pack_import"}, status=400)
    _rt()._broadcast("mochi:gallery-packs-changed", {"packId": meta.get("id")})
    return web.json_response({"ok": True, "meta": meta})


# ── Reset ──────────────────────────────────────────────────────────────────


#: Mochi-owned state files a reset clears. User-CREATED content is deliberately
#: absent: imported appearance packs are the user's own art, and the original's
#: own dialog only promised history, activity logs, screenshots and learned
#: preferences.
#:
#: Every entry is IMPORTED from the module that owns the file, never re-spelled.
#: A hand-written copy of this list had drifted from three of the real names
#: (`mochi-stats.json` vs `stats.json`, `mochi-pinned.json` vs
#: `pinned-files.json`, plus a notifications file that does not exist), and the
#: unlink loop treats a missing file as "already gone" — so reset reported
#: success while silently keeping the companion stats and the pin rail.
def _reset_files() -> tuple[str, ...]:

    return (
        LOG_FILE,
        YESTERDAY_LOG_FILE,
        STATS_FILE_NAME,
        _QUEUE_FILE,
        _WATCHLIST_FILE,
        _ARCHIVE_FILE,
        DATA_FILE_NAME,
        DISPLAYS_FILE,
    )


async def _handle_reset(_request: web.Request) -> web.Response:
    """Return Mochi to a fresh state: defaults, no memory, the cat again.

    The chat SLOT is not cleared here — that is core's
    `DELETE /api/chat/slots/{slot}`, which the caller issues alongside this. Doing
    it from the app would mean reaching into another subsystem's storage.
    """

    rt = _rt()
    defaults = _base_defaults()

    def _wipe() -> list[str]:
        # Each unlink must run under the SAME cross-process lock the file's
        # writers hold. A reset that unlinks outside those locks can land
        # between a concurrent writer's read and its atomic write (MCP adding a
        # queue item, the poller archiving the watchlist, the activity logger),
        # so the writer recreates the file from pre-reset state right after the
        # reset "succeeds" — the reset silently un-does itself.

        d = rt.data_dir
        gone: list[str] = []
        handled: set[str] = set()

        def _rm(name: str) -> None:
            target = d / name
            try:
                target.unlink()
                gone.append(name)
            except FileNotFoundError:
                return
            except OSError:
                # One stubborn file must not abort the rest of the reset; the user
                # asked for a clean slate and a partial one is still closer.
                logger.warning("mochi: reset could not remove %s", name)

        def _rm_locked(lock: AbstractContextManager, *names: str) -> None:
            with lock:
                for name in names:
                    _rm(name)
                    handled.add(name)

        # Lock-guarded stores: unlink under the writer's lock. The watchlist
        # archive is written while holding the watchlist lock, and YESTERDAY is
        # written under the activity LOG_FILE lock, so each pair takes one lock.
        _rm_locked(queue_mutation(d / _QUEUE_FILE), _QUEUE_FILE)
        _rm_locked(watchlist_mutation(d / _WATCHLIST_FILE), _WATCHLIST_FILE, _ARCHIVE_FILE)
        # Pinned is special: hold its mutation lock across BOTH the unlink AND the
        # service cache reload. Every pinned write reloads from disk under this
        # same lock before persisting, so serializing the wipe+reload here means
        # a concurrent owner-loop pin write lands EITHER fully before the delete
        # (then it is deleted) OR fully after the reload (then it reloads the now
        # -empty file and adds a genuinely new pin) — it can never flush a
        # pre-reset cached list back over the file the reset just cleared.
        with pins_mutation(str(d / DATA_FILE_NAME)):
            _rm(DATA_FILE_NAME)
            handled.add(DATA_FILE_NAME)
            try:
                rt.pinned.load(_now())
            except Exception:  # pragma: no cover - defensive
                logger.warning("mochi: reset could not reload pinned files")
        _rm_locked(activity_mutation(d), LOG_FILE, YESTERDAY_LOG_FILE)

        # Sweep the remaining reset targets (the displays cache: user-driven
        # endpoint only; plus anything future added to _reset_files()). These
        # have no concurrent RMW writer lock, so a plain unlink is correct.
        # Driving the remainder off _reset_files() keeps that tuple the single
        # completeness source — a new reset target can never be silently
        # forgotten here.
        #
        # STATS is deliberately EXCLUDED from this off-thread unlink: the stats
        # writer (tick/flush/save) is owner-loop-only and lockless, so unlinking
        # it here in the worker thread could interleave with an on-loop flush
        # that rewrites a stale snapshot back over the wipe. It is instead reset
        # on the loop below via StatsService.reset(), sequential with tick.

        handled.add(STATS_FILE_NAME)
        for name in _reset_files():
            if name not in handled:
                _rm(name)

        # Settings is REWRITTEN (not unlinked) to defaults, under its own
        # mutation lock so a concurrent save can't clobber the defaults we write.
        with settings_mutation(d):
            atomic_write(settings_path(d), json.dumps(defaults, indent=2), mode=0o600)
        return gone

    # One synchronous block, run off the event loop: a reset unlinks eight files
    # and rewrites settings, and doing that inline stalls chat for its duration.
    removed = await asyncio.to_thread(_wipe)

    # Pinned files are re-seeded INSIDE _wipe, under pins_mutation, so the
    # unlink+reload is atomic against a concurrent owner-loop pin write (see
    # _wipe). Stats is wiped HERE, off the loop like its writer: StatsService
    # runs both tick() and reset() via asyncio.to_thread under one lock, so the
    # unlink+reload is serialized with any due flush and a dirty flush cannot
    # rewrite stale counters over the wipe (see StatsService.reset).
    now = _now()
    try:

        if await asyncio.to_thread(rt.stats.reset, now):
            removed.append(STATS_FILE_NAME)
    except Exception:  # pragma: no cover - defensive
        logger.warning("mochi: reset could not reload stats")
    # A reset restores the default pack, so the persona follows it back.
    rt.soul.set_appearance(defaults.get("activeAppearance"))
    _rt()._broadcast("mochi:color-map-changed", defaults)

    return web.json_response({"ok": True, "removed": removed, "settings": defaults})


async def _handle_pack_save_content(request: web.Request) -> web.Response:
    """Save a pack from per-slot animation content — the pack editor's path.

    Distinct from ``POST /packs``, which takes a sprite SHEET plus row
    assignments. Without this route the Avatars window's "Create new" / "Edit"
    flows had nowhere to save to.
    """

    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "expected an object", "code": "invalid_json"}, status=400
        )
    states = body.get("states")
    moods = body.get("moods")
    if not isinstance(states, dict):
        return web.json_response(
            {"error": "states must be an object", "code": "invalid_states"}, status=400
        )
    try:
        # Off the event loop like every other pack write: a custom pack is a
        # file per slot, so a large one is seconds of synchronous I/O that would
        # otherwise stall chat and the heartbeats.
        meta = await asyncio.to_thread(
            save_pack,
            _rt().data_dir,
            body.get("meta") or {},
            states,
            moods if isinstance(moods, dict) else None,
        )
    except PackError as exc:
        return web.json_response(
            {"error": str(exc), "code": "invalid_pack_save_content"}, status=400
        )
    _rt()._broadcast("mochi:gallery-packs-changed", {"packId": meta.get("id")})
    return web.json_response({"ok": True, "meta": meta})
