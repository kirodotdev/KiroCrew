"""WhatsAppClient tests without the neonize extra installed.

Two techniques, per the coverage brief:

* inject fake ``neonize.*`` modules into ``sys.modules`` before ``connect()``
  so the lazy imports resolve to stubs (the Go core is never loaded); and
* drive the non-neonize outbound paths directly by setting ``client._client``
  to a fake exposing async ``send_message`` / ``get_me`` / ``logout`` / ``stop``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import wave
from pathlib import Path
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
    # as_posix(), not str(): on Windows str() renders a backslash separator, so
    # asserting on "/" fails there unconditionally and the cross-platform gate
    # goes red for a path that is perfectly correct.
    assert default_db_path("/home/x").as_posix().endswith("/whatsapp/session.db")


def test_neonize_available_false_when_absent(monkeypatch):
    # Patched on the MODULE, because find_spec is bound at module scope: it is
    # stdlib and unconditional, so an in-function import would only hide the
    # dependency from the reader. Patching importlib.util instead would not be
    # seen by the already-bound name.
    monkeypatch.setattr(wac, "find_spec", lambda name: None)
    assert neonize_available() is False


def test_neonize_available_true_when_present(monkeypatch):
    monkeypatch.setattr(wac, "find_spec", lambda name: object())
    assert neonize_available() is True


def test_neonize_available_handles_import_error(monkeypatch):
    def boom(name):
        raise ValueError("bad spec")

    monkeypatch.setattr(wac, "find_spec", boom)
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
    # Matches the DISTRIBUTION name: the message names neonize rather than the
    # `kirocrew[whatsapp]` extra, which pip cannot resolve from any index.
    with pytest.raises(RuntimeError, match="neonize"):
        asyncio.run(c.connect())
    assert "neonize" in MISSING_EXTRA_HINT
    assert "kirocrew[" not in MISSING_EXTRA_HINT


# ── connect(): fake neonize wiring ──────────────────────────────────────────
class _FakeNewAClient:
    """Captures event handlers by event class and records lifecycle calls.

    Mirrors two behaviours of the real ``NewAClient`` the channel depends on:

    * constructing it CREATES the sqlite session store, so any probe of that file
      after construction sees a non-empty session; and
    * the rotating QR code arrives through the dedicated ``qr()`` callback slot,
      carrying ONE code as bytes — never through the numbered event stream that
      ``event()`` feeds, which only dispatches ``INT_TO_EVENT`` codes.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.handlers: dict[Any, Any] = {}
        self.qr_callback: Any = None
        self.connected = False
        self.idled = False
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(db_path).write_bytes(b"sqlite-store")

    def event(self, ev_type):
        def _register(fn):
            self.handlers[ev_type] = fn
            return fn

        return _register

    def qr(self, fn):
        self.qr_callback = fn
        return fn

    async def connect(self):
        self.connected = True

    async def idle(self):
        self.idled = True


# Event marker classes (only identity matters as dict keys).
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
        PairStatusEv,
        ConnectedEv,
        DisconnectedEv,
        LoggedOutEv,
        TemporaryBanEv,
        MessageEv,
    }
    # QR is registered on the dedicated callback slot, never as a numbered event:
    # `client.event(QR)` is never dispatched by neonize, so a handler placed there
    # can never fire and the panel would wait for a code forever.
    assert fake.qr_callback is not None
    # No session file before the client was built -> pairing state announced. The
    # fake creates the store in its constructor exactly as neonize does, so this
    # also pins that the check is snapshotted BEFORE construction.
    assert c.state == STATE_PAIRING
    asyncio.run(c.disconnect())


def test_connect_with_existing_session_does_not_announce_pairing(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path, session=True)
    assert c.state != STATE_PAIRING
    asyncio.run(c.disconnect())


def test_qr_callback_records_single_bytes_code_and_calls_observer(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    codes: list[list[str]] = []
    c.on_qr = codes.append
    # neonize hands over ONE code as bytes per emission, not a batch.
    asyncio.run(c._client.qr_callback(None, b"2@rotating,code=="))
    assert c.latest_qr == ["2@rotating,code=="]
    assert codes == [["2@rotating,code=="]]
    assert c.state == STATE_PAIRING
    asyncio.run(c.disconnect())


def test_qr_callback_replaces_previous_code(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)
    asyncio.run(c._client.qr_callback(None, b"first"))
    asyncio.run(c._client.qr_callback(None, b"second"))
    # Each emission supersedes the last, so the panel renders the live code.
    assert c.latest_qr == ["second"]
    asyncio.run(c.disconnect())


def test_qr_handler_swallows_observer_error(monkeypatch, tmp_path):
    c = _connect(monkeypatch, tmp_path)

    def boom(_codes):
        raise RuntimeError("qr observer failed")

    c.on_qr = boom
    asyncio.run(c._client.qr_callback(None, b"c1"))  # must not raise
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


def test_logout_without_a_live_client_raises():
    """The caller deletes the session store on a clean return.

    That store is the only credential that can revoke this device, so returning
    quietly here would destroy it while the device stays linked on WhatsApp's
    side, still receiving and able to send. The endpoint keeps the store, and
    reports 502, only because this raises.
    """
    c = WhatsAppClient("/tmp/none.db")
    before = c.state
    with pytest.raises(RuntimeError):
        asyncio.run(c.logout())
    # Nothing was unlinked, so nothing may report itself unlinked.
    assert c.state == before


def test_a_refused_logout_never_reports_the_device_as_revoked():
    """Third thing on this path that must not lie about the same failure.

    The endpoint keeps the session store and answers 502, and the panel renders
    this state as its badge, so moving it here would show "the link was revoked"
    for a device that is still linked and still able to send.
    """
    c = WhatsAppClient("/tmp/none.db")

    class Refusing:
        async def logout(self):
            raise RuntimeError("wa logout refused")

    c._client = Refusing()
    before = c.state
    with pytest.raises(RuntimeError):
        asyncio.run(c.logout())
    assert c.state == before
    assert c.state != STATE_LOGGED_OUT


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
    assert sent and sent[0][1].conversation == "hello world"


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
        # Mirrors the real proto's contract: RawAgent/Device/Integrator are
        # REQUIRED fields, so SerializeToString() on the real class raises
        # EncodeError when they are unset. The fake records what was passed so
        # tests can pin that _parse_jid supplies all five (issue #6756), and
        # rejects unknown names because the real proto does too — a permissive
        # fake would let a field typo pass while production regresses.
        def __init__(self, User="", Server="", **required):
            unknown = set(required) - {"RawAgent", "Device", "Integrator"}
            if unknown:
                raise ValueError(f"unknown JID field(s): {sorted(unknown)}")
            self.User = User
            self.Server = Server
            self.RawAgent = required.get("RawAgent")
            self.Device = required.get("Device")
            self.Integrator = required.get("Integrator")

    proto.JID = JID

    # The E2E Message wrapper send_text uses. Passing the protobuf rather than a
    # bare str is what stops neonize interpreting "@<digits>" in agent output as
    # a real mention, so the fake has to carry it or the test cannot exercise
    # the send path at all.
    e2e = ModuleType("neonize.proto.waE2E.WAWebProtobufsE2E_pb2")

    class Message:
        def __init__(self, conversation="", **submessages):
            self.conversation = conversation
            # A media send sets exactly one submessage field, so the fake stores
            # whatever it is handed rather than enumerating the schema.
            for name, value in submessages.items():
                setattr(self, name, value)

        def __eq__(self, other):  # so a test can compare against a plain str
            return self.conversation == getattr(other, "conversation", other)

    class ContextInfo:
        """The carrier of mentions.

        Defaults to EMPTY on both fields, which is the whole assertion available
        to a caption test: neonize's own image build fills ``mentionedJID`` from a
        regex over the caption, so an empty one proves the caption was not read.
        """

        def __init__(self, mentionedJID=None, groupMentions=None):
            self.mentionedJID = list(mentionedJID or [])
            self.groupMentions = list(groupMentions or [])

    class _Submessage:
        """Records the protobuf fields the channel set, and nothing else.

        A real protobuf leaves an unset field at its default; here an unset field
        is simply absent, so a test that reads one the code never set fails loudly
        instead of asserting against a default that looks deliberate.
        """

        def __init__(self, **fields):
            self.fields = dict(fields)
            for name, value in fields.items():
                setattr(self, name, value)

    class ImageMessage(_Submessage):
        pass

    class AudioMessage(_Submessage):
        pass

    class DocumentMessage(_Submessage):
        pass

    e2e.Message = Message
    e2e.ContextInfo = ContextInfo
    e2e.ImageMessage = ImageMessage
    e2e.AudioMessage = AudioMessage
    e2e.DocumentMessage = DocumentMessage
    monkeypatch.setitem(sys.modules, "neonize", sys.modules.get("neonize", ModuleType("neonize")))
    monkeypatch.setitem(sys.modules, "neonize.proto", ModuleType("neonize.proto"))
    monkeypatch.setitem(sys.modules, "neonize.proto.Neonize_pb2", proto)
    monkeypatch.setitem(sys.modules, "neonize.proto.waE2E", ModuleType("neonize.proto.waE2E"))
    monkeypatch.setitem(sys.modules, "neonize.proto.waE2E.WAWebProtobufsE2E_pb2", e2e)


def _install_fake_enum(monkeypatch):
    enum_mod = ModuleType("neonize.utils.enum")
    enum_mod.ChatPresence = SimpleNamespace(
        CHAT_PRESENCE_COMPOSING="composing", CHAT_PRESENCE_PAUSED="paused"
    )
    enum_mod.ChatPresenceMedia = SimpleNamespace(CHAT_PRESENCE_MEDIA_TEXT="text")
    # Declared per upload so neonize never has to probe the bytes with libmagic
    # on the event loop to work out which media bucket they belong in.
    enum_mod.MediaType = SimpleNamespace(
        MediaImage="image", MediaAudio="audio", MediaDocument="document"
    )
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
@pytest.mark.parametrize(
    ("jid_str", "expected_user", "expected_server"),
    [
        ("447700900000", "447700900000", "s.whatsapp.net"),  # bare number
        ("447700900000@s.whatsapp.net", "447700900000", "s.whatsapp.net"),
        ("120363021033254949@g.us", "120363021033254949", "g.us"),  # group
        ("123456789@lid", "123456789", "lid"),  # linked-identity alias
    ],
)
def test_parse_jid_sets_required_proto_fields(monkeypatch, jid_str, expected_user, expected_server):
    """Regression for issue #6756: the neonize JID proto marks RawAgent,
    Device and Integrator as REQUIRED, so a JID built without them raises
    ``EncodeError`` from ``SerializeToString()`` at the FFI boundary and every
    outbound WhatsApp operation fails. neonize is an optional dependency not
    installed in CI, so this pins the constructor kwargs (all five fields
    passed, the required trio zeroed — mirroring ``neonize.utils.jid.build_jid``)
    rather than calling the real ``SerializeToString()``.
    """
    _install_fake_jid(monkeypatch)
    c = WhatsAppClient("/tmp/none.db")
    jid = c._parse_jid(jid_str)
    assert jid.User == expected_user
    assert jid.Server == expected_server
    # Explicit zeros, not merely absent: None means the kwarg was NOT passed
    # and the real proto would fail to serialize.
    assert jid.RawAgent == 0
    assert jid.Device == 0
    assert jid.Integrator == 0


def test_send_text_never_hands_neonize_a_bare_string(monkeypatch):
    """Agent reply text must not be INTERPRETED on the way out.

    neonize's ``send_message`` treats a ``str`` as something to parse: it runs
    ``_parse_mention`` over it, so an ``@<digits>`` run the model wrote becomes a
    real WhatsApp mention of that number, and in a group it notifies whoever
    owns it. Reply text is untrusted (a prompt-injected agent chooses what it
    writes), so the channel hands over an explicit protobuf and keeps the text
    inert. The same branch also does a group-mention lookup per send.
    """
    c = WhatsAppClient("/tmp/none.db")
    sent: list[object] = []

    class Fake:
        async def send_message(self, jid, message):
            sent.append(message)
            return SimpleNamespace(ID=f"id-{len(sent)}")

    c._client = Fake()
    _install_fake_jid(monkeypatch)
    payload = "calling @447700900000 and @447711111111 now"
    asyncio.run(c.send_text("447700900000@s.whatsapp.net", payload))
    assert sent, "nothing was sent"
    for message in sent:
        assert not isinstance(message, str), (
            "a bare str reaches neonize's mention parser, so agent-authored "
            "@<digits> would be delivered as a real mention"
        )
        assert message.conversation == payload, "text must pass through unaltered"


# ── outbound: media ─────────────────────────────────────────────────────────
#: A real 2x2 PNG, so the preview builder under test has something to decode.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


def _wav(seconds: int, rate: int = 22050) -> bytes:
    """*seconds* of silent PCM WAV, the container the bundled Piper voice emits."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * rate * seconds)
    return buffer.getvalue()


class _FakeMediaClient:
    """Records uploads and sends, and refuses every neonize send_* convenience.

    Those refusals are half the point: each of the three helpers this channel
    stopped calling is a real defect (a caption parsed as mentions, a raster
    decoded on the event loop, an ffmpeg subprocess for a duration), so a code
    change that reaches for one again fails here rather than in a chat.
    """

    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, Any]] = []
        self.sent: list[Any] = []

    async def upload(self, data, media_type=None):
        self.uploads.append((bytes(data), media_type))
        return SimpleNamespace(
            url="https://mmg.whatsapp.net/x",
            DirectPath="/v/t62/x",
            FileEncSHA256=b"enc-sha",
            FileSHA256=b"plain-sha",
            MediaKey=b"media-key",
            FileLength=len(data),
        )

    async def send_message(self, jid, message):
        self.sent.append(message)
        return SimpleNamespace(ID=f"id-{len(self.sent)}")

    async def send_image(self, *args, **kwargs):
        raise AssertionError("send_image parses the caption as mentions")

    async def send_audio(self, *args, **kwargs):
        raise AssertionError("send_audio shells out to ffmpeg for the duration")

    async def send_document(self, *args, **kwargs):
        raise AssertionError("send_document parses the caption as mentions")


def _media_client(monkeypatch) -> tuple[WhatsAppClient, _FakeMediaClient]:
    c = WhatsAppClient("/tmp/none.db")
    fake = _FakeMediaClient()
    c._client = fake
    _install_fake_jid(monkeypatch)
    _install_fake_enum(monkeypatch)
    return c, fake


def test_send_image_bytes_raises_when_not_connected():
    c = WhatsAppClient("/tmp/none.db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(c.send_image_bytes("447700900000@s.whatsapp.net", _PNG))


def test_the_image_caption_never_reaches_neonizes_mention_parser(monkeypatch):
    """An image caption is agent-authored alt text, so it must not be INTERPRETED.

    ``build_image_message`` runs ``_parse_mention`` over the caption and puts the
    result in ``contextInfo.mentionedJID``: an ``@<digits>`` run the model wrote
    would be delivered as a real WhatsApp mention of that number, and in a group
    it notifies whoever owns it. Same class of defect as a bare ``str`` handed to
    ``send_message``, and the same fix -- assemble the protobuf here.
    """
    c, fake = _media_client(monkeypatch)
    caption = "chart for @447700900000 and @447711111111"
    message_id = asyncio.run(
        c.send_image_bytes("447700900000@s.whatsapp.net", _PNG, caption, mimetype="image/png")
    )
    assert message_id == "id-1"
    assert len(fake.sent) == 1
    image = fake.sent[0].imageMessage
    assert image.caption == caption, "the caption must travel unaltered"
    assert image.contextInfo.mentionedJID == []
    assert image.contextInfo.groupMentions == []


def test_the_image_preview_is_built_off_the_event_loop(monkeypatch):
    """The preview is a full raster decode, a rescale and a JPEG encode.

    Inline, that stalls every other channel, every live turn and the liveness
    heartbeat on the one gateway loop. Asserted by asking the builder itself
    whether a loop is running in its thread, which is true only when it was
    called directly from the coroutine.
    """
    c, fake = _media_client(monkeypatch)
    on_loop: list[bool] = []

    def spy(data: bytes) -> bytes:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return b"preview-bytes"

    monkeypatch.setattr(wac, "_image_thumbnail", spy)
    asyncio.run(c.send_image_bytes("447700900000@s.whatsapp.net", _PNG))
    assert on_loop == [False], "the decode must run on a worker thread"
    assert fake.sent[0].imageMessage.JPEGThumbnail == b"preview-bytes"


def test_the_image_upload_declares_its_media_type_and_carries_the_upload_handles(monkeypatch):
    """Every field WhatsApp needs to decrypt the object must come off the upload.

    Dropping one produces a bubble the recipient cannot open, which no send error
    reports.
    """
    c, fake = _media_client(monkeypatch)
    asyncio.run(c.send_image_bytes("447700900000@s.whatsapp.net", _PNG, mimetype="image/png"))
    assert fake.uploads == [(_PNG, "image")]
    image = fake.sent[0].imageMessage
    assert image.URL == "https://mmg.whatsapp.net/x"
    assert image.directPath == "/v/t62/x"
    assert image.fileEncSHA256 == b"enc-sha"
    assert image.fileSHA256 == b"plain-sha"
    assert image.mediaKey == b"media-key"
    assert image.fileLength == len(_PNG)
    assert image.mimetype == "image/png"


def test_an_undeclared_image_type_is_sniffed_from_the_bytes(monkeypatch):
    """Never from a filename: leading bytes are the one signal a caller cannot fake."""
    c, fake = _media_client(monkeypatch)
    asyncio.run(c.send_image_bytes("447700900000@s.whatsapp.net", _PNG))
    assert fake.sent[0].imageMessage.mimetype == "image/png"


def test_the_image_id_reaches_the_echo_tracker_before_returning(monkeypatch):
    """An id the tracker never saw comes back as the operator typing."""
    c, _fake = _media_client(monkeypatch)
    seen: list[str] = []
    message_id = asyncio.run(
        c.send_image_bytes("447700900000@s.whatsapp.net", _PNG, "", seen.append)
    )
    assert seen == [message_id] == ["id-1"]


def test_a_voice_note_is_sent_push_to_talk(monkeypatch):
    """``PTT`` is the whole difference between a voice note and a file attachment.

    Without it the recipient sees an audio file bubble, which is not what the
    channel's inbound half transcribes and not what the operator sent.
    """
    c, fake = _media_client(monkeypatch)
    data = _wav(2)
    message_id = asyncio.run(
        c.send_voice_bytes("447700900000@s.whatsapp.net", data, mimetype="audio/wav")
    )
    assert message_id == "id-1"
    assert fake.uploads == [(data, "audio")]
    audio = fake.sent[0].audioMessage
    assert audio.PTT is True
    assert audio.mimetype == "audio/wav"
    assert audio.fileLength == len(data)


def test_a_voice_notes_duration_is_read_from_the_wav_header(monkeypatch):
    """WhatsApp labels a voice note ``0:00`` without ``seconds``.

    neonize derives the figure by shelling out to ffmpeg, which this channel
    cannot require, so the container the bundled voice emits is read directly.
    """
    c, fake = _media_client(monkeypatch)
    asyncio.run(c.send_voice_bytes("447700900000@s.whatsapp.net", _wav(3), mimetype="audio/wav"))
    assert fake.sent[0].audioMessage.seconds == 3


def test_a_caller_supplied_duration_wins_over_the_header(monkeypatch):
    """The caller may know the length of a container this cannot read (an MP3)."""
    c, fake = _media_client(monkeypatch)
    asyncio.run(
        c.send_voice_bytes(
            "447700900000@s.whatsapp.net", b"not-a-wav", mimetype="audio/mpeg", seconds=7
        )
    )
    assert fake.sent[0].audioMessage.seconds == 7


def test_an_unreadable_container_costs_the_label_and_nothing_else(monkeypatch):
    c, fake = _media_client(monkeypatch)
    asyncio.run(
        c.send_voice_bytes("447700900000@s.whatsapp.net", b"not-a-wav", mimetype="audio/mpeg")
    )
    assert fake.sent[0].audioMessage.seconds == 0
    assert fake.sent[0].audioMessage.PTT is True


def test_send_voice_bytes_raises_when_not_connected():
    c = WhatsAppClient("/tmp/none.db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(c.send_voice_bytes("447700900000@s.whatsapp.net", b"x", mimetype="audio/wav"))


def test_a_document_travels_under_its_basename_only(monkeypatch):
    """WhatsApp shows the recipient whatever ``fileName`` carries.

    The caller's name is a real path on the operator's machine, so sending it
    whole would put their directory layout in someone else's chat.
    """
    c, fake = _media_client(monkeypatch)
    message_id = asyncio.run(
        c.send_document_bytes(
            "447700900000@s.whatsapp.net",
            b"%PDF-1.4 ...",
            "/Users/alice/clients/acme-secret/report.pdf",
        )
    )
    assert message_id == "id-1"
    document = fake.sent[0].documentMessage
    assert document.fileName == "report.pdf"
    assert document.title == "report.pdf"
    assert "alice" not in document.fileName
    assert "alice" not in document.title


def test_a_documents_type_is_derived_from_its_name_when_undeclared(monkeypatch):
    c, fake = _media_client(monkeypatch)
    asyncio.run(c.send_document_bytes("447700900000@s.whatsapp.net", b"x", "notes.pdf"))
    assert fake.sent[0].documentMessage.mimetype == "application/pdf"
    assert fake.uploads == [(b"x", "document")]


def test_a_document_nothing_can_identify_still_sends(monkeypatch):
    c, fake = _media_client(monkeypatch)
    asyncio.run(c.send_document_bytes("447700900000@s.whatsapp.net", b"x", "mystery"))
    assert fake.sent[0].documentMessage.mimetype == "application/octet-stream"


def test_the_document_caption_never_reaches_the_mention_parser(monkeypatch):
    """``build_document_message`` parses its caption exactly as the image one does."""
    c, fake = _media_client(monkeypatch)
    asyncio.run(
        c.send_document_bytes(
            "447700900000@s.whatsapp.net", b"x", "a.pdf", caption="for @447700900000"
        )
    )
    document = fake.sent[0].documentMessage
    assert document.caption == "for @447700900000"
    assert document.contextInfo.mentionedJID == []
    assert document.contextInfo.groupMentions == []


def test_send_document_bytes_raises_when_not_connected():
    c = WhatsAppClient("/tmp/none.db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(c.send_document_bytes("447700900000@s.whatsapp.net", b"x", "a.pdf"))


# ── the preview builder itself ──────────────────────────────────────────────
def test_the_preview_is_a_small_jpeg():
    preview = wac._image_thumbnail(_PNG)
    assert preview.startswith(b"\xff\xd8\xff"), "WhatsApp expects a JPEG preview"
    assert len(preview) < len(_PNG) * 100


def test_the_preview_fits_inside_the_declared_box():
    from PIL import Image

    wide = io.BytesIO()
    Image.new("RGB", (1000, 500), "red").save(wide, format="PNG")
    with Image.open(io.BytesIO(wac._image_thumbnail(wide.getvalue()))) as preview:
        assert max(preview.size) <= wac._THUMBNAIL_EDGE_PX
        # Aspect ratio survives, so the preview is not a squashed 200x200.
        assert preview.size[0] > preview.size[1]


def test_a_transparent_source_is_converted_rather_than_refused():
    """JPEG carries no alpha channel, so an RGBA source has to be flattened."""
    from PIL import Image

    rgba = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(rgba, format="PNG")
    assert wac._image_thumbnail(rgba.getvalue()).startswith(b"\xff\xd8\xff")


def test_an_undecodable_source_costs_the_preview_not_the_reply():
    """A grey placeholder beats losing the whole message over a thumbnail."""
    assert wac._image_thumbnail(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) == b""
    assert wac._image_thumbnail(b"") == b""


def test_session_store_permissions_are_applied_off_the_loop(monkeypatch):
    """On Windows these resolve a SID and shell out to `icacls`, so running them
    inline would put a subprocess on the one gateway event loop.
    """
    import inspect

    src = inspect.getsource(wac.WhatsAppClient.connect)
    assert (
        "asyncio.to_thread(self._restrict_session_store)" in src
    ), "the permission setup must be handed to a worker thread"
    # And it must be AWAITED: pairing cannot begin before the credential's
    # directory is locked down.
    assert "await asyncio.to_thread(self._restrict_session_store)" in src


def test_session_store_restriction_fails_loud(monkeypatch, tmp_path):
    """Continuing after the restriction failed would pair the device and leave
    the linked-device credential readable by another local principal.
    """
    c = wac.WhatsAppClient(str(tmp_path / "whatsapp" / "session.db"))

    def boom(path):
        raise OSError("no ACL for you")

    monkeypatch.setattr(wac, "make_owner_only_dir", boom)
    with pytest.raises(OSError):
        c._restrict_session_store()


def test_the_session_store_is_behind_the_sensitive_path_keystone():
    """The store IS the credential, so the agent must not be able to read it.

    Owner-only file modes do not isolate another process running as the same UID,
    and a prompt-injected agent's file read is exactly that process. Anything that
    reads these bytes can act as the operator on WhatsApp: read every chat and
    send as them, with no second factor and nothing on the phone to notice.

    The sidecars are asserted too: SQLite's WAL and SHM hold the same key bytes,
    so protecting only the .db would leave the credential readable next to it.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.security import is_sensitive_path

    db = default_db_path(data_home())
    assert is_sensitive_path(str(db))
    assert is_sensitive_path(str(db) + "-wal")
    assert is_sensitive_path(str(db) + "-shm")
    assert is_sensitive_path(str(db.parent))
