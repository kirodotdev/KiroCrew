"""Tests for the credit-usage helpers in
kiro_crew.dashboard.handlers.sessions: _parse_usage, _redact_strings, and the
_fetch_usage_bg gating/redaction logic.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions import (
    _normalize_text_usage,
    _parse_usage,
    _redact_strings,
    _text_scrape_regresses_api_value,
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

    def test_parses_bonus_credits_section(self):
        # Bonus / welcome credits are a separate pool spent before the plan.
        raw = (
            "Estimated Usage | resets on 2026-08-01 | KIRO PRO\n"
            " Credits (41.00 of 1000 covered in plan)\n"
            " Bonus Credits:\n"
            "   Welcome bonus: 386.34/500 (expires in 15 days)\n"
        )
        r = _parse_usage(raw)
        assert r["credits_plan"] == 1000.0
        assert r["bonus_label"] == "Welcome bonus"
        assert r["bonus_used"] == 386.34
        assert r["bonus_limit"] == 500.0
        assert r["bonus_expires_label"] == "expires in 15 days"

    def test_no_bonus_fields_without_section(self):
        assert "bonus_limit" not in _parse_usage(SAMPLE_USAGE)


class TestTransientFailureCache:
    def test_preserves_last_good_as_stale(self):
        orig = sessions_mod._usage_cache
        try:
            sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 41.0}
            sessions_mod._cache_transient_failure()
            assert sessions_mod._usage_cache["credits_plan"] == 1000.0
            assert sessions_mod._usage_cache["stale"] is True
        finally:
            sessions_mod._usage_cache = orig

    def test_marks_unavailable_when_no_prior_value(self):
        orig = sessions_mod._usage_cache
        try:
            sessions_mod._usage_cache = {}
            sessions_mod._cache_transient_failure()
            assert sessions_mod._usage_cache == {"available": False}
        finally:
            sessions_mod._usage_cache = orig


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
    sessions_mod._usage_scrape_disabled_logged = False
    sessions_mod._usage_scrape_failures = 0
    sessions_mod._usage_scrape_backoff_until = 0.0


def _enable_text_scrape(monkeypatch):
    """Opt in to the billed /usage text scrape for tests that exercise it.

    The knob defaults to FALSE in production, so any test that expects the
    scrape to run must say so explicitly.
    """
    monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)


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
        _enable_text_scrape(monkeypatch)
        # Bypass OS-sandbox wrap — macOS 26 has no sandbox backend and wrap_argv
        # raises before the subprocess is spawned, making proc=None and skipping
        # the reap path that several tests assert on.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        # Force the text-scrape fallback path by default (the real API client
        # would otherwise read this host's live token). API-primary behavior is
        # covered explicitly in TestFetchUsageBgApi.
        with patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=None):
            yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_no_kiro_bin_caches_unavailable(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value=None):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_parseable_usage_is_cached(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("credits_plan") == 10000.0
        assert sessions_mod._usage_cache.get("plan") == "KIRO POWER"

    @pytest.mark.asyncio
    async def test_text_fallback_launches_resolved_binary_in_place(self):
        # The resolved binary is exec'd at its own path, with no inherited
        # snapshot descriptor — a copy/memfd would strand a multi-call CLI's
        # sibling subcommand executable.
        resolved = "/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli"
        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value=resolved),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await sessions_mod._fetch_usage_bg()

        # Assert the binary's POSITION in argv, not argv[0]: on Linux
        # cgroup_scope_argv prepends a `systemd-run --scope` wrapper, so argv[0]
        # is the wrapper there and the resolved binary follows it. What matters
        # is that the binary appears exactly as resolved — not a private copy.
        argv = list(spawn.await_args.args)
        assert resolved in argv, argv
        assert not any("kiro-cli-snapshots" in str(a) for a in argv), argv
        assert "pass_fds" not in spawn.await_args.kwargs

    @pytest.mark.asyncio
    async def test_unparseable_usage_caches_unavailable(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(b"no usage block here"))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_string_fields_redacted_before_cache(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)), \
             patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)):
            await sessions_mod._fetch_usage_bg()
        # String leaves are scrubbed; numeric fields are left intact.
        assert sessions_mod._usage_cache["plan"] == "REDACTED"
        assert sessions_mod._usage_cache["credits_plan"] == 10000.0

    @pytest.mark.asyncio
    async def test_text_scrape_does_not_clobber_richer_api_value(self):
        # Regression: on a refresh where the API call transiently fails and we
        # fall back to the overage-blind text scrape, the scrape must NOT
        # overwrite a fresher, richer API value for THE SAME account — the bug
        # that flipped the pill from the true 41,336/10,000 (413%) to a
        # misleading 10,000/10,000 (100%, overage hidden). Seed an API value
        # that shows overage and carries this account's email, then run a
        # refresh that falls through to the scrape (fixture stubs the API to
        # return None); whoami reports the SAME email. SAMPLE_USAGE scrapes to
        # total=3164, far below 41,336.
        sessions_mod._usage_cache = {
            "credits_used": 41336.0,
            "credits_plan": 10000.0,
            "credits_overage": 31336.0,
            "percentage": 413.4,
            "plan": "KIRO POWER",
            "resets": "2026-07-01",
            "email": "carol@amazon.com",
            "start_url": "https://amzn.awsapps.com/start",
            "source": "api",
        }
        whoami = AsyncMock(return_value={"email": "carol@amazon.com",
                                         "start_url": "https://amzn.awsapps.com/start"})
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        # The richer API value is kept (not the scrape's 3164) and dimmed stale.
        assert sessions_mod._usage_cache["credits_used"] == 41336.0
        assert sessions_mod._usage_cache["credits_overage"] == 31336.0
        assert sessions_mod._usage_cache["source"] == "api"
        assert sessions_mod._usage_cache["stale"] is True
        # whoami is fetched twice: once at the top of the refresh (credential
        # anchor) and once ADJACENT to the scrape — the guard judges against the
        # adjacent one so a mid-fallback account switch can't be preserved.
        assert whoami.await_count == 2

    @pytest.mark.asyncio
    async def test_billing_cycle_reset_lets_lower_scrape_win(self):
        # New billing cycle + API down: the cached (old-cycle) high value must
        # NOT be preserved just because it's numerically larger — the reset date
        # differs, so the lower scrape is the real new-cycle usage.
        sessions_mod._usage_cache = {
            "credits_used": 41336.0,
            "credits_plan": 10000.0,
            "credits_overage": 31336.0,
            "plan": "KIRO POWER",
            "resets": "2026-08-01",
            "email": "carol@amazon.com",
            "start_url": "https://amzn.awsapps.com/start",
            "source": "api",
        }
        whoami = AsyncMock(return_value={"email": "carol@amazon.com",
                                         "start_url": "https://amzn.awsapps.com/start"})
        # SAMPLE_USAGE carries resets "2026-07-01" — a different cycle from the
        # cached "2026-08-01".
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_used"] == 3164.0
        assert sessions_mod._usage_cache["source"] == "text"

    @pytest.mark.asyncio
    async def test_account_switch_does_not_preserve_prior_accounts_value(self):
        # Cross-account safety: cached value belongs to account A (higher
        # usage); the current identity is account B and its API call fails.
        # The prior A value must NOT be preserved — otherwise A's usage AND
        # email leak onto B's dashboard. The B scrape wins instead.
        sessions_mod._usage_cache = {
            "credits_used": 41336.0,
            "credits_plan": 10000.0,
            "credits_overage": 31336.0,
            "plan": "KIRO POWER",
            "email": "alice@amazon.com",
            "source": "api",
        }
        whoami = AsyncMock(return_value={"email": "bob@amazon.com"})
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        # B's fresh scrape replaced A's value (3164 = covered 3044 + overage 120).
        assert sessions_mod._usage_cache["credits_used"] == 3164.0
        assert sessions_mod._usage_cache["source"] == "text"
        assert sessions_mod._usage_cache.get("email") == "bob@amazon.com"

    @pytest.mark.asyncio
    async def test_reentrancy_guard_skips_when_already_fetching(self):
        sessions_mod._usage_fetching = True
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn") as resolve:
            await sessions_mod._fetch_usage_bg()
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_caches_unavailable_and_reaps(self):
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        # whoami is stubbed so `proc` stands in for the /usage scrape ALONE.
        # _fetch_usage_bg resolves the identity first (it anchors credential
        # selection), which is a second spawn; sharing one mock across both would
        # make this assert on whoami's reap instead of the scrape's.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
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
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
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


class TestTextScrapeRegressesApiValue:
    """The overage-blind text scrape must not clobber a richer API value."""

    ID = {"email": "carol@amazon.com"}

    ID = {"email": "carol@amazon.com", "start_url": "https://amzn.awsapps.com/start"}
    CYCLE = {"resets": "2026-08-01"}

    def test_api_prior_with_more_usage_blocks_scrape(self):
        prev = {"credits_used": 41336.0, "source": "api", **self.ID, **self.CYCLE}
        new = {"credits_used": 3164.0, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev, new, self.ID) is True

    def test_api_prior_with_equal_or_less_usage_allows_scrape(self):
        prev = {"credits_used": 3164.0, "source": "api", **self.ID, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 3164.0, **self.CYCLE}, self.ID) is False
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 9000.0, **self.CYCLE}, self.ID) is False

    def test_missing_reset_date_never_preserved(self):
        # GetUsageLimits omitting nextDateReset -> cached value has no `resets`;
        # a rollover must not be pinned, so preserve only with both dates present.
        prev_no_reset = {"credits_used": 41336.0, "source": "api", **self.ID}
        assert _text_scrape_regresses_api_value(prev_no_reset, {"credits_used": 100.0, **self.CYCLE}, self.ID) is False
        prev = {"credits_used": 41336.0, "source": "api", **self.ID, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 100.0}, self.ID) is False

    def test_different_identity_never_preserved(self):
        # Account switch A->B: cached A value (higher usage) must NOT be kept
        # when the current identity is B — otherwise A's usage + email leak.
        prev = {"credits_used": 41336.0, "source": "api",
                "email": "alice@amazon.com", "start_url": "https://a.awsapps.com/start"}
        new = {"credits_used": 100.0}
        assert _text_scrape_regresses_api_value(
            prev, new, {"email": "bob@amazon.com", "start_url": "https://b.awsapps.com/start"}
        ) is False

    def test_same_email_different_org_never_preserved(self):
        # Same human email across two Identity Center orgs (different start_url)
        # is NOT the same account — must not preserve.
        prev = {"credits_used": 41336.0, "source": "api",
                "email": "carol@amazon.com", "start_url": "https://orgA.awsapps.com/start"}
        new = {"credits_used": 100.0}
        assert _text_scrape_regresses_api_value(
            prev, new, {"email": "carol@amazon.com", "start_url": "https://orgB.awsapps.com/start"}
        ) is False

    def test_different_reset_date_allows_scrape(self):
        # New billing cycle (reset date changed): the lower scrape is a
        # legitimate rollover, not the overage-blind cap — let it win.
        prev = {"credits_used": 41336.0, "source": "api", "resets": "2026-08-01", **self.ID}
        new = {"credits_used": 200.0, "resets": "2026-09-01"}
        assert _text_scrape_regresses_api_value(prev, new, self.ID) is False

    def test_same_reset_date_still_blocks(self):
        prev = {"credits_used": 41336.0, "source": "api", "resets": "2026-08-01", **self.ID}
        new = {"credits_used": 3164.0, "resets": "2026-08-01"}
        assert _text_scrape_regresses_api_value(prev, new, self.ID) is True

    def test_missing_email_on_either_side_never_preserved(self):
        prev_no_email = {"credits_used": 41336.0, "source": "api"}
        assert _text_scrape_regresses_api_value(prev_no_email, {"credits_used": 1.0}, self.ID) is False
        prev = {"credits_used": 41336.0, "source": "api", **self.ID}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 1.0}, {}) is False

    def test_text_sourced_prior_never_protected(self):
        # Only an authoritative API prior is worth protecting; a prior text
        # value (itself capped) must not pin the pill.
        prev = {"credits_used": 10000.0, "source": "text", **self.ID}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 3164.0}, self.ID) is False

    def test_missing_or_non_dict_prior_allows_scrape(self):
        assert _text_scrape_regresses_api_value(None, {"credits_used": 1.0}, self.ID) is False
        assert _text_scrape_regresses_api_value({}, {"credits_used": 1.0}, self.ID) is False
        assert _text_scrape_regresses_api_value(
            {"available": False}, {"credits_used": 1.0}, self.ID
        ) is False

    def test_non_numeric_usage_allows_scrape(self):
        prev = {"credits_used": None, "source": "api", **self.ID, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 1.0, **self.CYCLE}, self.ID) is False
        prev2 = {"credits_used": 5.0, "source": "api", **self.ID, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev2, {"credits_used": None, **self.CYCLE}, self.ID) is False


class TestFetchUsageBgApi:
    """The API path (kiro_usage_api.fetch_usage_limits) is primary; the text
    scrape is only a fallback."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        _enable_text_scrape(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_api_result_is_primary_and_subprocess_not_spawned(self):
        api_dict = {
            "credits_used": 29527.0, "credits_plan": 10000.0,
            "credits_overage": 19527.0, "credits_covered": 10000.0,
            "percentage": 295.3, "cost_usd": 781.08, "plan": "KIRO POWER",
            "source": "api",
        }
        spawn = AsyncMock()
        # The API path now requires a PROVEN profile ARN (no ARN -> the scrape),
        # so whoami is stubbed with one rather than left to the bare spawn mock.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={
                              "email": "me@corp.com",
                              "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict), \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        # API path wins: real total cached, and the CREDIT-CONSUMING text scrape
        # (`kiro-cli chat ... /usage`) is never spawned.
        assert sessions_mod._usage_cache["credits_used"] == 29527.0
        assert sessions_mod._usage_cache["credits_overage"] == 19527.0
        assert sessions_mod._usage_cache["source"] == "api"
        for call in spawn.call_args_list:
            assert "/usage" not in call.args, f"credit-consuming scrape spawned: {call.args}"
            assert "chat" not in call.args, f"chat subprocess spawned: {call.args}"

    @pytest.mark.asyncio
    async def test_api_none_falls_back_to_text_scrape(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=None), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        # Fallback path normalizes: credits_used becomes the TOTAL, source=text.
        assert sessions_mod._usage_cache["credits_plan"] == 10000.0
        assert sessions_mod._usage_cache["credits_used"] == 3164.0
        assert sessions_mod._usage_cache["source"] == "text"

    @pytest.mark.asyncio
    async def test_api_string_fields_redacted_before_cache(self):
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "plan": "SENSITIVE"}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={
                              "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)), \
             patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["plan"] == "REDACTED"
        assert sessions_mod._usage_cache["credits_plan"] == 10.0


class TestFetchWhoami:
    """``_fetch_whoami`` parses the signed-in identity from kiro-cli whoami.

    kiro-cli prints a JSON object FOLLOWED by a non-JSON "Profile:" block, so
    the parser must take only the leading object. Identity is decorative — every
    failure path must yield {} rather than raising into the credit refresh.
    """

    def _run(self, stdout: bytes):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        proc.returncode = 0
        # An absolute path, as the real wrap_argv returns: the spawn shim execs
        # without a PATH search, so a bare name is not a realistic fixture.
        with patch.object(sessions_mod, "wrap_argv", return_value=(["/usr/bin/kiro-cli"], None)), \
             patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            return asyncio.run(sessions_mod._fetch_whoami("kiro-cli"))

    def test_parses_identity_ignoring_trailing_profile_block(self):
        out = self._run(
            b'{\n "accountType": "IamIdentityCenter",\n "email": "me@corp.com",\n'
            b' "region": "us-east-1",\n "startUrl": "https://x.awsapps.com/start"\n}\n'
            b"\nProfile:\nKiroProfile-us-east-1\narn:aws:codewhisperer:...\n"
        )
        assert out["email"] == "me@corp.com"
        assert out["account_type"] == "IamIdentityCenter"
        assert out["start_url"] == "https://x.awsapps.com/start"

    def test_builder_id_account_type(self):
        out = self._run(b'{"accountType":"BuilderId","email":"a@b.com"}')
        assert out == {"email": "a@b.com", "account_type": "BuilderId"}

    def test_non_string_values_dropped(self):
        assert self._run(b'{"email":{"nested":1},"accountType":null}') == {}

    def test_no_json_returns_empty(self):
        assert self._run(b"Not logged in\n") == {}

    def test_unterminated_json_returns_empty(self):
        assert self._run(b'{"email":"a@b.com"') == {}

    def test_values_are_length_bounded(self):
        out = self._run(b'{"email":"' + b"x" * 400 + b'@b.com"}')
        assert len(out["email"]) <= 254


class TestIdentityAccountCoupling:
    """An identity may only be shown next to credits it provably belongs to.

    fetch_usage_limits picks whichever candidate credential the API accepts
    (IDE cache first, then the kiro-cli store) while whoami always reports
    kiro-cli's identity -- so with two accounts signed in they can disagree.
    Attaching the wrong email to an overage bill is a misattribution, so the
    merge is refused unless the accounts provably match.
    """

    def test_matching_arns_are_coupled(self):
        assert sessions_mod._identity_matches_account(
            "arn:aws:codewhisperer:us-east-1:1:profile/A",
            {"email": "a@b.com", "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"},
        ) is True

    def test_differing_arns_are_refused(self):
        # The exact misattribution the reviewer flagged: API billed account A,
        # whoami describes account B.
        assert sessions_mod._identity_matches_account(
            "arn:aws:codewhisperer:us-east-1:1:profile/A",
            {"email": "b@b.com", "_profile_arn": "arn:aws:codewhisperer:us-east-1:2:profile/B"},
        ) is False

    def test_no_arns_is_never_coupled(self):
        # A lone READABLE credential is not proof: kiro-cli may authenticate from
        # a store this module does not enumerate, so whoami's account cannot be
        # tied to the billed one. Individual / Builder ID accounts (no profile
        # ARN) therefore show no identity rather than a possibly-foreign one.
        assert sessions_mod._identity_matches_account(None, {"email": "solo@b.com"}) is False

    def test_one_sided_arn_is_refused(self):
        assert sessions_mod._identity_matches_account(
            "arn:aws:codewhisperer:us-east-1:1:profile/A", {"email": "x@b.com"}
        ) is False
        assert sessions_mod._identity_matches_account(
            None, {"email": "x@b.com", "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"}
        ) is False

    def test_whoami_extracts_profile_arn_from_trailing_block(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(
            b'{"accountType":"IamIdentityCenter","email":"me@corp.com"}\n\n'
            b"Profile:\nKiroProfile-us-east-1\n"
            b"arn:aws:codewhisperer:us-east-1:713669222412:profile/7KHC74QYC9PQ\n", b""))
        proc.returncode = 0
        # An absolute path, as the real wrap_argv returns: the spawn shim execs
        # without a PATH search, so a bare name is not a realistic fixture.
        with patch.object(sessions_mod, "wrap_argv", return_value=(["/usr/bin/kiro-cli"], None)), \
             patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            out = asyncio.run(sessions_mod._fetch_whoami("kiro-cli"))
        assert out["email"] == "me@corp.com"
        assert out["_profile_arn"].endswith("profile/7KHC74QYC9PQ")

    @pytest.mark.asyncio
    async def test_private_coupling_keys_never_reach_the_cache(self):
        _reset_usage_globals()
        api_dict = {
            "credits_used": 100.0, "credits_plan": 10.0, "source": "api",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        identity = {
            "email": "me@corp.com",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=api_dict), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=identity)):
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert cache["email"] == "me@corp.com"          # coupled -> shown
        assert "_profile_arn" not in cache              # private, never served
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_mismatched_identity_is_not_cached(self):
        _reset_usage_globals()
        api_dict = {
            "credits_used": 100.0, "credits_plan": 10.0, "source": "api",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        identity = {
            "email": "other@corp.com",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:2:profile/B",
        }
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=api_dict), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=identity)):
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert "email" not in cache, "a foreign identity must not ride on these credits"
        assert cache["credits_used"] == 100.0
        _reset_usage_globals()


class TestPollDoesNotSpendCredits:
    """The 30s dashboard poll must not be able to trigger a refresh inside the
    interval.

    An earlier revision refreshed whenever the kiro-cli auth store changed on
    disk, to pick a profile switch up in seconds. But that store is SHARED --
    `data.sqlite3` holds `conversations`, `history` and `state` alongside
    `auth_kv` -- so ordinary chat traffic rewrites it roughly every 30 seconds
    (observed: the SQLite header change counter incrementing on that cadence with
    sessions active). The trigger therefore fired on almost every poll, and a fire
    can reach the `/usage` text scrape, which spends credits. Refreshing the
    credit readout must never cost credits on a timer faster than the interval.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        _reset_usage_globals()
        yield
        _reset_usage_globals()

    def _request(self):
        request = MagicMock()
        request.app = {"state": SimpleNamespace(_background_tasks=set())}
        return request

    @pytest.mark.asyncio
    async def test_fresh_cache_never_refreshes(self):
        sessions_mod._usage_cache = {"credits_plan": 10.0}
        sessions_mod._usage_cache_ts = time.time()
        with patch.object(sessions_mod, "reject_if_kiro_unverified",
                          AsyncMock(return_value=None)), \
             patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch:
            resp = await sessions_mod.api_sessions_usage(self._request())
        fetch.assert_not_called()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_elapsed_interval_does_refresh(self):
        # The timer is still the trigger -- this is not "never refresh".
        sessions_mod._usage_cache = {"credits_plan": 10.0}
        sessions_mod._usage_cache_ts = time.time() - (sessions_mod._USAGE_REFRESH_SECS + 1)
        with patch.object(sessions_mod, "reject_if_kiro_unverified",
                          AsyncMock(return_value=None)), \
             patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch:
            await sessions_mod.api_sessions_usage(self._request())
        fetch.assert_called_once()

    def test_no_auth_store_trigger_is_reintroduced(self):
        # Guard the reason, not just the behaviour: a filesystem-watching trigger
        # on this handler cannot distinguish a credential write from a chat write,
        # so reintroducing one re-creates the credit-spend loop.
        assert not hasattr(sessions_mod, "_auth_store_changed")
        assert not hasattr(sessions_mod, "_usage_auth_fingerprint")
        assert not hasattr(sessions_mod.kiro_usage_api, "auth_store_fingerprint")


class TestIdentityIsNotStale:
    """Identity must be re-resolved on every refresh, never memoized.

    A gateway-lifetime cache misattributed credits after an account switch:
    Builder ID A cached -> user signs in as Builder ID B -> the refresh accepts
    B's sole credential and, with no profile ARN on either side, the coupling
    check's single-credential branch passed the STALE A identity onto B's
    credits. whoami is credit-free, so it is simply fetched every refresh.
    """

    @pytest.mark.asyncio
    async def test_identity_refetched_each_refresh(self):
        _reset_usage_globals()
        ARN = "arn:aws:codewhisperer:us-east-1:1:profile/A"
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api",
                    "_profile_arn": ARN}
        calls = []

        async def fake_whoami(_bin):
            calls.append(1)
            return {"email": f"user{len(calls)}@corp.com", "_profile_arn": ARN}

        for _ in range(2):
            sessions_mod._usage_cache_ts = 0.0
            with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
                 patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
                 patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                              return_value=dict(api_dict)), \
                 patch.object(sessions_mod, "_fetch_whoami", fake_whoami):
                await sessions_mod._fetch_usage_bg()
        # Two refreshes -> two whoami resolutions, and the SECOND identity wins.
        assert len(calls) == 2, "whoami must not be memoized across refreshes"
        assert sessions_mod._usage_cache["email"] == "user2@corp.com"
        _reset_usage_globals()

    def test_no_lifetime_identity_cache_exists(self):
        # Guard against the memoization being reintroduced.
        assert not hasattr(sessions_mod, "_identity_cache")


class TestCredentialSelectionIsAnchored:
    """``_fetch_usage_bg`` resolves kiro-cli's own identity FIRST and hands its
    profile ARN to ``fetch_usage_limits``, so credential selection cannot land on
    a profile the user has signed out of."""

    ARN = "arn:aws:codewhisperer:us-east-1:1:profile/A"

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        _enable_text_scrape(monkeypatch)
        yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_whoami_arn_is_passed_as_the_anchor(self):
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api",
                    "_profile_arn": self.ARN}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "me@corp.com",
                                                  "_profile_arn": self.ARN})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict) as fetch:
            await sessions_mod._fetch_usage_bg()
        assert fetch.call_args.kwargs.get("expected_arn") == self.ARN

    @pytest.mark.asyncio
    async def test_api_branch_resolves_whoami_once(self):
        # The API branch gates the identity merge on _identity_matches_account, so
        # a stale identity is caught by the ARN comparison rather than needing a
        # re-fetch. One spawn is therefore correct HERE.
        whoami = AsyncMock(return_value={"email": "me@corp.com", "_profile_arn": self.ARN})
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value={"credits_used": 1.0, "credits_plan": 10.0,
                                        "source": "api", "_profile_arn": self.ARN}):
            await sessions_mod._fetch_usage_bg()
        assert whoami.await_count == 1

    @pytest.mark.asyncio
    async def test_text_branch_re_resolves_whoami_after_the_scrape(self):
        # The text branch merges the identity WITHOUT an ARN check, on the grounds
        # that the scrape and whoami are both kiro-cli's own output. That holds
        # only if they are read adjacently: up to ~2 minutes separates the
        # top-of-refresh whoami from the scrape (whoami 30s + API 30s + scrape
        # 60s), and a profile switch inside that window would pair the OLD email
        # with the NEW account's credits. So identity must be re-resolved AFTER
        # the scrape, and the fresh one must win.
        whoami = AsyncMock(side_effect=[
            {"email": "old@corp.com"},   # top of refresh (the anchor attempt)
            {"email": "new@corp.com"},   # after the scrape (what must be shown)
        ])
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=None), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        assert whoami.await_count == 2, "identity was not re-resolved after the scrape"
        assert sessions_mod._usage_cache["source"] == "text"
        assert sessions_mod._usage_cache["email"] == "new@corp.com", \
            "cached the pre-scrape identity beside post-scrape credits"

    @pytest.mark.asyncio
    async def test_unresolvable_identity_still_uses_the_api(self):
        # _fetch_whoami returns {} for ANY failure, including its 30s timeout. That
        # yields no ARN -- but the API is still safe to use, because with no ARN it
        # anchors on PROVENANCE (kiro-cli's own auth store only). Skipping it here
        # instead would push every such refresh onto the credit-spending scrape.
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api"}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict) as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert sessions_mod._usage_cache["source"] == "api"

    @pytest.mark.asyncio
    async def test_arnless_identity_still_uses_the_api(self):
        # Builder ID: whoami resolves but carries no profile ARN. Same route, and
        # this is the case that would otherwise bill the smallest quotas every
        # refresh, forever.
        api_dict = {"credits_used": 3.0, "credits_plan": 50.0, "source": "api"}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "solo@b.com",
                                                  "account_type": "BuilderId"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict) as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert sessions_mod._usage_cache["source"] == "api"

    @pytest.mark.asyncio
    async def test_arnless_identity_does_not_spawn_the_billed_scrape(self):
        # The harm being prevented, asserted directly: no `kiro-cli chat ... /usage`
        # subprocess for a profile-less account when the API succeeds.
        api_dict = {"credits_used": 3.0, "credits_plan": 50.0, "source": "api"}
        spawn = AsyncMock()
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"account_type": "BuilderId"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict), \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        for call in spawn.call_args_list:
            assert "/usage" not in call.args, f"billed scrape spawned: {call.args}"

    @pytest.mark.asyncio
    async def test_private_coupling_key_never_reaches_the_cache(self):
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api",
                    "_profile_arn": self.ARN}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"_profile_arn": self.ARN})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict):
            await sessions_mod._fetch_usage_bg()
        assert "_profile_arn" not in sessions_mod._usage_cache


class TestTextScrapeIsOptIn:
    """The `/usage` text scrape is a BILLED chat turn, so it only runs on request.

    The refresh fires every ``_USAGE_REFRESH_SECS`` for as long as a dashboard tab
    is open, so an ungated fallback spends credits forever merely to render the
    credit meter.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        # The API path yields no plan, which is exactly what used to fall through
        # to the billed scrape.
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api, "fetch_usage_limits", lambda **k: None
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value={}))
        yield
        _reset_usage_globals()

    def _spawn_mock(self, stdout: bytes = b""):
        return AsyncMock(return_value=_mock_proc(stdout))

    @pytest.mark.asyncio
    async def test_disabled_knob_never_spawns_the_billed_scrape(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        spawn = self._spawn_mock(SAMPLE_USAGE.encode())
        with patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 0, spawn.await_args_list

    @pytest.mark.asyncio
    async def test_enabled_knob_spawns_the_scrape_and_caches_it(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)
        spawn = self._spawn_mock(SAMPLE_USAGE.encode())
        with patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 1
        assert "/usage" in list(spawn.await_args.args)
        assert sessions_mod._usage_cache.get("credits_plan") == 10000.0

    @pytest.mark.asyncio
    async def test_disabled_degrades_to_unavailable_instead_of_erroring(self, monkeypatch):
        # Nothing to show: the pill hides on `available: False` rather than
        # rendering blanks, and the refresh does not raise. The spawn mock holds
        # PARSEABLE output, so a cache carrying a plan would prove the gate leaked.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(SAMPLE_USAGE.encode())):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert "credits_plan" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_disabled_keeps_partial_api_fields(self, monkeypatch):
        # The API answered but carried no plan (e.g. plan name + reset date only).
        # Keep what it gave alongside the unavailable marker instead of discarding it.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: {"plan": "KIRO POWER", "resets": "2026-09-01",
                         "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"},
        )
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("plan") == "KIRO POWER"
        assert sessions_mod._usage_cache.get("resets") == "2026-09-01"
        assert sessions_mod._usage_cache.get("available") is False
        # The private coupling key is stripped on this path too.
        assert "_profile_arn" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_disabled_preserves_a_prior_good_value_as_stale(self, monkeypatch):
        # A previously-good reading for THIS SAME account is dimmed, not blanked —
        # and not replaced by the scrape's own (parseable) numbers, which the gate
        # must never fetch.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(return_value={"email": "a@corp.com",
                                    "start_url": "https://a.awsapps.com/start"}),
        )
        sessions_mod._usage_cache = {"credits_used": 500.0, "credits_plan": 1000.0,
                                     "source": "api", "email": "a@corp.com",
                                     "start_url": "https://a.awsapps.com/start"}
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(SAMPLE_USAGE.encode())):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert sessions_mod._usage_cache["stale"] is True
        assert "available" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_disabled_never_serves_a_different_accounts_balance(self, monkeypatch):
        # Account A's reading is cached; the user switches to account B, whose API
        # returns no plan. With the scrape disabled that answer recurs on every
        # refresh forever, so preserving A would pin A's balance and email on
        # screen indefinitely under B's session.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(return_value={"email": "b@corp.com",
                                    "start_url": "https://b.awsapps.com/start"}),
        )
        sessions_mod._usage_cache = {"credits_used": 9999.0, "credits_plan": 10000.0,
                                     "source": "api", "email": "a@corp.com",
                                     "start_url": "https://a.awsapps.com/start"}
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache.get("credits_used") != 9999.0
        assert sessions_mod._usage_cache.get("credits_plan") != 10000.0
        assert sessions_mod._usage_cache.get("email") != "a@corp.com"

    @pytest.mark.asyncio
    async def test_disabled_never_preserves_an_unproven_identity(self, monkeypatch):
        # The cached reading carries no identity, so it cannot be proven to belong
        # to whoever is signed in now. Unproven means unavailable.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(return_value={"email": "b@corp.com",
                                    "start_url": "https://b.awsapps.com/start"}),
        )
        sessions_mod._usage_cache = {"credits_used": 500.0, "credits_plan": 1000.0,
                                     "source": "api"}
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_disabled_notice_is_logged_once_not_per_cycle(self, monkeypatch, caplog):
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: False)
        caplog.set_level("INFO", logger=sessions_mod.logger.name)
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            for _ in range(4):
                await sessions_mod._fetch_usage_bg()
        hits = [r for r in caplog.records if "text scrape is disabled" in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in hits]
        assert hits[0].levelname == "INFO"

    @pytest.mark.asyncio
    async def test_repeated_failures_back_off_instead_of_retrying_every_ttl(
        self, monkeypatch
    ):
        # Unparseable output costs a billed turn each time, so the scrape stops
        # after the failure threshold rather than firing on every refresh.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)
        spawn = self._spawn_mock(b"not a usage block")
        with patch("asyncio.create_subprocess_exec", spawn):
            for _ in range(sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD + 3):
                await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD
        assert sessions_mod._scrape_in_backoff() is True

    @pytest.mark.asyncio
    async def test_a_timeout_counts_toward_the_backoff(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        spawn = AsyncMock(return_value=proc)
        with patch("asyncio.create_subprocess_exec", spawn):
            for _ in range(sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD + 2):
                await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD
        assert sessions_mod._scrape_in_backoff() is True

    @pytest.mark.asyncio
    async def test_an_api_path_failure_does_not_count_toward_the_backoff(
        self, monkeypatch
    ):
        # A refresh that never reached the scrape says nothing about whether the
        # scrape works, so it must not consume the failure budget.
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(side_effect=OSError("boom"))
        )
        for _ in range(sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD + 2):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 0
        assert sessions_mod._scrape_in_backoff() is False

    @pytest.mark.asyncio
    async def test_a_success_clears_accumulated_failures(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "_text_scrape_enabled", lambda: True)
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(b"garbage")):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 1
        with patch("asyncio.create_subprocess_exec",
                   self._spawn_mock(SAMPLE_USAGE.encode())):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 0

    def test_the_gate_fails_closed_when_config_is_unreadable(self, monkeypatch):
        # A malformed config must never silently start billing chat turns.
        import kiro_crew.config.loader as loader_mod

        monkeypatch.setattr(
            loader_mod.KiroCrewConfig, "load", staticmethod(lambda *a, **k: 1 / 0)
        )
        assert sessions_mod._text_scrape_enabled() is False

    def test_the_knob_defaults_to_off(self):
        from kiro_crew.config.loader import DashboardConfig

        assert DashboardConfig().usage_text_scrape_enabled is False
