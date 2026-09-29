"""What the MCP gateway daemon must keep doing, whichever module now holds the code.

``mcp_gateway/gatewayd.py`` is the daemon's executable, its import path and the one
patch surface every gatewayd test uses. These tests pin the properties a structural
change could lose without any sibling suite noticing:

* **Wire bytes.** Every reply the daemon writes on a stub or control connection is
  compared as BYTES -- separators, key order and the trailing newline -- not as a
  parsed dict, because the stub, the manager and older daemons read these frames
  and a re-ordered or re-spaced frame is a protocol change a JSON comparison cannot
  see. The keepalive probe is the one frame written with ``json.dumps`` defaults.
* **Lifecycle order.** ``run_gatewayd`` arms its background tasks in a fixed order
  and takes them down in a different fixed order; both are pinned with recording
  stand-ins patched on the facade.
* **One logger, one audit sink.** Every line the daemon logs is written on
  ``gatewayd.logger``, and every SEL row the daemon's own owners write goes
  through ``gatewayd.SecurityEventLog`` (``app_call`` and ``backend`` keep theirs),
  so a capture or a patch keyed on the facade sees all of them.
* **The ``python -m`` entry.** Run as a module the daemon is ``__main__``; a real
  child daemon started that way must still publish, in its pong, the owner it was
  given on argv, and honour an orphan stand-down only once that owner is gone.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import functools
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.admission import Admission, SpawnGate, SpawnGateClosed, SpawnGateTimeout
from kiro_crew.mcp_gateway.backend import BackendGone
from kiro_crew.mcp_gateway.host_budget import HostBudget, HostBudgetExhausted, resolve_limits
from kiro_crew.mcp_gateway.pool import BackendUnavailable, PoolAtCapacity, PoolKey
from kiro_crew.platform_compat import (
    _UTF8_PROCESS_ENV,
    kill_process_tree,
    pid_exists,
    process_start_time,
    python_launcher_hops,
)

#: This module names the daemon's spawn and declared-env seams, so it opts out of
#: the suite-wide launch-approval stand-in (``test_mcp_launch_approval.py`` checks
#: that every such module chooses). Nothing here spawns a backend.
ENFORCE_LAUNCH_APPROVAL = True

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="binds an AF_UNIX endpoint under a short temp dir; Windows serves a named "
    "pipe whose name is not a filesystem path",
)

_STUB = "stub-contract-1"
_SRC_ROOT = Path(gw.__file__).resolve().parents[2]
_REPO_ROOT = _SRC_ROOT.parent


# --- doubles -----------------------------------------------------------------


class _Writer:
    """A stream writer double that keeps every write as the exact bytes."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(bytes(payload))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Reader:
    """Hands out scripted frames, then EOFs like a closed stub transport."""

    def __init__(self, *items: Any) -> None:
        self._items = list(items)

    @property
    def remaining(self) -> int:
        return len(self._items)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if not self._items:
            raise asyncio.IncompleteReadError(b"", None)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return item
        return json.dumps(item).encode("utf-8") + b"\n"


def _register(**overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "register",
        "stub_uuid": _STUB,
        "poolable": True,
        "server_name": "demo-mcp",
        "agent_name": "contract-agent",
        "command_args_hash": "a" * 8,
        "effective_env_hash": "e" * 8,
        "work_dir": "/tmp/contract",
        "binary_version": "1.0",
        "os_uid": 1000,
        "sandbox_mode": "none",
        "autoapprove_set_hash": "b" * 8,
        "approval_mode": "reads",
        "trust_all_tools": False,
        "config_snapshot_hash": "c" * 8,
        "session_key": "sess-contract",
        "session_type": "dashboard",
        "ancestor_pids": [99_999_999_999],
    }
    frame.update(overrides)
    return frame


def _pool() -> MagicMock:
    pool = MagicMock()
    pool.unreserve = MagicMock()
    pool.release_exclusive = AsyncMock(return_value=None)
    pool.get = AsyncMock(return_value=None)
    pool.all_backends = MagicMock(return_value=[])
    pool.metrics_snapshot_async = AsyncMock(return_value={"backends": 0})
    return pool


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.control_plane = False
    backend.control_plane_denial = ""
    backend._pending_requests = {}
    backend.quarantined = False
    backend.attach_stub = AsyncMock(return_value=asyncio.Queue())
    backend.cancel_in_flight_for_stub = AsyncMock(return_value=[])
    backend.detach_stub = AsyncMock(return_value=0)
    backend.recycle_if_idle = AsyncMock()
    return backend


def _resolver(pool_key: PoolKey) -> tuple[str, list[str], dict[str, str], str]:
    return "demo-mcp-server", [], {}, pool_key.work_dir


async def _handle(reader: Any, writer: Any, pool: Any, **kwargs: Any) -> None:
    await asyncio.wait_for(
        gw._handle_connection(
            reader, writer, pool, _resolver, Path("/tmp/contract-gatewayd.sock"), **kwargs
        ),
        timeout=30,
    )


def _admission() -> Admission:
    return Admission(
        gate=SpawnGate(4, floor=1, ceiling=8),
        budget=HostBudget(
            resolve_limits(max_procs=0, max_rss_mb=0, max_fds=0, available_mb=None, max_backends=4)
        ),
        initialize_timeout_secs=10.0,
        spawn_queue_wait_secs=600.0,
    )


def _line(obj: Any) -> bytes:
    """The one serialisation every daemon reply uses: compact, then a newline."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


@pytest.fixture(autouse=True)
def _clean_daemon_registries():
    """The connection index and the probe registry are process-global."""
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()


@pytest.fixture
def peer_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positively confirmed peer with no SO_PEERCRED pid to walk."""
    monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        gw.socketsec, "check_peer_is_self", lambda w: gw.socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda w: None)


@pytest.fixture
def sel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Every SEL row the daemon writes, captured through the facade's constructor."""
    log = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", MagicMock(return_value=log))
    return log


def _registered_line(frame: dict[str, Any]) -> bytes:
    pool_key = PoolKey.from_register(frame)
    return (
        '{"type":"registered","backend_id":"pending-%s","pool_label":%s,'
        '"capabilities":["ensure_backend","bridge_ping","poolable_ack",'
        '"spawn_queue","tenant_nonce"]}\n'
        % (pool_key.stable_hash()[:12], json.dumps(pool_key.human_readable()))
    ).encode("utf-8")


# --- wire bytes: the stub session --------------------------------------------


class TestStubSessionBytes:
    @pytest.mark.asyncio
    async def test_a_session_writes_these_bytes_in_this_order(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None, sel: MagicMock
    ) -> None:
        """Registered, ready, a bridge pong, then the terminal JSON-RPC error a
        backend death with no recovery answers the in-flight request with."""
        backend = _backend()
        backend.forward_from_stub = AsyncMock(side_effect=BackendGone("dead"))
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        monkeypatch.setattr(gw, "_respawn_backend_for_stub", AsyncMock(return_value=None))
        frame = _register()
        writer = _Writer()
        await _handle(
            _Reader(
                frame,
                {"type": "ensure_backend"},
                {"type": "ping"},
                {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            ),
            writer,
            _pool(),
        )
        assert writer.writes == [
            _registered_line(_register()),
            b'{"type":"ready"}\n',
            b'{"type":"pong"}\n',
            b'{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"backend gone: dead"}}\n',
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "poolable", "expected"),
        [
            (
                gw._TargetUnknown("no target mapping"),
                True,
                b'{"type":"rejected","reason":"no target mapping","class":"compat",'
                b'"fallback":true}\n',
            ),
            (
                PoolAtCapacity("pool full"),
                True,
                b'{"type":"rejected","reason":"pool full","class":"capacity",'
                b'"retry_after_secs":30}\n',
            ),
            (
                BackendUnavailable("breaker open"),
                True,
                b'{"type":"rejected","reason":"breaker open","class":"capacity",'
                b'"retry_after_secs":60}\n',
            ),
            (
                OSError(errno.ENOENT, "no such binary"),
                True,
                b'{"type":"rejected","reason":"backend spawn failed: [Errno 2] no such '
                b'binary","class":"compat","fallback":true}\n',
            ),
            (
                OSError(errno.ENOENT, "no such binary"),
                False,
                b'{"type":"rejected","reason":"backend spawn failed: [Errno 2] no such '
                b'binary","class":"isolation","fallback":true}\n',
            ),
            (
                OSError(errno.ENOMEM, "out of memory"),
                True,
                b'{"type":"rejected","reason":"backend spawn failed: [Errno 12] out of '
                b'memory","class":"capacity","retry_after_secs":30}\n',
            ),
            (
                RuntimeError("boom"),
                True,
                b'{"type":"rejected","reason":"internal error: boom"}\n',
            ),
        ],
        ids=[
            "compat",
            "capacity",
            "breaker",
            "oserror-pooled",
            "oserror-private",
            "pressure",
            "internal",
        ],
    )
    async def test_an_ensure_backend_refusal_is_one_classed_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
        peer_ok: None,
        sel: MagicMock,
        exc: BaseException,
        poolable: bool,
        expected: bytes,
    ) -> None:
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=exc))
        frame = _register(poolable=poolable)
        writer = _Writer()
        await _handle(_Reader(frame, {"type": "ensure_backend"}), writer, _pool())
        assert writer.writes == [_registered_line(_register(poolable=poolable)), expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (gw._TargetUnknown("no target"), b'{"type":"rejected","reason":"no target"}\n'),
            (
                PoolAtCapacity("pool full"),
                b'{"type":"rejected","reason":"pool full","class":"capacity",'
                b'"retry_after_secs":30}\n',
            ),
            (
                BackendUnavailable("breaker open"),
                b'{"type":"rejected","reason":"breaker open","class":"capacity",'
                b'"retry_after_secs":30}\n',
            ),
            (
                SpawnGateClosed("draining"),
                b'{"type":"rejected","reason":"draining","class":"capacity",'
                b'"retry_after_secs":30}\n',
            ),
            (
                SpawnGateTimeout(3, 4, 20.0),
                b'{"type":"rejected","reason":"spawn gate wait budget exhausted after 20s '
                b'(position 3, capacity 4)","class":"capacity","retry_after_secs":30}\n',
            ),
            (
                HostBudgetExhausted("procs", 1, 4, 4),
                b'{"type":"rejected","reason":"host budget exhausted on procs: in_use=4 + '
                b'wanted=1 > ceiling=4","class":"capacity","retry_after_secs":30}\n',
            ),
            (
                OSError(errno.ENOMEM, "out of memory"),
                b'{"type":"rejected","reason":"backend spawn failed: [Errno 12] out of '
                b'memory"}\n',
            ),
            (
                RuntimeError("boom"),
                b'{"type":"rejected","reason":"backend spawn failed: boom"}\n',
            ),
        ],
        ids=[
            "target",
            "capacity",
            "breaker",
            "closed",
            "gate-timeout",
            "budget",
            "oserror",
            "crash",
        ],
    )
    async def test_a_legacy_lazy_spawn_refusal_carries_no_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        peer_ok: None,
        sel: MagicMock,
        exc: BaseException,
        expected: bytes,
    ) -> None:
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=exc))
        writer = _Writer()
        await _handle(
            _Reader(_register(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            writer,
            _pool(),
        )
        assert writer.writes == [_registered_line(_register()), expected]

    @pytest.mark.asyncio
    async def test_a_disconnect_that_cancels_in_flight_work_is_audited(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None, sel: MagicMock
    ) -> None:
        backend = _backend()
        backend.cancel_in_flight_for_stub = AsyncMock(return_value=["r1", "r2"])
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        await _handle(_Reader(_register(), {"type": "ensure_backend"}), _Writer(), _pool())
        rows = [
            call.kwargs
            for call in sel.log_api_access.call_args_list
            if call.kwargs["operation"] == "mcp-gateway.disconnect-cancel"
        ]
        assert rows == [
            {
                "caller": "gatewayd",
                "operation": "mcp-gateway.disconnect-cancel",
                "outcome": "cancelled",
                "source": "gateway",
                "resources": f"stub={_STUB} refcount=0",
                "error": "cancelled=2 in-flight on stub disconnect",
            }
        ]
        backend.detach_stub.assert_awaited_once_with(_STUB)

    @pytest.mark.asyncio
    async def test_the_keepalive_probe_is_written_with_json_defaults(self) -> None:
        """The one frame not written by ``_write_json_line``: ``json.dumps``
        defaults, spaces included."""
        writer = _Writer()
        idle = asyncio.create_task(asyncio.Event().wait())
        probe = gw._StubProbe(_STUB, writer, idle)  # type: ignore[arg-type]
        gw._stub_probe_add(probe)
        try:
            assert await asyncio.wait_for(gw._probe_stub_transports(), timeout=10) == 0
        finally:
            gw._stub_probe_discard(probe)
            idle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle
        assert writer.writes == [b'{"type": "keepalive"}\n']


# --- wire bytes: one-shot control frames --------------------------------------


class TestControlFrameBytes:
    @pytest.mark.asyncio
    async def test_a_ping_is_answered_with_the_pong_fields_in_this_order(
        self, peer_ok: None
    ) -> None:
        writer = _Writer()
        await _handle(_Reader({"type": "ping"}), writer, _pool())
        assert len(writer.writes) == 1
        pong = json.loads(writer.writes[0])
        assert list(pong) == ["type", "targets", "fingerprint", "owner_pid", "pid", "start_time"]
        assert writer.writes[0] == _line(pong)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("frame", "expected"),
        [
            (
                {"type": "claim"},
                b'{"type":"claim-rejected","reason":"malformed claim: pid=None '
                b"session_key=''\"}\n",
            ),
            (
                {"type": "claim", "pid": 99_999_999_999, "session_key": "sess-x"},
                b'{"type":"claim-noop","updated":0,"connections":0}\n',
            ),
            ({"type": "abort"}, b'{"type":"abort-rejected","reason":"missing or invalid pids"}\n'),
            (
                {"type": "abort", "pids": [1]},
                b'{"type":"abort-rejected","reason":"no valid pids"}\n',
            ),
            (
                {"type": "abort", "pids": [99_999_999_999]},
                b'{"type":"aborted","cancelled":0,"stubs":0}\n',
            ),
            (
                {"type": "set-spawn-capacity", "capacity": 3},
                b'{"type":"spawn-capacity-rejected","reason":"no admission on this daemon"}\n',
            ),
            (
                {"type": "stand-down", "need": ["NOPE_STEM"]},
                b'{"type":"stand-down-rejected","reason":"shutdown not wired on this handler"}\n',
            ),
            ({"type": "not-a-control-frame"}, None),
        ],
        ids=[
            "claim-malformed",
            "claim-noop",
            "abort-missing",
            "abort-empty",
            "abort-applied",
            "capacity-no-admission",
            "stand-down-unwired",
            "unknown",
        ],
    )
    async def test_a_control_frame_is_acknowledged_in_one_line(
        self, peer_ok: None, sel: MagicMock, frame: dict[str, Any], expected: bytes | None
    ) -> None:
        writer = _Writer()
        await _handle(_Reader(frame), writer, _pool())
        assert writer.writes == ([] if expected is None else [expected])

    @pytest.mark.asyncio
    async def test_a_stand_down_that_is_honoured_says_why(
        self, peer_ok: None, sel: MagicMock
    ) -> None:
        stop = asyncio.Event()
        writer = _Writer()
        await _handle(
            _Reader({"type": "stand-down", "need": ["NOPE_STEM"]}),
            writer,
            _pool(),
            stop_event=stop,
        )
        assert stop.is_set()
        assert writer.writes == [
            b'{"type":"standing-down","missing":["NOPE_STEM"],"stale_code":false,'
            b'"orphaned":false}\n'
        ]

    @pytest.mark.asyncio
    async def test_a_capacity_move_reports_what_took_effect_first(self, peer_ok: None) -> None:
        admission = Admission(
            gate=SpawnGate(4, floor=1, ceiling=8),
            budget=HostBudget(
                resolve_limits(
                    max_procs=0, max_rss_mb=0, max_fds=0, available_mb=None, max_backends=4
                )
            ),
            initialize_timeout_secs=10.0,
            spawn_queue_wait_secs=600.0,
        )
        writer = _Writer()
        try:
            await _handle(
                _Reader({"type": "set-spawn-capacity", "capacity": 99}),
                writer,
                _pool(),
                admission=admission,
            )
        finally:
            await admission.close()
        assert len(writer.writes) == 1
        assert writer.writes[0].startswith(b'{"type":"spawn-capacity","capacity":8,')
        assert writer.writes[0] == _line(json.loads(writer.writes[0]))

    @pytest.mark.asyncio
    async def test_stats_fold_the_fallback_tally_after_the_pool_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None
    ) -> None:
        monkeypatch.setattr(gw, "stub_fallback_counts", lambda: {"terminal": 2})
        writer = _Writer()
        await _handle(_Reader({"type": "stats"}), writer, _pool())
        assert writer.writes == [b'{"type":"stats","backends":0,"stub_fallbacks":{"terminal":2}}\n']


# --- one logger, one audit sink ------------------------------------------------


class TestOneLoggerOneAuditSink:
    @pytest.mark.asyncio
    async def test_every_line_is_written_on_the_facade_logger(
        self, caplog: pytest.LogCaptureFixture, sel: MagicMock
    ) -> None:
        """A capture keyed on ``gatewayd.logger`` sees framing, control, token-fence
        and liveness lines alike."""
        with caplog.at_level(logging.DEBUG, logger=gw.logger.name):
            await gw._read_first_frame(_Reader(asyncio.IncompleteReadError(b"par", None)))
            await gw._apply_claim({"type": "claim"})
            gw._deny_control_plane("kirocrew-core", "contract probe")
            await gw._apply_abort({"type": "abort", "pids": [99_999_999_999]}, _pool())
        names = {record.name for record in caplog.records}
        messages = " | ".join(record.getMessage() for record in caplog.records)
        assert "partial first frame" in messages
        assert "claim rejected" in messages
        assert "contract probe" in messages
        assert "abort applied" in messages
        assert names == {gw.logger.name}

    @pytest.mark.asyncio
    async def test_every_audit_row_goes_through_the_facade_constructor(
        self, peer_ok: None, sel: MagicMock
    ) -> None:
        stop = asyncio.Event()
        await _handle(_Reader({"type": "claim"}), _Writer(), _pool())
        await _handle(_Reader({"type": "abort"}), _Writer(), _pool())
        await _handle(
            _Reader({"type": "stand-down", "need": ["X"]}), _Writer(), _pool(), stop_event=stop
        )
        await _handle(
            _Reader(_register(stub_uuid=f"{gw.INTERNAL_STUB_PREFIXES[0]}x")), _Writer(), _pool()
        )
        operations = [call.kwargs["operation"] for call in sel.log_api_access.call_args_list]
        assert operations == [
            "mcp-gateway.caller-claim",
            "mcp-gateway.abort-in-flight",
            "mcp-gateway.stand_down",
            "mcp-gateway.reserved-stub-prefix-denied",
        ]


# --- connection phases --------------------------------------------------------


class TestConnectionPhases:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supported", "verdict", "owner_only"),
        [(True, "MISMATCH", True), (True, "UNVERIFIABLE", True), (False, "UNVERIFIABLE", False)],
        ids=["mismatch", "unverifiable", "unsupported-not-owner-only"],
    )
    async def test_a_refused_peer_never_has_its_first_frame_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sel: MagicMock,
        supported: bool,
        verdict: str,
        owner_only: bool,
    ) -> None:
        """The peer gate precedes every control frame: nothing read, nothing written."""
        monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", supported)
        result = getattr(gw.socketsec.PeerCredResult, verdict)
        monkeypatch.setattr(gw.socketsec, "check_peer_is_self", lambda w: result)
        monkeypatch.setattr(gw.socketsec, "socket_owner_only", lambda p: owner_only)
        reader = _Reader({"type": "claim", "pid": 99_999_999_999, "session_key": "s"})
        writer = _Writer()
        await _handle(reader, writer, _pool())
        assert reader.remaining == 1
        assert writer.writes == []
        assert [c.kwargs["operation"] for c in sel.log_api_access.call_args_list] == [
            "mcp-gateway.connect"
        ]

    @pytest.mark.asyncio
    async def test_a_first_frame_claim_evicts_the_retargeted_stubs_subscriptions(
        self, peer_ok: None, sel: MagicMock
    ) -> None:
        """The claim dispatch hands the daemon's pool on, so a changed owner stops
        receiving the previous owner's resource updates."""
        conn = gw._StubConn(
            _STUB,
            [99_999_999_999],
            "label",
            CallerContext(session_key="old", session_type="x", from_gateway=True),
        )
        gw._conn_index_add(conn)
        backend = MagicMock()
        backend.evict_stub_subscriptions = AsyncMock()
        pool = _pool()
        pool.backends_hosting_stub = MagicMock(return_value=[backend])
        writer = _Writer()
        await _handle(
            _Reader({"type": "claim", "pid": 99_999_999_999, "session_key": "new"}), writer, pool
        )
        assert writer.writes == [b'{"type":"claimed","updated":1,"connections":1,"skipped":0}\n']
        backend.evict_stub_subscriptions.assert_awaited_once_with(_STUB)
        assert conn.caller is not None and conn.caller.session_key == "new"

    @pytest.mark.asyncio
    async def test_the_bridge_is_probed_on_its_own_task_until_it_ends(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None, sel: MagicMock
    ) -> None:
        seen: dict[str, Any] = {}
        backend = _backend()

        async def acquire(*_a: Any, **_k: Any) -> tuple[Any, bool]:
            seen["probes"] = list(gw._STUB_PROBES)
            seen["indexed"] = {pid: set(c) for pid, c in gw._CONN_INDEX.items()}
            return backend, True

        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        writer = _Writer()
        handler = asyncio.create_task(
            gw._handle_connection(
                _Reader(_register(), {"type": "ensure_backend"}),
                writer,  # type: ignore[arg-type]
                _pool(),
                _resolver,
                Path("/tmp/contract-gatewayd.sock"),
            )
        )
        await asyncio.wait_for(handler, timeout=30)
        (probe,) = seen["probes"]
        assert probe.stub_uuid == _STUB and probe.writer is writer
        assert probe.task is handler
        assert [c.stub_uuid for c in seen["indexed"][99_999_999_999]] == [_STUB]
        assert gw._STUB_PROBES == set() and gw._CONN_INDEX == {}

    @pytest.mark.asyncio
    async def test_a_token_bound_to_another_runtime_leaves_the_stub_unnamed(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None, sel: MagicMock
    ) -> None:
        """Even a self-reported session key does not survive a token some claim
        bound to a runtime this peer is not attested under."""
        bound = CallerContext(session_key="sess-bound", session_type="x", from_gateway=True)
        monkeypatch.setitem(gw._TOKEN_BINDINGS, "tok-other", (bound, 99_999_999_998, None))
        backend = _backend()
        backend.forward_from_stub = AsyncMock()
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        await _handle(
            _Reader(
                _register(stub_session_token="tok-other"),
                {"type": "ensure_backend"},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            ),
            _Writer(),
            _pool(),
        )
        assert backend.forward_from_stub.await_args.kwargs["caller"] is None
        rows = [c.kwargs for c in sel.log_api_access.call_args_list]
        denied = [r for r in rows if r["operation"] == "mcp-gateway.peer-identity-denied"]
        assert len(denied) == 1
        assert "session token not claimed from this peer's attested runtime" in (
            denied[0]["resources"]
        )
        allowed = [r for r in rows if r["operation"] == "mcp-gateway.connect"]
        assert [r["caller"] for r in allowed] == ["unknown"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stage", ["ensure_backend", "lazy", "respawn"])
    async def test_a_stub_that_leaves_mid_acquire_is_answered_nothing(
        self, monkeypatch: pytest.MonkeyPatch, peer_ok: None, sel: MagicMock, stage: str
    ) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked(*_a: Any, **_k: Any) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        backend = _backend()
        backend.forward_from_stub = AsyncMock(side_effect=BackendGone("dead"))
        if stage == "respawn":
            monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
            monkeypatch.setattr(gw, "_respawn_backend_for_stub", blocked)
            frames: tuple[Any, ...] = (
                {"type": "ensure_backend"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call"},
            )
        else:
            monkeypatch.setattr(gw, "_acquire_backend", blocked)
            frames = (
                ({"type": "ensure_backend"},)
                if stage == "ensure_backend"
                else ({"jsonrpc": "2.0", "id": 2, "method": "initialize"},)
            )
        writer = _Writer()
        await _handle(_Reader(_register(), *frames), writer, _pool())
        assert started.is_set() and cancelled.is_set()
        expected = [_registered_line(_register())]
        if stage == "respawn":
            expected.append(b'{"type":"ready"}\n')
        assert writer.writes == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("frames", "with_admission", "deadline_in", "queue_aware"),
        [
            (({"type": "ensure_backend"},), False, None, False),
            (({"type": "ensure_backend"},), True, 20.0, False),
            (({"type": "ensure_backend", "wait_budget_secs": 100},), True, 95.0, True),
            (({"jsonrpc": "2.0", "id": 1, "method": "initialize"},), False, None, None),
            (({"jsonrpc": "2.0", "id": 1, "method": "initialize"},), True, 20.0, None),
        ],
        ids=["bare-ungated", "bare-gated", "queue-aware", "lazy-ungated", "lazy-gated"],
    )
    async def test_the_wait_an_acquire_is_given_follows_what_the_stub_negotiated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        peer_ok: None,
        sel: MagicMock,
        frames: tuple[Any, ...],
        with_admission: bool,
        deadline_in: float | None,
        queue_aware: bool | None,
    ) -> None:
        acquire = AsyncMock(return_value=(_backend(), True))
        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        admission = _admission() if with_admission else None
        before = time.monotonic()
        try:
            await _handle(_Reader(_register(), *frames), _Writer(), _pool(), admission=admission)
        finally:
            if admission is not None:
                await admission.close()
        kwargs = acquire.await_args.kwargs
        assert kwargs["admission"] is admission
        assert kwargs["exclusive_stub_uuid"] == ""
        if deadline_in is None:
            assert kwargs["wait_deadline"] is None
        else:
            assert before + deadline_in <= kwargs["wait_deadline"] <= time.monotonic() + deadline_in
        if queue_aware is None:
            assert "on_queued" not in kwargs
        else:
            assert (kwargs["on_queued"] is not None) is queue_aware


# --- lifecycle order -----------------------------------------------------------


def _recording(name: str, events: list[tuple[str, str]]) -> Any:
    async def run(*_args: Any, **_kwargs: Any) -> None:
        events.append(("start", name))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(("cancel", name))
            raise

    return run


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_run_gatewayd_arms_and_retires_its_tasks_in_a_fixed_order(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Armed in bind order; retired with the prewarm top-up and the credential
    watchers ahead of the hot-key flush, whose final write comes last."""
    events: list[tuple[str, str]] = []
    # ``run_gatewayd`` publishes these for the life of the process; put them back.
    monkeypatch.setattr(gw, "_OWNER_PID", gw._OWNER_PID)
    monkeypatch.setattr(gw, "_OWN_START_TIME", gw._OWN_START_TIME)
    for name in (
        "_idle_sweeper",
        "_backend_tmp_sweeper",
        "_socket_liveness_sweeper",
        "_owner_liveness_sweeper",
        "_zombie_diagnostic",
        "_heartbeat_sweeper",
        "_hot_keys_flush_sweeper",
        "_prewarm_topup_sweeper",
    ):
        monkeypatch.setattr(gw, name, _recording(name, events))
    monkeypatch.setattr(gw.credwatch, "watch_credential", _recording("watch_credential", events))
    credential = tmp_path / "credential"
    credential.write_text("v1", encoding="utf-8")
    stop = asyncio.Event()
    run = asyncio.create_task(
        gw.run_gatewayd(
            short_sock_dir / "order.sock",
            max_backends=4,
            idle_timeout_secs=60,
            stop_event=stop,
            target_resolver=_resolver,
            prewarm_count=1,
            credential_watch_paths=[credential],
            owner_pid=os.getpid(),
        )
    )
    try:
        deadline = time.monotonic() + 30
        while len(events) < 9:
            assert time.monotonic() < deadline, f"tasks never armed: {events}"
            assert not run.done(), run
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(run, timeout=30)
    finally:
        stop.set()
        if not run.done():
            run.cancel()
            with contextlib.suppress(BaseException):
                await run
    started = [name for kind, name in events if kind == "start"]
    retired = [name for kind, name in events if kind == "cancel"]
    assert started == [
        "_idle_sweeper",
        "_backend_tmp_sweeper",
        "_socket_liveness_sweeper",
        "_owner_liveness_sweeper",
        "_zombie_diagnostic",
        "_heartbeat_sweeper",
        "_hot_keys_flush_sweeper",
        "_prewarm_topup_sweeper",
        "watch_credential",
    ]
    assert retired == [
        "_idle_sweeper",
        "_backend_tmp_sweeper",
        "_socket_liveness_sweeper",
        "_owner_liveness_sweeper",
        "_zombie_diagnostic",
        "_heartbeat_sweeper",
        "_prewarm_topup_sweeper",
        "watch_credential",
        "_hot_keys_flush_sweeper",
    ]


# --- the python -m entry --------------------------------------------------------


async def _control(sock: Path, frame: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(transport.connect(sock), timeout=15)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=15)
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=15)
        reply = json.loads(line.decode("utf-8"))
        assert isinstance(reply, dict)
        return reply
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=15)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and whatever it started: its own process group on POSIX, the
    tree on Windows, where a venv's ``python.exe`` is a redirector with a child."""
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(Exception):
            kill_process_tree(proc.pid)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        _kill_tree(proc)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=30)


def _log_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


@pytest.mark.asyncio
async def test_the_module_entry_point_publishes_its_owner_and_honours_an_orphan_claim(
    short_sock_dir: Path, tmp_path: Path
) -> None:
    """``python -m kiro_crew.mcp_gateway.gatewayd`` runs the daemon as ``__main__``.

    The owner PID it was given reaches the pong, and an orphan stand-down is
    refused while that owner lives and honoured once it is gone -- both read the
    lifecycle state the running ``run_gatewayd`` published. Every line it logs,
    whichever module wrote it, carries the ``__main__`` logger it always has.
    """
    home = tmp_path / "home"
    home.mkdir()
    sock = short_sock_dir / "entry.sock"
    env = {
        **os.environ,
        # The UTF-8 process setup the gateway gives the daemon it spawns.
        **_UTF8_PROCESS_ENV,
        "KIROCREW_HOME": str(home),
        "PYTHONPATH": str(_SRC_ROOT),
    }
    group: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(600)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(home),
        **group,
    )
    with (tmp_path / "gatewayd.log").open("wb") as log:
        daemon = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kiro_crew.mcp_gateway.gatewayd",
            "--socket",
            str(sock),
            "--idle-timeout-secs",
            "60",
            "--max-backends",
            "1",
            "--owner-pid",
            str(owner.pid),
            "--log-level",
            "INFO",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            cwd=str(home),
            **group,
        )
    try:
        from kiro_crew.mcp_gateway.daemon_control import describe_daemon

        deadline = time.monotonic() + 90
        info = None
        while info is None:
            assert daemon.returncode is None, _log_text(tmp_path / "gatewayd.log")
            assert time.monotonic() < deadline, "the daemon never answered a ping"
            info = await asyncio.to_thread(describe_daemon, sock)
            if info is None:
                await asyncio.sleep(0.1)
        assert info.owner_pid == owner.pid
        assert info.start_time
        if python_launcher_hops() == 0:
            # The pid spawned IS the interpreter; behind a Windows venv redirector
            # the daemon is that launcher's child and only its liveness is known here.
            assert info.pid == daemon.pid
            started = process_start_time(daemon.pid)
            assert started is None or info.start_time == started
        else:
            assert pid_exists(info.pid)

        refused = await _control(sock, {"type": "stand-down", "orphaned": True})
        assert refused == {
            "type": "stand-down-rejected",
            "reason": "missing or invalid 'need' stem list",
        }

        _kill_tree(owner)
        await asyncio.wait_for(owner.wait(), timeout=30)
        deadline = time.monotonic() + 30
        while pid_exists(owner.pid):
            assert time.monotonic() < deadline, "the killed owner never went away"
            await asyncio.sleep(0.05)
        honoured = await _control(sock, {"type": "stand-down", "orphaned": True})
        assert honoured == {
            "type": "standing-down",
            "missing": [],
            "stale_code": False,
            "orphaned": True,
        }
        await asyncio.wait_for(daemon.wait(), timeout=60)
        log = _log_text(tmp_path / "gatewayd.log")
        assert daemon.returncode == 0, log
        # One facade namespace, the running ``__main__``: the bootstrap's own line and
        # a control frame's line both carry its logger, and no second copy of the
        # module was imported to answer the control frame.
        assert " INFO __main__ gatewayd listening socket=" in log, log
        assert " WARNING __main__ gatewayd: standing down on request" in log, log
        assert "kiro_crew.mcp_gateway.gatewayd " not in log, log
    finally:
        await _reap(daemon)
        await _reap(owner)


# --- composition: one namespace over the owners -----------------------------

_DAEMON_DIR = Path(gw.__file__).with_name("daemon")
_FACADE_MODULE = "kiro_crew.mcp_gateway.gatewayd"

#: What ``from kiro_crew.mcp_gateway.gatewayd import *`` has always carried of the
#: daemon's own definitions.
_PUBLIC_NAMES = frozenset(
    {
        "CONTROL_PLANE_BACKENDS",
        "REGISTERED_CAPABILITIES",
        "REJECT_CLASS_CAPACITY",
        "REJECT_CLASS_COMPAT",
        "REJECT_CLASS_ISOLATION",
        "STUB_KEEPALIVE_TYPE",
        "TargetResolver",
        "env_target_resolver",
        "logger",
        "main",
        "resolvable_target_stems",
        "resolve_once_resolver",
        "run_gatewayd",
    }
)


def _owner_names() -> list[str]:
    return sorted(p.stem for p in _DAEMON_DIR.glob("*.py") if p.stem != "__init__")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defined_at_module_level(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _is_facade_import(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and "import_module" in ast.unparse(node.func)
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE_MODULE
    )


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every name a test module can hold the facade module under.

    Import spellings, ``import_module`` / ``sys.modules`` assignments, plain
    re-aliasing (to a fixed point), and a fixture -- any function -- that returns
    the facade, whose NAME is then what a test receives it as.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.mcp_gateway":
            aliases.update(a.asname or a.name for a in node.names if a.name == "gatewayd")
        elif isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == _FACADE_MODULE and a.asname)
        elif isinstance(node, ast.Assign) and (
            _is_facade_import(node.value)
            or (
                isinstance(node.value, ast.Subscript)
                and "sys.modules" in ast.unparse(node.value)
                and _FACADE_MODULE in ast.unparse(node.value)
            )
        ):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        grew = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name not in aliases
            ):
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        value = ret.value
                        if (isinstance(value, ast.Name) and value.id in aliases) or (
                            _is_facade_import(value)
                        ):
                            aliases.add(node.name)
                            grew = True
                            break
    return aliases


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _parametrized_values(fn: ast.AST, argname: str) -> list[ast.AST] | None:
    """The literal values ``@pytest.mark.parametrize`` gives ``argname``, or ``None``."""
    for deco in getattr(fn, "decorator_list", []):
        if not (isinstance(deco, ast.Call) and ast.unparse(deco.func).endswith("parametrize")):
            continue
        if len(deco.args) < 2 or not isinstance(deco.args[0], (ast.Constant, ast.Tuple, ast.List)):
            continue
        spec = deco.args[0]
        if isinstance(spec, ast.Constant):
            names = [n.strip() for n in str(spec.value).split(",")]
        else:
            names = [e.value for e in spec.elts if isinstance(e, ast.Constant)]
        if argname not in names:
            continue
        rows = deco.args[1]
        if not isinstance(rows, (ast.List, ast.Tuple)):
            return None
        index = names.index(argname)
        values: list[ast.AST] = []
        for row in rows.elts:
            if isinstance(row, ast.Call) and ast.unparse(row.func).endswith("param"):
                row = ast.Tuple(elts=list(row.args), ctx=ast.Load())
            if len(names) == 1:
                values.append(row)
            elif isinstance(row, (ast.Tuple, ast.List)) and index < len(row.elts):
                values.append(row.elts[index])
            else:
                return None
        return values
    return None


def _resolve_names(expr: ast.AST, at: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[str] | None:
    """The literal names ``expr`` can take where it is read, or ``None`` if unknowable."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if not isinstance(expr, ast.Name):
        return None
    node = at
    while node in parents:
        node = parents[node]
        if (
            isinstance(node, (ast.For, ast.AsyncFor))
            and isinstance(node.target, ast.Name)
            and node.target.id == expr.id
        ):
            if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts
            ):
                return {str(e.value) for e in node.iter.elts}  # type: ignore[attr-defined]
            return None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            values = _parametrized_values(node, expr.id)
            if values is None:
                return None
            found: set[str] = set()
            for value in values:
                if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    return None
                found.add(value.value)
            return found
    return None


def _patched_facade_names(source: str) -> tuple[set[str], list[tuple[int, str]]]:
    """Every name ``source`` replaces ON the facade module, and every site it cannot read.

    Spellings: ``monkeypatch.setattr`` / ``delattr`` / ``patch.object`` on a facade
    alias, positionally or through ``target=`` / ``name=`` / ``attribute=``; a dotted
    string target naming exactly ``gatewayd.<name>``; ``patch.multiple`` keywords;
    ``patch.dict`` / ``monkeypatch.setitem`` / ``delitem`` on the facade's
    ``__dict__``; and a plain attribute store. A name held in a variable resolves
    through an enclosing ``for`` over literals or the test's ``parametrize`` values.
    Anything else aimed at the facade is returned as an unresolved site -- the
    detector never drops one silently. A dotted target one level deeper
    (``gatewayd.os.stat``) patches an attribute of a shared object, not a name the
    owners read through the facade, so it is not one.
    """
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    parents = _parents(tree)

    def is_facade(node: ast.AST | None) -> bool:
        return (isinstance(node, ast.Name) and node.id in aliases) or (
            isinstance(node, ast.Attribute) and ast.unparse(node) == _FACADE_MODULE
        )

    def is_facade_dict(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "__dict__" and is_facade(node.value)

    names: set[str] = set()
    unresolved: list[tuple[int, str]] = []

    def take(expr: ast.AST | None, at: ast.AST) -> None:
        resolved = None if expr is None else _resolve_names(expr, at, parents)
        if resolved is None:
            fn = _enclosing_function(at, parents)
            unresolved.append((getattr(at, "lineno", 0), fn.name if fn else "<module>"))
        else:
            names.update(resolved)

    def arg(call: ast.Call, position: int, *keywords: str) -> ast.AST | None:
        if len(call.args) > position:
            return call.args[position]
        return next((kw.value for kw in call.keywords if kw.arg in keywords), None)

    prefix = _FACADE_MODULE + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if is_facade(node.value):
                names.add(node.attr)
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        target = arg(node, 0, "target")
        if callee.endswith(("setattr", "delattr", ".object")) and is_facade(target):
            take(arg(node, 1, "name", "attribute"), node)
        elif callee.endswith("multiple") and is_facade(target):
            for kw in node.keywords:
                if kw.arg is None:
                    take(None, node)
                elif kw.arg not in (
                    "target",
                    "spec",
                    "create",
                    "spec_set",
                    "autospec",
                    "new_callable",
                ):
                    names.add(kw.arg)
        elif callee.endswith(("setitem", "delitem")) and is_facade_dict(target):
            take(arg(node, 1, "name"), node)
        elif callee.endswith(".dict") and is_facade_dict(target):
            values = arg(node, 1, "values")
            if isinstance(values, ast.Dict):
                for key in values.keys:
                    take(key, node)
            elif values is not None:
                take(None, node)
            for kw in node.keywords:
                if kw.arg is None:
                    take(None, node)
                elif kw.arg not in ("values", "clear"):
                    names.add(kw.arg)
        if callee.endswith(("setattr", "delattr", "patch")) and target is not None:
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                rest = target.value[len(prefix) :]
                if target.value.startswith(prefix) and rest and "." not in rest:
                    names.add(rest)
    return names, unresolved


@functools.lru_cache(maxsize=1)
def _patched_names_in_the_suite() -> tuple[frozenset[str], tuple[str, ...]]:
    """Names the whole suite patches on the facade, and any patch site it cannot read."""
    roots = [_REPO_ROOT / "test", _SRC_ROOT / "kiro_crew"]
    found: set[str] = set()
    unreadable: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if root.name == "kiro_crew" and "tests" not in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "gatewayd" in text:
                names, unresolved = _patched_facade_names(text)
                found |= names
                rel = path.relative_to(_REPO_ROOT)
                unreadable.extend(f"{rel}:{line} in {fn}" for line, fn in unresolved)
    return frozenset(found), tuple(unreadable)


def _function_bound_names(fn: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spec = node.args
            for arg in spec.posonlyargs + spec.args + spec.kwonlyargs:
                bound.add(arg.arg)
            bound.update(a.arg for a in (spec.vararg, spec.kwarg) if a is not None)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
    return bound


def _bare_reads(tree: ast.Module, names: frozenset[str]) -> list[tuple[int, str]]:
    """Reads of ``names`` as plain globals inside any function body of ``tree``."""
    hits: set[tuple[int, str]] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _function_bound_names(fn)
        for statement in fn.body:
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in names
                    and node.id not in local
                ):
                    hits.add((node.lineno, node.id))
    return sorted(hits)


def _runtime_imports(tree: ast.Module) -> list[str]:
    """Modules ``tree`` imports when it runs: a ``TYPE_CHECKING`` block does not count."""
    skipped: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            skipped.update(id(n) for stmt in node.body for n in ast.walk(stmt))
    modules: list[str] = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
            modules.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
    return modules


class TestComposition:
    def test_every_owner_name_is_bound_on_the_facade_to_the_owners_object(self) -> None:
        import importlib

        facade_defines = set(_defined_at_module_level(_parse(Path(gw.__file__))))
        problems: list[str] = []
        for leaf in _owner_names():
            owner = importlib.import_module(f"kiro_crew.mcp_gateway.daemon.{leaf}")
            for name in _defined_at_module_level(_parse(_DAEMON_DIR / f"{leaf}.py")):
                if name in facade_defines:
                    problems.append(f"{leaf}.{name} is defined again by the facade")
                elif not hasattr(gw, name):
                    problems.append(f"{leaf}.{name} is not bound on the facade")
                elif getattr(gw, name) is not getattr(owner, name):
                    problems.append(f"{leaf}.{name} is a different object on the facade")
        assert problems == []

    def test_the_public_surface_is_the_one_it_always_was(self) -> None:
        public = {name for name in vars(gw) if not name.startswith("_")}
        assert _PUBLIC_NAMES <= public
        assert gw.run_gatewayd.__module__ == _FACADE_MODULE
        assert gw.main.__module__ == _FACADE_MODULE

    def test_every_gatewayd_citation_in_docs_and_source_resolves(self) -> None:
        """A doc or comment naming ``gatewayd.<symbol>`` still names something there.

        ``gatewayd._spawn`` is the spawn closure nested in ``_acquire_backend``; the
        other tokens the pattern meets are file suffixes and prose placeholders.
        """
        import re

        pattern = re.compile(r"gatewayd\.(_?[A-Za-z][A-Za-z0-9_]*)")
        not_symbols = {"py", "log", "stdout", "X"}
        cited: dict[str, str] = {}
        for root, suffix in ((_REPO_ROOT / "docs", "*.md"), (_SRC_ROOT / "kiro_crew", "*.py")):
            for path in root.rglob(suffix):
                if "_vendor" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                for match in pattern.finditer(text):
                    cited.setdefault(match.group(1), str(path.relative_to(_REPO_ROOT)))
        unresolved = {
            name: where
            for name, where in cited.items()
            if name not in not_symbols and name != "_spawn" and not hasattr(gw, name)
        }
        assert unresolved == {}
        acquire = next(
            node
            for node in _parse(Path(gw.__file__)).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_acquire_backend"
        )
        nested = {n.name for n in ast.walk(acquire) if isinstance(n, ast.AsyncFunctionDef)}
        assert "_spawn" in nested

    def test_the_patch_seam_detector_answers_both_ways(self) -> None:
        flagged = {
            "alpha": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch):\n    monkeypatch.setattr(gw, 'alpha', 1)\n"
            ),
            "beta": (
                "import kiro_crew.mcp_gateway.gatewayd as g\n"
                "from unittest.mock import patch\n"
                "def t():\n    with patch.object(g, 'beta'):\n        pass\n"
            ),
            "gamma": (
                "from unittest import mock\n"
                "def t():\n    with mock.patch('kiro_crew.mcp_gateway.gatewayd.gamma'):\n"
                "        pass\n"
            ),
            "delta": (
                "from kiro_crew.mcp_gateway import gatewayd\n" "def t():\n    gatewayd.delta = 1\n"
            ),
            "epsilon": (
                "import importlib\n"
                "gw = importlib.import_module('kiro_crew.mcp_gateway.gatewayd')\n"
                "alias = gw\n"
                "def t(monkeypatch):\n    monkeypatch.setattr(alias, 'epsilon', 1)\n"
            ),
            "zeta": (
                "def t(monkeypatch):\n"
                "    monkeypatch.setattr('kiro_crew.mcp_gateway.gatewayd.zeta', 1)\n"
            ),
            "eta": (
                "from unittest import mock\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t():\n    with mock.patch.multiple(gw, eta=1):\n        pass\n"
            ),
            "kappa": (
                "import pytest\n"
                "from kiro_crew.mcp_gateway import gatewayd\n"
                "@pytest.fixture\n"
                "def gwmod():\n    return gatewayd\n"
                "def t(monkeypatch, gwmod):\n    monkeypatch.setattr(gwmod, 'kappa', 1)\n"
            ),
            "lambda_": (
                "import importlib, pytest\n"
                "@pytest.fixture\n"
                "def facade():\n"
                "    return importlib.import_module('kiro_crew.mcp_gateway.gatewayd')\n"
                "def t(monkeypatch, facade):\n    monkeypatch.delattr(facade, 'lambda_')\n"
            ),
            "mu": (
                "import pytest\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "@pytest.mark.parametrize('exc,audit', [(1, 'mu'), pytest.param(2, 'mu')])\n"
                "def t(monkeypatch, exc, audit):\n    monkeypatch.setattr(gw, audit, 1)\n"
            ),
            "nu": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch):\n"
                "    for name in ('nu',):\n        monkeypatch.setattr(gw, name, 1)\n"
            ),
            "xi": (
                "from unittest.mock import patch\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t():\n    with patch.object(target=gw, attribute='xi', new=1):\n"
                "        pass\n"
            ),
            "omicron": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch):\n"
                "    monkeypatch.setattr(target=gw, name='omicron', value=1)\n"
            ),
            "pi": (
                "from unittest import mock\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t():\n    with mock.patch.dict(gw.__dict__, {'pi': 1}):\n        pass\n"
            ),
            "rho": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch):\n    monkeypatch.setitem(gw.__dict__, 'rho', 1)\n"
            ),
        }
        for name, source in flagged.items():
            assert _patched_facade_names(source) == ({name}, []), name
        unreadable = {
            "a computed name": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch):\n    monkeypatch.setattr(gw, pick(), 1)\n"
            ),
            "an unparametrized variable": (
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(monkeypatch, name):\n    monkeypatch.setattr(gw, name, 1)\n"
            ),
            "a dict built elsewhere": (
                "from unittest import mock\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(values):\n    with mock.patch.dict(gw.__dict__, values):\n        pass\n"
            ),
            "keyword splat": (
                "from unittest import mock\n"
                "from kiro_crew.mcp_gateway import gatewayd as gw\n"
                "def t(kw):\n    with mock.patch.multiple(gw, **kw):\n        pass\n"
            ),
        }
        for label, source in unreadable.items():
            found, sites = _patched_facade_names(source)
            assert found == set() and [fn for _, fn in sites] == ["t"], label
        ignored = [
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.mcp_gateway.gatewayd.os.stat'):\n        pass\n",
            "from kiro_crew.mcp_gateway import gatewayd as gw\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(gw.socketsec, 'x', 1)\n",
            "from kiro_crew.mcp_gateway import gatewayd as gw\n"
            "def t(monkeypatch, other):\n    monkeypatch.setattr(other, 'theta', 1)\n",
            "from kiro_crew.mcp_gateway import gatewayd as gw\n" "def t():\n    return gw.iota\n",
        ]
        for source in ignored:
            assert _patched_facade_names(source) == (set(), []), source

    def test_the_suites_patch_seams_include_the_known_ones(self) -> None:
        """The derived set must see the seams read-only suites patch, or the check
        below would pass on an empty set -- and no patch aimed at the facade may be
        one the detector cannot read."""
        seams, unreadable = _patched_names_in_the_suite()
        assert unreadable == ()
        assert {
            "_acquire_backend",
            "SecurityEventLog",
            "_MAX_PENDING_FRAMES",
            "_token_caller",
            "spawn_backend",
            "_respawn_backend_for_stub_unrecorded",
            "_audit_pool_rejected",
            "_audit_pool_fallback",
            "_drain_inbox_to_stub",
            "mcp_search_path",
            "_backend_tmp_sweeper",
        } <= seams

    def test_no_owner_reads_a_patched_name_as_its_own_global(self) -> None:
        """A name a test patches on the facade is read through ``daemon.facade``.

        An owner holding its own binding of it would keep running the original while
        the test believed it had replaced it.
        """
        seams, _ = _patched_names_in_the_suite()
        offenders = [
            f"daemon/{leaf}.py:{line}: {name}"
            for leaf in _owner_names()
            for line, name in _bare_reads(_parse(_DAEMON_DIR / f"{leaf}.py"), seams)
        ]
        assert offenders == []

    def test_every_name_an_owner_reads_through_the_facade_exists_there(self) -> None:
        """A misspelled ``facade.<name>`` would only fail when that line runs -- and
        inside an audit's ``except Exception`` it would never fail at all."""
        missing = [
            f"daemon/{leaf}.py:{node.lineno}: facade.{node.attr}"
            for leaf in _owner_names()
            for node in ast.walk(_parse(_DAEMON_DIR / f"{leaf}.py"))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "facade"
            and not hasattr(gw, node.attr)
        ]
        assert missing == []

    def test_no_owner_decides_the_control_plane_on_the_event_loop(self) -> None:
        """The verdict reads config and stats the filesystem, so every call runs in a
        thread -- in the facade, whose source a read-only test already scans, and in
        every owner, which that scan does not see."""
        import re

        on_loop = []
        paths = [Path(gw.__file__)] + [_DAEMON_DIR / f"{leaf}.py" for leaf in _owner_names()]
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"(?<!def )_spawns_own_control_plane\(", source):
                before = source[max(0, match.start() - 200) : match.start()].rstrip()
                if not before.endswith("await asyncio.to_thread("):
                    on_loop.append(f"{path.name}:{source.count(chr(10), 0, match.start()) + 1}")
        assert on_loop == []

    def test_the_bare_read_check_can_fail(self) -> None:
        tree = ast.parse(
            "def f(x):\n    y = 1\n    return seam(x) + y\n"
            "def g(seam):\n    return seam\n"
            "async def h():\n    return facade.seam\n"
        )
        assert _bare_reads(tree, frozenset({"seam"})) == [(3, "seam")]

    def test_the_owners_form_a_graph_that_never_imports_the_facade(self) -> None:
        package = "kiro_crew.mcp_gateway.daemon"
        edges: dict[str, set[str]] = {}
        for leaf in _owner_names():
            imports = _runtime_imports(_parse(_DAEMON_DIR / f"{leaf}.py"))
            assert _FACADE_MODULE not in imports, f"{leaf} imports the facade"
            edges[leaf] = {
                mod.rpartition(".")[2]
                for mod in imports
                if mod.startswith(package + ".") and mod.rpartition(".")[2] in _owner_names()
            } - {leaf}
        order: list[str] = []
        pending = dict(edges)
        while pending:
            ready = sorted(leaf for leaf, deps in pending.items() if deps <= set(order))
            assert ready, f"import cycle among {sorted(pending)}"
            order.extend(ready)
            for leaf in ready:
                del pending[leaf]
        init = _runtime_imports(_parse(_DAEMON_DIR / "__init__.py"))
        assert not any(mod.startswith(package + ".") for mod in init)

    def test_nothing_but_the_facade_imports_an_owner(self) -> None:
        import re

        pattern = re.compile(r"mcp_gateway\.daemon\b|from kiro_crew\.mcp_gateway import daemon\b")
        importers = sorted(
            str(path.relative_to(_SRC_ROOT))
            for path in (_SRC_ROOT / "kiro_crew").rglob("*.py")
            if _DAEMON_DIR not in path.parents
            and path != Path(gw.__file__)
            and pattern.search(path.read_text(encoding="utf-8"))
        )
        assert importers == []

    def test_the_owners_read_the_running_facade_and_cannot_write_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.mcp_gateway import daemon

        monkeypatch.setattr(gw, "_OWNER_PID", 4242)
        assert daemon.facade._OWNER_PID == 4242
        assert daemon.logger.name == gw.logger.name
        with pytest.raises(AttributeError):
            daemon.facade._OWNER_PID = 1  # type: ignore[misc]
        assert gw._OWNER_PID == 4242
