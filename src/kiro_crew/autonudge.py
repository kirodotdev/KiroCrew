"""Auto-nudge service — reactive same-session self-prompting loop.

Each active loop is bound to a dashboard chat slot. When the slot's turn
completes (``HOOK_EVENT_STOP``), we arm an idle timer. If no new user input
arrives within ``idle_secs``, we inject the configured nudge message as the
next turn into the same slot.

State is persisted to ``~/.kiro/crew/autonudge.json`` (fcntl-locked, atomic
write). On gateway restart, active loops are reloaded and timers re-armed.

The browser observes the loop through the normal chat stream path — nudges
appear as user-style messages tagged ``[auto-nudge cycle N]`` so they are
visually distinct from human input.

Feature-flagged via env ``KIROCREW_AUTONUDGE`` (on by default; set to ``0`` to disable).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from kiro_crew import platform_compat, shutdown_event
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import legacy_home
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

_NUDGES_FILE = "autonudge.json"
_STORE_VERSION = 1
_MIN_IDLE_SECS = 15
_MAX_IDLE_SECS = 86400  # 24h
# Re-arm delay after a skipped/failed fire so a busy slot or a transient fire
# error can't silently orphan the loop. The delay escalates exponentially per
# consecutive failure (base << streak) up to _REARM_MAX_BACKOFF_SECS, and is
# always capped by the loop's idle_secs, so a permanently-wedged callback backs
# off to a slow poll instead of hammering every base interval.
_REARM_BACKOFF_SECS = 15
_REARM_MAX_BACKOFF_SECS = 300  # 5m ceiling for the escalated re-arm delay
_REARM_BACKOFF_MAX_SHIFT = 16  # clamp the 2**shift exponent

# Sentinel file per loop: creating it halts the loop on next cycle.
STOP_SENTINEL = "STOP"

# Namespaced session-key prefixes that identify messaging-channel sessions
# (as opposed to bare dashboard chat-slot keys). Channel-bound loops have no
# dashboard turn-lifecycle hooks (notify_turn_complete / notify_user_input),
# so they run on a fixed interval instead of an idle timer: the timer re-arms
# itself right after every delivered fire.
_CHANNEL_KEY_PREFIXES = ("slack:", "discord:", "telegram:", "whatsapp:", "unified:")


def is_channel_key(key: str) -> bool:
    """True when *key* names a messaging-channel session (``slack:<ts>``,
    ``discord:{agent}:direct:{user}`` ...) rather than a dashboard chat slot."""
    return key.startswith(_CHANNEL_KEY_PREFIXES)


def binding_key_for(session_key: str) -> str | None:
    """Map a session key to its AutoNudge binding (slot) key, or ``None`` if the
    session is not nudge-able.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge layer
    keys dashboard loops on the bare slot key); ``slack:``/``discord:`` session
    keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Single source of truth shared by the ``monitor_start`` MCP tool and the
    workflow ``ctx.nudge`` port so both agree on what "nudge-able" means.
    """
    if not session_key:
        return None
    if session_key.startswith("dashboard:"):
        return session_key.split(":", 1)[1]
    if session_key.startswith(("slack:", "discord:")):
        return session_key
    return None


def enabled() -> bool:
    """Feature flag — on by default. Set ``KIROCREW_AUTONUDGE=0`` to disable."""
    return os.environ.get("KIROCREW_AUTONUDGE", "1").lower() not in ("0", "false", "no")


def repair_sentinel_path(raw: str) -> str:
    """Re-home a persisted ``stop_sentinel_path`` onto the CURRENT data home.

    The kill-switch path is resolved once at arm time (``resolve_stop_sentinel``,
    which builds it under ``workspace_dir_for(...)`` → normally
    ``config_dir()/workspace``) and then persisted verbatim in the loop store.
    That store survives the one-time ``~/.kirocrew`` → ``~/.kiro/crew`` data-home
    migration (``config/paths.py``) and is re-armed on the next ``start()``, so a
    loop armed BEFORE the move comes back pointing at a directory that no longer
    exists. ``_timer`` only ever tests ``Path(stop_sentinel_path).exists()``, so
    such a loop has a DEAD kill switch: a sentinel written at the freshly
    resolved (current-home) path is never seen, and the only remaining stops are
    ``max_cycles`` and an explicit remove.

    Three transformations, in order:

    1. **Pass through a path already under the CURRENT home.** Checked FIRST,
       because ``KIROCREW_HOME`` may legally point *inside* the legacy root
       (e.g. ``~/.kirocrew/dev``). Such a path is lexically under
       ``~/.kirocrew`` yet already live and correct; re-homing it would produce
       ``~/.kirocrew/dev/dev/workspace/…``, persist that, and — since the
       rewrite is not idempotent — append another segment every boot, disabling
       a WORKING kill switch with the very code meant to repair dead ones.
    2. **Re-home a STRANDED legacy-rooted path.** A path under ``~/.kirocrew``
       is rewritten onto the resolved current home. The migration relocated the
       whole tree wholesale, so the tail after the home prefix is still correct.
       Gated on the sentinel's directory no longer existing: an absolute
       ``workspaces.<name>.dir`` may legitimately live inside that tree (and the
       legacy root can survive as debris), and rewriting a live path would move
       a working kill switch outside its configured workspace and persist that.
       Skipped when the current home IS the legacy home (``KIROCREW_HOME``
       pointing there, or the migration's fall-back-to-legacy path) — there the
       persisted path is already live. Both sides are normalized LEXICALLY
       (``os.path.normpath``, no filesystem access) before the containment
       test, so an unnormalized value like ``~/.kirocrew/../workspace/STOP``
       is not mistaken for a legacy-contained path and rewritten elsewhere.
    3. **Re-apply the arm-time sensitivity refusal.** ``authorize_and_add_nudge``
       refuses a sensitive ``stop_sentinel_path`` at arm time, but the denylist
       can widen between releases and the persisted value outlives the original
       check. A path that is sensitive NOW is dropped to ``""`` (no sentinel)
       rather than kept, so the service never stats an attacker- or
       credential-adjacent location on a timer. The check itself FAILS CLOSED:
       if ``is_sensitive_path`` raises, the path is dropped rather than trusted,
       because an unvalidated path is exactly what this step exists to reject.

    Returns the (possibly rewritten) path, or ``""`` to mean "no sentinel".
    Non-``str`` input (a malformed store where ``stop_sentinel_path`` is a
    number or list) yields ``""`` instead of raising — this runs inside
    ``_load()`` during ``start()``, so an exception here would abort gateway
    startup entirely.

    Deliberately does NOT require the path to live under the data home: an
    absolute ``workspaces.<name>.dir`` is a legitimate configuration, and
    clearing those would break working kill switches.

    BLOCKING: performs no filesystem I/O itself, but ``is_sensitive_path``
    resolves realpaths, which can block on an unavailable network mount.
    ``start()`` therefore runs the whole load+repair phase in an executor.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    path = raw.strip()
    try:
        legacy = legacy_home()
        current = config_dir()
        candidate = Path(path).expanduser()
        # Lexical normalization only — never touch the filesystem here.
        norm_candidate = Path(os.path.normpath(str(candidate)))
        norm_legacy = Path(os.path.normpath(str(legacy)))
        norm_current = Path(os.path.normpath(str(current)))
        if norm_candidate.is_relative_to(norm_current):
            # Already live under the current home (including a nested
            # KIROCREW_HOME inside the legacy root) — nothing to re-home.
            pass
        elif norm_current != norm_legacy and norm_candidate.is_relative_to(norm_legacy):
            # Re-home ONLY when the legacy directory the sentinel lives in is
            # gone. A path under ``~/.kirocrew`` is not necessarily a migration
            # casualty: ``workspaces.<name>.dir`` may legitimately be configured
            # as an absolute path inside that tree (and the legacy root can
            # survive the migration as debris, which `kirocrew doctor` reports).
            # Rewriting a still-existing directory's sentinel would move a
            # WORKING kill switch outside its configured workspace and persist
            # that. The migration deletes the tree it moved, so "parent no
            # longer exists" is what distinguishes a stranded path from a live
            # one. A dead path stays dead either way, so the existence probe
            # only ever prevents damage.
            if norm_candidate.parent.exists():
                logger.debug(
                    "AutoNudge: keeping legacy-rooted sentinel %s — its directory "
                    "still exists, so it is a live configured path, not a "
                    "migration leftover",
                    path,
                )
            else:
                rehomed = norm_current / norm_candidate.relative_to(norm_legacy)
                logger.info(
                    "AutoNudge: re-homed stop sentinel from legacy data home: %s → %s",
                    path,
                    rehomed,
                )
                path = str(rehomed)
    except Exception:  # noqa: BLE001 - a repair failure must never block startup
        logger.warning("AutoNudge: could not re-home sentinel %r", raw, exc_info=True)
    try:
        sensitive = is_sensitive_path(path)
    except Exception:  # noqa: BLE001 - fail closed: unvalidated ⇒ untrusted
        logger.warning(
            "AutoNudge: sensitivity re-check failed for %r — dropping the sentinel",
            path,
            exc_info=True,
        )
        return ""
    if sensitive:
        logger.warning(
            "AutoNudge: dropping stop sentinel %r — path is now sensitive; "
            "the loop will be deactivated rather than left unstoppable by file",
            path,
        )
        return ""
    return path


# Module-level singleton so hooks in chat.py / messaging.py can notify the
# service without needing a reference to the gateway. Set by AutoNudgeService
# on start(); cleared on stop().
_INSTANCE: "AutoNudgeService | None" = None


def get_instance() -> "AutoNudgeService | None":
    return _INSTANCE


@dataclass
class NudgeLoop:
    """A single auto-nudge loop bound to one session.

    ``slot_key`` is the binding key: either a bare dashboard chat-slot key
    (e.g. ``chat-1-1721...``, idle-timer driven via notify_turn_complete) or a
    namespaced messaging-channel session key (e.g. ``slack:<thread_ts>``,
    ``discord:{agent}:direct:{user_id}``), which runs on a fixed interval.
    The field keeps its historical name for store/REST/WS compatibility.
    """

    id: str
    slot_key: str
    message: str
    idle_secs: int = 60
    max_cycles: int = 0  # 0 = unlimited
    cycle_count: int = 0
    active: bool = True
    last_fire_ts: float = 0.0
    created_ts: float = 0.0
    stop_sentinel_path: str = ""  # optional absolute path; if present loop halts
    # Wall-clock budget in seconds, measured from ``created_ts`` (0 = unlimited).
    # A cycle cap alone cannot bound COST: a loop whose turns are slow or whose
    # idle gap is long can run for days within its cycle budget. Anchoring on
    # the persisted ``created_ts`` (not arm time) makes the budget restart-proof
    # — a gateway restart re-arms the loop but never resets its clock.
    max_runtime_secs: int = 0
    # WHY the loop was last deactivated: "" (active / never stopped),
    # "manual" (user pause / any caller that didn't say otherwise),
    # "cycle_cap", or "runtime_budget" (set by _timer's terminal bounds).
    # Persisted so revival logic can distinguish a manual pause from a bound
    # expiry — elapsed wall-clock keeps growing after a manual pause, so
    # WITHOUT this record a paused loop whose budget has since elapsed is
    # indistinguishable from a budget-stopped one, and a budget raise would
    # resume unattended execution against the user's explicit pause.
    stopped_reason: str = ""


def runtime_budget_exceeded(loop: "NudgeLoop", now: float | None = None) -> bool:
    """True when *loop* has a wall-clock budget and it is spent.

    Single source of truth shared by ``_timer`` (enforcement) and the expiry
    notifier (wording), so the two can never disagree on WHY a loop stopped.
    A loop with no ``created_ts`` (a malformed/legacy store entry) never
    trips the budget — there is no anchor to measure from, and guessing one
    could kill a healthy loop on its first cycle after an upgrade.
    """
    if not loop.max_runtime_secs or not loop.created_ts:
        return False
    return (now if now is not None else time.time()) - loop.created_ts >= loop.max_runtime_secs


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if "r" in mode and not path.exists():
        path.write_text(json.dumps({"version": _STORE_VERSION, "loops": []}))
    # "r" -> "r+": Windows msvcrt.locking requires WRITE access on the fd — a
    # read-only handle fails with EACCES, which platform_compat.file_lock
    # swallows (best-effort), silently degrading the reader's lock to a no-op
    # and letting a concurrent _save race the read (same fix as
    # apps/bridges.py:_mcp_lock). The shared/exclusive decision keys off the
    # ORIGINAL mode so a reader still requests a shared lock.
    exclusive = "w" in mode or "+" in mode
    if mode == "r":
        mode = "r+"
    with open(path, mode, encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=exclusive):
            yield fh


class AutoNudgeService:
    """Manages reactive per-slot nudge loops with restart-survival."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_fire: Callable[[NudgeLoop], Awaitable[bool]] | None = None,
    ) -> None:
        self._base_dir = base_dir or config_dir()
        self._path = self._base_dir / _NUDGES_FILE
        self._on_fire = on_fire
        self._loops: dict[str, NudgeLoop] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # Loop ids whose re-arm was requested while their fire window was open.
        # Applied when the window closes (see _timer): a dashboard turn can
        # complete while the firing task is still persisting, and honouring the
        # hook immediately would cancel that task mid-persist.
        self._rearm_pending: set[str] = set()
        # Loop ids whose timer task is CURRENTLY inside its ``_on_fire`` await.
        # ``update()`` must not cancel such a timer: for channel-bound loops the
        # fire callback runs the unattended turn INLINE, so cancelling it kills
        # the in-flight turn and loses its transcript and cycle bookkeeping.
        self._firing: set[str] = set()
        # Set by _load() when a persisted loop was repaired in memory (currently
        # a re-homed/dropped stop_sentinel_path) so start() can flush the
        # correction back to disk ONCE instead of re-deriving it every boot.
        self._store_dirty = False
        # Consecutive non-delivery count per loop (drives escalating re-arm
        # backoff + once-per-streak failure logging). Not persisted; resets on
        # a delivered fire, on removal, and on restart.
        self._rearm_fail_count: dict[str, int] = {}
        # Strong refs to in-flight shielded add() tasks: keeps a detached
        # mutation supervised (no GC, failures logged) even when every awaiting
        # caller was cancelled. Discarded on completion.
        self._inflight_adds: set = set()
        self._observers: list[Callable[[str, NudgeLoop | None], None]] = []
        self._lock = asyncio.Lock()

    # ── Persistence ──

    def _load(self) -> None:
        """Read the store and repair each entry. BLOCKING — see ``start()``.

        Does file I/O (locked read) and, via ``repair_sentinel_path``, realpath
        resolution that can stall on an unavailable network mount, so callers on
        the event loop MUST offload this (``no-blocking-call-on-event-loop``).
        """
        with _locked_file(self._path, "r") as fh:
            data = json.load(fh)
        for raw in data.get("loops", []):
            try:
                loop = NudgeLoop(**{k: raw[k] for k in raw if k in NudgeLoop.__dataclass_fields__})
                # Re-home / re-validate the persisted kill-switch path. A loop
                # armed before the data-home move would otherwise be re-armed
                # with a sentinel path nothing can ever create (see
                # repair_sentinel_path). INSIDE the per-entry try: a malformed
                # store entry must be skipped, never abort start() and take the
                # gateway offline.
                repaired = repair_sentinel_path(loop.stop_sentinel_path)
            except Exception:
                logger.warning("AutoNudge: skipping malformed loop entry: %r", raw, exc_info=True)
                continue
            self._loops[loop.id] = loop
            if repaired != loop.stop_sentinel_path:
                dropped = bool(loop.stop_sentinel_path) and not repaired
                loop.stop_sentinel_path = repaired
                if dropped:
                    # FAIL CLOSED, matching the arm-time contract:
                    # authorize_and_add_nudge REFUSES to arm a loop whose
                    # sentinel is sensitive, so a persisted loop whose sentinel
                    # has become sensitive must not be re-armed with no kill
                    # switch at all. Deactivating leaves it inspectable and
                    # restartable rather than silently unstoppable-by-file.
                    logger.warning(
                        "AutoNudge: deactivating loop %s — its stop sentinel was dropped",
                        loop.id,
                    )
                    loop.active = False
                self._store_dirty = True
        logger.info("AutoNudge: loaded %d loops", len(self._loops))

    def _serialize_state(self) -> dict:
        """Snapshot the store payload ON THE CALLER'S THREAD.

        Loop state is mutated only under the service lock on the event loop, so
        the serialization must happen there too — a worker thread iterating
        ``self._loops`` concurrently with a mutation would race. The returned
        payload is immutable-by-convention and safe to hand to an executor.
        """
        return {
            "version": _STORE_VERSION,
            "loops": [asdict(lp) for lp in self._loops.values()],
        }

    def _write_state(self, payload: dict) -> None:
        # Atomic write: serialize to a temp file in the same dir, fsync, then
        # replace onto the target path. Eliminates the truncate-before-
        # flock race that plain open(path, "w") has — readers always see either
        # the old complete file or the new complete file, never a partial one.
        # The rename goes through replace_with_retry because on Windows it can
        # fail with PermissionError while another handle is transiently open on
        # the fresh temp file (indexer / AV), which loses the write (issue #1105).
        # Blocking (fsync) — async callers offload this to an executor.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            replace_with_retry(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _save(self) -> None:
        self._write_state(self._serialize_state())

    # ── Observer hook (for WS broadcasts) ──

    def subscribe(self, cb: Callable[[str, NudgeLoop | None], None]) -> None:
        self._observers.append(cb)

    def _emit(self, event: str, loop: NudgeLoop | None) -> None:
        for cb in self._observers:
            try:
                cb(event, loop)
            except Exception:
                logger.warning("AutoNudge observer failed", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        if not enabled():
            logger.info("AutoNudge disabled (KIROCREW_AUTONUDGE not set)")
            return
        # Load + repair OFF the event loop: the locked read is file I/O and
        # repair_sentinel_path's sensitivity check resolves realpaths, which can
        # stall on an unavailable network-mounted sentinel. Freezing the loop
        # here would take chat, heartbeat and liveness down until the watchdog
        # restarts the gateway (no-blocking-call-on-event-loop).
        await asyncio.get_running_loop().run_in_executor(None, self._load)
        # Persist any repair _load() made (re-homed sentinel paths) before the
        # timers go live, so the corrected value survives even if this process
        # never mutates a loop again. Routed through _persist_locked so it obeys
        # the one write protocol (snapshot under `_lock`, fsync off the loop)
        # rather than snapshotting unlocked.
        if self._store_dirty:
            try:
                await self._persist_locked()
                self._store_dirty = False
            except Exception:  # noqa: BLE001 - the in-memory repair still applies
                logger.warning("AutoNudge: could not persist sentinel repair", exc_info=True)
        # Re-arm timers for active loops on startup.
        for loop in self._loops.values():
            if loop.active:
                self._arm_timer(loop)
        global _INSTANCE
        _INSTANCE = self
        logger.info("AutoNudge started")

    def stop(self) -> None:
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None

    # ── Loop CRUD ──

    async def add(
        self,
        slot_key: str,
        message: str,
        idle_secs: int = 60,
        max_cycles: int = 0,
        stop_sentinel_path: str = "",
        max_runtime_secs: int = 0,
    ) -> NudgeLoop:
        # CANCELLATION SAFETY: the mutate+persist runs as a SHIELDED task. If
        # the awaiting caller is cancelled mid-write, a bare await would release
        # ``_lock`` while the executor write is still running — a subsequent
        # add/update could persist newer state first and then be clobbered by
        # this operation's stale snapshot (lost update after restart). Shielding
        # keeps the inner task (and the lock) alive until the write completes,
        # so writes remain strictly serialized; the cancelled caller still sees
        # CancelledError, with the arm possibly landed (same "mutation may have
        # already landed" semantics as other cancellation-uncertain mutations).
        # The inner task is retained in ``_inflight_adds`` (discarded when done)
        # so it stays SUPERVISED — strongly referenced and completion-logged —
        # even if every awaiting caller has been cancelled.
        inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
            self._add_locked(
                slot_key,
                message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                stop_sentinel_path=stop_sentinel_path,
                max_runtime_secs=max_runtime_secs,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def _add_locked(
        self,
        slot_key: str,
        message: str,
        *,
        idle_secs: int,
        max_cycles: int,
        stop_sentinel_path: str,
        max_runtime_secs: int = 0,
    ) -> NudgeLoop:
        idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
        async with self._lock:
            # One loop per slot — replace any existing loop on this slot.
            # persist=False: the offloaded write below persists the combined
            # removal+add atomically, avoiding a duplicate blocking save here.
            existing = self._find_by_slot(slot_key)
            if existing:
                self.remove_sync(existing.id, persist=False)
            loop = NudgeLoop(
                id=uuid.uuid4().hex[:8],
                slot_key=slot_key,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max(0, int(max_cycles)),
                created_ts=time.time(),
                stop_sentinel_path=stop_sentinel_path,
                max_runtime_secs=max(0, int(max_runtime_secs)),
            )
            self._loops[loop.id] = loop
            # Persist WITHOUT blocking the event loop (no-blocking-call rule:
            # _write_state fsyncs, and a wedged disk must not freeze the
            # gateway). Snapshot under the lock (mutation safety), write on a
            # worker thread, and await it so a persistence failure still
            # propagates to the caller before the loop is reported armed.
            payload = self._serialize_state()
            await asyncio.get_running_loop().run_in_executor(
                None, self._write_state, payload
            )
            self._arm_timer(loop)
        self._emit("added", loop)
        logger.info("AutoNudge: added loop %s on slot %s (idle=%ds)", loop.id, slot_key, idle_secs)
        return loop

    async def _persist_locked(self) -> None:
        """Snapshot under the service lock and write on a worker thread.

        The SINGLE async persistence path for post-arm mutations. Two properties
        matter and both were violated before:

        * **Serialization.** Every writer must snapshot while holding
          ``_lock``; otherwise a writer that snapshots, releases, and then
          writes can land a STALE payload on top of a newer one (e.g. a
          concurrent ``update()`` overwriting the post-fire ``cycle_count`` /
          ``active`` bookkeeping, which then resurrects obsolete state after a
          restart).
        * **Non-blocking.** ``_write_state`` fsyncs, so it must never run on the
          event loop.
        """
        async with self._lock:
            payload = self._serialize_state()
            await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)

    async def update(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
        max_runtime_secs: int | None = None,
        stopped_reason: str | None = None,
    ) -> NudgeLoop | None:
        # CANCELLATION SAFETY: same contract as add(). The mutate+persist runs
        # as a SHIELDED, supervised task so a caller cancelled mid-write cannot
        # release ``_lock`` while the executor write is still in flight — which
        # would let a later write land first and then be clobbered by this
        # operation's stale snapshot (lost update after restart).
        inner: "asyncio.Task[NudgeLoop | None]" = asyncio.ensure_future(
            self._update_locked(
                loop_id,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                active=active,
                max_runtime_secs=max_runtime_secs,
                stopped_reason=stopped_reason,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop | None]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: detached update() failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def _update_locked(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
        max_runtime_secs: int | None = None,
        stopped_reason: str | None = None,
    ) -> NudgeLoop | None:
        async with self._lock:
            loop = self._loops.get(loop_id)
            if not loop:
                return None
            if message is not None:
                loop.message = message
            if idle_secs is not None:
                loop.idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
            if max_cycles is not None:
                loop.max_cycles = max(0, int(max_cycles))
            if max_runtime_secs is not None:
                loop.max_runtime_secs = max(0, int(max_runtime_secs))
            if active is not None:
                # TERMINAL-TRANSITION ATOMICITY: a bound-tagged deactivation
                # (stopped_reason supplied — the _timer's cycle_cap /
                # runtime_budget paths) must never OVERWRITE a deactivation
                # that landed first. The race: user pauses right after the
                # timer detects expiry — the pause persists "manual" and
                # cancels the timer, but the timer's already-inflight shielded
                # update would stamp "runtime_budget" over it, making the loop
                # budget-revivable against an explicit pause. Both transitions
                # serialize on _lock, so re-checking here closes the race: the
                # bound's deactivation degrades to a no-op when the loop is
                # already inactive. The reverse order is already safe — a
                # manual pause overwriting a bound tag only ever NARROWS
                # revivability ("manual" never auto-revives).
                if stopped_reason and not active and not loop.active:
                    logger.info(
                        "AutoNudge: loop %s already deactivated (%s) — %s bound "
                        "not overwriting it",
                        loop.id,
                        loop.stopped_reason or "manual",
                        stopped_reason,
                    )
                else:
                    loop.active = bool(active)
                    # Record WHY on every deactivation and clear it on every
                    # revival, so the store always reflects the LAST transition.
                    # ``stopped_reason`` is an internal caller parameter (_timer's
                    # terminal bounds pass "cycle_cap"/"runtime_budget"); external
                    # deactivations (REST pause, deactivate-mid-fire) default to
                    # "manual", which the revive logic never auto-resumes.
                    if loop.active:
                        loop.stopped_reason = ""
                    else:
                        loop.stopped_reason = stopped_reason or "manual"
            # Persist WITHOUT blocking the event loop — _write_state fsyncs, and
            # a wedged disk must not freeze chat/heartbeat/liveness. Snapshot
            # under THIS lock hold (mutation safety + serialization vs the
            # post-fire write) and await the offloaded write so a persistence
            # failure still reaches the caller. Same contract as _add_locked.
            payload = self._serialize_state()
            await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
            # Re-arm the timer with the new settings — but NEVER while its
            # callback is mid-fire. Cancelling a firing timer cancels the
            # in-flight turn itself (channel loops run the turn inline in
            # _on_fire), destroying the response and the cycle accounting. A
            # firing timer re-arms itself on every exit path anyway (backoff
            # re-arm when undelivered, self-re-arm for channel keys,
            # notify_turn_complete for dashboard slots), and each of those reads
            # the freshly-updated idle_secs/active, so the new settings still
            # take effect on the next cycle.
            if loop.id in self._firing:
                logger.info(
                    "AutoNudge: loop %s updated mid-fire — deferring re-arm to the "
                    "running timer so the in-flight turn is not cancelled",
                    loop.id,
                )
            else:
                self._cancel_timer(loop_id)
                if loop.active:
                    self._arm_timer(loop)
        self._emit("updated", loop)
        return loop

    def remove_sync(self, loop_id: str, *, persist: bool = True) -> None:
        """Remove a loop. ``persist=False`` skips the blocking save — used by
        async callers that snapshot+offload the write themselves right after."""
        loop = self._loops.pop(loop_id, None)
        if loop is None:
            return
        self._cancel_timer(loop_id)
        self._rearm_fail_count.pop(loop_id, None)
        self._rearm_pending.discard(loop_id)
        if persist:
            self._save()
        self._emit("removed", loop)

    async def remove(self, loop_id: str) -> None:
        async with self._lock:
            existed = loop_id in self._loops
            # Remove in-memory but SKIP the blocking save: _save() -> _write_state
            # fsyncs, and a wedged disk must not freeze the event loop. Snapshot
            # under THIS lock hold (serialization vs the post-fire write). Keep
            # the removal INLINE (not a separate task) so _cancel_timer's
            # "never cancel the current task" self-guard still applies when
            # _timer removes its own loop.
            self.remove_sync(loop_id, persist=False)
            if existed:
                payload = self._serialize_state()
                fut = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    # Caller cancelled mid-write: the executor thread can't be
                    # cancelled and is still fsyncing. shield re-raised on us
                    # immediately, so DRAIN the write to completion before this
                    # `async with` exits and releases _lock — otherwise a waiter
                    # (add()/update()/_persist_locked) could acquire the lock and
                    # race a second os.replace(), clobbering newer state with this
                    # stale removal snapshot ("lost update after restart"). Then
                    # propagate the cancellation.
                    await asyncio.shield(fut)
                    raise

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._find_by_slot(slot_key)

    def list_all(self) -> list[NudgeLoop]:
        return list(self._loops.values())

    def _find_by_slot(self, slot_key: str) -> NudgeLoop | None:
        """The loop bound to *slot_key*, which may be a binding key OR a tab name.

        A channel-born conversation is bound under its channel session key
        (``slack:<ts>``) — the fire path needs that key to route the turn — but
        its dashboard tab knows itself only by slot NAME
        (``slack_<ts>``: the same key folded to the filename charset). The
        turn-lifecycle hooks and the tab's own loop lookups pass that name, so
        matching it here is what keeps one loop addressable from both sides
        instead of invisible from the dashboard.

        Exact match wins; the fold is a fallback, and is computed with the
        dashboard's own normalizer so no second derivation of the name exists.
        """
        for lp in self._loops.values():
            if lp.slot_key == slot_key:
                return lp
        if not slot_key or is_channel_key(slot_key):
            return None
        # Lazy: autonudge is imported BY the dashboard chat layer.
        from kiro_crew.dashboard.state import _normalize_slot_key

        for lp in self._loops.values():
            if is_channel_key(lp.slot_key) and _normalize_slot_key(lp.slot_key) == slot_key:
                return lp
        return None

    # ── Reactive arming ──

    def notify_turn_complete(self, slot_key: str) -> None:
        """Called by gateway after HOOK_EVENT_STOP — (re)arm idle timer for this slot.

        DEFERS while the loop's own timer task is mid-fire: ``_arm_timer``
        cancels the existing task, and during the fire window that task may be
        parked on ``_persist_locked()`` writing the delivered cycle. Cancelling
        it there loses the ``cycle_count`` bump and lets the loop run extra
        cycles after a restart. The deferred re-arm is applied when the window
        closes.
        """
        loop = self._find_by_slot(slot_key)
        if not loop or not loop.active:
            return
        if loop.id in self._firing:
            self._rearm_pending.add(loop.id)
            return
        self._arm_timer(loop)

    def notify_user_input(self, slot_key: str) -> None:
        """Called when user sends a message — cancel pending nudge (user takes priority).

        While the loop is mid-fire this must NOT cancel the timer: that task may
        be parked on ``_persist_locked()`` writing the delivered cycle, and
        cancelling it there abandons an in-flight executor write whose stale
        payload can later overwrite a newer update/delete (state resurrected
        after a restart). User priority is still honoured — the deferred re-arm
        is dropped, so no further nudge is scheduled from this cycle.
        """
        loop = self._find_by_slot(slot_key)
        if not loop:
            return
        if loop.id in self._firing:
            self._rearm_pending.discard(loop.id)
            logger.info(
                "AutoNudge: user input during loop %s's fire window — dropped the "
                "deferred re-arm instead of cancelling mid-persist",
                loop.id,
            )
            return
        self._cancel_timer(loop.id)

    def _cancel_timer(self, loop_id: str) -> None:
        t = self._timers.pop(loop_id, None)
        # Never cancel the currently running timer task (self-re-arm from inside
        # _timer): it is about to return on its own, and cancelling it would
        # inject a spurious CancelledError into the finishing task.
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def _arm_timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        self._cancel_timer(loop.id)
        self._timers[loop.id] = asyncio.create_task(self._timer(loop, delay))

    async def _timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(loop.idle_secs if delay is None else delay)
        except asyncio.CancelledError:
            return
        if shutdown_event.is_set():
            return
        # Kill switch: sentinel file present?
        if loop.stop_sentinel_path and Path(loop.stop_sentinel_path).exists():
            logger.info("AutoNudge: stop sentinel found for %s — removing loop", loop.id)
            await self.remove(loop.id)
            return
        # Cycle cap reached?
        if loop.max_cycles and loop.cycle_count >= loop.max_cycles:
            logger.info("AutoNudge: loop %s reached max_cycles — deactivating", loop.id)
            await self.update(loop.id, active=False, stopped_reason="cycle_cap")
            # Signal the cap. Reaching max_cycles is NOT a successful finish —
            # the loop ran out of cycles with its goal possibly unmet — yet the
            # only trace used to be this log line plus an ``updated`` event
            # indistinguishable from a user pressing Stop. A capped-out babysit
            # was therefore impossible to tell apart from the agent stopping on
            # its own. ``expired`` is emitted so an observer can raise a
            # notification the user actually sees.
            #
            # Emitted AFTER update() (which already persisted active=False and
            # emitted ``updated``), so a subscriber handling ``expired`` always
            # observes the loop in its final deactivated state. Deliberately a
            # NEW event kind rather than overloading ``updated``: the many
            # benign updates (message edits, manual pause) must not notify.
            self._emit("expired", loop)
            return
        # Wall-clock budget spent? Checked AFTER the cycle cap (both exhausted
        # → the cap wins, keeping historical wording) and BEFORE the fire, so
        # a spent budget never buys one more unattended turn. Same terminal
        # treatment as the cap: deactivate (inspectable/restartable, not
        # removed) and emit ``expired`` so the existing observer raises a
        # user-visible notification — a budget that stops a loop silently
        # would be indistinguishable from the agent stopping on its own.
        if runtime_budget_exceeded(loop):
            logger.info(
                "AutoNudge: loop %s exceeded max_runtime_secs=%d — deactivating",
                loop.id,
                loop.max_runtime_secs,
            )
            await self.update(loop.id, active=False, stopped_reason="runtime_budget")
            self._emit("expired", loop)
            return
        # Fire. Update state only if the callback reports actual delivery —
        # otherwise skipped nudges (e.g. slot mid-turn) inflate cycle_count and
        # prematurely trip max_cycles. Missing callback → nothing to deliver.
        if self._on_fire is None:
            return
        self._firing.add(loop.id)
        try:
            await self._run_fire_cycle(loop)
        finally:
            self._firing.discard(loop.id)
            # A re-arm requested DURING the fire window (a dashboard turn that
            # completed while we were still persisting) was deferred rather than
            # applied, because applying it would have cancelled this very task
            # mid-persist. Apply it now that the window is closed — dropping it
            # would leave a dashboard loop with no armed timer at all, since the
            # delivered path relies on notify_turn_complete for those slots.
            if loop.id in self._rearm_pending:
                self._rearm_pending.discard(loop.id)
                if loop.active and loop.id in self._loops:
                    self._arm_timer(loop)

    async def _run_fire_cycle(self, loop: NudgeLoop) -> None:
        """Fire once, then persist bookkeeping and decide the re-arm.

        Runs entirely inside the caller's ``_firing`` window so a concurrent
        ``update()`` never cancels this task between delivery and persistence.
        """
        if self._on_fire is None:
            return
        # Mark the fire window so a concurrent update() defers its re-arm
        # instead of cancelling this task mid-turn (see update()). The window
        # stays open through the post-delivery bookkeeping and the re-arm
        # decision, NOT just the callback: clearing it the moment _on_fire
        # returned let a waiting update() cancel this task while it was parked
        # on _persist_locked(), so the delivered cycle was never written and the
        # loop could run extra cycles after a restart. _run_fire_cycle owns the
        # window; this method is the body.
        try:
            delivered = await self._on_fire(loop)
        except Exception:
            delivered = False
            # Full traceback only on the first failure of a streak; subsequent
            # failures stay at debug so a permanently-wedged callback can't spam
            # a traceback every re-arm.
            if self._rearm_fail_count.get(loop.id, 0) == 0:
                logger.exception("AutoNudge fire callback failed for %s", loop.id)
            else:
                logger.debug(
                    "AutoNudge fire still failing for %s (streak=%d)",
                    loop.id,
                    self._rearm_fail_count.get(loop.id, 0) + 1,
                )
        if not delivered:
            # If the fire path already removed the loop (e.g. slot missing →
            # remove()), do NOT resurrect it with a fresh timer — that would
            # orphan-poll forever. Clear the streak and stop.
            if loop.id not in self._loops:
                self._rearm_fail_count.pop(loop.id, None)
                return
            # A concurrent update() may have DEACTIVATED this loop while the
            # callback was in flight; that update deliberately deferred the
            # cancel to avoid killing the turn, so the failure path must honour
            # the pause instead of re-arming. Otherwise "stop the loop" during a
            # cycle whose delivery then fails silently resumes unattended tool
            # execution.
            if not loop.active:
                logger.info(
                    "AutoNudge: loop %s was deactivated mid-fire — not re-arming",
                    loop.id,
                )
                self._rearm_fail_count.pop(loop.id, None)
                return
            # Slot was busy mid-turn, or the fire callback errored. Do NOT end
            # the loop — re-arm so it self-heals and never depends solely on the
            # external notify_turn_complete hook (skipped on a slot's error/
            # timeout/cancel exit paths). Escalate the delay per consecutive
            # failure so a never-delivering loop backs off to a slow poll
            # instead of hammering, capped by idle_secs and _REARM_MAX_BACKOFF.
            n = self._rearm_fail_count.get(loop.id, 0) + 1
            self._rearm_fail_count[loop.id] = n
            shift = min(n - 1, _REARM_BACKOFF_MAX_SHIFT)
            backoff = min(
                _REARM_BACKOFF_SECS * (2**shift),
                _REARM_MAX_BACKOFF_SECS,
                loop.idle_secs,
            )
            self._arm_timer(loop, delay=backoff)
            return
        # Delivered — clear any failure streak so the next skip starts fresh.
        self._rearm_fail_count.pop(loop.id, None)
        loop.cycle_count += 1
        loop.last_fire_ts = time.time()
        # Persist through the shared locked+offloaded path so this bookkeeping
        # cannot be clobbered by a concurrent update()'s snapshot (and so the
        # fsync stays off the event loop).
        await self._persist_locked()
        self._emit("fired", loop)
        # POST-DELIVERY budget check: the budget gates when turns START, so a
        # slow in-flight turn can overshoot it (bounded by the transport's
        # per-turn ceiling, constants.CHAT_TURN_TIMEOUT — this service must
        # not cancel a running turn; see the mid-fire contracts above). But
        # once the turn HAS finished, a spent budget must take effect NOW —
        # deactivating here instead of on the next idle timer closes the
        # window where notify_turn_complete arms another full idle cycle for
        # a loop that is already over budget.
        if runtime_budget_exceeded(loop) and loop.active and loop.id in self._loops:
            logger.info(
                "AutoNudge: loop %s exceeded max_runtime_secs=%d during its turn "
                "— deactivating post-delivery",
                loop.id,
                loop.max_runtime_secs,
            )
            await self.update(loop.id, active=False, stopped_reason="runtime_budget")
            self._emit("expired", loop)
            return
        # Channel-bound loops (Slack/Discord/...) have no dashboard
        # turn-lifecycle hook to re-arm them (notify_turn_complete never fires
        # for these keys), so they self-re-arm on a fixed interval. The fire
        # callback runs the turn inline, so the next fire lands idle_secs
        # after the previous turn finished; the busy-skip + backoff above
        # handles any overlap.
        if is_channel_key(loop.slot_key) and loop.active and loop.id in self._loops:
            self._arm_timer(loop)
