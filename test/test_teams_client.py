"""Tests for the Microsoft Teams client (JWT validation, inbound webhook,
outbound Connector REST).

JWT tests require PyJWT (the ``kirocrew[teams]`` extra) and are skipped when it
is absent. Inbound/outbound tests are fully mocked -- no network.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.teams.client as teams_client_mod
from kiro_crew.teams.client import (
    JwtValidator,
    TeamsAuthError,
    TeamsClient,
    TeamsInbound,
    TeamsSendError,
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
        """Expired beyond the clock-skew tolerance, so the rejection is real."""
        key = _keypair()
        with pytest.raises(TeamsAuthError):
            _validator(key).verify(_token(key, exp_delta=-3600))

    def test_tolerates_expiry_inside_the_clock_skew_window(self) -> None:
        """A token just past ``exp`` is accepted, deliberately.

        The Bot Framework guidance specifies the industry-standard five-minute
        skew. Without it a bot host whose clock trails the Connector's rejects
        otherwise-valid tokens and the channel goes silent behind a 401 that reads
        as a bad credential.
        """
        key = _keypair()
        claims = _validator(key).verify(_token(key, exp_delta=-10))
        assert claims["aud"] == _APP_ID

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
        # Every real Teams activity carries this. It is checked POSITIVELY, because
        # an Azure Bot resource serves Web Chat and Direct Line off the same endpoint
        # with the same credential (see the foreign-channel cases below).
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/",
        "from": {"aadObjectId": "aad-1", "userPrincipalName": "alice@example.com"},
        "conversation": {"id": "conv-1", "conversationType": ctype},
    }


def _client_with_validator(accept: bool) -> TeamsClient:
    class _V:
        def verify(self, token: str) -> dict:
            if accept and token:
                # Attested serviceUrl matches _msg_activity's serviceUrl.
                return {"aud": _APP_ID, "serviceurl": "https://smba.trafficmanager.net/"}
            raise TeamsAuthError("nope")

    return TeamsClient(app_id=_APP_ID, app_password="pw", validator=_V())  # type: ignore[arg-type]


class TestInboundWebhook:
    @pytest.mark.asyncio
    async def test_invalid_token_401(self) -> None:
        c = _client_with_validator(accept=False)
        seen: list[TeamsInbound] = []
        c.set_message_handler(lambda inb: seen.append(inb) or asyncio.sleep(0))
        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer bad"}, _msg_activity()))
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
        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, _msg_activity()))
        assert resp.status == 200
        await asyncio.sleep(0)  # let the scheduled task start
        assert len(seen) == 1
        assert seen[0].conversation_id == "conv-1"
        assert seen[0].user_email == "alice@example.com"
        assert seen[0].conversation_type == "personal"
        gate.set()  # release the pending handler

    @pytest.mark.asyncio
    async def test_close_waits_for_inflight_handler_cleanup(self) -> None:
        c = _client_with_validator(accept=True)
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def handler(inb: TeamsInbound) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        c.set_message_handler(handler)
        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, _msg_activity()))
        assert resp.status == 200
        await started.wait()
        handler_task = next(iter(c._handler_tasks))

        await c.close()

        cleaned_before_close_returned = cleaned_up.is_set()
        await asyncio.gather(handler_task, return_exceptions=True)
        assert cleaned_before_close_returned
        assert not c._handler_tasks

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
        await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, _msg_activity(text="   ")))
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
        act["serviceUrl"] = "http://smba.trafficmanager.net/"  # non-https
        await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
        await asyncio.sleep(0)
        assert seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["webchat", "directline", "emulator", "", "MSTEAMS_"])
    async def test_a_foreign_azure_bot_channel_drives_no_turn(self, channel: str) -> None:
        """The channel is checked POSITIVELY, so a new Azure channel is not trusted.

        Web Chat is enabled by DEFAULT on an Azure Bot resource and Direct Line can
        be added; both reach this endpoint with a token this validator accepts as
        fully trusted, and both default ``conversationType`` to "personal". On Direct
        Line the CLIENT composes ``from``, so ``aadObjectId`` is whatever the sender
        chose -- which is the value ``teams.allowed_emails`` would then match.
        """
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        act = _msg_activity()
        act["channelId"] = channel
        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
        assert resp.status == 200, "still fast-acks; the Connector must not retry"
        await asyncio.sleep(0)
        assert seen == []

    @pytest.mark.asyncio
    async def test_the_channel_check_is_case_insensitive(self) -> None:
        """The Connector's casing is not ours to depend on."""
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        act = _msg_activity()
        act["channelId"] = "MSTeams"
        await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
        await asyncio.sleep(0)
        assert len(seen) == 1


class TestTheDenialAuditStaysOffTheLoop:
    """The one audit an anonymous internet caller can drive at will.

    SEL's first call initializes its log (a mkdir plus a file open), so writing it
    synchronously stalls every other conversation and heartbeat on the gateway's single
    event loop -- once per process, and then once per write for every 401 in a flood.
    """

    @pytest.mark.asyncio
    async def test_an_invalid_token_audits_in_a_worker(self, monkeypatch) -> None:
        loops: list[bool] = []

        class _BlockingSel:
            def log_api_access(self, **kw: Any) -> None:
                # True only when this ran ON the loop thread, which is the defect.
                try:
                    asyncio.get_running_loop()
                    loops.append(True)
                except RuntimeError:
                    loops.append(False)

        monkeypatch.setattr("kiro_crew.teams.client.sel", lambda: _BlockingSel())
        c = _client_with_validator(accept=False)

        resp = await c.on_activity(_FakeRequest({"Authorization": "Bearer nope"}, _msg_activity()))

        assert resp.status == 401
        assert loops == [False], "the SEL write must run in a worker, not on the loop"


class TestIngressSaturation:
    """What gets shed when too many turns are already in flight -- and what never is."""

    @staticmethod
    def _saturate(client: TeamsClient) -> list[asyncio.Task]:
        """Fill the in-flight set with tasks that never finish on their own."""
        from kiro_crew.teams.client import _MAX_INFLIGHT_TURNS

        async def _park() -> None:
            await asyncio.sleep(3600)

        tasks = [asyncio.ensure_future(_park()) for _ in range(_MAX_INFLIGHT_TURNS)]
        client._handler_tasks.update(tasks)
        return tasks

    @pytest.mark.asyncio
    async def test_an_ordinary_message_is_shed_when_saturated(self) -> None:
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        tasks = self._saturate(c)
        try:
            resp = await c.on_activity(
                _FakeRequest({"Authorization": "Bearer ok"}, _msg_activity())
            )
            # 200, not 429: the Connector retries a 429 straight back into the same
            # saturated state.
            assert resp.status == 200
            await asyncio.sleep(0)
            assert seen == []
        finally:
            for task in tasks:
                task.cancel()

    @pytest.mark.asyncio
    async def test_a_card_click_is_never_shed(self) -> None:
        """Teams delivers an Action.Submit as an ordinary `message` activity.

        Shedding it drops the Approve/Deny press that would FREE a slot, so every
        waiting prompt deadlocks until it times out -- the saturation defends itself.
        """
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        tasks = self._saturate(c)
        try:
            act = _msg_activity(text="")
            act["value"] = {"kc": "kc_approval", "rid": "1", "nonce": "n", "decision": "approve"}
            await c.on_activity(_FakeRequest({"Authorization": "Bearer ok"}, act))
            await asyncio.sleep(0)
            assert len(seen) == 1
        finally:
            for task in tasks:
                task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["/stop", "/cancel", "  /STOP  ", "/stop now"])
    async def test_stop_is_never_shed(self, text: str) -> None:
        """It cancels a running turn, so it is the other way out of saturation."""
        c = _client_with_validator(accept=True)
        seen: list[TeamsInbound] = []

        async def handler(inb: TeamsInbound) -> None:
            seen.append(inb)

        c.set_message_handler(handler)
        tasks = self._saturate(c)
        try:
            await c.on_activity(
                _FakeRequest({"Authorization": "Bearer ok"}, _msg_activity(text=text))
            )
            await asyncio.sleep(0)
            assert len(seen) == 1
        finally:
            for task in tasks:
                task.cancel()

    def test_the_relief_aliases_come_from_the_command_table(self) -> None:
        """A hand-copied list here would drift the moment an alias is renamed."""
        from kiro_crew.teams.commands import COMMAND_SPEC, STOP_ALIASES

        expected = next(aliases for canonical, aliases, _d in COMMAND_SPEC if canonical == "stop")
        assert STOP_ALIASES == frozenset(expected)


class TestJwksRefetchDamper:
    """A bogus `kid` must not buy an outbound HTTPS GET per request.

    PyJWKClient answers ANY unknown kid with an unconditional refresh, and the
    webhook route's failed-auth throttle cannot absorb that: it is skipped whenever
    an X-Forwarded-* header is present. So the damper sits next to the refetch.
    """

    @staticmethod
    def _validator(fetches: list[int]) -> Any:
        from kiro_crew.teams.client import JwtValidator

        class _Jwks:
            def __init__(self) -> None:
                self.cached: list[Any] = []

            def get_signing_keys(self, refresh: bool = False) -> list[Any]:
                if refresh:
                    fetches.append(1)
                return self.cached

            @staticmethod
            def match_kid(signing_keys: list[Any], kid: str) -> Any:
                return None

        v = JwtValidator(_APP_ID)
        v._jwk_client = _Jwks()
        return v

    def test_a_repeated_unknown_kid_refetches_at_most_once(self, monkeypatch) -> None:
        import jwt as pyjwt

        from kiro_crew.teams.client import TeamsAuthError

        monkeypatch.setattr(pyjwt, "get_unverified_header", lambda token: {"kid": "bogus"})
        fetches: list[int] = []
        v = self._validator(fetches)

        for _ in range(50):
            with pytest.raises(TeamsAuthError):
                v._get_signing_key("token")

        assert len(fetches) == 1, "50 bogus-kid requests bought exactly one fetch"

    def test_the_damper_lifts_after_its_interval(self, monkeypatch) -> None:
        import jwt as pyjwt

        from kiro_crew.teams import client as mod

        monkeypatch.setattr(pyjwt, "get_unverified_header", lambda token: {"kid": "bogus"})
        clock = [1000.0]
        monkeypatch.setattr("kiro_crew.teams.client.time.monotonic", lambda: clock[0])
        fetches: list[int] = []
        v = self._validator(fetches)

        with pytest.raises(mod.TeamsAuthError):
            v._get_signing_key("token")
        clock[0] += mod._JWKS_REFRESH_MIN_INTERVAL_SECS + 1
        with pytest.raises(mod.TeamsAuthError):
            v._get_signing_key("token")

        assert len(fetches) == 2, "a genuinely rotated key must still be reachable"

    def test_a_known_kid_costs_no_refetch_at_all(self, monkeypatch) -> None:
        import jwt as pyjwt

        from kiro_crew.teams.client import JwtValidator

        monkeypatch.setattr(pyjwt, "get_unverified_header", lambda token: {"kid": "known"})
        fetches: list[int] = []

        class _Jwks:
            def get_signing_keys(self, refresh: bool = False) -> list[Any]:
                if refresh:
                    fetches.append(1)
                return [SimpleNamespace(key="the-key")]

            @staticmethod
            def match_kid(signing_keys: list[Any], kid: str) -> Any:
                return signing_keys[0]

        v = JwtValidator(_APP_ID)
        v._jwk_client = _Jwks()

        assert v._get_signing_key("token") == "the-key"
        assert fetches == []


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

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResp:
        """Outbound activities go through ``session.request`` so a card update can
        use PUT on the same ladder as a POST send."""
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
        mid = await c.send_message("conv-1", "hi there", "https://smba.trafficmanager.net/")
        assert mid == "activity-9"
        # token endpoint then activities endpoint
        assert "oauth2/v2.0/token" in c._session.calls[0][0]  # type: ignore[union-attr]
        activity_url = c._session.calls[1][0]  # type: ignore[union-attr]
        assert activity_url == "https://smba.trafficmanager.net/v3/conversations/conv-1/activities"
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
        await c.send_typing("conv-1", "https://smba.trafficmanager.net/")
        assert c._session.calls[0][1]["json"] == {"type": "typing"}  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_send_failure_contained_and_state_flips(self) -> None:
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        states: list[tuple[bool, str]] = []
        c.on_state_change = lambda ok, err: states.append((ok, err))
        # 500 is not in the retry set, so one attempt then a loud failure.
        c._session = _FakeSession([_FakeResp(status=500, text_data="boom")])
        with pytest.raises(TeamsSendError):
            await c.send_message("conv-1", "hi", "https://smba.trafficmanager.net/")
        assert states and states[-1][0] is False
        # The Connector's response body never reaches the badge: it echoes request
        # content and correlation ids, and last_error is surfaced verbatim.
        assert "boom" not in c.last_error
        assert "500" in c.last_error

    @pytest.mark.asyncio
    async def test_missing_service_url_raises_rather_than_reporting_delivery(self) -> None:
        """A caller treats a return as proof of delivery, so a refusal must raise."""
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        with pytest.raises(TeamsSendError):
            await c.send_message("conv-1", "hi", "")

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
        mid = await c.send_message("conv-1", "hi", "https://smba.trafficmanager.net/")
        assert mid == "activity-retried"
        assert slept == [2.5]
        assert len(c._session.calls) == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_persistent_429_still_fails_after_one_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistent throttle must still surface once the budget is spent.

        The budget is _SEND_ATTEMPTS (1 initial + 2 retries), and exhausting it
        RAISES: a bounded retry, never a mask that reports delivery."""

        async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
            pass

        monkeypatch.setattr(teams_client_mod.asyncio, "sleep", _fake_sleep)
        c = TeamsClient(app_id=_APP_ID, app_password="pw")
        c._token = "tok"
        c._token_expiry = time.monotonic() + 999
        states: list[tuple[bool, str]] = []
        c.on_state_change = lambda ok, err: states.append((ok, err))
        c._session = _FakeSession(
            [_FakeResp(status=429, headers={"Retry-After": "1"})] * teams_client_mod._SEND_ATTEMPTS
        )
        with pytest.raises(TeamsSendError):
            await c.send_message("conv-1", "hi", "https://smba.trafficmanager.net/")
        assert states and states[-1][0] is False
        assert len(c._session.calls) == teams_client_mod._SEND_ATTEMPTS  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_retry_after_is_clamped_and_defaulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []

        async def _fake_sleep(delay: float, *a: Any, **k: Any) -> None:
            slept.append(delay)

        monkeypatch.setattr(teams_client_mod.asyncio, "sleep", _fake_sleep)

        # With no usable Retry-After the wait is exponential from a 0.5s base
        # (0.5 * 2**attempt), so the first retry waits 0.5s. A header the
        # Connector DID send is authoritative and only clamped to [0.5, 10].
        cases = [
            ({}, 0.5),  # header absent -> exponential base
            ({"Retry-After": "not-a-number"}, 0.5),  # unparsable -> same
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
            await c.send_message("conv-1", "hi", "https://smba.trafficmanager.net/")
            assert slept[-1] == expected, f"{headers} -> expected {expected}, got {slept[-1]}"
