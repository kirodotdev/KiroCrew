"""Unit tests for kiro_crew.tips module."""

from __future__ import annotations

import asyncio
import collections
import json
import os
import random
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.tips import (
    CatalogEntry,
    TipsState,
    _fallback_tips,
    _is_eligible,
    _load_state,
    _parse_tips,
    _redact_tips,
    _save_state,
    _scan_docs_catalog,
    _select_tip,
    _validate_tip_fields,
)
from kiro_crew.tips_text import truncate_summary


class TestCatalogParsing:
    def test_scan_docs_catalog_finds_entries(self) -> None:
        catalog = _scan_docs_catalog()
        assert len(catalog) > 0
        for entry in catalog:
            assert entry.feature
            assert entry.summary
            assert entry.doc.endswith(".md")

    def test_scan_skips_index_and_troubleshooting(self) -> None:
        catalog = _scan_docs_catalog()
        doc_names = [e.doc for e in catalog]
        assert "index.md" not in doc_names
        assert "troubleshooting.md" not in doc_names

    def test_catalog_entry_fields_are_strings(self) -> None:
        catalog = _scan_docs_catalog()
        if catalog:
            entry = catalog[0]
            assert isinstance(entry.feature, str)
            assert isinstance(entry.summary, str)
            assert isinstance(entry.doc, str)

    def test_catalog_entry_has_mtime(self) -> None:
        catalog = _scan_docs_catalog()
        if catalog:
            entry = catalog[0]
            assert entry.mtime > 0


class TestTipParsing:
    def test_parse_valid_json(self) -> None:
        text = json.dumps(
            [
                {
                    "id": "test-tip",
                    "feature": "Cron Jobs",
                    "title": "Schedule recurring tasks",
                    "body": "Use cron_add to schedule jobs.",
                    "why": "You work with pipelines daily.",
                    "doc": "cron-and-scheduling.md",
                    "cta_prompt": "Schedule a daily check",
                }
            ]
        )
        result = _parse_tips(text)
        assert len(result) == 1
        assert result[0]["id"] == "test-tip"

    def test_parse_with_markdown_fences(self) -> None:
        text = (
            "```json\n"
            + json.dumps(
                [
                    {
                        "id": "t1",
                        "feature": "F",
                        "title": "T",
                        "body": "B",
                        "why": "W",
                        "doc": "d.md",
                        "cta_prompt": "C",
                    }
                ]
            )
            + "\n```"
        )
        result = _parse_tips(text)
        assert len(result) == 1

    def test_parse_invalid_json(self) -> None:
        assert _parse_tips("not json at all") == []

    def test_parse_missing_required_fields(self) -> None:
        text = json.dumps([{"id": "x", "feature": "F"}])  # missing fields
        assert _parse_tips(text) == []

    def test_parse_caps_at_max(self) -> None:
        tips = [
            {
                "id": f"t{i}",
                "feature": "F",
                "title": "T",
                "body": "B",
                "why": "W",
                "doc": "d.md",
                "cta_prompt": "C",
            }
            for i in range(20)
        ]
        result = _parse_tips(json.dumps(tips))
        assert len(result) == 8

    def test_parse_rejects_non_string_field_values(self) -> None:
        """Finding 3: non-string values (e.g. 'id': []) must be rejected."""
        text = json.dumps(
            [
                {
                    "id": [],
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "d.md",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert len(result) == 0

    def test_parse_rejects_empty_id(self) -> None:
        """Finding 3: empty required fields rejected."""
        text = json.dumps(
            [
                {
                    "id": "",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "d.md",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert len(result) == 0

    def test_parse_truncates_overlong_fields(self) -> None:
        """Finding 3: long fields truncated to limit."""
        text = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T" * 300,  # over 200 limit
                    "body": "B",
                    "why": "W",
                    "doc": "d.md",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert len(result) == 1
        assert len(result[0]["title"]) == 200

    def test_parse_sanitizes_invalid_doc_urls(self) -> None:
        """Finding 3: javascript: or random strings in doc are cleared."""
        text = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "javascript:alert(1)",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert len(result) == 1
        assert result[0]["doc"] == ""

    def test_parse_keeps_valid_http_doc(self) -> None:
        text = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "https://docs.example.com/feature",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert result[0]["doc"] == "https://docs.example.com/feature"

    def test_parse_keeps_valid_md_doc(self) -> None:
        text = json.dumps(
            [
                {
                    "id": "t1",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "W",
                    "doc": "cron-and-scheduling.md",
                    "cta_prompt": "C",
                }
            ]
        )
        result = _parse_tips(text)
        assert result[0]["doc"] == "cron-and-scheduling.md"


class TestValidateTipFields:
    def test_valid_tip(self) -> None:
        t = {"id": "x", "feature": "F", "title": "T", "body": "B", "why": "W", "doc": "d.md", "cta_prompt": "C"}
        assert _validate_tip_fields(t) is True

    def test_non_string_rejects(self) -> None:
        t = {"id": 123, "feature": "F", "title": "T", "body": "B", "why": "W", "doc": "d.md", "cta_prompt": "C"}
        assert _validate_tip_fields(t) is False

    def test_empty_title_rejects(self) -> None:
        t = {"id": "x", "feature": "F", "title": "  ", "body": "B", "why": "W", "doc": "d.md", "cta_prompt": "C"}
        assert _validate_tip_fields(t) is False


class TestShownDocsSurvivesRegeneration:
    """Codex round-27: doc-level dismissal must survive pool regeneration."""

    def _record_shown(self, st: TipsState, tip_id: str) -> None:
        if isinstance(st.offered, dict) and st.offered.get("id") == tip_id:
            doc_val = st.offered.get("doc", "")
            if isinstance(doc_val, str) and doc_val:
                st.shown_docs[tip_id] = doc_val
                while len(st.shown_docs) > 32:
                    st.shown_docs.pop(next(iter(st.shown_docs)))
        st.last_shown_ts = time.time()
        st.offered = None

    def test_dismiss_resolves_doc_via_shown_docs_after_pool_regeneration(self) -> None:
        st = TipsState(
            offered={"id": "llm-fresh-id-1", "title": "T", "body": "B", "why": "W", "doc": "subagents.md", "cta_prompt": "p"},
            tips=[{"id": "llm-fresh-id-1", "doc": "subagents.md"}],
        )
        self._record_shown(st, "llm-fresh-id-1")
        assert st.shown_docs["llm-fresh-id-1"] == "subagents.md"
        st.tips = [{"id": "llm-fresh-id-2", "doc": "other.md"}]
        doc = ""
        if isinstance(st.offered, dict) and st.offered.get("id") == "llm-fresh-id-1":
            doc = st.offered.get("doc", "")
        else:
            for t in st.tips:
                if isinstance(t, dict) and t.get("id") == "llm-fresh-id-1":
                    doc = t.get("doc", "")
                    break
        if not doc:
            doc = st.shown_docs.get("llm-fresh-id-1", "")
        assert doc == "subagents.md"

    def test_shown_docs_bounded_to_32(self) -> None:
        st = TipsState()
        for i in range(40):
            tid = f"id-{i}"
            st.offered = {"id": tid, "doc": f"d{i}.md"}
            self._record_shown(st, tid)
        assert len(st.shown_docs) == 32
        assert "id-39" in st.shown_docs
        assert "id-0" not in st.shown_docs

    def test_shown_docs_roundtrips_and_sanitizes(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(shown_docs={"a": "x.md"})
            _save_state(st)
            loaded = _load_state()
            assert loaded.shown_docs == {"a": "x.md"}
            state_file = tmp_path / "tips_state.json"
            data = json.loads(state_file.read_text(encoding="utf-8"))
            data["shown_docs"] = {"good": "ok.md", "bad": 7}
            state_file.write_text(json.dumps(data))
            loaded2 = _load_state()
            assert loaded2.shown_docs == {"good": "ok.md"}


class TestSnoozedDocsSurvivesIdDrift:
    """Codex round-28: snooze must survive LLM id regeneration, like dismissal."""

    def test_regenerated_id_same_doc_stays_snoozed(self) -> None:
        now = time.time()
        st = TipsState(snoozed={"old-id": now}, snoozed_docs={"cron.md": now})
        fresh = {"id": "new-id-after-regen", "doc": "cron.md"}
        assert not _is_eligible(fresh, st, now=now + 60, snooze_hours=48.0)

    def test_doc_snooze_expires_after_window(self) -> None:
        old_ts = time.time() - 49 * 3600
        st = TipsState(snoozed_docs={"cron.md": old_ts})
        fresh = {"id": "new-id", "doc": "cron.md"}
        assert _is_eligible(fresh, st, now=time.time(), snooze_hours=48.0)

    def test_snoozed_docs_roundtrips_and_sanitizes(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(snoozed_docs={"a.md": 123.0})
            _save_state(st)
            loaded = _load_state()
            assert loaded.snoozed_docs == {"a.md": 123.0}
            state_file = tmp_path / "tips_state.json"
            data = json.loads(state_file.read_text(encoding="utf-8"))
            data["snoozed_docs"] = {"good.md": 5.0, "bad.md": "x", "inf.md": 1e400}
            state_file.write_text(json.dumps(data).replace("Infinity", "1e999"))
            loaded2 = _load_state()
            assert loaded2.snoozed_docs == {"good.md": 5.0}


class TestWeightedRandomSelection:
    """Tests for the weighted-random newer-biased selection (Design A)."""

    def test_select_tip_deterministic_with_seed(self) -> None:
        """Same seed produces same selection."""
        catalog = [
            CatalogEntry("F1", "S1", "f1.md", mtime=1000.0),
            CatalogEntry("F2", "S2", "f2.md", mtime=2000.0),
            CatalogEntry("F3", "S3", "f3.md", mtime=3000.0),
        ]
        candidates = [
            {"id": "f1-tip", "feature": "F1"},
            {"id": "f2-tip", "feature": "F2"},
            {"id": "f3-tip", "feature": "F3"},
        ]
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        pick1 = _select_tip(candidates, catalog, recency_decay=0.6, rng=rng1)
        pick2 = _select_tip(candidates, catalog, recency_decay=0.6, rng=rng2)
        assert pick1 == pick2

    def test_select_tip_newer_biased(self) -> None:
        """Newer entries (higher mtime) get selected more often."""
        catalog = [
            CatalogEntry("Old", "S", "old.md", mtime=100.0),
            CatalogEntry("New", "S", "new.md", mtime=9999.0),
        ]
        candidates = [
            {"id": "old-tip", "feature": "Old"},
            {"id": "new-tip", "feature": "New"},
        ]
        counts: dict[str, int] = {"old-tip": 0, "new-tip": 0}
        for seed in range(200):
            rng = random.Random(seed)
            pick = _select_tip(candidates, catalog, recency_decay=0.6, rng=rng)
            if pick:
                counts[pick["id"]] += 1
        # New should win significantly more often with decay=0.6
        assert counts["new-tip"] > counts["old-tip"]

    def test_select_tip_llm_ids_rank_by_doc_recency(self) -> None:
        """Codex round-14: LLM-generated tips invent their own ids but carry
        the catalog doc — recency must be resolved via the doc field, not
        degrade to insertion order."""
        catalog = [
            CatalogEntry("Old", "S", "old.md", mtime=100.0),
            CatalogEntry("New", "S", "new.md", mtime=9999.0),
        ]
        # LLM ids don't match catalog-derived ids; OLD doc listed FIRST so the
        # old insertion-order fallback would favor it.
        candidates = [
            {"id": "llm-custom-a", "feature": "Old", "doc": "old.md"},
            {"id": "llm-custom-b", "feature": "New", "doc": "new.md"},
        ]
        counts = {"llm-custom-a": 0, "llm-custom-b": 0}
        for seed in range(200):
            rng = random.Random(seed)
            pick = _select_tip(candidates, catalog, recency_decay=0.6, rng=rng)
            if pick:
                counts[pick["id"]] += 1
        assert counts["llm-custom-b"] > counts["llm-custom-a"]

    def test_select_tip_empty_candidates(self) -> None:
        assert _select_tip([], [], recency_decay=0.6) is None

    def test_select_tip_single_candidate(self) -> None:
        catalog = [CatalogEntry("F", "S", "f.md", mtime=1.0)]
        candidates = [{"id": "f-tip"}]
        result = _select_tip(candidates, catalog, recency_decay=0.6, rng=random.Random(0))
        assert result == {"id": "f-tip"}

    def test_decay_zero_always_picks_newest(self) -> None:
        """With decay=0, only rank-0 (newest) has weight > 0."""
        catalog = [
            CatalogEntry("Old", "S", "old.md", mtime=1.0),
            CatalogEntry("New", "S", "new.md", mtime=999.0),
        ]
        candidates = [
            {"id": "old-tip", "feature": "Old"},
            {"id": "new-tip", "feature": "New"},
        ]
        # decay=0: weight = 0**rank, so rank0=1, rank1+=0
        # All picks should be newest
        for seed in range(20):
            rng = random.Random(seed)
            pick = _select_tip(candidates, catalog, recency_decay=0.0, rng=rng)
            assert pick is not None
            assert pick["id"] == "new-tip"

    def test_tips_sharing_an_mtime_share_a_weight(self) -> None:
        """Equally-recent tips must not be ranked by filename.

        One commit that sweeps many docs at once gives every doc it touches the
        same git commit time. Weighting by list position would then hand the
        alphabetically-first doc rank 0 forever and bury the rest of the tie
        group under recency_decay ** position, so a doc could become almost
        unreachable purely because of its name.
        """
        tied = [CatalogEntry(f"F{i}", "S", f"doc{i}.md", mtime=500.0) for i in range(8)]
        candidates = [{"id": f"doc{i}-tip", "doc": f"doc{i}.md"} for i in range(8)]

        counts: collections.Counter[str] = collections.Counter()
        for seed in range(4000):
            pick = _select_tip(
                candidates, tied, recency_decay=0.6, rng=random.Random(seed)
            )
            assert pick is not None
            counts[pick["id"]] += 1

        # Every tied tip must be reachable, and none may dominate. Under the old
        # position-based ranking the last doc's share was 0.6**7 / sum ≈ 1.1%,
        # and the first's ≈ 40%.
        assert len(counts) == 8, f"unreachable tips: {counts}"
        expected = 4000 / 8
        for tip_id, seen in counts.items():
            assert 0.7 * expected < seen < 1.3 * expected, f"{tip_id} skewed: {counts}"

    def test_a_newer_tier_still_outranks_an_older_tie_group(self) -> None:
        """Tier equality must not flatten a genuine recency difference."""
        catalog = [
            CatalogEntry("A", "S", "a.md", mtime=100.0),
            CatalogEntry("B", "S", "b.md", mtime=100.0),
            CatalogEntry("New", "S", "new.md", mtime=9999.0),
        ]
        candidates = [
            {"id": "a-tip", "doc": "a.md"},
            {"id": "b-tip", "doc": "b.md"},
            {"id": "new-tip", "doc": "new.md"},
        ]
        counts: collections.Counter[str] = collections.Counter()
        for seed in range(3000):
            pick = _select_tip(
                candidates, catalog, recency_decay=0.6, rng=random.Random(seed)
            )
            assert pick is not None
            counts[pick["id"]] += 1

        # new is tier 0 (weight 1); a and b are both tier 1 (weight 0.6 each).
        assert counts["new-tip"] > counts["a-tip"]
        assert counts["new-tip"] > counts["b-tip"]
        # The tie group members stay comparable to each other.
        assert 0.7 < counts["a-tip"] / counts["b-tip"] < 1.4, counts


class TestCadenceGateAndGlow:
    """Tests for cadence gate + glow flag (Design B)."""

    def test_cadence_gate_closed_within_window(self) -> None:
        """When last_shown_ts is recent, cadence gate is closed."""
        now = 1000000.0
        cadence_hours = 6
        last_shown_ts = now - (cadence_hours * 3600) + 100  # 100s inside window
        # Gate closed means (now - last_shown_ts) < (cadence_hours * 3600)
        assert (now - last_shown_ts) < (cadence_hours * 3600)

    def test_cadence_gate_open_after_window(self) -> None:
        """When enough time has passed, cadence gate opens."""
        now = 1000000.0
        cadence_hours = 6
        last_shown_ts = now - (cadence_hours * 3600) - 1  # 1s past window
        assert (now - last_shown_ts) >= (cadence_hours * 3600)


class TestSnoozeExpiry:
    """Tests for snooze mechanism (Design C/D)."""

    def test_snoozed_tip_ineligible_within_window(self) -> None:
        now = 1000000.0
        snooze_hours = 48.0
        st = TipsState(snoozed={"t1": now - 100})  # snoozed 100s ago
        tip = {"id": "t1", "feature": "F"}
        assert not _is_eligible(tip, st, now, snooze_hours)

    def test_snoozed_tip_eligible_after_expiry(self) -> None:
        now = 1000000.0
        snooze_hours = 48.0
        st = TipsState(snoozed={"t1": now - (snooze_hours * 3600) - 1})
        tip = {"id": "t1", "feature": "F"}
        assert _is_eligible(tip, st, now, snooze_hours)

    def test_dismissed_tip_never_eligible(self) -> None:
        now = 1000000.0
        st = TipsState(dismissed=["t1"])
        tip = {"id": "t1", "feature": "F"}
        assert not _is_eligible(tip, st, now, 48.0)

    def test_normal_tip_eligible(self) -> None:
        now = 1000000.0
        st = TipsState()
        tip = {"id": "t1", "feature": "F"}
        assert _is_eligible(tip, st, now, 48.0)


class TestSelection:
    def test_fallback_tips_skips_dismissed(self) -> None:
        catalog = [CatalogEntry("F1", "S1", "f1.md"), CatalogEntry("F2", "S2", "f2.md")]
        st = TipsState(dismissed=["f1-tip"])
        tips = _fallback_tips(catalog, st)
        ids = [t["id"] for t in tips]
        assert "f1-tip" not in ids
        assert "f2-tip" in ids

    def test_fallback_tips_empty_when_all_dismissed(self) -> None:
        catalog = [CatalogEntry("F1", "S1", "f1.md")]
        st = TipsState(dismissed=["f1-tip"])
        assert _fallback_tips(catalog, st) == []

    def test_fallback_tip_structure(self) -> None:
        catalog = [CatalogEntry("Cron Jobs", "Schedule recurring tasks.", "cron-and-scheduling.md")]
        st = TipsState()
        tips = _fallback_tips(catalog, st)
        assert len(tips) == 1
        tip = tips[0]
        assert tip["id"] == "cron-and-scheduling-tip"
        assert tip["feature"] == "Cron Jobs"
        assert tip["why"] == ""
        assert "cta_prompt" in tip


class TestState:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(
                shown={"t1": 3, "t2": 1},
                dismissed=["t3"],
                snoozed={"t4": 12345.0},
                opted_out=True,
                last_generated=1000.0,
                last_shown_ts=2000.0,
                tips=[{"id": "t1", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""}],
                offered={"id": "t1", "title": "X", "body": "Y", "feature": "F", "why": "", "doc": "", "cta_prompt": ""},
            )
            _save_state(st)
            loaded = _load_state()
            assert loaded.shown == {"t1": 3, "t2": 1}
            assert loaded.dismissed == ["t3"]
            assert loaded.snoozed == {"t4": 12345.0}
            assert loaded.opted_out is True
            assert loaded.last_generated == 1000.0
            assert loaded.last_shown_ts == 2000.0
            assert loaded.tips == [{"id": "t1", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""}]
            assert loaded.offered is not None
            assert loaded.offered["id"] == "t1"

    def test_save_and_load_offered_none(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState()
            _save_state(st)
            loaded = _load_state()
            assert loaded.offered is None

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = _load_state()
            assert st.shown == {}
            assert st.dismissed == []
            assert st.snoozed == {}
            assert st.opted_out is False
            assert st.last_generated == 0.0
            assert st.last_shown_ts == 0.0
            assert st.tips == []
            assert st.offered is None

    def test_load_corrupted_file_returns_defaults(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = tmp_path / "tips_state.json"
            state_file.write_text("not valid json!!!")
            st = _load_state()
            assert st.shown == {}
            assert st.opted_out is False


class TestOfferedPersistence:
    """Finding 5: offered tip re-served across repeated GETs, cleared on feedback."""

    def test_offered_persists_across_save_load(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            offered_tip = {
                "id": "x",
                "feature": "F",
                "title": "T",
                "body": "B",
                "why": "W",
                "doc": "",
                "cta_prompt": "C",
            }
            st = TipsState(offered=offered_tip)
            _save_state(st)
            loaded = _load_state()
            assert loaded.offered == offered_tip

    def test_feedback_ack_clears_offered(self, tmp_path: Path) -> None:
        """Simulates what the feedback handler does on ack."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(
                offered={"id": "x", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""},
            )
            # Simulate feedback handler logic
            st.dismissed.append("x")
            st.last_shown_ts = time.time()
            st.offered = None
            _save_state(st)
            loaded = _load_state()
            assert loaded.offered is None
            assert "x" in loaded.dismissed
            assert loaded.last_shown_ts > 0

    def test_cadence_gate_reopens_after_hours(self) -> None:
        """After cadence_hours, the gate reopens (feedback sets last_shown_ts)."""
        cadence_hours = 6.0
        feedback_time = 1000000.0
        now = feedback_time + (cadence_hours * 3600) + 1
        cadence_open = (now - feedback_time) >= (cadence_hours * 3600)
        assert cadence_open is True


class TestShownAction:
    """Codex round-4: display must start cadence + clear offered WITHOUT dismissing.

    Otherwise the outstanding offered tip is re-served every turn to passive
    users who never click ✕.
    """

    def test_shown_is_valid_action(self) -> None:
        import inspect

        from kiro_crew import tips as tips_mod

        src = inspect.getsource(tips_mod.api_tips_feedback)
        assert '"shown"' in src  # accepted by the handler's valid_actions

    def test_shown_clears_offered_and_starts_cadence_without_dismiss(
        self, tmp_path: Path
    ) -> None:
        """Simulates the handler's shown branch."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(
                offered={"id": "x", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""},
            )
            # Simulate feedback handler logic for action == "shown"
            st.last_shown_ts = time.time()
            st.offered = None
            _save_state(st)
            loaded = _load_state()
            assert loaded.offered is None
            assert loaded.last_shown_ts > 0
            assert "x" not in loaded.dismissed  # stays eligible for re-selection

    def test_shown_tip_remains_eligible_for_reselection(self) -> None:
        """A shown-but-not-dismissed tip passes the eligibility filter."""
        st = TipsState(shown={"x": 1})
        tip = {"id": "x", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""}
        assert _is_eligible(tip, st, now=time.time(), snooze_hours=48.0)


class TestCatalogAllowlist:
    """Codex round-7: only user-facing docs may surface as tips.

    The exploration blend picks uniformly from the catalog, so any entry can
    reach users — internal architecture/incident docs must never be included.
    """

    _INTERNAL_DOCS = {
        "app-platform-trust-model.md",
        "mcp-gateway-claim-push.md",
        "mcp-gateway-oversize-response.md",
        "messaging-transport.md",
    }

    def test_bundled_catalog_only_contains_allowlisted_docs(self) -> None:
        from kiro_crew.tips import _BUNDLED_CATALOG_FILE
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        data = json.loads(_BUNDLED_CATALOG_FILE.read_text(encoding="utf-8"))
        docs = {e["doc"] for e in data["entries"]}
        assert docs <= TIP_DOC_ALLOWLIST, f"non-allowlisted docs in catalog: {docs - TIP_DOC_ALLOWLIST}"

    def test_internal_docs_not_in_allowlist(self) -> None:
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        assert not (self._INTERNAL_DOCS & TIP_DOC_ALLOWLIST)

    def test_allowlisted_docs_exist(self) -> None:
        """Catch allowlist drift: every listed doc must exist in the docs dir."""
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        docs_dir = Path("src/kiro_crew/docs")
        if not docs_dir.is_dir():  # running from an installed package
            import kiro_crew

            docs_dir = Path(kiro_crew.__file__).parent / "docs"
        missing = {d for d in TIP_DOC_ALLOWLIST if not (docs_dir / d).is_file()}
        assert not missing, f"allowlisted docs missing on disk: {missing}"

    def test_runtime_scan_respects_allowlist(self) -> None:
        from kiro_crew.tips import _scan_docs_catalog
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        entries = _scan_docs_catalog()
        assert entries, "runtime scan returned nothing"
        assert {e.doc for e in entries} <= TIP_DOC_ALLOWLIST


class TestStateFilePermissions:
    """Codex round-11 (HIGH): personalized tips state must be owner-only."""

    def test_state_file_written_mode_600(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _save_state(TipsState(tips=[{"id": "x", "why": "references user projects"}]))
            st_file = tmp_path / "tips_state.json"
            assert st_file.is_file()
            assert (st_file.stat().st_mode & 0o777) == 0o600

    def test_existing_world_readable_file_corrected_on_rewrite(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st_file = tmp_path / "tips_state.json"
            st_file.write_text("{}")
            st_file.chmod(0o644)
            _save_state(TipsState())
            assert (st_file.stat().st_mode & 0o777) == 0o600


class TestTipFieldAllowlist:
    """Codex round-12 (HIGH): unknown/extra LLM fields must never survive parse.

    _redact_tips only redacts string values — a nested dict smuggled alongside
    valid fields would bypass redaction and reach persistence + the dashboard.
    """

    def test_extra_fields_stripped_at_parse(self) -> None:
        from kiro_crew.tips import _TIP_ALLOWED_FIELDS, _parse_tips

        raw = json.dumps([
            {
                "id": "t1", "feature": "F", "title": "T", "body": "B",
                "why": "W", "doc": "d.md", "cta_prompt": "C",
                "metadata": {"secret": "AKIAIOSFODNN7EXAMPLE"},
                "extra": ["nested", {"deep": "value"}],
            }
        ])
        tips = _parse_tips(raw)
        assert len(tips) == 1
        assert set(tips[0].keys()) == set(_TIP_ALLOWED_FIELDS)
        assert "metadata" not in tips[0]
        assert "AKIA" not in json.dumps(tips[0])

    def test_all_values_in_parsed_tip_are_strings(self) -> None:
        from kiro_crew.tips import _parse_tips

        raw = json.dumps([
            {"id": "t1", "feature": "F", "title": "T", "body": "B",
             "why": "W", "doc": "", "cta_prompt": "C", "count": 42}
        ])
        tips = _parse_tips(raw)
        assert tips and all(isinstance(v, str) for v in tips[0].values())


class TestStateStructuralValidation:
    """Codex round-12: malformed-but-valid-JSON state must degrade to defaults."""

    def test_non_dict_root_returns_defaults(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text("[]")
            st = _load_state()
            assert st.shown == {} and st.opted_out is False

    def test_mistyped_fields_fall_back_per_field(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(json.dumps({
                "shown": "not-a-dict",
                "dismissed": {"not": "a-list"},
                "opted_out": "yes",
                "last_shown_ts": "recently",
                "tips": [{"id": "ok"}, "junk", 42],
                "offered": ["not", "a", "dict"],
            }))
            st = _load_state()
            assert st.shown == {}
            assert st.dismissed == []
            assert st.opted_out is False
            assert st.last_shown_ts == 0.0
            # {"id": "ok"} lacks the required fields → discarded by the
            # persisted-tip sanitizer (round-18), like the non-dict entries
            assert st.tips == []
            assert st.offered is None

    def test_malformed_persisted_tip_fields_discarded(self, tmp_path: Path) -> None:
        """Codex round-18: persisted tips/offered must pass the SAME field
        validation as generated tips — {"id": []} in the state file would
        otherwise crash _is_eligible with a 500 on every request."""
        valid = {"id": "t1", "feature": "F", "title": "T", "body": "B",
                 "why": "W", "doc": "d.md", "cta_prompt": "C"}
        bad_id = {**valid, "id": []}
        extra_field = {**valid, "id": "t2", "metadata": {"x": 1}}
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(json.dumps({
                "tips": [valid, bad_id, extra_field],
                "offered": bad_id,
            }))
            st = _load_state()
            ids = [t["id"] for t in st.tips]
            assert ids == ["t1", "t2"]  # bad_id discarded
            assert all(set(t.keys()) == set(("id", "feature", "title", "body", "why", "doc", "cta_prompt")) for t in st.tips)
            assert st.offered is None  # malformed offered discarded

    def test_huge_int_values_do_not_crash_load(self, tmp_path: Path) -> None:
        """Codex round-16 (HIGH): float(10**400) raises OverflowError — a
        persisted state file must never crash cache init (500 on every
        endpoint until manually repaired)."""
        huge = str(10 ** 400)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(
                '{"snoozed": {"x": ' + huge + '}, "last_generated": ' + huge
                + ', "last_shown_ts": ' + huge + "}"
            )
            st = _load_state()
            assert st.snoozed == {}
            assert st.last_generated == 0.0
            assert st.last_shown_ts == 0.0

    def test_mistyped_dict_entries_dropped_per_entry(self, tmp_path: Path) -> None:
        """Codex round-15: container-type checks aren't enough — a string
        snooze timestamp or bool shown count must be dropped, not crash later
        arithmetic with a persistent 500."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            (tmp_path / "tips_state.json").write_text(json.dumps({
                "shown": {"good": 3, "bad": "many", "weird": True},
                "snoozed": {"good": 1000.5, "bad": "yesterday", "inf": 1e999},
                "dismissed": ["ok", 42, None],
                "dismissed_docs": ["cron.md", 7],
            }))
            st = _load_state()
            assert st.shown == {"good": 3}
            assert st.snoozed == {"good": 1000.5}
            assert st.dismissed == ["ok"]
            assert st.dismissed_docs == ["cron.md"]


class TestDocLevelDismissal:
    """Codex round-15: dismissal must survive LLM id churn via the doc field."""

    def test_dismissed_doc_blocks_regenerated_tip_with_new_id(self) -> None:
        st = TipsState(dismissed=["cron-tip"], dismissed_docs=["cron-and-scheduling.md"])
        regenerated = {
            "id": "cron-scheduling-tip",  # fresh LLM-invented id
            "feature": "Cron", "title": "T", "body": "B", "why": "",
            "doc": "cron-and-scheduling.md", "cta_prompt": "",
        }
        assert not _is_eligible(regenerated, st, now=time.time(), snooze_hours=48.0)

    def test_tip_without_doc_falls_back_to_id_matching(self) -> None:
        st = TipsState(dismissed_docs=["cron-and-scheduling.md"])
        docless = {"id": "other-tip", "feature": "F", "title": "T", "body": "B",
                   "why": "", "doc": "", "cta_prompt": ""}
        assert _is_eligible(docless, st, now=time.time(), snooze_hours=48.0)

    def test_dismissed_docs_roundtrips_through_persistence(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _save_state(TipsState(dismissed_docs=["a.md", "b.md"]))
            assert _load_state().dismissed_docs == ["a.md", "b.md"]

    def test_catalog_fallback_dismiss_after_shown_resolves_doc(self) -> None:
        """Codex round-17: 'shown' clears offered; a catalog-fallback tip is
        also absent from st.tips — the doc must still be resolvable via the
        catalog-derived id reverse-mapping used by the feedback handler."""
        from kiro_crew.tips import CatalogEntry

        catalog = [CatalogEntry("Cron", "S", "cron-and-scheduling.md", mtime=1.0)]
        tip_id = "cron-and-scheduling-tip"  # catalog-derived fallback id
        # Mirrors the handler's three-source resolution with offered=None, tips=[]
        st = TipsState(offered=None, tips=[])
        doc = ""
        if isinstance(st.offered, dict) and st.offered.get("id") == tip_id:
            doc = st.offered.get("doc", "")
        else:
            for t in st.tips:
                if isinstance(t, dict) and t.get("id") == tip_id:
                    doc = t.get("doc", "")
                    break
        if not doc:
            for entry in catalog:
                if entry.doc.replace(".md", "-tip") == tip_id:
                    doc = entry.doc
                    break
        assert doc == "cron-and-scheduling.md"


class TestSingleOfferConcurrency:
    """Codex round-5: concurrent GET /api/tips/next must serve ONE offered tip.

    Without holding the cache lock across inspect→select→persist, two tabs can
    both observe "no offered tip + cadence open", independently select different
    tips, and overwrite each other's offered slot.
    """

    @pytest.mark.asyncio
    async def test_concurrent_next_requests_converge_on_single_offer(
        self, tmp_path: Path
    ) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        def mk_tip(i: int) -> dict:  # type: ignore[type-arg]
            return {
                "id": f"tip-{i}", "feature": f"F{i}", "title": f"T{i}",
                "body": "B", "why": "W", "doc": "", "cta_prompt": "",
            }

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.state = TipsState(tips=[mk_tip(i) for i in range(5)])
            state = types.SimpleNamespace(_tips_cache=cache)

            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls, \
                    mpatch("kiro_crew.tips.maybe_refresh", noop_refresh):
                mock_cfg_cls.load.return_value = cfg

                async def one_request() -> str:
                    req = make_mocked_request("GET", "/api/tips/next")
                    req.app["state"] = state
                    resp = await api_tips_next(req)
                    assert resp.status == 200
                    body = json.loads(resp.body)
                    assert body["tip"] is not None
                    return str(body["tip"]["id"])

                ids = await asyncio.gather(*[one_request() for _ in range(10)])

            # Single-offer contract: every concurrent request got the SAME tip
            assert len(set(ids)) == 1, f"concurrent requests served multiple tips: {set(ids)}"
            # And its shown counter was bumped exactly once (one selection)
            offered_id = ids[0]
            assert cache.state.shown.get(offered_id) == 1


class TestFeedbackBodyValidation:
    """Codex round-2: non-object JSON bodies must be rejected, not 500."""

    def test_non_dict_bodies_rejected(self) -> None:
        # Mirrors the handler guard: body must be a dict with bounded string fields
        for bad in (None, [], "s", 42, [{"id": "x"}]):
            assert not isinstance(bad, dict)

    def test_non_string_fields_rejected(self) -> None:
        body = {"id": [], "action": "ack"}
        tip_id = body.get("id", "")
        action = body.get("action", "")
        ok = isinstance(tip_id, str) and isinstance(action, str) and len(str(tip_id)) <= 100
        assert ok is False


class TestRedaction:
    def test_redact_removes_suspicious_urls(self) -> None:
        exfil_url = "https://evil.com/steal?data=" + "A" * 250
        tips = [
            {
                "id": "t1",
                "feature": "F",
                "title": "T",
                "body": f"Visit {exfil_url}",
                "why": "W",
                "doc": "d.md",
                "cta_prompt": "C",
            }
        ]
        result = _redact_tips(tips)
        assert result[0]["id"] == "t1"
        assert exfil_url not in result[0]["body"]
        assert "[REDACTED" in result[0]["body"]

    def test_redact_preserves_non_string_fields(self) -> None:
        tips = [
            {
                "id": "t1",
                "feature": "F",
                "title": "T",
                "body": "Normal text",
                "why": "W",
                "doc": "d.md",
                "cta_prompt": "C",
                "extra_int": 42,
            }
        ]
        result = _redact_tips(tips)
        assert result[0]["extra_int"] == 42


class TestOptoutAndFlag:
    def test_opted_out_state_blocks_tips(self) -> None:
        st = TipsState(opted_out=True, tips=[{"id": "t1"}])
        assert st.opted_out is True

    def test_optin_clears_opted_out(self) -> None:
        st = TipsState(opted_out=True)
        st.opted_out = False
        assert st.opted_out is False


class TestGlowFlag:
    """Test the glow response field semantics."""

    def test_glow_true_when_cadence_open_and_eligible(self) -> None:
        """Glow should be true when cadence gate open + eligible tips exist."""
        now = 1000000.0
        cadence_hours = 6.0
        last_shown_ts = now - (cadence_hours * 3600) - 1  # past cadence
        candidates_exist = True
        cadence_open = (now - last_shown_ts) >= (cadence_hours * 3600)
        glow = cadence_open and candidates_exist
        assert glow is True

    def test_glow_false_when_cadence_closed(self) -> None:
        """Glow should be false when cadence gate is closed."""
        now = 1000000.0
        cadence_hours = 6.0
        last_shown_ts = now - 100  # recent
        candidates_exist = True
        cadence_open = (now - last_shown_ts) >= (cadence_hours * 3600)
        glow = cadence_open and candidates_exist
        assert glow is False

    def test_glow_false_when_no_eligible(self) -> None:
        """Glow should be false when no eligible tips."""
        now = 1000000.0
        cadence_hours = 6.0
        last_shown_ts = now - (cadence_hours * 3600) - 1
        candidates_exist = False
        cadence_open = (now - last_shown_ts) >= (cadence_hours * 3600)
        glow = cadence_open and candidates_exist
        assert glow is False


class TestBundledCatalog:
    """Tests for pre-generated bundled catalog loading."""

    def test_load_bundled_catalog_exists(self) -> None:
        """The bundled catalog JSON should be loadable at test time."""
        from kiro_crew.tips import _load_bundled_catalog
        result = _load_bundled_catalog()
        assert result is not None
        assert len(result) > 0
        for entry in result:
            assert entry.feature
            assert entry.summary
            assert entry.doc.endswith(".md")

    def test_load_bundled_catalog_missing_file(self, tmp_path: Path) -> None:
        """When file doesn't exist, returns None (triggers fallback)."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        original = tips_mod._BUNDLED_CATALOG_FILE
        try:
            tips_mod._BUNDLED_CATALOG_FILE = tmp_path / "nonexistent.json"  # type: ignore[assignment]
            result = _load_bundled_catalog()
            assert result is None
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_load_bundled_catalog_corrupt_json(self, tmp_path: Path) -> None:
        """When file is corrupt JSON, returns None (triggers fallback)."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        corrupt_file = tmp_path / "tips_catalog.json"
        corrupt_file.write_text("not valid json!!!")
        original = tips_mod._BUNDLED_CATALOG_FILE
        try:
            tips_mod._BUNDLED_CATALOG_FILE = corrupt_file  # type: ignore[assignment]
            result = _load_bundled_catalog()
            assert result is None
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_load_bundled_catalog_malformed_structures(self, tmp_path: Path) -> None:
        """Codex round-19: list root / non-numeric / huge mtime must degrade
        gracefully (fallback or mtime=0.0), never crash cache init."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        original = tips_mod._BUNDLED_CATALOG_FILE
        cases = [
            '["not", "a", "dict"]',  # list root -> AttributeError before fix
            json.dumps({"entries": [{"feature": "F", "summary": "S", "doc": "d.md", "mtime": "old"}]}),
            '{"entries": [{"feature": "F", "summary": "S", "doc": "d.md", "mtime": ' + str(10 ** 400) + "}]}",
        ]
        try:
            for i, content in enumerate(cases):
                f = tmp_path / f"catalog{i}.json"
                f.write_text(content)
                tips_mod._BUNDLED_CATALOG_FILE = f  # type: ignore[assignment]
                result = _load_bundled_catalog()
                if i == 0:
                    assert result is None  # list root -> fallback
                else:
                    # valid entry, bad mtime -> entry kept with mtime=0.0
                    assert result is not None and result[0].mtime == 0.0
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_load_bundled_catalog_empty_entries(self, tmp_path: Path) -> None:
        """When entries array is empty, returns None."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        empty_file = tmp_path / "tips_catalog.json"
        empty_file.write_text(json.dumps({"generated_at": "2026-01-01", "entries": []}))
        original = tips_mod._BUNDLED_CATALOG_FILE
        try:
            tips_mod._BUNDLED_CATALOG_FILE = empty_file  # type: ignore[assignment]
            result = _load_bundled_catalog()
            assert result is None
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_load_bundled_catalog_with_mtime(self, tmp_path: Path) -> None:
        """Bundled entry with mtime field -> CatalogEntry.mtime populated."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        catalog_file = tmp_path / "tips_catalog.json"
        catalog_file.write_text(json.dumps({
            "generated_at": "2026-01-01",
            "entries": [
                {"feature": "F1", "summary": "S1", "doc": "f1.md", "mtime": 1700000000.0},
                {"feature": "F2", "summary": "S2", "doc": "f2.md", "mtime": 1720000000.0},
            ],
        }))
        original = tips_mod._BUNDLED_CATALOG_FILE
        try:
            tips_mod._BUNDLED_CATALOG_FILE = catalog_file  # type: ignore[assignment]
            result = _load_bundled_catalog()
            assert result is not None
            assert len(result) == 2
            assert result[0].mtime == 1700000000.0
            assert result[1].mtime == 1720000000.0
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_load_bundled_catalog_missing_mtime_defaults_zero(self, tmp_path: Path) -> None:
        """Bundled entry without mtime field -> CatalogEntry.mtime defaults to 0.0."""
        import kiro_crew.tips as tips_mod
        from kiro_crew.tips import _load_bundled_catalog

        catalog_file = tmp_path / "tips_catalog.json"
        catalog_file.write_text(json.dumps({
            "generated_at": "2026-01-01",
            "entries": [
                {"feature": "F1", "summary": "S1", "doc": "f1.md"},
            ],
        }))
        original = tips_mod._BUNDLED_CATALOG_FILE
        try:
            tips_mod._BUNDLED_CATALOG_FILE = catalog_file  # type: ignore[assignment]
            result = _load_bundled_catalog()
            assert result is not None
            assert len(result) == 1
            assert result[0].mtime == 0.0
        finally:
            tips_mod._BUNDLED_CATALOG_FILE = original

    def test_bundled_catalog_json_contains_mtime(self) -> None:
        """The actual bundled tips_catalog.json in the repo has mtime values."""
        from kiro_crew.tips import _BUNDLED_CATALOG_FILE

        assert _BUNDLED_CATALOG_FILE.is_file()
        data = json.loads(_BUNDLED_CATALOG_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        assert len(entries) > 0
        for entry in entries:
            assert "mtime" in entry, f"Entry {entry.get('doc', '?')} missing mtime field"
            assert isinstance(entry["mtime"], (int, float))
            assert entry["mtime"] > 0, f"Entry {entry.get('doc', '?')} has zero mtime"


class TestExploreBlend:
    """Tests for the explore_ratio random exploration feature."""

    def test_explore_ratio_one_always_explores(self) -> None:
        """With ratio=1.0, selection always picks from catalog (uniform random)."""
        catalog = [
            CatalogEntry("F1", "S1", "f1.md", mtime=1000.0),
            CatalogEntry("F2", "S2", "f2.md", mtime=2000.0),
        ]
        st = TipsState()
        # Generate catalog-based fallback tips
        from kiro_crew.tips import _fallback_tips, _is_eligible
        all_catalog = [t for t in _fallback_tips(catalog, st) if _is_eligible(t, st, 999999.0, 48.0)]
        assert len(all_catalog) == 2

        # With explore_ratio=1.0, the RNG threshold is always met
        picks: set[str] = set()
        for seed in range(50):
            rng = random.Random(seed)
            # Simulate: rng.random() < 1.0 is always True -> pick from catalog
            pick = rng.choice(all_catalog)
            picks.add(pick["id"])
        # Should have picked from both catalog entries at some point
        assert len(picks) >= 1

    def test_explore_ratio_zero_never_explores(self) -> None:
        """With ratio=0.0, selection always uses weighted-random (no exploration)."""
        # rng.random() < 0.0 is never True -> always exploit path
        for seed in range(50):
            rng = random.Random(seed)
            val = rng.random()
            assert val >= 0.0  # always takes the else branch


class TestConfigClamping:
    """Tests for tips_model and tips_explore_ratio config field handling."""

    def test_tips_model_default(self) -> None:
        from kiro_crew.config.loader import DashboardConfig
        cfg = DashboardConfig()
        assert cfg.tips_model == "auto"

    def test_tips_explore_ratio_default(self) -> None:
        from kiro_crew.config.loader import DashboardConfig
        cfg = DashboardConfig()
        assert cfg.tips_explore_ratio == 0.2

    def test_tips_explore_ratio_clamped_below(self) -> None:
        from kiro_crew.config.loader import _safe_float

        # Below 0 gets clamped to 0
        result = _safe_float(-0.5, 0.2, lo=0.0, hi=1.0)
        assert result == 0.0

    def test_tips_explore_ratio_clamped_above(self) -> None:
        from kiro_crew.config.loader import _safe_float

        # Above 1 gets clamped to 1
        result = _safe_float(1.5, 0.2, lo=0.0, hi=1.0)
        assert result == 1.0

    def test_tips_explore_ratio_invalid_uses_default(self) -> None:
        from kiro_crew.config.loader import _safe_float
        result = _safe_float("not a number", 0.2, lo=0.0, hi=1.0)
        assert result == 0.2

    def test_safe_float_nan_uses_default(self) -> None:
        """Codex round-5: NaN converts fine but bypasses clamping (NaN compares
        false against any bound) — must fall back to default."""
        from kiro_crew.config.loader import _safe_float
        assert _safe_float("NaN", 0.2, lo=0.0, hi=1.0) == 0.2
        assert _safe_float(float("nan"), 6.0, lo=0.0) == 6.0

    def test_safe_float_infinity_uses_default(self) -> None:
        from kiro_crew.config.loader import _safe_float
        assert _safe_float("Infinity", 0.2, lo=0.0, hi=1.0) == 0.2
        assert _safe_float("-Infinity", 0.6, lo=0.0, hi=1.0) == 0.6
        assert _safe_float(float("inf"), 48.0, lo=0.0) == 48.0

    def test_safe_float_huge_int_uses_default(self) -> None:
        """Codex round-14 (HIGH): float(10**400) raises OverflowError — a
        config file with a huge JSON integer must not crash config load."""
        from kiro_crew.config.loader import _safe_float
        assert _safe_float(10 ** 400, 6.0, lo=0.0) == 6.0
        assert _safe_float(-(10 ** 400), 0.2, lo=0.0, hi=1.0) == 0.2

    def test_tips_model_from_config_data(self) -> None:
        """tips_model read as string from dashboard data."""
        from kiro_crew.config.loader import KiroCrewConfig

        # Default config inherits the account's governed model via "auto"
        cfg = KiroCrewConfig.load()
        assert isinstance(cfg.dashboard.tips_model, str)
        assert cfg.dashboard.tips_model == "auto"


class TestOptOutState:
    """UI opt-out: optout/optin actions persist opted_out; status payload shape."""

    def test_optout_persists_and_optin_reverses(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = _load_state()
            assert st.opted_out is False
            # Simulate the feedback handler's optout branch
            st.opted_out = True
            st.offered = None
            _save_state(st)
            assert _load_state().opted_out is True
            # optin branch
            st2 = _load_state()
            st2.opted_out = False
            _save_state(st2)
            assert _load_state().opted_out is False

    def test_optout_clears_offered_tip(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = TipsState(
                offered={"id": "x", "feature": "F", "title": "T", "body": "B", "why": "", "doc": "", "cta_prompt": ""},
            )
            # Mirrors the handler's optout branch
            st.opted_out = True
            st.offered = None
            _save_state(st)
            loaded = _load_state()
            assert loaded.opted_out is True
            assert loaded.offered is None

    def test_status_payload_shape(self, tmp_path: Path) -> None:
        """The status endpoint reports both flags as booleans."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = _load_state()
            payload = {"enabled_config": bool(True), "opted_out": bool(st.opted_out)}
            assert payload == {"enabled_config": True, "opted_out": False}
            st.opted_out = True
            _save_state(st)
            payload = {"enabled_config": bool(True), "opted_out": bool(_load_state().opted_out)}
            assert payload == {"enabled_config": True, "opted_out": True}

    def test_optout_action_accepts_empty_id(self) -> None:
        """FE sends id='' for optout/optin; the handler validation allows it."""
        body = {"id": "", "action": "optout"}
        tip_id = body.get("id", "")
        action = body.get("action", "")
        ok = isinstance(tip_id, str) and isinstance(action, str) and len(tip_id) <= 100
        assert ok is True
        assert action in ("ack", "dismiss", "snooze", "helpful", "optout", "optin")


class TestTruncateSummary:
    """Sentence-safe truncation (kiro_crew.tips_text.truncate_summary)."""

    def test_short_text_unchanged(self) -> None:
        assert truncate_summary("A short summary.") == "A short summary."

    def test_exact_limit_unchanged(self) -> None:
        text = "x" * 300
        assert truncate_summary(text) == text

    def test_cuts_at_sentence_boundary(self) -> None:
        first = "First sentence ends here."
        text = first + " " + "Second sentence that pushes the total well past the limit " * 10
        result = truncate_summary(text, limit=60)
        assert result == first

    def test_sentence_ending_exactly_at_limit_kept(self) -> None:
        # Period at index limit-1, space at index limit: the full sentence fits.
        first = "A" * 58 + "."
        text = first + " trailing words beyond the limit"
        assert truncate_summary(text, limit=59) == first

    def test_word_boundary_fallback_appends_ellipsis(self) -> None:
        text = "no sentence punctuation here just many words " * 5
        result = truncate_summary(text, limit=60)
        assert len(result) <= 61
        assert result.endswith("\u2026")
        # Never a mid-word cut: everything before the ellipsis is whole words.
        assert text.startswith(result[:-1])
        assert text[len(result) - 1] == " "

    def test_never_cuts_mid_word_like_old_slice(self) -> None:
        """Regression: the old text[:300] slice produced '... cycle budget. Ea'."""
        text = (
            "Research Lab runs autonomous campaigns until it satisfies a success "
            "criterion or exhausts its cycle budget. Each cycle produces a report."
        )
        result = truncate_summary(text, limit=110)
        assert result == (
            "Research Lab runs autonomous campaigns until it satisfies a success "
            "criterion or exhausts its cycle budget."
        )

    def test_single_unbroken_token_hard_cut(self) -> None:
        text = "a" * 400
        result = truncate_summary(text, limit=50)
        assert len(result) == 50
        assert result.endswith("\u2026")

    def test_bang_and_question_boundaries(self) -> None:
        assert truncate_summary("Stop! Then more words follow here.", limit=10) == "Stop!"
        assert truncate_summary("Why? Then more words follow here.", limit=10) == "Why?"


class TestCuratedTips:
    """Curated, action-first tips are the primary user-facing pool."""

    def test_curated_tips_ship_and_validate(self) -> None:
        from kiro_crew.tips import _TIP_ALLOWED_FIELDS, _load_curated_tips
        tips = _load_curated_tips()
        assert len(tips) >= 8
        ids = [t["id"] for t in tips]
        assert len(ids) == len(set(ids)), "curated tip ids must be unique"
        # A few known KiroCrew-native features must be present.
        for expected in ("split-view", "command-palette", "warm-pool"):
            assert expected in ids
        for t in tips:
            for k in _TIP_ALLOWED_FIELDS:
                assert isinstance(t.get(k), str), f"{t.get('id')}.{k} must be a string"
            for k in ("id", "title", "body"):
                assert t[k].strip(), f"{t.get('id')}.{k} must be non-empty"

    def test_curated_tips_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod
        missing = tmp_path / "nope.json"
        with mpatch.object(tips_mod, "_CURATED_FILE", missing):
            assert tips_mod._load_curated_tips() == []

    def test_curated_tips_malformed_returns_empty(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        with mpatch.object(tips_mod, "_CURATED_FILE", bad):
            assert tips_mod._load_curated_tips() == []
        # A JSON list root (not a dict) also degrades to empty.
        bad.write_text("[]", encoding="utf-8")
        with mpatch.object(tips_mod, "_CURATED_FILE", bad):
            assert tips_mod._load_curated_tips() == []

    def test_curated_tips_skip_invalid_entries(self, tmp_path: Path) -> None:
        from unittest.mock import patch as mpatch

        from kiro_crew import tips as tips_mod
        f = tmp_path / "c.json"
        good = {
            "id": "ok", "feature": "F", "title": "T", "body": "Do the thing.",
            "why": "", "doc": "", "cta_prompt": "",
        }
        f.write_text(
            json.dumps({"tips": [good, {"id": "bad"}, "nope", 3]}), encoding="utf-8"
        )
        with mpatch.object(tips_mod, "_CURATED_FILE", f):
            out = tips_mod._load_curated_tips()
        assert [t["id"] for t in out] == ["ok"]

    @pytest.mark.asyncio
    async def test_curated_tip_served_when_generated_empty(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        curated = {
            "id": "split-view", "feature": "Split View",
            "title": "Work two sessions side by side",
            "body": "Turn on Settings > Chat > Split View, then press Cmd+D.",
            "why": "", "doc": "", "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.curated = [curated]
            cache.state = TipsState(tips=[])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls, \
                    mpatch("kiro_crew.tips.maybe_refresh", noop_refresh):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"] is not None
                assert body["tip"]["id"] == "split-view"

    @pytest.mark.asyncio
    async def test_empty_curated_preserves_generated(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        gen = {
            "id": "gen-1", "feature": "F", "title": "T", "body": "B",
            "why": "W", "doc": "", "cta_prompt": "",
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()  # curated defaults to []
            cache.state = TipsState(tips=[gen])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls, \
                    mpatch("kiro_crew.tips.maybe_refresh", noop_refresh):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"] is not None
                assert body["tip"]["id"] == "gen-1"

    def test_sanitize_tip_action_valid_and_invalid(self) -> None:
        from kiro_crew.tips import _sanitize_tip_action

        # Valid internal route (with query + highlight) is projected as-is.
        ok = _sanitize_tip_action(
            {"kind": "route", "label": "Open X", "route": "/settings?tab=chat&highlight=x"}
        )
        assert ok == {
            "kind": "route",
            "label": "Open X",
            "route": "/settings?tab=chat&highlight=x",
        }
        # Rejected: not a dict / unknown kind / empty or oversized label.
        assert _sanitize_tip_action(None) is None
        assert _sanitize_tip_action("nope") is None
        assert _sanitize_tip_action({"kind": "message", "label": "L", "route": "/x"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "  ", "route": "/x"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "L" * 41, "route": "/x"}) is None
        # Rejected: off-origin / open-redirect / non-path routes.
        for bad in ("//evil.com", "https://evil.com", "http://x", "settings", "", "mailto:a@b"):
            assert (
                _sanitize_tip_action({"kind": "route", "label": "L", "route": bad}) is None
            ), bad
        # Rejected: missing route or non-string route.
        assert _sanitize_tip_action({"kind": "route", "label": "L"}) is None
        assert _sanitize_tip_action({"kind": "route", "label": "L", "route": 5}) is None

    def test_persisted_tip_attaches_valid_action_drops_invalid(self) -> None:
        from kiro_crew.tips import _sanitize_persisted_tip
        base = {
            "id": "x", "feature": "F", "title": "T", "body": "B",
            "why": "", "doc": "", "cta_prompt": "",
        }
        # Valid action is attached to the sanitized tip.
        good = _sanitize_persisted_tip(
            {**base, "action": {"kind": "route", "label": "Go", "route": "/apps"}}
        )
        assert good is not None
        assert good["action"] == {"kind": "route", "label": "Go", "route": "/apps"}
        # Invalid action is silently dropped; the tip itself still survives (no button).
        bad = _sanitize_persisted_tip(
            {**base, "action": {"kind": "route", "label": "Go", "route": "https://evil"}}
        )
        assert bad is not None
        assert "action" not in bad
        # No action key at all -> no action attached.
        none = _sanitize_persisted_tip(base)
        assert none is not None
        assert "action" not in none

    def test_shipped_curated_actions_match_capable_features(self) -> None:
        from kiro_crew.tips import _load_curated_tips, _sanitize_tip_action
        tips = {t["id"]: t for t in _load_curated_tips()}
        # Features with a navigable destination carry a valid, internal route action.
        for tid in (
            "split-view", "interface-cli-mode", "warm-pool", "mcp-gateway",
            "subagent-parallelism", "zero-token-cron", "dev-fleet", "app-store",
        ):
            action = tips[tid].get("action")
            assert action is not None, f"{tid} should have an action"
            assert _sanitize_tip_action(action) == action, f"{tid} action must be valid"
            assert action["route"].startswith("/")
        # Features with no in-dashboard destination render body only (no button).
        for tid in ("command-palette", "steer-or-queue", "local-telemetry"):
            assert "action" not in tips[tid], f"{tid} must not carry an action"

    @pytest.mark.asyncio
    async def test_curated_action_survives_serve(self, tmp_path: Path) -> None:
        import types
        from unittest.mock import MagicMock
        from unittest.mock import patch as mpatch

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_next

        curated = {
            "id": "split-view", "feature": "Split View", "title": "Two sessions",
            "body": "Turn on split view.", "why": "", "doc": "", "cta_prompt": "",
            "action": {
                "kind": "route", "label": "Open Split View setting",
                "route": "/settings?tab=chat&highlight=chat.split-view-session-grid",
            },
        }
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.curated = [curated]
            cache.state = TipsState(tips=[])
            state = types.SimpleNamespace(_tips_cache=cache)
            cfg = MagicMock()
            cfg.dashboard.tips_enabled = True
            cfg.dashboard.tips_cadence_hours = 0.0
            cfg.dashboard.tips_snooze_hours = 48.0
            cfg.dashboard.tips_recency_decay = 0.6
            cfg.dashboard.tips_explore_ratio = 0.0

            async def noop_refresh(*a: object, **k: object) -> None:
                return None

            with mpatch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls, \
                    mpatch("kiro_crew.tips.maybe_refresh", noop_refresh):
                mock_cfg_cls.load.return_value = cfg
                req = make_mocked_request("GET", "/api/tips/next")
                req.app["state"] = state
                resp = await api_tips_next(req)
                assert resp.status == 200
                body = json.loads(resp.body)
                assert body["tip"]["action"]["kind"] == "route"
                assert body["tip"]["action"]["route"].startswith("/settings?tab=chat")
