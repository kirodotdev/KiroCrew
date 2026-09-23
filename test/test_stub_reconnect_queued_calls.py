"""A call issued while the stub re-attaches must be answered, not held forever.

Failure shape pinned here: the bridge pumps are torn down for the whole of
``_reconnect``, so a request kiro-cli sends during that window is neither
forwarded nor failed -- it sits unread in the session's stdin queue until the
reattach lands or ``_RECONNECT_TOTAL_BUDGET_SECS`` (600s) runs out. A call
already in flight when the connection dropped is answered correctly by
``_split_abandoned_ids`` + ``_emit_error_frames``; only the arriving half was
silent, and raising the budget to cover a supervisor crash loop widened that
silence from one minute to ten.

The contract these tests pin is a BOUNDED wait, not fail-fast: a frame is held
for ``_QUEUED_GRACE_SECS`` so an ordinary restart forwards it and answers it for
real, and failed retryably only once that window passes. Held frames go back to
the head of kiro-cli's stream in order; failed ones are dropped, because their id
has been answered and answering twice is a protocol violation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import NamedTuple

import pytest

from kiro_crew.mcp_gateway import stub as stub_mod

#: Small enough to keep these tests fast, and the drain takes it as a parameter
#: for exactly that reason. Every timing assertion below is relative to it.
_GRACE = 0.05


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def _request(req_id, method: str = "tools/call") -> bytes:
    return _line({"jsonrpc": "2.0", "id": req_id, "method": method})


def _notification(method: str = "notifications/cancelled") -> bytes:
    return _line({"jsonrpc": "2.0", "method": method})


class _Answers:
    """Captures what the drain sends back to kiro-cli.

    Replaces ``_emit_error_frames`` rather than reading real stdout: the frames
    it writes are the observable under test, and the module-level function is the
    single place they are produced.
    """

    def __init__(self) -> None:
        self.emitted: list[tuple[list, str]] = []

    @property
    def ids(self) -> list:
        return [rid for ids, _msg in self.emitted for rid in ids]

    async def __call__(self, req_ids: list, message: str, *, pool_label: str) -> None:
        self.emitted.append((list(req_ids), message))


class _StdoutBuffer:
    """Minimal stand-in for ``sys.stdout.buffer`` collecting whole lines."""

    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def write(self, data: bytes) -> None:
        self._sink.append(data)

    def flush(self) -> None:
        pass


class _FakeStdout:
    """``sys.stdout`` whose ``.buffer`` collects what the real emit path writes."""

    def __init__(self, sink: list[bytes]) -> None:
        self.buffer = _StdoutBuffer(sink)


@pytest.fixture()
def answers(monkeypatch) -> _Answers:  # noqa: ANN001
    spy = _Answers()
    monkeypatch.setattr(stub_mod, "_emit_error_frames", spy)
    return spy


def _session(*lines: bytes, maxsize: int = stub_mod._STDIN_QUEUE_MAXSIZE) -> stub_mod.StubSession:
    """A session whose stdin queue is pre-seeded and whose reader never starts.

    Setting ``_line_q`` directly is what keeps the real reader thread off fd 0:
    these tests drive the queue that thread would fill, not the thread. The two
    cap tests raise ``maxsize`` because the real reader BLOCKS on a full queue
    while ``put_nowait`` raises, and what they measure is how many lines the drain
    takes, not how many the queue can hold.
    """
    session = stub_mod.StubSession()
    queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=maxsize)
    for line in lines:
        queue.put_nowait(line)
    session._line_q = queue
    return session


class _DrainRun(NamedTuple):
    """How a drain run ended, so a test can assert on the ending it expects."""

    reached: bool  # ``ready()`` became true before the ceiling
    ended_itself: bool  # the drain returned on its own (stdin EOF) before the cancel


async def _drain_until(
    session: stub_mod.StubSession,
    ready,  # noqa: ANN001
    ceiling: float = 5.0,
    grace_secs: float = _GRACE,
) -> _DrainRun:
    """Run the drain until ``ready()`` or ``ceiling`` seconds, then cancel it.

    Every test polls for the outcome it is about rather than sleeping a multiple of
    the grace. A fixed sleep asserts the SCHEDULE, and a runner slow enough to miss
    it turns a correct implementation red -- two Windows shards' worth, measured.

    Choose ``ready`` so it cannot become true before the step under test has
    happened. ``qsize() == 0`` says only that the line left the queue: the drain
    resumes one loop iteration later, and a cancel in that window hands the frame
    back through the salvage path, which looks exactly like a decision to hold it.
    A test measuring what the drain DID with a frame must poll that, not the queue.

    ``grace_secs`` is raised where an EXPIRY would otherwise be an equally good
    explanation of the answer being measured.
    """
    stop = asyncio.Event()
    task = asyncio.ensure_future(
        stub_mod._drain_while_disconnected(
            session, pool_label="probe:fake", grace_secs=grace_secs, stop=stop
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ceiling
    while loop.time() < deadline and not ready():
        await asyncio.sleep(_GRACE / 5)
    reached = ready()
    ended_itself = task.done()
    # Stopped the way the real caller stops it, so no test exercises an ending
    # production does not use.
    stop.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    return _DrainRun(reached=reached, ended_itself=ended_itself)


def _held(session: stub_mod.StubSession) -> list[bytes]:
    return list(session._head)


# --- (a) the grace window is derived, not picked -----------------------------


def test_grace_is_the_live_bridges_own_patience() -> None:
    """The same figure a LIVE bridge allows a silent gateway.

    Derivation rather than a chosen number: a queued call must not be held longer
    than a forwarded one would be before the peer is declared dead, and pinning
    it here is what stops the two waits drifting apart.
    """
    assert stub_mod._QUEUED_GRACE_SECS == (
        stub_mod._BRIDGE_PING_INTERVAL_SECS * stub_mod._BRIDGE_PING_MAX_MISSES
    )


def test_grace_is_a_different_scale_from_the_session_budget() -> None:
    """One CALL waiting and one SESSION waiting are priced differently.

    The session budget is minutes because giving up is irreversible; the call
    grace is seconds because failing a call retryably costs one retry. A grace
    that crept up toward the budget would restore the defect.
    """
    assert stub_mod._QUEUED_GRACE_SECS > 0.0
    assert stub_mod._QUEUED_GRACE_SECS <= stub_mod._RECONNECT_TOTAL_BUDGET_SECS / 10


# --- (b) the defect itself ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_call_arriving_while_disconnected_is_failed_after_grace(
    answers: _Answers,
) -> None:
    """A frame that arrives with no bridge to forward it still gets an answer.

    The one this whole file exists for: unanswered is the one outcome a caller
    cannot act on, so the drain must produce a response rather than hold the
    frame for as long as the reconnect runs.
    """
    session = _session(_request(41))

    run = await _drain_until(session, lambda: bool(answers.ids))

    assert run.reached, "the queued call was never answered"
    assert answers.ids == [41]
    message = answers.emitted[0][1]
    assert "Retry it." in message
    assert _held(session) == [], "an answered id must not also be forwarded"


@pytest.mark.asyncio
async def test_the_error_is_the_retryable_code_the_in_flight_half_uses(
    monkeypatch,
) -> None:  # noqa: ANN001
    """Same code and same advice as ``_split_abandoned_ids``' half.

    Deliberately does NOT take the ``answers`` fixture: this one runs the real
    ``_emit_error_frames`` and reads the bytes it puts on stdout, because a caller
    can only retry what it can recognise on the wire.
    """
    real_frames: list[bytes] = []
    monkeypatch.setattr(stub_mod.sys, "stdout", _FakeStdout(real_frames))
    session = _session(_request("call-7"))

    await _drain_until(session, lambda: bool(real_frames))

    frames = [json.loads(f) for f in real_frames if f.strip()]
    assert [f["id"] for f in frames] == ["call-7"]
    assert frames[0]["error"]["code"] == -32603
    assert "Retry it." in frames[0]["error"]["message"]


# --- (c) the bounded-wait decision ------------------------------------------


@pytest.mark.asyncio
async def test_a_call_is_not_failed_when_the_reattach_beats_grace(
    answers: _Answers,
) -> None:
    """The chosen UX: an ordinary restart serves the call rather than failing it.

    The caller cancels the drain the moment the reconnect resolves, so a
    cancellation inside the grace window stands for a reattach that landed in
    time. Nothing is answered and the frame is handed back for the new bridge to
    forward.
    """
    session = _session(_request(41))

    await _drain_until(session, lambda: session._line_q.qsize() == 0, grace_secs=60.0)

    assert answers.ids == []
    assert _held(session) == [_request(41)]


@pytest.mark.asyncio
async def test_only_the_frames_past_grace_are_failed(answers: _Answers) -> None:
    """Each frame carries its OWN deadline, measured from when it arrived."""
    session = _session(_request(1))
    stop = asyncio.Event()
    task = asyncio.ensure_future(
        stub_mod._drain_while_disconnected(
            session, pool_label="probe:fake", grace_secs=_GRACE, stop=stop
        )
    )
    # id=1 expires; id=2 arrives afterwards and still has its full grace left.
    while not answers.ids:
        await asyncio.sleep(_GRACE / 5)
    session._line_q.put_nowait(_request(2))
    while session._line_q.qsize():
        await asyncio.sleep(_GRACE / 5)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert answers.ids == [1]
    assert _held(session) == [_request(2)]


@pytest.mark.asyncio
async def test_a_continuously_ready_queue_cannot_starve_the_expiry(
    answers: _Answers,
) -> None:
    """The grace is enforced no matter how busy the stream is.

    A caller that keeps a line ready on every pass would, if the sweep ran only
    when the wait timed out, see the sweep never run at all: each pass takes the
    new line and loops. The held calls would then sit past their grace unanswered,
    which is the promise this whole function exists to keep.
    """
    session = _session(_request(1), maxsize=512)
    feeding = True

    async def _keep_it_ready() -> None:
        # Keeps a SMALL backlog rather than flooding: the condition under test is
        # "a line is ready on every pass", and outrunning the drain would only
        # fill the queue and prove nothing about the sweep.
        n = 100
        while feeding:
            if session._line_q.qsize() < 4:
                session._line_q.put_nowait(_notification(f"notifications/n{n}"))
                n += 1
            await asyncio.sleep(0)

    feeder = asyncio.ensure_future(_keep_it_ready())
    try:
        run = await _drain_until(session, lambda: 1 in answers.ids, ceiling=3.0)
    finally:
        feeding = False
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder

    assert run.reached, "the expiry never fired while the queue stayed ready"
    assert 1 in answers.ids


# --- (d) what may never be failed -------------------------------------------


@pytest.mark.asyncio
async def test_a_notification_is_never_answered_only_held(
    answers: _Answers,
) -> None:
    """A notification has no id; answering one is a protocol violation.

    Held rather than dropped, so the promise that a reconnect loses nothing
    survives this change.
    """
    session = _session(_notification())

    await _drain_until(session, lambda: session._line_q.qsize() == 0, grace_secs=60.0)

    assert answers.ids == []
    assert _held(session) == [_notification()]


@pytest.mark.asyncio
async def test_a_queued_initialize_is_never_failed(answers: _Answers) -> None:
    """The handshake replay can still answer it for real.

    Same exception ``_split_abandoned_ids`` makes for an in-flight ``initialize``,
    for the same reason: an id answered here would either contradict the replay's
    success or duplicate the terminal path's error.
    """
    session = _session(_request(7, method="initialize"))

    await _drain_until(session, lambda: session._line_q.qsize() == 0, grace_secs=60.0)

    assert answers.ids == []
    assert _held(session) == [_request(7, method="initialize")]


@pytest.mark.asyncio
async def test_an_unparseable_line_is_held_verbatim(answers: _Answers) -> None:
    """``stdin_pump`` forwards a line it cannot parse; so does this."""
    junk = b"{not json\n"
    session = _session(junk)

    await _drain_until(session, lambda: session._line_q.qsize() == 0, grace_secs=60.0)

    assert answers.ids == []
    assert _held(session) == [junk]


# --- (e) ordering and the handback ------------------------------------------


@pytest.mark.asyncio
async def test_held_frames_keep_their_order_around_a_failed_one(
    answers: _Answers,
) -> None:
    """Removing an answered frame must not reorder the ones that outlive it."""
    session = _session(
        _notification("notifications/a"), _request(41), _notification("notifications/b")
    )

    run = await _drain_until(session, lambda: bool(answers.ids))

    assert run.reached, "the expiry never fired"
    assert answers.ids == [41]
    assert _held(session) == [
        _notification("notifications/a"),
        _notification("notifications/b"),
    ]


@pytest.mark.asyncio
async def test_next_line_serves_the_head_before_the_queue() -> None:
    """The session contract the handback relies on."""
    session = _session(_request(2))
    session.push_front([_request(1)])

    first = await session.next_line()
    second = await session.next_line()

    assert [first, second] == [_request(1), _request(2)]


@pytest.mark.asyncio
async def test_push_front_preserves_order_across_two_handbacks() -> None:
    """A second handback goes in FRONT of the first, still in arrival order."""
    session = _session()
    session.push_front([_request(3), _request(4)])
    session.push_front([_request(1), _request(2)])

    got = [await session.next_line() for _ in range(4)]

    assert got == [_request(1), _request(2), _request(3), _request(4)]


@pytest.mark.asyncio
async def test_a_stop_while_frames_are_in_hand_loses_nothing(
    answers: _Answers,
) -> None:
    """The drain's ``finally`` owns the handback, so stopping cannot strand a frame.

    Stopped with everything dequeued and nothing expired: every frame must come
    back, in order.
    """
    session = _session(_request(1), _notification(), _request(2))

    run = await _drain_until(session, lambda: session._line_q.qsize() == 0, grace_secs=60.0)

    assert run.reached, "not every frame had left the queue"
    assert not run.ended_itself, "the stop, not an EOF, must be what ended this"
    assert answers.ids == []
    assert _held(session) == [_request(1), _notification(), _request(2)]


@pytest.mark.asyncio
async def test_stdin_eof_during_the_window_is_handed_to_the_next_pump(
    answers: _Answers,
) -> None:
    """kiro-cli closing stdin must still read as EOF, not be swallowed.

    ``stdin_pump`` reports ``stdin_eof`` on an empty line and sends
    ``unregister``; consuming the EOF here would leave the next bridge waiting on
    a stream nobody will write to again.
    """
    session = _session(b"")

    run = await _drain_until(session, lambda: bool(session._head))

    assert run.ended_itself, "the drain stops on its own once stdin is closed"
    assert answers.ids == []
    assert _held(session) == [b""]
    assert await session.next_line() == b""


# --- (f) the terminal path ---------------------------------------------------


def test_take_pending_request_ids_reports_requests_and_drops_the_frames() -> None:
    """What the terminal exit answers when no bridge will forward the frames."""
    session = _session()
    session.push_front([_request(1), _notification(), b"{not json\n", _request(7, "initialize")])

    ids = session.take_pending_request_ids()

    assert ids == [1, 7], "a notification and a junk line have no id to answer"
    assert _held(session) == [], "the frames are dropped as their ids are taken"


def test_take_pending_request_ids_reads_the_queue_as_well_as_the_head() -> None:
    """A line still in the queue has no reader left, so it must be answered too.

    The head holds what the drain handed back; the queue holds whatever the reader
    thread put there since -- a pipelined burst, or anything arriving in the window
    between the drain being awaited and the terminal extraction. Reading only the
    head strands the rest with stdout about to close.
    """
    session = _session(_request(2), _notification(), _request(3))
    session.push_front([_request(1)])

    ids = session.take_pending_request_ids()

    assert ids == [1, 2, 3], "every request waiting anywhere must be answered"
    assert _held(session) == []
    assert session._line_q.qsize() == 0, "the queue is emptied as its ids are taken"


def test_take_pending_request_ids_survives_a_session_with_no_reader() -> None:
    """A session whose reader never started has no queue to read."""
    session = stub_mod.StubSession()
    session.push_front([_request(1)])

    assert session.take_pending_request_ids() == [1]


def test_take_pending_request_ids_is_empty_when_nothing_was_held() -> None:
    assert _session().take_pending_request_ids() == []


# --- (g) the forwarding half, through the real pump -------------------------


class _CaptureWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._mc_write_lock = asyncio.Lock()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.mark.asyncio
async def test_the_next_bridge_forwards_held_frames_before_newer_ones() -> None:
    """End to end through ``run_bridge``'s real ``stdin_pump``.

    ``stdin=None`` is the point: the injected-stdin path used by the other bridge
    tests bypasses the session, and the session is what carries the handback.
    """
    session = _session(_request(2))
    session.push_front([_request(1)])
    session._line_q.put_nowait(b"")  # end the bridge once both are forwarded

    gateway = _CaptureWriter()
    stdout_writer = _CaptureWriter()
    await asyncio.wait_for(
        stub_mod.run_bridge(
            asyncio.StreamReader(),
            gateway,  # type: ignore[arg-type]
            asyncio.Event(),
            stdout_writer=stdout_writer,  # type: ignore[arg-type]
            session=session,
        ),
        timeout=10,
    )

    forwarded = [json.loads(b) for b in gateway.written if b.strip() and "id" in json.loads(b)]
    assert [f["id"] for f in forwarded] == [1, 2]
    assert session.reason == "stdin_eof"


# --- (h) retention is bounded in both dimensions -----------------------------


def test_the_hold_bound_has_both_dimensions() -> None:
    """A count bound AND a byte bound, because either alone is a hole.

    A count bound admits ``_STDIN_QUEUE_MAXSIZE`` frames of ``_HELD_FRAME_BYTES``
    each; a byte bound leaves the per-object overhead of very many tiny frames
    unaccounted. Same pair, and the same reasoning, as gatewayd's park bound.
    """
    assert stub_mod._HELD_FRAME_BYTES == stub_mod.READ_BUFFER_LIMIT_BYTES
    assert stub_mod._HELD_TOTAL_BYTES >= stub_mod._HELD_FRAME_BYTES, (
        "one frame the reader was willing to return must never trip the "
        "aggregate bound by itself"
    )
    assert (
        stub_mod._HELD_TOTAL_BYTES >= stub_mod._DEFAULT_READ_BUFFER_LIMIT
    ), "tuning read_buffer_limit_bytes DOWN must not tighten the hold"


@pytest.mark.parametrize(
    "held_count,held_bytes,line_len,expected",
    [
        (0, 0, 10, False),
        (stub_mod._STDIN_QUEUE_MAXSIZE, 0, 10, True),
        (0, 0, stub_mod._HELD_FRAME_BYTES + 1, True),
        (0, stub_mod._HELD_TOTAL_BYTES, 1, True),
    ],
    ids=["room", "count_full", "frame_too_big", "bytes_full"],
)
def test_no_room_to_hold_trips_on_each_dimension(
    held_count: int, held_bytes: int, line_len: int, expected: bool
) -> None:
    """Each dimension is load-bearing, exercised one at a time."""
    held = [(b"x", None, 0.0)] * held_count
    assert stub_mod._no_room_to_hold(b"x" * line_len, held, held_bytes) is expected


@pytest.mark.asyncio
async def test_a_request_past_the_bound_is_answered_at_once(
    answers: _Answers, monkeypatch
) -> None:  # noqa: ANN001
    """At a bound the HOLD yields, never the answer.

    Holding only buys a quick reattach the chance to serve the call for real. With
    no room for that courtesy the call is failed immediately rather than left
    unanswered, which is the outcome this file exists to remove.
    """
    monkeypatch.setattr(stub_mod, "_HELD_TOTAL_BYTES", 1)
    session = _session(_request(41))

    await _drain_until(session, lambda: bool(answers.ids), grace_secs=60.0)

    assert answers.ids == [41]
    assert _held(session) == [], "not held, because there was no room to hold it"


@pytest.mark.asyncio
async def test_an_unanswerable_frame_past_the_bound_is_dropped(
    answers: _Answers, monkeypatch, caplog
) -> None:  # noqa: ANN001
    """A notification has no id, so at a bound the only bounded choice is to drop.

    Logged because it is a real loss, and it is the same choice
    ``_serve_capacity_refusal`` already makes for the same reason.
    """
    monkeypatch.setattr(stub_mod, "_HELD_TOTAL_BYTES", 1)
    session = _session(_notification())

    dropped = "dropped an unanswerable frame"
    with caplog.at_level("WARNING"):
        # Polls the DROP, not the dequeue: between the two, a cancel would hand the
        # frame back through the salvage path and read as a decision to hold it.
        run = await _drain_until(
            session,
            lambda: any(dropped in r.message for r in caplog.records),
            grace_secs=60.0,
        )

    assert run.reached, "the frame was never dropped"

    assert answers.ids == []
    assert _held(session) == []


@pytest.mark.asyncio
async def test_a_frame_over_the_per_frame_ceiling_is_not_held(
    answers: _Answers, monkeypatch
) -> None:  # noqa: ANN001
    """One enormous line is answered now rather than parked for the reconnect."""
    monkeypatch.setattr(stub_mod, "_HELD_FRAME_BYTES", 8)
    session = _session(_request("big"))

    await _drain_until(session, lambda: bool(answers.ids), grace_secs=60.0)

    assert answers.ids == ["big"]
    assert _held(session) == []


@pytest.mark.asyncio
async def test_room_freed_by_an_expiry_lets_the_drain_hold_again(
    answers: _Answers,
) -> None:
    """The bound is a pressure valve, not a stop: every call is still answered."""
    cap = stub_mod._STDIN_QUEUE_MAXSIZE
    session = _session(*[_request(i) for i in range(cap + 3)], maxsize=cap + 3)

    run = await _drain_until(session, lambda: len(answers.ids) == cap + 3)

    assert run.reached, f"only {len(answers.ids)} of {cap + 3} calls were answered"
    assert session._line_q.qsize() == 0


@pytest.mark.asyncio
async def test_a_stop_arriving_during_an_answer_loses_nothing(monkeypatch) -> None:  # noqa: ANN001
    """The ending must not interrupt an answer already in flight.

    The drain drops a frame from its hold as it answers it, so an ending that
    interrupted the answering await would leave that frame neither forwarded nor
    reliably answered -- a silent hang, which is the one outcome this file exists
    to remove. The stop is observed only BETWEEN iterations, so the answer
    completes and the id is answered exactly once.
    """
    emitted: list = []
    started = asyncio.Event()

    async def _slow_emit(req_ids, message, *, pool_label):  # noqa: ANN001
        started.set()
        await asyncio.sleep(0.05)  # the window an interruption would land in
        emitted.extend(req_ids)

    monkeypatch.setattr(stub_mod, "_emit_error_frames", _slow_emit)
    session = _session(_request(41))
    stop = asyncio.Event()
    task = asyncio.ensure_future(
        stub_mod._drain_while_disconnected(
            session, pool_label="probe:fake", grace_secs=_GRACE, stop=stop
        )
    )

    await asyncio.wait_for(started.wait(), timeout=5.0)
    stop.set()  # lands while the answer is mid-flight
    await asyncio.wait_for(task, timeout=5.0)

    assert emitted == [41], "the answer must complete rather than be abandoned"
    assert _held(session) == [], "an answered id must not also be forwarded"


@pytest.mark.asyncio
async def test_the_pairing_ends_the_drain_without_interrupting_an_answer(
    monkeypatch,
) -> None:  # noqa: ANN001
    """Same property, asserted through the real pairing rather than the drain.

    A reconnect that resolves while an answer is in flight must still produce that
    answer. A pairing that tore the drain down instead of asking it to stop would
    abandon the emit here, so this is what pins the ending the caller uses.
    """
    emitted: list = []
    started = asyncio.Event()

    async def _slow_emit(req_ids, message, *, pool_label):  # noqa: ANN001
        started.set()
        await asyncio.sleep(0.05)
        emitted.extend(req_ids)

    async def _reconnect_landing_mid_answer(*_args, **_kwargs):  # noqa: ANN202
        await asyncio.wait_for(started.wait(), timeout=5.0)
        return None

    monkeypatch.setattr(stub_mod, "_emit_error_frames", _slow_emit)
    monkeypatch.setattr(stub_mod, "_reconnect", _reconnect_landing_mid_answer)
    monkeypatch.setattr(stub_mod, "_QUEUED_GRACE_SECS", _GRACE)
    session = _session(_request(41))

    await stub_mod._reconnect_while_draining(
        "unused",
        {"stub_uuid": "u"},
        session,
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )

    assert emitted == [41], "the reconnect ending abandoned an answer in flight"
    assert _held(session) == []


# --- (i) the pairing: a drain nobody starts fixes nothing --------------------


@pytest.mark.asyncio
async def test_the_drain_runs_while_the_reconnect_runs(
    answers: _Answers, monkeypatch
) -> None:  # noqa: ANN001
    """The call site, tested by behaviour rather than by reading the source.

    A reconnect that spends longer than the grace window must see the queued call
    answered by the time it returns. This is the assertion that fails if the
    reconnect is ever called on its own, with nothing reading kiro-cli's stream.
    """
    reconnect_ran = asyncio.Event()

    async def _slow_reconnect(*_args, **_kwargs):  # noqa: ANN202
        reconnect_ran.set()
        await asyncio.sleep(_GRACE * 4)
        return None

    monkeypatch.setattr(stub_mod, "_reconnect", _slow_reconnect)
    monkeypatch.setattr(stub_mod, "_QUEUED_GRACE_SECS", _GRACE)
    session = _session(_request(41))

    attached = await stub_mod._reconnect_while_draining(
        "unused",
        {"stub_uuid": "u"},
        session,
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )

    assert reconnect_ran.is_set()
    assert attached is None
    assert answers.ids == [41], "the queued call must be answered during the window"


@pytest.mark.asyncio
async def test_a_reconnect_that_beats_grace_leaves_the_call_to_the_new_bridge(
    answers: _Answers, monkeypatch
) -> None:  # noqa: ANN001
    """The other half of the same pairing: a quick reattach answers nothing."""

    async def _quick_reconnect(*_args, **_kwargs):  # noqa: ANN202
        await asyncio.sleep(_GRACE / 5)
        return (asyncio.StreamReader(), _CaptureWriter(), {}, "uuid")

    monkeypatch.setattr(stub_mod, "_reconnect", _quick_reconnect)
    monkeypatch.setattr(stub_mod, "_QUEUED_GRACE_SECS", _GRACE)
    session = _session(_request(41))

    attached = await stub_mod._reconnect_while_draining(
        "unused",
        {"stub_uuid": "u"},
        session,
        asyncio.Event(),
        poolable=True,
        pool_label="probe:fake",
    )

    assert attached is not None
    assert answers.ids == []
    assert _held(session) == [_request(41)], "handed back for the new bridge"


def test_the_serve_loop_reconnects_only_through_the_paired_function() -> None:
    """Counted, not grepped for a name: a second direct call would raise the count.

    ``_reconnect`` must have exactly one caller -- the pairing above -- because a
    serve loop that called it directly would reconnect with nothing reading
    kiro-cli's stream, which is the defect this file exists for.
    """
    import inspect

    source = inspect.getsource(stub_mod)
    assert source.count("await _reconnect(") == 1
    assert "await _reconnect_while_draining(" in source
    assert "_drain_while_disconnected(" in inspect.getsource(stub_mod._reconnect_while_draining)
