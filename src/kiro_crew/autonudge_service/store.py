"""``autonudge.json`` and ``autonudge.quarantine.json``: the durable store's state and protocol.

:class:`LoopStore` owns everything about the store that is not a live loop: the two
paths, whether the last load could vet the store at all, the rows held aside in the
sidecar and the rows the loader could not parse, the disk-only cycle-claim markers,
the stop-record baseline of the last committed store, and the removals whose durable
write has not landed yet. It writes a payload atomically (temp file, fsync, sidecar
first, rename as the commit point, stop record under the commit lock, directory sync,
sidecar compaction) and builds every payload's ``loops`` list so no writer can omit a
held or unparsed row.

:class:`~kiro_crew.autonudge.AutoNudgeService` composes one ``LoopStore`` and keeps the
entry points callers and tests patch (``_write_state``, ``_save``, ``_serialize_state``,
``_serialized_loops``). The loader that decides what each stored row MEANS stays on the
service in :mod:`kiro_crew.autonudge`, because it vets and scrubs every row at the trust
boundary; it reads and records through this store.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from kiro_crew import autonudge_stop_log, platform_compat
from kiro_crew.autonudge_service.model import AutoNudgeStoreUnvetted, NudgeLoop
from kiro_crew.monitoring.models import monitor_state_to_dict

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


_NUDGES_FILE = "autonudge.json"
# A build predating the ``quarantined`` key writes only ``autonudge.json``, so an
# embedded copy dies with its next wholesale write; a sidecar it never opens survives.
_QUARANTINE_FILE = "autonudge.quarantine.json"
_STORE_VERSION = 1


def _quarantine_row_key(row: dict) -> str:
    """Stable identity for a held-aside row, for de-duplicating an additive write.

    The WHOLE serialized row, never just ``id``: two held rows can share an id while
    differing in content, and collapsing those drops the copy an operator repaired --
    which a failed main-store replacement then loses permanently.
    """
    return json.dumps(row, sort_keys=True, default=repr)


def _rows_or_empty(value: Any) -> list:
    """Return ``value`` if it is a list, else ``[]``.

    ``data.get(key, [])`` yields the default only when the key is ABSENT, so a
    hand-edited store carrying ``"loops": null`` returns ``None`` and every
    iteration or unpack of it raises ``TypeError`` uncaught during startup.
    """
    return value if isinstance(value, list) else []


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


class LoopStore:
    """The durable store of one data home: its state and its file protocol.

    Composed once by :class:`~kiro_crew.autonudge.AutoNudgeService`. Everything here is
    either on disk or about disk: the live loops themselves stay on the service and are
    passed in to :meth:`serialized_loops`, with the service's own row serializer.
    """

    def __init__(self, base_dir: Path) -> None:
        self.path = base_dir / _NUDGES_FILE
        self.quarantine_path = base_dir / _QUARANTINE_FILE
        # Stop record (see autonudge_stop_log): the loops ACTIVE in the last store this
        # instance committed or loaded, and the reason a REMOVAL in flight gives for
        # the row it is deleting. ``commit_lock`` spans the rename and the diff so
        # commit order and diff order are one order; only worker threads take it.
        # ``stop_notes`` holds an entry only while its removal's write is in flight
        # and is touched with single dict operations, so the event loop never waits.
        self.committed_active: dict[str, dict[str, Any]] = {}
        self.stop_notes: dict[str, tuple[str, str]] = {}
        self.commit_lock = threading.Lock()
        # Rows withheld from the live map but preserved on disk for repair. Kept off
        # every egress path because ADDRESSING_FIELDS are exempt from the scrub.
        self.quarantined: list[dict] = []
        # Whole-row keys THIS instance enumerated from the sidecar at load. Compaction may
        # remove only these: a row it never saw belongs to a writer it cannot account for.
        self.sidecar_seen: set[str] = set()
        # Rows ``_load`` could not parse, kept VERBATIM so a rewrite round-trips them:
        # skipping a row must not delete the entry the operator was warned to repair.
        self.unparsed_rows: list[Any] = []
        # Persisted but rolled back in memory for the delivery window, so another
        # writer snapshotting mid-turn records the claim rather than erasing it.
        self.delivering_claim: dict[str, tuple[int, float]] = {}
        #: loop id -> the claimed cycle that loaded unresolved, awaiting a
        #: deliberate re-activation to settle it.
        self.unreconciled_claim: dict[str, int] = {}
        #: loop ids whose claim is held for a turn the fire path reported did NOT go out,
        #: so reactivation retries the release rather than charging the reader for it.
        self.undelivered_claim: set[str] = set()
        # Depth, not a flag: the sidecar lock is taken on nested paths within one process.
        self._sidecar_lock_depth = 0
        # Loop ids removed from memory whose durable state write has not yet
        # succeeded. A caller may retry remove(id) after the first write fails;
        # an arbitrary unknown id remains a no-op.
        self.pending_removals: set[str] = set()
        # Set when ``_load`` could not vet the store at all, so an empty map means
        # "could not vet" rather than "empty" and every persist raises, never deletes.
        self.load_refused: bool = False

    def serialized_loops(
        self,
        loops: Mapping[str, NudgeLoop],
        serialize: Callable[[NudgeLoop], dict[str, Any]],
        *,
        replace: dict[str, NudgeLoop] | None = None,
        skip: set[str] | None = None,
        extra: list[NudgeLoop] | None = None,
    ) -> list[Any]:
        """Build EVERY store payload's ``loops`` list, so no writer can omit a row.

        Two classes of row are invisible in ``_loops`` and were dropped by any builder that
        walked it directly -- which the monitor paths did, replacing the whole store:

        * a row ``_load`` could not PARSE, held verbatim in ``unparsed_rows``;
        * a cycle CLAIM persisted before a turn went out and rolled back in memory for the
          delivery window, which a concurrent write would otherwise erase.
        """
        rows: list[Any] = []
        for candidate in list(loops.values()) + list(extra or []):
            if skip and candidate.id in skip:
                continue
            row = serialize((replace or {}).get(candidate.id, candidate))
            claimed = self.delivering_claim.get(candidate.id)
            if claimed is not None:
                # BESIDE the spent count, never inside it: a restart must be able to tell a
                # claimed-but-undelivered cycle from a spent one. Disk-only, so no client sees it.
                row["inflight_cycle"], row["last_fire_ts"] = claimed
            elif candidate.id in self.unreconciled_claim:
                # Carried from an earlier run: dropping it here would leave the next restart
                # with no marker at all, and re-activation would then replay that turn.
                row["inflight_cycle"] = self.unreconciled_claim[candidate.id]
            if candidate.id in self.undelivered_claim and "inflight_cycle" in row:
                # Disk-only, like the claim it qualifies: without it a restart cannot tell a
                # turn that never went out from one whose delivery is merely unknown.
                row["inflight_undelivered"] = True
            rows.append(row)
        return rows + list(self.unparsed_rows)

    @staticmethod
    def serialize_loop(loop: NudgeLoop) -> dict[str, Any]:
        payload = asdict(loop)
        # In-memory only: the load path re-mints it unconditionally, so a persisted
        # value could never be honoured and writing one would dirty a clean store.
        payload.pop("goal_token", None)
        if loop.monitor is None:
            # Preserve the legacy wire shape instead of eagerly migrating every
            # record the next time an unrelated loop is saved.
            payload.pop("monitor", None)
        else:
            payload["monitor"] = monitor_state_to_dict(loop.monitor)
        return payload

    def write_state(self, payload: dict) -> None:
        from kiro_crew import autonudge as seams  # read at call time: the facade imports us

        # Atomic write: serialize to a temp file in the same dir, fsync, then
        # replace onto the target path. Eliminates the truncate-before-
        # flock race that plain open(path, "w") has — readers always see either
        # the old complete file or the new complete file, never a partial one.
        # The rename goes through replace_with_retry because on Windows it can
        # fail with PermissionError while another handle is transiently open on
        # the fresh temp file (indexer / AV), which loses the write.
        # Blocking (fsync) — async callers offload this to an executor.
        # Every mutation caller wraps its persist in ``except BaseException`` and rolls
        # back, so reporting success here confirmed a row that vanished on restart.
        if self.load_refused:
            raise AutoNudgeStoreUnvetted(
                "refusing to persist -- the last load could not vet the store, so the "
                "in-memory list is empty for that reason rather than because the store "
                "is empty. Fix the store entry or the host's credential policy and "
                "restart; the file on disk is untouched."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            # BEFORE the main store lands: if this raises, the file on disk is still the
            # old consistent one rather than a new one whose rows have no durable copy.
            self._write_quarantine_sidecar()
            with self.commit_lock:
                seams.replace_with_retry(tmp_path, self.path)
                self._record_stops_committed(payload)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        # The rename is the COMMIT POINT, so nothing past it may raise: the caller rolls
        # its loop back on an exception while disk KEEPS the change.
        try:
            seams.fsync_dir(self.path.parent)
        except OSError:
            # Compaction DELETES rows and its durability rests on this sync, so an
            # unsynced store keeps the superset exactly as a failed compaction does.
            logger.warning(
                "autonudge: could not sync the store directory after a committed write; "
                "the write STANDS and the quarantine superset is kept uncompacted",
                exc_info=True,
            )
            return
        # Non-fatal for the same reason. Compaction drops only rows this write observed,
        # so a failure leaves a superset that the next successful write retries.
        try:
            self._compact_quarantine_sidecar()
        except Exception:
            logger.warning(
                "autonudge: could not compact the quarantine sidecar after a committed "
                "store write; the durable copy is kept and the next write retries",
                exc_info=True,
            )

    def _record_stops_committed(self, payload: dict) -> None:
        """Log and record every loop this just-committed store stopped.

        Runs under ``commit_lock`` right after the rename. The commit already
        stands, so nothing here may raise: a failed record costs the record, never
        the write.
        """
        rows = payload.get("loops") or []
        try:
            records = autonudge_stop_log.stop_records(self.committed_active, rows, self.stop_notes)
            for record in records:
                autonudge_stop_log.log_record(record)
                autonudge_stop_log.append_record(self.path.parent, record)
        except Exception:  # noqa: BLE001 - the store write already committed
            logger.warning("AutoNudge: could not record a loop stop", exc_info=True)
        finally:
            # Reseeded even when recording failed: a baseline left stale would make
            # every later commit fail the same way and report nothing again.
            try:
                self.committed_active = autonudge_stop_log.active_summaries(rows)
            except Exception:  # noqa: BLE001 - see above
                self.committed_active = {}

    def read_quarantine_sidecar(self) -> list:
        """Read held-aside rows from the sidecar, tolerating absence but not corruption.

        A missing file is the normal case and reads as "nothing held aside".

        Content we cannot parse is different, and returning ``[]`` for it was a data-loss
        path: the loader would report nothing held aside, and the next write would call
        ``_drop_quarantine_sidecar`` and UNLINK the only surviving copy of rows the
        loader itself refused. So an unreadable or wrongly-shaped sidecar refuses every
        persist in this process with ``AutoNudgeStoreUnvetted``, and MOVES THE FILE ASIDE
        under a ``.corrupt-<ts>`` name so recovery is a restart rather than a human
        editing JSON -- the bytes an operator needs are preserved either way.

        ``_load`` also ARMS NOTHING once this flag is set. Arming while writes are refused
        is worse than arming nothing: a delivered cycle cannot persist its counter, so a
        restart re-fires it past its own cycle cap.
        """
        try:
            raw = json.loads(self.quarantine_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning(
                "autonudge: quarantine sidecar at %s is unreadable; refusing writes so "
                "it is not replaced or unlinked before it can be recovered",
                self.quarantine_path,
            )
            self._refuse_writes_and_preserve_sidecar()
            return []
        # Not a startup failure on its own -- `raw.get` would raise AttributeError straight
        # out of `_load` -- but it is still an unreadable copy, so writes stay refused.
        if not isinstance(raw, dict):
            logger.warning(
                "autonudge: quarantine sidecar at %s is not an object (%s); refusing "
                "writes so it is not replaced or unlinked",
                self.quarantine_path,
                type(raw).__name__,
            )
            self._refuse_writes_and_preserve_sidecar()
            return []
        # `_rows_or_empty` answers a dict- or scalar-shaped value with [], which reads as
        # "nothing is held aside" and lets the next persist unlink the only copy.
        if "quarantined" in raw and not isinstance(raw["quarantined"], list):
            logger.warning(
                "autonudge: quarantine sidecar at %s has a non-list `quarantined` (%s); "
                "refusing writes so it is not replaced or unlinked",
                self.quarantine_path,
                type(raw["quarantined"]).__name__,
            )
            self._refuse_writes_and_preserve_sidecar()
            return []
        # ABSENT is not EMPTY: `raw.get` answers a dict with no `quarantined` key with None,
        # which read as "nothing held aside" and let the next persist unlink the only copy.
        if "quarantined" not in raw:
            logger.warning(
                "autonudge: quarantine sidecar at %s has no `quarantined` key; refusing "
                "writes so it is not replaced or unlinked",
                self.quarantine_path,
            )
            self._refuse_writes_and_preserve_sidecar()
            return []
        rows = _rows_or_empty(raw["quarantined"])
        # FILTERING a non-dict member would silently shrink the held-aside set and let the
        # load proceed, so an unreadable member refuses the store exactly as a bad file does.
        if any(not isinstance(row, dict) for row in rows):
            logger.warning(
                "autonudge: quarantine sidecar at %s holds a non-object entry; refusing "
                "writes so it is not replaced or unlinked",
                self.quarantine_path,
            )
            self._refuse_writes_and_preserve_sidecar()
            return []
        return rows

    def _drop_quarantine_sidecar(self) -> None:
        """Remove the sidecar once no rows remain -- ONLY after the main store landed.

        Removing it is itself the deletion of a durable copy: a row repaired in the
        sidecar and dropped from ``quarantined`` exists nowhere else until the new
        store is on disk, so unlinking before a replacement that can fail would lose
        it permanently.

        """
        if self.quarantined:
            return
        with contextlib.suppress(OSError):
            self.quarantine_path.unlink()

    def _refuse_writes_and_preserve_sidecar(self) -> None:
        """Refuse persistence for THIS process, and move the unreadable file aside.

        Both halves are load-bearing. Refusing keeps the store consistent now, because
        a write would compact around rows nothing enumerated. The move-aside is what
        stops that being a permanent outage: recovery becomes a restart, not a human
        editing JSON, and the original bytes survive under a ``.corrupt-<ts>`` name.
        """
        self.load_refused = True
        self._move_aside_unreadable_sidecar()

    def _move_aside_unreadable_sidecar(self) -> None:
        """Rename an unreadable sidecar so recovery does not need a human repair.

        The bytes are PRESERVED under a ``.corrupt-<ts>`` suffix rather than unlinked --
        an operator still needs them to re-inject the held rows -- but the service can
        persist again after a restart instead of staying down until someone edits JSON.
        """
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = f"{self.quarantine_path.name}.corrupt-{stamp}"
        target = self.quarantine_path.with_name(base)
        try:
            with self._sidecar_transaction():
                # REVALIDATE inside the lock. Detection ran earlier and unlocked, so a peer may
                # have published a readable replacement whose rows this rename would discard.
                if self._quarantine_rows_on_disk() is not None:
                    logger.warning(
                        "autonudge: the quarantine sidecar at %s is readable again -- another "
                        "instance replaced it since detection, so it is left in place",
                        self.quarantine_path,
                    )
                    return
                self._move_aside_locked(target, base)
        except OSError:
            # The LOCK itself can be unopenable -- a directory in its place, an exhausted
            # disk -- and this runs during startup, where raising ends the process.
            logger.warning(
                "autonudge: could not take the quarantine sidecar lock at %s to move the "
                "unreadable file aside; it stays in place and writes remain refused",
                self.quarantine_path,
                exc_info=True,
            )

    def _move_aside_locked(self, target: Path, base: str) -> None:
        """Reserve a free ``.corrupt-`` name and rename the sidecar onto it."""
        # ``replace`` CLOBBERS and the stamp is second-granular, so RESERVE the name with
        # O_EXCL first -- two instances in one second would otherwise destroy these bytes.
        for _ in range(8):
            try:
                os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                break
            except FileExistsError:
                target = self.quarantine_path.with_name(f"{base}-{secrets.token_hex(4)}")
            except OSError:
                break
        try:
            self.quarantine_path.replace(target)
        except OSError:
            logger.warning(
                "autonudge: could not move the unreadable quarantine sidecar at %s "
                "aside; it stays in place and writes remain refused",
                self.quarantine_path,
                exc_info=True,
            )
            return
        logger.warning(
            "autonudge: quarantine sidecar at %s was unreadable and has been moved to "
            "%s; its held-aside rows must be re-injected from there",
            self.quarantine_path,
            target,
        )

    def _quarantine_rows_on_disk(self) -> list[dict] | None:
        """Read the sidecar's rows, or None when it cannot be enumerated."""
        try:
            raw = self.quarantine_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        rows = data.get("quarantined")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None
        return rows

    @contextmanager
    def _sidecar_transaction(self) -> Iterator[None]:
        """Hold an EXCLUSIVE cross-process lock for a sidecar read-modify-write.

        The union below is not atomic across PROCESSES: a second AutoNudge writing the
        same home can add a row between the read and the replace, and the sidecar is that
        row's only durable copy. Within one event loop the pair is synchronous so the
        intra-process race cannot happen -- this closes the inter-process one.

        Distinct from the stat bracket removed earlier: that COMPARED a snapshot identity
        and hoped nothing moved, which POSIX rename cannot make atomic. This EXCLUDES the
        other writer, so there is no window to lose a row in.

        The lock lives on a stable sentinel beside the sidecar rather than on the sidecar
        itself, which is renamed and replaced underneath. Mode ``a+`` is exclusive without
        tripping ``_locked_file``'s seed-a-store-shaped-file branch.
        """
        lock_path = self.quarantine_path.with_name(self.quarantine_path.name + ".lock")
        if self._sidecar_lock_depth:
            # RE-ENTRANT: ``flock`` is per-fd, so a second open here would block on a lock
            # this process already holds, and the nested body is already excluded by it.
            yield
            return
        with _locked_file(lock_path, "a+"):
            self._sidecar_lock_depth += 1
            try:
                yield
            finally:
                self._sidecar_lock_depth -= 1

    def _write_quarantine_sidecar(self) -> None:
        """Publish held-aside rows under the cross-process sidecar lock."""
        with self._sidecar_transaction():
            self._write_quarantine_sidecar_locked()

    def _write_quarantine_sidecar_locked(self) -> None:
        """Persist held-aside rows ADDITIVELY, before the main store replacement.

        Writing only the in-memory set SHRINKS the file whenever a row was repaired
        this pass while a sibling stayed held: if the replacement then fails, that
        repaired row is in neither the reduced sidecar nor the unchanged store. So
        union with what is already on disk, and compact once the store has landed.

        The union is not STAT-BRACKETED. That bracket was a check-then-mutate that could
        not close the window it narrowed, and it made a repair an operator saved inside
        that window destroyable. Exclusion by ``_sidecar_transaction`` replaces it.

        Callers here must NOT re-enter the lock: the flock is per-fd, so a second
        acquisition from this process on a fresh fd would block against itself.
        """
        on_disk = self._quarantine_rows_on_disk()
        if on_disk is None:
            # Fail CLOSED: returning here let the store land and the sidecar compact
            # around rows this process never enumerated, overwriting or unlinking them.
            self._refuse_writes_and_preserve_sidecar()
            raise AutoNudgeStoreUnvetted(
                f"quarantine sidecar at {self.quarantine_path} could not be read, so "
                "this write is refused; it has been moved aside for inspection"
            )
        rows = deepcopy(self.quarantined)
        seen = {_quarantine_row_key(row) for row in rows}
        for row in on_disk:
            key = _quarantine_row_key(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        self._write_quarantine_rows(rows)

    def _compact_quarantine_sidecar(self) -> None:
        """Compact the sidecar under the cross-process sidecar lock."""
        with self._sidecar_transaction():
            self._compact_quarantine_sidecar_locked()

    def _compact_quarantine_sidecar_locked(self) -> None:
        """Reduce the sidecar to rows this write can PROVE it superseded, after the commit.

        Compacting from ``self.quarantined`` alone deletes rows this process never saw. The
        cross-process lock does not help: it SERIALIZES writers, so a peer's row is already
        durably on disk and simply absent from this instance's memory, which is stale rather
        than racing. An empty local set then unlinked the file and took the peer's only
        durable copy with it.

        So the licence to remove a row is having ENUMERATED it at load and not holding
        it. Absence from ``sidecar_seen`` means another writer owns it, and it is kept. The
        file is dropped only when nothing survives that test.

        Called with the lock ALREADY held, so neither the read nor the drop re-enters it.
        """
        on_disk = self._quarantine_rows_on_disk()
        if on_disk is None:
            # Cannot enumerate: keeping the superset is the whole point of the file.
            return
        held = {_quarantine_row_key(row) for row in self.quarantined}
        keep = [
            row
            for row in on_disk
            if _quarantine_row_key(row) in held or _quarantine_row_key(row) not in self.sidecar_seen
        ]
        if not keep:
            self._drop_quarantine_sidecar()
            return
        self._write_quarantine_rows(keep)

    def _write_quarantine_rows(self, rows: list[dict]) -> None:
        """Atomically write exactly ``rows``. Never REMOVES the file -- see the drop half.

        Written from a single sink so EVERY caller is correct by construction rather
        than each having to remember the atomicity and never-unlink invariants.
        """
        from kiro_crew import autonudge as seams  # read at call time: the facade imports us

        if not rows:
            return
        payload = {"version": _STORE_VERSION, "quarantined": rows}
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.quarantine_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            seams.replace_with_retry(tmp_path, self.quarantine_path)
            # Fsyncing the bytes leaves the RENAME unflushed, so a crash could drop
            # these rows from the only place still holding them.
            seams.fsync_dir(self.quarantine_path.parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
