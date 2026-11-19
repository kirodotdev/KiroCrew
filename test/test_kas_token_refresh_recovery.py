"""A KAS start recovers from a failed or refused Crew sign-in without a gateway restart.

Two gaps, both on the Crew-owned route (the engine draws its token from Crew's
``_kiro/auth/getAccessToken`` callback):

* A refresh that fails on the network (DNS, connect, timeout) failed the callback
  at once, so a runtime that started inside the blip had no token. The callback
  now retries it a bounded number of times; a refused grant is never retried.
* The engine caches the token it was served until that token's own expiry and
  never asks again when Kiro refuses it, and the callback serves the vault's
  token for as long as the vault thinks it is live. A token Kiro refuses early
  therefore reached every new runtime, whose governance read then failed and
  fail-closed MCP. That start now renews the served token, so the retried start
  draws a new one, and when the renewal is refused it fails at once with the
  sign-in error instead of a retryable governance error.

Driven against a real :class:`TokenStore` in a temp data home; only the HTTP
refresh (``auth.refresh._refresh``) is replaced.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from kiro_crew.acp import kas_host_auth, runtime_session_load, session_handle_mcp
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError, answer_get_access_token
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_mcp import KasGovernanceOffError
from kiro_crew.acp.transport_errors import AcpAuthRequired
from kiro_crew.acp.types import ACP_BACKEND_KAS, JsonRpcMessage
from kiro_crew.auth.refresh import RefreshRejected
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.llm_helpers import acp_error_is_transient

_OLD = "at-served"
_NEW = "at-renewed"


def _token(access_token: str = _OLD, *, expires_in: int = 3600) -> KasToken:
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider="Google",
        identity="social",
        refresh_token="rt-value",
        profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x",
    )


def token_digest(access_token: str) -> str:
    # The digest the runtime keeps: sha256 hex, spelled out so the test does not
    # take it from the code under test.
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def sign_in_refused(served_digest):
    # Looked up at call time, so a tree without it fails the test, not collection.
    return kas_host_auth.sign_in_refused(served_digest)


def _dns_error() -> aiohttp.ClientConnectorDNSError:
    # The class the reporter's gateway logged; its key and OS error are opaque here.
    return aiohttp.ClientConnectorDNSError(MagicMock(), OSError("name resolution failed"))


@pytest.fixture
def data_home(tmp_path):
    home = tmp_path / "data-home"
    home.mkdir()
    with patch("kiro_crew.auth.bridge.data_home", return_value=home):
        yield home


@pytest.fixture
def no_backoff(_floor_monkeypatch):
    """Zero backoff for the tests that retry: they count tries, never time them.

    Requested by name, not autouse, so a tree without the retry fails the
    test that needs it instead of erroring every test's setup.
    """
    delays = (0.0, 0.0)
    _floor_monkeypatch.setattr(
        kas_host_auth, "REFRESH_RETRY_DELAYS_SECS", delays
    )  # flake-ok: zero backoff, the test counts tries and never waits


def _refresh(*outcomes):
    """Patch the HTTP refresh with *outcomes* in order (an exception raises)."""
    return patch("kiro_crew.auth.refresh._refresh", AsyncMock(side_effect=list(outcomes)))


class TestCallbackRetriesANetworkFailure:
    @pytest.mark.asyncio
    async def test_dns_failure_then_success_serves_the_new_token(self, data_home, no_backoff):
        TokenStore(data_home).save(_token(expires_in=-60))
        with _refresh(_dns_error(), _token(_NEW)) as refresh:
            answer = await answer_get_access_token()
        assert answer["accessToken"] == _NEW
        assert refresh.await_count == 2
        assert TokenStore(data_home).resolve().access_token == _NEW

    @pytest.mark.asyncio
    async def test_timeout_then_success_serves_the_new_token(self, data_home, no_backoff):
        TokenStore(data_home).save(_token(expires_in=-60))
        with _refresh(asyncio.TimeoutError(), _token(_NEW)):
            assert (await answer_get_access_token())["accessToken"] == _NEW

    @pytest.mark.asyncio
    async def test_a_refresh_that_never_succeeds_stops_after_three_tries(
        self, data_home, caplog, no_backoff
    ):
        TokenStore(data_home).save(_token(expires_in=-60))
        with _refresh(_dns_error(), _dns_error(), _dns_error(), _token(_NEW)) as refresh:
            with pytest.raises(HostAuthCallbackError, match="refresh failed"):
                await answer_get_access_token()
        assert refresh.await_count == 3
        assert _OLD not in caplog.text and "rt-value" not in caplog.text

    @pytest.mark.asyncio
    async def test_no_retry_starts_past_the_window(self, data_home, monkeypatch):
        TokenStore(data_home).save(_token(expires_in=-60))
        # A zero window: no retry starts, so nothing waits on it.
        monkeypatch.setattr(kas_host_auth, "REFRESH_RETRY_WINDOW_SECS", 0.0)
        with _refresh(_dns_error(), _token(_NEW)) as refresh:
            with pytest.raises(HostAuthCallbackError):
                await answer_get_access_token()
        assert refresh.await_count == 1

    @pytest.mark.asyncio
    async def test_a_refused_grant_is_not_retried(self, data_home):
        TokenStore(data_home).save(_token(expires_in=-60))
        with _refresh(RefreshRejected("social refresh failed: HTTP 401"), _token(_NEW)) as refresh:
            with pytest.raises(HostAuthCallbackError):
                await answer_get_access_token()
        assert refresh.await_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_whose_backoff_ends_past_the_window(self, data_home, _floor_monkeypatch):
        # The window is still open when the try fails, but the backoff would end past it.
        TokenStore(data_home).save(_token(expires_in=-60))
        window, delays = 1.0, (5.0, 5.0)
        patch_ = _floor_monkeypatch.setattr
        patch_(kas_host_auth, "REFRESH_RETRY_WINDOW_SECS", window)  # flake-ok: never waited
        patch_(kas_host_auth, "REFRESH_RETRY_DELAYS_SECS", delays)  # flake-ok: never slept
        with _refresh(_dns_error(), _token(_NEW)) as refresh:
            with pytest.raises(HostAuthCallbackError):
                await answer_get_access_token()
        assert refresh.await_count == 1


class TestARefusedServedTokenIsRenewed:
    @pytest.mark.asyncio
    async def test_root_cause_the_callback_keeps_serving_a_live_token(self, data_home):
        """Nothing tells the callback Kiro refused its token: while the vault
        thinks the token is live, every runtime is served the same one."""
        TokenStore(data_home).save(_token())
        with _refresh(_token(_NEW)) as refresh:
            first = await answer_get_access_token()
            second = await answer_get_access_token()
        assert first["accessToken"] == second["accessToken"] == _OLD
        assert refresh.await_count == 0

    @pytest.mark.asyncio
    async def test_renewing_the_served_token_gives_the_next_runtime_a_new_one(self, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(_token(_NEW)) as refresh:
            assert await sign_in_refused(token_digest(_OLD)) is False
            assert (await answer_get_access_token())["accessToken"] == _NEW
        assert refresh.await_count == 1

    @pytest.mark.asyncio
    async def test_a_refused_renewal_is_a_sign_in(self, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(RefreshRejected("social refresh failed: HTTP 401")):
            assert await sign_in_refused(token_digest(_OLD)) is True
        assert TokenStore(data_home).refresh_rejected("social") is not None

    @pytest.mark.asyncio
    async def test_a_network_failure_on_renewal_is_not_a_sign_in(self, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(_dns_error()):
            assert await sign_in_refused(token_digest(_OLD)) is False

    @pytest.mark.asyncio
    async def test_a_token_a_peer_already_replaced_is_not_renewed_again(self, data_home):
        TokenStore(data_home).save(_token(_NEW))
        with _refresh(_token("at-third")) as refresh:
            assert await sign_in_refused(token_digest(_OLD)) is False
        assert refresh.await_count == 0

    @pytest.mark.asyncio
    async def test_a_live_token_that_cannot_be_renewed_is_not_a_sign_in(self, data_home):
        # No refresh token (an external IdP may issue none): the renewal is
        # blocked locally, which proves nothing about Kiro refusing the token.
        token = _token()
        token.refresh_token = None
        TokenStore(data_home).save(token)
        # The real refresh: its local blocker check raises before any request.
        assert await sign_in_refused(token_digest(_OLD)) is False

    @pytest.mark.asyncio
    async def test_an_expired_token_that_cannot_be_renewed_is_a_sign_in(self, data_home):
        token = _token(expires_in=-60)
        token.refresh_token = None
        TokenStore(data_home).save(token)
        assert await sign_in_refused(token_digest(_OLD)) is True

    @pytest.mark.asyncio
    async def test_an_empty_vault_is_a_sign_in(self, data_home):
        assert await sign_in_refused(None) is True

    @pytest.mark.asyncio
    async def test_many_runtimes_at_once_make_one_refresh(self, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(_token(_NEW), _token("at-third")) as refresh:
            verdicts = await asyncio.gather(
                *(sign_in_refused(token_digest(_OLD)) for _ in range(5))
            )
        assert verdicts == [False] * 5
        assert refresh.await_count == 1
        assert TokenStore(data_home).resolve().access_token == _NEW


# ── The start itself ────────────────────────────────────────────────────────

_SID = "sess-refused"
_CRON = "kirocrew-cron"
_AGENT = "kirocrew"
_PARAMS = {
    "sessionId": _SID,
    "mcpServers": [{"name": _CRON}],
    "_meta": {"kiro": {"customAgents": [{"id": _AGENT, "tools": ["read", f"@{_CRON}"]}]}},
}


def _governance_off_handle() -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    handle._queue.put_nowait(
        JsonRpcMessage(
            method="_kiro/mcp/governance_disabled",
            params={"sessionId": _SID, "reason": "api_failure"},
        )
    )
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


@pytest.fixture
def runtime(tmp_path, monkeypatch, _floor_monkeypatch, data_home):
    monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
    monkeypatch.setattr(runtime_session_load, "policy_hidden_servers", lambda *_a: frozenset())
    _floor_monkeypatch.setattr(session_handle_mcp, "GOVERNED_SETTLE_SECS", 0)
    rt = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)
    rt._kas_host_auth = True
    rt._kas_served_token_digest = token_digest(_OLD)
    return rt


class TestAFailClosedStart:
    @pytest.mark.asyncio
    async def test_a_refused_sign_in_fails_fast_as_sign_in(self, runtime, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(RefreshRejected("social refresh failed: HTTP 401")):
            with pytest.raises(AcpAuthRequired) as raised:
                await runtime._wait_managed_mcp(_governance_off_handle(), _PARAMS, _AGENT, 30.0, 0)
        assert raised.value.auth_required is True
        assert "Sign in" in str(raised.value)
        assert "governance" not in str(raised.value).lower()
        assert acp_error_is_transient(raised.value) is False

    @pytest.mark.asyncio
    async def test_a_renewed_sign_in_keeps_the_retryable_error(self, runtime, data_home):
        TokenStore(data_home).save(_token())
        with _refresh(_token(_NEW)):
            with pytest.raises(KasGovernanceOffError) as raised:
                await runtime._wait_managed_mcp(_governance_off_handle(), _PARAMS, _AGENT, 30.0, 0)
        assert acp_error_is_transient(raised.value) is True
        assert TokenStore(data_home).resolve().access_token == _NEW

    @pytest.mark.asyncio
    async def test_a_cli_owned_runtime_is_not_checked(self, runtime, data_home):
        runtime._kas_host_auth = False
        check = AsyncMock()
        with patch.object(runtime_session_load, "sign_in_refused", check, create=True):
            with pytest.raises(KasGovernanceOffError):
                await runtime._wait_managed_mcp(_governance_off_handle(), _PARAMS, _AGENT, 30.0, 0)
        check.assert_not_awaited()
