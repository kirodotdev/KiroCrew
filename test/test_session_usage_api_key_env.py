"""The credit pill never shows kiro-cli's login balance for sessions that run on ``KIRO_API_KEY``.

kiro-cli answers ``whoami`` and ``/usage`` from its stored login even when
``KIRO_API_KEY`` is set (checked on kiro-cli 3.3.0), while the sessions this gateway
spawns bill the key's account. Before the fix the pill read the login's balance and sat
still while the key's credits were spent. These tests pin the refresh to publishing the
``api_key`` marker instead, and keep the two neighbouring behaviours: whoami reporting the
key itself still scrapes, and an install with no key still reads the login's API balance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from session_usage_helpers import (
    _CACHED_A,
    _IDENTITY_A,
    SAMPLE_USAGE,
    _api_result,
    _mock_proc,
    _reset_usage_globals,
)

import kiro_crew.dashboard.handlers.sessions as sessions_mod

_LOGIN_WHOAMI = {**_IDENTITY_A, "account_type": "IamIdentityCenter"}


@pytest.fixture(autouse=True)
def _isolated_usage_state(_floor_monkeypatch):
    _reset_usage_globals()
    _floor_monkeypatch.delenv("KIRO_API_KEY", raising=False)
    _floor_monkeypatch.setattr("kiro_crew.config.loader.read_env_file_credential", lambda key: "")
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions.wrap_argv",
        lambda argv, **k: (list(argv), None),
    )
    yield
    _reset_usage_globals()


async def _refresh(whoami: dict[str, object]):
    """Run one refresh with *whoami* as kiro-cli's identity; return the API mock and spawn mock."""
    spawn = AsyncMock(return_value=_mock_proc(SAMPLE_USAGE.encode()))
    api_dict = {"credits_used": 8.0, "credits_plan": 2000.0, "source": "api"}
    with (
        patch.object(sessions_mod, "_resolve_kiro_bin_for_spawn", return_value="/bin/kiro"),
        patch.object(sessions_mod, "_fetch_whoami", AsyncMock(return_value=whoami)),
        patch.object(
            sessions_mod.kiro_usage_api,
            "fetch_usage_limits",
            return_value=_api_result(api_dict),
        ) as fetch,
        patch("asyncio.create_subprocess_exec", spawn),
    ):
        await sessions_mod._fetch_usage_bg()
    return fetch, spawn


@pytest.mark.asyncio
async def test_key_in_environment_hides_the_login_balance(monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "ksk_example")
    fetch, spawn = await _refresh(_LOGIN_WHOAMI)
    # Neither source is consulted: both would answer for the login, not the key.
    fetch.assert_not_called()
    assert spawn.await_count == 0
    assert sessions_mod._usage_cache == {"available": False, "reason": "api_key"}
    assert sessions_mod._usage_fetching is False


@pytest.mark.asyncio
async def test_key_only_in_dot_env_hides_the_login_balance(monkeypatch):
    # A Docker install keeps the key in the data home's .env, out of the environment.
    monkeypatch.setattr(
        "kiro_crew.config.loader.read_env_file_credential",
        lambda key: "ksk_example" if key == "KIRO_API_KEY" else "",
    )
    fetch, _spawn = await _refresh(_LOGIN_WHOAMI)
    fetch.assert_not_called()
    assert sessions_mod._usage_cache == {"available": False, "reason": "api_key"}


@pytest.mark.asyncio
async def test_a_login_reading_already_cached_is_replaced_not_kept_stale(monkeypatch):
    # Setting the key after a reading was cached must not leave that reading on screen
    # dimmed as "stale": it still names the wrong account.
    sessions_mod._usage_cache = dict(_CACHED_A)
    monkeypatch.setenv("KIRO_API_KEY", "ksk_example")
    await _refresh(_LOGIN_WHOAMI)
    assert sessions_mod._usage_cache.get("credits_plan") is None
    assert sessions_mod._usage_cache.get("reason") == "api_key"


@pytest.mark.asyncio
async def test_a_whoami_that_named_no_one_is_not_taken_for_a_login(monkeypatch):
    # A whoami that timed out, failed or printed no identity comes back as `{}`. That
    # proves no login is held, and the key may be the only credential, so the refresh
    # must take the ordinary path (and the scrape) instead of publishing `api_key`.
    monkeypatch.setenv("KIRO_API_KEY", "ksk_example")
    fetch, _spawn = await _refresh({})
    fetch.assert_called_once()
    assert sessions_mod._usage_cache.get("reason") != "api_key"


@pytest.mark.asyncio
async def test_whoami_reporting_the_key_itself_still_scrapes(monkeypatch):
    # With no stored login whoami reports ApiKey and kiro-cli's own `/usage` answers for
    # that key, so the existing scrape path must keep supplying the number.
    monkeypatch.setenv("KIRO_API_KEY", "ksk_example")
    fetch, _spawn = await _refresh({"account_type": "ApiKey"})
    fetch.assert_not_called()
    assert sessions_mod._usage_cache.get("credits_plan") == 10000.0
    assert sessions_mod._usage_cache.get("source") == "text"


@pytest.mark.asyncio
async def test_no_key_reads_the_login_balance_as_before():
    fetch, _spawn = await _refresh(_LOGIN_WHOAMI)
    fetch.assert_called_once()
    assert sessions_mod._usage_cache["credits_plan"] == 2000.0
    assert "reason" not in sessions_mod._usage_cache


class TestKiroApiKeyConfigured:
    def test_blank_values_are_not_a_key(self, monkeypatch):
        monkeypatch.setenv("KIRO_API_KEY", "   ")
        monkeypatch.setattr("kiro_crew.config.loader.read_env_file_credential", lambda key: "  ")
        assert sessions_mod._kiro_api_key_configured() is False

    def test_environment_value_counts(self, monkeypatch):
        monkeypatch.setenv("KIRO_API_KEY", "ksk_example")
        assert sessions_mod._kiro_api_key_configured() is True

    def test_unreadable_dot_env_proves_no_key(self, monkeypatch):
        def boom(key):
            raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.config.loader.read_env_file_credential", boom)
        assert sessions_mod._kiro_api_key_configured() is False
