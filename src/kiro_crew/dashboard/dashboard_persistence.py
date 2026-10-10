"""Dashboard slot and context-snapshot persistence coordination.

The dashboard facade owns every mutable field used here.  This component
deliberately retains no slot map, dirty flag, lock, or task reference: each
operation reads the current value from its owner, so direct access, test
replacement, and shutdown ordering all stay valid.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.dashboard.slot_registry import SlotRegistry, TakeoverBasis

AtomicWriter = Callable[..., None]
JsonCodecProvider = Callable[[], Any]
# Keyword-accepting: the periodic writer passes ``expected_slot_name`` so the
# save's in-lock ownership guard can refuse a write whose slot was replaced, and
# ``takeover_basis`` so a truncating retry's ordering claim is anchored to the
# instant this pass vouched for the slot.
SlotSaver = Callable[..., Any]


def _current_shutdown_event() -> Any:
    """Resolve the process shutdown event when a flush loop actually starts."""
    from kiro_crew import shutdown_event

    return shutdown_event


def _current_slot_saver() -> SlotSaver:
    """Resolve the re-export patched by existing flush characterizations."""
    from kiro_crew.dashboard.chat import _save_slot_to_history

    return _save_slot_to_history


class DashboardPersistenceCoordinator:
    """Coordinate durable dashboard state while the facade owns its data."""

    def __init__(
        self,
        *,
        config_dir_provider: Callable[[], Path],
        atomic_write_provider: Callable[[], AtomicWriter],
        logger_provider: Callable[[], logging.Logger],
        json_codec_provider: JsonCodecProvider,
        wall_time_provider: Callable[[], float],
        slot_saver_provider: Callable[[], SlotSaver] = _current_slot_saver,
        shutdown_event_provider: Callable[[], Any] = _current_shutdown_event,
    ) -> None:
        self._config_dir_provider = config_dir_provider
        self._atomic_write_provider = atomic_write_provider
        self._logger_provider = logger_provider
        self._json_codec_provider = json_codec_provider
        self._wall_time_provider = wall_time_provider
        self._slot_saver_provider = slot_saver_provider
        self._shutdown_event_provider = shutdown_event_provider

    @staticmethod
    def _owner_method(
        owner: Any,
        name: str,
        fallback: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Resolve an instance-replaceable facade method for this call."""
        try:
            return getattr(owner, name)
        except AttributeError:
            return partial(fallback, owner)

    def start_flush_loop(self, owner: Any) -> None:
        """Start the five-second dirty-state flush loop once."""
        if owner._flush_task is None:
            flush_loop = self._owner_method(owner, "_flush_loop", self._flush_loop)
            owner._flush_task = asyncio.ensure_future(flush_loop())

    async def _flush_loop(self, owner: Any) -> None:
        """Periodically save dirty slots so a crash loses at most one interval."""
        shutdown_event = self._shutdown_event_provider()
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=owner._FLUSH_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            # Resolve after every timeout. Tests and callers replace this facade
            # seam while a long-lived loop is already running.
            flush_dirty = self._owner_method(owner, "_flush_dirty_slots", self._flush_dirty_slots)
            await asyncio.get_running_loop().run_in_executor(None, flush_dirty)
            # circular import: state -> dashboard_persistence -> chat_utils -> state
            from kiro_crew.dashboard.chat_utils import apply_pending_slot_memory_mode

            for slot in list(owner._slots.values()):
                apply_pending_slot_memory_mode(owner, slot)

    def flush_slot_now(
        self, owner: Any, slot: Any, takeover_basis: TakeoverBasis | None = None
    ) -> None:
        """Write one dirty slot and clear only the generation that was saved.

        ``takeover_basis`` is the slot key's open takeover watch, opened and
        closed by the caller around this call (``_flush_dirty_slots`` opens it
        while it can still vouch the slot came out of the live table). A
        truncating retry (``_pending_rewrite``) carries it into the save's
        publication-ordering claim, so a same-key takeover landing between
        that instant and the claim registration still refuses the write.
        ``None`` means the caller anchored to no earlier instant and the claim
        observes takeovers from its registration on.
        """
        # Endpoint metadata is applied to the live slot before its guarded
        # history write.  Do not let this unpinned periodic writer make that
        # provisional value durable while the guarded writer is still waiting.
        if getattr(slot, "_metadata_persist_inflight", 0):
            return
        # Nor while the slot's NAME is being retracted. A fenced slot is still
        # the occupant of its name until the pop, so this 5-second pass still
        # visits it, and this write carries no ``expected_slot_name`` -- so the
        # in-lock recreate-won guard is skipped and the retraction's wait, which
        # only drains REGISTERED guarded writes, cannot see it either. A tick
        # landing between the fence and the pop would then overwrite whatever
        # adopts the name next. ``_dirty`` is deliberately left armed: the next
        # pass writes it if the close is abandoned, and a close that completes
        # persists the window itself through its own archival save.
        if getattr(slot, "is_closing", False):
            return
        if not owner.conversation_log:
            return
        # A queued user prompt is persisted by the metadata line, not by a row,
        # so an enqueue does not make the transcript dirty. Save on either
        # signal: ``_dirty`` for the window, queue drift for the queue.
        if not slot._dirty and not getattr(slot, "queue_persist_pending", False):
            return
        # A message-less slot has no window to write, but it can still owe a
        # metadata edit: a newborn from ``session_create`` has a birth line, and
        # a sidebar color, title or similar edit made before its first message
        # only marks it dirty. The forced save takes the empty-window merge for
        # it, which writes into an existing line only, so a plain empty tab
        # still gets no file. Queue drift alone (an enqueue or a cancel) is saved
        # too. A line-less tab writes nothing, and the merge credits the queue
        # witness for it, so the drift does not re-run the save on every tick.
        force = not slot.messages
        save_slot_to_history = self._slot_saver_provider()

        # Keep the dirty bit true for the whole save. chat_fork treats it as
        # "unpersisted state exists", and the history writer's resumed-slot
        # guard also reads it during the write. A generation comparison avoids
        # erasing a new dirty mark set concurrently by the event loop.
        generation = slot._dirty_gen
        try:
            # The fence read above happens on this executor thread while the
            # retraction runs on the loop, and the write does not reach the
            # transcript lock until after the snapshot, routing and retention
            # stretch -- so the fence is necessary but cannot be sufficient, in
            # exactly the shape of the defect this ordering exists to close:
            # event-loop state read from a worker thread decides a commit that is
            # still ahead. ``expected_slot_name`` closes it at the only place that
            # can decide, INSIDE the lock with no await before the write: the save
            # refuses when the map holds a different slot under this name. That
            # matters here more than elsewhere, because a periodic save is a full
            # metadata rebuild -- it does not request the ``rows_only`` deferral
            # that keeps another holder's folder, title and tag.
            if force:
                save_slot_to_history(
                    owner,
                    slot,
                    expected_slot_name=slot.key,
                    force=True,
                    refuse_stale_empty_merge=True,
                    takeover_basis=takeover_basis,
                )
            else:
                save_slot_to_history(
                    owner, slot, expected_slot_name=slot.key, takeover_basis=takeover_basis
                )
        except Exception:
            # A failed write remains owed to the next periodic pass.
            self._logger_provider().warning("Flush failed for slot %s", slot.key, exc_info=True)
        else:
            if slot._dirty_gen == generation:
                slot._dirty = False

    def _flush_dirty_slots(self, owner: Any) -> None:
        """Persist dirty transcripts, open tabs, then context snapshots."""
        if not owner.conversation_log:
            return

        for slot in list(owner._slots.values()):
            # Skip a slot still under construction: its transcript is
            # mid-hydration and its constructor persists it at its own tail
            # (import saves after joining Layer B). A background flush landing
            # here would write a half-built transcript and, worse, could win the
            # race against the constructor's own save. The slot stays registered
            # for resume dedup; it is simply not persisted by anyone but its
            # constructor until construction ends.
            if slot.key in getattr(owner, "_slots_under_construction", ()):
                continue
            # The takeover watch opens HERE, while this pass can still vouch
            # that ``slot`` came out of the live table: the per-slot saves
            # below take real disk time each, so a same-key close-and-recreate
            # can replace a later slot long before its turn comes. The re-read
            # below is the vouch itself, checked AFTER the open: a takeover
            # before the open changes the occupant (skip), one after it moves
            # the watch count past the basis (a truncating retry's claim is
            # born overtaken), so no arrival instant lands the popped slot's
            # window on the replacement's transcript. A skipped slot is not
            # flushed by anyone again — the close flow that popped it owns its
            # tail, exactly as it does for every slot it pops.
            basis = SlotRegistry.open_takeover_basis(owner, slot.key)
            try:
                if owner._slots.get(slot.key) is not slot:
                    self._logger_provider().debug(
                        "Flush skipped for slot %s: the key changed owner after this "
                        "pass snapshotted the slot table",
                        slot.key,
                    )
                    continue
                flush_slot_now = self._owner_method(owner, "flush_slot_now", self.flush_slot_now)
                flush_slot_now(slot, takeover_basis=basis)
            finally:
                SlotRegistry.close_takeover_basis(owner, basis)

        # Preserve the original ordering. Open tabs are the authoritative set
        # used to prune context snapshots, and both disk writes stay off-loop.
        persist_open_slots = self._owner_method(
            owner, "_persist_open_slots", self._persist_open_slots
        )
        persist_open_slots()
        persist_context_snapshots = self._owner_method(
            owner,
            "_persist_context_snapshots",
            self._persist_context_snapshots,
        )
        persist_context_snapshots()

    def _read_persisted_open_slot_keys(self, path: Path) -> list[str] | None:
        """Return the raw string ``keys`` already on disk.

        Used only to merge the existing seed into a pre-restore snapshot so a
        flush in the boot window cannot shrink it. Touches no slot state, so it
        is safe to call under the periodic flush.

        Returns an empty list when the file is genuinely absent (no seed yet) or
        holds no usable ``keys`` -- a merge against nothing is a safe no-op.
        Returns ``None`` on a TRANSIENT read failure (e.g. a Windows sharing
        violation from a foreign handle open on the file): the seed exists but
        could not be read, so the caller must NOT write -- a merge against an
        empty read would shrink the file and lose the very tabs the seed holds.
        """
        try:
            raw = self._json_codec_provider().loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            # The seed may well exist and be non-empty; we just could not read
            # it this instant. Signal "unknown" so the caller skips the write
            # and preserves the on-disk seed for the next flush to retry.
            self._logger_provider().debug(
                "open_slots.json unreadable; skipping pre-restore write", exc_info=True
            )
            return None
        keys = raw.get("keys") if isinstance(raw, dict) else None
        if not isinstance(keys, list):
            return []
        return [key for key in keys if isinstance(key, str)]

    def _persist_open_slots(self, owner: Any) -> None:
        """Atomically snapshot the current persistent open-slot keys."""
        if owner.restoring_open_slots:
            self._logger_provider().debug("open_slots snapshot skipped: restore in progress")
            return
        try:
            path = self._config_dir_provider() / "open_slots.json"
            # Incognito, temporary, and future non-persistent modes must never
            # be resurrected by a later gateway process.
            keys = [
                name
                for name, slot in list(owner._slots.items())
                if getattr(slot, "memory_mode", "persistent") == "persistent"
                # A slot still under construction is not yet a session; leaving it
                # out of the restart-restore set means a crash mid-build does not
                # resurrect a half-hydrated slot. Its constructor adds it on the
                # normal flush once construction ends.
                and name not in getattr(owner, "_slots_under_construction", ())
            ]
            # A transient read failure during restore must not let the next
            # live-slot snapshot erase the unread key permanently. The restore
            # guard above also protects iteration from its sole mutator.
            seen = set(keys)
            keys.extend(
                key
                for key in getattr(owner, "unrestored_slot_keys", frozenset())
                if key not in seen
            )
            # The periodic flush loop is armed BEFORE the startup open-tab
            # restore runs, so a flush can fire while ``_slots`` has not been
            # populated yet. In that gap ``_slots`` is not the authoritative
            # open-tab set: it is empty, or holds only a tab created during boot
            # (the HTTP listener binds before restore). Pruning the seed down to
            # it would drop every tab the restore has yet to read, so the NEXT
            # restart places those sessions in "older sessions". Until the
            # restore latch flips, MERGE instead of prune: fold the existing
            # on-disk seed in so a crash in this window keeps both the seeded
            # tabs and any boot-time tab. After restore has run, _slots is
            # authoritative and the merge is a no-op (its keys are already live).
            if not getattr(owner, "open_slots_restored", False):
                seed = self._read_persisted_open_slot_keys(path)
                if seed is None:
                    # The seed exists but could not be read this instant (a
                    # transient failure such as a Windows sharing violation).
                    # Merging an empty read would shrink the file and lose the
                    # tabs the seed holds, so skip the whole write and let the
                    # next flush retry against the intact on-disk seed.
                    self._logger_provider().debug(
                        "open_slots snapshot skipped: pre-restore seed unreadable"
                    )
                    return
                for key in seed:
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
            payload = self._json_codec_provider().dumps(
                {"keys": keys, "ts": self._wall_time_provider()}
            )
            # The canonical writer uses a unique temporary file, which avoids
            # collisions between the periodic and shutdown flush threads.
            self._atomic_write_provider()(path, payload, mode=0o600)
        except Exception:
            self._logger_provider().debug("Failed to persist open_slots.json", exc_info=True)

    def broadcast_context_usage(
        self,
        owner: Any,
        slot_key: str,
        payload: dict,
    ) -> None:
        """Broadcast one context reading and record its durable snapshot."""
        owner.broadcast_ws("context_usage", payload)
        slot = owner.get_slot(slot_key)
        if slot is None:
            return

        # WebSocket broadcast is invisible to SSE-only consumers. Feed the
        # identical payload to the slot queue as a wire-only frame before any
        # persistence eligibility checks.
        try:
            encoded = self._json_codec_provider().dumps(payload)
            slot.push_wire_frame("context_usage", encoded)
        except (TypeError, ValueError):
            pass

        if getattr(slot, "memory_mode", "persistent") != "persistent":
            return
        pct = payload.get("pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            return
        snapshot: dict[str, Any] = {"pct": pct, "model": slot.model}
        window = payload.get("window_tokens") or 0
        if window:
            snapshot["window_tokens"] = window
            snapshot["used_tokens"] = payload.get("used_tokens", 0)
        with owner._context_snapshots_lock:
            if owner._context_snapshots.get(slot_key) == snapshot:
                return
            owner._context_snapshots[slot_key] = snapshot
            owner._context_snapshots_dirty = True

    def ensure_context_snapshots_loaded(self, owner: Any) -> bool:
        """Merge earlier-process snapshots into memory without overwriting live data.

        Returns ``True`` once the disk copy is merged (or was merged earlier).
        Returns ``False`` when the file exists but could not be READ this
        instant (an ``OSError`` such as a Windows sharing violation): its
        content is unknown and may be intact, so the loaded flag stays unset
        and the next call retries. Latching an empty map here would let the
        next flush replace the file with only the live readings and destroy
        every other tab's saved reading. Content that was read but does not
        parse (truncated or non-object JSON) holds nothing recoverable, so it
        is treated as empty and latched, and the next write repairs the file.
        """
        with owner._context_snapshots_lock:
            if owner._context_snapshots_loaded:
                return True
        try:
            text = (self._config_dir_provider() / "context_snapshots.json").read_text()
        except FileNotFoundError:
            text = None
        except OSError:
            self._logger_provider().debug(
                "context_snapshots.json unreadable; not latching, will retry",
                exc_info=True,
            )
            return False
        raw: Any = {}
        if text is not None:
            try:
                raw = self._json_codec_provider().loads(text)
            except Exception:
                self._logger_provider().debug(
                    "context_snapshots.json corrupt; starting empty",
                    exc_info=True,
                )
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        with owner._context_snapshots_lock:
            if owner._context_snapshots_loaded:
                return True
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, dict):
                    owner._context_snapshots.setdefault(key, value)
            # Publish the loaded flag only after the merge, under the same lock.
            owner._context_snapshots_loaded = True
        return True

    @staticmethod
    def context_snapshot_for(owner: Any, slot_key: str) -> dict | None:
        """Return a detached copy of a slot's recorded context reading."""
        with owner._context_snapshots_lock:
            snapshot = owner._context_snapshots.get(slot_key)
            return dict(snapshot) if isinstance(snapshot, dict) else None

    def _persist_context_snapshots(self, owner: Any) -> None:
        """Prune and atomically write the current context-snapshot map."""
        if owner.restoring_open_slots:
            self._logger_provider().debug("context snapshot flush skipped: restore in progress")
            return
        # Same pre-restore gap as _persist_open_slots: the flush loop is armed
        # before the open-tab restore, and this write prunes the snapshot map
        # down to ``set(owner._slots)``. In the gap that set is empty (or holds
        # only a boot-time tab), so pruning would delete the context readings of
        # every tab the restore has yet to rebuild. Until the restore latch
        # flips, write WITHOUT pruning: ``ensure_loaded()`` below folds the disk
        # snapshots into memory, so the write is the union of disk and live
        # readings and a crash in this window loses nothing. After restore has
        # run, _slots is authoritative and the prune resumes.
        prune = bool(getattr(owner, "open_slots_restored", False))
        with owner._context_snapshots_lock:
            if not owner._context_snapshots_dirty:
                return

        # Disk is merged before pruning so a new reading cannot overwrite
        # still-live readings left by an earlier process.
        ensure_loaded = self._owner_method(
            owner,
            "ensure_context_snapshots_loaded",
            self.ensure_context_snapshots_loaded,
        )
        if ensure_loaded() is False:
            # The file exists but could not be read this instant, so its
            # readings are not in memory. Writing now would replace them with
            # only the live readings: before restore that is every tab the
            # restore has yet to rebuild, after restore every open tab with no
            # fresh reading in this process. Skip the write and keep the map
            # dirty; the next flush retries the read against the intact file.
            # Same rule as the pre-restore open_slots.json seed read.
            self._logger_provider().debug(
                "context snapshot flush skipped: existing snapshots unreadable"
            )
            return

        # Serialize complete flushes. The data lock intentionally excludes IO,
        # but the flush lock prevents an older stalled write from landing after
        # a newer one and rolling the file back.
        with owner._context_snapshots_flush_lock:
            try:
                with owner._context_snapshots_lock:
                    owner._context_snapshots_dirty = False
                    if prune:
                        live_keys = set(owner._slots)
                        for key in [
                            key for key in owner._context_snapshots if key not in live_keys
                        ]:
                            del owner._context_snapshots[key]
                    payload = self._json_codec_provider().dumps(owner._context_snapshots)
                self._atomic_write_provider()(
                    self._config_dir_provider() / "context_snapshots.json",
                    payload,
                    mode=0o600,
                )
            except Exception:
                self._logger_provider().debug(
                    "Failed to persist context_snapshots.json", exc_info=True
                )
                with owner._context_snapshots_lock:
                    owner._context_snapshots_dirty = True
