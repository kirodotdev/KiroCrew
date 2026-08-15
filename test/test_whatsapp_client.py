"""WhatsAppClient tests without the neonize extra installed.

Two techniques, per the coverage brief:

* inject fake ``neonize.*`` modules into ``sys.modules`` before ``connect()``
  so the lazy imports resolve to stubs (the Go core is never loaded); and
* drive the non-neonize outbound paths directly by setting ``client._client``
  to a fake exposing async ``send_message`` / ``get_me`` / ``logout`` / ``stop``.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import kiro_crew.whatsapp.client as wac
from kiro_crew.whatsapp.client import (
    MISSING_EXTRA_HINT,
    STATE_BANNED,
    STATE_CONNECTED,
    STATE_ERROR,
    STATE_LOGGED_OUT,
    STATE_PAIRING,
    STATE_UNPAIRED,
    WhatsAppClient,
    default_db_path,
    neonize_available,
)


# ── module-level helpers ────────────────────────────────────────────────────
def test_default_db_path():
    assert str(default_db_path("/home/x")).endswith("/whatsapp/session.db")


def test_neonize_available_false_when_absent(monkeypatch):
    monkeypatch.setattr(wac, "find_spec", None, raising=False)
    # find_spec is imported lazily inside; patch importlib.util instead.
    import importlib.util as iu

    monkeypatch.setattr(iu, "find_spec", lambda name: None)
    assert neonize_available() is False


def test_neonize_available_true_when_present(monkeypatch):
    import importlib.util as iu

    monkeypatch.setattr(iu, "find_spec", lambda name: object())
    assert neonize_available() is True


def test_neonize_available_handles_import_error(monkeypatch):
    import importlib.util as iu

    def boom(name):
        raise ValueError("bad spec")

    monkeypatch.setattr(iu, "find_spec", boom)
    assert neonize_available() is False


# ── state machine ───────────────────────────────────────────────────────────
def test_initial_state_is_unpaired():
    c = WhatsAppClient("/tmp/none.db")
    assert c.state == STATE_UNPAIRED
    assert c.is_connected is False


def test_set_state_notifies_observer():
    c = WhatsAppClient("/tmp/none.db")
    seen: list[tuple[str, str]] = []
    c.on_state_change = lambda s, d: seen.append((s, d))
    c._set_state(STATE_CONNECTED, "paired")
    assert c.state == STATE_CONNECTED
    assert c.is_connected is True
    assert seen == [(STATE_CONNECTED, "paired")]


def test_set_state_swallows_observer_errors():
    c = WhatsAppClient("/tmp/none.db")

    def boom(_s, _d):
        raise RuntimeError("observer exploded")

    c.on_state_change = boom
    c._set_state(STATE_ERROR, "x")  # must not raise
    assert c.state == STATE_ERROR


def test_session_exists_true_and_false(tmp_path):
    db = tmp_path / "s.db"
    c = WhatsAppClient(str(db))
    assert c.session_exists() is False
    db.write_bytes(b"data")
    assert c.session_exists() is True


# ── connect(): missing extra ────────────────────────────────────────────────
def test_connect_raises_without_extra(monkeypatch):
    monkeypatch.setattr(wac, "neonize_available", lambda: False)
    c = WhatsAppClient("/tmp/none.db")
    with pytest.raises(RuntimeError, match="whatsapp"):
        asyncio.run(c.connect())
    assert MISSING_EXTRA_HINT


# ── connect(): fake neonize wiring ──────────────────────────────────────────
class _FakeNewAClient:
    """Captures event handlers by event class and records lifecycle calls."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.handlers: dict[Any, Any] = {}
        self.connected = False
        self.idled = False

    def event(self, ev_type):
        def _register(fn):
            self.handlers[ev_type] = fn
            return fn

        return _register

    async def connect(self):
        self.connected = True

    async def idle(self):
        self.idled = True


# Event marker classes (only identity matters as dict keys).
class QREv:  # noqa: D401
    pass


class PairStatusEv:
    pass


class ConnectedEv:
    pass


class DisconnectedEv:
    pass


class LoggedOutEv:
    pass


class TemporaryBanEv:
    pass


class MessageEv:
    pass


def _install_fake_neonize(monkeypatch):
    """Register fake neonize.aioze.{client,events} in sys.modules."""
    client_mod = ModuleType("neonize.aioze.client")
    client_mod.NewAClient = _FakeNewAClient
    events_mod = ModuleType("neonize.aioze.events")
    for name, obj in {
        "QREv": QREv,
        "PairStatusEv": PairStatusEv,
        "ConnectedEv": ConnectedEv,
        "DisconnectedEv": DisconnectedEv,
        "LoggedOutEv": LoggedOutEv,
        "TemporaryBanEv": TemporaryBanEv,
        "MessageEv": MessageEv,
    }.items():
        setattr(events_mod, name, obj)
    base = ModuleType("neonize")
    aioze = ModuleType("neonize.aioze")
    monkeypatch.setitem(sys.modules, "neonize", base)
    monkeypatch.setitem(sys.modules, "neonize.aioze", aioze)
    monkeypatch.setitem(sys.modules, "neonize.aioze.client", client_mod)
    monkeypatch.setitem(sys.modules, "neonize.aioze.events", events_mod)


def _connect(monkeypatch, tmp_path, *, session=False) -> WhatsAppClient:
    monkeypatch.setattr(wac, "neonize_available", lambda: True)
    _install_fake_neonize(monkeypatch)
    db = tmp_path / "session.db"
    if session:
        db.write_bytes(b"seed")
    c = WhatsAppClient(str(db))
    asyncio.run(c.connect())
    return c


def test_connect_registers_handlers_and_starts_idle(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    fake = c._client
    assert fake.connected is True
    assert set(fake.handlers) == {
        QREv, PairStatusEv, ConnectedEv, DisconnectedEv,
        LoggedOutEv, TemporaryBanEv, MessageEv,
    }
    # No session file -> pairing state announced before connect.
    assert c.state == STATE_PAIRING
    asyncio.run(c.disconnect())


def test_qr_handler_records_codes_and_calls_observer(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    codes: list[list[str]] = []
    c.on_qr = codes.append
    handler = c._client.handlers[QREv]
    asyncio.run(handler(None, SimpleNamespace(Codes=["c1", "c2"])))
    assert c.latest_qr == ["c1", "c2"]
    assert codes == [["c1", "c2"]]
    assert c.state == STATE_PAIRING
    asyncio.run(c.disconnect())


def test_qr_handler_swallows_observer_error(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)

    def boom(_codes):
        raise RuntimeError("qr observer failed")

    c.on_qr = boom
    handler = c._client.handlers[QREv]
    asyncio.run(handler(None, SimpleNamespace(Codes=["c1"])))  # must not raise
    asyncio.run(c.disconnect())


def test_pair_status_success_and_failure(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    handler = c._client.handlers[PairStatusEv]
    asyncio.run(handler(None, SimpleNamespace(Status=2)))
    assert c.state == STATE_CONNECTED
    asyncio.run(handler(None, SimpleNamespace(Status=0, Error="nope")))
    assert c.state == STATE_ERROR
    asyncio.run(c.disconnect())


def test_connected_handler_loads_identity(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)

    async def get_me():
        return SimpleNamespace(
            JID=SimpleNamespace(User="447700900000", Server="s.whatsapp.net"),
            LID=SimpleNamespace(User="123", Server="lid"),
            PushName="Alice",
        )

    c._client.get_me = get_me
    handler = c._client.handlers[ConnectedEv]
    asyncio.run(handler(None, None))
    assert c.state == STATE_CONNECTED
    assert c.connected_at is not None
    assert c.me.jid == "447700900000@s.whatsapp.net"
    assert c.push_name == "Alice"
    asyncio.run(c.disconnect())


def test_disconnected_handler_reports_when_connected(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    c._set_state(STATE_CONNECTED)
    handler = c._client.handlers[DisconnectedEv]
    asyncio.run(handler(None, None))
    assert c.state == STATE_ERROR
    asyncio.run(c.disconnect())


def test_logged_out_and_ban_handlers(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    asyncio.run(c._client.handlers[LoggedOutEv](None, SimpleNamespace(Reason="revoked")))
    assert c.state == STATE_LOGGED_OUT
    asyncio.run(c._client.handlers[TemporaryBanEv](None, SimpleNamespace()))
    assert c.state == STATE_BANNED
    asyncio.run(c.disconnect())


def test_message_handler_forwards_and_swallows_errors(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    got: list[Any] = []

    async def on_message(ev):
        got.append(ev)

    c.on_message = on_message
    handler = c._client.handlers[MessageEv]
    asyncio.run(handler(None, "evt"))
    assert got == ["evt"]

    async def boom(_ev):
        raise RuntimeError("handler failed")

    c.on_message = boom
    asyncio.run(handler(None, "evt2"))  # must not raise
    asyncio.run(c.disconnect())


def test_message_handler_noop_without_callback(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    c.on_message = None
    asyncio.run(c._client.handlers[MessageEv](None, "evt"))  # no-op
    asyncio.run(c.disconnect())


def test_connect_with_existing_session_skips_pairing_announce(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path, session=True)
    # session on disk -> no forced pairing announce; state stays unpaired.
    assert c.state == STATE_UNPAIRED
    asyncio.run(c.disconnect())


# ── disconnect / logout ─────────────────────────────────────────────────────
def test_disconnect_without_client_is_a_noop():
    c = WhatsAppClient("/tmp/none.db")
    asyncio.run(c.disconnect())  # no _client, no idle task


def test_disconnect_stops_client_and_cancels_idle():
    c = WhatsAppClient("/tmp/none.db")
    stopped: list[bool] = []

    class Fake:
        async def stop(self):
            stopped.append(True)

    c._client = Fake()
    c._set_state(STATE_CONNECTED)
    asyncio.run(c.disconnect())
    assert stopped == [True]
    assert c._client is None


def test_disconnect_swallows_stop_error():
    c = WhatsAppClient("/tmp/none.db")

    class Fake:
        async def stop(self):
            raise RuntimeError("stop failed")

    c._client = Fake()
    asyncio.run(c.disconnect())  # must not raise
    assert c._client is None


def test_logout_without_client_is_a_noop():
    c = WhatsAppClient("/tmp/none.db")
    asyncio.run(c.logout())
    assert c._client is None


def test_logout_unlinks_and_sets_state():
    c = WhatsAppClient("/tmp/none.db")
    called: list[bool] = []

    class Fake:
        async def logout(self):
            called.append(True)

    c._client = Fake()
    asyncio.run(c.logout())
    assert called == [True]
    assert c.state == STATE_LOGGED_OUT


def test_load_identity_swallows_errors():
    c = WhatsAppClient("/tmp/none.db")

    class Fake:
        async def get_me(self):
            raise RuntimeError("no device")

    c._client = Fake()
    asyncio.run(c._load_identity())  # must not raise


# ── outbound: send_text ─────────────────────────────────────────────────────
def test_send_text_raises_when_not_connected():
    c = WhatsAppClient("/tmp/none.db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(c.send_text("447700900000@s.whatsapp.net", "hi"))


def test_send_text_returns_message_ids(monkeypatch):
    c = WhatsAppClient("/tmp/none.db")
    sent: list[Any] = []

    class Fake:
        async def send_message(self, jid, chunk):
            sent.append((jid, chunk))
            return SimpleNamespace(ID=f"id-{len(sent)}")

    c._client = Fake()
    # Fake JID proto so _parse_jid resolves without neonize.
    _install_fake_jid(monkeypatch)
    ids = asyncio.run(c.send_text("447700900000@s.whatsapp.net", "hello world"))
    assert ids == ["id-1"]
    assert sent and sent[0][1] == "hello world"


def test_send_text_chunks_long_messages(monkeypatch):
    c = WhatsAppClient("/tmp/none.db")
    sent: list[str] = []

    class Fake:
        async def send_message(self, jid, chunk):
            sent.append(chunk)
            return SimpleNamespace(ID=f"id-{len(sent)}")

    c._client = Fake()
    _install_fake_jid(monkeypatch)
    body = "\n\n".join(["p" * 2000] * 3)
    ids = asyncio.run(c.send_text("447700900000@s.whatsapp.net", body))
    assert len(sent) > 1
    assert len(ids) == len(sent)


# ── outbound: send_typing ───────────────────────────────────────────────────
def _install_fake_jid(monkeypatch):
    proto = ModuleType("neonize.proto.Neonize_pb2")

    class JID:
        def __init__(self, User="", Server=""):
            self.User = User
            self.Server = Server

    proto.JID = JID
    monkeypatch.setitem(sys.modules, "neonize", sys.modules.get("neonize", ModuleType("neonize")))
    monkeypatch.setitem(sys.modules, "neonize.proto", ModuleType("neonize.proto"))
    monkeypatch.setitem(sys.modules, "neonize.proto.Neonize_pb2", proto)


def _install_fake_enum(monkeypatch):
    enum_mod = ModuleType("neonize.utils.enum")
    enum_mod.ChatPresence = SimpleNamespace(
        CHAT_PRESENCE_COMPOSING="composing", CHAT_PRESENCE_PAUSED="paused"
    )
    enum_mod.ChatPresenceMedia = SimpleNamespace(CHAT_PRESENCE_MEDIA_TEXT="text")
    monkeypatch.setitem(sys.modules, "neonize", sys.modules.get("neonize", ModuleType("neonize")))
    monkeypatch.setitem(sys.modules, "neonize.utils", ModuleType("neonize.utils"))
    monkeypatch.setitem(sys.modules, "neonize.utils.enum", enum_mod)


def test_send_typing_noop_without_client():
    c = WhatsAppClient("/tmp/none.db")
    asyncio.run(c.send_typing("447700900000@s.whatsapp.net", True))  # no-op


def test_send_typing_sends_presence(monkeypatch):
    c = WhatsAppClient("/tmp/none.db")
    calls: list[Any] = []

    class Fake:
        async def send_chat_presence(self, jid, state, media):
            calls.append((state, media))

    c._client = Fake()
    _install_fake_jid(monkeypatch)
    _install_fake_enum(monkeypatch)
    asyncio.run(c.send_typing("447700900000@s.whatsapp.net", True))
    asyncio.run(c.send_typing("447700900000@s.whatsapp.net", False))
    assert calls == [("composing", "text"), ("paused", "text")]


def test_send_typing_swallows_errors(monkeypatch):
    c = WhatsAppClient("/tmp/none.db")

    class Fake:
        async def send_chat_presence(self, *a):
            raise RuntimeError("presence failed")

    c._client = Fake()
    _install_fake_jid(monkeypatch)
    _install_fake_enum(monkeypatch)
    asyncio.run(c.send_typing("447700900000@s.whatsapp.net", True))  # must not raise


# ── outbound: list_groups ───────────────────────────────────────────────────
def test_list_groups_empty_without_client():
    c = WhatsAppClient("/tmp/none.db")
    assert asyncio.run(c.list_groups()) == []


def test_list_groups_maps_jid_and_name():
    c = WhatsAppClient("/tmp/none.db")

    class Fake:
        async def get_joined_groups(self):
            return [
                SimpleNamespace(
                    JID=SimpleNamespace(User="g1", Server="g.us"),
                    GroupName=SimpleNamespace(Name="Team"),
                ),
                SimpleNamespace(JID=SimpleNamespace(User="", Server=""), GroupName=None),
            ]

    c._client = Fake()
    groups = asyncio.run(c.list_groups())
    assert groups == [{"jid": "g1@g.us", "name": "Team"}]


def test_list_groups_swallows_errors():
    c = WhatsAppClient("/tmp/none.db")

    class Fake:
        async def get_joined_groups(self):
            raise RuntimeError("picker failed")

    c._client = Fake()
    assert asyncio.run(c.list_groups()) == []


# ── _parse_jid ──────────────────────────────────────────────────────────────
def test_parse_jid_builds_proto(monkeypatch):
    _install_fake_jid(monkeypatch)
    c = WhatsAppClient("/tmp/none.db")
    jid = c._parse_jid("447700900000@s.whatsapp.net")
    assert jid.User == "447700900000"
    assert jid.Server == "s.whatsapp.net"


def test_parse_jid_defaults_server(monkeypatch):
    _install_fake_jid(monkeypatch)
    c = WhatsAppClient("/tmp/none.db")
    jid = c._parse_jid("447700900000")
    assert jid.Server == "s.whatsapp.net"
