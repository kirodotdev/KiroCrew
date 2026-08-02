"""Mochi's own MCP server — the tools its agents call, over stdio.

WHY STDIO + FILES, not HTTP to the gateway
------------------------------------------
The original ran these tools inside the Electron main process and exposed them
on its own HTTP MCP port. Neither half of that survives the builtin migration:

* There is no second app to host a port, and hardcoding one is what the original
  got bitten by (a stale default pointing at nothing).
* An app *backend* would be a separate spawned process, but Mochi's autonomous
  core (``MochiRuntime``) lives in the GATEWAY process. Hosting tools in a second
  process would fork the state — two owner loops writing one queue.

So this is a stdio server that talks to the same JSON files the runtime owns.
That is not a workaround, it is how the original coordinated too: every one of
these tools went through ``queueFile`` / ``watchlistFile`` /
``pinnedFilesService``, which are atomic-write file stores. The owner loop
re-reads them every second, so a write here is picked up on the next tick. The
tool descriptions in the original say so outright: *"Actions are queued and
executed by the Electron app."*

Consequences worth knowing:

* No auth, no port discovery, no gateway dependency — the tools work whenever the
  data dir is readable, and cannot be broken by a gateway restart.
* Writes are atomic (``write_queue_atomic`` / ``write_atomic``), so a concurrent
  owner-loop read never observes a torn file.
* Latency to a VISIBLE effect is one owner-loop tick (≤1s), because the runtime,
  not this process, animates the pet.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.pinned_files_service import pins_mutation
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments

logger = logging.getLogger(__name__)

SERVER_NAME = "mochi"
SERVER_VERSION = "1.0.0"

_QUEUE_FILE = qf.QUEUE_FILE
_WATCHLIST_FILE = "mochi-watchlist.json"
_PINNED_FILE = "pinned-files.json"
_ACTIVITY_FILE = "mochi-activity.json"
_ACTIVITY_PREV_FILE = "mochi-activity-yesterday.json"
_STATS_FILE = "stats.json"

# read_mochi_file is deliberately confined to the app's own data dir. The
# original resolved a path under its Application Support dir; the agent must not
# be able to walk out of it via `..` or an absolute path.
#
# ``activity`` is not a convenience: the notification de-duplication rule in
# every notify path ("has the user already been told this?"), the planner's
# user-rhythm derivation, and the companion-milestone check all read the
# activity log, and no other surface exposes it — the log is written by the
# gateway and is never injected into session context. Without it here those
# rules are unexecutable, so the agent re-notifies on every check.
_READABLE = {
    "queue": _QUEUE_FILE,
    "watchlist": _WATCHLIST_FILE,
    "pinned": _PINNED_FILE,
    "activity": _ACTIVITY_FILE,
    "activity-yesterday": _ACTIVITY_PREV_FILE,
    "stats": _STATS_FILE,
}


def _data_dir() -> Path:
    """Mochi's app data dir — the same one the runtime uses.

    The import is deliberately function-local: this module is the stdio MCP
    server, a separate short-lived process, and ``kiro_crew.apps.manager``
    transitively imports the platform context, SEL, and app discovery — none
    of which the other tools need. Importing it at module scope would tax
    every server start with that graph; here only the first data-dir lookup
    pays it.
    """
    from kiro_crew.apps.manager import app_data_dir

    return app_data_dir("mochi")


def _now_ms() -> int:
    return qf._now_ms()


def _ok(payload: Any) -> str:
    return json.dumps(payload, indent=2)


def _err(tool: str, exc: Exception) -> str:
    """Tool errors are returned as text, matching the original's shape.

    A raised exception would surface as a protocol error and give the model
    nothing to act on; the original returned ``"<tool> failed: <reason>"``.
    """
    logger.warning("[mochi-mcp] %s failed: %s", tool, exc)
    # The "Error:" prefix is load-bearing, not cosmetic: `call_tool_with_logging`
    # derives the SEL outcome from it, so without the prefix a failed call would
    # be audited as "completed". The original's "<tool> failed: <reason>" wording
    # is kept after it so the model still gets the same actionable text.
    return f"Error: {tool} failed: {exc}"


# ── Tool schemas ───────────────────────────────────────────────────────────


def _list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_plan",
            "description": (
                "Read Mochi's current behavior queue (planned moves, moods, "
                "notifications) plus planner notes."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "update_plan",
            "description": (
                "Replace or amend the behavior queue.\n"
                "`full_replace` swaps the WHOLE queue — pass the complete "
                "object (planned_at, planned_until, tasks, mood, narrative, "
                "planner_notes, needs_replan). Unexecuted reminder tasks are "
                "carried over for you.\n"
                "Otherwise the change is incremental: `tasks_to_add`, "
                "`tasks_to_remove` (ids), `tasks_to_modify` (objects with an "
                "id), then the metadata keys. Omit a metadata field to leave it "
                "unchanged; pass null to clear it.\n"
                "There is no bare `tasks` parameter — use full_replace to set "
                "the queue, or tasks_to_add to append to it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "full_replace": {
                        "type": "object",
                        "description": "The complete new queue. Wins over the incremental keys.",
                        # read_queue() rejects a queue whose `tasks` is not a list
                        # or whose `planned_at`/`planned_until` are not strings, and
                        # the write is not undoable — so an incomplete full_replace
                        # (e.g. {"tasks": []}) would overwrite and LOSE the current
                        # plan. Require + type the three load-bearing keys here so
                        # the write is refused before it lands. additionalProperties
                        # stays open: the queue carries other planner-written fields.
                        "required": ["planned_at", "planned_until", "tasks"],
                        "additionalProperties": True,
                        "properties": {
                            "planned_at": {"type": "string"},
                            "planned_until": {"type": "string"},
                            "tasks": {"type": "array", "items": {"type": "object"}},
                            "mood": {"type": "string"},
                            "narrative": {"type": "string"},
                            "planner_notes": {"type": "object"},
                            "needs_replan": {"type": "boolean"},
                        },
                    },
                    "tasks_to_add": {"type": "array", "items": {"type": "object"}},
                    "tasks_to_remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids to drop. Unknown ids are skipped.",
                    },
                    "tasks_to_modify": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Partial task objects, each carrying its `id`.",
                    },
                    "mood": {"type": "string"},
                    "narrative": {"type": "string"},
                    # An object, not a string: the planner stores structured
                    # memory here (skipped_tips, user_rhythm,
                    # watching_last_status) and reads it back on the next cycle.
                    "planner_notes": {"type": "object"},
                    "needs_replan": {"type": "boolean"},
                },
            },
        },
        {
            "name": "perform_pet_action",
            "description": (
                "Control the pet: move, notify, change expression, or query "
                "position. Actions are queued and executed by the host app "
                "(coordinated with animations).\n"
                'action "notify" — show a bubble (summary) with optional mood / '
                "sticky / pushToChat / watchItemId. Use pushToChat sparingly and "
                "do NOT also send a separate text reply."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["query", "move", "notify", "mood"],
                        "description": 'What to do: "query", "move", "notify", or "mood".',
                    },
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "summary": {
                        "type": "string",
                        "description": 'Bubble text, max 100 chars (action "notify").',
                    },
                    "chatMessage": {
                        "type": "string",
                        "description": (
                            "Detailed message for chat when pushToChat is set. "
                            "Defaults to summary. No length limit."
                        ),
                    },
                    "mood": {"type": "string"},
                    "sticky": {"type": "boolean"},
                    "pushToChat": {"type": "boolean"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "critical"],
                        "description": (
                            'Notification priority (action "notify"). "critical" '
                            "bypasses the merge window and the user's quiet mode — "
                            "reserve it for time-sensitive alerts (e.g. a meeting "
                            'in 5 minutes). "low" merges quietly and is evicted '
                            "first under pressure. Default: normal."
                        ),
                    },
                    "watchItemId": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": (
                            'Notification source for merge grouping (action "notify"): '
                            '"watch", "reminder", "approval", or "system". '
                            "Default: chat-agent."
                        ),
                    },
                    # Restored from the original tool schema. Without these the
                    # pet could only be sent to a single point: no multi-point
                    # strolls, no tuck-against-an-edge, no appending to a walk in
                    # flight, and no moving to another monitor.
                    "waypoints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                        },
                        "description": 'Multi-point stroll (action "move").',
                    },
                    "behavior": {
                        "type": "string",
                        "enum": ["hide_left", "hide_right", "return"],
                        "description": "Tuck against the left/right edge, or walk back to centre.",
                    },
                    "interrupt": {
                        "type": "boolean",
                        "description": (
                            "Default true: cancel any walk in flight. Pass false to "
                            "APPEND the new waypoints to the current path."
                        ),
                    },
                    "display": {
                        "type": "number",
                        "description": 'Move to this display id first (see action "query").',
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "get_watchlist",
            "description": (
                "Read the watch list: tracked items, their status, and check "
                "history.\n"
                "Items that are done / cancelled / expired are RETURNED HERE "
                "too, for a retention window after they finish — so use this "
                "(not the archive) to find a recently completed item, to "
                "re-watch something, or to avoid re-adding an item the user "
                "cancelled. Pass includeArchive only for older history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "includeArchive": {
                        "type": "boolean",
                        "description": "Also return long-term archived items.",
                    }
                },
            },
        },
        {
            "name": "update_watchlist",
            "description": (
                "Add, update, cancel, or remove watch items. `add` creates "
                "items, `update` patches them by id (status, result, "
                "historyEntry, triggerAt), `cancel` stops watching by id but "
                "keeps the item (so it is not re-added later), `remove` deletes "
                "by id outright.\n"
                "Each item carries `checkIntervalMins` — how often the queue "
                "poller re-checks it. THIS is the item's schedule: never create "
                "a separate cron for a watch item. ALWAYS set checkIntervalMins "
                "explicitly on `add` from the user's stated cadence (daily=1440, "
                "hourly=60, every 30 min=30, weekly=10080). If omitted it "
                "defaults to 10 minutes (aggressive polling), which is almost "
                "never what the user meant by 'daily' or 'hourly'. Floor is 3 "
                "minutes. The value you pass on `add` is remembered as "
                "`baseIntervalMins` — adaptive backoff never re-checks faster "
                "than that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "add": {
                        "type": "array",
                        # Typed, bounded: a mistyped field (e.g. `label: {}`) is
                        # persisted verbatim and then crashes the renderer, so
                        # every client-settable field is constrained here.
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "kind": {"type": "string"},
                                "target": {"type": "string"},
                                "notes": {"type": ["string", "object"]},
                                "triggerCondition": {"type": "string"},
                                "triggerAt": {"type": "string"},
                                "checkIntervalMins": {"type": "number"},
                                "priority": {"type": "string"},
                                "maxWatchDurationHours": {"type": "number"},
                                "notifyOnChange": {"type": "boolean"},
                                "autoComplete": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "update": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "status": {"type": "string"},
                                "lastResult": {"type": "string"},
                                "lastChecked": {"type": "string"},
                                "checkCount": {"type": "number"},
                                "failCount": {"type": "number"},
                                "priority": {"type": "string"},
                                "notes": {"type": ["string", "object"]},
                                "triggerAt": {"type": "string"},
                                "checkIntervalMins": {"type": "number"},
                                # The shipped mochi-watch/poller prompts write
                                # these on every check; omitting them from the
                                # schema (with additionalProperties:false) hard-
                                # rejects the whole call, so nextCheckAfter never
                                # advances and the item re-spawns a check forever.
                                "historyEntry": {"type": "object"},
                                "notified": {"type": "boolean"},
                                "nextCheckAfter": {"type": ["string", "null"]},
                            },
                            "required": ["id"],
                            "additionalProperties": False,
                        },
                    },
                    "cancel": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "pin_file",
            "description": "Pin a file so Mochi tracks it and surfaces changes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "unpin_file",
            "description": "Remove a pinned file.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "read_mochi_file",
            "description": (
                "Read one of Mochi's own data files. Confined to Mochi's data "
                "dir.\n"
                "'queue' / 'watchlist' / 'pinned' — the same state the "
                "get_plan / get_watchlist tools return.\n"
                "'activity' — today's activity log (moves, notifications, "
                "chat, [memory] entries). Read this BEFORE pushing a "
                "notification to check whether the user was already told; it is "
                "the only record of what has been said. "
                "'activity-yesterday' is the previous day's rotated log.\n"
                "'stats' — companion stats (hours together, streaks)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "which": {"type": "string", "enum": sorted(_READABLE)},
                },
                "required": ["which"],
            },
        },
    ]


# ── Tool implementations ───────────────────────────────────────────────────


def _empty_queue(now_ms: int) -> Any:
    """A queue that will survive a read_queue round-trip.

    read_queue REJECTS a queue whose ``planned_at`` / ``planned_until`` are not
    strings (queue_file.py) — it returns None and warns. A bare
    ``{"tasks": []}`` seed therefore writes a file that can never be read back,
    so every update would silently vanish. The window is deliberately zero-length
    (planned_until == planned_at): this is a placeholder, and a real plan window
    is set by the planner via update_plan.
    """
    stamp = qf._iso(now_ms)
    return {"tasks": [], "planned_at": stamp, "planned_until": stamp}


def _tool_get_plan() -> str:
    # read_queue returns None when the file is absent or unreadable — report
    # that as an empty plan rather than leaking a null to the model.
    queue = qf.read_queue(str(_data_dir() / _QUEUE_FILE))
    if queue is None:
        return _ok({"tasks": [], "note": "no plan yet"})
    return _ok(queue)


def _tool_update_plan(args: dict[str, Any]) -> str:
    path = str(_data_dir() / _QUEUE_FILE)
    now = _now_ms()
    # apply_update owns the merge semantics (including "explicit null clears the
    # field", which differs from "key absent = leave alone") — don't re-derive it
    # here.
    # Read-modify-write under the cross-process lock: the gateway's poller
    # mutates the same file (see queue_file.queue_mutation).
    with qf.queue_mutation(path):
        queue = qf.read_queue(path) or _empty_queue(now)
        result = qf.apply_update(queue, args, now)
        updated = result["queue"]
        qf.write_queue_atomic(path, updated)
    out: dict[str, Any] = {"ok": True, "tasks": len(updated.get("tasks", []))}
    # apply_update warns when it drops agent tasks over budget or rejects a bad
    # planned_until. Silently swallowing that would let the planner believe every
    # task it submitted survived.
    if result.get("warning"):
        out["warning"] = result["warning"]
    return _ok(out)


def _tool_perform_pet_action(args: dict[str, Any]) -> str:
    """Queue a pet action for the runtime to execute on its next tick."""
    action = args.get("action")
    path = str(_data_dir() / _QUEUE_FILE)
    now = _now_ms()

    if action == "query":
        # Monitors are visible only to the Electron shell, so the pet window posts
        # its display list to the backend on every displays-info event and this
        # reads that cache. Upstream answered from `screen.getAllDisplays()`
        # directly, which is not reachable from a Python MCP server.
        cached: dict[str, Any] = {}
        try:
            cached = json.loads((_data_dir() / "mochi-displays.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        displays = cached.get("displays", [])
        active_id = cached.get("activeId")
        current = next(
            (d for d in displays if isinstance(d, dict) and d.get("id") == active_id),
            None,
        )
        return _ok(
            {
                "displays": displays,
                "currentDisplay": active_id,
                # Spelled out rather than left for the caller to join: the pet was
                # reporting the wrong monitor because an id alone says nothing
                # about WHICH screen that is, so the answer was being guessed.
                "currentDisplayIndex": (current or {}).get("index"),
                "currentDisplayIsPrimary": (current or {}).get("primary"),
                "note": (
                    "no display data yet — the pet window reports it once open"
                    if not cached
                    else "`index` is 1-based in the order the OS lists monitors — "
                    "that is the number the user means by 'screen 2'. Never infer "
                    "the screen from anything else; if index is missing, say you "
                    "do not know rather than guessing."
                ),
            }
        )

    # Field-for-field the shape the poller consumes. Three of these are NOT
    # cosmetic:
    #   execute_after — get_executable_tasks() treats a missing/unparseable value
    #     as not-due (mirroring the original's `NaN <= now` being false), so a
    #     task without it sits in the queue forever and the pet never moves;
    #   id            — the poller marks a task done by id, so an id-less task
    #     would re-execute on every poll;
    #   urgent        — exempts the task from the stale-skip that otherwise marks
    #     an overdue planned move done-unexecuted.
    # The original also poked its poller in-process (onUrgentTask). This MCP
    # server is a SEPARATE process and cannot, but the runtime's owner loop polls
    # every second, so the action lands on the next tick.
    task: dict[str, Any] = {
        # Timestamp + UUID suffix: a bare `pa{now}` collides when a foreground
        # and a background action are queued in the same millisecond — the poller
        # marks the first id-match done and the duplicate re-executes every tick.
        "id": f"pa{now}-{uuid.uuid4().hex[:8]}",
        "type": action,
        "at": qf._iso(now),
        "done": False,
        "execute_after": qf._iso(now),
        "urgent": True,
        "source": str(args.get("source") or "chat-agent"),
    }
    for key in (
        "x",
        "y",
        "summary",
        "chatMessage",
        "mood",
        "sticky",
        "pushToChat",
        "priority",
        "watchItemId",
        "waypoints",
        "behavior",
        "interrupt",
        "display",
    ):
        if key in args:
            task[key] = args[key]

    # Append under the cross-process lock — an append landing inside the
    # poller's read/write window would otherwise be dropped by its replace.
    with qf.queue_mutation(path):
        queue = qf.read_queue(path) or _empty_queue(now)
        tasks = list(queue.get("tasks", []))
        tasks.append(task)
        queue["tasks"] = tasks
        qf.write_queue_atomic(path, queue)
    return _ok({"ok": True, "queued": action})


def _tool_get_watchlist(args: dict[str, Any]) -> str:
    d = _data_dir()
    now = _now_ms()
    out: dict[str, Any] = {"watchlist": wf.read_watchlist(str(d / _WATCHLIST_FILE), now_ms=now)}
    if args.get("includeArchive"):
        out["archive"] = wf.read_archive(str(d / "mochi-watchlist-archive.json"), now_ms=now)
    return _ok(out)


def _tool_update_watchlist(args: dict[str, Any]) -> str:
    path = str(_data_dir() / _WATCHLIST_FILE)
    now = _now_ms()
    # Read-modify-write under the cross-process lock: this process is one of three
    # writers (gateway watchlist service, dashboard routes), and without the lock a
    # concurrent archive sweep writes its stale snapshot back over the item that was
    # just added here.
    with wf.watchlist_mutation(path):
        result = wf.apply_watchlist_update(wf.read_watchlist(path, now_ms=now), args, now_ms=now)
        wf.write_atomic(path, {"version": 1, "items": result["items"]})
    return _ok({"ok": True, "items": len(result["items"])})


def _tool_pin_file(args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "Error: pin_file failed: path is required"
    # A pin is not passive: the runtime POLLS every pinned path and surfaces its
    # `updatedAt`. Pinning `~/.ssh/id_rsa` therefore turns the pin list into a
    # side channel that reports when a credential file changed, without ever
    # reading it — so the path has to clear the same gate every other file surface
    # uses (hooks.on_tool_call, validate_file_path, artifacts, knowledge indexing)
    # rather than a check of its own.
    from kiro_crew.security import is_sensitive_path

    if is_sensitive_path(path):
        return "Error: pin_file failed: that path is not allowed"
    pins_path = _data_dir() / _PINNED_FILE
    # Under the shared lock: the gateway's PinnedFilesService persists its WHOLE
    # in-memory list, so a disk-only write from this process was deleted by its
    # next mutation. The service reloads from disk inside the same lock, which is
    # what makes the two writers agree.
    with pins_mutation(str(pins_path)):
        data = _read_json(pins_path, {"version": 1, "pins": []})
        pins = data.get("pins", [])
        if any(p.get("path") == path for p in pins):
            return _ok({"ok": True, "alreadyPinned": True})
        entry = {"path": path, "pinnedAt": _now_ms()}
        if args.get("label"):
            entry["label"] = args["label"]
        pins.append(entry)
        data["pins"] = pins
        _write_json(pins_path, data)
    return _ok({"ok": True, "pins": len(pins)})


def _tool_unpin_file(args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "Error: unpin_file failed: path is required"
    pins_path = _data_dir() / _PINNED_FILE
    with pins_mutation(str(pins_path)):
        data = _read_json(pins_path, {"version": 1, "pins": []})
        before = data.get("pins", [])
        after = [p for p in before if p.get("path") != path]
        data["pins"] = after
        _write_json(pins_path, data)
    return _ok({"ok": True, "removed": len(before) - len(after)})


def _tool_read_mochi_file(args: dict[str, Any]) -> str:
    which = args.get("which")
    filename = _READABLE.get(which) if isinstance(which, str) else None
    if not filename:
        return (
            f"read_mochi_file failed: unknown file {which!r} (expected one of {sorted(_READABLE)})"
        )
    target = _data_dir() / filename
    if not target.is_file():
        return _ok({"exists": False, "which": which})
    return target.read_text(encoding="utf-8")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    """Atomic write with owner-only mode, matching the services' own persist."""
    from kiro_crew.atomic_write import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n", mode=0o600)


_DISPATCH = {
    "get_plan": lambda a: _tool_get_plan(),
    "update_plan": _tool_update_plan,
    "perform_pet_action": _tool_perform_pet_action,
    "get_watchlist": _tool_get_watchlist,
    "update_watchlist": _tool_update_watchlist,
    "pin_file": _tool_pin_file,
    "unpin_file": _tool_unpin_file,
    "read_mochi_file": _tool_read_mochi_file,
}


def _validate_args(name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
    """Check arguments against the declared inputSchema before dispatch.

    The arguments arrive from outside this process, and the handlers persist them
    (queue / watchlist files the poller then executes every cycle) — an
    unvalidated malformed write would wedge every subsequent poll, not just this
    call. Fail-closed: unknown keys and undeclared schemas reject (see
    validate_mcp_tool_arguments), so every accepted argument is one the schema
    names.
    """
    args = raw_args or {}
    try:
        validate_mcp_tool_arguments(args, _INPUT_SCHEMAS.get(name))
    except ValidationError as exc:
        # Re-raise naming the tool. The shared helper renders a ValidationError as
        # `Error: <message>`, and a bare `arguments.which: not in enum` does not say
        # WHICH call produced it — the model gets one line of text and nothing else
        # to correlate. Keeps the original's "<tool> failed: <reason>" wording.
        raise ValidationError(f"{name} failed", str(exc)) from exc
    return args


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"Error: Unknown tool: {name}"
    try:
        return fn(args or {})
    except Exception as exc:  # noqa: BLE001 — a tool error must not kill the server
        return _err(name, exc)


def _call_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch one tool call, audited.

    Routed through the shared helper rather than calling the handler directly, so
    every outcome lands in the SEL audit log. This server's tools are
    auto-approved for the app's own agent, which means the PreToolUse gate — the
    other place a tool call is recorded — never sees them: without this, a
    successful mutation (a queue write, a watch item added, a pet action) left no
    trace at all, and only the crash path was visible in the gateway log.
    """
    return call_tool_with_logging(
        name,
        args,
        _validate_args,
        _call_tool_inner,
        session_key=f"mcp_{SERVER_NAME}",
        downstream_service=f"kirocrew-{SERVER_NAME}",
    )


#: name -> declared inputSchema, built once from the same list `tools/list`
#: advertises so the schema enforced is byte-identical to the schema published.
_INPUT_SCHEMAS: dict[str, Any] = {t["name"]: t.get("inputSchema") for t in _list_tools()}


def run_mcp_server() -> None:
    """Run the stdio MCP server. Entry point for ``kirocrew app mcp mochi``."""
    # Keep logging off stdout — stdout is the JSON-RPC channel.
    logging.basicConfig(level=os.environ.get("KIROCREW_LOG_LEVEL", "WARNING"), stream=None)
    run_mcp_stdio_loop(SERVER_NAME, SERVER_VERSION, _list_tools, _call_tool)


if __name__ == "__main__":  # pragma: no cover — process entry point
    run_mcp_server()
