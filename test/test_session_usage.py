"""Tests for the credit-usage helpers in
kiro_crew.dashboard.handlers.sessions: _parse_usage, _redact_strings, and the
_fetch_usage_bg gating/redaction logic.
"""
from __future__ import annotations

import asyncio
import threading
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

SAMPLE_USAGE_WITH_BONUS = (
    "Estimated Usage | resets on 2026-08-01 | KIRO PRO+\n"
    "Bonus Credits:\n"
    "   Welcome bonus - 500.00/500 used (13 days left)\n"
    "   Amb-Kiro-crew-test - 185.84/2000 used (153 days left)\n"
    "Credits (635.58 of 2000 covered in plan)\n"
    "Overages: Disabled\n"
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
        assert r["bonus_credits"] == [
            {"name": "Welcome bonus", "used": 386.34, "total": 500.0, "days_left": 15}
        ]

    def test_no_bonus_fields_without_section(self):
        assert "bonus_credits" not in _parse_usage(SAMPLE_USAGE)

    def test_parses_every_bounded_bonus_grant_in_dash_format(self):
        assert _parse_usage(SAMPLE_USAGE_WITH_BONUS)["bonus_credits"] == [
            {"name": "Welcome bonus", "used": 500.0, "total": 500.0, "days_left": 13},
            {
                "name": "Amb-Kiro-crew-test",
                "used": 185.84,
                "total": 2000.0,
                "days_left": 153,
            },
        ]

    def test_skips_malformed_or_unbounded_bonus_grants(self):
        raw = (
            "Estimated Usage\nBonus Credits:\n"
            f"{'x' * 101} - 1/10 used (2 days left)\n"
            "Negative - -1/10 used (2 days left)\n"
            "Huge - 1/1000001 used (2 days left)\n"
            "Malformed - nope\nCredits (1 of 10 covered in plan)\n"
        )
        assert _parse_usage(raw)["bonus_credits"] == []


_IDENTITY_A = {"email": "a@corp.com", "start_url": "https://a.awsapps.com/start"}
_IDENTITY_B = {"email": "b@corp.com", "start_url": "https://b.awsapps.com/start"}
_CACHED_A = {"credits_plan": 1000.0, "credits_used": 41.0, **_IDENTITY_A}


class TestTransientFailureCache:
    """A failed refresh keeps a prior reading only for the SAME account.

    A plan-less refresh is what an account switch A->B looks like when B's
    credential lapsed, and it recurs on every interval until B signs in again --
    so preserving without an identity check would show A's balance and email
    under B's session for as long as that lasts.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        orig = sessions_mod._usage_cache
        yield
        sessions_mod._usage_cache = orig

    def test_preserves_last_good_as_stale_for_the_same_account(self):
        sessions_mod._usage_cache = dict(_CACHED_A)
        sessions_mod._cache_transient_failure(_IDENTITY_A)
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert sessions_mod._usage_cache["stale"] is True

    def test_never_preserves_another_accounts_reading(self):
        sessions_mod._usage_cache = dict(_CACHED_A)
        sessions_mod._cache_transient_failure(_IDENTITY_B, reason="signin_required")
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}

    def test_never_preserves_an_unproven_identity(self):
        # No identity at all (whoami failed or was never reached), and a cached
        # reading that carries none: neither side proves the account, so nothing
        # is kept.
        for identity in (None, {}):
            sessions_mod._usage_cache = dict(_CACHED_A)
            sessions_mod._cache_transient_failure(identity)
            assert sessions_mod._usage_cache == {"available": False}
        sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 41.0}
        sessions_mod._cache_transient_failure(_IDENTITY_A)
        assert sessions_mod._usage_cache == {"available": False}

    def test_marks_unavailable_when_no_prior_value(self):
        sessions_mod._usage_cache = {}
        sessions_mod._cache_transient_failure(_IDENTITY_A)
        assert sessions_mod._usage_cache == {"available": False}


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
    sessions_mod._usage_scrape_failures = 0
    sessions_mod._usage_scrape_backoff_until = 0.0


def _api_result(usage, auth_state=None):
    """Wrap a fake usage value in the ``UsageResult`` fetch_usage_limits returns.

    ``auth_state`` defaults to what the real function would report for this usage
    value -- a dict means a credential was accepted, a bare None means a failure
    it could not prove was an auth problem -- so a test that only cares about the
    number does not have to name a state. The tests that ARE about the auth-class
    states pass one explicitly.
    """
    api = sessions_mod.kiro_usage_api
    if auth_state is None:
        auth_state = api.AUTH_OK if usage is not None else api.AUTH_OTHER
    return api.UsageResult(usage, auth_state)


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
        # A proven, unchanging account: the scrape branch publishes only when the
        # whoami before and after the scrape agree. Tests about identity stub
        # `_fetch_whoami` themselves.
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        # Force the text-scrape fallback path by default (the real API client
        # would otherwise read this host's live token). API-primary behavior is
        # covered explicitly in TestFetchUsageBgApi.
        with patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(None)):
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
    async def test_text_fallback_launches_resolved_binary_in_place(self, monkeypatch):
        # The resolved binary is exec'd at its own path, with no inherited
        # snapshot descriptor — a copy/memfd would strand a multi-call CLI's
        # sibling subcommand executable.
        resolved = "/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli"
        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
        monkeypatch.setenv("PYTHONHOME", "/gateway/pythonhome")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")
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
        env = spawn.await_args.kwargs["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "PYTHONHOME" not in env
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert env["KIROCREW_UNRELATED_KEEPME"] == "keep-this-value"

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
        # whoami is fetched three times: at the top of the refresh (credential
        # anchor), then immediately before and after the scrape — the guard
        # judges against the after-snapshot, the one proven to have been signed
        # in throughout the read, so a mid-fallback switch can't be preserved.
        assert whoami.await_count == 3

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
        # SAMPLE_USAGE carries a reset date from a different cycle than the
        # cached reading.
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
    async def test_timeout_reaps_the_scrape_before_the_adjacent_whoami(self):
        """The wedged child dies BEFORE the identity re-resolve, not after it.

        The adjacent whoami is itself a kiro-cli subprocess of up to its own
        timeout; reaping only in ``finally`` let the scrape outlive the deadline
        by that much, holding the agent lock and its sandbox scope. The order is
        the assertion, on a recording fake; the top-of-refresh whoami is the
        first event, the reap must precede the second.
        """
        events: list[str] = []
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock(side_effect=lambda: events.append("kill"))
        proc.wait = AsyncMock(side_effect=lambda: events.append("wait") or 0)

        async def whoami(_bin):
            events.append("whoami")
            return {}

        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            started = time.monotonic()
            await sessions_mod._fetch_usage_bg()
            elapsed = time.monotonic() - started
        # top whoami, the pre-spawn whoami, the reap, then the handler's whoami.
        assert events == ["whoami", "whoami", "kill", "wait", "whoami"], events
        proc.kill.assert_called_once()  # the finally does not reap a second time
        assert elapsed < 5  # the (mocked) reap is bounded; nothing waited on the child
        assert sessions_mod._usage_cache == {"available": False}
        assert sessions_mod._usage_fetching is False

    @pytest.mark.asyncio
    async def test_exception_reaps_the_scrape_before_the_adjacent_whoami(self):
        events: list[str] = []
        proc = _mock_proc(b"")
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=RuntimeError("pipe broke"))
        proc.kill = MagicMock(side_effect=lambda: events.append("kill"))
        proc.wait = AsyncMock(side_effect=lambda: events.append("wait") or 0)

        async def whoami(_bin):
            events.append("whoami")
            return {}

        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "whoami", "kill", "wait", "whoami"], events
        proc.kill.assert_called_once()
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


class TestFetchUsageDeadline:
    """A hung refresh must release the in-flight guard, not park it forever.

    `_usage_fetching` gates every refresh, and it is only cleared in
    `_fetch_usage_bg`'s `finally`. That `finally` does run on cancellation — but
    not on a hang, so without an overall deadline one wedged call leaves the
    guard True for the process lifetime: the cache is never populated and the
    dashboard's credit pill shows "Checking usage..." indefinitely.
    """

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
    async def test_hung_api_read_still_clears_the_guard(self, monkeypatch):
        # Short deadline so the test does not wait on the production ceiling.
        # raising=False keeps the test independent of the constant existing, so
        # on unfixed code it exercises the real hang instead of erroring on a
        # missing attribute.
        monkeypatch.setattr(
            sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 0.2, raising=False
        )
        released = threading.Event()

        def _hang(*_args, **_kwargs):
            # Blocks like a wedged TLS handshake or a DNS lookup with no
            # resolver: urlopen's own timeout does not cover either.
            released.wait(30)
            return _api_result(None)

        try:
            with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
                 patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
                 patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", _hang):
                # The outer wait_for is the assertion: unfixed, _fetch_usage_bg
                # never returns and this raises instead of hanging the suite.
                await asyncio.wait_for(sessions_mod._fetch_usage_bg(), timeout=10)
        finally:
            released.set()

        assert sessions_mod._usage_fetching is False, "in-flight guard was left set"
        # The pill must resolve rather than spin: no credit plan was obtained, so
        # usage is reported unavailable.
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache_ts > 0

    @pytest.mark.asyncio
    async def test_refresh_after_a_hang_can_still_succeed(self, monkeypatch):
        monkeypatch.setattr(
            sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 0.2, raising=False
        )
        released = threading.Event()

        def _hang(*_args, **_kwargs):
            released.wait(30)
            return _api_result(None)

        try:
            with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
                 patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
                 patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", _hang):
                await asyncio.wait_for(sessions_mod._fetch_usage_bg(), timeout=10)
        finally:
            released.set()

        api_dict = {"credits_used": 12.0, "credits_plan": 100.0, "source": "api"}
        arn = "arn:aws:codewhisperer:us-east-1:1:profile/A"
        # The short deadline exists to bound the phase-1 hang, and its job is
        # done once that pass timed out. A loaded runner can legitimately spend
        # more than 0.2s just reaching the subprocess executor on this pass,
        # and a timeout here lands in the same transient-failure handler as a
        # real hang (leaving credits_plan unset), so give the success pass a
        # window only real work can fill.
        monkeypatch.setattr(
            sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 5.0, raising=False
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "me@corp.com", "_profile_arn": arn})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({**api_dict, "_profile_arn": arn})):
            await asyncio.wait_for(sessions_mod._fetch_usage_bg(), timeout=10)

        assert sessions_mod._usage_cache.get("credits_plan") == 100.0


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
                          return_value=_api_result(api_dict)), \
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
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(None)), \
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
                          return_value=_api_result(api_dict)), \
             patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)), \
             patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["plan"] == "REDACTED"
        assert sessions_mod._usage_cache["credits_plan"] == 10.0


class TestApiKeyAuthFailFast:
    """API-key accounts short-circuit the usage refresh entirely.

    ``kiro-cli whoami`` reports ``accountType=ApiKey`` for API-key auth. Such
    accounts hold no SSO/OIDC bearer token, so ``fetch_usage_limits`` would burn
    its full timeout walking credential stores that cannot contain one — and
    with the text scrape disabled (the production default) no EXPLANATORY
    terminal state ever reached the frontend: the credits panel spun through
    the timeout and then hid itself with no explanation, every refresh. The
    fix publishes a reasoned unavailable marker straight after the identity
    read, BEFORE any credential search or billed scrape.
    """

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
    async def test_api_key_auth_short_circuits_before_usage_api(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "a@b.com",
                                                  "account_type": "ApiKey"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        assert sessions_mod._usage_cache == {"available": False, "reason": "api_key_auth"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported", ["apikey", "APIKEY", " ApiKey ", "API_KEY", "Api-Key"])
    async def test_account_type_comparison_tolerates_respelling(self, reported):
        # The enum spelling is upstream's to change; a drift must degrade to the
        # old slow path at worst, and these spellings must all still fail fast.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"account_type": reported})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        assert sessions_mod._usage_cache == {"available": False, "reason": "api_key_auth"}

    @pytest.mark.asyncio
    async def test_api_key_auth_never_spawns_the_scrape(self, monkeypatch):
        # An API-key account must not reach the text scrape: the harm being
        # prevented, asserted directly.
        spawn = AsyncMock()
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"account_type": "ApiKey"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch, \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_sso_account_types_still_reach_the_usage_api(self):
        # Negative control pinning the branch's condition: a non-ApiKey account
        # takes the normal API path. Removing the fail-fast branch flips the
        # short-circuit tests red; widening its match flips this one red.
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api"}
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={**_IDENTITY_A, "account_type": "IamIdentityCenter"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(api_dict)) as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
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

    def test_spawn_uses_full_agent_environment_scrub(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
        monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/gateway/pycache")
        monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b'{"email":"me@corp.com"}', b""))
        proc.returncode = 0
        spawn = AsyncMock(return_value=proc)
        with (
            patch.object(sessions_mod, "wrap_argv", return_value=(["/usr/bin/kiro-cli"], None)),
            patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            out = asyncio.run(sessions_mod._fetch_whoami("kiro-cli"))

        assert out["email"] == "me@corp.com"
        env = spawn.await_args.kwargs["env"]
        for key in (
            "AWS_SECRET_ACCESS_KEY",
            "SSH_AUTH_SOCK",
            "PYTHONPYCACHEPREFIX",
            "WECOM_SECRET",
        ):
            assert key not in env
        assert env["KIROCREW_UNRELATED_KEEPME"] == "keep-this-value"


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
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(api_dict)), \
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
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(api_dict)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=identity)):
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert "email" not in cache, "a foreign identity must not ride on these credits"
        assert cache["credits_used"] == 100.0
        _reset_usage_globals()


class TestPollNeverRefreshesInsideTheInterval:
    """The 30s dashboard poll must not be able to trigger a refresh inside the
    interval.

    An earlier revision refreshed whenever the kiro-cli auth store changed on
    disk, to pick a profile switch up in seconds. But that store is SHARED --
    `data.sqlite3` holds `conversations`, `history` and `state` alongside
    `auth_kv` -- so ordinary chat traffic rewrites it roughly every 30 seconds
    (observed: the SQLite header change counter incrementing on that cadence with
    sessions active). The trigger therefore fired on almost every poll, and a fire
    can reach the `/usage` text scrape, a minute-scale kiro-cli subprocess.
    Refreshing the credit readout must never spawn that faster than the interval.
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
        current = {"n": 0}

        async def fake_whoami(_bin):
            # A different account per REFRESH (the session was switched between
            # them), stable within one: both whoami reads of a refresh agree.
            calls.append(1)
            return {"email": f"user{current['n']}@corp.com", "_profile_arn": ARN}

        for n in (1, 2):
            current["n"] = n
            sessions_mod._usage_cache_ts = 0.0
            with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
                 patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
                 patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                              return_value=_api_result(dict(api_dict))), \
                 patch.object(sessions_mod, "_fetch_whoami", fake_whoami):
                await sessions_mod._fetch_usage_bg()
        # Two refreshes -> two whoami pairs (top + adjacent to the publish), none
        # memoized, and the LATEST identity wins. The same ARN throughout proves
        # each pair describes one account, so every refresh publishes.
        assert len(calls) == 4, "whoami must not be memoized across refreshes"
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
                          return_value=_api_result(api_dict)) as fetch:
            await sessions_mod._fetch_usage_bg()
        assert fetch.call_args.kwargs.get("expected_arn") == self.ARN

    @pytest.mark.asyncio
    async def test_api_branch_re_resolves_whoami_adjacent_to_the_publish(self):
        # The top-of-refresh whoami anchors the credential the API uses, so the
        # API's own ARN matching THAT identity proves nothing about who is signed
        # in by the time the numbers arrive. A second whoami, adjacent to the
        # publish, is what proves it: whoami, api, whoami -- exactly two.
        events: list[str] = []

        async def whoami(_bin):
            events.append("whoami")
            return {"email": "me@corp.com", "_profile_arn": self.ARN}

        def api(**_k):
            events.append("api")
            return _api_result(
                {"credits_used": 1.0, "credits_plan": 10.0, "source": "api", "_profile_arn": self.ARN}
            )

        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami"], events
        assert sessions_mod._usage_cache["credits_plan"] == 10.0
        assert sessions_mod._usage_cache["email"] == "me@corp.com"

    @pytest.mark.asyncio
    async def test_api_reading_is_discarded_when_the_account_switched_before_the_publish(self):
        # whoami says A at the top; the API (anchored on A) returns A's plan; by
        # the time it arrives the user is on B. Nothing of A may be published:
        # not the balance, not the email -- not even for the moment the adjacent
        # whoami takes -- and nothing of A that the cache already held is kept.
        arn_b = "arn:aws:codewhisperer:us-east-1:2:profile/B"
        sessions_mod._usage_cache = dict(_CACHED_A)  # A's earlier reading is on screen
        seen_during_second_whoami: list[dict] = []
        snapshots = iter([
            {**_IDENTITY_A, "_profile_arn": self.ARN},
            {**_IDENTITY_B, "_profile_arn": arn_b},
        ])

        async def whoami(_bin):
            seen_during_second_whoami.append(dict(sessions_mod._usage_cache))
            return next(snapshots)

        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(
                              {"credits_used": 1.0, "credits_plan": 10.0,
                               "source": "api", "_profile_arn": self.ARN})), \
             patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome is None
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)
        # The check comes BEFORE any publish: while the adjacent whoami ran, the
        # cache still held what it held before this refresh, never the new numbers.
        assert seen_during_second_whoami[1] == _CACHED_A
        assert spawn.await_count == 0  # no scrape is attempted for the previous account either

    @pytest.mark.asyncio
    async def test_api_reading_for_the_same_account_is_published_with_the_adjacent_fields(self):
        # Control for the test above: whoami stays A, so A's reading is published
        # -- labelled from the ADJACENT snapshot, not the top one.
        snapshots = [
            {**_IDENTITY_A, "_profile_arn": self.ARN},
            {**_IDENTITY_A, "_profile_arn": self.ARN, "account_type": "IamIdentityCenter"},
        ]
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=snapshots)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(
                              {"credits_used": 1.0, "credits_plan": 10.0,
                               "source": "api", "_profile_arn": self.ARN})):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 10.0
        assert sessions_mod._usage_cache["email"] == "a@corp.com"
        assert sessions_mod._usage_cache["account_type"] == "IamIdentityCenter"
        assert "_profile_arn" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_two_identity_less_whoamis_fall_through_to_the_scrape(self):
        # The sign-out half of a profile switch: whoami prints no identity, twice,
        # while the outgoing account's token is still on disk -- so the API,
        # anchored on nothing, returned THAT account's plan. Absence of a switch
        # is not evidence of an account: the API numbers are never published,
        # and the refresh falls through to kiro-cli's own /usage panel -- whose
        # reading is held to the same proof, so with whoami still silent on both
        # sides of the scrape the refresh ends with no reading at all.
        api_plan = {"credits_used": 777.0, "credits_plan": 9000.0, "source": "api"}
        published: list[dict] = []
        real_publish = sessions_mod._publish_usage

        def recording_publish(payload):
            published.append(dict(payload))
            real_publish(payload)

        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(dict(api_plan))) as fetch, \
             patch.object(sessions_mod, "_publish_usage", recording_publish), \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        # The scrape WAS attempted (the fallback, not a dead end)...
        assert spawn.await_count == 1
        assert any("/usage" in call.args for call in spawn.call_args_list)
        # ...but its reading is unproven too: nothing is published, from either
        # source, at any moment.
        assert sessions_mod._usage_cache == {"available": False}
        assert published == [], published

    @pytest.mark.asyncio
    async def test_two_identity_less_whoamis_parked_surface_no_api_numbers(self):
        # Same unproven pair, scrape parked: the API reading must not ride the
        # parked marker either -- the frontend reads `credits_plan` before it
        # reads `available`, so a partial payload carrying it would render.
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"credits_used": 777.0, "credits_plan": 9000.0})), \
             patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert spawn.await_count == 0
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_an_identity_that_appeared_only_at_the_publish_is_a_switch(self):
        # Nothing at the top (the API anchored on nothing), B adjacent to the
        # publish: whoever the API read, it was not proven to be B. Discard,
        # keep nothing, and do not spend a scrape on the previous account.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[{}, dict(_IDENTITY_B)])), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0})), \
             patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        assert spawn.await_count == 0

    # ---- the /usage scrape is bracketed too -------------------------------

    def _scrape_bracket(self, whoami_side_effect, stdout=SAMPLE_USAGE.encode(), prior=None):
        """Run one refresh whose API yields no plan, recording every publish and
        the order of whoami / api / spawn events."""
        events: list[str] = []
        published: list[dict] = []
        real_publish = sessions_mod._publish_usage
        snapshots = iter(whoami_side_effect)

        async def whoami(_bin):
            events.append("whoami")
            return next(snapshots)

        def api(**_k):
            events.append("api")
            return _api_result(None)

        async def spawn(*_a, **_k):
            events.append("spawn")
            return _mock_proc(stdout)

        def recording_publish(payload):
            published.append(dict(payload))
            real_publish(payload)

        if prior is not None:
            sessions_mod._usage_cache = dict(prior)
        return events, published, whoami, api, spawn, recording_publish

    @pytest.mark.asyncio
    async def test_the_scrape_is_bracketed_by_a_whoami_before_and_after(self):
        # whoami, api, whoami, spawn, whoami: one snapshot immediately before the
        # child is spawned and one immediately after it returns. The reading is
        # labelled from the AFTER snapshot (the one proven to have been signed in
        # throughout the read), so a field only that snapshot carries is what
        # lands on the reading.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [dict(_IDENTITY_A), dict(_IDENTITY_A), {**_IDENTITY_A, "account_type": "IamIdentityCenter"}]
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api), \
             patch.object(sessions_mod, "_publish_usage", pub), \
             patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert len(published) == 1
        assert published[0]["credits_plan"] == 10000.0
        assert published[0]["email"] == "a@corp.com"
        assert published[0]["account_type"] == "IamIdentityCenter"

    @pytest.mark.asyncio
    async def test_a_switch_between_the_scrape_and_its_after_whoami_publishes_nothing(self):
        # A before the child ran, B once it returned: whoever the child read, the
        # reading is unpublishable -- and A's prior reading is not kept either.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [dict(_IDENTITY_A), dict(_IDENTITY_A), dict(_IDENTITY_B)], prior=_CACHED_A
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api), \
             patch.object(sessions_mod, "_publish_usage", pub), \
             patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert published == [], published
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)
        assert "b@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_switch_before_the_scrape_is_caught_by_the_pre_spawn_whoami(self):
        # A at the top, B on both sides of the child: the child read B and the
        # bracket proves B, so B's reading publishes -- labelled B, never A.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [dict(_IDENTITY_A), dict(_IDENTITY_B), dict(_IDENTITY_B)], prior=_CACHED_A
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api), \
             patch.object(sessions_mod, "_publish_usage", pub), \
             patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            await sessions_mod._fetch_usage_bg()
        assert len(published) == 1
        assert published[0]["email"] == "b@corp.com"
        assert sessions_mod._usage_cache["email"] == "b@corp.com"
        assert "a@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_two_empty_whoamis_around_the_scrape_publish_nothing(self):
        # A switch between two empty snapshots is invisible, so a scrape read
        # between them is unpublishable; there is no source left to fall back
        # to, so this refresh ends with no reading.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [{}, {}, {}], prior=_CACHED_A
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api), \
             patch.object(sessions_mod, "_publish_usage", pub), \
             patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            await sessions_mod._fetch_usage_bg()
        assert events == ["whoami", "api", "whoami", "spawn", "whoami"], events
        assert published == [], published
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_top_whoami_that_failed_does_not_veto_a_proven_scrape(self):
        # The top whoami timed out (empty); the bracket around the child proves
        # A on both sides. The scrape is judged by ITS bracket, not the top.
        events, published, whoami, api, spawn, pub = self._scrape_bracket(
            [{}, dict(_IDENTITY_A), dict(_IDENTITY_A)]
        )
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", api), \
             patch.object(sessions_mod, "_publish_usage", pub), \
             patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            await sessions_mod._fetch_usage_bg()
        assert len(published) == 1
        assert published[0]["email"] == "a@corp.com"

    @pytest.mark.asyncio
    async def test_a_parked_refresh_drops_the_api_partials_when_the_account_is_unproven(self):
        # The API's plan name came from a credential anchored on the top whoami;
        # it rides the parked marker only when the parked path's own whoami
        # proves that account is still signed in.
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(side_effect=[dict(_IDENTITY_A), dict(_IDENTITY_B)])), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"plan": "KIRO POWER", "resets": "2026-09-01"})), \
             patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert spawn.await_count == 0
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_builder_id_pair_that_agrees_on_its_email_alone_publishes(self):
        # Builder ID reports neither an ARN nor a start_url: the email is the
        # whole evidence, and an unchanged one is agreement, not a refusal.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "solo@b.com", "account_type": "BuilderId"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"credits_used": 3.0, "credits_plan": 50.0})):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 50.0
        # No ARN on either side: the numbers publish, the identity is not coupled.
        assert "email" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_the_same_email_in_a_different_org_is_a_switch(self):
        # Same human email, different Identity Center instance: the start_url
        # differs, so this is two accounts, exactly as _same_identity rules.
        snapshots = [dict(_IDENTITY_A), {**_IDENTITY_A, "start_url": _IDENTITY_B["start_url"]}]
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=snapshots)), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0})):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_switch_seen_by_email_alone_is_still_a_switch(self):
        # Identity Center shape without ARNs: email + start_url flip -> refused.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(side_effect=[dict(_IDENTITY_A), dict(_IDENTITY_B)])), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result({"credits_used": 1.0, "credits_plan": 10.0})):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_an_identity_that_vanished_before_the_publish_is_a_switch(self):
        # A at the top, nothing at all adjacent (the session lapsed mid-refresh):
        # evidence on one side only is a switch, not agreement -- discard, keep
        # nothing, no scrape for the account that is gone.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(side_effect=[{**_IDENTITY_A, "_profile_arn": self.ARN}, {}])), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(
                              {"credits_used": 1.0, "credits_plan": 10.0, "_profile_arn": self.ARN})), \
             patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        assert spawn.await_count == 0

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
            {"email": "new@corp.com"},   # immediately before the scrape
            {"email": "new@corp.com"},   # immediately after it (what must be shown)
        ])
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", whoami), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(None)), \
             patch("asyncio.create_subprocess_exec",
                   AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))):
            await sessions_mod._fetch_usage_bg()
        assert whoami.await_count == 3, "the scrape was not bracketed by whoami before and after"
        assert sessions_mod._usage_cache["source"] == "text"
        assert sessions_mod._usage_cache["email"] == "new@corp.com", \
            "cached the pre-scrape identity beside post-scrape credits"

    @pytest.mark.asyncio
    async def test_unresolvable_identity_asks_the_api_but_cannot_publish_it(self):
        # _fetch_whoami returns {} for ANY failure, including its 30s timeout. That
        # yields no ARN -- the API is still asked (with no ARN it anchors on
        # PROVENANCE, kiro-cli's own auth store), but with no identity on either
        # side of the refresh nothing proves WHOSE token that store held, so the
        # answer is not published; the /usage scrape is tried and, unproven for
        # the same reason, publishes nothing either.
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api"}
        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(api_dict)) as fetch, \
             patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert spawn.await_count == 1
        assert sessions_mod._usage_cache == {"available": False}

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
                          return_value=_api_result(api_dict)) as fetch:
            await sessions_mod._fetch_usage_bg()
        fetch.assert_called_once()
        assert fetch.call_args.kwargs.get("expected_arn") is None
        assert sessions_mod._usage_cache["source"] == "api"

    @pytest.mark.asyncio
    async def test_arnless_identity_does_not_spawn_the_billed_scrape(self):
        # The harm being prevented, asserted directly: no `kiro-cli chat ... /usage`
        # subprocess for a profile-less account when the API succeeds. The account
        # is still PROVEN across the refresh by its email, which Builder ID reports.
        api_dict = {"credits_used": 3.0, "credits_plan": 50.0, "source": "api"}
        spawn = AsyncMock()
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"), \
             patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)), \
             patch.object(sessions_mod, "_fetch_whoami",
                          AsyncMock(return_value={"email": "solo@b.com", "account_type": "BuilderId"})), \
             patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits",
                          return_value=_api_result(api_dict)), \
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
                          return_value=_api_result(api_dict)):
            await sessions_mod._fetch_usage_bg()
        assert "_profile_arn" not in sessions_mod._usage_cache


class TestScrapeIsTheAutomaticFallback:
    """The `/usage` text scrape runs whenever the API path returns no plan.

    `/usage` is a kiro-cli slash command answered locally from the same free
    GetUsageLimits call, so there is no config key in front of it. The only gate
    left is the back-off that parks a scrape whose output stops parsing, and
    everything the parked path caches must stay identity-safe.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        # The API path yields no plan -- the case that falls through to the scrape.
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api, "fetch_usage_limits", lambda **k: _api_result(None)
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        yield
        _reset_usage_globals()

    def _spawn_mock(self, stdout: bytes = b""):
        return AsyncMock(return_value=_mock_proc(stdout))

    def _park(self):
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600

    @pytest.mark.asyncio
    async def test_no_plan_from_the_api_runs_the_scrape_with_no_config_key(self):
        spawn = self._spawn_mock(SAMPLE_USAGE.encode())
        with patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 1
        assert "/usage" in list(spawn.await_args.args)
        assert sessions_mod._usage_cache.get("credits_plan") == 10000.0

    def test_there_is_no_opt_in_left_to_consult(self):
        from kiro_crew.config.sections import DashboardConfig

        assert not hasattr(sessions_mod, "_text_scrape_enabled")
        assert "usage_text_scrape_enabled" not in DashboardConfig.__dataclass_fields__

    @pytest.mark.asyncio
    async def test_no_kiro_bin_marker_stays_reason_free(self):
        # The definitive kiro-cli-absent verdict must keep hiding the pill: a
        # non-Kiro provider has no credits to explain, so no reason rides it.
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value=None):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_parked_scrape_is_not_spawned(self):
        self._park()
        spawn = self._spawn_mock(SAMPLE_USAGE.encode())
        with patch("asyncio.create_subprocess_exec", spawn):
            outcome = await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 0
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert sessions_mod._usage_cache.get("available") is False
        assert "credits_plan" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_a_running_scrape_reports_no_parking(self):
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(SAMPLE_USAGE.encode())):
            outcome = await sessions_mod._fetch_usage_bg()
        assert outcome is None

    @pytest.mark.asyncio
    async def test_parked_keeps_partial_api_fields(self, monkeypatch):
        # The API answered but carried no plan (e.g. plan name + reset date only).
        # Keep what it gave alongside the unavailable marker instead of discarding it.
        self._park()
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result(
                {
                    "plan": "KIRO POWER",
                    "resets": "2026-09-01",
                    "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
                }
            ),
        )
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("plan") == "KIRO POWER"
        assert sessions_mod._usage_cache.get("resets") == "2026-09-01"
        assert sessions_mod._usage_cache.get("available") is False
        # The private coupling key is stripped on this path too.
        assert "_profile_arn" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_parked_preserves_a_prior_good_value_as_stale(self, monkeypatch):
        # An earlier good reading for THIS SAME account is dimmed, not blanked.
        self._park()
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(
                return_value={"email": "a@corp.com", "start_url": "https://a.awsapps.com/start"}
            ),
        )
        sessions_mod._usage_cache = {
            "credits_used": 500.0,
            "credits_plan": 1000.0,
            "source": "api",
            "email": "a@corp.com",
            "start_url": "https://a.awsapps.com/start",
        }
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(SAMPLE_USAGE.encode())):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert sessions_mod._usage_cache["stale"] is True
        assert "available" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_parked_never_serves_a_different_accounts_balance(self, monkeypatch):
        # Account A's reading is cached; the user switches to account B, whose API
        # returns no plan. While the scrape is parked that answer recurs for hours,
        # so preserving A would pin A's balance and email on screen under B's session.
        self._park()
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(
                return_value={"email": "b@corp.com", "start_url": "https://b.awsapps.com/start"}
            ),
        )
        sessions_mod._usage_cache = {
            "credits_used": 9999.0,
            "credits_plan": 10000.0,
            "source": "api",
            "email": "a@corp.com",
            "start_url": "https://a.awsapps.com/start",
        }
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache.get("credits_used") != 9999.0
        assert sessions_mod._usage_cache.get("credits_plan") != 10000.0
        assert sessions_mod._usage_cache.get("email") != "a@corp.com"

    @pytest.mark.asyncio
    async def test_parked_never_preserves_an_unproven_identity(self, monkeypatch):
        # The cached reading carries no identity, so it cannot be proven to belong
        # to whoever is signed in now. Unproven means unavailable.
        self._park()
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(
                return_value={"email": "b@corp.com", "start_url": "https://b.awsapps.com/start"}
            ),
        )
        sessions_mod._usage_cache = {"credits_used": 500.0, "credits_plan": 1000.0, "source": "api"}
        with patch("asyncio.create_subprocess_exec", self._spawn_mock()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_repeated_failures_back_off_instead_of_retrying_every_ttl(self):
        # Unparseable output is a subprocess spent for nothing each time, so the
        # scrape stops after the failure threshold rather than firing on every
        # refresh.
        spawn = self._spawn_mock(b"not a usage block")
        with patch("asyncio.create_subprocess_exec", spawn):
            for _ in range(sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD + 3):
                await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD
        assert sessions_mod._scrape_in_backoff() is True

    @pytest.mark.asyncio
    async def test_a_timeout_counts_toward_the_backoff(self):
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
    async def test_an_api_path_failure_does_not_count_toward_the_backoff(self, monkeypatch):
        # A refresh that never reached the scrape says nothing about whether the
        # scrape works, so it must not consume the failure budget.
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(side_effect=OSError("boom"))
        )
        for _ in range(sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD + 2):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 0
        assert sessions_mod._scrape_in_backoff() is False

    @pytest.mark.asyncio
    async def test_a_success_clears_accumulated_failures(self):
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(b"garbage")):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 1
        with patch("asyncio.create_subprocess_exec", self._spawn_mock(SAMPLE_USAGE.encode())):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_scrape_failures == 0


class TestUnavailableReasonNamesTheRealRemedy:
    """The pill's ``reason`` names a lapsed sign-in when that is what the API saw.

    ``signin_required`` renders "sign in again". It is attached only when the
    API's own verdict was auth-class (no readable credential, or a rejected one)
    AND the refresh still ended without a reading -- because the scrape failed
    too, or because it was parked. A working scrape always outranks the verdict:
    an empty candidate list does not prove kiro-cli cannot authenticate.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        yield
        _reset_usage_globals()

    def _api(self, monkeypatch, usage, auth_state):
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result(usage, auth_state),
        )

    def _scrape(self, stdout: bytes = b"not a usage block"):
        return patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_mock_proc(stdout)))

    # ---- the scrape ran and failed ----------------------------------------

    @pytest.mark.asyncio
    async def test_expired_credential_reports_signin_required(self, monkeypatch):
        # kiro-cli's stored token lapsed with nothing renewing it: no candidate
        # remained for the API, and the scrape (the same sign-in) prints no table.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_NO_CREDENTIAL)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}

    @pytest.mark.asyncio
    async def test_rejected_credential_reports_signin_required(self, monkeypatch):
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_REJECTED)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("reason") == "signin_required"

    @pytest.mark.asyncio
    async def test_no_plan_for_the_account_reports_no_reason(self, monkeypatch):
        # The API answered ABOUT the account and it has no credit plan; the scrape
        # found none either. Nothing here is a sign-in problem, so no remedy is
        # claimed and the pill hides as it does for any read that found nothing.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_prior_good_value_for_this_account_is_kept_stale_over_the_reason(
        self, monkeypatch
    ):
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_NO_CREDENTIAL)
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=_IDENTITY_A))
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["stale"] is True
        assert "reason" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_a_lapsed_switch_to_another_account_never_shows_the_previous_one(
        self, monkeypatch
    ):
        # Account A is cached. The user switches kiro-cli to account B, whose
        # credential has lapsed: the API reports no credential, and the scrape
        # (the same sign-in) prints no table. This refresh must not keep A's
        # balance and email on screen under B's session.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_NO_CREDENTIAL)
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=_IDENTITY_B))
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape() as spawn:
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 1
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_switch_inside_the_refresh_window_is_caught_before_preserving(
        self, monkeypatch
    ):
        # whoami at the top of the refresh still said A; by the time the scrape
        # has failed (API attempt + scrape timeout later) the user is on B. The
        # failure path must judge against a whoami resolved THEN, not the stale
        # top-of-refresh one, or A's balance is kept under B's session.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        monkeypatch.setattr(
            sessions_mod,
            "_fetch_whoami",
            AsyncMock(side_effect=[_IDENTITY_A, _IDENTITY_B, _IDENTITY_B]),
        )
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_whoami_that_fails_adjacent_to_the_failed_scrape_preserves_nothing(
        self, monkeypatch
    ):
        # Top-of-refresh whoami A, the adjacent one answers nothing: unproven.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[_IDENTITY_A, {}, {}])
        )
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_an_unproven_identity_never_preserves_after_a_failed_scrape(self, monkeypatch):
        # whoami answered with no identity throughout: unproven means unavailable,
        # the same rule the parked path applies.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value={}))
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_a_timed_out_refresh_keeps_only_the_same_accounts_reading(self, monkeypatch):
        # The deadline path re-resolves whoami before deciding what to keep: the
        # top-of-refresh identity was A, the hang outlasted a switch to B, so A
        # is not kept.
        monkeypatch.setattr(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 0.2, raising=False)
        # A at the top; the handler's whoami (the API hang is what times out
        # here, so no scrape bracket is taken) says B.
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[_IDENTITY_A, _IDENTITY_B])
        )
        released = threading.Event()

        def _hang(**_kwargs):
            released.wait(30)
            return _api_result(None)

        monkeypatch.setattr(sessions_mod.kiro_usage_api, "fetch_usage_limits", _hang)
        sessions_mod._usage_cache = dict(_CACHED_A)
        try:
            await asyncio.wait_for(sessions_mod._fetch_usage_bg(), timeout=10)
        finally:
            released.set()
        assert sessions_mod._usage_cache == {"available": False}

    # ---- the scrape was parked --------------------------------------------

    @pytest.mark.asyncio
    async def test_a_parked_scrape_carries_the_api_verdict(self, monkeypatch):
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_NO_CREDENTIAL)
        with self._scrape(SAMPLE_USAGE.encode()) as spawn:
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 0
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}

    @pytest.mark.asyncio
    async def test_a_parked_scrape_judges_the_prior_reading_against_an_adjacent_whoami(
        self, monkeypatch
    ):
        # Top-of-refresh whoami still said A; by the time the API has answered
        # "no plan" the user is on B, and the scrape is parked. The parked path
        # must judge A's cached reading against a whoami resolved THEN, or A's
        # balance and email stay on screen under B's session.
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[_IDENTITY_A, _IDENTITY_B])
        )
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape(SAMPLE_USAGE.encode()) as spawn:
            outcome = await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 0
        assert outcome == sessions_mod._SKIPPED_SCRAPE_PARKED
        assert sessions_mod._usage_cache == {"available": False}
        assert "a@corp.com" not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_parked_scrape_keeps_the_same_accounts_reading_stale(self, monkeypatch):
        # Control: the adjacent whoami still says A, so A's reading is dimmed, not dropped.
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_OTHER)
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=_IDENTITY_A))
        sessions_mod._usage_cache = dict(_CACHED_A)
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert sessions_mod._usage_cache["stale"] is True

    @pytest.mark.asyncio
    async def test_partial_api_fields_are_still_kept_when_parked(self, monkeypatch):
        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        self._api(
            monkeypatch,
            {"plan": "KIRO POWER", "resets": "2026-09-01"},
            sessions_mod.kiro_usage_api.AUTH_OTHER,
        )
        with self._scrape():
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("plan") == "KIRO POWER"
        assert sessions_mod._usage_cache.get("available") is False
        assert "reason" not in sessions_mod._usage_cache

    # ---- the non-regression guard ---------------------------------------

    @pytest.mark.asyncio
    async def test_the_scrape_still_runs_and_wins_without_a_readable_credential(self, monkeypatch):
        # An empty candidate list does NOT prove kiro-cli cannot authenticate: it
        # may authenticate from a store kiro_usage_api does not enumerate (see
        # _identity_matches_account). The scrape runs, and its number wins over
        # any unavailable marker.
        self._api(monkeypatch, None, sessions_mod.kiro_usage_api.AUTH_NO_CREDENTIAL)
        with self._scrape(SAMPLE_USAGE.encode()) as spawn:
            await sessions_mod._fetch_usage_bg()
        assert spawn.await_count == 1
        assert sessions_mod._usage_cache.get("credits_plan") == 10000.0
        assert "reason" not in sessions_mod._usage_cache

    # ---- the mapping itself ---------------------------------------------

    def test_only_the_two_auth_class_states_map_to_signin_required(self):
        api = sessions_mod.kiro_usage_api
        mapping = {
            state: sessions_mod._unavailable_reason(api.UsageResult(None, state))
            for state in (
                api.AUTH_OK,
                api.AUTH_NO_CREDENTIAL,
                api.AUTH_REJECTED,
                api.AUTH_OTHER,
            )
        }
        assert mapping == {
            api.AUTH_OK: None,
            api.AUTH_NO_CREDENTIAL: "signin_required",
            api.AUTH_REJECTED: "signin_required",
            api.AUTH_OTHER: None,
        }

    def test_an_unknown_state_claims_no_remedy(self):
        # Fail-safe direction: an unrecognised state must not invent a
        # re-authentication demand for a user whose sign-in is fine.
        api = sessions_mod.kiro_usage_api
        assert sessions_mod._unavailable_reason(api.UsageResult(None, "something-new")) is None


def _refresh_app():
    """A bare app carrying the usage GET and the refresh POST, as the router wires them."""
    from aiohttp import web

    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_get("/api/sessions/usage", sessions_mod.api_sessions_usage)
    app.router.add_post("/api/sessions/usage/refresh", sessions_mod.api_sessions_usage_refresh)
    return app


def _owner(verdict: bool):
    """Pin the shared owner predicate, to prove the route does not consult it."""
    return patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=verdict,
    )


class TestUsageRefreshRoute:
    """``POST /api/sessions/usage/refresh`` refreshes the credit reading on demand.

    It runs the same API-first, scrape-second refresh the timer runs. What is
    pinned here: any authenticated caller may use it; two refreshes never run at
    once; and a parked scrape is REPORTED after the API attempt, never used as an
    excuse to skip the API.
    """

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        # The API yields no plan -- the case where only the scrape can produce a
        # reading.
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api, "fetch_usage_limits", lambda **k: _api_result(None)
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        monkeypatch.setattr(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None))
        yield
        _reset_usage_globals()

    def _spawn_mock(self, stdout: bytes = SAMPLE_USAGE.encode()):
        return AsyncMock(return_value=_mock_proc(stdout))

    @pytest.mark.asyncio
    async def test_the_click_runs_the_scrape_and_replaces_the_cache(self):
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_cache = {"available": False}
        sessions_mod._usage_cache_ts = time.time()
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            # Same envelope as the GET, so the frontend reuses one parser.
            assert body["usage"]["credits_plan"] == 10000.0
            assert "skipped" not in body
            # The cache the pill polls now serves the refreshed reading.
            got = await (await c.get("/api/sessions/usage")).json()
        assert got["usage"]["credits_plan"] == 10000.0
        assert spawn.await_count == 1
        assert "/usage" in list(spawn.await_args.args)

    @pytest.mark.asyncio
    async def test_any_authenticated_caller_may_refresh(self):
        """No owner gate: the read costs nothing, so a dashboard token is enough."""
        from aiohttp.test_utils import TestClient, TestServer

        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with _owner(False), patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
        assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_the_unverified_kiro_guard_still_applies(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            sessions_mod,
            "reject_if_kiro_unverified",
            AsyncMock(return_value=web.json_response({"error": "kiro"}, status=503)),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 503
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_back_to_back_clicks_both_run(self):
        """No cool-down: the second click is a second refresh, not a refusal."""
        from aiohttp.test_utils import TestClient, TestServer

        assert not hasattr(sessions_mod, "_USAGE_REFRESH_COOLDOWN_SECS")
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                first = await c.post("/api/sessions/usage/refresh")
                second = await c.post("/api/sessions/usage/refresh")
            assert first.status == 200
            assert second.status == 200
        assert spawn.await_count == 2

    @pytest.mark.asyncio
    async def test_a_click_during_an_in_flight_refresh_is_refused_with_409(self):
        """Two concurrent clicks: ONE subprocess, the loser told a refresh is running."""
        from aiohttp.test_utils import TestClient, TestServer

        release = asyncio.Event()
        proc = MagicMock()

        async def _communicate():
            await release.wait()
            return SAMPLE_USAGE.encode(), b""

        proc.communicate = _communicate
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        spawn = AsyncMock(return_value=proc)
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                first = asyncio.ensure_future(c.post("/api/sessions/usage/refresh"))
                # Let the first click reach the scrape and block inside it.
                for _ in range(200):
                    if spawn.await_count:
                        break
                    await asyncio.sleep(0.01)
                assert spawn.await_count == 1
                second = await c.post("/api/sessions/usage/refresh")
                assert second.status == 409, await second.text()
                assert (await second.json())["code"] == "refresh_in_flight"
                release.set()
                resp = await first
                assert resp.status == 200, await resp.text()
                assert (await resp.json())["usage"]["credits_plan"] == 10000.0
        assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_a_timer_refresh_in_flight_also_refuses_the_click(self):
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_fetching = True
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 409
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_a_parked_scrape_is_reported_after_the_api_returns_no_plan(self):
        """Parked and no plan from the API: the cache comes back unchanged, marked."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        sessions_mod._usage_cache = {"available": False}
        sessions_mod._usage_cache_ts = time.time()
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["skipped"] == "scrape_parked"
        assert body["usage"] == {"available": False}
        assert 1 <= body["retry_after"] <= 3600
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_a_scrape_that_parks_during_this_refresh_is_reported_too(self):
        """The third miss lands INSIDE the click: the answer still says parked.

        Two misses are on the counter; this refresh's scrape prints no table
        and parks the scrape. The user pressed Refresh and got no reading, and
        must learn that pressing again is pointless until the park lifts --
        the same ``skipped`` + ``retry_after`` a pre-parked refresh reports.
        """
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        spawn = self._spawn_mock(b"not a usage block")
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert spawn.await_count == 1  # the scrape DID run: this is not a pre-check
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"
        assert 1 <= body["retry_after"] <= sessions_mod._USAGE_SCRAPE_BACKOFF_SECS
        assert body["usage"] == {"available": False}

    @pytest.mark.asyncio
    async def test_a_scrape_that_times_out_into_the_park_is_reported_too(self):
        """Same third miss, but the scrape hangs: the timeout path reports the park."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        proc = _mock_proc(b"")
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"
        assert 1 <= body["retry_after"] <= sessions_mod._USAGE_SCRAPE_BACKOFF_SECS
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_scrape_that_errors_into_the_park_is_reported_too(self):
        """Same third miss, the scrape spawn raises: the exception path reports the park."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 1
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("spawn"))):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert sessions_mod._scrape_in_backoff()
        assert body["skipped"] == "scrape_parked"

    @pytest.mark.asyncio
    async def test_a_miss_short_of_the_park_reports_no_skip(self):
        """Control: a failed scrape that does NOT park is a plain failed refresh."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD - 2
        spawn = self._spawn_mock(b"not a usage block")
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert not sessions_mod._scrape_in_backoff()
        assert "skipped" not in body
        assert body["usage"] == {"available": False}

    @pytest.mark.asyncio
    async def test_a_parked_scrape_does_not_block_a_reading_the_api_returns(self, monkeypatch):
        """The park is about the scrape only: the API attempt still runs first."""
        from aiohttp.test_utils import TestClient, TestServer

        sessions_mod._usage_scrape_backoff_until = time.monotonic() + 3600
        # A proven account: the API reading publishes only when whoami names one.
        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result({"credits_plan": 500.0, "credits_used": 12.0}),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["usage"]["credits_plan"] == 500.0
        assert "skipped" not in body
        assert spawn.await_count == 0

    @pytest.mark.asyncio
    async def test_the_api_is_still_preferred_over_the_scrape(self, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A)))
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: _api_result({"credits_plan": 500.0, "credits_used": 12.0}),
        )
        spawn = self._spawn_mock()
        async with TestClient(TestServer(_refresh_app())) as c:
            with patch("asyncio.create_subprocess_exec", spawn):
                resp = await c.post("/api/sessions/usage/refresh")
            assert (await resp.json())["usage"]["credits_plan"] == 500.0
        assert spawn.await_count == 0

    def test_the_route_is_registered_next_to_the_get(self):
        from aiohttp import web

        from kiro_crew.dashboard.routes.system import register as register_system_routes

        app = web.Application()
        register_system_routes(app)
        routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
        assert ("POST", "/api/sessions/usage/refresh") in routes
        assert ("GET", "/api/sessions/usage") in routes
