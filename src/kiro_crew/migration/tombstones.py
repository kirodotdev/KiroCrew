"""Durable, queryable tombstone registry — Task 5.2 / Requirement 7.3.

Requirement 7.3: "THE Tombstone SHALL be discoverable from the surface that
listed the unit before the move."

Before this module, every tombstone lived in a per-adapter in-memory dict
(``CronMigrationAdapter._tombstones``, ``slot.new_home``). That failed the
requirement in two ways.

It was not DURABLE. Note carefully what was and was not lost: the double-fire
guard survived a restart, because ``CronJob.enabled=False`` is persisted (see
``cron.py``'s ``"enabled": j.enabled``). So this is not a double-fire bug. What
was lost is the *reason* — on reload ``cron.py``'s ``_record_is_enabled`` path
derives ``user_paused`` from ``not enabled``, so a migrated job reads back as an
ordinary user-paused job. The work had moved to another crew and the surface
that listed it could no longer say so.

And it was not QUERYABLE. A listing surface could only learn about a move by
holding the very adapter instance that performed it — which the Schedule page
and ``kirocrew cron list`` do not.

Design notes:

* One file, keyed ``kind -> unit_id -> tombstone``. Kinds are namespaced because
  a cron job id and a chat slot key may collide as strings while being unrelated
  units.
* ``store_dir`` is injected, following ``taskrunner``'s ``_work_dir / "runs.json"``
  convention, so tests are hermetic and no global path is invented here.
* Reads are defensive: a corrupt file degrades to "nothing moved" rather than
  taking the listing surface down. Losing a redirect hint is recoverable; a
  crashed schedule list hides every job the user has.
* A tombstone is a REDIRECT, not a copy. Only the four Tombstone fields are
  stored — never the bundle payload, which may hold a command line or a
  transcript that has no business in a file whose only job is answering "where
  did this go?".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.migration import protocol as P

logger = logging.getLogger(__name__)


class TombstoneRegistry:
    _FILE = "tombstones.json"

    def __init__(self, *, store_dir: Path | str) -> None:
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # -- paths / io -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._dir / self._FILE

    def _read_all(self) -> dict[str, dict[str, dict]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("tombstone registry unreadable at %s", self.path)
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Corrupt file: answer "nothing moved" rather than break every
            # caller that lists units. A later record() overwrites the garbage.
            logger.warning("tombstone registry is not valid JSON at %s", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict) -> None:
        # fsync: this is the only record of where the work went. A tombstone lost
        # to a power cut leaves a unit that is non-executing here for a reason
        # nobody can look up.
        atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True), fsync=True)

    # -- serialization --------------------------------------------------------

    @staticmethod
    def _to_json(ts: P.Tombstone) -> dict:
        return {
            "unit_kind": ts.unit_kind,
            "target_crew": {
                "crew_id": ts.target_crew.crew_id,
                "label": ts.target_crew.label,
            },
            "remote_unit_id": ts.remote_unit_id,
            "migrated_ts": ts.migrated_ts,
        }

    @staticmethod
    def _from_json(raw: dict) -> P.Tombstone | None:
        try:
            crew = raw["target_crew"]
            return P.Tombstone(
                unit_kind=raw["unit_kind"],
                target_crew=P.CrewRef(crew_id=crew["crew_id"], label=crew.get("label")),
                remote_unit_id=raw["remote_unit_id"],
                migrated_ts=raw["migrated_ts"],
            )
        except (KeyError, TypeError):
            # One malformed entry must not hide the rest.
            logger.warning("skipping malformed tombstone entry")
            return None

    # -- api ------------------------------------------------------------------

    def record(self, kind: str, unit_id: str, tombstone: P.Tombstone) -> None:
        """Persist where this unit went. Re-recording replaces: after A->B->C the
        tombstone names C, the current home, not the intermediate B."""
        data = self._read_all()
        data.setdefault(kind, {})[unit_id] = self._to_json(tombstone)
        self._write_all(data)

    def clear(self, kind: str, unit_id: str) -> None:
        """Drop a tombstone because the unit is live HERE again.

        Called when a unit is materialized locally — including the move-back case
        (Req 7.4). Leaving a stale tombstone would tell the user their running
        job had moved elsewhere. Clearing a unit that was never tombstoned is a
        no-op, because the caller does not know whether it was."""
        data = self._read_all()
        bucket = data.get(kind)
        if not bucket or unit_id not in bucket:
            return
        del bucket[unit_id]
        if not bucket:
            del data[kind]
        self._write_all(data)

    def lookup(self, kind: str, unit_id: str) -> P.Tombstone | None:
        """Where did this unit go? ``None`` means it did not move.

        A listing surface asks this about every row it renders, so the common
        answer has to be a value rather than an exception."""
        raw = self._read_all().get(kind, {}).get(unit_id)
        return self._from_json(raw) if isinstance(raw, dict) else None

    def list_for_kind(self, kind: str) -> dict[str, P.Tombstone]:
        """Every tombstone of one kind, for a surface that lists units in bulk
        and would otherwise do one lookup per row."""
        out: dict[str, P.Tombstone] = {}
        for unit_id, raw in (self._read_all().get(kind) or {}).items():
            if isinstance(raw, dict):
                ts = self._from_json(raw)
                if ts is not None:
                    out[unit_id] = ts
        return out
