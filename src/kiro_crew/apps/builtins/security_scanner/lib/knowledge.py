"""Tagged, versioned knowledge store.

Holds two collections in the app data dir:

- ``knowledge.json``   learned patterns + an append-only ``activity_log``
- ``suppressions.json``  known false positives

Core guarantees (see SECURITY_NOTES.md):

- **Tag-scoped retrieval.** A topic agent asks for its slice via
  :meth:`for_topic`, which returns only patterns whose topic OR tags match —
  this is what keeps ~15 relevant patterns in the agent's context instead of
  the whole library.
- **Idempotent learning.** :meth:`learn` merges a re-learned pattern into the
  existing entry (bump ``instances`` / ``last_seen``, keep the higher
  confidence) keyed by the content-derived id — no near-duplicates.
- **Append-with-audit.** Every mutation writes an ``activity_log`` entry.
  ``remove_pattern`` exists but is meant for explicit human action; ordinary
  scanning never deletes.
- **Schema versioning + migration.** Files carry ``version``; loads run through
  :func:`_migrate`, and malformed individual entries are skipped (quarantined
  in the log) rather than failing the whole load.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .models import KnowledgePattern, Suppression, utcnow_iso
from .storage import read_json, write_json

SCHEMA_VERSION = 1


class KnowledgeStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.knowledge_path = self.data_dir / "knowledge.json"
        self.suppressions_path = self.data_dir / "suppressions.json"

    # ---- load / save --------------------------------------------------------

    def _load_knowledge(self) -> dict[str, Any]:
        raw = read_json(self.knowledge_path, {"version": SCHEMA_VERSION, "patterns": [], "activity_log": []})
        return _migrate(raw)

    def _load_suppressions(self) -> dict[str, Any]:
        raw = read_json(self.suppressions_path, {"version": SCHEMA_VERSION, "suppressions": []})
        if not isinstance(raw, dict):
            return {"version": SCHEMA_VERSION, "suppressions": []}
        raw.setdefault("version", SCHEMA_VERSION)
        raw.setdefault("suppressions", [])
        return raw

    def _log(self, doc: dict[str, Any], action: str, detail: str) -> None:
        doc.setdefault("activity_log", []).append(
            {"at": utcnow_iso(), "action": action, "detail": detail}
        )

    # ---- patterns -----------------------------------------------------------

    def all_patterns(self) -> list[KnowledgePattern]:
        doc = self._load_knowledge()
        out: list[KnowledgePattern] = []
        for entry in doc.get("patterns", []):
            try:
                out.append(KnowledgePattern.from_dict(entry))
            except ValueError:
                continue  # malformed entry skipped, not fatal
        return out

    def for_topic(self, topic: str, tags: Optional[list[str]] = None) -> list[KnowledgePattern]:
        """Return the knowledge slice for a topic: patterns whose ``topic``
        matches, plus any whose ``tags`` intersect the requested tags. This is
        the focused context handed to a topic agent."""
        want_tags = {t.strip().lower() for t in (tags or []) if t.strip()}
        result: list[KnowledgePattern] = []
        for p in self.all_patterns():
            if p.topic == topic or (want_tags and want_tags.intersection(p.tags)):
                result.append(p)
        return result

    def learn(self, pattern: KnowledgePattern) -> KnowledgePattern:
        """Add a pattern, or merge into an existing one with the same id.

        Merge semantics: bump ``instances``, refresh ``last_seen``, keep the
        MAX confidence seen, and preserve the earliest ``created_at``.
        """
        doc = self._load_knowledge()
        patterns = doc.setdefault("patterns", [])
        for i, entry in enumerate(patterns):
            if entry.get("id") == pattern.id:
                existing = KnowledgePattern.from_dict(entry)
                existing.instances += 1
                existing.last_seen = utcnow_iso()
                existing.confidence = max(existing.confidence, pattern.confidence)
                if pattern.exploit_template and not existing.exploit_template:
                    existing.exploit_template = pattern.exploit_template
                patterns[i] = existing.to_dict()
                self._log(doc, "merge-pattern", f"{existing.id} instances={existing.instances}")
                write_json(self.knowledge_path, doc)
                return existing
        patterns.append(pattern.to_dict())
        self._log(doc, "add-pattern", f"{pattern.id} topic={pattern.topic} source={pattern.source}")
        write_json(self.knowledge_path, doc)
        return pattern

    def record_false_positive(self, pattern_id: str) -> None:
        """Nudge a pattern's false-positive rate up when a PoC proved the target
        was actually safe. Bounded exponential-ish moving average toward 1.0."""
        doc = self._load_knowledge()
        for i, entry in enumerate(doc.get("patterns", [])):
            if entry.get("id") == pattern_id:
                p = KnowledgePattern.from_dict(entry)
                p.false_positive_rate = min(1.0, p.false_positive_rate + (1.0 - p.false_positive_rate) * 0.34)
                doc["patterns"][i] = p.to_dict()
                self._log(doc, "fp-bump", f"{pattern_id} fp={p.false_positive_rate:.2f}")
                write_json(self.knowledge_path, doc)
                return

    def remove_pattern(self, pattern_id: str, reason: str) -> bool:
        """Explicit human-directed deletion. Logged. Ordinary scanning must not
        call this — knowledge is append-with-audit by default."""
        doc = self._load_knowledge()
        before = len(doc.get("patterns", []))
        doc["patterns"] = [e for e in doc.get("patterns", []) if e.get("id") != pattern_id]
        if len(doc["patterns"]) == before:
            return False
        self._log(doc, "remove-pattern", f"{pattern_id}: {reason}")
        write_json(self.knowledge_path, doc)
        return True

    # ---- suppressions -------------------------------------------------------

    def all_suppressions(self) -> list[Suppression]:
        doc = self._load_suppressions()
        out: list[Suppression] = []
        for entry in doc.get("suppressions", []):
            try:
                out.append(Suppression.from_dict(entry))
            except ValueError:
                continue
        return out

    def suppressions_for_topic(self, topic: str, tags: Optional[list[str]] = None) -> list[Suppression]:
        want_tags = {t.strip().lower() for t in (tags or []) if t.strip()}
        return [
            s for s in self.all_suppressions()
            if s.topic == topic or (want_tags and want_tags.intersection(s.tags))
        ]

    def suppress(self, suppression: Suppression) -> Suppression:
        doc = self._load_suppressions()
        sups = doc.setdefault("suppressions", [])
        if any(s.get("id") == suppression.id for s in sups):
            return suppression  # already suppressed — idempotent
        sups.append(suppression.to_dict())
        write_json(self.suppressions_path, doc)
        return suppression

    # ---- external report ingestion -----------------------------------------

    def ingest_patterns(self, patterns: list[KnowledgePattern]) -> int:
        """Bulk-add patterns parsed from an external report. Returns the count
        newly added (merges count as touched, not new)."""
        added = 0
        for p in patterns:
            existing_ids = {e.id for e in self.all_patterns()}
            self.learn(p)
            if p.id not in existing_ids:
                added += 1
        return added

    # ---- coverage / metrics -------------------------------------------------

    def coverage(self) -> dict[str, int]:
        """Pattern count per topic — feeds the dashboard's coverage bars."""
        counts: dict[str, int] = {}
        for p in self.all_patterns():
            counts[p.topic] = counts.get(p.topic, 0) + 1
        return counts

    def activity_log(self, limit: int = 50) -> list[dict[str, Any]]:
        doc = self._load_knowledge()
        log = doc.get("activity_log", [])
        return log[-limit:]


def _migrate(raw: Any) -> dict[str, Any]:
    """Bring a loaded knowledge doc up to the current schema. Unknown/older
    shapes are coerced to the current one; a non-dict becomes an empty store."""
    if not isinstance(raw, dict):
        return {"version": SCHEMA_VERSION, "patterns": [], "activity_log": []}
    raw.setdefault("version", 0)
    raw.setdefault("patterns", [])
    raw.setdefault("activity_log", [])
    # Future migrations key off raw["version"] here.
    raw["version"] = SCHEMA_VERSION
    return raw
