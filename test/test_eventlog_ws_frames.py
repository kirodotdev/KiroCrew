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

    def activate(self, ws, kind, unit_id, events):
        self.calls.append(("activate", kind, unit_id, tuple(e.get("seq") for e in events)))

    def drop(self, ws):
        self.calls.append(("drop",))


class _FakeUnit:
    id_field = "memberId"

    def __init__(self, last_seq: int = 7, events: list | None = None) -> None:
        self._last_seq = last_seq
        # Oldest-first events the catch-up read returns; default to none so the
        # tests that do not exercise replay keep their existing call sequence.
        self._events = events or []

    def service(self):
        unit = self

        class _Svc:
            def last_seq(self, unit_id):
                return unit._last_seq

            def events_after(self, unit_id, *, after=-1, limit=200):
                return [e for e in unit._events if e.get("seq", -1) > after][:limit]

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
    the catch-up replay is enqueued next, ahead of any queued live frame; releasing
    the pump last means nothing is written ahead of the ``subscribed`` frame that
    carries the client's baseline.
    """
    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=7))

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    assert hub.calls == [
        ("subscribe", "member", "alice"),
        ("activate", "member", "alice", ()),
        ("start_pump",),
    ]
    assert len(ws.sent) == 1
    data = ws.sent[0]["data"]
    assert data["lastSeq"] == 7
    # Both the generic ``id`` and the unit's own field name, so a contributor can
    # read whichever its schema uses.
    assert data["id"] == "alice"
    assert data["memberId"] == "alice"


@pytest.mark.asyncio
async def test_the_handshake_replays_the_gap_between_the_folded_cursor_and_now(monkeypatch):
    """A client folded through ``fromSeq`` is replayed everything after it.

    This is the fix for the catch-up/live gap: an append that landed between the
    client's HTTP catch-up read and this subscribe is neither in the fold nor in
    the live queue, so the handshake reads the tail after ``fromSeq`` and enqueues
    it ahead of live delivery. The replay is enqueued AFTER ``subscribe`` (so a
    racing append queues behind it) and BEFORE ``start_pump`` (so the pump drains
    replay-then-live).
    """
    hub, ws = _FakeHub(), _FakeWs()
    events = [{"seq": 5, "type": "a/x", "data": {}}, {"seq": 6, "type": "a/y", "data": {}}]
    _install(
        monkeypatch,
        hub=hub,
        resolve=lambda kind, id_: _FakeUnit(last_seq=6, events=events),
    )

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 4}},
    )

    assert hub.calls == [
        ("subscribe", "member", "alice"),
        ("activate", "member", "alice", (5, 6)),
        ("start_pump",),
    ]
    assert ws.sent[0]["data"]["lastSeq"] == 6


@pytest.mark.asyncio
async def test_a_client_already_current_gets_no_replay(monkeypatch):
    """``fromSeq`` at or beyond ``lastSeq`` means nothing to bridge, so no replay."""
    hub, ws = _FakeHub(), _FakeWs()
    events = [{"seq": 5, "type": "a/x", "data": {}}, {"seq": 6, "type": "a/y", "data": {}}]
    _install(
        monkeypatch,
        hub=hub,
        resolve=lambda kind, id_: _FakeUnit(last_seq=6, events=events),
    )

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 6}},
    )

    assert hub.calls == [
        ("subscribe", "member", "alice"),
        ("activate", "member", "alice", ()),
        ("start_pump",),
    ]


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
    assert not any(c[0] == "activate" for c in hub.calls)


@pytest.mark.asyncio
async def test_a_gap_wider_than_the_cap_is_not_replayed_inline(monkeypatch):
    """A large gap must NOT be bulk-replayed: it would overflow the socket queue.

    The regression this guards: replaying a log hundreds of events past the cursor
    pushed more frames than the queue holds and disconnected the subscriber on every
    attempt. The bridge is capped strictly below the queue; a wider gap replays
    nothing inline and the client folds the tail from the HTTP catch-up read.
    """
    hub, ws = _FakeHub(), _FakeWs()
    cap = ws_mod._MAX_REPLAY_EVENTS
    # A log far past the client's cursor: gap = cap + 500, well over the ceiling.
    last = cap + 600
    events = [{"seq": s, "type": "a/x", "data": {}} for s in range(1, last + 1)]
    _install(
        monkeypatch,
        hub=hub,
        resolve=lambda kind, id_: _FakeUnit(last_seq=last, events=events),
    )

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 10}},
    )

    # Registered and pumped (not disconnected); replay is EMPTY because the gap
    # exceeds the cap, so the client recovers via the HTTP catch-up read.
    assert hub.calls == [
        ("subscribe", "member", "alice"),
        ("activate", "member", "alice", ()),
        ("start_pump",),
    ]


@pytest.mark.asyncio
async def test_a_bridged_gap_never_exceeds_the_cap(monkeypatch):
    """A gap at/under the cap is bridged, and the replay is capped at the ceiling."""
    hub, ws = _FakeHub(), _FakeWs()
    cap = ws_mod._MAX_REPLAY_EVENTS
    last = cap  # gap of exactly `cap` from fromSeq=0
    events = [{"seq": s, "type": "a/x", "data": {}} for s in range(1, last + 1)]
    _install(
        monkeypatch,
        hub=hub,
        resolve=lambda kind, id_: _FakeUnit(last_seq=last, events=events),
    )

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 0}},
    )

    replay_call = next(c for c in hub.calls if c[0] == "activate")
    assert len(replay_call[3]) <= cap, "replay must never exceed the queue-safe cap"


@pytest.mark.asyncio
async def test_no_folded_cursor_on_a_nonempty_log_signals_catch_up_required(monkeypatch):
    """GPT ws.py:638 -- a subscriber that folded nothing must be TOLD it is behind.

    Without a ``fromSeq`` the handshake replays nothing (bulk-replaying an
    established log would overflow the queue), but a documented client that
    expected the socket to carry it forward would then silently miss every event
    already in the log. The ack now carries ``catchUpRequired: True`` so the client
    knows to page the tail from the HTTP catch-up read before trusting the live
    stream as complete.
    """
    hub, ws = _FakeHub(), _FakeWs()
    events = [{"seq": 5, "type": "a/x", "data": {}}, {"seq": 6, "type": "a/y", "data": {}}]
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=6, events=events))

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    data = ws.sent[0]["data"]
    assert data["catchUpRequired"] is True, "a cursor-less subscribe on a live log must signal it"
    # And nothing was replayed inline (the client pages it over HTTP).
    replay_call = next(c for c in hub.calls if c[0] == "activate")
    assert replay_call[3] == ()


@pytest.mark.asyncio
async def test_a_gap_beyond_the_replay_cap_signals_catch_up_required(monkeypatch):
    """A folded cursor whose gap exceeds the inline cap replays nothing and must
    signal catch-up rather than leave the client silently short."""
    cap = ws_mod._MAX_REPLAY_EVENTS
    hub, ws = _FakeHub(), _FakeWs()
    last = cap + 50
    events = [{"seq": s, "type": "a/x", "data": {}} for s in range(1, last + 1)]
    _install(
        monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=last, events=events)
    )

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 0}},
    )

    data = ws.sent[0]["data"]
    assert data["catchUpRequired"] is True, "a gap beyond the cap must signal catch-up"
    replay_call = next(c for c in hub.calls if c[0] == "activate")
    assert replay_call[3] == (), "nothing is replayed inline when the gap exceeds the cap"


@pytest.mark.asyncio
async def test_a_fully_bridged_cursor_does_not_signal_catch_up(monkeypatch):
    """A folded cursor replayed all the way to lastSeq is current -- no catch-up."""
    hub, ws = _FakeHub(), _FakeWs()
    events = [{"seq": 5, "type": "a/x", "data": {}}, {"seq": 6, "type": "a/y", "data": {}}]
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=6, events=events))

    await ws_mod._handle_eventlog_frame(
        ws,
        "some-app",
        "eventlog_subscribe",
        {"data": {"kind": "member", "id": "alice", "fromSeq": 4}},
    )

    data = ws.sent[0]["data"]
    assert data["catchUpRequired"] is False, "a fully-bridged cursor is current"


@pytest.mark.asyncio
async def test_an_empty_log_needs_no_catch_up(monkeypatch):
    """A cursor-less subscribe to a log with no events (lastSeq < 0) is current --
    the live stream is the whole story, so no catch-up is signalled."""
    hub, ws = _FakeHub(), _FakeWs()
    _install(monkeypatch, hub=hub, resolve=lambda kind, id_: _FakeUnit(last_seq=-1, events=[]))

    await ws_mod._handle_eventlog_frame(
        ws, "some-app", "eventlog_subscribe", {"data": {"kind": "member", "id": "alice"}}
    )

    data = ws.sent[0]["data"]
    assert data["catchUpRequired"] is False, "an empty log needs no catch-up"
