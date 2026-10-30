"""How the gateway reads and writes a backend's stdio pipes, and what a bad line costs.

These tests pin ``run_stdout_pump`` against a real ``asyncio.StreamReader``: frames routed until EOF
marks the backend dead, the exit code winning over the generic reason, a truncated final line, an
oversize line failing only its own request (or recycling the backend when no id is recoverable), the
spill and image-budget hooks with their fail-open paths, and cancellation.
``_fail_oversize_request`` finds the id from a complete prefix, from a regex over the head or from
the line's tail, which the pump's drain keeps alongside the head, fails an oversize initialize along
with its waiters and an oversize MCP Apps fetch at once, leaves an id with no pending entry alone,
and recycles rather than guess. A stray line (each of ``stray_line_helpers.STRAY_LINES`` plus a
non-string method), an unparseable response or subscribe or release verdict, and a raise while
handling one line each cost only that line: the request it answered is failed or its lease settled,
an id nested in an unparseable notification fails nothing, and the pump goes on. A subscribe reply
the gateway cannot use releases the lease, as the subscriber's caller where it has one, while a
refusal the server sent releases nothing. A non-object ``capabilities`` reads as none in the pump's
handshake and in ``send_initialize``, whose pre-init reader skips stray lines, and
``prime_initialize`` refuses a backend that died while it waited. ``_write_json_line`` writes one
newline-terminated frame under the per-backend lock and turns a drain timeout into
``BrokenPipeError``, and ``_pump_stderr`` drains to EOF past an oversize line and ends without
raising when its reader fails. A topic of the ``test_mcp_gateway_backend_coverage`` family:
``test_mcp_gateway_backend_coverage.py`` keeps the initialize state machine, the other siblings are
``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _drain, _frames, _line, _make_backend, _reader
from stray_line_helpers import STRAY_LINES, too_deep_to_decode

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    Backend,
    BackendGone,
    _PendingRequest,
    _pump_stderr,
    _write_json_line,
    send_initialize,
)
from kiro_crew.testing.wait import await_until, finished

# --- run_stdout_pump --------------------------------------------------------


class TestRunStdoutPump:
    @pytest.mark.asyncio
    async def test_routes_frames_then_broadcasts_on_eof(self) -> None:
        backend = _make_backend(stdout=None)
        backend.stdout = cast(
            Any,
            _reader(
                _line({"id": "gw-1", "result": {"ok": 1}}),
                _line({"method": "notifications/tools/list_changed"}),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["id"] == 5
        assert (await _drain(inbox))["method"] == "notifications/tools/list_changed"
        assert backend.dead_reason == "stdout EOF"

    @pytest.mark.asyncio
    async def test_exit_code_wins_over_generic_eof_reason(self) -> None:
        backend = _make_backend(returncode=3)
        backend.stdout = cast(Any, _reader())
        await backend.run_stdout_pump()
        assert backend.dead_reason == "exit rc=3"

    @pytest.mark.asyncio
    async def test_partial_final_line_is_reported(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b'{"id":"gw-1","result"'))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        # The truncated frame is dropped, and the stub gets a backend-gone error.
        assert (await _drain(inbox))["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_oversize_line_dropped_without_eating_the_next_frame(self) -> None:
        backend = _make_backend()
        oversize = b'{"id":"gw-1","result":"' + b"x" * 400 + b'"}\n'
        backend.stdout = cast(
            Any,
            _reader(
                oversize,
                _line({"id": "gw-2", "result": {"ok": True}}),
                limit=64,
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests.update(
            {
                "gw-1": _PendingRequest("s1", 1, "tools/call"),
                "gw-2": _PendingRequest("s1", 2, "tools/call"),
            }
        )
        await backend.run_stdout_pump()
        first = await _drain(inbox)
        assert first["id"] == 1
        assert "exceeded size limit" in first["error"]["message"]
        # The frame AFTER the oversize line still arrives intact.
        assert (await _drain(inbox))["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_unterminated_oversize_line_at_eof_recycles(self) -> None:
        """An oversize line that never terminates: the drain loop hits EOF, the
        id cannot be recovered, so the shared backend is recycled rather than
        failing an innocent stub."""
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b"x" * 400, limit=64))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert "unrecoverable request id" in (backend.dead_reason or "")
        err = await _drain(inbox)
        assert err["id"] == 1
        assert "unrecoverable request id" in err["error"]["message"]

    @pytest.mark.asyncio
    async def test_over_threshold_line_is_spilled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backend_mod, "RESPONSE_SPILL_THRESHOLD_BYTES", 8)
        monkeypatch.setattr(
            backend_mod,
            "maybe_spill_response",
            lambda line, server, threshold: _line({"id": "gw-1", "result": "spilled"}),
        )
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": "x" * 64})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "spilled"

    @pytest.mark.asyncio
    async def test_spill_failure_routes_the_raw_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backend_mod, "RESPONSE_SPILL_THRESHOLD_BYTES", 8)

        def _boom(line: bytes, server: str, threshold: int) -> bytes:
            raise OSError("disk full")

        monkeypatch.setattr(backend_mod, "maybe_spill_response", _boom)
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": "raw"})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "raw"

    @pytest.mark.asyncio
    async def test_image_bearing_line_goes_through_the_budget_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A line matching the image probe is parse-confirmed, then the
        REWRITTEN line from the image stage is what gets routed."""
        monkeypatch.setattr(
            backend_mod,
            "parse_image_bearing_frame",
            lambda line: {"parsed": True},
        )
        monkeypatch.setattr(
            backend_mod,
            "rewrite_image_frame",
            lambda msg, line, server: _line({"id": "gw-1", "result": "budgeted"}),
        )
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(
                _line(
                    {"id": "gw-1", "result": {"content": [{"type": "image", "data": "AA=="}]}},
                )
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "budgeted"

    @pytest.mark.asyncio
    async def test_non_image_frame_matching_probe_skips_the_image_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe false positive (escaped text, no image blocks) must not
        occupy the image pool: the parse stage returns None and the original
        line is routed with no rewrite call."""
        rewrite_calls = []
        monkeypatch.setattr(backend_mod, "parse_image_bearing_frame", lambda line: None)
        monkeypatch.setattr(
            backend_mod,
            "rewrite_image_frame",
            lambda msg, line, server: rewrite_calls.append(1) or line,
        )
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": 'has "image" text'})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == 'has "image" text'
        assert rewrite_calls == []

    @pytest.mark.asyncio
    async def test_image_budget_failure_routes_the_raw_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected hook failure must not take the relay down: the raw
        line is routed (per-block fail-closed lives INSIDE the hook)."""

        def _boom(line: bytes) -> dict:
            raise RuntimeError("pillow exploded")

        monkeypatch.setattr(backend_mod, "parse_image_bearing_frame", _boom)
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(
                _line(
                    {"id": "gw-1", "result": {"content": [{"type": "image", "data": "AA=="}]}},
                )
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        routed = await _drain(inbox)
        assert routed["result"]["content"][0]["type"] == "image"

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(eof=False))
        task = asyncio.create_task(backend.run_stdout_pump())
        # flake-ok: pre-existing one-tick yield, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing await of the pump task the test just cancelled, moved verbatim
            await task


# --- _fail_oversize_request -------------------------------------------------


class TestFailOversizeRequest:
    @pytest.mark.asyncio
    async def test_complete_prefix_json_identifies_the_request(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-9"] = _PendingRequest("s1", 90, "tools/call")
        await backend._fail_oversize_request(b'{"jsonrpc":"2.0","id":"gw-9","result":{}}')
        err = await _drain(inbox)
        assert err["id"] == 90 and "exceeded size limit" in err["error"]["message"]

    @pytest.mark.asyncio
    async def test_regex_fallback_recovers_truncated_head(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-8"] = _PendingRequest("s1", 80, "tools/call")
        await backend._fail_oversize_request(
            b'{"jsonrpc":"2.0","id":"gw-8","result":{"content":"' + b"y" * 600
        )
        assert (await _drain(inbox))["id"] == 80

    @pytest.mark.asyncio
    async def test_brace_terminated_but_invalid_json_falls_back_to_regex(self) -> None:
        """The head looks complete (ends in ``}``) but is not valid JSON, so the
        prefix parse raises and the regex recovers the id."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-5"] = _PendingRequest("s1", 50, "tools/call")
        await backend._fail_oversize_request(b'{"id":"gw-5", not-valid-json}')
        assert (await _drain(inbox))["id"] == 50

    @pytest.mark.asyncio
    async def test_regex_match_that_is_not_decodable_recycles(self) -> None:
        """The regex matches a quoted id containing an invalid escape, so the
        second decode also fails and the backend is recycled."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend._fail_oversize_request(b'{"id":"\\x"}')
        assert "unrecoverable request id" in (backend.dead_reason or "")

    @pytest.mark.asyncio
    async def test_known_id_with_no_pending_entry_is_a_noop(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._fail_oversize_request(b'{"id":"gw-unknown","result":{}}')
        assert inbox.empty()
        assert backend._gone_broadcast is False

    @pytest.mark.asyncio
    async def test_oversize_initialize_fails_the_handshake_and_answers_its_waiters(self) -> None:
        """The handshake can never complete, so it fails, the backend reads dead
        and the done-event is woken, and every queued initialize waiter is
        answered with the failure."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._init_state = "in_flight"
        backend._pending_requests["gw-7"] = _PendingRequest("__init__", None, "initialize")
        backend._init_pending = [("s1", 1)]
        await backend._fail_oversize_request(b'{"id":"gw-7","result":{}}')
        assert "exceeded size limit" in (backend.dead_reason or "")
        assert backend._init_state == "failed"
        assert backend._init_done_event.is_set()
        assert backend._init_pending == []
        assert not backend.is_alive
        reply = await _drain(inbox)
        assert reply["id"] == 1 and "backend init failed" in reply["error"]["message"]

    @pytest.mark.asyncio
    async def test_an_oversize_apps_fetch_is_failed_at_once(self) -> None:
        """The MCP Apps fetch is resolved at once, so the tool result it holds
        back is delivered without waiting out the fetch's full timeout."""
        backend = _make_backend()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        backend._pending_requests["gw-8"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut
        )
        await backend._fail_oversize_request(b'{"jsonrpc":"2.0","id":"gw-8","result":{"x"')
        assert fut.done()
        assert "exceeded size limit" in fut.result()["error"]["message"]

    @pytest.mark.asyncio
    async def test_an_oversize_response_with_its_id_last_is_found_by_the_tail(self) -> None:
        """The MCP TypeScript SDK writes ``{result, jsonrpc, id}``: the id sits
        past any head the drain keeps, so the tail must find it."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-9"] = _PendingRequest("s1", 90, "tools/call")
        await backend._fail_oversize_request(
            b'{"result":{"content":[{"type":"text","text":"xxxx',
            b'xxxx"}],"meta":{"id":"rec-1"}},"jsonrpc":"2.0","id":"gw-9"}\n',
        )
        reply = await _drain(inbox)
        assert reply["id"] == 90 and "exceeded size limit" in reply["error"]["message"]
        assert backend.is_alive

    @pytest.mark.asyncio
    async def test_the_drain_keeps_both_ends_of_an_oversize_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[bytes, bytes]] = []

        async def _record(head: bytes, tail: bytes = b"") -> None:
            seen.append((head, tail))

        backend = _make_backend()
        monkeypatch.setattr(backend, "_fail_oversize_request", _record)
        big = b'{"result":"' + b"x" * 4096 + b'","jsonrpc":"2.0","id":"gw-3"}\n'
        backend.stdout = cast(Any, _reader(big, _line({"method": "x"}), limit=1024))
        await finished(backend.run_stdout_pump(), "stdout pump")
        [(head, tail)] = seen
        assert head.startswith(b'{"result":"x') and len(head) == 512
        assert tail.endswith(b'"id":"gw-3"}\n') and len(tail) == 512

    @pytest.mark.asyncio
    async def test_unrecoverable_id_recycles_rather_than_guessing(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._pending_requests["gw-6"] = _PendingRequest("s1", 60, "tools/call")
        await backend._fail_oversize_request(b"z" * 300)
        assert "unrecoverable request id" in (backend.dead_reason or "")
        assert backend._pending_requests == {}


# --- Low-level helpers ------------------------------------------------------


class TestWriteJsonLine:
    @pytest.mark.asyncio
    async def test_serialises_one_newline_terminated_frame(self) -> None:
        writer = MagicMock()
        writer.drain = AsyncMock()
        await _write_json_line(cast(Any, writer), {"a": 1})
        assert writer.write.call_args.args[0] == b'{"a":1}\n'
        writer.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_backend_lock_is_used_when_present(self) -> None:
        backend = _make_backend()
        lock = getattr(backend.stdin, "_mc_write_lock")
        assert isinstance(lock, asyncio.Lock)
        await _write_json_line(backend.stdin, {"a": 1})
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_drain_timeout_surfaces_as_broken_pipe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "_WRITE_DRAIN_TIMEOUT_SECS", 0)
        writer = MagicMock()

        async def _slow_drain() -> None:
            # flake-ok: pre-existing drain stand-in that never finishes; the patched zero drain timeout fires first, moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing hang of the stubbed drain moved verbatim; the zeroed drain timeout cuts it short
            await asyncio.sleep(3600)

        writer.drain = _slow_drain
        with pytest.raises(BrokenPipeError, match="drain timed out"):
            await _write_json_line(cast(Any, writer), {"a": 1})


class TestPumpStderr:
    @pytest.mark.asyncio
    async def test_drains_until_eof(self) -> None:
        reader = _reader(b"line one\nline two\n")
        await _pump_stderr(reader, "kirocrew:example-mcp")
        assert reader.at_eof()

    @pytest.mark.asyncio
    async def test_oversize_line_skipped_without_wedging(self) -> None:
        reader = _reader(b"x" * 400 + b"\n" + b"short\n", limit=32)
        await _pump_stderr(reader, "kirocrew:example-mcp")
        assert reader.at_eof()

    @pytest.mark.asyncio
    async def test_reader_error_ends_the_pump(self) -> None:
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=RuntimeError("closed"))
        await _pump_stderr(cast(Any, reader), "label")
        # One read, then return: a pump that retried a closed reader would spin.
        assert reader.readline.await_count == 1


# --- One stray stdout line costs that line, never the shared pump ------------


#: Lines a backend may print that are not a routable JSON-RPC object. The pump
#: is shared by every co-pooled session, and its ``finally`` fails them all, so
#: each of these must be dropped rather than raised.
_STRAY_BACKEND_LINES = {
    **STRAY_LINES,
    "list-method": lambda: b'{"jsonrpc":"2.0","method":["x"]}\n',
    "dict-method": lambda: b'{"jsonrpc":"2.0","method":{"a":1},"id":3}\n',
}


def _roomy_reader(*chunks: bytes) -> asyncio.StreamReader:
    """``_reader`` with a limit that admits every chunk, so a deep line reaches
    the parse, not the oversize arm."""
    return _reader(*chunks, limit=max(len(c) for c in chunks) * 2 + 65536)


class TestStrayStdoutLines:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stray", sorted(_STRAY_BACKEND_LINES))
    async def test_the_pump_routes_the_next_frame_instead_of_failing_every_session(
        self, stray: str
    ) -> None:
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                _STRAY_BACKEND_LINES[stray](),
                _line({"id": "gw-1", "result": {"ok": 1}}),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")

        await finished(backend.run_stdout_pump(), "stdout pump")

        assert await _drain(inbox) == {"id": 5, "result": {"ok": 1}}
        assert backend.dead_reason == "stdout EOF"

    @pytest.mark.asyncio
    async def test_an_unparseable_response_fails_its_request_instead_of_hanging(self) -> None:
        """The id probe finds whom it answers, so its caller is failed rather
        than left to the heartbeat's hard ceiling."""
        backend = _make_backend()
        deep = too_deep_to_decode()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                ('{"jsonrpc":"2.0","id":"gw-1","result":' + deep + "}\n").encode("ascii"),
                _line({"id": "gw-2", "result": {"ok": 2}}),
            ),
        )
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")
        backend._pending_requests["gw-2"] = _PendingRequest("s2", 6, "tools/call")

        await finished(backend.run_stdout_pump(), "stdout pump")

        failed = await _drain(inbox1)
        assert failed["id"] == 5
        assert "could not be parsed" in failed["error"]["message"]
        assert await _drain(inbox2) == {"id": 6, "result": {"ok": 2}}

    @pytest.mark.asyncio
    async def test_an_unparseable_response_with_its_id_last_fails_its_request(self) -> None:
        """The MCP TypeScript SDK writes ``{result, jsonrpc, id}``, so the probe
        reads the tail of the line too."""
        backend = _make_backend()
        deep = too_deep_to_decode()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                (
                    '{"result":{"meta":{"id":"rec-1"},"x":'
                    + deep
                    + '},"jsonrpc":"2.0","id":"gw-1"}\n'
                ).encode("ascii"),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")

        await finished(backend.run_stdout_pump(), "stdout pump")

        failed = await _drain(inbox)
        assert failed["id"] == 5 and "could not be parsed" in failed["error"]["message"]

    @pytest.mark.asyncio
    async def test_a_nested_id_in_an_unparseable_notification_fails_nothing(self) -> None:
        """A server logging the call it serves echoes that call's id inside
        ``params``; failing the call off it reported a success as a failure."""
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                b'{"jsonrpc":"2.0","method":"notifications/message","params":'
                b'{"level":"debug","data":{"id":"gw-1","raw":"caf\xe9"}}}\n',
                _line({"jsonrpc": "2.0", "id": "gw-1", "result": {"ok": 1}}),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")

        await finished(backend.run_stdout_pump(), "stdout pump")

        assert await _drain(inbox) == {"jsonrpc": "2.0", "id": 5, "result": {"ok": 1}}

    @pytest.mark.asyncio
    async def test_an_unparseable_subscribe_verdict_settles_the_lease(self) -> None:
        """The parked rider is answered and the URI stops awaiting a grant, so a
        later subscriber does not park behind a lease nothing is left to settle
        (and nothing pending is left for the heartbeat to recycle)."""
        backend = _make_backend()
        inbox_a = await backend.attach_stub("A")
        inbox_b = await backend.attach_stub("B")

        def _sub(i: int) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": i,
                "method": "resources/subscribe",
                "params": {"uri": "res://x"},
            }

        await backend.forward_from_stub("A", _sub(11))
        await backend.forward_from_stub("B", _sub(22))
        (fid,) = [f for f, p in backend._pending_requests.items() if p.resource_uri]
        assert backend._lease_awaiting_grant == {"res://x"}

        await backend._route_backend_line(
            b'{"jsonrpc":"2.0","id":"' + fid.encode() + b'","error":'
            b'{"code":-32000,"message":"caf\xe9 not found"}}\n'
        )

        assert (await _drain(inbox_a))["id"] == 11
        assert (await _drain(inbox_b))["id"] == 22
        assert backend._lease_awaiting_grant == set()
        assert backend._lease_pending_riders == {}

    @staticmethod
    def _releases(backend: Backend) -> list[dict]:
        return [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", ["unparseable", "oversize"])
    async def test_a_coalesced_subscribe_reply_it_cannot_use_releases_the_lease(
        self, shape: str
    ) -> None:
        """The gateway's error is not the server's refusal: read as one, it
        skipped the release, and a lease the server granted stayed live
        upstream with nothing routing its updates."""
        backend = _make_backend()
        inbox_a = await backend.attach_stub("A")
        inbox_b = await backend.attach_stub("B")
        for stub_uuid, rid in (("A", 11), ("B", 22)):
            await backend.forward_from_stub(
                stub_uuid,
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "resources/subscribe",
                    "params": {"uri": "res://x"},
                },
            )
        (fid,) = [f for f, p in backend._pending_requests.items() if p.resource_uri]

        if shape == "unparseable":
            await backend._route_backend_line(
                b'{"jsonrpc":"2.0","id":"'
                + fid.encode()
                + b'","result":{"_meta":{"n":"caf\xe9"}}}\n'
            )
        else:
            await backend._fail_oversize_request(
                b'{"jsonrpc":"2.0","id":"' + fid.encode() + b'","result":{"x":"'
            )

        assert [r["params"]["uri"] for r in self._releases(backend)] == ["res://x"]
        assert backend._resource_subscriptions == {}
        assert "error" in await _drain(inbox_a) and "error" in await _drain(inbox_b)

    @pytest.mark.asyncio
    async def test_a_per_caller_subscribe_reply_it_cannot_use_releases_as_that_caller(
        self,
    ) -> None:
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox = await backend.attach_stub("A")
        caller = CallerContext(session_key="sess-a", from_gateway=True)
        await backend.forward_from_stub(
            "A",
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "resources/subscribe",
                "params": {"uri": "res://x"},
            },
            caller=caller,
        )
        (fid,) = [f for f, p in backend._pending_requests.items() if p.resource_uri]

        await backend._route_backend_line(
            b'{"jsonrpc":"2.0","id":"' + fid.encode() + b'","result":{"n":"caf\xe9"}}\n'
        )

        (release,) = self._releases(backend)
        assert CallerContext.from_meta(release["params"]["_meta"]).session_key == "sess-a"
        assert backend._resource_subscriptions == {}
        assert "error" in await _drain(inbox)

    @pytest.mark.asyncio
    async def test_a_refusal_the_server_sent_still_releases_nothing(self) -> None:
        """The unknown-verdict release is for the gateway's own error only."""
        backend = _make_backend()
        await backend.attach_stub("A")
        await backend.forward_from_stub(
            "A",
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "resources/subscribe",
                "params": {"uri": "res://x"},
            },
        )
        (fid,) = [f for f, p in backend._pending_requests.items() if p.resource_uri]
        await backend._route_backend_line(
            _line({"jsonrpc": "2.0", "id": fid, "error": {"code": -32000, "message": "no"}})
        )
        assert self._releases(backend) == []

    @pytest.mark.asyncio
    async def test_an_unparseable_release_verdict_settles_its_waiters(self) -> None:
        """A gateway-owned release has no stub to answer; skipping it left the
        URI awaiting release and its waiters unanswered."""
        backend = _make_backend()
        inbox = await backend.attach_stub("w")
        backend._pending_requests["gw-r"] = _PendingRequest(
            backend_mod._RELEASE_STUB_SENTINEL,
            None,
            "resources/unsubscribe",
            resource_uri="res://y",
        )
        backend._lease_awaiting_release.add("res://y")
        backend._lease_release_waiters["res://y"] = [("w", 31)]

        await backend._route_backend_line(
            b'{"jsonrpc":"2.0","id":"gw-r","error":{"message":"\xff"}}\n'
        )

        assert backend._lease_awaiting_release == set()
        assert (await _drain(inbox))["id"] == 31

    @pytest.mark.asyncio
    async def test_a_line_whose_handling_raises_costs_that_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anything one line's handling raises, past the routing guard too, costs
        that line: ending the pump would fail every co-pooled session in its
        ``finally``."""
        backend = _make_backend()
        real = backend._route_backend_line
        calls: list[bytes] = []

        async def _raise_once(line: bytes) -> None:
            calls.append(line)
            if len(calls) == 1:
                raise RuntimeError("delivery path broke")
            await real(line)

        monkeypatch.setattr(backend, "_route_backend_line", _raise_once)
        backend.stdout = cast(
            Any,
            _reader(
                _line({"method": "x"}),
                _line({"jsonrpc": "2.0", "id": "gw-1", "result": {}}),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")

        await finished(backend.run_stdout_pump(), "stdout pump")

        assert (await _drain(inbox))["id"] == 5
        assert backend.dead_reason == "stdout EOF"

    @pytest.mark.asyncio
    async def test_a_routing_raise_answers_the_request_it_had_claimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = backend_mod._tool_call_error_text

        def _boom(msg: Any) -> Any:
            # A fault in this RESPONSE's content: the error that answers it routes.
            if "result" in msg:
                raise RuntimeError("routing failed")
            return real(msg)

        monkeypatch.setattr(backend_mod, "_tool_call_error_text", _boom)
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                _line({"id": "gw-1", "result": {"ok": 1}}),
                _line({"method": "notifications/tools/list_changed"}),
            ),
        )
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 5, "tools/call", t_start_ms=time.monotonic() * 1000.0
        )

        await finished(backend.run_stdout_pump(), "stdout pump")

        failed = await _drain(inbox)
        assert failed["id"] == 5
        assert "could not be routed" in failed["error"]["message"]
        # The pump went on to the next frame.
        assert (await _drain(inbox))["method"] == "notifications/tools/list_changed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "capabilities",
        [["tools"], "x", True, [], 0, None],
        ids=["list", "str", "bool", "empty-list", "zero", "null"],
    )
    async def test_a_non_object_capabilities_reads_as_none_and_initializes(
        self, capabilities: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cosmetic field, so it must not fail the shared backend's handshake;
        falsy and truthy values are treated alike (the old ``or {}`` let ``[]``
        through as-is and failed ``["tools"]``), and no later session is
        replayed the bad value."""
        backend = _make_backend()
        reader = asyncio.StreamReader(limit=1 << 20)
        backend.stdout = cast(Any, reader)
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        init = {"jsonrpc": "2.0", "method": "initialize", "params": {"capabilities": {}}}
        await backend.forward_from_stub("s1", dict(init, id=1))
        await backend.forward_from_stub("s2", dict(init, id=7))
        (fid,) = [k for k, p in backend._pending_requests.items() if p.stub_uuid == "__init__"]
        pump = asyncio.create_task(backend.run_stdout_pump())
        with caplog.at_level(logging.WARNING, logger=backend_mod.__name__):
            reader.feed_data(
                _line({"jsonrpc": "2.0", "id": fid, "result": {"capabilities": capabilities}})
            )
            reader.feed_eof()
            await await_until(pump)

        assert backend._init_result == {"capabilities": {}}
        assert backend.supports_caller_identity is False
        for inbox, original_id in ((inbox1, 1), (inbox2, 7)):
            reply = await _drain(inbox)
            assert reply["id"] == original_id
            assert reply["result"]["capabilities"] == {}
        assert any("non-object capabilities" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_prime_refuses_a_ready_backend_that_died_while_it_waited(self) -> None:
        backend = _make_backend()
        backend._init_state = "in_flight"

        async def _ready_then_dead() -> None:
            backend._init_state = "ready"
            backend._dead_reason = "stdout EOF"
            backend._init_done_event.set()

        resolver = asyncio.create_task(_ready_then_dead())
        with pytest.raises(BackendGone, match="stdout EOF"):
            await backend.prime_initialize({"method": "initialize", "id": 1}, timeout=10)
        # flake-ok: pre-existing await of the resolver task, which sets the handshake event and returns, moved verbatim
        await resolver

    @pytest.mark.asyncio
    async def test_the_pre_init_reader_skips_stray_lines_and_initializes(self) -> None:
        result: dict[str, Any] = {"capabilities": {}}
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _roomy_reader(
                *(make() for make in _STRAY_BACKEND_LINES.values()),
                _line({"id": backend_mod._GATEWAY_INIT_ID, "result": result}),
            ),
        )
        assert await send_initialize(backend, timeout=10) == result

    @pytest.mark.asyncio
    async def test_send_initialize_reads_a_non_object_capabilities_as_none(self) -> None:
        """The same parser as the pump's handshake: one rule on both paths."""
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(
                _line({"id": backend_mod._GATEWAY_INIT_ID, "result": {"capabilities": ["tools"]}})
            ),
        )
        assert await send_initialize(backend, timeout=5) == {"capabilities": {}}
        assert backend.supports_caller_identity is False

    @pytest.mark.asyncio
    async def test_send_initialize_offers_the_newest_version_and_steps_down_once(self) -> None:
        refusal = {"code": -32602, "message": "Unsupported", "data": {"supported": ["2024-11-05"]}}
        result: dict[str, Any] = {"protocolVersion": "2024-11-05", "capabilities": {}}
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(
                _line({"id": backend_mod._GATEWAY_INIT_ID, "error": refusal}),
                _line({"id": backend_mod._GATEWAY_INIT_ID, "result": result}),
            ),
        )
        assert await send_initialize(backend, timeout=5) == result
        offered = [f["params"]["protocolVersion"] for f in _frames(backend)]
        assert offered == ["2025-06-18", "2024-11-05"]

    @pytest.mark.asyncio
    async def test_send_initialize_does_not_retry_an_unknown_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(_line({"id": backend_mod._GATEWAY_INIT_ID, "error": {"code": -32000}})),
        )
        with pytest.raises(ValueError, match="initialize error"):
            await send_initialize(backend, timeout=5)
        assert [f["params"]["protocolVersion"] for f in _frames(backend)] == ["2025-06-18"]
