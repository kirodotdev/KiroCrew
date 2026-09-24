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


@pytest.mark.asyncio
async def test_the_grant_cache_is_warmed_off_loop_before_the_first_check(monkeypatch):
    """A frame on an established socket never passes through the HTTP auth middleware.

    That middleware is where the HTTP side warms the contribution grant, so without a
    warm of its own this handler makes the FIRST grant lookup of an app's session --
    and a cold lookup resolves ``installed.json`` and the app manifest inline, on the
    serving loop, stalling every other task.

    The ORDER is the assertion: warming after the check buys nothing, because the
    check is what does the blocking read. Note it is warmed once, before the first
    check only -- the second check further down must stay synchronous with its
    registration, and its own comment explains that an await between them reopens the
    revocation race it exists to close.
    """
    hub, ws = _FakeHub(), _FakeWs()
    order: list[str] = []

    async def _recording_warm(app):
        order.append(f"warm:{app}")

    monkeypatch.setattr(grants, "warm", _recording_warm)
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit())
    # AFTER `_install`, which sets `may_use_kind` itself -- patched before it, this
    # recorder is silently clobbered and `order` never sees a check at all, which
    # would leave the ordering assertion below measuring nothing.
    monkeypatch.setattr(grants, "may_use_kind", lambda app, kind: order.append("check") is None)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert "check" in order, "the grant check never ran, so the order proves nothing"
    assert order[0] == "warm:some-app", (
        f"the first grant check ran before the cache was warmed (order={order}), so the "
        "cold resolve happens on the serving loop"
    )
    # Warmed ONCE, not before each check: the second check is deliberately adjacent to
    # its registration.
    assert order.count("warm:some-app") == 1, order


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


@pytest.mark.asyncio
async def test_both_outcomes_of_a_subscription_decision_are_audited(monkeypatch):
    """A subscription opens a durable read of a unit's whole event stream to
    third-party code, so "who was allowed to read what" is the half an auditor
    needs -- and a deny-only log cannot answer it. Both outcomes are recorded on
    the same ``contribution_protocol`` stream the REST surface writes to, so one
    query covers both surfaces.
    """
    import kiro_crew.dashboard.handlers.eventlog as elh

    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        elh,
        "_audit",
        lambda app, operation, outcome, resources, error="": seen.append((app, outcome, resources)),
    )

    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub, may_use_kind=False)
    await ws_mod._handle_eventlog_frame(
        ws, "mochi", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )
    assert seen == [("mochi", "denied", "member/alice")], seen

    seen.clear()
    hub2, ws2 = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub2, resolve=lambda kind, unit_id: _FakeUnit(), may_use_kind=True)
    await ws_mod._handle_eventlog_frame(
        ws2, "mochi", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )
    assert seen == [("mochi", "allowed", "member/alice")], seen


@pytest.mark.asyncio
async def test_a_grant_revoked_during_the_resolve_refuses_the_subscription(monkeypatch):
    """The first grant check sits upstream of a real await, so it can go stale.

    ``resolve_unit`` runs off-loop, and teardown revokes the grant and closes the
    app's sockets in that window. The close cannot see a subscription that does not
    exist yet, so a continuation trusting only the first check registers one the
    sweep already passed -- a live read for an app whose grant is gone. The resolver
    here revokes mid-call, which is exactly that interleaving.
    """
    hub, ws = _FakeHub(), _FakeWs()
    revoked = False

    def _resolve(kind, id_):
        nonlocal revoked
        revoked = True  # teardown lands while this continuation is suspended
        return _FakeUnit()

    _install(monkeypatch, hub=hub, resolve=_resolve)
    monkeypatch.setattr(grants, "may_use_kind", lambda app, kind: not revoked)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    data = _only_refusal(ws)
    assert data["code"] == "unit_kind_not_granted"
    assert hub.calls == [], f"a subscription was registered for a revoked app: {hub.calls}"


@pytest.mark.asyncio
async def test_a_grant_that_survives_the_resolve_still_subscribes(monkeypatch):
    """CONTROL. The re-check is conditional on the revocation, not on the resolve.

    Without this, a second check that refused unconditionally would satisfy the
    test above while making every subscription impossible.
    """
    hub, ws = _FakeHub(), _FakeWs()
    calls: list[int] = []

    def _may(app, kind):
        calls.append(1)
        return True

    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=7))
    monkeypatch.setattr(grants, "may_use_kind", _may)

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert hub.calls == [("subscribe", "member", "alice"), ("start_pump",)]
    assert ws.sent[0]["data"]["lastSeq"] == 7
    # Asked TWICE, which is the whole mechanism: one check cannot span the await.
    assert len(calls) == 2, f"the grant was asked {len(calls)} time(s), not twice"
