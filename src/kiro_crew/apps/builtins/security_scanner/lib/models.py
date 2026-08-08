"""Data models for the Security Scanner knowledge + findings core.

Plain dataclasses with explicit ``to_dict`` / ``from_dict`` so persistence is a
stable JSON contract (not pickle, not a schema library) — the store files are
human-readable and forward-compatible. Every model validates on ``from_dict``:
a malformed entry raises ``ValueError`` so the store can quarantine it instead
of loading garbage.

Severity and finding-status are constrained to known vocabularies; unknown
values are rejected at the boundary rather than silently flowing through.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---- vocabularies -----------------------------------------------------------

SEVERITIES = ("critical", "high", "medium", "low", "info")

# Lifecycle of a finding, matching the design doc's finding states.
FINDING_STATUSES = (
    "pattern-learned",  # static match only
    "confirmed",        # validated by analysis, exploit not yet run
    "exploited",        # PoC succeeded against the sandbox
    "blocked",          # PoC ran, target was safe (candidate false positive)
    "suppressed",       # confirmed false positive, folded into suppressions
)

KNOWLEDGE_SOURCES = ("seed", "self-discovered", "external-report")

# The three v1 topics. Kept here so both the store and the scan engine agree.
V1_TOPICS = ("path-traversal", "auth-bypass", "prompt-injection")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_tags(tags: Any) -> list[str]:
    """Normalize a tag list: strings only, lowercased, trimmed, de-duplicated,
    order preserved. Non-list or non-string members are rejected."""
    if not isinstance(tags, list):
        raise ValueError(f"tags must be a list, got {type(tags).__name__}")
    out: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            raise ValueError(f"tag must be a string, got {type(t).__name__}")
        norm = t.strip().lower()
        if norm and norm not in out:
            out.append(norm)
    return out


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _clamp_confidence(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError("confidence must be a number") from None
    return max(0.0, min(1.0, f))


# ---- knowledge pattern ------------------------------------------------------


@dataclass
class KnowledgePattern:
    """A learned, generalized security pattern the scanner looks for.

    ``id`` is content-derived (topic + pattern) so re-learning the same pattern
    is idempotent — the store merges into the existing entry (bumping
    ``instances`` / ``last_seen``) instead of creating a near-duplicate.
    """

    topic: str
    pattern: str
    tags: list[str] = field(default_factory=list)
    exploit_template: str = ""
    confidence: float = 0.5
    source: str = "self-discovered"
    instances: int = 1
    false_positive_rate: float = 0.0
    created_at: str = field(default_factory=utcnow_iso)
    last_seen: str = field(default_factory=utcnow_iso)
    id: str = ""

    def __post_init__(self) -> None:
        self.topic = _require_str(self.topic, "topic")
        self.pattern = _require_str(self.pattern, "pattern")
        self.tags = _clean_tags(self.tags)
        self.confidence = _clamp_confidence(self.confidence)
        self.false_positive_rate = _clamp_confidence(self.false_positive_rate)
        if self.source not in KNOWLEDGE_SOURCES:
            raise ValueError(f"unknown knowledge source: {self.source!r}")
        if not isinstance(self.instances, int) or self.instances < 1:
            raise ValueError("instances must be a positive int")
        if not self.id:
            self.id = self.derive_id(self.topic, self.pattern)

    @staticmethod
    def derive_id(topic: str, pattern: str) -> str:
        digest = hashlib.sha256(f"{topic}\n{pattern.strip()}".encode()).hexdigest()
        return f"pat-{digest[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "pattern": self.pattern,
            "tags": self.tags,
            "exploit_template": self.exploit_template,
            "confidence": self.confidence,
            "source": self.source,
            "instances": self.instances,
            "false_positive_rate": self.false_positive_rate,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgePattern":
        if not isinstance(d, dict):
            raise ValueError("knowledge pattern must be an object")
        return cls(
            topic=d.get("topic", ""),
            pattern=d.get("pattern", ""),
            tags=d.get("tags", []),
            exploit_template=str(d.get("exploit_template", "")),
            confidence=d.get("confidence", 0.5),
            source=d.get("source", "self-discovered"),
            instances=int(d.get("instances", 1)),
            false_positive_rate=d.get("false_positive_rate", 0.0),
            created_at=str(d.get("created_at") or utcnow_iso()),
            last_seen=str(d.get("last_seen") or utcnow_iso()),
            id=str(d.get("id", "")),
        )


# ---- suppression ------------------------------------------------------------


@dataclass
class Suppression:
    """A known false positive to skip. ``id`` is content-derived so the same
    suppression is not stored twice."""

    topic: str
    pattern: str
    reason: str
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)
    id: str = ""

    def __post_init__(self) -> None:
        self.topic = _require_str(self.topic, "topic")
        self.pattern = _require_str(self.pattern, "pattern")
        self.reason = _require_str(self.reason, "reason")
        self.tags = _clean_tags(self.tags)
        if not self.id:
            digest = hashlib.sha256(f"{self.topic}\n{self.pattern.strip()}".encode()).hexdigest()
            self.id = f"sup-{digest[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "pattern": self.pattern,
            "reason": self.reason,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Suppression":
        if not isinstance(d, dict):
            raise ValueError("suppression must be an object")
        return cls(
            topic=d.get("topic", ""),
            pattern=d.get("pattern", ""),
            reason=d.get("reason", ""),
            tags=d.get("tags", []),
            created_at=str(d.get("created_at") or utcnow_iso()),
            id=str(d.get("id", "")),
        )


# ---- finding ----------------------------------------------------------------


@dataclass
class Finding:
    """One discovered issue. ``id`` is derived from (topic, location, title) so
    the same issue found by two topic agents, or across re-scans, collapses to a
    single finding rather than piling up duplicates."""

    topic: str
    title: str
    location: str  # "path/to/file.py:142"
    severity: str = "medium"
    description: str = ""
    exploit_suggestion: str = ""
    status: str = "pattern-learned"
    evidence: str = ""
    scan_id: str = ""
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    id: str = ""

    def __post_init__(self) -> None:
        self.topic = _require_str(self.topic, "topic")
        self.title = _require_str(self.title, "title")
        self.location = _require_str(self.location, "location")
        sev = str(self.severity).strip().lower()
        if sev not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")
        self.severity = sev
        if self.status not in FINDING_STATUSES:
            raise ValueError(f"unknown finding status: {self.status!r}")
        if not self.id:
            self.id = self.derive_id(self.topic, self.location, self.title)

    @staticmethod
    def derive_id(topic: str, location: str, title: str) -> str:
        digest = hashlib.sha256(
            f"{topic}\n{location.strip()}\n{title.strip().lower()}".encode()
        ).hexdigest()
        return f"find-{digest[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "title": self.title,
            "location": self.location,
            "severity": self.severity,
            "description": self.description,
            "exploit_suggestion": self.exploit_suggestion,
            "status": self.status,
            "evidence": self.evidence,
            "scan_id": self.scan_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        if not isinstance(d, dict):
            raise ValueError("finding must be an object")
        return cls(
            topic=d.get("topic", ""),
            title=d.get("title", ""),
            location=d.get("location", ""),
            severity=d.get("severity", "medium"),
            description=str(d.get("description", "")),
            exploit_suggestion=str(d.get("exploit_suggestion", "")),
            status=d.get("status", "pattern-learned"),
            evidence=str(d.get("evidence", "")),
            scan_id=str(d.get("scan_id", "")),
            created_at=str(d.get("created_at") or utcnow_iso()),
            updated_at=str(d.get("updated_at") or utcnow_iso()),
            id=str(d.get("id", "")),
        )


# ---- scan record ------------------------------------------------------------


@dataclass
class ScanRecord:
    """Metadata for one scan run. Persisted to ``scan_history/<id>.json``."""

    id: str
    topics: list[str] = field(default_factory=list)
    mode: str = "full"
    status: str = "running"  # running | complete | failed
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = ""
    finding_ids: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        self.id = _require_str(self.id, "id")
        if self.status not in ("running", "complete", "failed"):
            raise ValueError(f"unknown scan status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topics": self.topics,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "finding_ids": self.finding_ids,
            "stats": self.stats,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScanRecord":
        if not isinstance(d, dict):
            raise ValueError("scan record must be an object")
        return cls(
            id=d.get("id", ""),
            topics=list(d.get("topics", [])),
            mode=str(d.get("mode", "full")),
            status=d.get("status", "running"),
            started_at=str(d.get("started_at") or utcnow_iso()),
            finished_at=str(d.get("finished_at", "")),
            finding_ids=list(d.get("finding_ids", [])),
            stats=dict(d.get("stats", {})),
            error=str(d.get("error", "")),
        )
