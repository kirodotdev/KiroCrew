"""Tests for the Microsoft Teams client (JWT validation, inbound webhook,
outbound Connector REST).

JWT tests require PyJWT (the ``kirocrew[teams]`` extra) and are skipped when it
is absent. Inbound/outbound tests are fully mocked -- no network.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import kiro_crew.teams.client as teams_client_mod
from kiro_crew.teams.client import (
    JwtValidator,
    TeamsAuthError,
    TeamsClient,
    TeamsInbound,
)

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_ISS = "https://api.botframework.com"
_APP_ID = "app-123"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, *, aud=_APP_ID, iss=_ISS, exp_delta=3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"aud": aud, "iss": iss, "exp": now + exp_delta, "iat": now},
        key,
        algorithm="RS256",
    )


def _validator(key) -> JwtValidator:
    # Inject the signing key so no JWKS network fetch happens.
    return JwtValidator(_APP_ID, signing_key_getter=lambda tok: key.public_key())


# ── JWT validation ──


class TestJwtValidation:
    def test_accepts_valid_token(self) -> None:
        key = _keypair()
        claims = _validator(key).verify(_token(key))
        assert claims["aud"] == _APP_ID
        assert claims["iss"] == _ISS

    def test_rejects_wrong_audience(self) -> None:
        key = _keypair()
        with pytest.raises(TeamsAuthError):
            _validator(key).verify(_token(key, aud="someone-else"))

    def test_rejects_expired(self) -> None:
        key = _keypair()
        with pytest.raises(TeamsAuthError):
            _validator(key).verify(_token(key, exp_delta=-10))

    def test_rejects_bad_signature(self) -> None:
        key, other = _keypair(), _keypair()
        # token signed by `other`, verified against `key`'s public key
        v = JwtValidator(_APP_ID, signing_key_getter=lambda tok: key.public_key())
        with pytest.raises(TeamsAuthError):
            v.verify(_token(other))

    def test_rejects_untrusted_issuer(self) -> None:
        key = _keypair()
        with pytest.raises(TeamsAuthError):
            _validator(key).verify(_token(key, iss="https://evil.example.com"))

    def test_rejects_empty_token(self) -> None:
        key = _keypair()
        with pytest.raises(TeamsAuthError):
            _validator(key).verify("")

    def test_jwks_uri_scheme_pinned_to_https(self) -> None:
        # Non-https metadata / jwks URLs are rejected before any fetch
        # (closes the file:// arbitrary-read vector).
        v = JwtValidator(_APP_ID, metadata_url="http://evil.example/meta")
        with pytest.raises(TeamsAuthError):
            v._resolve_jwks_uri()
        assert JwtValidator._require_https("https://ok.example", "x") == "https://ok.example"
        for bad in ("http://x", "file:///etc/passwd", "ftp://x", ""):
            with pytest.raises(TeamsAuthError):
                JwtValidator._require_https(bad, "x")


# ── Inbound webhook ──


class _FakeRequest:
    def __init__(self, headers: dict[str, str], body: Any) -> None:
        self.headers = headers
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _msg_activity(text: str = "hello", ctype: str = "personal") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "act-1",
        "text": text,
        "serviceUrl": "https://smba.example.com/",
        "from": {"aadObjectId": "aad-1", "userPrincipalName": "alice@example.com"},
        "conversation": {"id": "conv-1", "conversationType": ctype},
    }


def _client_with_validator(accept: bool) -> TeamsClient:
    class _V:
        def verify(self, token: str) -> dict:
            if accept and token:
                # Attested serviceUrl matches _msg_activity's serviceUrl.
                return {"aud": _APP_ID, "serviceurl": "https://smba.example.com/"}
            raise TeamsAuthError("nope")

    return TeamsClient(app_id=_APP_ID, app_password="pw", validator=_V())  # type: ignore[arg-type]


class TestInboundWebhook:
    @pytest.mark.asyncio
    async def test_invalid_token_401(self) -> None:
        c = _client_with_validator(accept=False)
        seen: list[TeamsInbound] = []
        c.set_message_handler(lambda inb: seen.append(inb) or asyncio.sleep(0))
        resp = await c.on_activity(
            _FakeRequest({"Authorization": "Bearer bad"}, _msg_activity())
        )
        assert resp.status == 401
        assert seen == []

    @pytest.mark.asyncio
    async def test_invalid_token_401_is_audited(self) -> None:
        # Regression (CWE-778 / SEC-E9FBAC19): a failed inbound-token attempt on
        # this external, cookie-auth-exempt surface MUST emit a structured SEL
        # audit line so the denial is visible to security monitoring.
        from unittest import mock

        c = _client_with_validator(accept=False)
        with mock.patch("kiro_crew.teams.client.sel") as m_sel:
            resp = await c.on_activity(
                _FakeRequest({"Authorization": "Bearer bad"}, _msg_activity())
            )
        assert resp.status == 401
        m_sel.return_value.log_api_access.assert_called_once()
        kwargs = m_sel.return_value.log_api_access.call_args.kwargs
        assert kwargs["source"] == "teams"
        assert kwargs["operation"] == "teams_client.on_activity"
        assert kwargs["outcome"] == "denied_invalid_token"

    @pytest.mark.asyncio
    async def test_401_survives_audit_sink_failure(self) -> None:
        # The 401 denial is the security decision and MUST stand even if the
        # audit sink raises (e.g. a corrupt SEL key) -- a sink failure must not
        # surface as a 500 that masks the denial.
        from unittest import mock

        c = _client_with_validator(accept=False)
        with mock.patch("kiro_crew.teams.client.sel") as m_sel:
            m_sel.return_value.log_api_access.side_effect = RuntimeError("corrupt SEL key")
            resp = await c.on_activity(
                _FakeRequest({"Authorization": "Bearer bad"}, _msg_activity())
            )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_valid_message_fast_ack_and_dispatch(self) -> None:
        c = _client_with_validator(accept=True)
        gate = asyncio.Event()
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)
            await gate.wait()  # stay pending to prove the ack didn't wait on us

        c.set_message_handler(handler)
        resp = await c.on_activity(
            _FakeRequest({"Authorization": "Bearer ok"}, _msg_activity())
        )
        assert resp.status == 200
        await asyncio.sleep(0)  # let the scheduled task start
        assert len(seen) == 1
        assert seen[0].conversation_id == "conv-1"
        assert seen[0].user_email == "alice@example.com"
        assert seen[0].conversation_type == "personal"
        gate.set()  # release the pending handler

    @pytest.mark.asyncio
    async def test_conversation_update_no_turn(self) -> None:
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        resp = await c.on_activity(
            _FakeRequest({"Authorization": "Bearer ok"}, {"type": "conversationUpdate"})
        )
        assert resp.status == 200
        await asyncio.sleep(0)
        assert seen == []

    @pytest.mark.asyncio
    async def test_empty_text_no_turn(self) -> None:
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        await c.on_activity(
            _FakeRequest({"Authorization": "Bearer ok"}, _msg_activity(text="   "))
        )
        await asyncio.sleep(0)
        assert seen == []

    @pytest.mark.asyncio
    async def test_malformed_body_400(self) -> None:
        c = _client_with_validator(accept=True)
        resp = await c.on_activity(
            _FakeRequest({"Authorization": "Bearer ok"}, ValueError("bad json"))
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_serviceurl_mismatch_no_turn(self) -> None:
        # Activity serviceUrl differs from the JWT's attested 'serviceurl' claim
        # -> must be denied so the app bearer token can't be exfiltrated.
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        act = _msg_activity()
        act["serviceUrl"] = "https://attacker.example.com/"
        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
        assert resp.status == 200  # still fast-acks
        await asyncio.sleep(0)
        assert seen == []  # but drives no turn

    @pytest.mark.asyncio
    async def test_non_https_serviceurl_no_turn(self) -> None:
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        act = _msg_activity()
        act["serviceUrl"] = "http://smba.example.com/"  # non-https
        await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
        await asyncio.sleep(0)
        assert seen == []


# ── Outbound Connector REST ──


class _FakeResp:
    def __init__(
        self,
        status: int = 200,
        json_data: Any = None,
        text_data: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text_data
        self.headers = headers or {}

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._json

    async def text(self) -> str:
        return self._text


class _FakeSession:
    def __init__(self, responses: list[_FakeResp]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses

    def post(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append((url, kwargs))
        return self._responses.pop(0)

    @property
    def closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class TestOutbound:
    @pytest.mark.asyncio
    async def test_token_fetch_and_send_message(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._session = _FakeSession(
            [
                _FakeResp(json_data={"access_token": "tok", "expires_in": 3600}),
                _FakeResp(json_data={"id": "activity-9"}),
            ]
        )
        mid = await c.send_message("conv-1", "hi there", "https://smba.example.com/")
        assert mid == "activity-9"
        # token endpoint then activities endpoint
        assert "oauth2/v2.0/token" in c._session.calls[0][0]  # type: ignore[union-attr]
        activity_url = c._session.calls[1][0]  # type: ignore[union-attr]
        assert activity_url == "https://smba.example.com/v3/conversations/conv-1/activities"
        assert c._session.calls[1][1]["headers"]["Authorization"] == "Bearer tok"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_token_cached_then_refreshed(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        # fresh cached token -> no network
        c._token = "cached"
        c._token_expiry = time.monotonic() + 999
        assert await c._get_app_token() == "cached"
        # expired -> refresh via network
        c._token_expiry = time.monotonic() - 1
        c._session = _FakeSession(
            [_FakeResp(json_data={"access_token": "fresh", "expires_in": 3600})]
        )
        assert await c._get_app_token() == "fresh"

    @pytest.mark.asyncio
    async def test_typing_posts_typing_activity(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        c._session = _FakeSession([_FakeResp(json_data={})])
        await c.send_typing("conv-1", "https://smba.example.com/")
        assert c._session.calls[0][1]["json"] == {"type": "typing"}  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_send_failure_contained_and_state_flips(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        states: list[tuple[bool, str]] = []
        c.on_state_change = lambda ok, err: states.append((ok, err))
        c._session = _FakeSession([_FakeResp(status=500, text_data="boom")])
        mid = await c.send_message("conv-1", "hi", "https://smba.example.com/")
        assert mid is None  # contained, no raise
        assert states and states[-1][0] is False

    @pytest.mark.asyncio
    async def test_missing_service_url_drops_send(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        mid = await c.send_message("conv-1", "hi", "")
        assert mid is None

    @pytest.mark.asyncio
    async def test_429_is_retried_once_honoring_retry_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors the Discord/Telegram/Webex clients' _api(): a rate-limited
        outbound send must not be dropped on the first 429 -- the Bot
        Framework Connector API enforces per-bot rate limits and returns 429
        on excess traffic, and TeamsRenderer.on_done stops at the first
        failed chunk of a multi-chunk answer, so an un-retried 429 here used
        to silently truncate the user's answer."""
        slept: list[float] = []

        async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
            slept.append(delay)

        monkeypatch.setattr(teams_client_mod.asyncio, "sleep", _fake_sleep)

        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        c._session = _FakeSession(
            [
                _FakeResp(status=429, headers={"Retry-After": "2.5"}),
                _FakeResp(json_data={"id": "activity-retried"}),
            ]
        )
        mid = await c.send_message("conv-1", "hi", "https://smba.example.com/")
        assert mid == "activity-retried"
        assert slept == [2.5]
        assert len(c._session.calls) == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_persistent_429_still_fails_after_one_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine outage/persistent throttle must still surface as a
        failure once the single-retry budget is exhausted -- this is a
        bounded retry, not a mask."""

        async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
            pass

        monkeypatch.setattr(teams_client_mod.asyncio, "sleep", _fake_sleep)
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        states: list[tuple[bool, str]] = []
        c.on_state_change = lambda ok, err: states.append((ok, err))
        c._session = _FakeSession(
            [
                _FakeResp(status=429, headers={"Retry-After": "1"}),
                _FakeResp(status=429, headers={"Retry-After": "1"}),
            ]
        )
        mid = await c.send_message("conv-1", "hi", "https://smba.example.com/")
        assert mid is None
        assert states and states[-1][0] is False
        assert len(c._session.calls) == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_retry_after_is_clamped_and_defaulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []

        async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
            slept.append(delay)

        monkeypatch.setattr(teams_client_mod.asyncio, "sleep", _fake_sleep)

        cases = [
            ({}, 1.0),  # header absent -> the "1" string default
            ({"Retry-After": "not-a-number"}, 1.0),  # unparsable -> default
            ({"Retry-After": "0.01"}, 0.5),  # below floor -> clamped up
            ({"Retry-After": "900"}, 10.0),  # above ceiling -> clamped down
        ]
        for headers, expected in cases:
            c = TeamsClient(app_id=_APP_ID, app_password="pw")
            c._token = "tok"
            c._token_expiry = time.monotonic() + 999
            c._session = _FakeSession(
                [_FakeResp(status=429, headers=headers), _FakeResp(json_data={"id": "x"})]
            )
            await c.send_message("conv-1", "hi", "https://smba.example.com/")
            assert slept[-1] == expected, f"{headers} -> expected {expected}, got {slept[-1]}"
