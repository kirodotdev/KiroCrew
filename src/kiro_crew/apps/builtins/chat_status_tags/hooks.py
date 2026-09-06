"""Lifecycle hooks — the health-tag and auto-resume loops.

Two background tasks, started on enable:

* **health** (every 60 s) — seeds the tag vocabulary once, then keeps the
  ``stuck`` / ``network`` / ``error`` health tags on every slot in sync with
  its live state. Zero tokens: pure polling and tag writes.
* **auto-resume** (every 60 s) — finds idle chats whose latest message is a
  NETWORK-class terminal error card and injects "Continue" once the network
  has held stable for a full minute. Auth and unclassified errors are left
  tagged for the human. Capped per failure episode so a chat whose backend
  keeps dying can never trigger a resume storm. Deliberately silent: the
  resume is self-evident in the chat itself, and notifying per resume floods
  the bell on a flapping connection.

Both loops reach the gateway's own chat tags/slots state IN-PROCESS (see
``store.py``) — they run inside the gateway, so they read the live
``DashboardState`` directly instead of dialing their own HTTP surface (an
earlier HTTP transport 403'd on every cycle: a loop cannot authenticate to the
process it runs in). The decision logic stays identical to what an external
packaging would run from cron scripts. Every blocking call is pushed off the
event loop with ``asyncio.to_thread`` — the gateway must never stall on its own
tagger — and each in-process WRITE is marshalled back onto the serving loop by
``store.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any

from kiro_crew.apps.builtins.chat_status_tags import logic, settings
from kiro_crew.apps.builtins.chat_status_tags.store import (
    GatewayUnavailable,
    TagsStore,
)

logger = logging.getLogger(__name__)

_HEALTH_INTERVAL_SECS = 60
_RESUME_INTERVAL_SECS = 60
_DETAIL_TAIL = 6  # last few messages suffice to spot a terminal error card

#: The scheduler stores this app's reconcile cron under the app-namespaced name
#: (``{app}/{resource}``), so the on-disk job is named with this prefix, NOT the
#: bare manifest name. Kept in sync with ``backend/routes.py``.
_RECONCILE_JOB_NAME = "chat-status-tags/sdlc-tag-reconcile"

# Connectivity probes for the resume stability gate: the model backend plus
# a control endpoint. Both must accept a TCP connect. Override per install
# via the app config key ``probe_hosts`` (list of "host:port" strings).
_DEFAULT_PROBES = (("q.us-east-1.amazonaws.com", 443), ("checkip.amazonaws.com", 443))
_STABLE_SECS = 60
_PROBE_EVERY = 20
_PROBE_TIMEOUT = 3.0

_RESUME_TEXT = "Continue"
_STATE_FILE = "auto_resume_state.json"

_tasks: list[asyncio.Task[None]] = []


# ── health loop ──────────────────────────────────────────────────────────


def _seed_vocabulary(client: TagsStore) -> dict[str, str]:
    """Ensure the full tag vocabulary exists; return the canonical name→id map.

    Creation is idempotent by case-insensitive name, and ids are
    server-assigned — they MUST be read back, never assumed. The map is keyed
    by ``name.strip().lower()`` on BOTH the initial scan and the create
    read-back: a pre-existing user tag like ``"Error"`` makes ``create_tag``
    return that tag verbatim with its original casing, and a verbatim-keyed
    map would then miss the lowercase name the sweep looks up — a silent
    KeyError that would stop health tagging on that install for good.
    """

    def _canon(name: object) -> str:
        return str(name or "").strip().lower()

    have = {_canon(t["name"]): t["id"] for t in client.list_tags()}
    for name in logic.STATUS_ORDER:
        if name not in have:
            tag = client.create_tag(name, logic.TAG_COLORS[name], status=True)
            have[_canon(tag["name"])] = tag["id"]
    for name in logic.HEALTH_TAGS:
        if name not in have:
            tag = client.create_tag(name, logic.TAG_COLORS[name], status=False)
            have[_canon(tag["name"])] = tag["id"]
    return have


def _health_pass(client: TagsStore, stuck_min: int) -> list[str]:
    """One health sweep. Returns a change log (empty = nothing to do)."""
    ids = _seed_vocabulary(client)
    managed = {ids[n] for n in logic.HEALTH_TAGS}
    now = datetime.now(timezone.utc)
    changes: list[str] = []

    for slot in client.list_slots():
        cur = slot.get("tags") or []
        stuck = logic.is_stuck(slot, now, stuck_min)
        error_class = ""
        if not slot.get("running") and (
            any(t in managed for t in cur) or logic.is_recent(slot, now)
        ):
            try:
                msgs = client.slot_messages(slot["key"], _DETAIL_TAIL)
            except Exception:
                continue
            error_class = logic.latest_error_class(msgs)

        want_names = logic.desired_health_tags(stuck=stuck, error_class=error_class)
        want_ids = {ids[n] for n in want_names}
        # Snapshot comparison only decides whether a write is worth attempting;
        # the authoritative merge happens INSIDE the store's write lock against
        # the live tag list, so a user edit landing between this read and the
        # write is preserved (merge_slot_tags re-checks and no-ops when the
        # live state already matches).
        if set(logic.merge_tags(cur, managed, want_ids)) != set(cur):
            if client.merge_slot_tags(slot["key"], managed, want_ids):
                changes.append(f"{slot['key']}->{'+'.join(sorted(want_names)) or 'clear'}")
    return changes


async def _health_loop(ctx: Any) -> None:
    client = TagsStore()
    try:
        stuck_min = int(ctx.config.get("stuck_min") or logic.DEFAULT_STUCK_MIN)
    except (TypeError, ValueError):
        # A malformed value (e.g. "30m") must degrade to the default, not raise
        # before the loop's exception boundary and silently kill health tagging.
        logger.warning(
            "chat-status-tags: invalid stuck_min %r — using default %d",
            ctx.config.get("stuck_min"),
            logic.DEFAULT_STUCK_MIN,
        )
        stuck_min = logic.DEFAULT_STUCK_MIN
    while True:
        try:
            changes = await asyncio.to_thread(_health_pass, client, stuck_min)
            if changes:
                logger.info("chat-status-tags health: %s", " | ".join(changes))
        except GatewayUnavailable as exc:
            logger.debug("chat-status-tags health skipped: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("chat-status-tags health pass failed", exc_info=True)
        await asyncio.sleep(_HEALTH_INTERVAL_SECS)


# ── auto-resume loop ─────────────────────────────────────────────────────


def _probe_hosts(ctx: Any) -> tuple[tuple[str, int], ...]:
    raw = ctx.config.get("probe_hosts") or []
    out: list[tuple[str, int]] = []
    for item in raw:
        host, _, port = str(item).rpartition(":")
        try:
            out.append((host, int(port)))
        except ValueError:
            continue
    return tuple(out) or _DEFAULT_PROBES


def _network_up(probes: tuple[tuple[str, int], ...]) -> bool:
    """True only if EVERY probe endpoint accepts a TCP connection."""
    for host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
                pass
        except OSError:
            return False
    return True


def _load_episodes(ctx: Any) -> dict[str, logic.Episode]:
    try:
        raw = json.loads((ctx.data_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        k: logic.Episode(last_ts=v.get("last_ts", ""), attempts=int(v.get("attempts", 0)))
        for k, v in raw.items()
        if isinstance(v, dict)
    }


def _save_episodes(ctx: Any, episodes: dict[str, logic.Episode]) -> None:
    try:
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {k: {"last_ts": v.last_ts, "attempts": v.attempts} for k, v in episodes.items()}
        (ctx.data_dir / _STATE_FILE).write_text(json.dumps(payload))
    except OSError:
        logger.warning("chat-status-tags: could not persist resume state", exc_info=True)


def _find_resume_candidates(
    client: TagsStore, episodes: dict[str, logic.Episode]
) -> list[tuple[str, logic.Episode]]:
    """Idle slots whose latest message is a network-class error, under cap."""
    cands: list[tuple[str, logic.Episode]] = []
    for slot in client.list_slots():
        key = slot["key"]
        if slot.get("running") or slot.get("queue_depth"):
            continue  # already working / already has queued input
        try:
            msgs = client.slot_messages(key, _DETAIL_TAIL)
        except Exception:
            continue
        if logic.latest_error_class(msgs) != "network":
            continue
        # Key the episode on the failure run's ANCHOR, not the slot's last_ts:
        # our own injected "Continue" and each fresh error card advance last_ts,
        # so keying on it would grant a fresh attempt budget after every failed
        # resume and unbound the cap.
        anchor = logic.failure_anchor(msgs, _RESUME_TEXT)
        ep = logic.next_episode(episodes.get(key), anchor)
        episodes[key] = ep
        if logic.may_resume(ep):
            cands.append((key, ep))
    return cands


async def _resume_loop(ctx: Any) -> None:
    client = TagsStore()
    probes = _probe_hosts(ctx)
    while True:
        await asyncio.sleep(_RESUME_INTERVAL_SECS)
        try:
            # Read the toggle INSIDE the loop, every cycle, so disabling
            # auto-resume from the app page takes effect within one interval
            # (~60s) with no gateway restart. When off, the loop does NO work:
            # it never lists slots, never probes the network, and never resumes
            # or tags anything — it just idles cheaply until re-enabled.
            if not (await asyncio.to_thread(settings.get_flags)).get("auto_resume_enabled", True):
                continue

            episodes = _load_episodes(ctx)
            cands = await asyncio.to_thread(_find_resume_candidates, client, episodes)
            if not cands:
                continue

            # Stability gate: the link must hold for a full window of
            # consecutive up-probes. Any single down probe aborts this tick,
            # so a dead-zone flap can never fire a resume mid-reconnect.
            stable = True
            checks = max(1, _STABLE_SECS // _PROBE_EVERY)
            for i in range(checks):
                if not await asyncio.to_thread(_network_up, probes):
                    stable = False
                    break
                if i < checks - 1:
                    await asyncio.sleep(_PROBE_EVERY)
            if not stable:
                continue

            resumed: list[str] = []
            for key, ep in cands:
                try:
                    await asyncio.to_thread(client.send_message, key, _RESUME_TEXT)
                except Exception:
                    logger.warning("chat-status-tags: resume of %s failed", key, exc_info=True)
                    continue
                ep.attempts += 1
                resumed.append(f"{key}#{ep.attempts}")
            _save_episodes(ctx, episodes)
            if resumed:
                # Log line only — silent by design (see module docstring).
                logger.info(
                    "chat-status-tags auto-resumed %d chat(s) after %ds stable network: %s",
                    len(resumed),
                    _STABLE_SECS,
                    ", ".join(resumed),
                )
        except GatewayUnavailable as exc:
            logger.debug("chat-status-tags resume skipped: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("chat-status-tags resume pass failed", exc_info=True)


# ── lifecycle ────────────────────────────────────────────────────────────


async def _sync_reconcile_prompt(ctx: Any) -> None:
    """Push the stored reconcile prompt into the live cron job's ``message``.

    ``register_app_crons_with_service`` rebuilds this app's crons from the
    IMMUTABLE manifest on every gateway startup and on any heal, so the job's
    message is reset to the manifest default each time. Without this sync a
    custom prompt an operator saved through the app page would be silently
    clobbered back to the default on the next restart. Re-applying the stored
    prompt here (after the crons have been (re)registered for this boot) makes a
    customization durable across restarts and self-heals.

    Owner-scoped through ``ctx.cron`` (a :class:`CronSDK`), so it can only touch
    this app's own job. A no-cron install (``ctx.cron is None``) or a missing job
    is a silent no-op — the file remains the store of record and the next
    save/repair will sync it.
    """
    sdk = getattr(ctx, "cron", None)
    if sdk is None:
        return
    prompt = await asyncio.to_thread(settings.get_prompt)
    try:
        jobs = sdk.list_jobs()
        # An unreadable store loads as an EMPTY list without raising, and an
        # absent job routes AROUND the write below — so probe the store right
        # after the read, or this sync would report success over a corrupt
        # store (see test_cron_store_unreadable_boundaries). The service handle
        # is duck-typed: CronSDK exposes no passthrough, so reach the probe on
        # the underlying service it wraps.
        probe = getattr(sdk, "_cron", sdk)
        probe.raise_if_store_unreadable()
        job = next(
            (j for j in jobs if getattr(j, "name", "") == _RECONCILE_JOB_NAME),
            None,
        )
        if job is not None:
            await sdk.update_job_async(job.id, message=prompt)
    except Exception:  # noqa: BLE001 — a sync failure must not block the loops
        logger.warning("chat-status-tags: reconcile-prompt sync to cron failed", exc_info=True)


async def on_startup(ctx: Any) -> None:
    """Start both loops. Idempotent across repeated enables.

    Also seeds the reconcile-prompt file if it is absent (the persisted store of
    record), then SYNCS the stored prompt into the live reconcile cron's
    ``message`` so a custom prompt survives the manifest-driven cron rebuild that
    runs on every restart. The seed is off the loop (a lock-guarded disk write)
    and never clobbers an operator's edit — it only writes when nothing is there.
    """
    await on_shutdown(ctx)
    try:
        await asyncio.to_thread(settings.seed_default)
    except Exception:  # noqa: BLE001 — a seed failure must not block the loops
        logger.warning("chat-status-tags: reconcile-prompt seed failed", exc_info=True)
    await _sync_reconcile_prompt(ctx)
    _tasks.append(asyncio.create_task(_health_loop(ctx), name="chat-status-tags-health"))
    _tasks.append(asyncio.create_task(_resume_loop(ctx), name="chat-status-tags-resume"))
    logger.info("chat-status-tags: health + auto-resume loops started")


async def on_shutdown(ctx: Any) -> None:  # noqa: ARG001 — kept for the hook ABI
    """Stop the loops. A cancelled pass finishes nothing half-way: a tag write
    is one marshalled in-process mutation and episode state is rewritten
    atomically per pass."""
    while _tasks:
        task = _tasks.pop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
