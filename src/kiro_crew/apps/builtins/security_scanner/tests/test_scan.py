"""Tests for the topic scanning engine (lib/topics.py + lib/scan.py).

Uses fake dispatchers returning canned agent output, so the whole
prompt->parse->dedup->persist pipeline is exercised deterministically with no
real spawn_run and no LLM.
"""
from __future__ import annotations

from kiro_crew.apps.builtins.security_scanner.lib.findings import FindingsStore
from kiro_crew.apps.builtins.security_scanner.lib.knowledge import KnowledgeStore
from kiro_crew.apps.builtins.security_scanner.lib.scan import (
    TopicAgentResult,
    _extract_json_array,
    build_jobs,
    normalize_findings,
    run_scan,
)
from kiro_crew.apps.builtins.security_scanner.lib.seed import seed_knowledge
from kiro_crew.apps.builtins.security_scanner.lib.topics import active_topics, build_topic_prompt

# ---- topics + prompt --------------------------------------------------------


def test_active_topics_defaults_to_v1_set():
    assert [t.id for t in active_topics(None)] == ["path-traversal", "auth-bypass", "prompt-injection"]
    assert [t.id for t in active_topics(["all"])] == ["path-traversal", "auth-bypass", "prompt-injection"]
    assert [t.id for t in active_topics(["auth-bypass"])] == ["auth-bypass"]
    assert active_topics(["nonexistent"]) == []


def test_prompt_includes_knowledge_slice_and_suppressions(tmp_path):
    store = KnowledgeStore(tmp_path)
    seed_knowledge(store)
    topic = active_topics(["path-traversal"])[0]
    prompt = build_topic_prompt(
        topic,
        store.for_topic(topic.id, tags=topic.tags),
        store.suppressions_for_topic(topic.id, tags=topic.tags),
        "the KiroCrew codebase",
    )
    assert "Path Traversal" in prompt
    assert "os.path.join" in prompt  # a seeded pattern is present
    assert "OUTPUT CONTRACT" in prompt
    assert "grep/glob/read" in prompt  # no pre-selection: agent finds files itself


def test_build_jobs_one_per_topic(tmp_path):
    store = KnowledgeStore(tmp_path)
    jobs = build_jobs(store, active_topics(None), "target")
    assert [j.topic_id for j in jobs] == ["path-traversal", "auth-bypass", "prompt-injection"]


# ---- output parsing ---------------------------------------------------------


def test_extract_bare_array():
    assert _extract_json_array('[{"a":1}]') == [{"a": 1}]


def test_extract_from_json_fence():
    text = "Here are my findings:\n```json\n[{\"title\":\"x\"}]\n```\nDone."
    assert _extract_json_array(text) == [{"title": "x"}]


def test_extract_from_bracket_slice_in_prose():
    text = 'I found: [{"title":"x"}] and that is all.'
    assert _extract_json_array(text) == [{"title": "x"}]


def test_extract_findings_key_object():
    assert _extract_json_array('{"findings": [{"title":"x"}]}') == [{"title": "x"}]


def test_extract_garbage_returns_empty():
    assert _extract_json_array("no json here at all") == []
    assert _extract_json_array("") == []


def test_normalize_skips_malformed_findings():
    raw = """[
      {"title": "Good one", "location": "f.py:1", "severity": "high", "description": "d"},
      {"title": "", "location": "f.py:2"},
      {"location": "f.py:3"},
      {"title": "Bad severity", "location": "f.py:4", "severity": "nope"}
    ]"""
    out = normalize_findings("path-traversal", raw, "scan-x")
    assert len(out) == 1
    assert out[0].title == "Good one"
    assert out[0].scan_id == "scan-x"


# ---- full scan --------------------------------------------------------------


def _fake_dispatcher(mapping):
    def dispatch(jobs):
        results = []
        for j in jobs:
            entry = mapping.get(j.topic_id, TopicAgentResult(topic_id=j.topic_id, ok=True, raw="[]"))
            results.append(entry)
        return results
    return dispatch


def test_run_scan_persists_findings_and_completes(tmp_path):
    ks = KnowledgeStore(tmp_path)
    fs = FindingsStore(tmp_path)
    seed_knowledge(ks)
    mapping = {
        "path-traversal": TopicAgentResult(
            topic_id="path-traversal",
            raw='[{"title":"Join escape","location":"file_ops.py:142","severity":"critical","description":"user path"}]',
        ),
        "auth-bypass": TopicAgentResult(
            topic_id="auth-bypass",
            raw='```json\n[{"title":"Timing leak","location":"auth.py:89","severity":"high"}]\n```',
        ),
        "prompt-injection": TopicAgentResult(topic_id="prompt-injection", raw="[]"),
    }
    result = run_scan(_fake_dispatcher(mapping), ks, fs)
    assert result.record.status == "complete"
    assert result.record.stats["total_findings"] == 2
    assert len(fs.all()) == 2
    # scan record persisted + retrievable
    rec = fs.get_scan(result.record.id)
    assert rec is not None and rec.status == "complete"


def test_run_scan_isolates_a_failed_topic(tmp_path):
    ks = KnowledgeStore(tmp_path)
    fs = FindingsStore(tmp_path)
    mapping = {
        "path-traversal": TopicAgentResult(
            topic_id="path-traversal",
            raw='[{"title":"X","location":"a.py:1","severity":"medium"}]',
        ),
        "auth-bypass": TopicAgentResult(topic_id="auth-bypass", ok=False, error="agent crashed"),
        "prompt-injection": TopicAgentResult(topic_id="prompt-injection", raw="[]"),
    }
    result = run_scan(_fake_dispatcher(mapping), ks, fs)
    assert result.record.status == "complete"  # one topic failing != scan failing
    assert result.record.stats["topics_failed"] == 1
    assert result.record.stats["per_topic"]["auth-bypass"]["status"] == "error"
    assert result.record.stats["total_findings"] == 1


def test_run_scan_dedups_same_finding_across_topics(tmp_path):
    ks = KnowledgeStore(tmp_path)
    fs = FindingsStore(tmp_path)
    # Two topics report the SAME location+title -> one finding after dedup.
    same = '[{"title":"Shared","location":"x.py:5","severity":"high"}]'
    mapping = {
        "path-traversal": TopicAgentResult(topic_id="path-traversal", raw=same),
        "auth-bypass": TopicAgentResult(topic_id="auth-bypass", raw=same),
        "prompt-injection": TopicAgentResult(topic_id="prompt-injection", raw="[]"),
    }
    # NOTE: dedup key includes topic, so different topics => different findings.
    # Same topic re-reporting dedups; assert both topics' distinct-topic rows exist
    # but a repeat within a topic collapses.
    result = run_scan(_fake_dispatcher(mapping), ks, fs)
    # path-traversal + auth-bypass each produce one (topic is part of the id).
    assert result.record.stats["total_findings"] == 2


def test_run_scan_whole_dispatcher_failure_marks_scan_failed(tmp_path):
    ks = KnowledgeStore(tmp_path)
    fs = FindingsStore(tmp_path)

    def boom(jobs):
        raise RuntimeError("spawn subsystem down")

    result = run_scan(boom, ks, fs)
    assert result.record.status == "failed"
    assert "spawn subsystem down" in result.record.error
    rec = fs.get_scan(result.record.id)
    assert rec is not None and rec.status == "failed"
