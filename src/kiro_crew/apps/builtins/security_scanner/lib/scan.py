"""Topic-scan orchestration.

Flow: build one :class:`TopicScanJob` per active topic (topic prompt + its
tagged knowledge slice), fan them out through an injected **dispatcher**,
normalize each agent's JSON output into deduplicated :class:`Finding` objects,
and persist a :class:`ScanRecord`.

**Why an injected dispatcher.** The real fan-out is ``spawn_run`` — an MCP tool
only present in the live agent runtime. Hard-calling it would make this module
impossible to unit-test. Instead the engine depends on a plain callable:

    Dispatcher = Callable[[list[TopicScanJob]], list[TopicAgentResult]]

The skill/agent layer supplies a real dispatcher that spawns the topic agents in
parallel; tests supply a fake that returns canned agent output. The engine's job
— prompt assembly, robust JSON parsing, dedup, persistence, per-topic failure
isolation — is identical either way and fully covered by tests.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .findings import FindingsStore
from .knowledge import KnowledgeStore
from .models import Finding, ScanRecord, utcnow_iso
from .topics import SecurityTopic, active_topics, build_topic_prompt


@dataclass
class TopicScanJob:
    topic_id: str
    prompt: str
    topic: SecurityTopic


@dataclass
class TopicAgentResult:
    """One topic agent's outcome. ``raw`` is the agent's text output (expected
    to contain the JSON findings array); ``ok=False`` + ``error`` means the
    agent failed and its findings are skipped without failing the whole scan."""

    topic_id: str
    ok: bool = True
    raw: str = ""
    error: str = ""


Dispatcher = Callable[[list[TopicScanJob]], list[TopicAgentResult]]


def build_jobs(
    knowledge: KnowledgeStore,
    topics: list[SecurityTopic],
    target_desc: str,
) -> list[TopicScanJob]:
    jobs: list[TopicScanJob] = []
    for topic in topics:
        slice_ = knowledge.for_topic(topic.id, tags=topic.tags)
        sups = knowledge.suppressions_for_topic(topic.id, tags=topic.tags)
        prompt = build_topic_prompt(topic, slice_, sups, target_desc)
        jobs.append(TopicScanJob(topic_id=topic.id, prompt=prompt, topic=topic))
    return jobs


# ---- robust output parsing --------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_array(text: str) -> list[Any]:
    """Pull a JSON array of findings out of an agent's text output.

    Agents wrap JSON in prose or ``` fences unpredictably, so try, in order:
    (1) the whole string, (2) each fenced block, (3) the first '[' .. last ']'
    slice. Returns ``[]`` when nothing parses — a malformed agent reply yields
    no findings rather than raising.
    """
    text = (text or "").strip()
    if not text:
        return []

    def _as_list(obj: Any) -> list[Any] | None:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            return obj["findings"]
        return None

    try:
        got = _as_list(json.loads(text))
        if got is not None:
            return got
    except (json.JSONDecodeError, ValueError):
        pass

    for block in _FENCE_RE.findall(text):
        try:
            got = _as_list(json.loads(block.strip()))
            if got is not None:
                return got
        except (json.JSONDecodeError, ValueError):
            continue

    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            got = _as_list(json.loads(text[start : end + 1]))
            if got is not None:
                return got
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def normalize_findings(topic_id: str, raw: str, scan_id: str) -> list[Finding]:
    """Turn one agent's raw output into validated Finding objects. Entries that
    fail validation (missing title/location, bad severity) are skipped."""
    findings: list[Finding] = []
    for item in _extract_json_array(raw):
        if not isinstance(item, dict):
            continue
        try:
            findings.append(
                Finding(
                    topic=topic_id,
                    title=str(item.get("title", "")).strip(),
                    location=str(item.get("location", "")).strip(),
                    severity=str(item.get("severity", "medium")).strip().lower() or "medium",
                    description=str(item.get("description", "")),
                    exploit_suggestion=str(item.get("exploit_suggestion", "")),
                    status="pattern-learned",
                    scan_id=scan_id,
                )
            )
        except ValueError:
            continue  # malformed finding skipped, not fatal
    return findings


# ---- orchestration ----------------------------------------------------------


@dataclass
class ScanResult:
    record: ScanRecord
    findings: list[Finding] = field(default_factory=list)


def run_scan(
    dispatcher: Dispatcher,
    knowledge: KnowledgeStore,
    findings_store: FindingsStore,
    topic_ids: list[str] | None = None,
    mode: str = "full",
    target_desc: str = "the KiroCrew codebase",
) -> ScanResult:
    """Run one scan end-to-end.

    A single topic agent failing is isolated: its error is recorded in the scan
    stats and the scan continues with the other topics. The record is persisted
    as ``running`` up front and ``complete`` at the end so an interrupted scan
    is visible as stuck-in-running rather than lost.
    """
    topics = active_topics(topic_ids)
    scan_id = f"scan-{utcnow_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:6]}"
    record = ScanRecord(
        id=scan_id,
        topics=[t.id for t in topics],
        mode=mode,
        status="running",
    )
    findings_store.save_scan(record)

    jobs = build_jobs(knowledge, topics, target_desc)

    results: list[TopicAgentResult]
    try:
        results = dispatcher(jobs)
    except Exception as exc:  # dispatcher itself blew up — whole scan fails
        record.status = "failed"
        record.finished_at = utcnow_iso()
        record.error = f"dispatcher error: {exc}"
        findings_store.save_scan(record)
        return ScanResult(record=record, findings=[])

    by_topic = {r.topic_id: r for r in results}
    per_topic_stats: dict[str, Any] = {}
    all_findings: list[Finding] = []

    for job in jobs:
        res = by_topic.get(job.topic_id)
        if res is None:
            per_topic_stats[job.topic_id] = {"status": "missing", "findings": 0}
            continue
        if not res.ok:
            per_topic_stats[job.topic_id] = {"status": "error", "error": res.error, "findings": 0}
            continue
        topic_findings = normalize_findings(job.topic_id, res.raw, scan_id)
        persisted = findings_store.upsert_many(topic_findings)
        all_findings.extend(persisted)
        per_topic_stats[job.topic_id] = {"status": "ok", "findings": len(persisted)}

    record.finding_ids = sorted({f.id for f in all_findings})
    record.stats = {
        "per_topic": per_topic_stats,
        "total_findings": len(record.finding_ids),
        "topics_ok": sum(1 for s in per_topic_stats.values() if s.get("status") == "ok"),
        "topics_failed": sum(1 for s in per_topic_stats.values() if s.get("status") in ("error", "missing")),
    }
    record.status = "complete"
    record.finished_at = utcnow_iso()
    findings_store.save_scan(record)
    return ScanResult(record=record, findings=all_findings)


def data_dir_from_env(default: str = "~/.kiro/crew/workspace/security-scanner") -> Path:
    """Resolve the app's runtime data dir. Kept here so the skill/backend layers
    agree on one location."""
    import os

    root = os.environ.get("SECURITY_SCANNER_DATA") or default
    return Path(os.path.expanduser(root))
