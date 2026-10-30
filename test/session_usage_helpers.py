"""Shared sample output, identities and refresh helpers for the session-usage tests.

``SAMPLE_USAGE`` is a ``/usage`` panel, ``_IDENTITY_A`` and ``_IDENTITY_B`` are two
accounts and ``_CACHED_A`` is account A's cached reading; ``_reset_usage_globals``
clears the refresh's module state, ``_api_result`` wraps a usage value in the
``UsageResult`` the API client returns, and ``_mock_proc`` is a subprocess double.
test_session_usage.py and its three topic parts import from this module.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import kiro_crew.dashboard.handlers.sessions as sessions_mod

SAMPLE_USAGE = (
    "Some preamble line\n"
    "Estimated Usage\n"
    "Credits used: 120.0\n"
    "You have covered in plan (3044 of 10000) credits, "
    "resets on 2026-07-01 | KIRO POWER\n"
    "Est. cost: $1.50\n"
    "Overage billed at $0.04 per credit\n"
)


_IDENTITY_A = {"email": "a@corp.com", "start_url": "https://a.awsapps.com/start"}
_IDENTITY_B = {"email": "b@corp.com", "start_url": "https://b.awsapps.com/start"}
_CACHED_A = {"credits_plan": 1000.0, "credits_used": 41.0, **_IDENTITY_A}


def _reset_usage_globals():
    sessions_mod._usage_cache = {}
    sessions_mod._usage_cache_ts = 0.0
    sessions_mod._usage_fetching = False
    sessions_mod._usage_scrape_failures = 0
    sessions_mod._usage_scrape_backoff_until = 0.0
    sessions_mod._usage_cache_vault_fp = ""


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
