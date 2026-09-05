"""Durable in-flight handoff journal — the half of the invariant a restart needs.

The single-owner invariant says any failure short of a durable ack leaves the
SOURCE owning executable work. One ordering escapes that: the target acked and
the source died before it tombstoned. Both crews then hold a claim, and
``MigrationCoordinator.reconcile`` exists to collapse it to exactly one.

But ``reconcile`` needs a ``handoff_id``, and after a crash that id lives only in
the memory of the process that died. Without a durable record written BEFORE the
unit is transmitted, a rebooted gateway cannot enumerate what to reconcile, so
the window stays open forever. That is why nothing called ``migrate()``: wiring
transmit without this would manufacture windows nothing can close.

This journal is deliberately NOT the tombstone registry. A tombstone is a
settled redirect ("this went there"); a journal entry is an unsettled question
("did this land?"). Conflating them would make an in-flight handoff look like a
completed move to every surface that lists tombstones — the exact misreading
Req 7.3 exists to prevent.

Entries are removed once resolved. A journal that retained settled handoffs
would have every restart replay history.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..atomic_write import atomic_write

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InFlightHandoff:
    """One handoff that was transmitted but not yet known to be settled.

    ``quiesce_token`` is carried because the source-reclaim path has to
    ``unquiesce`` with the same opaque proof the dead process was handed. A
    reborn process that could not reclaim would leave the unit quiesced and
    non-executing on both sides — no owner at all, which is as wrong as two.
    """

    handoff_id: str
    unit_id: str
    kind: str
    target_crew_id: str
    quiesce_token: str | None = None


class MigrationJournal:
    """Append/remove durable records of handoffs whose outcome is still unknown.

    Same durability discipline as ``TombstoneRegistry``: one fsync'd atomic JSON
    write, and a corrupt file degrades to "nothing in flight" rather than
    breaking every caller. Degrading is safe here in a way it is not for
    tombstones: losing a journal entry means a window goes unreconciled and is
    surfaced by the ``unreconciled-handoffs`` band, whereas raising would stop a
    gateway from booting at all.
    """

    _FILE = "inflight.json"

    def __init__(self, *, store_dir: Path | str) -> None:
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._dir / self._FILE

    # -- io -------------------------------------------------------------------

    def _read_all(self) -> dict[str, dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("in-flight journal unreadable at %s", self.path)
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("in-flight journal is not valid JSON at %s", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict) -> None:
        # fsync: this is the ONLY record that a handoff was in flight. Lost to a
        # power cut, the ack->tombstone window becomes unreconcilable — the very
        # failure this file exists to prevent.
        atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True), fsync=True)

    # -- api ------------------------------------------------------------------

    def open(self, entry: InFlightHandoff) -> None:
        """Record a handoff as in flight. MUST be called before transmitting."""
        data = self._read_all()
        data[entry.handoff_id] = {
            "handoff_id": entry.handoff_id,
            "unit_id": entry.unit_id,
            "kind": entry.kind,
            "target_crew_id": entry.target_crew_id,
            "quiesce_token": entry.quiesce_token,
        }
        self._write_all(data)

    def close(self, handoff_id: str) -> None:
        """Drop a settled handoff. Idempotent: closing an absent id is a no-op."""
        data = self._read_all()
        if data.pop(handoff_id, None) is not None:
            self._write_all(data)

    def outstanding(self) -> list[InFlightHandoff]:
        """Every handoff whose outcome is still unknown, oldest id order.

        Sorted so a boot sequence is deterministic and reproducible in a test;
        an unordered dict iteration would make a multi-entry failure depend on
        insertion history nobody can see.
        """
        out: list[InFlightHandoff] = []
        for raw in self._read_all().values():
            if not isinstance(raw, dict):
                continue
            hid, uid = raw.get("handoff_id"), raw.get("unit_id")
            if not isinstance(hid, str) or not isinstance(uid, str) or not hid or not uid:
                # A record missing its identity cannot be reconciled; skipping is
                # better than crashing a boot, and the band reports the gap.
                logger.warning("in-flight journal entry has no usable identity; skipped")
                continue
            tok = raw.get("quiesce_token")
            out.append(
                InFlightHandoff(
                    handoff_id=hid,
                    unit_id=uid,
                    kind=str(raw.get("kind") or ""),
                    target_crew_id=str(raw.get("target_crew_id") or ""),
                    quiesce_token=tok if isinstance(tok, str) else None,
                )
            )
        return sorted(out, key=lambda e: e.handoff_id)
