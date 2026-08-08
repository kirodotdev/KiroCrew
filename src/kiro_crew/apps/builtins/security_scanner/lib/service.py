"""Scanner service — the coordination layer the cron/skill and backend share.

Responsibilities:

- **One scan at a time** (:class:`ScanLock`): a file lock with pid + timestamp.
  A fresh lock blocks a second scan; a stale lock (older than ``ttl_s``, e.g. a
  crashed run) is reclaimed.
- **Interrupted-run recovery**: on each entry, scans stuck ``running`` past a
  threshold are marked ``failed`` so they don't linger as phantom active runs.
- **Single locked entrypoint** (:meth:`run_scan_locked`): recover -> acquire ->
  run the topic scan (injected dispatcher) -> validate exploits is a later
  concern of the caller -> select new actionable findings -> notify (injected)
  -> persist notified-state -> release. Both the cron and the backend go
  through this one path so scheduled and on-demand scans behave identically.
- **Read/ingest helpers** for the backend routes (status, findings, knowledge,
  external-report ingestion).

The ``dispatcher`` (fan-out to topic agents via spawn_run) and ``notifier``
(deliver a message via send_message) are injected callables — the service has
no MCP dependency and is fully unit-testable.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .findings import FindingsStore
from .knowledge import KnowledgeStore
from .models import KnowledgePattern, utcnow_iso
from .reporter import format_notification, select_new_actionable
from .scan import Dispatcher, ScanResult, run_scan
from .storage import read_json, write_json

Notifier = Callable[[str], None]

DEFAULT_LOCK_TTL_S = 1800  # a scan lock older than this is considered crashed
DEFAULT_RUNNING_STALE_S = 3600  # a scan 'running' longer than this is recovered as failed


@dataclass
class ScanLock:
    path: Path
    ttl_s: float = DEFAULT_LOCK_TTL_S

    def _read(self) -> Optional[dict[str, Any]]:
        return read_json(self.path, None)

    def is_held(self) -> bool:
        data = self._read()
        if not data:
            return False
        started = float(data.get("epoch", 0))
        return (time.time() - started) < self.ttl_s

    def acquire(self, scan_id: str) -> bool:
        """Acquire the lock. Returns False if a fresh (non-stale) lock exists."""
        if self.is_held():
            return False
        write_json(self.path, {"pid": os.getpid(), "scan_id": scan_id, "epoch": time.time(), "at": utcnow_iso()})
        return True

    def release(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass


class ScannerService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.knowledge = KnowledgeStore(self.data_dir)
        self.findings = FindingsStore(self.data_dir)
        self.state_path = self.data_dir / "service_state.json"
        self.lock = ScanLock(self.data_dir / "scan.lock")

    # ---- state --------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        st = read_json(self.state_path, {})
        return st if isinstance(st, dict) else {}

    def _save_state(self, st: dict[str, Any]) -> None:
        write_json(self.state_path, st)

    # ---- recovery -----------------------------------------------------------

    def recover_interrupted(self, stale_s: float = DEFAULT_RUNNING_STALE_S) -> int:
        """Mark scans stuck in ``running`` past ``stale_s`` as ``failed``.
        Returns the number recovered. Also drops a stale lock."""
        recovered = 0
        for rec in self.findings.recent_scans(limit=50):
            if rec.status != "running":
                continue
            started = _parse_iso(rec.started_at)
            if started is None or (datetime.now(timezone.utc) - started).total_seconds() > stale_s:
                rec.status = "failed"
                rec.error = "recovered: scan was interrupted (stuck in running)"
                rec.finished_at = utcnow_iso()
                self.findings.save_scan(rec)
                recovered += 1
        if not self.lock.is_held():
            self.lock.release()  # clear any stale lock file
        return recovered

    # ---- the single locked scan entrypoint ---------------------------------

    def run_scan_locked(
        self,
        dispatcher: Dispatcher,
        notifier: Notifier,
        topic_ids: list[str] | None = None,
        mode: str = "full",
        target_desc: str = "the KiroCrew codebase",
    ) -> Optional[ScanResult]:
        """Run a scan under the lock. Returns None if a scan is already running.

        NOTE: exploit validation of the resulting findings is performed by the
        caller (the skill agent, which has pod access), then folded back via
        ``learn_feedback.apply_verdict``. This method owns scheduling, locking,
        recovery, and notification — the deterministic, testable coordination.
        """
        self.recover_interrupted()
        scan_id_probe = f"scan-{utcnow_iso().replace(':', '').replace('-', '')}"
        if not self.lock.acquire(scan_id_probe):
            return None
        try:
            result = run_scan(dispatcher, self.knowledge, self.findings, topic_ids, mode, target_desc)
            st = self._state()
            notified: set[str] = set(st.get("notified_ids", []))
            new = select_new_actionable(result.findings, notified)
            if new:
                notifier(format_notification(new, result.record.id))
                notified.update(f.id for f in new)
                st["notified_ids"] = sorted(notified)
            st["last_scan_id"] = result.record.id
            st["last_scan_at"] = utcnow_iso()
            self._save_state(st)
            return result
        finally:
            self.lock.release()

    # ---- backend read helpers ----------------------------------------------

    def status(self) -> dict[str, Any]:
        st = self._state()
        all_findings = self.findings.all()
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in all_findings:
            by_status[f.status] = by_status.get(f.status, 0) + 1
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        patterns = self.knowledge.all_patterns()
        fp_rates = [p.false_positive_rate for p in patterns]
        return {
            "running": self.lock.is_held(),
            "last_scan_id": st.get("last_scan_id", ""),
            "last_scan_at": st.get("last_scan_at", ""),
            "findings_total": len(all_findings),
            "findings_by_status": by_status,
            "findings_by_severity": by_severity,
            "patterns_total": len(patterns),
            "coverage": self.knowledge.coverage(),
            "avg_false_positive_rate": round(sum(fp_rates) / len(fp_rates), 3) if fp_rates else 0.0,
        }

    def list_findings(self, status: str | None = None, topic: str | None = None) -> list[dict[str, Any]]:
        out = []
        for f in self.findings.all():
            if status and f.status != status:
                continue
            if topic and f.topic != topic:
                continue
            out.append(f.to_dict())
        return out

    def get_finding(self, finding_id: str) -> Optional[dict[str, Any]]:
        f = self.findings.get(finding_id)
        return f.to_dict() if f else None

    def knowledge_overview(self) -> dict[str, Any]:
        return {
            "patterns": [p.to_dict() for p in self.knowledge.all_patterns()],
            "suppressions": [s.to_dict() for s in self.knowledge.all_suppressions()],
            "coverage": self.knowledge.coverage(),
            "activity": self.knowledge.activity_log(limit=30),
        }

    def recent_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.findings.recent_scans(limit=limit)]

    def ingest_report_text(self, text: str, topic_hint: str = "") -> dict[str, Any]:
        """Parse an external report into knowledge patterns and ingest them.

        Accepts a JSON array of pattern objects, or free text (one pattern per
        non-empty, non-heading line). Robust to messy input — unparseable lines
        are skipped. Returns {added, parsed}.
        """
        patterns = _parse_report(text, topic_hint)
        added = self.knowledge.ingest_patterns(patterns)
        return {"parsed": len(patterns), "added": added}


# ---- helpers ----------------------------------------------------------------


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_report(text: str, topic_hint: str) -> list[KnowledgePattern]:
    """Best-effort parse of an external report into patterns."""
    import json

    text = (text or "").strip()
    if not text:
        return []
    topic = topic_hint or "path-traversal"
    patterns: list[KnowledgePattern] = []

    # Try JSON array of {topic?, pattern, tags?, exploit_template?} first.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    patterns.append(
                        KnowledgePattern(
                            topic=str(item.get("topic") or topic),
                            pattern=str(item.get("pattern", "")),
                            tags=list(item.get("tags", [])) if isinstance(item.get("tags"), list) else [],
                            exploit_template=str(item.get("exploit_template", "")),
                            source="external-report",
                            confidence=float(item.get("confidence", 0.6)),
                        )
                    )
                except (ValueError, TypeError):
                    continue
            return patterns
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to one pattern per meaningful line.
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if len(line) < 12 or line.startswith("#"):
            continue
        try:
            patterns.append(KnowledgePattern(topic=topic, pattern=line, source="external-report", confidence=0.5))
        except ValueError:
            continue
    return patterns
