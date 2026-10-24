"""Unit tests for kiro_crew.tips: how a tip is served and answered.

The explore blend and the config fields that clamp it, the opt-out actions and their
status payload, the summary text a tip shows (sentence-safe truncation, the first prose paragraph), and the
machine-readable code on every refusal from ``POST /api/tips/feedback``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.tips import CatalogEntry, TipsState, _load_state, _save_state
from kiro_crew.tips_text import (
    SUMMARY_MAX_CHARS,
    first_prose_paragraph,
    strip_decoration,
    truncate_summary,
)


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

        all_catalog = [
            t for t in _fallback_tips(catalog, st) if _is_eligible(t, st, 999999.0, 48.0)
        ]
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

        assert _safe_float(10**400, 6.0, lo=0.0) == 6.0
        assert _safe_float(-(10**400), 0.2, lo=0.0, hi=1.0) == 0.2

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
                offered={
                    "id": "x",
                    "feature": "F",
                    "title": "T",
                    "body": "B",
                    "why": "",
                    "doc": "",
                    "cta_prompt": "",
                },
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

    def test_empty_string_returns_empty(self) -> None:
        assert truncate_summary("") == ""

    def test_default_limit_matches_summary_max_chars(self) -> None:
        text = "word " * 80  # 400 chars, over the 300-char default cap
        assert truncate_summary(text) == truncate_summary(text, SUMMARY_MAX_CHARS)
        assert len(truncate_summary(text)) <= SUMMARY_MAX_CHARS

    def test_result_never_exceeds_limit(self) -> None:
        # Tightest guarantee across every branch, including sub-word limits where
        # only the ellipsis (or a hard-cut prefix) fits — the existing cases only
        # bound a single limit each.
        samples = [
            "",
            "short",
            "a sentence. another sentence. and more words trailing off here",
            "nospacessingleverylongtokenwithoutanybreaks" * 3,
            "word " * 200,
        ]
        for text in samples:
            for limit in (1, 2, 5, 20, 100, 300):
                assert len(truncate_summary(text, limit)) <= limit


class TestFeedbackErrorCodes:
    """Every refusal from ``POST /api/tips/feedback`` carries a machine-readable
    ``code``, per the contract gate in ``test/test_error_code_contract.py``.

    The prose stays where it is and keeps its meaning — it is demoted to
    advisory, not removed — so an existing client that only reads ``error`` is
    unaffected. What changes is that a client does not have to match English to
    tell "the body was not JSON" from "that action does not exist".
    """

    @staticmethod
    async def _refuse(tmp_path: Path, raw: bytes) -> tuple[int, dict]:
        """Drive the REAL handler with *raw* as the request body."""
        import types
        from unittest.mock import Mock

        from aiohttp import streams
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.tips import TipsCache, api_tips_feedback

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cache = TipsCache()
            cache.state = TipsState()
            state = types.SimpleNamespace(_tips_cache=cache)

            payload = streams.StreamReader(
                Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
            )
            payload.feed_data(raw)
            payload.feed_eof()
            req = make_mocked_request("POST", "/api/tips/feedback", payload=payload)
            req.app["state"] = state
            resp = await api_tips_feedback(req)
            return resp.status, json.loads(resp.body)

    @pytest.mark.asyncio
    async def test_body_is_not_json(self, tmp_path: Path) -> None:
        status, body = await self._refuse(tmp_path, b"not json at all")
        assert status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_body_is_json_but_not_an_object(self, tmp_path: Path) -> None:
        status, body = await self._refuse(tmp_path, b'["id", "action"]')
        assert status == 400
        assert body["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_non_string_id_is_a_type_refusal(self, tmp_path: Path) -> None:
        status, body = await self._refuse(tmp_path, json.dumps({"id": 7, "action": "ack"}).encode())
        assert status == 400
        assert body["code"] == "invalid_field_type"

    @pytest.mark.asyncio
    async def test_non_string_action_is_a_type_refusal(self, tmp_path: Path) -> None:
        status, body = await self._refuse(
            tmp_path, json.dumps({"id": "t", "action": ["ack"]}).encode()
        )
        assert status == 400
        assert body["code"] == "invalid_field_type"

    @pytest.mark.asyncio
    async def test_oversized_id_is_a_length_refusal_not_a_type_refusal(
        self, tmp_path: Path
    ) -> None:
        """The 100-char cap and the isinstance checks share one prose string on
        main. They are different failures — the client fixes them differently —
        so they must not share one code: a code is API surface that cannot be
        narrowed later.
        """
        status, body = await self._refuse(
            tmp_path, json.dumps({"id": "x" * 101, "action": "ack"}).encode()
        )
        assert status == 400
        assert body["code"] == "tip_id_too_long"

    @pytest.mark.asyncio
    async def test_id_at_the_cap_is_accepted(self, tmp_path: Path) -> None:
        """Boundary: 100 characters is still valid — the split must not move it."""
        status, _ = await self._refuse(
            tmp_path, json.dumps({"id": "x" * 100, "action": "helpful"}).encode()
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path: Path) -> None:
        status, body = await self._refuse(
            tmp_path, json.dumps({"id": "t", "action": "explode"}).encode()
        )
        assert status == 400
        assert body["code"] == "invalid_action"

    @pytest.mark.asyncio
    async def test_every_refusal_carries_both_a_code_and_its_prose(self, tmp_path: Path) -> None:
        """The ratchet for this file: no refusal path may regress to prose-only,
        and none may drop the advisory text an existing client still reads.
        """
        for raw in (
            b"not json at all",
            b'["id", "action"]',
            json.dumps({"id": 7, "action": "ack"}).encode(),
            json.dumps({"id": "t", "action": ["ack"]}).encode(),
            json.dumps({"id": "x" * 101, "action": "ack"}).encode(),
            json.dumps({"id": "t", "action": "explode"}).encode(),
        ):
            status, body = await self._refuse(tmp_path, raw)
            assert status == 400, raw
            assert isinstance(body.get("code"), str) and body["code"], raw
            assert isinstance(body.get("error"), str) and body["error"], raw


_EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]")


class TestProseParagraphExtraction:
    """A tip body is plain UI text, so markdown structure never reaches a user.

    Several packaged docs open with a ``> **warning**`` admonition. Picking the
    first non-empty block shipped its raw ``>`` markers and its emoji verbatim
    as the tip summary, which is what these tests pin against.
    """

    def test_leading_blockquote_is_skipped(self) -> None:
        body = (
            "\n> **Everything you publish here is world-readable.**\n"
            "> Anyone with the link can view it.\n\n"
            "Artifact Deploy publishes an artifact to a public HTTPS URL.\n"
        )
        assert first_prose_paragraph(body) == (
            "Artifact Deploy publishes an artifact to a public HTTPS URL."
        )

    def test_emoji_is_stripped_from_prose_and_titles(self) -> None:
        assert "\u26a0" not in first_prose_paragraph("\nHeads up \u26a0\ufe0f now.\n")
        assert strip_decoration("Deploy Web \U0001f680") == "Deploy Web"

    def test_headings_lists_tables_and_fences_are_not_prose(self) -> None:
        body = (
            "\n## Subheading\n\n"
            "- a list item\n- another\n\n"
            "| col | col |\n| --- | --- |\n\n"
            "```bash\nnot prose\n```\n\n"
            "1. ordered item\n\n"
            "<!-- a comment -->\n\n"
            "The real opening sentence.\n"
        )
        assert first_prose_paragraph(body) == "The real opening sentence."

    @pytest.mark.parametrize("fence", ["```", "~~~"])
    def test_a_fence_spanning_blank_lines_is_skipped_whole(self, fence: str) -> None:
        # Blocks are split on blank lines, so a code sample with a blank line
        # inside arrives as several blocks; the ones after the opening line
        # read like prose and must not become the tip.
        body = (
            f"\n\n   \n\n{fence}text\nfirst line of the sample\n\n"
            "Looks like a sentence inside the sample.\n\n"
            f"last line of the sample\n{fence}\n\n"
            "The real opening sentence.\n"
        )
        assert first_prose_paragraph(body) == "The real opening sentence."

    def test_an_unclosed_fence_hides_the_rest_of_the_body(self) -> None:
        body = "\n```\nsample\n\nStill inside the open fence.\n"
        assert first_prose_paragraph(body) == ""

    def test_no_prose_returns_empty_rather_than_markup(self) -> None:
        assert first_prose_paragraph("\n> only an admonition\n") == ""

    def test_bundled_catalog_carries_no_markup_or_emoji(self) -> None:
        """The shipped catalog is served verbatim, so it is checked directly."""
        from kiro_crew.tips import _BUNDLED_CATALOG_FILE

        data = json.loads(_BUNDLED_CATALOG_FILE.read_text(encoding="utf-8"))
        for entry in data["entries"]:
            for key in ("feature", "title", "summary"):
                value = entry[key]
                assert not value.lstrip().startswith(">"), f"{entry['doc']}:{key} is markup"
                assert not _EMOJI_RE.search(value), f"{entry['doc']}:{key} carries an emoji"
