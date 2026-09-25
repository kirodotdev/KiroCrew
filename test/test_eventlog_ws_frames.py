"""Tests for the ``eventlog_subscribe`` / ``eventlog_unsubscribe`` socket frames.

``ws._handle_eventlog_frame`` is the contribution protocol's §3 handshake on the
dashboard websocket. Two properties make it worth testing at this seam rather
than end to end:

* **A refusal answers on the socket, it does not close it.** The same socket
  multiplexes everything else the app uses, so dropping it to report a bad
  ``kind`` would take unrelated traffic down with it. Every refusal path below
  asserts an ``eventlog_subscribed`` frame carrying ``error`` and no ``lastSeq``.
* **The order of the happy path is the contract.** The hub registers the socket
  for fan-out BEFORE ``lastSeq`` is read and the ``subscribed`` frame is written,
  so an append racing the handshake is queued rather than lost; the pump is only
  released afterwards. A test that just checked "subscribed was sent" would pass
  on an implementation that loses that race, so the assertions here are on the
  ORDER of the recorded calls.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import ws as ws_mod
from kiro_crew.dashboard.eventlog_ws import WS_SUBSCRIBED, SubscriptionLimit
from kiro_crew.eventlog import grants
from kiro_crew.eventlog.contrib import ContribError


class _FakeWs:
    """Records what the handler writes, and can fail the write on demand."""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self._fail_send = fail_send

    async def send_json(self, payload: dict) -> None:
        if self._fail_send:
            raise ConnectionResetError("socket died mid-handshake")
        self.sent.append(payload)


class _FakeHub:
    """Records hub calls in order, so the handshake's SEQUENCE can be asserted."""

    def __init__(self, *, subscribe_raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._subscribe_raises = subscribe_raises

    def subscribe(self, ws, kind, unit_id):
        self.calls.append(("subscribe", kind, unit_id))
        if self._subscribe_raises is not None:
            raise self._subscribe_raises

    def unsubscribe(self, ws, kind, unit_id):
        self.calls.append(("unsubscribe", kind, unit_id))

    def start_pump(self, ws):
        self.calls.append(("start_pump",))

    def drop(self, ws):
        self.calls.append(("drop",))


class _FakeUnit:
    id_field = "memberId"

    def __init__(self, last_seq: int = 7) -> None:
        self._last_seq = last_seq

    def service(self):
        unit = self

        class _Svc:
            def last_seq(self, unit_id):
                return unit._last_seq

        return _Svc()


def _install(monkeypatch, *, hub, resolve=None, may_use_kind=True):
    """Point the handler's late imports at the fakes."""
    import kiro_crew.dashboard.eventlog_ws as elws
    import kiro_crew.eventlog.contrib as contrib

    monkeypatch.setattr(elws, "get_hub", lambda: hub)
    monkeypatch.setattr(grants, "may_use_kind", lambda app, kind: may_use_kind)
    if resolve is not None:
        monkeypatch.setattr(contrib, "resolve_unit", resolve)


def _only_refusal(ws):
    assert len(ws.sent) == 1, ws.sent
    frame = ws.sent[0]
    assert frame["type"] == WS_SUBSCRIBED
    # A refusal carries the machine-readable code and NO cursor: a client that
    # read lastSeq off a refusal would fold from a baseline it never received.
    assert "lastSeq" not in frame["data"]
    assert frame["data"]["error"]
    return frame["data"]


@pytest.mark.asyncio
async def test_a_dashboard_session_is_told_this_channel_is_for_app_tokens(monkeypatch):
    """An owner surface gets ``member_projection`` frames, not the delta channel.

    Refused rather than silently ignored, because a dashboard client that asked
    would otherwise wait forever for a ``subscribed`` that is never coming.
    """
    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub)

    await ws_mod._handle_eventlog_frame(ws, "", "eventlog_subscribe", {"data": {"kind": "member"}})

    assert _only_refusal(ws)["code"] == "unit_kind_not_granted"
    assert hub.calls == []


@pytest.mark.asyncio
async def test_an_unsubscribe_drops_the_registration_and_answers_nothing(monkeypatch):
    """Unsubscribe is fire-and-forget: there is no frame to wait for."""
    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_unsubscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert hub.calls == [("unsubscribe", "member", "alice")]
    assert ws.sent == []


@pytest.mark.asyncio
async def test_a_kind_the_manifest_does_not_grant_is_refused(monkeypatch):
    """The grant is checked BEFORE the unit is resolved.

    Resolving first would let an ungranted app probe which unit ids exist by
    reading the refusal code apart.
    """
    hub, ws = _FakeHub(), _FakeWs()
    resolved: list[tuple] = []

    def _resolve(kind, id_):
        resolved.append((kind, id_))
        return _FakeUnit()

    _install(monkeypatch, hub=hub, resolve=_resolve, may_use_kind=False)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert _only_refusal(ws)["code"] == "unit_kind_not_granted"
    assert resolved == []
    assert hub.calls == []


@pytest.mark.asyncio
async def test_a_contrib_refusal_keeps_its_own_code(monkeypatch):
    """The contract's code is the contributor's branch point, so it passes through
    rather than being flattened into one generic failure."""
    hub, ws = _FakeHub(), _FakeWs()

    def _resolve(kind, id_):
        raise ContribError("unit_not_found", "invalid memberId: no such member")

    _install(monkeypatch, hub=hub, resolve=_resolve)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "nope"}}
    )

    data = _only_refusal(ws)
    assert data["code"] == "unit_not_found"
    assert "no such member" in data["error"]
    assert hub.calls == []


@pytest.mark.asyncio
async def test_an_unexpected_resolver_failure_reads_as_not_found(monkeypatch):
    """An internal error must not leak its text to a contributor.

    It is reported as ``unit_not_found`` -- the honest outside-visible answer --
    with the real cause left in the debug log.
    """
    hub, ws = _FakeHub(), _FakeWs()

    def _resolve(kind, id_):
        raise RuntimeError("registry table is wedged")

    _install(monkeypatch, hub=hub, resolve=_resolve)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    data = _only_refusal(ws)
    assert data["code"] == "unit_not_found"
    assert "registry table is wedged" not in data["error"]


@pytest.mark.asyncio
async def test_hitting_the_subscription_limit_is_refused_not_dropped(monkeypatch):
    """A busy app is told it is at its cap; the socket stays up."""
    hub = _FakeHub(subscribe_raises=SubscriptionLimit("too many subscriptions for this socket"))
    ws = _FakeWs()
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit())

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    data = _only_refusal(ws)
    assert data["code"] == "unit_kind_not_granted"
    assert "too many subscriptions" in data["error"]
    assert ("start_pump",) not in hub.calls


@pytest.mark.asyncio
async def test_the_handshake_registers_before_it_answers_and_pumps_last(monkeypatch):
    """The ORDER is the contract, which is why this asserts the call sequence.

    Registering for fan-out first means an append racing the handshake is queued;
    releasing the pump last means the queued append cannot be written ahead of the
    ``subscribed`` frame that carries the client's baseline.
    """
    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=7))

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert hub.calls == [("subscribe", "member", "alice"), ("start_pump",)]
    assert len(ws.sent) == 1
    data = ws.sent[0]["data"]
    assert data["lastSeq"] == 7
    # Both the generic ``id`` and the unit's own field name, so a contributor can
    # read whichever its schema uses.
    assert data["id"] == "alice"
    assert data["memberId"] == "alice"


@pytest.mark.asyncio
async def test_a_socket_that_dies_mid_handshake_is_not_left_registered(monkeypatch):
    """Otherwise the hub keeps queueing frames for a dead socket and holds its pump.

    The unsubscribe after the failed write is the whole point: without it the leak
    is invisible until the gateway's memory shows it.
    """
    hub, ws = _FakeHub(), _FakeWs(fail_send=True)
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit())

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert hub.calls == [
        ("subscribe", "member", "alice"),
        ("unsubscribe", "member", "alice"),
    ]
    assert ("start_pump",) not in hub.calls
