"""Findings store: deduplicated findings + per-scan history.

- ``findings.json``            all findings, keyed by content-derived id
- ``scan_history/<id>.json``   one record per scan run

Dedup is by :meth:`Finding.derive_id` (topic + location + title). When the same
issue is seen again — by a second topic agent in the same scan, or in a later
scan — :meth:`upsert` updates the existing finding (status can advance
pattern-learned -> confirmed -> exploited; ``updated_at`` and ``scan_id``
refresh) instead of creating a duplicate row. Status never silently regresses:
an already-``exploited`` finding is not downgraded by a later weaker sighting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .models import FINDING_STATUSES, Finding, ScanRecord, utcnow_iso
from .storage import read_json, write_json

# Higher index = further along the lifecycle. Used to prevent status regression.
_STATUS_RANK = {name: i for i, name in enumerate(FINDING_STATUSES)}


class FindingsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.findings_path = self.data_dir / "findings.json"
        self.history_dir = self.data_dir / "scan_history"

    # ---- findings -----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.findings_path, {"findings": []})
        if not isinstance(raw, dict):
            return {"findings": []}
        raw.setdefault("findings", [])
        return raw

    def all(self) -> list[Finding]:
        out: list[Finding] = []
        for entry in self._load().get("findings", []):
            try:
                out.append(Finding.from_dict(entry))
            except ValueError:
                continue
        return out

    def get(self, finding_id: str) -> Optional[Finding]:
        for f in self.all():
            if f.id == finding_id:
                return f
        return None

    def upsert(self, finding: Finding) -> Finding:
        """Insert a new finding or update an existing one with the same id.

        On update: refresh ``updated_at`` / ``scan_id`` and let status advance,
        but never regress (an ``exploited`` finding stays exploited even if a
        later scan only re-detects it statically). Evidence, description, and
        exploit suggestion are filled in when the new sighting has richer data.
        """
        doc = self._load()
        findings = doc.setdefault("findings", [])
        for i, entry in enumerate(findings):
            if entry.get("id") == finding.id:
                existing = Finding.from_dict(entry)
                existing.updated_at = utcnow_iso()
                if finding.scan_id:
                    existing.scan_id = finding.scan_id
                # Advance status only forward.
                if _STATUS_RANK.get(finding.status, 0) >= _STATUS_RANK.get(existing.status, 0):
                    existing.status = finding.status
                if finding.evidence:
                    existing.evidence = finding.evidence
                if finding.description and len(finding.description) > len(existing.description):
                    existing.description = finding.description
                if finding.exploit_suggestion and not existing.exploit_suggestion:
                    existing.exploit_suggestion = finding.exploit_suggestion
                # Severity: keep the more severe of the two.
                if _severity_rank(finding.severity) > _severity_rank(existing.severity):
                    existing.severity = finding.severity
                findings[i] = existing.to_dict()
                write_json(self.findings_path, doc)
                return existing
        findings.append(finding.to_dict())
        write_json(self.findings_path, doc)
        return finding

    def upsert_many(self, findings: list[Finding]) -> list[Finding]:
        return [self.upsert(f) for f in findings]

    def set_status(self, finding_id: str, status: str, evidence: str = "") -> Optional[Finding]:
        if status not in FINDING_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        f = self.get(finding_id)
        if not f:
            return None
        f.status = status
        f.updated_at = utcnow_iso()
        if evidence:
            f.evidence = evidence
        return self.upsert(f)

    # ---- scan history -------------------------------------------------------

    def save_scan(self, record: ScanRecord) -> None:
        write_json(self.history_dir / f"{record.id}.json", record.to_dict())

    def get_scan(self, scan_id: str) -> Optional[ScanRecord]:
        raw = read_json(self.history_dir / f"{scan_id}.json", None)
        if raw is None:
            return None
        try:
            return ScanRecord.from_dict(raw)
        except ValueError:
            return None

    def recent_scans(self, limit: int = 20) -> list[ScanRecord]:
        if not self.history_dir.exists():
            return []
        records: list[ScanRecord] = []
        for path in self.history_dir.glob("*.json"):
            raw = read_json(path, None)
            if raw is None:
                continue
            try:
                records.append(ScanRecord.from_dict(raw))
            except ValueError:
                continue
        records.sort(key=lambda r: r.started_at, reverse=True)
        return records[:limit]


def _severity_rank(sev: str) -> int:
    order = ["info", "low", "medium", "high", "critical"]
    try:
        return order.index(sev)
    except ValueError:
        return 0
