"""Credit usage from Crew's own sign-in vault, with no kiro-cli on the host.

Covers the vault branch of ``_fetch_usage_bg`` (``_refresh_from_vault``), the
readiness-gate bypass on ``/api/sessions/usage``, and the single-credential API
client ``fetch_usage_limits_with_credential``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.kiro_usage_api as api
import kiro_crew.dashboard.handlers.sessions as sessions_mod

_FP_A = "fp-a"
_FP_B = "fp-b"
_USAGE = {"credits_plan": 1000.0, "credits_used": 42.0, "plan": "KIRO PRO", "_profile_arn": "arn:x"}


def _cred(profile_arn: str | None = "arn:aws:codewhisperer:us-east-1:1:profile/p"):
    return SimpleNamespace(
        access_token="FAKE-access",
        profile_arn=profile_arn,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _reset():
    def reset():
        sessions_mod._usage_cache = {}
        sessions_mod._usage_cache_ts = 0.0
        sessions_mod._usage_fetching = False
        sessions_mod._usage_cache_vault_fp = ""

    reset()
    yield
    reset()


def _vault(*, holds=True, fps=(_FP_A, _FP_A), current=None, result=None):
    """Patch the vault seams: probe, fingerprint sequence, provider, API client."""
    fp_iter = iter(fps)
    provider = MagicMock()
    provider.current = current or AsyncMock(return_value=_cred())
    return [
        patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=holds)),
        patch("kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: next(fp_iter)),
        patch("kiro_crew.auth.bridge.default_token_store", MagicMock()),
        patch("kiro_crew.auth.provider.KasAuthProvider", MagicMock(return_value=provider)),
        patch.object(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits_with_credential",
            MagicMock(
                return_value=(
                    result if result is not None else api.UsageResult(dict(_USAGE), api.AUTH_OK)
                )
            ),
        ),
    ]


async def _run(patches, resolve=None):
    resolve = resolve or MagicMock(return_value=None)
    for p in patches:
        p.start()
    try:
        with patch.object(
            sessions_mod, "_resolve_kiro_bin_for_spawn", AsyncMock(side_effect=resolve)
        ):
            await sessions_mod._fetch_usage_bg()
    finally:
        for p in reversed(patches):
            p.stop()
    return resolve


class TestRefreshFromVault:
    @pytest.mark.asyncio
    async def test_publishes_reading_without_kiro_cli(self):
        resolve = await _run(_vault())
        assert sessions_mod._usage_cache["credits_plan"] == 1000.0
        assert "_profile_arn" not in sessions_mod._usage_cache
        assert sessions_mod._usage_cache_vault_fp == _FP_A
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_vault_identity_takes_kiro_cli_path(self):
        resolve = await _run(_vault(holds=False))
        resolve.assert_called_once()
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_identity_change_discards_reading(self):
        await _run(_vault(fps=(_FP_A, _FP_B)))
        assert sessions_mod._usage_cache == {"available": False}

    @pytest.mark.asyncio
    async def test_auth_rejected_reports_signin_and_does_not_fall_back(self):
        resolve = await _run(_vault(result=api.UsageResult(None, api.AUTH_REJECTED)))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_refresh_failure_reports_signin(self):
        current = AsyncMock(side_effect=RuntimeError("issuer said no"))
        resolve = await _run(_vault(current=current))
        assert sessions_mod._usage_cache == {"available": False, "reason": "signin_required"}
        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_keeps_same_identity_reading_stale(self):
        sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 1.0}
        sessions_mod._usage_cache_vault_fp = _FP_A
        await _run(_vault(result=api.UsageResult(None, api.AUTH_OTHER)))
        assert sessions_mod._usage_cache["stale"] is True
        assert sessions_mod._usage_cache["credits_used"] == 1.0

    @pytest.mark.asyncio
    async def test_failure_never_keeps_kiro_cli_reading(self):
        # A reading published by the kiro-cli path carries no vault fingerprint.
        sessions_mod._usage_cache = {"credits_plan": 1000.0, "credits_used": 1.0}
        await _run(_vault(result=api.UsageResult(None, api.AUTH_OTHER)))
        assert sessions_mod._usage_cache == {"available": False}


def _request():
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    return make_mocked_request("GET", "/api/sessions/usage", app=app)


class TestUsageGateBypass:
    @pytest.mark.asyncio
    async def test_vault_identity_skips_kiro_readiness_gate(self):
        gate = AsyncMock(return_value="BLOCKED")
        sessions_mod._usage_cache_ts = 10**12  # fresh: no background refresh
        sessions_mod._usage_cache = {"credits_plan": 5.0}
        request = _request()
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=True)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        ):
            resp = await sessions_mod.api_sessions_usage(request)
        gate.assert_not_called()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_vault_identity_keeps_gate(self):
        blocked = MagicMock()
        gate = AsyncMock(return_value=blocked)
        with (
            patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
            patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        ):
            resp = await sessions_mod.api_sessions_usage(_request())
        assert resp is blocked


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


_CREDIT_BODY = {
    "usageBreakdownList": [
        {
            "resourceType": "CREDIT",
            "currentUsageWithPrecision": 42.0,
            "usageLimitWithPrecision": 1000.0,
        }
    ],
    "subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
}


class TestFetchWithCredential:
    def test_uses_stored_arn_and_maps_response(self):
        arn = "arn:aws:codewhisperer:us-east-1:1:profile/p"
        posted = []

        def fake_post(token, target, payload, *, endpoint=None):
            posted.append((target, payload))
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": arn, "profileName": "Team"}]})
            return _resp(200, _CREDIT_BODY)

        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        with patch.object(api, "_post", fake_post):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", arn)
        assert result.auth_state == api.AUTH_OK
        assert result.usage["credits_plan"] == 1000.0
        assert result.usage["_profile_arn"] == arn
        assert result.usage["account"] == "Team"
        assert posted[-1] == (api._TARGET_GET_USAGE, {"origin": "AI_EDITOR", "profileArn": arn})

    def test_401_is_auth_rejected(self):
        api._PROFILE_ARN_CACHE.clear()
        with patch.object(api, "_post", lambda *a, **k: _resp(401, {})):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", None)
        assert result == api.UsageResult(None, api.AUTH_REJECTED)

    def test_transport_error_is_other_and_never_raises(self):
        def boom(*a, **k):
            raise api._RequestError("dns")

        api._PROFILE_ARN_CACHE.clear()
        with patch.object(api, "_post", boom):
            result = api.fetch_usage_limits_with_credential("FAKE-tok", None)
        assert result == api.UsageResult(None, api.AUTH_OTHER)

    def test_empty_token_is_no_credential(self):
        assert api.fetch_usage_limits_with_credential("", None).auth_state == api.AUTH_NO_CREDENTIAL
