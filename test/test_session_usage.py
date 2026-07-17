"""Tests for the credit-usage helpers in
kiro_crew.dashboard.handlers.sessions: _parse_usage, _redact_strings, and the
_fetch_usage_bg gating/redaction logic.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions import (
    _normalize_text_usage,
    _parse_usage,
    _redact_strings,
)

SAMPLE_USAGE = (
    "Some preamble line\n"
    "Estimated Usage\n"
    "Credits used: 120.0\n"
    "You have covered in plan (3044 of 10000) credits, "
    "resets on 2026-07-01 | KIRO POWER\n"
    "Est. cost: $1.50\n"
    "Overage billed at $0.04 per credit\n"
)


class TestParseUsage:
    def test_parses_all_fields(self):
        r = _parse_usage(SAMPLE_USAGE)
        assert r["credits_used"] == 120.0
        assert r["credits_covered"] == 3044.0
        assert r["credits_plan"] == 10000.0
        assert r["resets"] == "2026-07-01"
        assert r["plan"] == "KIRO POWER"
        assert r["cost_usd"] == 1.50
        assert r["overage_rate"] == 0.04  # float on both sources (canonical shape)
        assert "Estimated Usage" in str(r["raw"])

    def test_strips_ansi_escapes(self):
        raw = "\x1b[32mEstimated Usage\x1b[0m\nCredits used: 5\n"
        assert _parse_usage(raw)["credits_used"] == 5.0

    def test_unrecognized_output_has_no_plan(self):
        assert "credits_plan" not in _parse_usage("totally different CLI output")

    def test_empty_input(self):
        assert _parse_usage("") == {"raw": ""}

    def test_malformed_float_skips_field_without_crashing(self):
        # A malformed number must not abort the whole parse (finding: safe float).
        raw = "Estimated Usage\nCredits used: ..\ncovered in plan (3044 of 10000)\n"
        r = _parse_usage(raw)
        assert "credits_used" not in r
        assert r["credits_plan"] == 10000.0

    def test_first_wins_on_duplicate_field(self):
        # A later echoed line must not overwrite the first real value.
        raw = "Estimated Usage\nCredits used: 100\nCredits used: 99999\n"
        assert _parse_usage(raw)["credits_used"] == 100.0


class TestRedactStrings:
    def test_redacts_a_string_leaf(self):
        with patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: (s + "_U", 0)), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s + "_C", 0)):
            assert _redact_strings("x") == "x_U_C"

    def test_recurses_into_dicts_and_lists(self):
        with patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: (s.upper(), 0)), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)):
            out = _redact_strings({"a": "x", "b": ["y", {"c": "z"}]})
        assert out == {"a": "X", "b": ["Y", {"c": "Z"}]}

    def test_non_string_leaves_pass_through(self):
        assert _redact_strings(42) == 42
        assert _redact_strings(3.5) == 3.5
        assert _redact_strings(None) is None


def _reset_usage_globals():
    sessions_mod._usage_cache = {}
    sessions_mod._usage_cache_ts = 0.0
    sessions_mod._usage_fetching = False


def _mock_proc(stdout: bytes):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestFetchUsageBg:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        # Bypass OS-sandbox wrap — macOS 26 has no sandbox backend and wrap_argv
        # raises before the subprocess is spawned, making proc=None and skipping
        # the reap path that several tests assert on.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_no_kiro_bin_caches_unavailable(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value=None):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_parseable_usage_is_cached(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("credits_plan") == 10000.0
        assert sessions_mod._usage_cache.get("plan") == "KIRO POWER"

    @pytest.mark.asyncio
    async def test_unparseable_usage_caches_unavailable(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(b"no usage block here"))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_string_fields_redacted_before_cache(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)), \
             patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)):
            await sessions_mod._fetch_usage_bg()
        # String leaves are scrubbed; numeric fields are left intact.
        assert sessions_mod._usage_cache["plan"] == "REDACTED"
        assert sessions_mod._usage_cache["credits_plan"] == 10000.0

    @pytest.mark.asyncio
    async def test_reentrancy_guard_skips_when_already_fetching(self):
        sessions_mod._usage_fetching = True
        with patch.object(sessions_mod, "_resolve_kiro_bin") as resolve:
            await sessions_mod._fetch_usage_bg()
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_caches_unavailable_and_reaps(self):
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()  # reaped (FDs closed) on the timeout path
        assert sessions_mod._usage_fetching is False

    @pytest.mark.asyncio
    async def test_generic_exception_caches_unavailable_and_reaps(self):
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(sessions_mod, "_resolve_kiro_bin", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()  # reaped (FDs closed) on the error path


class TestNormalizeTextUsage:
    def test_maps_overage_and_total(self):
        # Text parse: credits_used is the OVERAGE field, covered/plan the in-plan.
        parsed = {"credits_used": 120.0, "credits_covered": 3044.0,
                  "credits_plan": 10000.0, "plan": "KIRO POWER", "raw": "x"}
        out = _normalize_text_usage(parsed)
        assert out["credits_used"] == 3164.0        # total = covered + overage
        assert out["credits_overage"] == 120.0
        assert out["credits_covered"] == 3044.0
        assert out["credits_plan"] == 10000.0
        assert out["percentage"] == round(3164.0 / 10000.0 * 100, 1)
        assert out["source"] == "text"
        assert out["plan"] == "KIRO POWER"

    def test_no_overage_line_reports_covered_as_total(self):
        # Post-2.11.x: no "Credits used:" line -> overage defaults to 0.
        parsed = {"credits_covered": 10000.0, "credits_plan": 10000.0}
        out = _normalize_text_usage(parsed)
        assert out["credits_used"] == 10000.0
        assert out["credits_overage"] == 0.0

    def test_no_plan_preserved_untouched(self):
        assert _normalize_text_usage({"raw": ""}) == {"raw": ""}
