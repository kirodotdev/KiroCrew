"""Gateway->stub keepalive transport probe (issue #1574).

A stub whose transport dies without a clean close leaves its connection handler
parked in ``reader.readuntil()``, so the ``finally`` that owns ``detach_stub``
never runs and the backend's refcount never reaches 0 -- putting the backend
permanently out of reach of the idle sweep.

A half-open transport is invisible to a reader and observable only on a write,
and an idle session performs no writes. These tests cover the probe that
supplies that write, and assert the two properties that matter:

* a dead transport cancels exactly its own handler task (so the existing
  teardown runs and the stub detaches), and
* a live transport is left completely alone.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from unittest.mock import MagicMock

from kiro_crew.mcp_gateway import gatewayd as gw


def _run(coro):
    """Run an async test body without pytest-asyncio."""
    return asyncio.run(coro)


class _FakeWriter:
    """StreamWriter double that records writes and can fail on drain."""

    def __init__(self, *, fail: Optional[BaseException] = None) -> None:
        self.writes: list[bytes] = []
        self._fail = fail
        self.drains = 0

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        self.drains += 1
        if self._fail is not None:
            raise self._fail


async def _park() -> None:
    """Stand in for a handler parked on a read that will never return."""
    await asyncio.Event().wait()


def _clear_registry() -> None:
    gw._STUB_PROBES.clear()


# --- dead transport -> handler cancelled ------------------------------------


def test_dead_transport_cancels_its_handler() -> None:
    """A write failure means the peer is gone. The probe cancels that stub's
    handler so the handler's finally can run detach_stub."""

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)
        writer = _FakeWriter(fail=BrokenPipeError("client closed"))
        gw._stub_probe_add(gw._StubProbe("stub-dead", writer, task))  # type: ignore[arg-type]

        dead = await gw._probe_stub_transports()

        assert dead == 1
        # Let the cancellation land, then confirm the handler is finished --
        # that is what runs the teardown in the real handler.
        await asyncio.sleep(0)
        assert task.done()
        assert task.cancelled()
        _clear_registry()

    _run(_inner())


def test_connection_error_also_counts_as_dead() -> None:
    """ConnectionError (not just BrokenPipeError) is a dead transport."""

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)
        writer = _FakeWriter(fail=ConnectionResetError("reset"))
        gw._stub_probe_add(gw._StubProbe("stub-reset", writer, task))  # type: ignore[arg-type]

        assert await gw._probe_stub_transports() == 1
        await asyncio.sleep(0)
        assert task.done()
        _clear_registry()

    _run(_inner())


def test_drain_timeout_counts_as_dead() -> None:
    """A stub that accepts the write but never drains is treated as dead: it
    must not pin the sweeper, matching the drain pump's own bound."""

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)

        class _HangingWriter(_FakeWriter):
            async def drain(self) -> None:
                await asyncio.sleep(3600)

        writer = _HangingWriter()
        gw._stub_probe_add(gw._StubProbe("stub-hang", writer, task))  # type: ignore[arg-type]

        # Shrink the bound so the test does not wait the production timeout.
        original = gw._STUB_KEEPALIVE_TIMEOUT_SECS
        gw._STUB_KEEPALIVE_TIMEOUT_SECS = 0.01
        try:
            assert await gw._probe_stub_transports() == 1
        finally:
            gw._STUB_KEEPALIVE_TIMEOUT_SECS = original
        await asyncio.sleep(0)
        assert task.done()
        _clear_registry()

    _run(_inner())


# --- live transport -> untouched --------------------------------------------


def test_live_transport_is_left_alone() -> None:
    """A healthy stub is not cancelled, and receives exactly one keepalive."""

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)
        writer = _FakeWriter()
        gw._stub_probe_add(gw._StubProbe("stub-live", writer, task))  # type: ignore[arg-type]

        assert await gw._probe_stub_transports() == 0
        assert not task.done()
        assert len(writer.writes) == 1
        assert writer.drains == 1

        task.cancel()
        _clear_registry()

    _run(_inner())


def test_keepalive_frame_is_the_reserved_control_type() -> None:
    """The probe writes a single newline-terminated reserved control frame.

    Shape matters: the stub consumes it by ``type``, and it deliberately
    carries no ``jsonrpc``/``id``/``method`` so that an older stub which
    forwards it hands an MCP client nothing to dispatch on.
    """

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)
        writer = _FakeWriter()
        gw._stub_probe_add(gw._StubProbe("stub-shape", writer, task))  # type: ignore[arg-type]

        await gw._probe_stub_transports()

        raw = writer.writes[0]
        assert raw.endswith(b"\n")
        frame = json.loads(raw)
        assert frame == {"type": gw.STUB_KEEPALIVE_TYPE}
        assert "jsonrpc" not in frame and "id" not in frame and "method" not in frame

        task.cancel()
        _clear_registry()

    _run(_inner())


def test_already_finished_handler_is_skipped() -> None:
    """A handler that is already exiting owns its own teardown; the probe must
    not write to it or double-cancel."""

    async def _inner():
        _clear_registry()

        async def _done() -> None:
            return None

        task = asyncio.create_task(_done())
        await task
        writer = _FakeWriter(fail=BrokenPipeError("would fail if written"))
        gw._stub_probe_add(gw._StubProbe("stub-gone", writer, task))  # type: ignore[arg-type]

        assert await gw._probe_stub_transports() == 0
        assert writer.writes == []
        _clear_registry()

    _run(_inner())


def test_one_dead_stub_does_not_stop_the_sweep() -> None:
    """Probing is per-connection: a dead stub must not prevent the live ones
    behind it in the iteration from being probed."""

    async def _inner():
        _clear_registry()
        dead_task = asyncio.create_task(_park())
        live_task = asyncio.create_task(_park())
        await asyncio.sleep(0)
        dead_writer = _FakeWriter(fail=BrokenPipeError("gone"))
        live_writer = _FakeWriter()
        gw._stub_probe_add(gw._StubProbe("stub-a", dead_writer, dead_task))  # type: ignore[arg-type]
        gw._stub_probe_add(gw._StubProbe("stub-b", live_writer, live_task))  # type: ignore[arg-type]

        assert await gw._probe_stub_transports() == 1
        # The live stub was still probed.
        assert len(live_writer.writes) == 1
        assert not live_task.done()

        live_task.cancel()
        _clear_registry()

    _run(_inner())


def test_unexpected_exception_does_not_cancel_or_crash() -> None:
    """An unexpected error is not evidence of a dead peer, so the handler is
    left running rather than being torn down on a guess."""

    async def _inner():
        _clear_registry()
        task = asyncio.create_task(_park())
        await asyncio.sleep(0)

        class _OddWriter(_FakeWriter):
            def write(self, payload: bytes) -> None:
                raise RuntimeError("unexpected")

        gw._stub_probe_add(gw._StubProbe("stub-odd", _OddWriter(), task))  # type: ignore[arg-type]

        assert await gw._probe_stub_transports() == 0
        assert not task.done()

        task.cancel()
        _clear_registry()

    _run(_inner())


# --- registry lifetime ------------------------------------------------------


def test_registry_add_and_discard() -> None:
    """Discard is idempotent: the handler's finally may run after the probe has
    already observed the connection."""
    _clear_registry()
    writer: Any = _FakeWriter()
    task: Any = MagicMock()
    probe = gw._StubProbe("stub-x", writer, task)

    gw._stub_probe_add(probe)
    assert probe in gw._STUB_PROBES
    gw._stub_probe_discard(probe)
    assert probe not in gw._STUB_PROBES
    gw._stub_probe_discard(probe)  # idempotent
    _clear_registry()


def test_two_connections_for_one_stub_uuid_both_tracked() -> None:
    """A reconnecting stub can briefly overlap with its predecessor. Both
    records must be tracked, or the probe loses the stale handler it exists to
    tear down."""
    _clear_registry()
    writer: Any = _FakeWriter()
    old = gw._StubProbe("same-uuid", writer, MagicMock())
    new = gw._StubProbe("same-uuid", writer, MagicMock())

    gw._stub_probe_add(old)
    gw._stub_probe_add(new)
    assert len(gw._STUB_PROBES) == 2
    _clear_registry()
