"""Coverage tests for the Discord Gateway + REST client (``discord/client.py``).

Targets the paths the behavioural suite leaves untouched: the lifecycle
(start/close/wait_ready), every outbound REST wrapper, the gateway connection
loop and its close-code classification, frame handling (hello / resume /
heartbeat / reconnect / invalid-session), the heartbeat task's failure modes,
dispatch normalization for READY / RESUMED / INTERACTION_CREATE, handler
error isolation, and the ``_api`` retry + degrade ladder.

Everything runs against stubbed transports -- no socket, no network, no
subprocess, and no writes outside ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp
import pytest

from kiro_crew.discord import client as dc
from kiro_crew.discord.client import (
    _API_BASE,
    _CALLBACK_DEFERRED_UPDATE_MESSAGE,
    _GATEWAY_URL,
    _INTENT_DIRECT_MESSAGES,
    _OP_DISPATCH,
    _OP_HEARTBEAT,
    _OP_HEARTBEAT_ACK,
    _OP_HELLO,
    _OP_IDENTIFY,
    _OP_INVALID_SESSION,
    _OP_RECONNECT,
    _OP_RESUME,
    DISCORD_MAX_TEXT,
    DiscordClient,
    DiscordInbound,
    DiscordInteraction,
    _resolve_proxy,
)

_PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)

_REAL_SLEEP = asyncio.sleep


# ── Stub transports ────────────────────────────────────────────────────────


class FakeWS:
    """Minimal stand-in for an aiohttp WebSocketResponse."""

    def __init__(
        self,
        messages: list[Any] | None = None,
        *,
        close_code: int | None = None,
        raise_on_iter: BaseException | None = None,
    ) -> None:
        self._messages = list(messages or [])
        self._raise_on_iter = raise_on_iter
        self.closed = False
        self.close_code = close_code
        self.sent: list[dict] = []
        self.close_calls = 0
        self.send_error: BaseException | None = None

    async def send_json(self, payload: dict) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        if self._raise_on_iter is not None:
            raise self._raise_on_iter


class _WSMessage:
    def __init__(self, type_: Any, data: str = "") -> None:
        self.type = type_
        self.data = data


class _AsyncCM:
    def __init__(self, value: Any, *, enter_error: BaseException | None = None) -> None:
        self._value = value
        self._enter_error = enter_error

    async def __aenter__(self) -> Any:
        if self._enter_error is not None:
            raise self._enter_error
        return self._value

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: Any = None,
        *,
        json_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._json_error = json_error
        self.raise_for_status_calls = 0

    async def json(self, content_type: Any = None) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._body

    def raise_for_status(self) -> None:
        self.raise_for_status_calls += 1


class FakeSession:
    """Stand-in for aiohttp.ClientSession covering ws_connect + request."""

    def __init__(
        self,
        *,
        ws: FakeWS | None = None,
        ws_error: BaseException | None = None,
        responses: list[Any] | None = None,
    ) -> None:
        self._ws = ws
        self._ws_error = ws_error
        self._responses = list(responses or [])
        self.closed = False
        self.ws_urls: list[str] = []
        self.ws_kwargs: list[dict] = []
        self.requests: list[tuple[str, str, Any, dict]] = []
        self.close_calls = 0

    def ws_connect(self, url: str, **kwargs: Any) -> _AsyncCM:
        self.ws_urls.append(url)
        self.ws_kwargs.append(kwargs)
        return _AsyncCM(self._ws, enter_error=self._ws_error)

    def request(self, method: str, url: str, **kwargs: Any) -> _AsyncCM:
        self.requests.append((method, url, kwargs.get("json"), kwargs))
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            return _AsyncCM(None, enter_error=nxt)
        return _AsyncCM(nxt)

    async def get(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("download paths must stub get() explicitly")

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _make_client(**kwargs: Any) -> DiscordClient:
    kwargs.setdefault("token", "bot-secret")
    kwargs.setdefault("proxy", "http://proxy.invalid:8080")
    return DiscordClient(**kwargs)


def _bind_session(
    client: DiscordClient, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ensure() -> Any:
        return session

    monkeypatch.setattr(client, "_ensure_session", _ensure)


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep with a yielding no-op that records durations."""
    slept: list[float] = []

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        slept.append(delay)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(dc.asyncio, "sleep", _fake_sleep)
    return slept


# ── Readiness + state observer ─────────────────────────────────────────────


class TestReadinessAndStateObserver:
    @pytest.mark.asyncio
    async def test_wait_ready_returns_false_on_timeout(self) -> None:
        client = _make_client()
        assert await client.wait_ready(timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_wait_ready_returns_true_once_set(self) -> None:
        client = _make_client()
        client.ready.set()
        assert await client.wait_ready(timeout=1.0) is True

    def test_notify_state_forwards_to_observer(self) -> None:
        client = _make_client()
        seen: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: seen.append((connected, error))
        client._notify_state(True, "")
        client._notify_state(False, "boom")
        assert seen == [(True, ""), (False, "boom")]

    def test_notify_state_swallows_observer_exception(self) -> None:
        client = _make_client()

        def _explode(connected: bool, error: str) -> None:
            raise RuntimeError("observer down")

        client.on_state_change = _explode
        client._notify_state(True, "")  # must not raise

    def test_notify_state_without_observer_is_a_noop(self) -> None:
        client = _make_client()
        assert client.on_state_change is None
        client._notify_state(False, "x")


# ── Lifecycle ──────────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_launches_the_gateway_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        ran = asyncio.Event()

        async def _loop() -> None:
            ran.set()

        monkeypatch.setattr(client, "_gateway_loop", _loop)
        await client.start()
        assert client._task is not None
        await client._task
        assert ran.is_set()
        assert client._closed is False

    @pytest.mark.asyncio
    async def test_close_stops_task_ws_and_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        ws = FakeWS()
        session = FakeSession()
        client._ws = ws
        client._session = session  # type: ignore[assignment]

        async def _forever() -> None:
            await _REAL_SLEEP(30)

        client._task = asyncio.create_task(_forever())
        hb_ws = FakeWS()

        async def _hb(_ws: Any, _interval: float) -> None:
            await _REAL_SLEEP(30)

        monkeypatch.setattr(client, "_heartbeat_loop", _hb)
        client._start_heartbeat(hb_ws, 1.0)
        hb_task = client._hb_task
        await _REAL_SLEEP(0)

        await client.close()

        assert client._closed is True
        assert ws.close_calls == 1
        assert client._task is None
        assert session.close_calls == 1
        assert client._session is None
        assert hb_task is not None
        assert hb_task.done()

    @pytest.mark.asyncio
    async def test_close_tolerates_ws_close_failure_and_closed_session(self) -> None:
        client = _make_client()

        class _BadWS(FakeWS):
            async def close(self) -> None:
                self.close_calls += 1
                raise RuntimeError("already gone")

        client._ws = _BadWS()
        closed_session = FakeSession()
        closed_session.closed = True
        client._session = closed_session  # type: ignore[assignment]
        await client.close()
        assert client._session is not None  # closed session is left in place
        assert closed_session.close_calls == 0

    @pytest.mark.asyncio
    async def test_close_skips_an_already_closed_ws(self) -> None:
        client = _make_client()
        ws = FakeWS()
        ws.closed = True
        client._ws = ws
        await client.close()
        assert ws.close_calls == 0

    def test_set_message_handler_replaces_the_handler(self) -> None:
        client = _make_client()

        async def _handler(_inbound: DiscordInbound) -> None:
            return None

        client.set_message_handler(_handler)
        assert client._on_message is _handler

    def test_thread_intents_are_requested_only_when_enabled(self) -> None:
        assert _make_client()._intents == _INTENT_DIRECT_MESSAGES
        assert _make_client(enable_guild_threads=True)._intents > _INTENT_DIRECT_MESSAGES


# ── Outbound REST wrappers ─────────────────────────────────────────────────


class TestOutboundRest:
    @pytest.mark.asyncio
    async def test_send_message_truncates_and_carries_components_and_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        calls: list[tuple[str, str, Any]] = []

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            calls.append((method, path, payload))
            return {"id": 991}

        monkeypatch.setattr(client, "_api", _api)
        result = await client.send_message(
            "c1",
            "x" * (DISCORD_MAX_TEXT + 50),
            components=[{"type": 1}],
            reply_to_message_id="m7",
        )
        assert result == "991"
        method, path, payload = calls[0]
        assert method == "POST"
        assert path == "/channels/c1/messages"
        assert len(payload["content"]) == DISCORD_MAX_TEXT
        assert payload["components"] == [{"type": 1}]
        assert payload["message_reference"] == {
            "message_id": "m7",
            "fail_if_not_exists": False,
        }

    @pytest.mark.asyncio
    async def test_send_message_returns_none_on_api_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()

        async def _api(*_args: Any, **_kwargs: Any) -> Any:
            return None

        monkeypatch.setattr(client, "_api", _api)
        assert await client.send_message("c1", "hi") is None

    @pytest.mark.asyncio
    async def test_edit_message_omits_components_when_not_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        calls: list[tuple[str, str, Any]] = []

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            calls.append((method, path, payload))
            return {}

        monkeypatch.setattr(client, "_api", _api)
        assert await client.edit_message("c1", "m1", "body") is True
        assert "components" not in calls[0][2]
        assert calls[0][1] == "/channels/c1/messages/m1"

        assert await client.edit_message("c1", "m1", "body", components=[]) is True
        assert calls[1][2]["components"] == []

    @pytest.mark.asyncio
    async def test_edit_message_returns_false_on_api_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()

        async def _api(*_args: Any, **_kwargs: Any) -> Any:
            return None

        monkeypatch.setattr(client, "_api", _api)
        assert await client.edit_message("c1", "m1", "body") is False
        assert await client.edit_message_components("c1", "m1", []) is False

    @pytest.mark.asyncio
    async def test_typing_reaction_and_ack_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        calls: list[tuple[str, str, Any]] = []

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            calls.append((method, path, payload))
            return {}

        monkeypatch.setattr(client, "_api", _api)
        await client.send_typing("c1")
        await client.add_reaction("c1", "m1", "\N{EYES}")
        await client.ack_component_interaction("i1", "tok")

        assert calls[0] == ("POST", "/channels/c1/typing", {})
        assert calls[1][0] == "PUT"
        assert calls[1][1] == "/channels/c1/messages/m1/reactions/%F0%9F%91%80/@me"
        assert calls[1][2] is None
        assert calls[2] == (
            "POST",
            "/interactions/i1/tok/callback",
            {"type": _CALLBACK_DEFERRED_UPDATE_MESSAGE},
        )

    @pytest.mark.asyncio
    async def test_edit_message_components_sends_only_components(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        calls: list[tuple[str, str, Any]] = []

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            calls.append((method, path, payload))
            return {}

        monkeypatch.setattr(client, "_api", _api)
        assert await client.edit_message_components("c1", "m1", []) is True
        assert calls[0] == ("PATCH", "/channels/c1/messages/m1", {"components": []})

    @pytest.mark.asyncio
    async def test_create_dm_channel_success_and_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        outcomes: list[Any] = [{"id": 42}, None]

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            assert (method, path) == ("POST", "/users/@me/channels")
            assert payload == {"recipient_id": "u9"}
            return outcomes.pop(0)

        monkeypatch.setattr(client, "_api", _api)
        assert await client.create_dm_channel("u9") == "42"
        assert await client.create_dm_channel("u9") == ""


class TestIsThreadChannel:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel_type,expected",
        [(10, True), (11, True), (12, True), (0, False), (5, False)],
    )
    async def test_resolves_and_caches_channel_type(
        self, monkeypatch: pytest.MonkeyPatch, channel_type: int, expected: bool
    ) -> None:
        client = _make_client()
        calls: list[str] = []

        async def _api(method: str, path: str, payload: Any, timeout: int = 30) -> Any:
            calls.append(path)
            return {"type": channel_type}

        monkeypatch.setattr(client, "_api", _api)
        assert await client.is_thread_channel("c1") is expected
        assert client._channel_types == {"c1": channel_type}
        # Second call is served from the cache -- no extra REST hit.
        assert await client.is_thread_channel("c1") is expected
        assert calls == ["/channels/c1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("result", [None, {}, {"type": "11"}, "nope"])
    async def test_fails_closed_on_unusable_result(
        self, monkeypatch: pytest.MonkeyPatch, result: Any
    ) -> None:
        client = _make_client()

        async def _api(*_args: Any, **_kwargs: Any) -> Any:
            return result

        monkeypatch.setattr(client, "_api", _api)
        assert await client.is_thread_channel("c1") is False
        assert client._channel_types == {}


# ── Attachment download guards ─────────────────────────────────────────────


class TestAttachmentDownloadGuards:
    @pytest.mark.asyncio
    async def test_unparsable_port_is_rejected(self, tmp_path: Any) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="invalid Discord attachment URL"):
            await client.download_attachment(
                "https://cdn.discordapp.com:notaport/f.png", str(tmp_path / "out")
            )

    @pytest.mark.asyncio
    async def test_redirect_is_refused_before_any_write(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        dest = tmp_path / "out.bin"

        class _Session:
            def get(self, *_args: Any, **_kwargs: Any) -> _AsyncCM:
                return _AsyncCM(FakeResponse(302))

        _bind_session(client, _Session(), monkeypatch)
        with pytest.raises(ValueError, match="refusing redirected"):
            await client.download_attachment(
                "https://cdn.discordapp.com/attachments/c/m/out.bin", str(dest)
            )
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_allowed_host_writes_bytes_to_dest(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        dest = tmp_path / "ok.bin"

        class _Content:
            async def iter_chunked(self, size: int) -> Any:
                assert size == 8192
                yield b"ab"
                yield b"cd"

        class _Resp(FakeResponse):
            content = _Content()

        class _Session:
            def get(self, *_args: Any, **kwargs: Any) -> _AsyncCM:
                assert kwargs["allow_redirects"] is False
                return _AsyncCM(_Resp(200))

        _bind_session(client, _Session(), monkeypatch)
        await client.download_attachment(
            "https://media.discordapp.net/attachments/c/m/ok.bin", str(dest)
        )
        assert dest.read_bytes() == b"abcd"
        assert os.path.realpath(str(dest)) == os.path.realpath(str(tmp_path / "ok.bin"))


# ── Gateway loop ───────────────────────────────────────────────────────────


class TestGatewayLoop:
    @pytest.mark.asyncio
    async def test_transport_error_backs_off_then_stops_when_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)
        attempts: list[int] = []

        async def _run() -> None:
            attempts.append(1)
            if len(attempts) >= 3:
                client._closed = True
                return
            raise aiohttp.ClientError("drop")

        monkeypatch.setattr(client, "_run_connection", _run)
        await client._gateway_loop()

        assert len(attempts) == 3
        # Exponential backoff with jitter: 1s then 2s base.
        assert len(slept) == 2
        assert 1.0 <= slept[0] < 2.0
        assert 2.0 <= slept[1] < 3.0

    @pytest.mark.asyncio
    async def test_transport_error_after_close_breaks_without_sleeping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)

        async def _run() -> None:
            client._closed = True
            raise OSError("gone")

        monkeypatch.setattr(client, "_run_connection", _run)
        await client._gateway_loop()
        assert slept == []

    @pytest.mark.asyncio
    async def test_unexpected_error_sleeps_a_flat_five_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)
        calls: list[int] = []

        async def _run() -> None:
            calls.append(1)
            if len(calls) >= 2:
                client._closed = True
                return
            raise RuntimeError("unexpected")

        monkeypatch.setattr(client, "_run_connection", _run)
        await client._gateway_loop()
        assert slept == [5.0]

    @pytest.mark.asyncio
    async def test_unexpected_error_after_close_breaks_without_sleeping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)

        async def _run() -> None:
            client._closed = True
            raise RuntimeError("unexpected")

        monkeypatch.setattr(client, "_run_connection", _run)
        await client._gateway_loop()
        assert slept == []

    @pytest.mark.asyncio
    async def test_cancellation_breaks_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()

        async def _run() -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(client, "_run_connection", _run)
        await client._gateway_loop()
        assert client._closed is False

    @pytest.mark.asyncio
    async def test_loop_exits_immediately_when_already_closed(self) -> None:
        client = _make_client()
        client._closed = True
        await client._gateway_loop()


# ── One connection: close-code classification ──────────────────────────────


class TestRunConnection:
    @pytest.mark.asyncio
    async def test_dispatches_text_frames_and_breaks_on_close_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        ws = FakeWS(
            [
                _WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({"op": _OP_HEARTBEAT_ACK})),
                _WSMessage(aiohttp.WSMsgType.CLOSED),
                _WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({"op": _OP_HEARTBEAT_ACK})),
            ],
            close_code=1000,
        )
        session = FakeSession(ws=ws)
        _bind_session(client, session, monkeypatch)
        client._hb_acked = False

        await client._run_connection()

        assert client._hb_acked is True
        assert client._ws is None
        assert session.ws_urls == [_GATEWAY_URL]
        assert session.ws_kwargs[0]["max_msg_size"] == 0
        assert session.ws_kwargs[0]["heartbeat"] == dc._WS_HEARTBEAT_SECS

    @pytest.mark.asyncio
    async def test_resume_url_is_preferred_when_a_session_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        client._session_id = "sid"
        client._resume_url = "wss://resume.example/?v=10&encoding=json"
        session = FakeSession(ws=FakeWS(close_code=1000))
        _bind_session(client, session, monkeypatch)
        await client._run_connection()
        assert session.ws_urls == ["wss://resume.example/?v=10&encoding=json"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
    async def test_non_recoverable_close_stops_the_channel(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        client.ready.set()
        _bind_session(client, FakeSession(ws=FakeWS(close_code=code)), monkeypatch)

        await client._run_connection()

        assert client._closed is True
        assert client.fatal_error == f"gateway close {code} (check bot token/intents)"
        assert states == [(False, client.fatal_error)]
        assert client.ready.is_set() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [4007, 4009])
    async def test_invalid_sequence_close_clears_resume_state(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        client = _make_client()
        client._session_id = "sid"
        client._seq = 12
        client._resume_url = "wss://resume.example/?v=10"
        _bind_session(client, FakeSession(ws=FakeWS(close_code=code)), monkeypatch)

        await client._run_connection()

        assert client._session_id == ""
        assert client._seq is None
        assert client._closed is False

    @pytest.mark.asyncio
    async def test_ordinary_close_notifies_reconnecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        _bind_session(client, FakeSession(ws=FakeWS(close_code=None)), monkeypatch)

        await client._run_connection()

        assert states == [(False, "reconnecting (close none)")]

    @pytest.mark.asyncio
    async def test_exception_escape_still_clears_ready_and_notifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        client.ready.set()
        ws = FakeWS(close_code=1006, raise_on_iter=aiohttp.ClientError("mid-dispatch"))
        _bind_session(client, FakeSession(ws=ws), monkeypatch)

        with pytest.raises(aiohttp.ClientError):
            await client._run_connection()

        assert client.ready.is_set() is False
        assert states == [(False, "reconnecting (close 1006)")]
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_connect_failure_before_bind_reports_no_close_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        _bind_session(client, FakeSession(ws_error=OSError("refused")), monkeypatch)

        with pytest.raises(OSError):
            await client._run_connection()

        assert states == [(False, "reconnecting (close none)")]


# ── Frame handling ─────────────────────────────────────────────────────────


class TestHandleFrame:
    @pytest.mark.asyncio
    async def test_hello_without_a_session_identifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(enable_guild_threads=True)
        monkeypatch.setattr(client, "_start_heartbeat", lambda ws, interval: None)
        ws = FakeWS()
        await client._handle_frame(ws, {"op": _OP_HELLO, "d": {"heartbeat_interval": 41250}})
        assert ws.sent[0]["op"] == _OP_IDENTIFY
        assert ws.sent[0]["d"]["intents"] == client._intents
        assert ws.sent[0]["d"]["properties"] == {
            "os": "linux",
            "browser": "kirocrew",
            "device": "kirocrew",
        }

    @pytest.mark.asyncio
    async def test_hello_with_a_session_resumes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        intervals: list[float] = []
        monkeypatch.setattr(
            client, "_start_heartbeat", lambda ws, interval: intervals.append(interval)
        )
        client._session_id = "sid"
        client._resume_url = "wss://resume.example/?v=10"
        ws = FakeWS()
        await client._handle_frame(
            ws, {"op": _OP_HELLO, "d": {"heartbeat_interval": 41250}, "s": 7}
        )
        assert intervals == [41.25]
        assert client._seq == 7
        assert ws.sent[0] == {
            "op": _OP_RESUME,
            "d": {"token": "bot-secret", "session_id": "sid", "seq": 7},
        }

    @pytest.mark.asyncio
    async def test_server_heartbeat_request_is_echoed(self) -> None:
        client = _make_client()
        client._seq = 5
        ws = FakeWS()
        await client._handle_frame(ws, {"op": _OP_HEARTBEAT})
        assert ws.sent == [{"op": _OP_HEARTBEAT, "d": 5}]

    @pytest.mark.asyncio
    async def test_heartbeat_ack_marks_the_connection_live(self) -> None:
        client = _make_client()
        client._hb_acked = False
        await client._handle_frame(FakeWS(), {"op": _OP_HEARTBEAT_ACK})
        assert client._hb_acked is True

    @pytest.mark.asyncio
    async def test_reconnect_opcode_closes_the_socket(self) -> None:
        client = _make_client()
        ws = FakeWS()
        await client._handle_frame(ws, {"op": _OP_RECONNECT})
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_invalid_session_non_resumable_clears_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)
        client._session_id = "sid"
        client._seq = 9
        ws = FakeWS()
        await client._handle_frame(ws, {"op": _OP_INVALID_SESSION, "d": False})
        assert client._session_id == ""
        assert client._seq is None
        assert ws.close_calls == 1
        assert 1.0 <= slept[0] <= 5.0

    @pytest.mark.asyncio
    async def test_invalid_session_resumable_keeps_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        client._session_id = "sid"
        client._seq = 9
        ws = FakeWS()
        await client._handle_frame(ws, {"op": _OP_INVALID_SESSION, "d": True})
        assert client._session_id == "sid"
        assert client._seq == 9
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_dispatch_opcode_is_routed_with_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            client, "_on_dispatch", lambda event, d: seen.append((event, d))
        )
        await client._handle_frame(FakeWS(), {"op": _OP_DISPATCH})
        assert seen == [("", {})]

    @pytest.mark.asyncio
    async def test_unknown_opcode_is_ignored(self) -> None:
        client = _make_client()
        ws = FakeWS()
        await client._handle_frame(ws, {"op": 99})
        assert ws.sent == []


# ── Heartbeat task ─────────────────────────────────────────────────────────


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_start_heartbeat_replaces_the_previous_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()

        async def _hb(_ws: Any, _interval: float) -> None:
            await _REAL_SLEEP(30)

        monkeypatch.setattr(client, "_heartbeat_loop", _hb)
        client._start_heartbeat(FakeWS(), 1.0)
        first = client._hb_task
        client._start_heartbeat(FakeWS(), 1.0)
        second = client._hb_task
        assert first is not None and second is not None and first is not second
        await _REAL_SLEEP(0)
        assert first.cancelled() or first.done()
        client._stop_heartbeat()
        assert client._hb_task is None

    def test_stop_heartbeat_without_a_task_is_a_noop(self) -> None:
        client = _make_client()
        client._stop_heartbeat()
        assert client._hb_task is None

    @pytest.mark.asyncio
    async def test_loop_sends_heartbeats_until_the_socket_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        client._seq = 3
        ws = FakeWS()
        calls: list[float] = []

        async def _fake_sleep(delay: float, *_a: Any, **_k: Any) -> None:
            calls.append(delay)
            if len(calls) >= 2:
                ws.closed = True
            await _REAL_SLEEP(0)

        monkeypatch.setattr(dc.asyncio, "sleep", _fake_sleep)
        await client._heartbeat_loop(ws, 10.0)

        assert ws.sent == [{"op": _OP_HEARTBEAT, "d": 3}]
        assert client._hb_acked is False
        assert 0.0 <= calls[0] < 10.0  # jittered first beat
        assert calls[1] == 10.0

    @pytest.mark.asyncio
    async def test_missed_ack_recycles_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        client._hb_acked = False
        ws = FakeWS()
        await client._heartbeat_loop(ws, 1.0)
        assert ws.close_calls == 1
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_send_failure_closes_the_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        ws = FakeWS()
        ws.send_error = RuntimeError("socket wedged")
        await client._heartbeat_loop(ws, 1.0)
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_recycle_close_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)

        class _BadWS(FakeWS):
            async def close(self) -> None:
                self.close_calls += 1
                raise RuntimeError("close failed too")

        ws = _BadWS()
        ws.send_error = RuntimeError("socket wedged")
        await client._heartbeat_loop(ws, 1.0)
        assert ws.close_calls == 1

    @pytest.mark.asyncio
    async def test_cancellation_is_absorbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        ws = FakeWS()
        ws.send_error = asyncio.CancelledError()
        await client._heartbeat_loop(ws, 1.0)
        assert ws.close_calls == 0

    @pytest.mark.asyncio
    async def test_loop_exits_when_the_client_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        client._closed = True
        ws = FakeWS()
        await client._heartbeat_loop(ws, 1.0)
        assert ws.sent == []
        assert ws.close_calls == 0


# ── Dispatch normalization ─────────────────────────────────────────────────


class TestDispatchNormalization:
    def test_ready_records_session_and_appends_query_to_resume_url(self) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        client._on_dispatch(
            "READY",
            {
                "session_id": "sid-1",
                "resume_gateway_url": "wss://resume.example",
                "user": {"id": 777},
            },
        )
        assert client._session_id == "sid-1"
        assert client._resume_url == "wss://resume.example/?v=10&encoding=json"
        assert client.bot_user_id == "777"
        assert client.ready.is_set() is True
        assert states == [(True, "")]

    def test_ready_keeps_a_resume_url_that_already_has_a_query(self) -> None:
        client = _make_client()
        client._on_dispatch(
            "READY", {"resume_gateway_url": "wss://resume.example/?v=10&encoding=json"}
        )
        assert client._resume_url == "wss://resume.example/?v=10&encoding=json"
        assert client.bot_user_id == ""

    def test_ready_without_a_resume_url_leaves_it_empty(self) -> None:
        client = _make_client()
        client._on_dispatch("READY", {})
        assert client._resume_url == ""

    def test_resumed_marks_the_channel_connected(self) -> None:
        client = _make_client()
        states: list[tuple[bool, str]] = []
        client.on_state_change = lambda connected, error: states.append((connected, error))
        client._on_dispatch("RESUMED", {})
        assert client.ready.is_set() is True
        assert states == [(True, "")]

    def test_unknown_event_is_ignored(self) -> None:
        client = _make_client()
        client._on_dispatch("TYPING_START", {"channel_id": "c1"})
        assert client._handler_tasks == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "author",
        [{"id": "u2", "bot": True}, {"id": "self-id"}],
    )
    async def test_bot_and_self_messages_are_dropped(self, author: dict) -> None:
        client = _make_client()
        client.bot_user_id = "self-id"
        client._on_dispatch("MESSAGE_CREATE", {"channel_id": "c1", "author": author})
        assert client._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_message_create_normalizes_and_drops_bad_attachments(self) -> None:
        seen: list[DiscordInbound] = []

        async def _handler(inbound: DiscordInbound) -> None:
            seen.append(inbound)

        client = _make_client(on_message=_handler)
        client._on_dispatch(
            "MESSAGE_CREATE",
            {
                "channel_id": 5,
                "id": 6,
                "guild_id": None,
                "content": "hey",
                "author": {"id": 7, "username": "zed"},
                "attachments": [{"filename": "a"}, "not-a-dict", None],
            },
        )
        tasks = tuple(client._handler_tasks)
        assert tasks
        await asyncio.gather(*tasks)
        assert seen[0] == DiscordInbound(
            channel_id="5",
            user_id="7",
            username="zed",
            text="hey",
            message_id="6",
            guild_id="",
            is_bot=False,
            attachments=[{"filename": "a"}],
        )
        assert client._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_interaction_create_recovers_label_and_member_user(self) -> None:
        seen: list[DiscordInteraction] = []

        async def _handler(interaction: DiscordInteraction) -> None:
            seen.append(interaction)

        client = _make_client(on_interaction=_handler)
        client._on_dispatch(
            "INTERACTION_CREATE",
            {
                "id": 1,
                "token": "tok",
                "type": 3,
                "channel_id": 2,
                "guild_id": 3,
                "member": {"user": {"id": 4, "username": "zed"}},
                "data": {"custom_id": "opt:1"},
                "message": {
                    "id": 5,
                    "components": [
                        {"components": [{"custom_id": "opt:1", "label": "Merge it now"}]}
                    ],
                },
            },
        )
        tasks = tuple(client._handler_tasks)
        assert tasks
        await asyncio.gather(*tasks)
        assert seen[0] == DiscordInteraction(
            interaction_id="1",
            interaction_token="tok",
            channel_id="2",
            user_id="4",
            message_id="5",
            custom_id="opt:1",
            label="Merge it now",
            guild_id="3",
            username="zed",
        )

    def test_non_component_interactions_are_ignored(self) -> None:
        client = _make_client()
        client._on_dispatch("INTERACTION_CREATE", {"id": 1, "type": 2})
        assert client._handler_tasks == set()


class TestHandlerIsolation:
    @pytest.mark.asyncio
    async def test_message_handler_absence_is_a_noop(self) -> None:
        client = _make_client()
        await client._invoke_message(DiscordInbound(channel_id="c", user_id="u"))

    @pytest.mark.asyncio
    async def test_message_handler_exception_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _boom(_inbound: DiscordInbound) -> None:
            raise RuntimeError("handler blew up")

        client = _make_client(on_message=_boom)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.discord.client"):
            await client._invoke_message(DiscordInbound(channel_id="c", user_id="u9"))
        assert "on_message handler raised" in caplog.text

    @pytest.mark.asyncio
    async def test_interaction_handler_absence_is_a_noop(self) -> None:
        client = _make_client()
        await client._invoke_interaction(
            DiscordInteraction(
                interaction_id="i",
                interaction_token="t",
                channel_id="c",
                user_id="u",
                message_id="m",
            )
        )

    @pytest.mark.asyncio
    async def test_interaction_handler_exception_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _boom(_interaction: DiscordInteraction) -> None:
            raise RuntimeError("handler blew up")

        client = _make_client(on_interaction=_boom)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.discord.client"):
            await client._invoke_interaction(
                DiscordInteraction(
                    interaction_id="i",
                    interaction_token="t",
                    channel_id="c",
                    user_id="u",
                    message_id="m",
                )
            )
        assert "on_interaction handler raised" in caplog.text


# ── HTTP transport ─────────────────────────────────────────────────────────


class TestEnsureSession:
    @pytest.mark.asyncio
    async def test_session_is_created_once_and_recreated_after_close(self) -> None:
        client = _make_client()
        try:
            first = await client._ensure_session()
            assert await client._ensure_session() is first
            await first.close()
            second = await client._ensure_session()
            assert second is not first
        finally:
            await client.close()


class TestApi:
    @pytest.mark.asyncio
    async def test_204_returns_an_empty_dict_and_sends_auth_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        session = FakeSession(responses=[FakeResponse(204)])
        _bind_session(client, session, monkeypatch)
        assert await client._api("POST", "/channels/c1/typing", {}) == {}
        method, url, payload, kwargs = session.requests[0]
        assert method == "POST"
        assert url == _API_BASE + "/channels/c1/typing"
        assert payload == {}
        assert kwargs["headers"] == {"Authorization": "Bot bot-secret"}
        assert kwargs["proxy"] == "http://proxy.invalid:8080"

    @pytest.mark.asyncio
    async def test_2xx_json_body_is_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _bind_session(client, FakeSession(responses=[FakeResponse(201, {"id": "9"})]), monkeypatch)
        assert await client._api("POST", "/p", None) == {"id": "9"}

    @pytest.mark.asyncio
    async def test_2xx_with_undecodable_body_degrades_to_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        session = FakeSession(
            responses=[FakeResponse(200, json_error=ValueError("not json"))]
        )
        _bind_session(client, session, monkeypatch)
        assert await client._api("GET", "/p", None) == {}

    @pytest.mark.asyncio
    async def test_rate_limit_backs_off_once_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)
        session = FakeSession(
            responses=[
                FakeResponse(429, {"retry_after": 2.5}),
                FakeResponse(200, {"ok": True}),
            ]
        )
        _bind_session(client, session, monkeypatch)
        assert await client._api("POST", "/p", None) == {"ok": True}
        assert slept == [2.5]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,expected_delay",
        [
            ({"retry_after": "soon"}, 1.0),
            ({"retry_after": None}, 1.0),
            ({}, 1.0),
            ({"retry_after": 0.01}, 0.5),
            ({"retry_after": 900}, 5.0),
        ],
    )
    async def test_retry_after_is_clamped_and_defaulted(
        self, monkeypatch: pytest.MonkeyPatch, body: dict, expected_delay: float
    ) -> None:
        client = _make_client()
        slept = _patch_sleep(monkeypatch)
        session = FakeSession(responses=[FakeResponse(429, body), FakeResponse(204)])
        _bind_session(client, session, monkeypatch)
        assert await client._api("POST", "/p", None) == {}
        assert slept == [expected_delay]

    @pytest.mark.asyncio
    async def test_second_rate_limit_gives_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        _patch_sleep(monkeypatch)
        session = FakeSession(
            responses=[FakeResponse(429, {"retry_after": 1}), FakeResponse(429, {})]
        )
        _bind_session(client, session, monkeypatch)
        assert await client._api("POST", "/p", None) is None
        assert len(session.requests) == 2

    @pytest.mark.asyncio
    async def test_error_status_logs_code_and_message_then_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _make_client()
        session = FakeSession(
            responses=[FakeResponse(403, {"code": 50013, "message": "Missing Permissions"})]
        )
        _bind_session(client, session, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
            assert await client._api("POST", "/channels/c1/messages", {}) is None
        assert "50013" in caplog.text
        assert "Missing Permissions" in caplog.text
        assert "bot-secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_error_status_with_unparsable_body_still_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()
        session = FakeSession(
            responses=[FakeResponse(500, json_error=ValueError("html error page"))]
        )
        _bind_session(client, session, monkeypatch)
        assert await client._api("GET", "/p", None) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [aiohttp.ClientError("reset"), asyncio.TimeoutError()],
    )
    async def test_transport_errors_degrade_to_none_without_leaking_the_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exc: BaseException,
    ) -> None:
        client = _make_client()
        _bind_session(client, FakeSession(responses=[exc]), monkeypatch)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
            assert await client._api("POST", "/p", {}) is None
        assert "transport error" in caplog.text
        assert "bot-secret" not in caplog.text


# ── Proxy resolution ───────────────────────────────────────────────────────


class TestResolveProxy:
    @pytest.mark.parametrize("var", list(_PROXY_VARS))
    def test_each_supported_variable_is_honored(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        for name in _PROXY_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(var, "http://proxy.invalid:3128")
        assert _resolve_proxy() == "http://proxy.invalid:3128"

    def test_no_proxy_variables_resolves_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in _PROXY_VARS:
            monkeypatch.delenv(name, raising=False)
        assert _resolve_proxy() is None

    def test_empty_variable_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _PROXY_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "")
        monkeypatch.setenv("http_proxy", "http://fallback.invalid:3128")
        assert _resolve_proxy() == "http://fallback.invalid:3128"

    def test_explicit_proxy_argument_wins_over_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://env.invalid:3128")
        client = DiscordClient(token="t", proxy="http://arg.invalid:1")
        assert client._proxy == "http://arg.invalid:1"
