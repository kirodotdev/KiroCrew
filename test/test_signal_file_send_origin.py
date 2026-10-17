"""A conversation started on Signal is its own outbound mirror.

``file_send`` delivers through ``/api/channel/upload-file``, whose destination
comes only from the caller's session-map mirror link (``resolve_channel`` ->
``_resolve_mirror_target``). Telegram, Discord and WeCom bind that link -- and
the session's origin -- from their dispatchers; without the same bind a
Signal-born session has no destination and the file lands in the dashboard
outbox with ``no_channel_destination``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.files import api_channel_upload_file
from kiro_crew.messaging.link import ChannelLink, build_dm_session_key
from kiro_crew.signal_cli.client import KIND_DATA, KIND_NOTE_TO_SELF, SignalClient, SignalInbound
from kiro_crew.signal_cli.transport import SignalTransport
from kiro_crew.signal_cli.transport_dispatch import SignalDispatcher

ACCOUNT = "+15550100100"
OWNER = "+15550101112"


class FakePeer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ts = 1700000001000

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        self.ts += 1
        return {"timestamp": self.ts}


def _client() -> tuple[SignalClient, FakePeer]:
    client = SignalClient(ACCOUNT)
    peer = FakePeer()
    client._peer = peer  # type: ignore[assignment]
    client.ready.set()
    return client, peer


def _inbound(*, kind: str = KIND_DATA) -> SignalInbound:
    sender = ACCOUNT if kind == KIND_NOTE_TO_SELF else OWNER
    inbound = SignalInbound(
        kind=kind, text="send me a file", source_number=sender, timestamp=1, account=ACCOUNT
    )
    inbound.principal = sender
    inbound.operator = True
    return inbound


def _dispatcher(sessions: Any, *, dm_scope: str = "per-user") -> SignalDispatcher:
    cfg = KiroCrewConfig()
    cfg.messaging.dm_scope = dm_scope
    dispatcher = SignalDispatcher(
        sessions=sessions, ctx_builder=object(), cfg=cfg, conv_log=None  # type: ignore[arg-type]
    )
    dispatcher.client = MagicMock()
    return dispatcher


@pytest.fixture(autouse=True)
def _permit(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    async def _yes(_channel: str) -> bool:
        return True

    _floor_monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _yes)


@pytest.fixture
def no_turn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stop at the turn: what matters here is what dispatch bound before it."""
    turns: list[Any] = []

    async def _drive(turn: Any, **_kw: Any) -> None:
        turns.append(turn)

    async def _pre_turn(*, key: str, session_key_for: Any, **_kw: Any) -> str:
        return session_key_for(key)

    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", _drive)
    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.resolve_pre_turn", _pre_turn)

    async def _admit(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.admit_inbound_callback", _admit)
    return turns


def _state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, transport: SignalTransport) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.max_generation = MagicMock(return_value=0)
    state.sessions.mirror_opt_out = MagicMock(return_value=False)
    state.register_channel_transport(transport)
    return state


def _key(handle: str) -> str:
    return build_dm_session_key("signal", "kirocrew", handle, gen=0, dm_scope="per-user")


async def _upload(state: Any, outbox_file: Any, tmp_path: Any, session_key: str) -> dict:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/channel/upload-file", api_channel_upload_file)
    with (
        patch("kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        async with TestClient(TestServer(app)) as http:
            resp = await http.post(
                "/api/channel/upload-file",
                json={"file_path": str(outbox_file), "filename": outbox_file.name},
                headers={"X-Session-Key": session_key},
            )
            assert resp.status == 200, await resp.text()
            return await resp.json()


@pytest.fixture
def outbox_file(tmp_path: Any) -> Any:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "date.txt"
    f.write_text("2026-10-17\n", encoding="utf-8")
    return f


class TestFileSendFromASignalSession:
    @pytest.mark.asyncio
    async def test_a_signal_born_session_delivers_file_send_natively(
        self, tmp_path, monkeypatch, outbox_file, no_turn
    ) -> None:
        client, peer = _client()
        transport = SignalTransport(client, allowed_numbers=[OWNER])
        state = _state(tmp_path, monkeypatch, transport)
        dispatcher = _dispatcher(state.sessions)

        await dispatcher.handle_message(_inbound())
        assert no_turn, "the turn was never reached"
        session_key = no_turn[0].session_key

        body = await _upload(state, outbox_file, tmp_path, session_key)
        assert body.get("delivered") is True, body
        sends = [p for m, p in peer.calls if m == "send"]
        assert len(sends) == 1 and sends[0]["recipient"] == [OWNER]
        assert sends[0]["attachments"][0].startswith("data:text/plain")

    @pytest.mark.asyncio
    async def test_without_the_bind_the_same_call_has_no_destination(
        self, tmp_path, monkeypatch, outbox_file
    ) -> None:
        # The pre-fix state, pinned so the test above cannot pass for a reason
        # other than the dispatcher's bind.
        client, peer = _client()
        state = _state(tmp_path, monkeypatch, SignalTransport(client, allowed_numbers=[OWNER]))
        body = await _upload(state, outbox_file, tmp_path, _key(OWNER))
        assert body.get("delivered") is False and body.get("skipped") == "no_channel_destination"
        assert peer.calls == []


class TestTheDispatcherBindsTheOrigin:
    @pytest.mark.asyncio
    async def test_a_dm_binds_mirror_and_origin_to_the_conversation(
        self, tmp_path, monkeypatch, no_turn
    ) -> None:
        state = _state(
            tmp_path, monkeypatch, SignalTransport(_client()[0], allowed_numbers=[OWNER])
        )
        await _dispatcher(state.sessions).handle_message(_inbound())
        key = no_turn[0].session_key
        expected = ChannelLink("signal", channel_id=OWNER, thread_id=None)
        assert state.sessions.get_mirror_link(key) == expected
        assert state.sessions.get_origin_link(key) == expected

    @pytest.mark.asyncio
    async def test_note_to_self_binds_the_account_itself(
        self, tmp_path, monkeypatch, no_turn
    ) -> None:
        state = _state(
            tmp_path, monkeypatch, SignalTransport(_client()[0], allowed_numbers=[ACCOUNT])
        )
        await _dispatcher(state.sessions).handle_message(_inbound(kind=KIND_NOTE_TO_SELF))
        key = no_turn[0].session_key
        assert state.sessions.get_mirror_link(key) == ChannelLink("signal", channel_id=ACCOUNT)

    @pytest.mark.asyncio
    async def test_a_unified_bucket_binds_nothing(self, tmp_path, monkeypatch, no_turn) -> None:
        # dm_scope="unified" folds every allowed user's DMs into one session, so
        # it has no single origin: binding one would send one user's files and
        # unattended output into another user's chat.
        state = _state(
            tmp_path, monkeypatch, SignalTransport(_client()[0], allowed_numbers=[OWNER])
        )
        await _dispatcher(state.sessions, dm_scope="unified").handle_message(_inbound())
        key = no_turn[0].session_key
        assert key.startswith("unified:")
        assert state.sessions.get_mirror_link(key) is None
        assert state.sessions.get_origin_link(key) is None
