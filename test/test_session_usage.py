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
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
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


class TestFetchUsageBgApi:
    """The API path (kiro_usage_api.fetch_usage_limits) is primary; the text
    scrape is only a fallback."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
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
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=api_dict), \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        # API path wins: real total cached, and the CREDIT-CONSUMING text scrape
        # (`kiro-cli chat ... /usage`) is never spawned. A cheap `whoami` spawn
        # for the identity row is allowed -- it costs no credits -- so assert on
        # the invariant that matters rather than on zero subprocesses.
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
        with patch.object(sessions_mod, "wrap_argv", return_value=(["kiro-cli"], None)), \
             patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a), \
             patch.object(sessions_mod, "resource_limit_preexec", return_value=None), \
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
        with patch.object(sessions_mod, "wrap_argv", return_value=(["kiro-cli"], None)), \
             patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a), \
             patch.object(sessions_mod, "resource_limit_preexec", return_value=None), \
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
