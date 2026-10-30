"""A cached credit reading is kept only for the same IdC user.

Two accounts on one IAM Identity Center org can share the email and the
``start_url`` that :func:`_same_identity` compares. When the refresh fails right
after a switch A->B between such accounts, the cached reading must not stay on
screen as B's. The IdC ``userId`` kiro-cli's own store carries tells them apart:
``fetch_usage_limits`` reports it on its result, the reading remembers the one it
was published for, and a mismatch on both sides means "not the same account".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_usage_helpers import SAMPLE_USAGE, _mock_proc, _reset_usage_globals

import kiro_crew.dashboard.handlers.kiro_usage_api as api
import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions_parts.usage_scrape import (
    _same_identity,
    _text_scrape_regresses_api_value,
)

_ARN = "arn:aws:codewhisperer:us-east-1:1:profile/SHARED"
# One IdC org, one shared profile: A and B differ only in their IdC userId.
_IDENTITY = {"email": "dev@corp.com", "start_url": "https://corp.awsapps.com/start"}
_WHOAMI = {**_IDENTITY, "_profile_arn": _ARN}
_UID_A = "d-org.aaaa"
_UID_B = "d-org.bbbb"
_READING_A = {
    "credits_used": 9000.0,
    "credits_plan": 10000.0,
    "source": "api",
    # SAMPLE_USAGE's cycle, so the overage-blind-scrape guard is in play.
    "resets": "2026-07-01",
    "_profile_arn": _ARN,
}


class TestSameIdentityComparesUserId:
    def test_different_user_ids_are_not_the_same_identity(self):
        assert not _same_identity(_IDENTITY, _IDENTITY, cached_user_id=_UID_A, user_id=_UID_B)

    def test_equal_user_ids_are_the_same_identity(self):
        assert _same_identity(_IDENTITY, _IDENTITY, cached_user_id=_UID_A, user_id=_UID_A)

    @pytest.mark.parametrize(
        "cached_uid, uid",
        [(None, _UID_B), (_UID_A, None), (None, None), ("", _UID_B), (_UID_A, "")],
    )
    def test_a_side_without_a_user_id_falls_back_to_email_and_start_url(self, cached_uid, uid):
        assert _same_identity(_IDENTITY, _IDENTITY, cached_user_id=cached_uid, user_id=uid)

    def test_a_matching_user_id_never_rescues_a_different_email(self):
        other = {**_IDENTITY, "email": "other@corp.com"}
        assert not _same_identity(_IDENTITY, other, cached_user_id=_UID_A, user_id=_UID_A)

    def test_the_text_scrape_guard_does_not_keep_another_users_api_value(self):
        # The overage-blind scrape caps at the plan; A's API value is higher.
        new = {"credits_used": 5000.0, "credits_plan": 10000.0, "resets": "2026-07-01"}
        prev = {**_READING_A, **_IDENTITY}
        assert not _text_scrape_regresses_api_value(
            prev, new, _IDENTITY, prev_user_id=_UID_A, user_id=_UID_B
        )
        assert _text_scrape_regresses_api_value(
            prev, new, _IDENTITY, prev_user_id=_UID_A, user_id=_UID_A
        )


class TestFetchUsageLimitsReportsTheSignedInUserId:
    """The result carries the userId of kiro-cli's own store token, success or not."""

    @staticmethod
    def _sqlite(uid):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        def fake(db, _now):
            return ("signed-in", expiry, uid) if db == api._CLI_SQLITE_DBS[0] else None

        return fake

    @staticmethod
    def _post(status):
        def fake(token, target, payload, **_kwargs):
            resp = MagicMock()
            if target == api._TARGET_LIST_PROFILES:
                resp.status_code = 200
                resp.json.return_value = {"profiles": [{"arn": _ARN}]}
            else:
                resp.status_code = status
                resp.json.return_value = {
                    "usageBreakdownList": [
                        {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}
                    ]
                }
            return resp

        return fake

    @pytest.mark.parametrize("status, has_usage", [(200, True), (500, False)])
    def test_user_id_rides_the_result(self, status, has_usage):
        with (
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(api, "_token_from_sqlite", side_effect=self._sqlite(_UID_B)),
            patch.object(api, "_post", side_effect=self._post(status)),
        ):
            result = api.fetch_usage_limits(expected_arn=_ARN)
        assert (result.usage is not None) is has_usage
        assert result.user_id == _UID_B

    def test_no_user_id_when_the_store_blob_carries_none(self):
        with (
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(api, "_token_from_sqlite", side_effect=self._sqlite(None)),
            patch.object(api, "_post", side_effect=self._post(500)),
        ):
            assert api.fetch_usage_limits(expected_arn=_ARN).user_id is None

    def test_no_user_id_without_a_candidate(self):
        with (
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(api, "_token_from_sqlite", return_value=None),
        ):
            assert api.fetch_usage_limits(expected_arn=_ARN).user_id is None


class TestSignedInUserId:
    """The re-read picks the userId exactly as ``fetch_usage_limits`` anchors on it."""

    @staticmethod
    def _stores(json_hit, sqlite_hit):
        return (
            patch.object(
                api,
                "_token_from_json",
                side_effect=lambda rid, _n: (
                    json_hit if rid == api._JSON_TOKEN_READ_IDS[0] else None
                ),
            ),
            patch.object(
                api,
                "_token_from_sqlite",
                side_effect=lambda db, _n: sqlite_hit if db == api._CLI_SQLITE_DBS[0] else None,
            ),
        )

    def test_a_json_user_id_merged_onto_the_cli_token_agrees_with_the_usage_read(self):
        # One token in the JSON cache with a userId and in kiro-cli's store
        # without one: both reads report the merged userId, so no switch is seen.
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        json_store, sqlite_store = self._stores(("tok", expiry, _UID_A), ("tok", expiry, None))
        with json_store, sqlite_store, patch.object(api, "_post", side_effect=_post_500):
            usage_read = api.fetch_usage_limits(expected_arn=_ARN).user_id
            reread = api.signed_in_user_id()
        assert usage_read == reread == _UID_A

    def test_only_a_cli_store_token_anchors(self):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        json_store, sqlite_store = self._stores(("other", expiry, _UID_B), None)
        with json_store, sqlite_store:
            assert api.signed_in_user_id() is None

    def test_no_token_is_no_user_id(self):
        json_store, sqlite_store = self._stores(None, None)
        with json_store, sqlite_store:
            assert api.signed_in_user_id() is None

    def test_a_read_error_is_no_user_id(self):
        with patch.object(api, "_candidate_tokens", side_effect=OSError("x")):
            assert api.signed_in_user_id() is None


def _post_500(token, target, payload, **_kwargs):
    resp = MagicMock()
    resp.status_code = 200 if target == api._TARGET_LIST_PROFILES else 500
    resp.json.return_value = {"profiles": [{"arn": _ARN}]}
    return resp


class TestAFailedRefreshAfterASwitchOnOneOrg:
    """A->B on one org (same email, start_url and ARN), then B's refresh fails."""

    @pytest.fixture(autouse=True)
    def _isolate(self, _floor_monkeypatch):
        _reset_usage_globals()
        _floor_monkeypatch.setattr(sessions_mod, "wrap_argv", lambda argv, **k: (list(argv), None))
        _floor_monkeypatch.setattr(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="/bin/kiro")
        )
        _floor_monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(return_value=dict(_WHOAMI))
        )
        # The userId kiro-cli's store holds when a decision re-reads it; never
        # the real store.
        self.store_uid: str | None = None
        _floor_monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "signed_in_user_id",
            lambda: self.store_uid,
            raising=False,
        )
        yield
        _reset_usage_globals()

    def _api(self, monkeypatch, usage, uid, *, store_uid="same"):
        auth = api.AUTH_OK if usage is not None else api.AUTH_OTHER
        monkeypatch.setattr(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            lambda **k: api.UsageResult(usage, auth, uid),
        )
        self.store_uid = uid if store_uid == "same" else store_uid

    def _scrape(self, stdout: bytes):
        return patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_mock_proc(stdout)))

    async def _publish_a(self, monkeypatch):
        self._api(monkeypatch, dict(_READING_A), _UID_A)
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_used"] == 9000.0
        assert sessions_mod._usage_cache["email"] == _IDENTITY["email"]

    @pytest.mark.asyncio
    async def test_a_failed_scrape_does_not_keep_as_reading(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        with self._scrape(b"not a usage block"):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert "credits_used" not in sessions_mod._usage_cache
        assert _IDENTITY["email"] not in str(sessions_mod._usage_cache)

    @pytest.mark.asyncio
    async def test_a_parked_scrape_does_not_keep_as_reading(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        sessions_mod._usage_scrape_failures = sessions_mod._USAGE_SCRAPE_FAILURE_THRESHOLD
        sessions_mod._usage_scrape_backoff_until = 1e18
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache.get("credits_used") != 9000.0

    @pytest.mark.asyncio
    async def test_a_lower_text_scrape_replaces_as_api_value(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        with self._scrape(SAMPLE_USAGE.encode()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("source") == "text"
        assert sessions_mod._usage_cache.get("stale") is not True

    @pytest.mark.asyncio
    async def test_a_scrape_reading_is_tagged_with_the_refreshs_user_id(self, monkeypatch):
        # A's reading comes from the /usage scrape, B's refresh then fails.
        self._api(monkeypatch, None, _UID_A)
        with self._scrape(SAMPLE_USAGE.encode()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("source") == "text"
        self._api(monkeypatch, None, _UID_B)
        with self._scrape(b"not a usage block"):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_no_scrape_engine_does_not_keep_as_reading(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        monkeypatch.setattr(sessions_mod, "_wrap_argv_usage_scrape", lambda _b: (None, None))
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_an_unproven_scrape_does_not_keep_as_reading(self, monkeypatch):
        # top, pre-spawn and post-spawn whoami: the pre-spawn one printed nothing,
        # so the scrape is discarded and the cache judged against the last one.
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[dict(_WHOAMI), {}, dict(_WHOAMI)])
        )
        with self._scrape(SAMPLE_USAGE.encode()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_a_scrape_that_raises_does_not_keep_as_reading(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_B)
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=RuntimeError("x"))):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_a_reading_dropped_for_a_switch_keeps_nothing_of_a(self, monkeypatch):
        # B's API answer is dropped because whoami changed during the read (the
        # profile ARN vanished); email and start_url still match A's reading.
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, dict(_READING_A), _UID_B)
        monkeypatch.setattr(
            sessions_mod, "_fetch_whoami", AsyncMock(side_effect=[dict(_WHOAMI), dict(_IDENTITY)])
        )
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False

    @pytest.mark.asyncio
    async def test_a_switch_after_the_api_read_discards_the_scrape(self, monkeypatch):
        # The API read anchored on A and found no plan; kiro-cli switched to B
        # before the scrape ran. The scrape is not published under A's userId,
        # and A's reading is not kept for B.
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_A, store_uid=_UID_B)
        with self._scrape(SAMPLE_USAGE.encode()):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert sessions_mod._usage_cache_user_id is None

    @pytest.mark.asyncio
    async def test_a_switch_after_the_api_read_discards_the_api_reading(self, monkeypatch):
        self._api(monkeypatch, dict(_READING_A), _UID_A, store_uid=_UID_B)
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert "credits_used" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_an_api_call_that_never_returns_does_not_keep_as_reading(self, monkeypatch):
        # The refresh fails before the API answers, so it has no userId of its
        # own; the cached reading's userId still forces a fresh comparison.
        await self._publish_a(monkeypatch)

        def api_fails(**_k):
            raise RuntimeError("api hung")

        monkeypatch.setattr(sessions_mod.kiro_usage_api, "fetch_usage_limits", api_fails)
        self.store_uid = _UID_B
        await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache.get("available") is False
        assert "credits_used" not in sessions_mod._usage_cache

    @pytest.mark.asyncio
    async def test_no_store_read_without_an_api_user_id(self, monkeypatch):
        # An account whose store token has no userId keeps today's behaviour
        # and never reads the store again.
        reads = MagicMock(return_value=_UID_B)
        monkeypatch.setattr(sessions_mod.kiro_usage_api, "signed_in_user_id", reads)
        self._api(monkeypatch, None, None)
        with self._scrape(b"not a usage block"):
            await sessions_mod._fetch_usage_bg()
        assert reads.call_count == 0

    @pytest.mark.asyncio
    async def test_the_same_user_keeps_its_reading_stale(self, monkeypatch):
        await self._publish_a(monkeypatch)
        self._api(monkeypatch, None, _UID_A)
        with self._scrape(b"not a usage block"):
            await sessions_mod._fetch_usage_bg()
        assert sessions_mod._usage_cache["credits_used"] == 9000.0
        assert sessions_mod._usage_cache["stale"] is True

    @pytest.mark.asyncio
    async def test_the_user_id_is_never_served(self, monkeypatch):
        await self._publish_a(monkeypatch)
        assert _UID_A not in str(sessions_mod._usage_cache)
        assert not any(str(k).startswith("_") for k in sessions_mod._usage_cache)
