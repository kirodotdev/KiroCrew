"""Tests for the Security Scanner knowledge + findings core.

Run: python -m pytest security-scanner/tests -q
Stdlib + pytest only; every test uses a tmp_path data dir so nothing touches a
real install.
"""
from __future__ import annotations

import json

import pytest

from kiro_crew.apps.builtins.security_scanner.lib.findings import FindingsStore
from kiro_crew.apps.builtins.security_scanner.lib.knowledge import KnowledgeStore
from kiro_crew.apps.builtins.security_scanner.lib.models import (
    Finding,
    KnowledgePattern,
    ScanRecord,
    Suppression,
)
from kiro_crew.apps.builtins.security_scanner.lib.seed import seed_knowledge

# ---- models -----------------------------------------------------------------


def test_pattern_id_is_content_derived_and_stable():
    a = KnowledgePattern(topic="path-traversal", pattern="os.path.join escape")
    b = KnowledgePattern(topic="path-traversal", pattern="os.path.join escape")
    assert a.id == b.id
    c = KnowledgePattern(topic="auth-bypass", pattern="os.path.join escape")
    assert c.id != a.id


def test_pattern_normalizes_tags_and_clamps_confidence():
    p = KnowledgePattern(
        topic="t", pattern="p", tags=["FS", " Path ", "fs", "path"], confidence=5.0
    )
    assert p.tags == ["fs", "path"]  # lowercased, trimmed, de-duped, order kept
    assert p.confidence == 1.0  # clamped into [0, 1]


def test_pattern_rejects_unknown_source():
    with pytest.raises(ValueError):
        KnowledgePattern(topic="t", pattern="p", source="made-up")


def test_pattern_roundtrips_through_dict():
    p = KnowledgePattern(topic="t", pattern="p", tags=["a"], source="seed", confidence=0.7)
    p2 = KnowledgePattern.from_dict(p.to_dict())
    assert p2.to_dict() == p.to_dict()


def test_finding_rejects_bad_severity_and_status():
    with pytest.raises(ValueError):
        Finding(topic="t", title="x", location="f.py:1", severity="nope")
    with pytest.raises(ValueError):
        Finding(topic="t", title="x", location="f.py:1", status="nope")


def test_finding_id_dedup_key():
    a = Finding(topic="auth-bypass", title="Timing leak", location="auth.py:89")
    b = Finding(topic="auth-bypass", title="timing leak", location="auth.py:89")
    assert a.id == b.id  # title compared case-insensitively


# ---- knowledge store --------------------------------------------------------


def test_learn_is_idempotent_and_merges(tmp_path):
    store = KnowledgeStore(tmp_path)
    p = KnowledgePattern(topic="path-traversal", pattern="join escape", confidence=0.6)
    store.learn(p)
    store.learn(KnowledgePattern(topic="path-traversal", pattern="join escape", confidence=0.9))
    patterns = store.all_patterns()
    assert len(patterns) == 1
    assert patterns[0].instances == 2
    assert patterns[0].confidence == 0.9  # kept the higher confidence


def test_for_topic_returns_only_the_slice(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.learn(KnowledgePattern(topic="path-traversal", pattern="a", tags=["fs"]))
    store.learn(KnowledgePattern(topic="auth-bypass", pattern="b", tags=["auth"]))
    store.learn(KnowledgePattern(topic="prompt-injection", pattern="c", tags=["prompt"]))
    slice_ = store.for_topic("path-traversal")
    assert {p.topic for p in slice_} == {"path-traversal"}
    # Tag intersection also pulls a cross-topic pattern in.
    store.learn(KnowledgePattern(topic="other", pattern="d", tags=["fs"]))
    slice2 = store.for_topic("path-traversal", tags=["fs"])
    assert any(p.topic == "other" for p in slice2)


def test_suppress_is_idempotent(tmp_path):
    store = KnowledgeStore(tmp_path)
    s = Suppression(topic="auth-bypass", pattern="test fixture creds", reason="fixture")
    store.suppress(s)
    store.suppress(Suppression(topic="auth-bypass", pattern="test fixture creds", reason="fixture"))
    assert len(store.all_suppressions()) == 1


def test_remove_pattern_is_logged(tmp_path):
    store = KnowledgeStore(tmp_path)
    p = store.learn(KnowledgePattern(topic="t", pattern="p"))
    assert store.remove_pattern(p.id, "human cleanup") is True
    assert store.all_patterns() == []
    log = store.activity_log()
    assert any(e["action"] == "remove-pattern" for e in log)


def test_false_positive_rate_climbs(tmp_path):
    store = KnowledgeStore(tmp_path)
    p = store.learn(KnowledgePattern(topic="t", pattern="p"))
    store.record_false_positive(p.id)
    store.record_false_positive(p.id)
    got = store.all_patterns()[0]
    assert got.false_positive_rate > 0.0


def test_corrupt_knowledge_file_is_quarantined_not_fatal(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.knowledge_path.write_text("{ this is not valid json", encoding="utf-8")
    # Load must not raise; returns empty and quarantines the bad file.
    assert store.all_patterns() == []
    assert list(tmp_path.glob("knowledge.json.corrupt-*"))


def test_malformed_entry_skipped_not_fatal(tmp_path):
    store = KnowledgeStore(tmp_path)
    store.learn(KnowledgePattern(topic="t", pattern="good"))
    # Inject a malformed pattern (missing required fields) alongside the good one.
    doc = json.loads(store.knowledge_path.read_text())
    doc["patterns"].append({"id": "pat-bogus", "topic": "", "pattern": ""})
    store.knowledge_path.write_text(json.dumps(doc), encoding="utf-8")
    patterns = store.all_patterns()
    assert len(patterns) == 1  # bogus entry skipped
    assert patterns[0].pattern == "good"


def test_migration_coerces_legacy_shape(tmp_path):
    store = KnowledgeStore(tmp_path)
    # A legacy file with no version and a bare list-less shape.
    store.knowledge_path.write_text(json.dumps({"patterns": []}), encoding="utf-8")
    store.learn(KnowledgePattern(topic="t", pattern="p"))
    doc = json.loads(store.knowledge_path.read_text())
    assert doc["version"] >= 1


# ---- seeding ----------------------------------------------------------------


def test_seed_is_idempotent(tmp_path):
    store = KnowledgeStore(tmp_path)
    first = seed_knowledge(store)
    assert first > 0
    total_after_first = len(store.all_patterns())
    second = seed_knowledge(store)
    assert second == 0  # nothing new the second time
    assert len(store.all_patterns()) == total_after_first


def test_seed_covers_all_three_topics(tmp_path):
    store = KnowledgeStore(tmp_path)
    seed_knowledge(store)
    cov = store.coverage()
    assert cov.get("path-traversal", 0) >= 1
    assert cov.get("auth-bypass", 0) >= 1
    assert cov.get("prompt-injection", 0) >= 1


# ---- findings store ---------------------------------------------------------


def test_findings_dedup_on_upsert(tmp_path):
    store = FindingsStore(tmp_path)
    f = Finding(topic="auth-bypass", title="Timing leak", location="auth.py:89", severity="high")
    store.upsert(f)
    store.upsert(Finding(topic="auth-bypass", title="Timing leak", location="auth.py:89", severity="low"))
    all_ = store.all()
    assert len(all_) == 1
    assert all_[0].severity == "high"  # kept the more severe


def test_finding_status_advances_but_never_regresses(tmp_path):
    store = FindingsStore(tmp_path)
    f = Finding(topic="t", title="x", location="f.py:1", status="confirmed")
    store.upsert(f)
    store.set_status(f.id, "exploited", evidence="poc ok")
    # A later weaker sighting must not downgrade it.
    store.upsert(Finding(topic="t", title="x", location="f.py:1", status="pattern-learned"))
    got = store.get(f.id)
    assert got is not None
    assert got.status == "exploited"
    assert got.evidence == "poc ok"


def test_scan_history_roundtrip_and_ordering(tmp_path):
    store = FindingsStore(tmp_path)
    store.save_scan(ScanRecord(id="scan-1", started_at="2026-08-08T01:00:00Z", status="complete"))
    store.save_scan(ScanRecord(id="scan-2", started_at="2026-08-08T02:00:00Z", status="running"))
    recent = store.recent_scans()
    assert [r.id for r in recent] == ["scan-2", "scan-1"]  # newest first
    scan1 = store.get_scan("scan-1")
    assert scan1 is not None and scan1.status == "complete"


def test_corrupt_findings_file_is_not_fatal(tmp_path):
    store = FindingsStore(tmp_path)
    store.findings_path.write_text("not json at all", encoding="utf-8")
    assert store.all() == []
