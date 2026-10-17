"""Oversize stdout frames on the ``AcpRuntime`` reader.

A frame over the stdout buffer costs that one frame, never the shared runtime: the
reply it carried fails its own request with the size and the limit, and the reader
recovers at the next frame.
"""

import asyncio

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _feed, _make_runtime, _next, _register, _start_reader, _stop_reader

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.client import _OVERSIZE_DRAIN_MAX_BYTES
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_oversize_stdout_frame_is_dropped_not_fatal():
    """A single JSON-RPC line over the stdout buffer must cost ONE frame, not
    the whole runtime.

    Marking the runtime dead on overrun would poison every multiplexed
    session's queue and fail every pending future, surfacing as
    "process exited / chat failure" mid-turn after one huge tool result.

    Driven through a REAL StreamReader so this asserts asyncio's actual
    behaviour, not a mock's.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"X" * 1024 + b"\n")  # oversize, newline present
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_oversize_reply_fails_its_awaited_request_with_the_size_and_limit():
    """A session/new reply over the frame limit (every agent's welcomeMessage,
    say) must fail that request at once naming the frame size and the limit --
    not surface minutes later as a session/new timeout blamed on MCP servers.
    The reader keeps routing afterwards."""
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    rt._pending_requests[7] = future
    task = await _start_reader(rt)
    try:
        frame = b'{"jsonrpc":"2.0","id":7,"result":{"modes":"' + b"W" * 2048 + b'"}}\n'
        reader.feed_data(frame)
        with pytest.raises(AcpFrameTooLarge) as excinfo:
            await await_until(future, what="the pending request's future")
        message = str(excinfo.value)
        assert f"{len(frame):,} bytes" in message
        assert f"{runtime_mod._STDOUT_BUFFER_LIMIT:,}-byte" in message
        assert not getattr(excinfo.value, "transient", False)
        assert "MCP" not in message
        assert 7 not in rt._pending_requests
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_oversize_host_request_never_fails_one_of_ours():
    """The host numbers its own requests (a permission prompt) independently, so
    an oversize frame carrying ``method`` must not fail our request with that id."""
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    rt._pending_requests[3] = future
    task = await _start_reader(rt)
    try:
        reader.feed_data(
            b'{"jsonrpc":"2.0","id":3,"method":"session/request_permission","params":"'
            + b"P" * 2048
            + b'"}\n'
        )
        # flake-ok: pre-existing 200 ms window before a has-not-happened assertion, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.2)
        assert not future.done()
        assert 3 in rt._pending_requests
    finally:
        future.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "load"])
async def test_an_oversize_session_start_reply_is_a_tagged_start_failure(monkeypatch, path):
    """A self-driving caller counts start failures by ``session_start_failed`` to back
    off; an oversize session/new or session/load reply is one, and it is not
    transient -- the same request gets the same reply."""
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    rt, _, _ = _make_runtime()
    rt._can_load_session = True

    async def _oversize(method, params, timeout=None):
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            raise AcpFrameTooLarge(runtime_mod._oversize_frame_message(11_000_000))
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _oversize)
    with pytest.raises(AcpFrameTooLarge) as excinfo:
        if path == "create":
            await rt.create_session(cwd="/work", agent="kirocrew")
        else:
            await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-1.json", "sid-1", cwd="/work", agent="kirocrew"
            )
    assert excinfo.value.session_start_failed is True
    assert excinfo.value.transient is False
    from kiro_crew.llm_helpers import acp_error_is_transient

    assert acp_error_is_transient(excinfo.value) is False


@pytest.mark.asyncio
async def test_an_oversize_session_new_reply_releases_its_start_permit(monkeypatch):
    """The start gate admits a few session starts at once and has no reaper, so a
    start that fails on an oversize reply must give its slot back -- otherwise two
    such failures block every later ``create_session`` on the loop."""
    from kiro_crew.acp import runtime_start
    from kiro_crew.acp.session_handle import AcpFrameTooLarge

    released: list[str] = []
    recorded: list[bool] = []
    real_acquire = runtime_mod.SessionStartGate.acquire

    async def acquire(self, *a, **k):
        permit = await real_acquire(self, *a, **k)
        original = permit.release

        def release():
            released.append("x")
            return original()

        permit.release = release
        return permit

    monkeypatch.setattr(runtime_mod.SessionStartGate, "acquire", acquire)
    monkeypatch.setattr(
        runtime_start, "_record_session_start", lambda *_a, ok, **_k: recorded.append(ok)
    )
    rt, _, _ = _make_runtime()

    async def _oversize(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            raise AcpFrameTooLarge(runtime_mod._oversize_frame_message(11_000_000))
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _oversize)
    with pytest.raises(AcpFrameTooLarge):
        await rt.create_session(cwd="/work", agent="kirocrew")
    assert released == ["x"]
    assert recorded == [False]


def test_the_oversize_frame_id_is_read_only_from_a_reply_envelope():
    rid = runtime_mod._oversize_frame_request_id
    assert rid(b'{"jsonrpc":"2.0","id":12,"result":{') == 12
    assert rid(b'{"jsonrpc":"2.0","result":{"availableModes":[{"id":"kiro_default"}') is None
    assert rid(b'{"jsonrpc":"2.0","id":4,"method":"session/request_permission"') is None
    assert rid(b"XXXX") is None
    assert rid(b'{"jsonrpc":"2.0","result":{"x":{"id":5,"y":1}}') is None


@pytest.mark.asyncio
async def test_unterminated_oversize_stdout_recovers_at_next_frame():
    """The shape actually observed in the field: an oversize line whose newline
    has NOT arrived yet, so the reader drains prefix after prefix before the
    stream is back in sync. It must ride through every step and route the next
    real frame.

    Asserts the outcome (recovery), not the step count: how many buffer-fulls
    the reader sees depends on how the feeds interleave with its task.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for _ in range(4):
            reader.feed_data(b"Y" * 512)  # no newline anywhere
            await asyncio.sleep(0)
        reader.feed_data(b"TAIL-OF-OVERSIZE-LINE\n")  # line finally terminates
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_oversize_frame_split_mid_multibyte_does_not_kill_demux():
    """The drained remainder must never reach json.loads.

    The drain must not consume only the buffered prefix and let the recovered
    tail through as a line, because that tail is
    a byte-slice cut at an arbitrary offset, so an oversize frame carrying
    multibyte UTF-8 (CJK, emoji — ordinary in tool output) splits a character;
    `json.loads` then raises UnicodeDecodeError, which is NOT a
    json.JSONDecodeError, so it escaped the non-JSON guard into the loop's crash
    handler and killed EVERY multiplexed session.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # Two conditions make the tail reach the parser, and both are ordinary:
        #  - the discard boundary must fall mid-character, which the UNTERMINATED
        #    branch does by construction (it reports `consumed = len(buffer)`, an
        #    arbitrary byte offset; a newline-terminated overrun instead reports
        #    the newline's offset, already a character boundary), and
        #  - the remainder after the last discard must be UNDER the reader limit,
        #    so readuntil returns it as a normal-looking line instead of
        #    overrunning again.
        # Dense CJK, fed in 500-byte slices that are not multiples of 3.
        blob = ("苹" * 400).encode() + b"\n"  # 1201 bytes
        assert len(blob) % 3 != 0
        for off in range(0, 1000, 500):
            reader.feed_data(blob[off : off + 500])
            await asyncio.sleep(0)
        reader.feed_data(blob[1000:])  # 201 bytes < limit → returned as a line
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_many_terminated_oversize_frames_never_exhaust_the_budget():
    """A run of oversize-but-properly-terminated frames must stay survivable.

    The guard counts bytes-without-a-boundary, not oversize *frames*: counting
    frames would let a replay of N newline-terminated >limit frames walk
    straight into runtime death even though every one recovers a frame
    boundary. The budget is scoped to a single drain call, each of which
    provably ends on a boundary.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    rounds = 40
    try:
        for i in range(rounds):
            reader.feed_data(b"X" * 4096 + b"\n")
            _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "n": i}})
        for i in range(rounds):
            msg = await _next(q["sA"])
            assert msg.params["n"] == i
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unterminated_blob_past_the_byte_budget_marks_runtime_dead():
    """The escape hatch: a stream that never yields a frame boundary would have
    the reader draining forever, so exceeding the byte budget must still reach the
    terminal state.

    The liveness oracle cannot cover this case — it reads CPU/IO movement, and a
    garbage-spewing stream moves both, so it would be judged WORKING.
    """
    rt, _, proc = _make_runtime()
    reader = asyncio.StreamReader(limit=256)
    proc.stdout = reader
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        fed = 0
        while fed <= _OVERSIZE_DRAIN_MAX_BYTES and not rt._dead:
            reader.feed_data(b"Z" * 65536)  # never a newline
            fed += 65536
            await asyncio.sleep(0)
        await await_until(task, what="the reader loop exiting")
    except Exception:
        pass
    finally:
        await _stop_reader(task)
    assert rt._dead


def test_runtime_reuses_clients_oversize_drain_helper():
    """The consume-prefix-and-retry drain must have ONE definition. A second copy
    is how two read paths drift apart (they already disagreed once, when only one
    of them killed the process)."""
    import kiro_crew.acp.client as client_mod
    import kiro_crew.acp.runtime as runtime_mod

    assert runtime_mod._drain_oversize_line is client_mod._drain_oversize_line
    assert runtime_mod.OversizeLineUnrecoverable is client_mod.OversizeLineUnrecoverable
