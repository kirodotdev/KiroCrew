"""Event-log fan-out invariants: a consistent snapshot, and a closed frame.

Both are about ``EventLogHub.publish``, which runs on whatever thread appended
the log and must neither raise nor leak:

* F5 -- it iterated the LIVE subscriber set while the serving loop mutated it, so
  a subscribe or drop racing an append raised inside a sink whose caller swallows
  failures: the committed event silently never reached the subscribers.
* F12 -- when redaction failed it fell back to publishing the RAW event, which is
  the one outcome redaction exists to prevent.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from kiro_crew.dashboard.eventlog_ws import EventLogHub


class _FakeWs:
    """Enough of a WebSocketResponse for the hub's registry and close path."""

    def __init__(self, app: str = "demoapp") -> None:
        self._data = {"_app": app}
        self.closed = False
        self.close_reasons: list[bytes] = []

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    async def close(self, *, code=None, message=b""):
        self.closed = True
        self.close_reasons.append(message)

    async def send_str(self, msg):  # pragma: no cover - pump not started here
        return None


def _event(data=None):
    return {"type": "demoapp/ping", "seq": 0, "time": 1, "data": data or {"n": 1}}


# ---------------------------------------------------------------------------
# F5 -- the subscriber registry is not iterated while another thread mutates it
# ---------------------------------------------------------------------------
def test_publishing_while_subscribers_churn_never_raises_and_never_drops():
    """Stress path: one thread publishes while another subscribes and drops.

    Honest about what it can prove. On CPython ``list(a_set)`` is a single C-level
    copy that no bytecode boundary interrupts, so the finding's stated crash
    ("Set changed size during iteration") is not reachable while the GIL makes
    that copy atomic -- which is exactly the accident a free-threaded build
    removes. This case exercises the path; the case below is the one that
    discriminates, by asserting the snapshot is taken under the lock.
    """
    hub = EventLogHub(loop_provider=lambda: None)
    hub._captured_loop = None
    resident = _FakeWs()
    hub.subscribe(resident, "member", "alice")

    errors: list[BaseException] = []
    stop = threading.Event()

    def _churn() -> None:
        try:
            while not stop.is_set():
                ws = _FakeWs()
                hub.subscribe(ws, "member", "alice")
                hub.drop(ws)
        except BaseException as exc:  # pragma: no cover - the bug's signature
            errors.append(exc)

    def _publish() -> None:
        try:
            for _ in range(3000):
                hub.publish("member", "alice", _event())
        except BaseException as exc:  # pragma: no cover - the bug's signature
            errors.append(exc)

    churn = threading.Thread(target=_churn)
    churn.start()
    try:
        _publish()
    finally:
        stop.set()
        churn.join(timeout=10)

    assert not errors, f"publish raced the registry: {errors!r}"


def test_the_publish_snapshot_is_taken_while_the_registry_lock_is_held():
    """The invariant the fix installs, asserted by BEHAVIOUR rather than by a grep.

    A set that refuses to be iterated unless the lock is held fails the moment the
    snapshot moves back outside it, on any interpreter -- including one where
    ``list(a_set)`` is atomic and the crash the finding described cannot happen.
    """
    hub = EventLogHub(loop_provider=lambda: None)
    ws = _FakeWs()
    hub.subscribe(ws, "member", "alice")

    class _LockAsserting(set):
        def __iter__(self):
            assert hub._registry_lock.locked(), (
                "the subscriber set was read without the registry lock, so a "
                "concurrent subscribe/drop can change it mid-copy"
            )
            return super().__iter__()

    hub._by_unit[("member", "alice")] = _LockAsserting({ws})
    assert hub._peers("member", "alice") == [ws]
    assert hub.subscriber_count("member", "alice") == 1


def test_the_snapshot_is_a_copy_so_a_later_drop_cannot_shrink_it():
    hub = EventLogHub(loop_provider=lambda: None)
    ws = _FakeWs()
    hub.subscribe(ws, "member", "alice")
    peers = hub._peers("member", "alice")
    hub.drop(ws)
    assert peers == [ws]
    assert hub._peers("member", "alice") == []


# ---------------------------------------------------------------------------
# F12 -- a frame that cannot be redacted is dropped, and its readers are closed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_redaction_failure_drops_the_frame_and_closes_the_subscribers(monkeypatch):
    """An unredactable frame is never published: it is dropped, readers closed."""
    from kiro_crew.eventlog import service as service_mod

    hub = EventLogHub()
    ws = _FakeWs()
    hub.subscribe(ws, "member", "alice")
    queue = hub._sockets[ws].queue

    def _boom(value, _depth=1):
        raise RecursionError("crafted payload")

    monkeypatch.setattr(service_mod, "_redact_projection_value", _boom)
    hub.publish("member", "alice", _event({"secret": "AKIAIOSFODNN7EXAMPLE"}))

    assert queue.qsize() == 0, "the unredactable frame must not be queued"
    # The close is scheduled on the serving loop; let it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ws.closed
    assert hub._sockets.get(ws) is None or hub._sockets[ws].closing


@pytest.mark.asyncio
async def test_a_redactable_frame_is_still_queued_scrubbed():
    """The guard must not turn every publish into a close."""
    hub = EventLogHub()
    ws = _FakeWs()
    hub.subscribe(ws, "member", "alice")
    hub.activate(ws, "member", "alice", [])  # finish the handshake -> live frames queue
    queue = hub._sockets[ws].queue

    hub.publish("member", "alice", _event({"token": "AKIAIOSFODNN7EXAMPLE"}))

    assert queue.qsize() == 1
    msg = queue.get_nowait()
    assert "AKIAIOSFODNN7EXAMPLE" not in msg
    assert not ws.closed


# ---------------------------------------------------------------------------
# A revoked app is denied fan-out even while its socket is still registered,
# closing the subscribe-races-revoke (F1) and replacement-window (F3) races.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_skips_a_revoked_apps_socket(monkeypatch):
    """A subscription that raced in during a revoke window receives no events.

    Registration and revocation can interleave: a socket can register between a
    grant fence check and its registration, or during an app replacement. The
    registry cannot know that, so fan-out re-checks the revocation tombstone at
    delivery and drops a revoked app's socket -- the app is denied regardless of
    when it registered.
    """
    from kiro_crew.eventlog import grants

    hub = EventLogHub()
    live = _FakeWs(app="liveapp")
    revoked = _FakeWs(app="goneapp")
    hub.subscribe(live, "member", "alice")
    hub.subscribe(revoked, "member", "alice")
    hub.activate(live, "member", "alice", [])
    hub.activate(revoked, "member", "alice", [])
    live_q = hub._sockets[live].queue
    revoked_q = hub._sockets[revoked].queue

    # goneapp is torn down / mid-replacement: the tombstone is set even though its
    # socket is still in the registry (the close has not yet landed).
    monkeypatch.setattr(grants, "is_revoked", lambda app: app == "goneapp")

    hub.publish("member", "alice", _event())

    assert live_q.qsize() == 1, "a live-granted app must still receive the event"
    assert revoked_q.qsize() == 0, "a revoked app must not receive fan-out"


# ---------------------------------------------------------------------------
# F1 -- the REPLAY path re-checks authorization at activation. The replay list
# is captured in `subscribe` before several awaits (off-loop reads + the ack
# send); a grant revoked during that handshake must not be handed the replay,
# the same gate live fan-out applies per frame.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_denies_replay_to_an_app_revoked_mid_handshake(monkeypatch):
    """A revoke that lands between subscribe and activate drops the replay.

    `subscribe` captured the replay before its off-loop reads and the ack; by the
    time `activate` runs the app has been revoked. The captured replay must NOT be
    enqueued, and the socket is torn down -- its recovery is reconnect + re-auth.
    """
    from kiro_crew.eventlog import grants

    hub = EventLogHub()
    ws = _FakeWs(app="goneapp")
    hub.subscribe(ws, "member", "alice")
    queue = hub._sockets[ws].queue

    monkeypatch.setattr(grants, "is_revoked", lambda app: app == "goneapp")

    # Replay captured during the (now-stale) handshake.
    hub.activate(ws, "member", "alice", [_event(), _event()])

    assert queue.qsize() == 0, "a revoked app must not receive the captured replay"
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ws.closed, "the revoked subscription is torn down"


@pytest.mark.asyncio
async def test_activate_denies_replay_when_the_captured_fence_went_stale(monkeypatch):
    """A disable (incl. cross-process file-only) after subscribe drops the replay.

    The socket captured `_app_fence` at subscribe; a disable landing before
    activate moves the current `grant_fence` so `fence_admits` denies. The replay
    must not be enqueued and the socket is closed -- mirroring the per-frame fence
    gate in `_live_peers`.
    """
    from kiro_crew.eventlog import grants

    hub = EventLogHub()
    ws = _FakeWs(app="demoapp")
    ws["_app_fence"] = 100  # fence captured at subscribe time
    hub.subscribe(ws, "member", "alice")
    queue = hub._sockets[ws].queue

    monkeypatch.setattr(grants, "is_revoked", lambda app: False)
    # Current fence advanced past the captured one (a disable landed mid-handshake).
    monkeypatch.setattr(grants, "grant_fence", lambda app: 200)

    hub.activate(ws, "member", "alice", [_event()])

    assert queue.qsize() == 0, "a stale-fence subscription must not receive the replay"
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ws.closed, "the stale-fence subscription is torn down"


@pytest.mark.asyncio
async def test_activate_delivers_replay_when_the_app_is_still_granted(monkeypatch):
    """The gate must not turn every activation into a drop.

    A still-granted app whose captured fence matches the current one receives its
    full replay and is not closed.
    """
    from kiro_crew.eventlog import grants

    hub = EventLogHub()
    ws = _FakeWs(app="demoapp")
    ws["_app_fence"] = 100
    hub.subscribe(ws, "member", "alice")
    queue = hub._sockets[ws].queue

    monkeypatch.setattr(grants, "is_revoked", lambda app: False)
    monkeypatch.setattr(grants, "grant_fence", lambda app: 100)  # unchanged

    hub.activate(ws, "member", "alice", [_event(), _event()])

    assert queue.qsize() == 2, "a still-granted app receives its full replay"
    await asyncio.sleep(0)
    assert not ws.closed
