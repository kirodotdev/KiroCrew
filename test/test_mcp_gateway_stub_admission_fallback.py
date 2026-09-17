"""A stub that gatewayd REFUSES at admission must degrade, never die.

This is the mitigation that bounds the blast radius of the peer-principal
admission gate, and it had no test.

``handle_stub_connection`` performs the principal check *before* reading the
Register frame, so a refusal is not a protocol reply -- the daemon simply closes
the connection. From the stub's side that surfaces as a broken handshake, and in
practice as ``ECONNRESET`` rather than a clean EOF: the Register bytes the stub
already wrote are still unread in the socket buffer when the daemon closes, so
the kernel answers RST instead of FIN. Either way the two possible outcomes are
very far apart:

* raise :class:`~kiro_crew.mcp_gateway.stub.FallbackRequestedError`, which
  ``main`` converts into ``fallback_exec`` -- the stub then ``execvpe`` the real
  backend, so the session works with pooling lost and one line in
  ``stub_fallback.jsonl``; or
* raise anything else, which escapes before ``fallback_exec`` and kills the stub
  with the MCP server never started -- a broken session.

The distinction is what makes fail-closed admission affordable. macOS was held
out of ``PEER_IDENTITY_SUPPORTED`` while ``LOCAL_PEERCRED`` was unproven
precisely because an ``UNVERIFIABLE`` refusal looked like it could lock a Mac
user out of their own gateway; it cannot, *because* of this path. Promoting macOS
therefore leans on behaviour nothing was asserting, so assert it -- on every
platform, against a real endpoint, since the refusal is a transport-level close
and its shape is transport-specific.

The stub's own docstring calls this the "always-degrade-to-per-session
guarantee"; the code has two comments warning that a stray exception here defeats
it (the non-dict reply guard, and the logging-level guard). Those comments are
the only thing that was defending it.

Named ``test_mcp_gateway_stub_*`` deliberately, matching the sibling stub suites:
that is the prefix the macOS job's glob selects, so this lands on Darwin without
editing the workflow. Under any other name the coverage would silently be
Linux-and-Windows-only -- which is the failure mode the glob exists to avoid.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import stub, transport


def _endpoint_dir() -> str | None:
    """Where to put the test endpoint.

    On POSIX this binds a real socket and ``AF_UNIX`` caps ``sun_path`` at ~104
    bytes, which pytest's ``tmp_path`` exceeds on macOS -- so bind under
    ``/tmp``. A Windows named pipe has neither the cap nor a ``/tmp``.
    """
    return None if pc.IS_WINDOWS else "/tmp"


def _register_payload() -> dict[str, Any]:
    """The minimum Register frame ``handshake`` needs to send one.

    Deliberately not built via ``build_register_payload``: that does a /proc
    ancestry walk and hashes the target binary, none of which this test is
    about. ``handshake`` only reads ``stub_uuid`` from the payload.
    """
    return {"type": "register", "stub_uuid": "admission-fallback-probe"}


async def _serve(handler: Any, sock_dir: Path) -> tuple[Any, Path]:
    sock = sock_dir / "gw.sock"
    transport.prepare_dir(sock)
    server = await transport.serve(sock, handler, limit=1 << 16)
    return server, sock


@pytest.mark.asyncio
async def test_handshake_requests_fallback_when_admission_closes_the_connection(short_sock_dir) -> None:
    """The exact shape of a principal-check refusal: closed before any reply."""
    accepted = asyncio.Event()

    def on_connect(_reader: Any, writer: Any) -> None:
        # What handle_stub_connection does on a non-MATCH peer: no reply frame,
        # just a close. It never even reads the Register frame.
        accepted.set()
        writer.close()

    server, sock = await _serve(on_connect, short_sock_dir)
    try:
        with pytest.raises(stub.FallbackRequestedError) as excinfo:
            await asyncio.wait_for(
                stub.handshake(str(sock), _register_payload()), timeout=30
            )
    finally:
        server.close()
        await server.wait_closed()

    assert accepted.is_set(), "the endpoint never accepted, so nothing was tested"
    # Deliberately NOT asserting a specific reason string. Measured: this lands on
    # "register io failed: [Errno 104] Connection reset by peer", not the clean-EOF
    # "gateway closed during handshake" branch -- because gatewayd refuses BEFORE
    # reading the Register frame, so those bytes are still unread in the socket
    # buffer at close() and the kernel answers with RST rather than FIN. Which of
    # the two branches runs is a timing/buffer detail, so pinning one would make
    # this test brittle about the wrong thing. What must hold is the type (that is
    # what `main` keys fallback_exec on) and a non-empty reason (that is what lands
    # in stub_fallback.jsonl, the only signal an operator has that pooling quietly
    # stopped engaging).
    assert excinfo.value.reason, "fallback_exec would audit an empty reason"


@pytest.mark.asyncio
async def test_handshake_requests_fallback_on_an_explicit_rejection(short_sock_dir) -> None:
    """A ``rejected`` reply degrades too, and carries the daemon's reason through.

    Distinct from the close above: this is the path where gatewayd got far enough
    to answer, e.g. a capacity refusal. Both must reach ``fallback_exec``.
    """

    async def on_connect(reader: Any, writer: Any) -> None:
        await reader.readline()
        writer.write(b'{"type":"rejected","reason":"at capacity"}\n')
        await writer.drain()
        writer.close()

    def _spawn(reader: Any, writer: Any) -> None:
        asyncio.get_running_loop().create_task(on_connect(reader, writer))

    server, sock = await _serve(_spawn, short_sock_dir)
    try:
        with pytest.raises(stub.FallbackRequestedError) as excinfo:
            await asyncio.wait_for(
                stub.handshake(str(sock), _register_payload()), timeout=30
            )
    finally:
        server.close()
        await server.wait_closed()

    assert "at capacity" in excinfo.value.reason


@pytest.mark.asyncio
async def test_handshake_requests_fallback_when_no_endpoint_exists(short_sock_dir) -> None:
    """Daemon absent entirely -- the baseline degrade case.

    Included so the three refusal shapes gatewayd can present (never there,
    closed at admission, answered with a rejection) are pinned together rather
    than one of them being covered by accident.
    """
    missing = short_sock_dir / "absent.sock"
    with pytest.raises(stub.FallbackRequestedError) as excinfo:
        await stub.handshake(str(missing), _register_payload())
    assert "connect failed" in excinfo.value.reason


# --- spawn_queue admission -----------------------------------------------------
#
# The daemon may now hold a stub in its spawn gate for minutes. What the stub
# must do with that is pinned here against a scripted daemon on a real endpoint:
# a queue-aware wait is a SILENCE timer renewed by ``queued`` frames (never a
# fixed deadline), an old daemon's single reply is still the verdict, and only
# the two target-shaped rejection classes ever lead to an exec.


async def _open(sock: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await transport.connect(str(sock))


def _scripted_daemon(script: Any) -> Any:
    """Accept one connection, read the ``ensure_backend`` frame, run ``script``."""
    seen: list[dict[str, Any]] = []

    async def on_connect(reader: Any, writer: Any) -> None:
        line = await reader.readline()
        seen.append(json.loads(line))
        try:
            await script(writer)
        finally:
            writer.close()

    def spawn(reader: Any, writer: Any) -> None:
        asyncio.get_running_loop().create_task(on_connect(reader, writer))

    spawn.seen = seen  # type: ignore[attr-defined]
    return spawn


async def _send(writer: Any, frame: dict[str, Any]) -> None:
    writer.write(json.dumps(frame).encode("utf-8") + b"\n")
    await writer.drain()


@pytest.mark.asyncio
async def test_queued_frames_extend_a_queue_aware_wait(short_sock_dir) -> None:
    """Six keepalives at 0.05 s under a 0.12 s silence window: a fixed deadline of
    0.12 s would have given up long before the ``ready`` at 0.3 s."""

    async def script(writer: Any) -> None:
        for _ in range(6):
            await asyncio.sleep(0.05)
            await _send(writer, {"type": "queued", "position": 1, "capacity": 4})
        await _send(writer, {"type": "ready"})
        await asyncio.sleep(0.05)

    handler = _scripted_daemon(script)
    server, sock = await _serve(handler, short_sock_dir)
    try:
        reader, writer = await _open(sock)
        outcome, frame = await asyncio.wait_for(
            stub._ensure_backend_admitted(
                reader, writer, queue_aware=True, total_budget_secs=10.0, silence_secs=0.12
            ),
            timeout=10,
        )
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert (outcome, frame) == (stub._ADMIT_READY, {"type": "ready"})
    assert handler.seen[0]["wait_budget_secs"] == 10.0, "a queue-aware stub declares its budget"


@pytest.mark.asyncio
async def test_silence_from_a_queue_aware_daemon_times_out_before_the_budget(short_sock_dir) -> None:
    """The timer is silence, not budget: a daemon that promised keepalives and
    sends none for one window is not serving, however much budget remains."""

    async def script(writer: Any) -> None:
        await asyncio.sleep(0.5)

    server, sock = await _serve(_scripted_daemon(script), short_sock_dir)
    try:
        reader, writer = await _open(sock)
        t0 = time.monotonic()
        outcome, frame = await asyncio.wait_for(
            stub._ensure_backend_admitted(
                reader, writer, queue_aware=True, total_budget_secs=60.0, silence_secs=0.1
            ),
            timeout=10,
        )
        elapsed = time.monotonic() - t0
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert (outcome, frame) == (stub._ADMIT_TIMEOUT, None)
    assert elapsed < 0.4, f"gave up after {elapsed:.2f}s -- the silence window, not the budget"


@pytest.mark.asyncio
async def test_the_total_budget_caps_a_wait_however_lively_the_daemon(short_sock_dir) -> None:
    async def script(writer: Any) -> None:
        for _ in range(40):
            await asyncio.sleep(0.02)
            await _send(writer, {"type": "queued", "position": 9, "capacity": 1})

    server, sock = await _serve(_scripted_daemon(script), short_sock_dir)
    try:
        reader, writer = await _open(sock)
        outcome, _ = await asyncio.wait_for(
            stub._ensure_backend_admitted(
                reader, writer, queue_aware=True, total_budget_secs=0.25, silence_secs=1.0
            ),
            timeout=10,
        )
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert outcome == stub._ADMIT_TIMEOUT


@pytest.mark.asyncio
async def test_an_old_daemon_gets_the_legacy_single_reply_protocol(short_sock_dir) -> None:
    """No ``spawn_queue``: the frame carries no budget, and the one reply is the
    verdict -- an unexpected frame there is a rejection, exactly as before."""

    async def script(writer: Any) -> None:
        await _send(writer, {"type": "rejected", "reason": "at capacity", "fallback": True})
        await asyncio.sleep(0.05)

    handler = _scripted_daemon(script)
    server, sock = await _serve(handler, short_sock_dir)
    try:
        reader, writer = await _open(sock)
        outcome, frame = await asyncio.wait_for(
            stub._ensure_backend_admitted(
                reader, writer, queue_aware=False, total_budget_secs=600.0, silence_secs=1.0
            ),
            timeout=10,
        )
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert outcome == stub._ADMIT_REJECTED
    assert frame is not None and frame["fallback"] is True
    assert "wait_budget_secs" not in handler.seen[0], "an old daemon must see the old frame"
    assert stub._rejection_class(frame) is None, "no class means the pre-class rules apply"


@pytest.mark.asyncio
async def test_a_queued_frame_to_an_old_protocol_wait_is_the_verdict(short_sock_dir) -> None:
    """Belt and braces for the negotiation: a stub that did NOT see ``spawn_queue``
    treats any non-ready frame as the reply. That is why the daemon must never
    send ``queued`` without the capability -- pinned from the stub's side."""

    async def script(writer: Any) -> None:
        await _send(writer, {"type": "queued", "position": 1, "capacity": 4})
        await asyncio.sleep(0.05)

    server, sock = await _serve(_scripted_daemon(script), short_sock_dir)
    try:
        reader, writer = await _open(sock)
        outcome, frame = await stub._ensure_backend_admitted(
            reader, writer, queue_aware=False, total_budget_secs=600.0, silence_secs=1.0
        )
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert outcome == stub._ADMIT_REJECTED and frame == {"type": "queued", "position": 1, "capacity": 4}


def test_only_the_target_shaped_classes_fall_back() -> None:
    assert stub._REJECT_CLASS_COMPAT in stub._FALLBACK_CLASSES
    assert stub._REJECT_CLASS_ISOLATION in stub._FALLBACK_CLASSES
    assert stub._REJECT_CLASS_CAPACITY not in stub._FALLBACK_CLASSES
    assert stub._rejection_class({"type": "rejected", "class": "capacity"}) == "capacity"
    assert stub._rejection_class({"type": "rejected", "class": ""}) is None
    assert stub._rejection_class({"type": "rejected"}) is None


@pytest.mark.asyncio
async def test_a_capacity_refusal_answers_requests_with_a_typed_error_and_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kiro-cli sees a live server that says WHY it cannot serve, never a crash
    and never a per-session exec."""
    session = stub.StubSession()
    q: "asyncio.Queue[bytes]" = asyncio.Queue()
    session._line_q = q
    q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}).encode() + b"\n")
    q.put_nowait(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
    q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": "t-1", "method": "tools/list"}).encode() + b"\n")
    q.put_nowait(b"")  # kiro-cli hangs up
    written: list[bytes] = []
    monkeypatch.setattr(stub, "_write_stdout_line", written.append)
    rc = await asyncio.wait_for(
        stub._serve_capacity_refusal(
            session, asyncio.Event(), reason="pool full", retry_after_secs=30, pool_label="a:b"
        ),
        timeout=5,
    )
    assert rc == 0
    replies = [json.loads(w) for w in written]
    assert [r["id"] for r in replies] == [7, "t-1"], "every request answered, the notification dropped"
    for reply in replies:
        assert reply["error"]["code"] == stub._CAPACITY_ERROR_CODE
        assert reply["error"]["data"] == {"class": "capacity", "retry_after_secs": 30}
        assert "pool full" in reply["error"]["message"]
