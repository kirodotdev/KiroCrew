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
    queue = hub._sockets[ws].queue

    hub.publish("member", "alice", _event({"token": "AKIAIOSFODNN7EXAMPLE"}))

    assert queue.qsize() == 1
    msg = queue.get_nowait()
    assert "AKIAIOSFODNN7EXAMPLE" not in msg
    assert not ws.closed
