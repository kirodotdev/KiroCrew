"""Behavioural coverage for ``mcp_gateway.gatewayd``'s connection handler.

The existing gatewayd suites cover the supporting machinery (sweepers, codec,
audit emitters) and the happy-path register/claim/recaller flows over a real
socket. What stays untested is the decision tree inside
:func:`gatewayd._handle_connection`: the control-frame short-circuits, the
peer-identity degradation paths, the bridge-frame hygiene checks, every
backend-acquire rejection arm (ensure_backend pre-flight vs the legacy lazy
path -- they differ in whether the reply is tagged fallback-eligible), the
transparent-respawn arms, and the disconnect-time cancel/recycle block.

Everything is driven with in-memory doubles: the reader is a scripted frame
source, the writer records bytes, and the backend-acquire seam
(``gatewayd._acquire_backend``) is replaced, so no subprocess is spawned and
no sandbox is touched. The two ``run_gatewayd`` tests bind a real local
endpoint under ``tmp_path`` and are POSIX-only for that reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend, BackendGone, _PendingRequest
from kiro_crew.mcp_gateway.pool import BackendUnavailable, PoolAtCapacity, PoolKey

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="binds an AF_UNIX endpoint under tmp_path; Windows uses a named pipe "
    "whose name is not a filesystem path",
)

_STUB = "stub-cov-1"


# --- doubles -----------------------------------------------------------------


class _FakeWriter:
    """``asyncio.StreamWriter`` double recording every written frame."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        return None

    def frames(self) -> list[Any]:
        return [json.loads(p.decode("utf-8")) for p in self.writes]


class _ScriptedReader:
    """Hands out queued frames, then EOFs like a closed stub transport.

    A queued ``bytes`` is returned verbatim (so malformed / oversize frames can
    be exercised), a ``dict`` is serialised as one JSON line, and a queued
    exception is raised on that read.
    """

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


def _register_frame(**overrides: Any) -> dict[str, Any]:
    """A complete Register payload: every PoolKey field plus stub identity."""
    frame: dict[str, Any] = {
        "type": "register",
        "stub_uuid": _STUB,
        "poolable": True,
        "server_name": "demo-mcp",
        "agent_name": "cov-agent",
        "command_args_hash": "a" * 8,
        "effective_env_hash": "e" * 8,
        "work_dir": "/tmp/cov",
        "binary_version": "1.0",
        "os_uid": 1000,
        "sandbox_mode": "none",
        "autoapprove_set_hash": "b" * 8,
        "approval_mode": "reads",
        "trust_all_tools": False,
        "user_identity": "cov",
        "config_snapshot_hash": "c" * 8,
        "session_key": "sess-cov",
        "session_type": "dashboard",
        "ancestor_pids": [4242],
    }
    frame.update(overrides)
    return frame


def _pool_label(**overrides: Any) -> str:
    """The human-readable pool label gatewayd puts on audit records."""
    return PoolKey.from_register(_register_frame(**overrides)).human_readable()


def _fake_pool(**overrides: Any) -> MagicMock:
    pool = MagicMock()
    pool.unreserve = MagicMock()
    pool.reserve = MagicMock()
    pool.release_exclusive = AsyncMock(return_value=None)
    pool.get = AsyncMock(return_value=None)
    pool.all_backends = MagicMock(return_value=[])
    pool.metrics_snapshot_async = AsyncMock(return_value={"backends": 0})
    for name, value in overrides.items():
        setattr(pool, name, value)
    return pool


def _resolver(pool_key: PoolKey) -> tuple[str, list[str], dict[str, str], str]:
    return "demo-mcp-server", [], {}, pool_key.work_dir


async def _noop_pump() -> None:
    return None


def _fake_backend(pid: int = 4242) -> Backend:
    """A real ``Backend`` over mock pipes: alive, with an inert stdout pump."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = pid
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    backend = Backend(
        pool_key=PoolKey.from_register(_register_frame()),
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    return backend


async def _handle(
    reader: Any,
    writer: Any,
    pool: Any,
    *,
    socket_path: Path = Path("/tmp/cov-gatewayd.sock"),
    hot_keys: Any = None,
) -> None:
    await gw._handle_connection(reader, writer, pool, _resolver, socket_path, hot_keys)


@pytest.fixture(autouse=True)
def _isolate_module_globals():
    """The conn index and probe registry are process-global; keep tests clean."""
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()


@pytest.fixture
def peer_ok(monkeypatch):
    """Peer principal positively confirmed, with no SO_PEERCRED pid available."""
    monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        gw.socketsec, "check_peer_is_self", lambda w: gw.socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda w: None)


# --- peer gate ---------------------------------------------------------------


class TestPeerGate:
    @pytest.mark.asyncio
    async def test_mismatched_peer_is_refused_before_the_first_frame_is_read(
        self, monkeypatch
    ):
        """On a platform with no identity mechanism a positively-parsed foreign
        principal is still refused -- and refused before any frame is read."""
        monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", False)
        monkeypatch.setattr(
            gw.socketsec,
            "check_peer_is_self",
            lambda w: gw.socketsec.PeerCredResult.MISMATCH,
        )
        owner_only = MagicMock(return_value=True)
        monkeypatch.setattr(gw.socketsec, "socket_owner_only", owner_only)
        denials: list[str] = []
        monkeypatch.setattr(gw, "_audit_peer_denied", denials.append)

        reader = _ScriptedReader(_register_frame())
        writer = _FakeWriter()
        await _handle(reader, writer, _fake_pool())

        assert writer.writes == []
        assert reader.remaining == 1, "the register frame must never be read"
        assert denials and "mismatch" in denials[0]
        owner_only.assert_not_called()

    @pytest.mark.asyncio
    async def test_supported_platform_refuses_an_unverifiable_peer(self, monkeypatch):
        monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", True)
        monkeypatch.setattr(
            gw.socketsec,
            "check_peer_is_self",
            lambda w: gw.socketsec.PeerCredResult.UNVERIFIABLE,
        )
        denials: list[str] = []
        monkeypatch.setattr(gw, "_audit_peer_denied", denials.append)

        writer = _FakeWriter()
        await _handle(_ScriptedReader(_register_frame()), writer, _fake_pool())

        assert writer.writes == []
        assert denials and "unverifiable" in denials[0]


# --- first-frame short circuits ---------------------------------------------


class TestControlFrames:
    @pytest.mark.asyncio
    async def test_eof_before_the_first_frame_closes_without_a_reply(self, peer_ok):
        writer = _FakeWriter()
        await _handle(_ScriptedReader(), writer, _fake_pool())
        assert writer.writes == []

    @pytest.mark.asyncio
    async def test_ping_is_answered_with_pong_and_closes(self, peer_ok):
        writer = _FakeWriter()
        await _handle(_ScriptedReader({"type": "ping"}, {"type": "ping"}), writer, _fake_pool())
        assert writer.frames() == [{"type": "pong"}]

    @pytest.mark.asyncio
    async def test_stats_merges_the_warm_pool_hit_tally(self, peer_ok):
        hot_keys = MagicMock()
        hot_keys.hit_stats = MagicMock(return_value={"warm_hits": 7, "warm_misses": 2})
        pool = _fake_pool(
            metrics_snapshot_async=AsyncMock(return_value={"backends": 3, "sessions": 5})
        )
        writer = _FakeWriter()

        await _handle(_ScriptedReader({"type": "stats"}), writer, pool, hot_keys=hot_keys)

        assert writer.frames() == [
            {"type": "stats", "backends": 3, "sessions": 5, "warm_hits": 7, "warm_misses": 2}
        ]

    @pytest.mark.asyncio
    async def test_stats_omits_warm_keys_when_prewarming_is_disabled(self, peer_ok):
        writer = _FakeWriter()
        await _handle(_ScriptedReader({"type": "stats"}), writer, _fake_pool())
        assert writer.frames() == [{"type": "stats", "backends": 0}]

    @pytest.mark.asyncio
    async def test_abort_frame_cancels_in_flight_work_for_the_named_runtime(self, peer_ok):
        conn = gw._StubConn("stub-abort", [7777], "demo-mcp", None)
        gw._conn_index_add(conn)
        backend = MagicMock()
        backend.cancel_in_flight_for_stub = AsyncMock(return_value=["f1", "f2"])
        pool = _fake_pool(all_backends=MagicMock(return_value=[backend]))
        writer = _FakeWriter()

        await _handle(
            _ScriptedReader({"type": "abort", "pids": [7777], "reason": "hard stop"}),
            writer,
            pool,
        )

        assert writer.frames() == [{"type": "aborted", "cancelled": 2, "stubs": 1}]
        backend.cancel_in_flight_for_stub.assert_awaited_once_with("stub-abort")

    @pytest.mark.asyncio
    async def test_app_call_is_refused_and_audited_when_the_feature_is_off(
        self, peer_ok, monkeypatch
    ):
        from kiro_crew.mcp_gateway import app_call as app_call_mod
        from kiro_crew.mcp_gateway import backend as backend_mod

        monkeypatch.setattr(backend_mod, "_mcp_apps_enabled", lambda: False)
        audits: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        monkeypatch.setattr(
            app_call_mod, "_audit", lambda *a, **k: audits.append((a, k))
        )
        forwarded = AsyncMock()
        monkeypatch.setattr(app_call_mod, "handle_app_call", forwarded)
        writer = _FakeWriter()

        await _handle(
            _ScriptedReader({"type": "app-call", "spool_id": "sp-1", "tool": "do_thing"}),
            writer,
            _fake_pool(),
        )

        assert writer.frames() == [
            {"type": "app-call-rejected", "reason": "mcp-apps feature disabled"}
        ]
        forwarded.assert_not_awaited()
        assert audits == [
            (
                ("denied", "mcp-apps feature disabled"),
                {"spool_id": "sp-1", "tool": "do_thing"},
            )
        ]

    @pytest.mark.asyncio
    async def test_app_call_is_forwarded_when_the_feature_is_on(self, peer_ok, monkeypatch):
        from kiro_crew.mcp_gateway import app_call as app_call_mod
        from kiro_crew.mcp_gateway import backend as backend_mod

        monkeypatch.setattr(backend_mod, "_mcp_apps_enabled", lambda: True)
        handled = AsyncMock(return_value={"type": "app-call-result", "ok": True})
        monkeypatch.setattr(app_call_mod, "handle_app_call", handled)
        pool = _fake_pool()
        frame = {"type": "app-call", "spool_id": "sp-2", "tool": "render"}
        writer = _FakeWriter()

        await _handle(_ScriptedReader(frame), writer, pool)

        assert writer.frames() == [{"type": "app-call-result", "ok": True}]
        assert handled.await_args.args[0] is pool
        assert handled.await_args.args[1]["spool_id"] == "sp-2"

    @pytest.mark.asyncio
    async def test_unknown_first_frame_type_is_dropped_silently(self, peer_ok):
        writer = _FakeWriter()
        await _handle(_ScriptedReader({"type": "not-a-thing"}), writer, _fake_pool())
        assert writer.writes == []

    @pytest.mark.asyncio
    async def test_malformed_register_is_rejected_with_the_missing_field_list(self, peer_ok):
        writer = _FakeWriter()
        await _handle(
            _ScriptedReader({"type": "register", "stub_uuid": _STUB}), writer, _fake_pool()
        )

        frames = writer.frames()
        assert len(frames) == 1
        assert frames[0]["type"] == "rejected"
        assert frames[0]["reason"].startswith("malformed Register:")
        assert "server_name" in frames[0]["reason"]

    @pytest.mark.asyncio
    async def test_register_without_a_stub_uuid_is_rejected(self, peer_ok):
        writer = _FakeWriter()
        await _handle(_ScriptedReader(_register_frame(stub_uuid="")), writer, _fake_pool())
        assert writer.frames() == [{"type": "rejected", "reason": "missing stub_uuid"}]


# --- registration bookkeeping ------------------------------------------------


class TestRegisterBookkeeping:
    @pytest.mark.asyncio
    async def test_registered_reply_advertises_the_negotiated_capabilities(self, peer_ok):
        writer = _FakeWriter()
        register = _register_frame()
        await _handle(_ScriptedReader(register), writer, _fake_pool())

        frames = writer.frames()
        assert len(frames) == 1
        expected_prefix = "pending-" + PoolKey.from_register(register).stable_hash()[:12]
        assert frames[0]["type"] == "registered"
        assert frames[0]["backend_id"] == expected_prefix
        assert frames[0]["capabilities"] == list(gw.REGISTERED_CAPABILITIES)

    @pytest.mark.asyncio
    async def test_peer_identity_resolution_failure_leaves_the_stub_unidentified(
        self, monkeypatch
    ):
        monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", True)
        monkeypatch.setattr(
            gw.socketsec, "check_peer_is_self", lambda w: gw.socketsec.PeerCredResult.MATCH
        )
        monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda w: 4321)

        def _boom(peer_pid: int) -> tuple[str, list[int]]:
            raise RuntimeError("/proc read failed")

        monkeypatch.setattr(gw, "_resolve_peer_identity", _boom)
        allowed: list[tuple[str, str]] = []
        monkeypatch.setattr(
            gw, "_audit_peer_allowed", lambda caller, label: allowed.append((caller, label))
        )

        register = _register_frame()
        register.pop("session_key")
        writer = _FakeWriter()
        await _handle(_ScriptedReader(register), writer, _fake_pool())

        assert writer.frames()[0]["type"] == "registered"
        assert allowed == [("", _pool_label())]

    @pytest.mark.asyncio
    async def test_pid_start_id_snapshot_failure_registers_with_unknown_tokens(
        self, peer_ok, monkeypatch
    ):
        """A failed start-token snapshot must degrade to 'unknown', not deny:
        an empty map makes every later claim count as a match."""

        def _boom(pid: int) -> Optional[str]:
            raise OSError("procfs unreadable")

        monkeypatch.setattr(gw, "_get_process_start_id", _boom)
        seen: list[gw._StubConn] = []
        real_add = gw._conn_index_add

        def _spy(conn: gw._StubConn) -> None:
            seen.append(conn)
            real_add(conn)

        monkeypatch.setattr(gw, "_conn_index_add", _spy)

        writer = _FakeWriter()
        await _handle(_ScriptedReader(_register_frame()), writer, _fake_pool())

        assert len(seen) == 1
        assert seen[0].ancestor_pids == [4242]
        assert seen[0].pid_start_ids == {}

    @pytest.mark.asyncio
    async def test_hot_key_observation_records_the_pool_hit_outcome(self, peer_ok):
        hot_keys = MagicMock()
        pool = _fake_pool(get=AsyncMock(return_value=MagicMock()))
        register = _register_frame()

        await _handle(_ScriptedReader(register), _FakeWriter(), pool, hot_keys=hot_keys)

        hot_keys.record.assert_called_once_with(register)
        hot_keys.record_outcome.assert_called_once_with(hit=True)

    @pytest.mark.asyncio
    async def test_hot_key_observation_records_a_miss_on_a_cold_key(self, peer_ok):
        hot_keys = MagicMock()
        await _handle(
            _ScriptedReader(_register_frame()), _FakeWriter(), _fake_pool(), hot_keys=hot_keys
        )
        hot_keys.record_outcome.assert_called_once_with(hit=False)


# --- bridge-phase frame hygiene ---------------------------------------------


class TestBridgeFrameHygiene:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_frame,is_fatal",
        [
            (asyncio.LimitOverrunError("no separator found", 64), True),
            (b"", True),
            (b"x" * (gw._MAX_FRAME_BYTES + 2), True),
            (b"not json at all\n", False),
            (b"[1, 2, 3]\n", False),
        ],
        ids=["limit-overrun", "empty-line", "oversize", "non-json", "non-object"],
    )
    async def test_bad_bridge_frames_never_reach_the_backend(
        self, peer_ok, monkeypatch, bad_frame, is_fatal
    ):
        """A malformed frame is dropped and the session continues; a frame that
        breaks the framing itself (overrun, empty read, oversize) drops the
        connection. Neither may ever acquire a backend."""
        acquire = AsyncMock()
        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        # The trailing ping is the probe: it is answered only if the connection
        # survived the bad frame.
        reader = _ScriptedReader(_register_frame(), bad_frame, {"type": "ping"})
        writer = _FakeWriter()

        await _handle(reader, writer, _fake_pool())

        acquire.assert_not_awaited()
        if is_fatal:
            assert [f["type"] for f in writer.frames()] == ["registered"]
            assert reader.remaining == 1, "the connection must end at the bad frame"
        else:
            assert [f["type"] for f in writer.frames()] == ["registered", "pong"]
            assert reader.remaining == 0

    @pytest.mark.asyncio
    async def test_bridge_ping_is_answered_without_acquiring_a_backend(
        self, peer_ok, monkeypatch
    ):
        acquire = AsyncMock()
        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        writer = _FakeWriter()

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ping"}), writer, _fake_pool()
        )

        assert [f["type"] for f in writer.frames()] == ["registered", "pong"]
        acquire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unregister_closes_the_connection(self, peer_ok, monkeypatch):
        acquire = AsyncMock()
        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        reader = _ScriptedReader(
            _register_frame(), {"type": "unregister"}, {"jsonrpc": "2.0", "id": 1}
        )
        writer = _FakeWriter()

        await _handle(reader, writer, _fake_pool())

        assert [f["type"] for f in writer.frames()] == ["registered"]
        assert reader.remaining == 1, "frames after unregister must not be read"
        acquire.assert_not_awaited()


# --- backend acquire rejection arms -----------------------------------------


class TestEnsureBackendRejections:
    """The pre-flight arm: a rejection the stub can still recover from is
    tagged ``fallback: True`` so it runs an unpooled per-session exec."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc,expected_reason,fallback,audit",
        [
            (
                gw._TargetUnknown("no target mapping for server 'demo-mcp'"),
                "no target mapping for server 'demo-mcp'",
                False,
                "_audit_pool_rejected",
            ),
            (
                BackendUnavailable("circuit breaker OPEN"),
                "circuit breaker OPEN",
                True,
                "_audit_pool_fallback",
            ),
            (
                PoolAtCapacity("pool full"),
                "pool full",
                True,
                "_audit_pool_fallback",
            ),
            (
                OSError("ENOMEM"),
                "backend spawn failed: ENOMEM",
                True,
                "_audit_pool_fallback",
            ),
            (
                RuntimeError("gateway bug"),
                "internal error: gateway bug",
                False,
                "_audit_pool_rejected",
            ),
        ],
        ids=["target-unknown", "breaker-open", "at-capacity", "spawn-oserror", "internal"],
    )
    async def test_each_failure_mode_produces_its_own_rejection_shape(
        self, peer_ok, monkeypatch, exc, expected_reason, fallback, audit
    ):
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=exc))
        audited: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            gw, audit, lambda caller, label, reason: audited.append((caller, label, reason))
        )
        reader = _ScriptedReader(
            _register_frame(), {"type": "ensure_backend"}, {"type": "ping"}
        )
        writer = _FakeWriter()

        await _handle(reader, writer, _fake_pool())

        frames = writer.frames()
        assert [f["type"] for f in frames] == ["registered", "rejected"]
        assert frames[1]["reason"] == expected_reason
        assert frames[1].get("fallback", False) is fallback
        assert reader.remaining == 1, "the connection must end at the rejection"
        assert audited and audited[0][0] == "sess-cov"
        assert audited[0][1] == _pool_label()

    @pytest.mark.asyncio
    async def test_ready_is_sent_after_the_stub_is_attached(self, peer_ok, monkeypatch):
        backend = _fake_backend()
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        pool = _fake_pool()
        writer = _FakeWriter()

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}), writer, pool
        )

        assert [f["type"] for f in writer.frames()] == ["registered", "ready"]
        # Reservation released once the attach made refcount>0 protect the key,
        # and the stub detached again when the connection ended.
        pool.unreserve.assert_called_once()
        assert backend.refcount == 0

    @pytest.mark.asyncio
    async def test_a_private_backend_never_releases_a_pooled_reservation(
        self, peer_ok, monkeypatch
    ):
        """``poolable`` absent => connection-private backend, which took no
        reservation; releasing one would decrement a co-digest pooled stub."""
        backend = _fake_backend()
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        register = _register_frame()
        register.pop("poolable")
        pool = _fake_pool()

        await _handle(
            _ScriptedReader(register, {"type": "ensure_backend"}), _FakeWriter(), pool
        )

        pool.unreserve.assert_not_called()
        pool.release_exclusive.assert_awaited_once_with(_STUB)


class TestLazySpawnRejections:
    """The legacy arm: the stub already forwarded a real frame, so a fallback
    exec would lose it -- no rejection here may be tagged fallback-eligible."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc,expected_reason,audit_reason",
        [
            (gw._TargetUnknown("no target mapping"), "no target mapping", "no target mapping"),
            (PoolAtCapacity("pool full"), "pool full", "pool full"),
            (
                BackendUnavailable("circuit breaker OPEN"),
                "circuit breaker OPEN",
                "circuit breaker OPEN",
            ),
            (RuntimeError("boom"), "backend spawn failed: boom", "spawn failed: boom"),
        ],
        ids=["target-unknown", "at-capacity", "breaker-open", "internal"],
    )
    async def test_rejections_are_terminal_and_never_fallback_eligible(
        self, peer_ok, monkeypatch, exc, expected_reason, audit_reason
    ):
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=exc))
        rejected: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            gw,
            "_audit_pool_rejected",
            lambda caller, label, reason: rejected.append((caller, label, reason)),
        )
        writer = _FakeWriter()

        await _handle(
            _ScriptedReader(
                _register_frame(), {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
            ),
            writer,
            _fake_pool(),
        )

        frames = writer.frames()
        assert [f["type"] for f in frames] == ["registered", "rejected"]
        assert frames[1]["reason"] == expected_reason
        assert "fallback" not in frames[1]
        assert rejected and rejected[0][2] == audit_reason


# --- transparent respawn -----------------------------------------------------


class TestBackendGoneHandling:
    @pytest.mark.asyncio
    async def test_unrecoverable_backend_death_ends_with_a_jsonrpc_error(
        self, peer_ok, monkeypatch
    ):
        backend = _fake_backend()
        backend.forward_from_stub = AsyncMock(  # type: ignore[method-assign]
            side_effect=BackendGone("stdin closed")
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        monkeypatch.setattr(gw, "_respawn_backend_for_stub", AsyncMock(return_value=None))
        reader = _ScriptedReader(
            _register_frame(),
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            {"type": "ping"},
        )
        writer = _FakeWriter()

        await _handle(reader, writer, _fake_pool())

        frames = writer.frames()
        assert frames[-1] == {
            "jsonrpc": "2.0",
            "id": 9,
            "error": {"code": -32000, "message": "backend gone: stdin closed"},
        }
        assert reader.remaining == 1, "a terminal error must close the connection"

    @pytest.mark.asyncio
    async def test_successful_respawn_replays_the_captured_initialize_and_retries(
        self, peer_ok, monkeypatch
    ):
        backend = _fake_backend()
        backend.forward_from_stub = AsyncMock(  # type: ignore[method-assign]
            side_effect=[None, BackendGone("pipe died")]
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))

        fresh = _fake_backend(pid=5151)
        inbox: "asyncio.Queue[bytes]" = asyncio.Queue()
        replacement = asyncio.create_task(asyncio.Event().wait(), name="cov-drain")
        respawn_args: list[tuple[Any, ...]] = []

        async def _respawn(*args: Any):
            # Mirror the real helper's contract: it stops the old inbox drain
            # before handing back a fresh backend, so the two writer tasks
            # never race onto the same socket.
            respawn_args.append(args)
            old_writer_task = args[8]
            if old_writer_task is not None:
                old_writer_task.cancel()
            return fresh, inbox, replacement

        monkeypatch.setattr(gw, "_respawn_backend_for_stub", _respawn)

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        writer = _FakeWriter()
        await _handle(
            _ScriptedReader(
                _register_frame(),
                init,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call"},
            ),
            writer,
            _fake_pool(),
        )

        # The respawn helper is handed the initialize frame captured earlier on
        # this connection, so the fresh backend can be re-primed.
        assert respawn_args and respawn_args[0][5] == init
        assert writer.frames()[-1] == {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32000,
                "message": "backend restarted mid-call, retry: pipe died",
            },
        }
        assert replacement.done(), "the replacement writer task is cancelled on exit"


# --- disconnect-time cancel / recycle ---------------------------------------


class TestDisconnectTeardown:
    def _seed_in_flight(self, backend: Backend) -> None:
        backend._pending_requests["f1"] = _PendingRequest(
            stub_uuid=_STUB, original_id=7, method="tools/call"
        )

    @pytest.mark.asyncio
    async def test_in_flight_work_is_cancelled_then_the_drained_backend_recycles(
        self, peer_ok, monkeypatch
    ):
        backend = _fake_backend()
        self._seed_in_flight(backend)
        recycle = AsyncMock(return_value=True)
        backend.recycle_if_idle = recycle  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            _FakeWriter(),
            _fake_pool(),
        )

        sent = [call.args[0] for call in backend.stdin.write.call_args_list]
        assert any(b"notifications/cancelled" in payload for payload in sent)
        assert backend.refcount == 0
        recycle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_cancel_still_detaches_the_stub(self, peer_ok, monkeypatch):
        backend = _fake_backend()
        self._seed_in_flight(backend)
        backend.cancel_in_flight_for_stub = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("stdin gone")
        )
        recycle = AsyncMock(return_value=True)
        backend.recycle_if_idle = recycle  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            _FakeWriter(),
            _fake_pool(),
        )

        assert backend.refcount == 0
        assert _STUB not in backend._stub_inboxes
        recycle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_quarantined_backend_recycles_once_it_drains(self, peer_ok, monkeypatch):
        backend = _fake_backend()
        backend.quarantined = True
        recycle = AsyncMock(return_value=True)
        backend.recycle_if_idle = recycle  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            _FakeWriter(),
            _fake_pool(),
        )

        recycle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_healthy_idle_backend_is_left_for_the_sweeper(self, peer_ok, monkeypatch):
        backend = _fake_backend()
        recycle = AsyncMock(return_value=True)
        backend.recycle_if_idle = recycle  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))

        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            _FakeWriter(),
            _fake_pool(),
        )

        recycle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_private_backend_release_never_masks_teardown(
        self, peer_ok, monkeypatch
    ):
        backend = _fake_backend()
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(backend, True)))
        pool = _fake_pool(
            release_exclusive=AsyncMock(side_effect=RuntimeError("pool wedged"))
        )

        # The handler must return normally: a teardown-time failure here would
        # otherwise skip the writer-task cancel and mask the real cause.
        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            _FakeWriter(),
            pool,
        )

        pool.release_exclusive.assert_awaited_once_with(_STUB)


# --- daemon lifecycle --------------------------------------------------------


@_POSIX_ONLY
class TestRunGatewaydLifecycle:
    @pytest.mark.asyncio
    async def test_prewarm_count_is_clamped_and_persisted_hot_keys_are_warmed(
        self, tmp_path, monkeypatch
    ):
        """``prewarm_count >= max_backends`` would pin the whole pool, so it is
        clamped to leave one reclaimable slot -- and only the clamped number of
        persisted hot keys is warmed."""
        socket_path = tmp_path / "gw.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        hot_keys_path = tmp_path / "hot-keys.json"
        now = time.time()
        hot_keys_path.write_text(
            json.dumps(
                {
                    "keys": [
                        {
                            "register": _register_frame(server_name="hot-one"),
                            "hits": 9,
                            "last_seen": now,
                        },
                        {
                            "register": _register_frame(server_name="hot-two"),
                            "hits": 4,
                            "last_seen": now,
                        },
                    ],
                    "totals": {"hits": 3, "misses": 1},
                }
            ),
            encoding="utf-8",
        )
        credential_path = tmp_path / "creds.json"
        credential_path.write_text("{}", encoding="utf-8")

        warmed: list[str] = []
        first_warm = asyncio.Event()

        async def _fake_acquire(pool, pool_key, resolver, **kwargs):
            warmed.append(pool_key.server_name)
            first_warm.set()
            return _fake_backend(), True

        monkeypatch.setattr(gw, "_acquire_backend", _fake_acquire)
        audits: list[str] = []
        monkeypatch.setattr(gw, "_audit_prewarm_spawn", audits.append)

        stop_event = asyncio.Event()
        daemon = asyncio.create_task(
            gw.run_gatewayd(
                socket_path,
                max_backends=2,
                idle_timeout_secs=300,
                stop_event=stop_event,
                target_resolver=_resolver,
                prewarm_count=5,
                credential_watch_paths=[credential_path],
            )
        )
        try:
            await asyncio.wait_for(first_warm.wait(), timeout=10)
            assert socket_path.exists(), "the endpoint must be bound before warming"
        finally:
            stop_event.set()
            await asyncio.wait_for(daemon, timeout=15)

        # max_backends=2 clamps prewarm_count 5 -> 1, so only the hottest key
        # is warmed and audited.
        assert warmed == ["hot-one"]
        assert audits == [_pool_label(server_name="hot-one")]
        # Clean shutdown unbinds the endpoint and flushes the hot-key tally.
        assert not socket_path.exists()
        assert hot_keys_path.exists()

    @pytest.mark.asyncio
    async def test_a_crashing_connection_handler_is_logged_and_the_daemon_keeps_serving(
        self, tmp_path, monkeypatch, caplog
    ):
        socket_path = tmp_path / "gw-crash.sock"
        handled = asyncio.Event()

        async def _explode(*args, **kwargs):
            handled.set()
            raise RuntimeError("handler blew up")

        monkeypatch.setattr(gw, "_handle_connection", _explode)

        stop_event = asyncio.Event()
        daemon = asyncio.create_task(
            gw.run_gatewayd(
                socket_path,
                max_backends=2,
                idle_timeout_secs=300,
                stop_event=stop_event,
                target_resolver=_resolver,
            )
        )
        try:
            for _ in range(500):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.02)
            assert socket_path.exists(), "the daemon never bound its endpoint"
            with caplog.at_level(logging.ERROR, logger=gw.logger.name):
                _, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.close()
                await asyncio.wait_for(handled.wait(), timeout=10)
                # The accept loop survives a crashing handler: a second
                # connection is still accepted.
                _, writer2 = await asyncio.open_unix_connection(str(socket_path))
                writer2.close()
                await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(daemon, timeout=15)

        assert any("connection handler crashed" in rec.message for rec in caplog.records)
