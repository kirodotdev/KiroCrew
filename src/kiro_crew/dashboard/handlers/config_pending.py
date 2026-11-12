"""Pending keystone changes: the Settings review of a hand-edited sealed file.

Where the Linux namespace sandbox seals ``config.json``, ``config.local.json``,
``denied_commands.json`` and ``computer_use.json``, a file whose on-disk bytes differ
from its gateway-endorsed copy keeps loading as the endorsed copy and is PENDING
(:func:`kiro_crew.config.loosening_provenance.pending_changes`). These routes let the
owner see the changed keys and decide:

* ``GET  /api/security/config-pending`` -- the pending leaves and their key diffs;
* ``POST /api/security/config-pending/{leaf}/apply`` -- endorse the on-disk version;
* ``POST /api/security/config-pending/{leaf}/discard`` -- restore the endorsed copy.

Apply and discard name the reviewed version (``{"signature": ...}``) and refuse with
409 when the file changed since, so a different edit can never ride the owner's click.
Discard checks only that version, so an edit that is not valid JSON, or a file that was
deleted (signature ``removed``), can always be discarded; Apply of a deleted file is
``409 pending_removed``.
Both go through the leaf's own endorsing writer (``update_config_locked``, the
denied-command saver, ``enable_state.save_state``), never a hand-rolled write, and are
the only callers that pass ``resolving_pending=True``: every other save of a pending
file is refused (``loosening_provenance.refuse_save_if_pending``). Apply first runs
the validation that leaf's normal save runs: the config publish
floor (inside ``update_config_locked``), the denied-command routes' rules
(``security.denied_edit_problem``) and the computer-use PUT's field checks
(``computer_use.keystone_patch``). Content one of them refuses is ``409
write_refused`` with the plain reason, and the file stays pending. A resolve that
changes whether computer use is on then rebuilds the agent spec and resets sessions,
as the computer-use PUT does (``computer_use.apply_enable_change``).

Owner only: every route calls ``require_owner_dashboard_request`` first, like the
other Settings > Security writes, and the POSTs sit behind the dashboard's CSRF
barrier (they are not in ``token_auth.CSRF_EXEMPT_EXACT_METHODS``). An app token or
an ``X-Internal-Secret`` caller (an agent's MCP tools) is not the owner, and no
agent or MCP tool names these routes, so a sandboxed process cannot approve its own
edit.

:func:`sync_pending_notes` rides the event-loop heartbeat: a leaf that goes pending
raises one bell notification per on-disk version (re-notified only for a new edit) on
the channel users cannot mute (:data:`CHANNEL`), and a resolved leaf's note is
acknowledged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew.config import loosening_provenance as lp

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")

OP_READ = "security.config_pending.read"
OP_APPLY = "security.config_pending.apply"
OP_DISCARD = "security.config_pending.discard"

#: Where the bell note sends the owner: Settings > Security at the pending card.
SETTINGS_URL = "/settings/security?highlight=security.config-pending"
#: Owner-only WebSocket event carrying ``{"count": n}`` whenever the pending set changes:
#: a leaf goes pending or is resolved, or a pending leaf is edited again (a new
#: signature at the same count). The frontend re-reads the list on every one.
WS_EVENT = "config_pending_changed"
#: Internal note key ``{"leaf", "signature"}``. Underscore-prefixed, so a pushed note's
#: meta can never set it (``NotificationBus.push``) and a forged note cannot suppress
#: the real one.
NOTE_KEY = "_config_pending"
#: The channel users cannot mute (``notifications.settings.PROTECTED_CHANNELS``): the
#: edit is not in effect until the owner decides, so the note is that approval's prompt
#: and must not be silenced. ``kind`` stays ``agent``, so the bell links to the card
#: rather than offering a tool-approval review.
CHANNEL = "system.approval"
#: Heartbeat ticks every 5 s; the on-disk check runs at most this often.
_SYNC_INTERVAL_SECS = 30.0
_SYNC_TIMEOUT_SECS = 2.0
_NEXT_SYNC_ATTR = "_config_pending_next_sync"
_SYNC_TASK_ATTR = "_config_pending_sync_task"
_PUBLISHED_ATTR = "_config_pending_published"
#: Set while a ``resolved`` mark is only in memory: its log rewrite failed, so the
#: next sync writes again rather than leave a restart to read the old record.
_RESOLVED_UNSAVED_ATTR = "_config_pending_resolved_unsaved"


def _snapshot() -> dict:
    """``{enforcing, count, pending}``. Blocking (the file reads):
    call it off the event loop."""
    pending = lp.pending_changes()
    return {"enforcing": lp.enforcement_active(), "count": len(pending), "pending": pending}


async def _gate(request: web.Request, op: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, op)


def _audit(request: web.Request, op: str, outcome: str, resources: str) -> None:
    from kiro_crew.dashboard.handlers.security import _audit as audit

    audit(request, operation=op, outcome=outcome, resources=resources)


async def api_config_pending_list(request: web.Request) -> web.Response:
    """GET /api/security/config-pending -- ``{enforcing, count, pending}``."""
    denied = await _gate(request, OP_READ)
    if denied is not None:
        return denied
    return web.json_response(await asyncio.to_thread(_snapshot))


# ── writes: each leaf's own endorsing writer ──


def _config_writer(leaf: str, apply: bool, signature: str) -> Callable[[], None]:
    from kiro_crew.config.loader import update_config_locked
    from kiro_crew.config.paths import config_dir

    def _write() -> None:
        def _mutate(endorsed: dict) -> dict:
            # Under the config.json sidecar lock: the version checked is the one written.
            if apply:
                return lp.read_pending_disk(leaf, signature)
            lp.check_pending(leaf, signature)
            return endorsed

        # Only config.json carries the meta block: stamping config.local.json would
        # write bytes other than the ones the owner reviewed.
        update_config_locked(
            config_dir() / leaf,
            mutate=_mutate,
            stamp_meta=leaf == lp.LEAF_CONFIG,
            resolving_pending=True,
        )

    return _write


async def _write_config(leaf: str, apply: bool, signature: str) -> None:
    from kiro_crew.dashboard.chat_utils import run_config_write

    await run_config_write(_config_writer(leaf, apply, signature))


async def _write_denied(request: web.Request, apply: bool, signature: str) -> None:
    from kiro_crew.config.loader import ConfigWriteRefused
    from kiro_crew.dashboard.handlers import security

    def _mutate(endorsed: dict) -> None:
        if not apply:
            lp.check_pending(lp.LEAF_DENIED_COMMANDS, signature)
            return
        disk = lp.read_pending_disk(lp.LEAF_DENIED_COMMANDS, signature)
        # The save routes' own rules, on what the edit changes.
        reason = security.denied_edit_problem(endorsed, disk)
        if reason is not None:
            raise ConfigWriteRefused(reason)
        endorsed.clear()
        endorsed.update(disk)

    state = await security._write_denied_state(_mutate, resolving_pending=True)
    security._reload_live_hooks(request, state)


async def _write_computer_use(request: web.Request, apply: bool, signature: str) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.config.loader import ConfigWriteRefused
    from kiro_crew.dashboard.handlers import computer_use as cu
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.user_json import strict_json_equal

    def _write() -> None:
        if not apply:
            lp.check_pending(lp.LEAF_COMPUTER_USE, signature)
            enable_state.save_state(cu._read_state_strict(), resolving_pending=True)
            return
        disk = lp.read_pending_disk(lp.LEAF_COMPUTER_USE, signature)
        endorsed = cu._read_state_strict()
        # The PUT's own validation, on the keys the edit changed: an unchanged
        # value is the one already in force, as a PUT that does not send it.
        # Type-strict, so ``1`` where ``true`` is in force is a change the PUT judges.
        changed = {
            k: v
            for k, v in disk.items()
            if k not in endorsed or not strict_json_equal(endorsed[k], v)
        }
        try:
            patch = cu.keystone_patch(changed)
        except cu.KeystoneFieldRefused as exc:
            raise ConfigWriteRefused(exc.error) from None
        enable_state.save_state({**disk, **patch}, resolving_pending=True)

    async with _get_config_lock():
        enabled_before = await asyncio.to_thread(enable_state.is_enabled)
        await asyncio.to_thread(_write)
        enabled_after = await asyncio.to_thread(enable_state.is_enabled)
    if enabled_after != enabled_before:
        # The same spec rebuild and session reset as the computer-use PUT, after
        # the lock is released (it takes the lock again).
        await cu.apply_enable_change(request)


async def _resolve(request: web.Request, op: str, apply: bool) -> web.Response:
    denied = await _gate(request, op)
    if denied is not None:
        return denied
    leaf = request.match_info["leaf"]
    if leaf not in lp.PROTECTED_LEAVES:
        _audit(request, op, "denied", "unknown_leaf")
        return web.json_response({"error": "unknown file", "code": "unknown_leaf"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = None
    signature = body.get("signature") if isinstance(body, dict) else None
    if not isinstance(signature, str) or not signature:
        _audit(request, op, "denied", f"{leaf}=bad_signature")
        return web.json_response(
            {"error": "signature must be a non-empty string", "code": "invalid_signature"},
            status=400,
        )
    from kiro_crew.config.loader import ConfigReadError, ConfigWriteRefused
    from kiro_crew.dashboard.handlers.computer_use import StateCorruptError
    from kiro_crew.dashboard.handlers.security import ConfigCorruptError

    try:
        if leaf == lp.LEAF_DENIED_COMMANDS:
            await _write_denied(request, apply, signature)
        elif leaf == lp.LEAF_COMPUTER_USE:
            await _write_computer_use(request, apply, signature)
        else:
            await _write_config(leaf, apply, signature)
    except lp.PendingChanged:
        _audit(request, op, "denied", f"{leaf}=changed")
        return web.json_response(
            {"error": "the file changed since it was reviewed", "code": "pending_changed"},
            status=409,
        )
    except lp.PendingInvalid:
        _audit(request, op, "denied", f"{leaf}=invalid")
        return web.json_response(
            {"error": "the edited file is not valid JSON", "code": "pending_invalid"}, status=409
        )
    except lp.PendingRemoved:
        _audit(request, op, "denied", f"{leaf}=removed")
        return web.json_response(
            {"error": "the file was removed; discard to put it back", "code": "pending_removed"},
            status=409,
        )
    except ConfigWriteRefused as exc:
        _audit(request, op, "denied", f"{leaf}=refused")
        return web.json_response({"error": str(exc), "code": "write_refused"}, status=409)
    except (ConfigReadError, ConfigCorruptError, StateCorruptError, OSError):
        logger.warning("config-pending %s of %s failed", op, leaf, exc_info=True)
        _audit(request, op, "error", f"{leaf}=unwritable")
        return web.json_response(
            {"error": "the settings file could not be written", "code": "write_failed"},
            status=500,
        )
    _audit(request, op, "ok", leaf)
    snapshot = await asyncio.to_thread(_snapshot)
    await _publish(request.app["state"], snapshot["pending"])
    return web.json_response(snapshot)


async def api_config_pending_apply(request: web.Request) -> web.Response:
    """POST /api/security/config-pending/{leaf}/apply -- ``{"signature": str}``."""
    return await _resolve(request, OP_APPLY, apply=True)


async def api_config_pending_discard(request: web.Request) -> web.Response:
    """POST /api/security/config-pending/{leaf}/discard -- ``{"signature": str}``."""
    return await _resolve(request, OP_DISCARD, apply=False)


# ── bell note + nav count ──


def _notes(state: Any) -> list[tuple[dict, dict]]:
    found = []
    for note in getattr(state, "_notification_log", ()):
        record = note.get(NOTE_KEY)
        if isinstance(record, dict):
            found.append((note, record))
    return found


async def _publish(state: Any, pending: list[dict]) -> None:
    """Notify each newly pending version once; acknowledge resolved ones; push a change."""
    from kiro_crew.notifications.bus import NotificationPayload

    current = {p["leaf"]: p["signature"] for p in pending}
    notes = _notes(state)
    # A resolved note (applied, discarded or put back by hand) answers for a version
    # that STOPPED being pending: the same bytes going pending again later -- a file
    # removed twice, a script re-applying one edit -- is a new event and is notified.
    seen = {(r.get("leaf"), r.get("signature")) for _, r in notes if not r.get("resolved")}
    # Versions this process already announced: the owner can delete a note and the log
    # cap can drop one, and neither may bring the same version's note back.
    published = getattr(state, _PUBLISHED_ATTR, None) or {}
    added = False
    for leaf, signature in current.items():
        if (leaf, signature) in seen or published.get(leaf) == signature:
            continue
        note = state.notification_bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                # Not the channel's critical default: the edit blocks no agent turn.
                # A user override on the channel can still raise it.
                priority="default",
                kind="agent",
                title="Your config was changed outside Kiro Crew",
                body=f"{leaf} was edited outside Kiro Crew. Review to apply or discard it.",
                url=SETTINGS_URL,
                group_key=f"config-pending:{leaf}",
                meta={"config_pending_leaf": leaf},
            )
        )
        note[NOTE_KEY] = {"leaf": leaf, "signature": signature}
        added = True
    if added:
        # The delivery already queued its row; persist the record so a restart
        # does not notify the same version again.
        await state._rewrite_notifications_async()
    resolved = False
    for note, record in notes:
        if current.get(record.get("leaf")) == record.get("signature"):
            continue
        if not record.get("resolved"):
            # Marked even on a note the owner already read, so its version can be
            # notified again if it goes pending again.
            record["resolved"] = True
            resolved = True
        if note.get("acked"):
            continue
        ts = note.get("ts")
        if isinstance(ts, str):
            await state.ack_notification(ts)
    if resolved or getattr(state, _RESOLVED_UNSAVED_ATTR, False):
        saved = await state._rewrite_notifications_async()
        setattr(state, _RESOLVED_UNSAVED_ATTR, saved is False)
    if getattr(state, _PUBLISHED_ATTR, None) != current:
        setattr(state, _PUBLISHED_ATTR, current)
        state.broadcast_ws_owners(WS_EVENT, {"count": len(current)})


async def _sync(state: Any) -> None:
    await _publish(state, await asyncio.to_thread(lp.pending_changes))


async def sync_pending_notes(state: Any, now: float) -> None:
    """Heartbeat hook: check the sealed files at most every 30 s. Never raises.

    The scan AND the note writes are bounded by :data:`_SYNC_TIMEOUT_SECS`, so a stalled
    filesystem cannot hold the heartbeat. A sync that runs past it keeps running
    (shielded, so a note's persistence is not cut off half-done) and no new one starts
    until it ends.
    """
    try:
        if now < getattr(state, _NEXT_SYNC_ATTR, 0.0):
            return
        task = getattr(state, _SYNC_TASK_ATTR, None)
        if task is not None and not task.done():
            return
        setattr(state, _NEXT_SYNC_ATTR, now + _SYNC_INTERVAL_SECS)
        task = asyncio.ensure_future(_sync(state))
        setattr(state, _SYNC_TASK_ATTR, task)
        await asyncio.wait_for(asyncio.shield(task), _SYNC_TIMEOUT_SECS)
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug("config-pending sync still running; the heartbeat moves on")
    except Exception:
        logger.debug("config-pending sync failed", exc_info=True)
