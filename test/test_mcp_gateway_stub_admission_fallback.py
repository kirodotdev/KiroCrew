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
