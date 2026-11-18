"""The owner's redaction OFF reaches only what the owner alone reads, at every dashboard edge.

With the setting off the chat turn produces its text as written (``output_seam(CHAT)``).
An owner socket, the owner's chat SSE stream and the owner's history read get it so; a
socket, a stream or a read that is not the owner's gets it redacted in full
(``dashboard.redaction_audience``). With the setting on every reader gets it redacted.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import redaction_seams
from kiro_crew.dashboard import redaction_audience
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, finished

_SLOT = "audience-slot"
_CREDENTIAL = github_classic(seed=37)
_TEXT = f"the token is {_CREDENTIAL} and that is all"
#: The reply streamed in slices, the credential cut across two of them.
_SLICES = ("the token is ", _CREDENTIAL[:12], _CREDENTIAL[12:], " and that is all")


@pytest.fixture(params=[True, False], ids=["off", "on"])
def off(request, monkeypatch):
    """The owner's setting, as the gateway publishes it."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", request.param)
    return request.param


def _produced(text: str) -> str:
    """*text* as the chat turn produces it: its credential pass inside the CHAT seam."""
    with redaction_seams.output_seam(redaction_seams.CHAT):
        return redact(text)


def _streamed(slices: tuple[str, ...]) -> list[str]:
    """*slices* as the chat turn streams them: its stream redactor inside the CHAT seam."""
    stream = StreamRedactor()
    with redaction_seams.output_seam(redaction_seams.CHAT):
        wire = [stream.feed(part) for part in slices]
        wire.append(stream.flush())
    return [part for part in wire if part]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_SLOT, origin=SlotOrigin.USER)
    return state


# ── the websocket send chokepoint ──


class _Socket:
    """A dashboard-user socket that records what it is sent."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self._items: dict = {"_is_dashboard_user": True}

    def get(self, key, default=None):
        return self._items.get(key, default)

    def __setitem__(self, key, value) -> None:
        self._items[key] = value

    def pop(self, key, default=None):
        return self._items.pop(key, default)

    async def send_str(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    def streamed(self) -> str:
        return "".join(f["data"]["content"] for f in self.sent if f["type"] == "chat_chunk")

    def text(self) -> str:
        return json.dumps(self.sent)


async def _settle(state) -> None:
    for _ in range(3):
        await asyncio.sleep(0)
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_websocket_frame_is_raw_only_on_an_owner_socket_with_redaction_off(state, off):
    owner, teammate = _Socket(), _Socket()
    state._ws_clients.extend([owner, teammate])
    state._owner_ws_clients.add(owner)

    for seq, part in enumerate(_streamed(_SLICES), start=1):
        state.broadcast_ws("chat_chunk", {"slot": _SLOT, "content": part, "seq": seq, "gen": 1})
    state.broadcast_ws(
        "chat_message", {"slot": _SLOT, "role": "assistant", "content": _produced(_TEXT)}
    )
    await _settle(state)

    assert (_CREDENTIAL in owner.streamed()) is off
    assert (_CREDENTIAL in owner.text()) is off
    # A credential cut across two slices is caught whole, and the reply still reads.
    assert _CREDENTIAL not in teammate.streamed()
    assert _CREDENTIAL not in teammate.text()
    assert teammate.streamed().startswith("the token is ")
    assert [f["type"] for f in teammate.sent] == [f["type"] for f in owner.sent]


@pytest.mark.asyncio
async def test_a_subagent_replay_is_raw_only_on_an_owner_socket_with_redaction_off(state, off):
    from kiro_crew.dashboard.ws_subagent_send import _send_subagent_replay

    frame = {"type": "subagent_update", "data": {"slot": _SLOT, "text": _produced(_TEXT)}}
    owner, teammate = _Socket(), _Socket()
    state._owner_ws_clients.add(owner)
    state._ws_client_allowed = lambda *_args, **_kwargs: True
    for ws in (owner, teammate):
        await _send_subagent_replay(ws, state, [frame])

    assert (_CREDENTIAL in owner.text()) is off
    assert _CREDENTIAL not in teammate.text()


# ── the chat SSE writer ──


def _as_caller(user: str):
    """A layer outer to the test auth that names who sent the request."""

    @web.middleware
    async def caller(request, handler):
        request["user"] = user
        request["app"] = ""
        return await handler(request)

    return caller


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_chat_sse_stream_is_raw_only_for_the_owner_with_redaction_off(
    state, off, monkeypatch, user
):
    async def reply(
        st,
        slot,
        message,
        *,
        _directive_user_origin,
        _directive_owner_origin=False,
        _stop_hold_stamp="",
    ):
        for part in _streamed(_SLICES):
            slot.append("chunk", part, "chunk")
        slot.append("assistant", _produced(_TEXT))
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", reply)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", json={"slot": _SLOT, "message": "go on"})
        assert response.status == 200
        body = await finished(response.text(), "response.text", timeout=FALLBACK_TIMEOUT_SECS)
    events = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    streamed = "".join(e["content"] for e in events if e["type"] == "chunk")
    replies = [e["content"] for e in events if e["type"] == "assistant"]
    assert body.rstrip().endswith("data: [DONE]")
    assert len(replies) == 1
    raw = off and user == "local-app"
    assert (_CREDENTIAL in streamed) is raw
    assert (_CREDENTIAL in replies[0]) is raw
    assert streamed.endswith(" and that is all")


# ── HTTP reads ──


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["local-app", "teammate-7"], ids=["owner", "teammate"])
async def test_a_history_read_is_raw_only_for_the_owner_with_redaction_off(state, off, user):
    # A row the turn saved: as written with the setting off.
    state.get_slot(_SLOT).append("assistant", _produced(_TEXT))
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    # Where the server chains put it: right after the layer that authenticates.
    app.middlewares.insert(2, redaction_audience.redaction_audience_middleware)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{_SLOT}")
        assert response.status == 200
        body = await response.text()
    assert "the token is " in body
    assert (_CREDENTIAL in body) is (off and user == "local-app")


# ── who the reader is ──


class _Request(dict):
    def __init__(self, owner_id: str = "", **claims) -> None:
        super().__init__(claims)
        self.app = {"state": type("S", (), {"owner_id": owner_id})()}


@pytest.mark.parametrize(
    ("request_", "audience"),
    [
        (_Request(user="local-app", app=""), redaction_seams.OWNER_DASHBOARD),
        (_Request("U-OWNER", user="U-OWNER", app=""), redaction_seams.OWNER_DASHBOARD),
        (_Request("U-OWNER", user="U-TEAMMATE", app=""), redaction_seams.NON_OWNER),
        (_Request(user="local-app", app="some-app"), redaction_seams.NON_OWNER),
        # The crew's own agent, through the internal secret: its answer goes to the model.
        (_Request(internal_auth=True), redaction_seams.MODEL),
        (_Request(internal_auth=True, app="some-app"), redaction_seams.NON_OWNER),
        (_Request(), redaction_seams.NON_OWNER),
    ],
    ids=["local-owner", "owner", "teammate", "app", "agent", "app-agent", "anonymous"],
)
def test_the_audience_of_a_request(request_, audience):
    assert redaction_audience.request_audience(request_) == audience


def test_a_request_the_owner_check_fails_on_reads_as_non_owner():
    broken = _Request(user="local-app", app="")
    broken.app = {}  # no state: the check raises
    assert redaction_audience.request_audience(broken) == redaction_seams.NON_OWNER


def test_n_non_owner_sockets_cost_one_redaction(monkeypatch):
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    calls = []
    real = redaction_audience._in_full
    monkeypatch.setattr(
        redaction_audience, "_in_full", lambda fn, *args: calls.append(fn) or real(fn, *args)
    )
    text = json.dumps({"type": "chat_message", "data": {"slot": _SLOT, "content": _TEXT}})
    shown = {redaction_audience.for_socket(_Socket(), text, owner=False) for _ in range(5)}
    assert len(shown) == 1 and _CREDENTIAL not in shown.pop()
    assert len(calls) == 1


# ── the stream across a change of the setting, and the frame memo's bound ──


def _chunk(content: str) -> str:
    return json.dumps({"type": "chat_chunk", "data": {"slot": _SLOT, "content": content}})


def test_turning_redaction_on_mid_reply_releases_no_credential_to_a_non_owner(monkeypatch):
    """The turn's stream redactor passed the head of a header through while the setting
    was off, and starts over once it is on; the non-owner socket's own redactor must
    still read the header and its value together."""
    socket = _Socket()
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    head = redaction_audience.for_socket(socket, _chunk("Authorization: Bearer "), owner=False)
    monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    token = "Q7mVx9pLw2Kd8Rt4Ny6sB3cF"
    tail = redaction_audience.for_socket(socket, _chunk(f"{token} then more"), owner=False)
    shown = "".join(json.loads(frame)["data"]["content"] for frame in (head, tail) if frame)
    assert token not in shown
    # The owner's own socket reads what the turn sent, as it is.
    assert redaction_audience.for_socket(_Socket(), _chunk(token), owner=True) == _chunk(token)


def test_turning_redaction_on_mid_reply_releases_no_credential_to_a_non_owner_stream(
    monkeypatch,
):
    """A non-owner chat SSE stream's chunks keep going through its own stream redactor
    once the setting is on, as a non-owner socket's slices do."""
    events = redaction_audience.SseEvents(_Request("U-OWNER", user="U-TEAMMATE", app=""))
    token = "Q7mVx9pLw2Kd8Rt4Ny6sB3cF"
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    sent = events.events(json.dumps({"type": "chunk", "content": "Authorization: Bearer "}))
    monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    sent += events.events(json.dumps({"type": "chunk", "content": f"{token} then more"}))
    sent += events.close()
    shown = "".join(json.loads(event)["content"] for event in sent)
    assert token not in shown
    assert shown.endswith(" then more")


def test_a_frame_past_the_memo_bound_is_redacted_and_not_kept(monkeypatch):
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    monkeypatch.setattr(redaction_audience, "_frames", {})
    small = json.dumps({"type": "chat_message", "data": {"slot": _SLOT, "content": _TEXT}})
    pad = "x" * redaction_audience._MAX_FRAME_CHARS
    large = json.dumps(
        {"type": "chat_message", "data": {"slot": _SLOT, "content": f"{_TEXT} {pad}"}}
    )
    for frame in (small, large):
        shown = redaction_audience.for_socket(_Socket(), frame, owner=False)
        assert shown is not None and _CREDENTIAL not in shown
    assert list(redaction_audience._frames) == [small]


# ── caches an owner's read must not fill for everyone ──


async def _read_history(state, user: str) -> str:
    app = _make_app(state)
    app.middlewares.insert(0, _as_caller(user))
    app.middlewares.insert(2, redaction_audience.redaction_audience_middleware)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{_SLOT}")
        assert response.status == 200
        return await response.text()


@pytest.mark.asyncio
async def test_an_owners_history_read_does_not_fill_a_non_owners(state, monkeypatch):
    """The row render memo is keyed by what the rows' own CHAT seam answers, so the
    owner's read with redaction off, and the memo fill it starts, leave nothing a
    teammate's read can hit."""
    from kiro_crew.dashboard import wire_render

    monkeypatch.setattr(wire_render, "FILL_DELAY_S", 0)
    # This test's own fill table, so fills other tests left running cannot refuse it.
    monkeypatch.setattr(wire_render, "_fills", set())
    monkeypatch.setattr(wire_render, "_scopes", type(wire_render._scopes)())
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state.get_slot(_SLOT).append("assistant", _produced(_TEXT))
    assert _CREDENTIAL in await _read_history(state, "local-app")
    await async_wait_until(
        lambda: any(kind == wire_render._FILLED for kind, _n in wire_render._scopes.values()),
        timeout=FALLBACK_TIMEOUT_SECS,
        describe=lambda: dict(wire_render._scopes),
    )
    shown = await _read_history(state, "teammate-7")
    assert _CREDENTIAL not in shown
    assert "the token is " in shown


def test_an_owners_preview_does_not_fill_a_non_owners(monkeypatch):
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.security.redaction_switch import always_scan

    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    text = f"{_TEXT} (preview {id(object())})"
    assert _CREDENTIAL in chat_utils._redacted_preview(text, 200)
    with always_scan():
        assert _CREDENTIAL not in chat_utils._redacted_preview(text, 200)
        assert chat_utils.display_redaction_stamp() is not None
    assert chat_utils.display_redaction_stamp() is None


def test_an_owners_slot_list_does_not_fill_the_row_memo_a_teammate_reads(state, monkeypatch):
    """The sidebar row memos are keyed by ``display_redaction_stamp``, which names no
    pass that stands down: the owner's list with redaction off leaves nothing a
    teammate's list can hit."""
    from kiro_crew.security.redaction_switch import always_scan

    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state.get_slot(_SLOT).append("assistant", _produced(_TEXT))
    assert _CREDENTIAL in json.dumps(state.serialize_slots())
    with always_scan():
        shown = json.dumps(state.serialize_slots())
    assert _CREDENTIAL not in shown
    assert "the token is " in shown


@pytest.mark.asyncio
async def test_the_hub_keeps_a_non_owners_stream_across_turning_redaction_on(state, monkeypatch):
    owner, teammate = _Socket(), _Socket()
    state._ws_clients.extend([owner, teammate])
    state._owner_ws_clients.add(owner)
    token = "Q7mVx9pLw2Kd8Rt4Ny6sB3cF"
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state.broadcast_ws("chat_chunk", {"slot": _SLOT, "content": "Authorization: Bearer "})
    await _settle(state)
    monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    state.broadcast_ws("chat_chunk", {"slot": _SLOT, "content": f"{token} then more"})
    await _settle(state)
    assert token in owner.streamed()
    assert token not in teammate.streamed()


class _SlotState:
    """A state whose slot list says whether its pass ran with redaction standing down."""

    def __init__(self) -> None:
        self.passes = 0

    def serialize_slots(self, *, include_check_status: bool, dashboard_user: bool) -> list:
        from kiro_crew.security.redaction_switch import bypassed_in

        self.passes += 1
        return [{"raw": bypassed_in(redaction_seams.CHAT)}]


@pytest.mark.asyncio
async def test_an_owners_slot_pass_is_not_shared_with_a_non_owner(monkeypatch):
    from kiro_crew.dashboard import slot_list_pass
    from kiro_crew.security.redaction_switch import always_scan

    monkeypatch.setattr(
        slot_list_pass, "PASS_WINDOW_MIN_S", 0.5  # flake-ok: a window waited out, not a bound
    )
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    state = _SlotState()

    async def owner() -> list:
        with redaction_seams.output_seam(redaction_seams.CHAT):
            return await slot_list_pass.shared_slot_list(state)

    async def teammate() -> list:
        with always_scan():
            return await slot_list_pass.shared_slot_list(state)

    assert await owner() == [{"raw": True}]
    # A second owner read arms a trailing pass in the owner's context; a teammate who
    # asks inside its window must not be handed that pass.
    owner_rows, teammate_rows = await asyncio.gather(owner(), teammate())
    assert owner_rows == [{"raw": True}]
    assert teammate_rows == [{"raw": False}]


@pytest.mark.asyncio
async def test_a_slot_pass_a_teammate_waits_for_runs_in_full_once_redaction_is_off(monkeypatch):
    """A trailing pass runs in its first caller's context. One the owner armed with
    redaction on, and a teammate joined, must not stand down when the owner turns it
    off before the pass runs."""
    from kiro_crew.dashboard import slot_list_pass
    from kiro_crew.security.redaction_switch import always_scan

    monkeypatch.setattr(
        slot_list_pass, "PASS_WINDOW_MIN_S", 0.5  # flake-ok: a window waited out, not a bound
    )
    monkeypatch.setattr(redaction_seams, "_redaction_off", False)
    state = _SlotState()

    async def teammate() -> list:
        with always_scan():
            return await slot_list_pass.shared_slot_list(state)

    assert await slot_list_pass.shared_slot_list(state) == [{"raw": False}]
    owner = asyncio.ensure_future(slot_list_pass.shared_slot_list(state))
    await asyncio.sleep(0)
    joined = asyncio.ensure_future(teammate())
    await asyncio.sleep(0)
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    _owner_rows, teammate_rows = await asyncio.gather(owner, joined)
    assert teammate_rows == [{"raw": False}]


def test_a_socket_at_its_stream_cap_keeps_every_redactor_it_holds(monkeypatch) -> None:
    """A slice of one stream past the cap is not sent; the redactor that read a
    credential's head is kept, so its value is still masked when it arrives."""
    from kiro_crew.dashboard import redaction_audience as ra

    monkeypatch.setattr(ra, "_MAX_STREAMS_PER_SOCKET", 2)
    monkeypatch.setattr(ra.redaction_seams, "published_off", lambda: True)
    ws: dict = {}
    token = "ghp_" + "Zq8vT3mK9pLxW2nR7bC4dF6hJ1sA5eG0yU2i"

    def chunk(slot: str, text: str) -> str:
        return json.dumps({"type": "chat_chunk", "data": {"slot": slot, "content": text}})

    shown = [ra.for_socket(ws, chunk("a", "Authorization: Bearer "), owner=False)]
    shown.append(ra.for_socket(ws, chunk("b", "hello "), owner=False))
    assert ra.for_socket(ws, chunk("c", "more "), owner=False) is None
    shown.append(ra.for_socket(ws, chunk("a", token + " done"), owner=False))
    assert token not in "".join(s or "" for s in shown)


def test_a_frame_built_while_off_is_redacted_for_a_non_owner_once_on(monkeypatch) -> None:
    """A non-stream frame a producer built from what it read while the setting was off
    (a native subagent's cached task, say) reaches a non-owner socket redacted in full
    after the owner turns redaction on."""
    from kiro_crew.dashboard import redaction_audience as ra
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=181)
    frame = json.dumps({"type": "subagent_done", "data": {"slot": "s", "task": f"use {token}"}})
    monkeypatch.setattr(ra.redaction_seams, "published_off", lambda: False)
    shown = ra.for_socket({}, frame, owner=False)
    assert shown is not None and token not in shown
    assert ra.for_socket({}, frame, owner=True) == frame
