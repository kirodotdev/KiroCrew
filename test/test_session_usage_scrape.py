"""The credit refresh falls back to the ``/usage`` scrape, which parks after misses.

The scrape runs whenever the API path returns no plan, with no config key in front
of it, and parks after repeated failures; a parked refresh keeps the API's partial
fields and a prior reading only for the same account. When a refresh ends with
no reading, its ``reason`` names a lapsed sign-in only for an auth-class API
verdict, and a working scrape outranks that verdict. The kiro-cli children the
refresh spawns (whoami and the scrape) carry the no-window creation flag, and the
scrape pins the ``v2`` agent engine only on a release verified to accept it. The
whoami bracket around the scrape is pinned in test_session_usage_account_proof.py,
and the scrape's environment scrub and child reaping in test_session_usage.py.
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
from kiro_crew.testing.wait import finished


class TestScrapeIsTheAutomaticFallback:
    """The `/usage` text scrape runs whenever the API path returns no plan.

    `/usage` is a kiro-cli slash command answered locally from the same free
    GetUsageLimits call, so there is no config key in front of it. The only gate
    left is the back-off that parks a scrape whose output stops parsing, and
    everything the parked path caches must stay identity-safe.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test of the class calls monkeypatch.undo()
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
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        yield
        _reset_usage_globals()

    def _spawn_mock(self, stdout: bytes = b""):
        return AsyncMock(return_value=_mock_proc(stdout))

    def _park(self):
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
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
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test of the class calls monkeypatch.undo()
    def _reset(self, monkeypatch):
        _reset_usage_globals()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )
        monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
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
            # The bound is the assertion: the patched 0.2 s deadline must end the
            # refresh long before the hung API's own 30 s release would.
            await finished(
                sessions_mod._fetch_usage_bg(), "the deadline ending the refresh", timeout=10
            )
        finally:
            released.set()
        assert sessions_mod._usage_cache == {"available": False}

    # ---- the scrape was parked --------------------------------------------

    @pytest.mark.asyncio
    async def test_a_parked_scrape_carries_the_api_verdict(self, monkeypatch):
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing real-clock read parking the scrape an hour ahead of the product's own monotonic clock, moved verbatim; tracked for a de-flake pass
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


class TestSpawnSuppressesConsoleWindow:
    """The identity/usage helper spawns must thread ``creationflags`` carrying
    ``_SUBPROCESS_NO_WINDOW`` through ``spawn_supervised_oneshot``.

    On Windows the gateway runs without a console, so a console-subsystem child
    spawned without ``CREATE_NO_WINDOW`` gets a fresh console allocated that
    flashes on screen ~every 30s. On POSIX the constant is ``0``; the kwarg is
    forwarded untouched to ``asyncio.create_subprocess_exec`` by the spawn
    shim, so the assertion still pins that the call threads the flag through
    rather than dropping it.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture, moved verbatim; no test of the class calls monkeypatch.undo()
    def _bypass_sandbox_wrap(self, monkeypatch):
        # wrap_argv cold-probes the OS sandbox and can raise on a host with no
        # backend; the creationflags on the spawn is what these tests assert on.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )

    @pytest.mark.asyncio
    async def test_whoami_spawn_sets_no_window(self):
        spawn = AsyncMock(return_value=_mock_proc(b"{}"))
        with patch("asyncio.create_subprocess_exec", spawn):
            await sessions_mod._fetch_whoami("/bin/kiro")
        assert spawn.await_args is not None, "whoami did not spawn"
        assert spawn.await_args.kwargs.get("creationflags") == sessions_mod._SUBPROCESS_NO_WINDOW

    @pytest.mark.asyncio
    async def test_usage_scrape_spawn_sets_no_window(self, monkeypatch):
        _reset_usage_globals()
        # A proven, unchanging account so the scrape branch runs and publishes;
        # force the text-scrape fallback by making the API read return nothing.
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_IDENTITY_A))
        )
        spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
        try:
            with (
                patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
                patch.object(
                    sessions_mod.kiro_usage_api,
                    "fetch_usage_limits",
                    return_value=_api_result(None),
                ),
                patch("asyncio.create_subprocess_exec", spawn),
            ):
                await sessions_mod._fetch_usage_bg()
        finally:
            _reset_usage_globals()
        assert spawn.await_args is not None, "usage scrape did not spawn"
        assert spawn.await_args.kwargs.get("creationflags") == sessions_mod._SUBPROCESS_NO_WINDOW


class TestUsageScrapePinsTheEngine:
    """The ``/usage`` scrape names its agent engine instead of inheriting one.

    A kiro-cli whose global ``chat.agentEngine`` is ``v3`` sends ``/usage`` to the
    model as a prompt: it spends a model turn and prints no usage table. The
    scrape pins ``v2``, where ``/usage`` is a local slash command, on every
    release verified to accept the selector. Below that floor, or when the
    version cannot be read, the argv stays as it is, since an unknown flag
    fails the whole command.
    """

    @staticmethod
    def _no_sandbox(monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.sessions.wrap_argv",
            lambda argv, **k: (list(argv), None),
        )

    @staticmethod
    def _pin(monkeypatch, pinned):
        monkeypatch.setattr(sessions_mod, "pin_kiro_cli", lambda: (pinned, False))

    def _argv(self, monkeypatch, version):
        self._no_sandbox(monkeypatch)
        self._pin(monkeypatch, "/bin/kiro")
        monkeypatch.setattr(sessions_mod, "kiro_cli_version_at", lambda binary: version)
        argv, _cleanup = sessions_mod._wrap_argv_usage_scrape("/bin/kiro")
        return argv

    def test_a_verified_release_pins_the_v2_engine(self, monkeypatch):
        argv = self._argv(monkeypatch, sessions_mod.USAGE_SCRAPE_ENGINE_PIN_MIN_VERSION)
        assert argv == [
            "/bin/kiro",
            "chat",
            "--no-interactive",
            "--agent-engine",
            "v2",
            "--agent",
            "kirocrew-lite",
            "/usage",
        ]

    def test_a_newer_release_pins_the_v2_engine(self, monkeypatch):
        argv = self._argv(monkeypatch, (3, 0, 0))
        assert argv[argv.index("--agent-engine") + 1] == "v2"
        assert argv[-1] == "/usage"

    @pytest.mark.parametrize("version", [None, (2, 27, 9)])
    def test_an_unverified_or_unknown_release_keeps_the_plain_argv(self, monkeypatch, version):
        argv = self._argv(monkeypatch, version)
        assert argv == [
            "/bin/kiro",
            "chat",
            "--no-interactive",
            "--agent",
            "kirocrew-lite",
            "/usage",
        ]

    def test_the_version_probe_reads_the_spawned_binary(self, monkeypatch):
        seen: list[str] = []

        def probe(binary):
            seen.append(binary)
            return None

        self._no_sandbox(monkeypatch)
        self._pin(monkeypatch, "/opt/kiro/bin/kiro-cli")
        monkeypatch.setattr(sessions_mod, "kiro_cli_version_at", probe)
        sessions_mod._wrap_argv_usage_scrape("/opt/kiro/bin/kiro-cli")
        assert seen == ["/opt/kiro/bin/kiro-cli"]

    @pytest.mark.parametrize("pinned", [None, "/opt/kiro/bin/kiro-cli"])
    def test_a_binary_that_is_not_the_pin_is_never_probed(self, monkeypatch, pinned):
        # A kiro-cli found only on the inherited PATH may sit in an
        # agent-writable directory; the version probe runs outside the
        # sandbox, so such a binary keeps the plain argv and is never run.
        seen: list[str] = []

        def probe(binary):
            seen.append(binary)
            return (3, 0, 0)

        self._no_sandbox(monkeypatch)
        self._pin(monkeypatch, pinned)
        monkeypatch.setattr(sessions_mod, "kiro_cli_version_at", probe)
        argv, _cleanup = sessions_mod._wrap_argv_usage_scrape("/tmp/venv/bin/kiro-cli")
        assert seen == []
        assert "--agent-engine" not in argv
