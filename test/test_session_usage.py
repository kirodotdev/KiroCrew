"""Tests for the credit-usage helpers in
kiro_crew.dashboard.handlers.sessions: _parse_usage, _redact_strings, and the
_fetch_usage_bg gating/redaction logic.

This file pins the usage parsers, the transient-failure cache, string redaction,
the refresh's text-scrape path with its environment scrub and child reaping, the
refresh's overall deadline, the API-primary path, the API-key fail-fast, whoami
parsing, the guard that keeps a scrape from replacing a richer API value, and the
identity coupling rule. The per-refresh account proof is in
test_session_usage_account_proof.py; when the scrape runs and parks, the
unavailable reason, the children's no-window flag and the scrape's engine pin are
in test_session_usage_scrape.py; the poll interval and the refresh route are in
test_session_usage_refresh_routes.py. session_usage_helpers.py holds what they
share.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_usage_helpers import (
    _CACHED_A,
    _IDENTITY_A,
    _IDENTITY_B,
    SAMPLE_USAGE,
    _api_result,
    _mock_proc,
    _reset_usage_globals,
)

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions import (
    _normalize_text_usage,
    _parse_usage,
    _redact_strings,
    _text_scrape_regresses_api_value,
)
from kiro_crew.testing.wait import finished

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
        with (
            patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: (s + "_U", 0)),
            patch.object(sessions_mod, "redact_credentials", lambda s: (s + "_C", 0)),
        ):
            assert _redact_strings("x") == "x_U_C"

    def test_recurses_into_dicts_and_lists(self):
        with (
            patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: (s.upper(), 0)),
            patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)),
        ):
            out = _redact_strings({"a": "x", "b": ["y", {"c": "z"}]})
        assert out == {"a": "X", "b": ["Y", {"c": "Z"}]}

    def test_non_string_leaves_pass_through(self):
        assert _redact_strings(42) == 42
        assert _redact_strings(3.5) == 3.5
        assert _redact_strings(None) is None


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
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        # Force the text-scrape fallback path by default (the real API client
        # would otherwise read this host's live token). API-primary behavior is
        # covered explicitly in TestFetchUsageBgApi.
        with patch.object(
            sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=_api_result(None)
        ):
            yield
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_no_kiro_bin_caches_unavailable(self):
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value=None):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_parseable_usage_is_cached(self):
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
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
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(b"no usage block here")),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_string_fields_redacted_before_cache(self):
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
            patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)),
            patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)),
        ):
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
        whoami = AsyncMock(
            return_value={
                "email": "carol@amazon.com",
                "start_url": "https://amzn.awsapps.com/start",
            }
        )
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", whoami),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
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
        whoami = AsyncMock(
            return_value={
                "email": "carol@amazon.com",
                "start_url": "https://amzn.awsapps.com/start",
            }
        )
        # SAMPLE_USAGE carries a reset date from a different cycle than the
        # cached reading.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", whoami),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
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
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", whoami),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        # B's fresh scrape replaced A's value (3164 = covered 3044 + overage 120).
        assert sessions_mod._usage_cache["credits_used"] == 3164.0
        assert sessions_mod._usage_cache["source"] == "text"
        assert sessions_mod._usage_cache.get("email") == "bob@amazon.com"

    @pytest.mark.asyncio
    async def test_reentrancy_guard_skips_when_already_fetching(self):
        sessions_mod._usage_fetching = True
        cache_before = sessions_mod._usage_cache
        with patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn") as resolve:
            assert await sessions_mod._fetch_usage_bg() is None
        resolve.assert_not_called()
        assert sessions_mod._usage_cache is cache_before
        assert sessions_mod._usage_fetching is True, "the in-flight refresh still owns the flag"

    @pytest.mark.asyncio
    async def test_timeout_caches_unavailable_and_reaps(self):
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        # whoami is stubbed so `proc` stands in for the /usage scrape ALONE.
        # _fetch_usage_bg resolves the identity first (it anchors credential
        # selection), which is a second spawn; sharing one mock across both would
        # make this assert on whoami's reap instead of the scrape's.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        proc.kill.assert_called_once()
        # kill_and_reap drains the pipes with a second communicate() (FDs closed).
        assert proc.communicate.await_count == 2  # reaped on the timeout path
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

        async def communicate():
            events.append("communicate")
            raise asyncio.TimeoutError

        proc.communicate = communicate
        proc.kill = MagicMock(side_effect=lambda: events.append("kill"))

        async def whoami(_bin):
            events.append("whoami")
            return {}

        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            started = time.monotonic()
            await sessions_mod._fetch_usage_bg()
            elapsed = time.monotonic() - started
        # top whoami, the pre-spawn whoami, the scrape, the reap (kill + draining
        # communicate), then the handler's whoami.
        assert events == [
            "whoami",
            "whoami",
            "communicate",
            "kill",
            "communicate",
            "whoami",
        ], events
        proc.kill.assert_called_once()  # the finally does not reap a second time
        assert elapsed < 5  # the (mocked) reap is bounded; nothing waited on the child
        assert sessions_mod._usage_cache == {"available": False}
        assert sessions_mod._usage_fetching is False

    @pytest.mark.asyncio
    async def test_exception_reaps_the_scrape_before_the_adjacent_whoami(self):
        events: list[str] = []
        proc = _mock_proc(b"")
        proc.returncode = None

        async def communicate():
            events.append("communicate")
            raise RuntimeError("pipe broke")

        proc.communicate = communicate
        proc.kill = MagicMock(side_effect=lambda: events.append("kill"))

        async def whoami(_bin):
            events.append("whoami")
            return {}

        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(side_effect=whoami)),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert events == [
            "whoami",
            "whoami",
            "communicate",
            "kill",
            "communicate",
            "whoami",
        ], events
        proc.kill.assert_called_once()
        assert sessions_mod._usage_fetching is False

    @pytest.mark.asyncio
    async def test_generic_exception_caches_unavailable_and_reaps(self):
        proc = _mock_proc(b"")
        proc.returncode = None  # still running
        proc.communicate = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache == {"available": False}
        proc.kill.assert_called_once()
        assert proc.communicate.await_count == 2  # reaped (FDs closed) on the error path


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
        monkeypatch.setattr(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 0.2, raising=False)
        released = threading.Event()

        def _hang(*_args, **_kwargs):
            # Blocks like a wedged TLS handshake or a DNS lookup with no
            # resolver: urlopen's own timeout does not cover either.
            released.wait(30)
            return _api_result(None)

        try:
            with (
                patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
                patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
                patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", _hang),
            ):
                # The outer bound is the assertion: unfixed, _fetch_usage_bg only
                # returns once the 30 s hang releases, so this must fail first.
                await finished(sessions_mod._fetch_usage_bg(), "the usage fetch", timeout=10)
        finally:
            released.set()

        assert sessions_mod._usage_fetching is False, "in-flight guard was left set"
        # The pill must resolve rather than spin: no credit plan was obtained, so
        # usage is reported unavailable.
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache_ts > 0

    @pytest.mark.asyncio
    async def test_refresh_after_a_hang_can_still_succeed(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 0.2, raising=False)
        released = threading.Event()

        def _hang(*_args, **_kwargs):
            released.wait(30)
            return _api_result(None)

        try:
            with (
                patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
                patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value={})),
                patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits", _hang),
            ):
                # Bounded below the 30 s hang: unfixed, the fetch would wait it out.
                await finished(sessions_mod._fetch_usage_bg(), "the hung fetch", timeout=10)
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
        monkeypatch.setattr(sessions_mod, "_USAGE_FETCH_DEADLINE_SECS", 5.0, raising=False)
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "me@corp.com", "_profile_arn": arn}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result({**api_dict, "_profile_arn": arn}),
            ),
        ):
            await finished(sessions_mod._fetch_usage_bg(), "the successful usage fetch")

        assert sessions_mod._usage_cache.get("credits_plan") == 100.0


class TestNormalizeTextUsage:
    def test_maps_overage_and_total(self):
        # Text parse: credits_used is the OVERAGE field, covered/plan the in-plan.
        parsed = {
            "credits_used": 120.0,
            "credits_covered": 3044.0,
            "credits_plan": 10000.0,
            "plan": "KIRO POWER",
            "raw": "x",
        }
        out = _normalize_text_usage(parsed)
        assert out["credits_used"] == 3164.0  # total = covered + overage
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
        assert (
            _text_scrape_regresses_api_value(prev, {"credits_used": 3164.0, **self.CYCLE}, self.ID)
            is False
        )
        assert (
            _text_scrape_regresses_api_value(prev, {"credits_used": 9000.0, **self.CYCLE}, self.ID)
            is False
        )

    def test_missing_reset_date_never_preserved(self):
        # GetUsageLimits omitting nextDateReset -> cached value has no `resets`;
        # a rollover must not be pinned, so preserve only with both dates present.
        prev_no_reset = {"credits_used": 41336.0, "source": "api", **self.ID}
        assert (
            _text_scrape_regresses_api_value(
                prev_no_reset, {"credits_used": 100.0, **self.CYCLE}, self.ID
            )
            is False
        )
        prev = {"credits_used": 41336.0, "source": "api", **self.ID, **self.CYCLE}
        assert _text_scrape_regresses_api_value(prev, {"credits_used": 100.0}, self.ID) is False

    @pytest.mark.parametrize(
        "prev_email, prev_start_url, email, start_url",
        [
            # Account switch A->B: cached A value (higher usage) must NOT be kept
            # when the current identity is B — otherwise A's usage + email leak.
            pytest.param(
                "alice@amazon.com",
                "https://a.awsapps.com/start",
                "bob@amazon.com",
                "https://b.awsapps.com/start",
                id="different_identity_never_preserved",
            ),
            # Same human email across two Identity Center orgs (different start_url)
            # is NOT the same account — must not preserve.
            pytest.param(
                "carol@amazon.com",
                "https://orgA.awsapps.com/start",
                "carol@amazon.com",
                "https://orgB.awsapps.com/start",
                id="same_email_different_org_never_preserved",
            ),
        ],
    )
    def test_a_different_identity_is_never_preserved(
        self, prev_email, prev_start_url, email, start_url
    ):
        prev = {
            "credits_used": 41336.0,
            "source": "api",
            "email": prev_email,
            "start_url": prev_start_url,
        }
        new = {"credits_used": 100.0}
        assert (
            _text_scrape_regresses_api_value(prev, new, {"email": email, "start_url": start_url})
            is False
        )

    @pytest.mark.parametrize(
        "scrape_credits, resets, expected",
        [
            # New billing cycle (reset date changed): the lower scrape is a
            # legitimate rollover, not the overage-blind cap — let it win.
            pytest.param(200.0, "2026-09-01", False, id="different_reset_date_allows_scrape"),
            pytest.param(3164.0, "2026-08-01", True, id="same_reset_date_still_blocks"),
        ],
    )
    def test_reset_date_decides_whether_the_scrape_wins(self, scrape_credits, resets, expected):
        prev = {"credits_used": 41336.0, "source": "api", "resets": "2026-08-01", **self.ID}
        new = {"credits_used": scrape_credits, "resets": resets}
        assert _text_scrape_regresses_api_value(prev, new, self.ID) is expected

    def test_missing_email_on_either_side_never_preserved(self):
        prev_no_email = {"credits_used": 41336.0, "source": "api"}
        assert (
            _text_scrape_regresses_api_value(prev_no_email, {"credits_used": 1.0}, self.ID) is False
        )
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
        assert (
            _text_scrape_regresses_api_value({"available": False}, {"credits_used": 1.0}, self.ID)
            is False
        )

    def test_non_numeric_usage_allows_scrape(self):
        prev = {"credits_used": None, "source": "api", **self.ID, **self.CYCLE}
        assert (
            _text_scrape_regresses_api_value(prev, {"credits_used": 1.0, **self.CYCLE}, self.ID)
            is False
        )
        prev2 = {"credits_used": 5.0, "source": "api", **self.ID, **self.CYCLE}
        assert (
            _text_scrape_regresses_api_value(prev2, {"credits_used": None, **self.CYCLE}, self.ID)
            is False
        )


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
            "credits_used": 29527.0,
            "credits_plan": 10000.0,
            "credits_overage": 19527.0,
            "credits_covered": 10000.0,
            "percentage": 295.3,
            "cost_usd": 781.08,
            "plan": "KIRO POWER",
            "source": "api",
        }
        spawn = AsyncMock()
        # The API path now requires a PROVEN profile ARN (no ARN -> the scrape),
        # so whoami is stubbed with one rather than left to the bare spawn mock.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(
                    return_value={
                        "email": "me@corp.com",
                        "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
                    }
                ),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
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
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))),
            patch.object(
                sessions_mod.kiro_usage_api, "fetch_usage_limits", return_value=_api_result(None)
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode())),
            ),
        ):
            await sessions_mod._fetch_usage_bg()
        # Fallback path normalizes: credits_used becomes the TOTAL, source=text.
        assert sessions_mod._usage_cache["credits_plan"] == 10000.0
        assert sessions_mod._usage_cache["credits_used"] == 3164.0
        assert sessions_mod._usage_cache["source"] == "text"

    @pytest.mark.asyncio
    async def test_api_string_fields_redacted_before_cache(self):
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "plan": "SENSITIVE"}
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(
                    return_value={"_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"}
                ),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
            patch.object(sessions_mod, "redact_credentials", lambda s: (s, 0)),
            patch.object(sessions_mod, "redact_exfiltration_urls", lambda s: ("REDACTED", 0)),
        ):
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
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={"email": "a@b.com", "account_type": "ApiKey"}),
            ),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch,
        ):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        assert sessions_mod._usage_cache == {"available": False, "reason": "api_key_auth"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported", ["apikey", "APIKEY", " ApiKey ", "API_KEY", "Api-Key"])
    async def test_account_type_comparison_tolerates_respelling(self, reported):
        # The enum spelling is upstream's to change; a drift must degrade to the
        # old slow path at worst, and these spellings must all still fail fast.
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod, "_fetch_whoami", AsyncMock(return_value={"account_type": reported})
            ),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch,
        ):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        assert sessions_mod._usage_cache == {"available": False, "reason": "api_key_auth"}

    @pytest.mark.asyncio
    async def test_api_key_auth_never_spawns_the_scrape(self, monkeypatch):
        # An API-key account must not reach the text scrape: the harm being
        # prevented, asserted directly.
        spawn = AsyncMock()
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod, "_fetch_whoami", AsyncMock(return_value={"account_type": "ApiKey"})
            ),
            patch.object(sessions_mod.kiro_usage_api, "fetch_usage_limits") as fetch,
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await sessions_mod._fetch_usage_bg()
        fetch.assert_not_called()
        spawn.assert_not_called()
        assert sessions_mod._usage_cache == {"available": False, "reason": "api_key_auth"}
        assert sessions_mod._usage_fetching is False

    @pytest.mark.asyncio
    async def test_sso_account_types_still_reach_the_usage_api(self):
        # Negative control pinning the branch's condition: a non-ApiKey account
        # takes the normal API path. Removing the fail-fast branch flips the
        # short-circuit tests red; widening its match flips this one red.
        api_dict = {"credits_used": 1.0, "credits_plan": 10.0, "source": "api"}
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(
                sessions_mod,
                "_fetch_whoami",
                AsyncMock(return_value={**_IDENTITY_A, "account_type": "IamIdentityCenter"}),
            ),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ) as fetch,
        ):
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
        with (
            patch.object(sessions_mod, "wrap_argv", return_value=(["/usr/bin/kiro-cli"], None)),
            patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
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

    @pytest.mark.parametrize(
        "stdout",
        [
            pytest.param(
                b'{"email":{"nested":1},"accountType":null}', id="non_string_values_dropped"
            ),
            pytest.param(b"Not logged in\n", id="no_json_returns_empty"),
            pytest.param(b'{"email":"a@b.com"', id="unterminated_json_returns_empty"),
        ],
    )
    def test_unusable_whoami_output_returns_empty(self, stdout):
        assert self._run(stdout) == {}

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

    @pytest.mark.parametrize(
        "expected, email, profile_arn",
        [
            pytest.param(
                True,
                "a@b.com",
                "arn:aws:codewhisperer:us-east-1:1:profile/A",
                id="matching_arns_are_coupled",
            ),
            # The exact misattribution the reviewer flagged: API billed account A,
            # whoami describes account B.
            pytest.param(
                False,
                "b@b.com",
                "arn:aws:codewhisperer:us-east-1:2:profile/B",
                id="differing_arns_are_refused",
            ),
        ],
    )
    def test_identity_account_coupling(self, expected, email, profile_arn):
        assert (
            sessions_mod._identity_matches_account(
                "arn:aws:codewhisperer:us-east-1:1:profile/A",
                {"email": email, "_profile_arn": profile_arn},
            )
            is expected
        )

    def test_no_arns_is_never_coupled(self):
        # A lone READABLE credential is not proof: kiro-cli may authenticate from
        # a store this module does not enumerate, so whoami's account cannot be
        # tied to the billed one. Individual / Builder ID accounts (no profile
        # ARN) therefore show no identity rather than a possibly-foreign one.
        assert sessions_mod._identity_matches_account(None, {"email": "solo@b.com"}) is False

    def test_one_sided_arn_is_refused(self):
        assert (
            sessions_mod._identity_matches_account(
                "arn:aws:codewhisperer:us-east-1:1:profile/A", {"email": "x@b.com"}
            )
            is False
        )
        assert (
            sessions_mod._identity_matches_account(
                None,
                {"email": "x@b.com", "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A"},
            )
            is False
        )

    def test_whoami_extracts_profile_arn_from_trailing_block(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(
            return_value=(
                b'{"accountType":"IamIdentityCenter","email":"me@corp.com"}\n\n'
                b"Profile:\nKiroProfile-us-east-1\n"
                b"arn:aws:codewhisperer:us-east-1:713669222412:profile/7KHC74QYC9PQ\n",
                b"",
            )
        )
        proc.returncode = 0
        # An absolute path, as the real wrap_argv returns: the spawn shim execs
        # without a PATH search, so a bare name is not a realistic fixture.
        with (
            patch.object(sessions_mod, "wrap_argv", return_value=(["/usr/bin/kiro-cli"], None)),
            patch.object(sessions_mod, "cgroup_scope_argv", side_effect=lambda a: a),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            out = asyncio.run(sessions_mod._fetch_whoami("kiro-cli"))
        assert out["email"] == "me@corp.com"
        assert out["_profile_arn"].endswith("profile/7KHC74QYC9PQ")

    @pytest.mark.asyncio
    async def test_private_coupling_keys_never_reach_the_cache(self):
        _reset_usage_globals()
        api_dict = {
            "credits_used": 100.0,
            "credits_plan": 10.0,
            "source": "api",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        identity = {
            "email": "me@corp.com",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=identity)),
        ):
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert cache["email"] == "me@corp.com"  # coupled -> shown
        assert "_profile_arn" not in cache  # private, never served
        _reset_usage_globals()

    @pytest.mark.asyncio
    async def test_mismatched_identity_is_not_cached(self):
        _reset_usage_globals()
        api_dict = {
            "credits_used": 100.0,
            "credits_plan": 10.0,
            "source": "api",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/A",
        }
        identity = {
            "email": "other@corp.com",
            "_profile_arn": "arn:aws:codewhisperer:us-east-1:2:profile/B",
        }
        with (
            patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
            patch.object(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None)),
            patch.object(
                sessions_mod.kiro_usage_api,
                "fetch_usage_limits",
                return_value=_api_result(api_dict),
            ),
            patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=identity)),
        ):
            await sessions_mod._fetch_usage_bg()
        cache = sessions_mod._usage_cache
        assert "email" not in cache, "a foreign identity must not ride on these credits"
        assert cache["credits_used"] == 100.0
        _reset_usage_globals()
