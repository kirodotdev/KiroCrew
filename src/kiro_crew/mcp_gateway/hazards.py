"""Record, per server, the moments a shared backend betrayed per-client state.

The gateway already detects two behaviours that PROVE a server is not safe to
share, and today it only debug-logs them:

* a server-initiated request with no ``relatedRequestId`` while more than one
  stub is attached — unroutable without a cross-tenant leak, so the backend is
  recycled;
* a request-scoped notification that cannot be attributed to a pending call —
  dropped rather than broadcast.

Both are observations of real traffic, which makes them the strongest evidence
this system can have, and strictly better than any declaration a server makes
about itself. This ledger is what carries them out of gatewayd's memory so the
dashboard can withdraw a recommendation it previously made.

Design constraints this shape satisfies
---------------------------------------
* **Cross-process.** The observer is gatewayd; the reader is the dashboard, a
  different process. A file is the only channel they already share, and
  ``hot-keys.json`` in this same directory is the precedent.
* **Never blocks the loop.** Callers record into memory synchronously and flush
  off-loop. A hazard that is only in memory is still correct for the current
  process; losing the last flush costs a recommendation withdrawal, never
  correctness of routing.
* **Monotonic, and bounded.** Hazards accumulate but the per-server record is
  collapsed to a set of codes plus a count and a last-seen wall clock, so the
  file cannot grow with traffic.
* **Never fabricates.** A missing or corrupt file reads as "no hazards
  observed", which is the truth: we have not seen one. It does NOT read as
  "safe" — safety is decided by ``shareability.assess``, which treats absence
  of hazards as absence of evidence.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_gateway.record_json import finite_float

logger = logging.getLogger(__name__)

HAZARDS_FILENAME = "observed-hazards.json"

# Stable codes. These are the vocabulary the UI translates, so they are part of
# the contract and must not be renamed without updating the catalogs.
HAZARD_UNROUTABLE_SERVER_REQUEST = "unroutable_server_request"
HAZARD_UNATTRIBUTABLE_NOTIFICATION = "unattributable_notification"

_KNOWN_HAZARDS = frozenset(
    {HAZARD_UNROUTABLE_SERVER_REQUEST, HAZARD_UNATTRIBUTABLE_NOTIFICATION}
)

_SCHEMA = 1


@dataclass
class _Record:
    codes: set[str] = field(default_factory=set)
    count: int = 0
    last_seen: float = 0.0


class HazardLedger:
    """Per-server hazard observations, persisted as one small JSON object.

    Not thread-safe by design: gatewayd records from its event loop, and the
    dashboard only ever reads. A reader that races a writer sees either the old
    or the new file because the write is atomic — never a torn one.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, _Record] = {}
        self._dirty = False
        # Bumped by every ``record``. ``flush`` captures it alongside the payload
        # so a concurrent observation cannot be marked persisted.
        self._generation = 0
        # Held across a whole flush. More than one caller flushes (the periodic
        # sweep and the shutdown path), both from worker threads, so without this
        # the later WRITE can carry the earlier READ and revert what is on disk.
        self._flush_lock = threading.Lock()

    # -- writer side (gatewayd) ------------------------------------------

    def record(self, server_name: str, code: str) -> bool:
        """Note that *server_name* exhibited *code*. Returns True if new.

        Unknown codes are refused rather than stored: the vocabulary is a
        contract with the UI, and a typo that silently became a permanent
        "hazard" would disqualify a server forever with no way to read why.
        """
        if code not in _KNOWN_HAZARDS:
            logger.warning("hazards: refusing unknown code %r for %r", code, server_name)
            return False
        if not server_name:
            return False
        rec = self._records.setdefault(server_name, _Record())
        is_new = code not in rec.codes
        rec.codes.add(code)
        rec.count += 1
        rec.last_seen = time.time()
        self._dirty = True
        self._generation += 1
        return is_new

    def flush(self) -> None:
        """Persist if anything changed. Safe to call often; cheap when clean.

        Serialized end to end, because there is more than one flush caller and
        they run in worker threads. The periodic sweep and the shutdown flush can
        overlap — cancellation starts the second while the first is mid-write —
        and each builds its payload from the in-memory records before writing. Two
        unsynchronised flushes therefore race, and the one that happens to write
        last wins even if it read an OLDER snapshot, silently reverting an
        observation that was already on disk. Holding the lock across build AND
        write makes the last writer the last reader too.

        The generation guard inside handles the other direction: ``record`` runs
        ON the event loop while this runs off it, so an observation can land
        between the payload being built and the dirty flag being cleared.
        Clearing unconditionally would mark it persisted and lose it for good, so
        dirty is cleared only when nothing moved in between.
        """
        with self._flush_lock:
            if not self._dirty:
                return
            generation = self._generation
            payload: dict[str, Any] = {
                "schema": _SCHEMA,
                "servers": {
                    name: {
                        "codes": sorted(rec.codes),
                        "count": rec.count,
                        "lastSeen": rec.last_seen,
                    }
                    for name, rec in sorted(self._records.items())
                },
            }
            try:
                atomic_write(self._path, json.dumps(payload, indent=2) + "\n")
            except OSError as exc:
                # A ledger we cannot persist must not take the daemon down: the
                # in-memory copy still guards this process, and the cost of the
                # failure is a stale recommendation, not a routing error.
                logger.warning("hazards: could not write %s: %s", self._path, exc)
                return
            if self._generation == generation:
                self._dirty = False

    # -- reader side (dashboard) -----------------------------------------

    def load(self) -> None:
        """Read the file into memory. Absence and corruption both mean empty."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Same gap as the verdict cache, but this loader runs from
            # ``install_sink`` during gatewayd startup, so an uncaught decode
            # error keeps the daemon from binding at all.
            logger.warning("hazards: ignoring unreadable %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
            # A future schema is not an error and not a hazard — a newer writer
            # simply knows more than this reader. Reading it as "no hazards"
            # loses a withdrawal; guessing at its shape would invent one.
            return
        servers = raw.get("servers")
        if not isinstance(servers, dict):
            return
        for name, entry in servers.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            codes = entry.get("codes")
            if not isinstance(codes, list):
                continue
            kept = {c for c in codes if isinstance(c, str) and c in _KNOWN_HAZARDS}
            if not kept:
                continue
            count = entry.get("count")
            last = entry.get("lastSeen")
            self._records[name] = _Record(
                codes=kept,
                count=count if isinstance(count, int) and count >= 0 else len(kept),
                last_seen=finite_float(last),
            )

    def codes_for(self, server_name: str) -> tuple[str, ...]:
        """Hazard codes observed for *server_name*, oldest-agnostic, sorted."""
        rec = self._records.get(server_name)
        return tuple(sorted(rec.codes)) if rec else ()

    def as_dict(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(sorted(rec.codes)) for name, rec in self._records.items()}


def ledger_path(runtime_dir: Path) -> Path:
    """Sibling of ``hot-keys.json`` in the gateway runtime directory."""
    return runtime_dir / HAZARDS_FILENAME


def load_ledger(runtime_dir: Path) -> HazardLedger:
    """Read-only convenience for the dashboard side."""
    ledger = HazardLedger(ledger_path(runtime_dir))
    ledger.load()
    return ledger


# -- process-wide sink (gatewayd) ----------------------------------------
#
# The observation sites sit deep in per-request routing, where threading a
# ledger handle through every frame would touch code that has nothing to do
# with this feature. A process-global sink is the same ownership shape the
# daemon already uses for its hot-key store, and the default is a no-op so
# unit tests and the stub process never need to install anything.

_sink: HazardLedger | None = None


def install_sink(runtime_dir: Path) -> HazardLedger:
    """Bind the process-wide sink. Called once by gatewayd at startup."""
    global _sink
    _sink = HazardLedger(ledger_path(runtime_dir))
    _sink.load()  # keep prior observations; a restart must not forget them
    return _sink


def record_observed(server_name: str, code: str) -> bool:
    """Note a hazard on the process sink. In-memory only; never touches disk.

    Safe to call from the event loop precisely because it does no IO — the
    flush is a separate, off-loop step. Returns True when this is the first
    time *code* was seen for *server_name*, so a caller can log it once at a
    louder level than the per-occurrence debug line.
    """
    if _sink is None:
        return False
    return _sink.record(server_name, code)


def flush_sink() -> None:
    """Persist the sink. Blocking IO — call via ``asyncio.to_thread``."""
    if _sink is not None:
        _sink.flush()
