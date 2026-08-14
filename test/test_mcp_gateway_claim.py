"""In-process functional tests for claim-push (gateway → gatewayd ``claim``).

Claim-push is the event-driven replacement for the stub-side recaller poll:
on warm-pool ``rekey()`` the gateway sends a one-shot ``claim`` frame naming
the runtime PID and the claiming session; gatewayd re-targets the caller of
every live stub connection indexed under that PID. These tests drive the real
``gatewayd._handle_connection`` loop (same harness as the recaller tests) and
verify:

* a claim re-targets a key-less connection mid-stream — the very next
  forwarded call carries the claimed identity (per-frame pickup),
* a claim REPLACES an existing identity (re-claim correctness; unlike the
  deny-by-default stub ``recaller``) with an ``allowed`` audit,
* malformed claims (bad pid / empty key) update nothing and audit ``denied``,
* the claim first-frame connection is acked with ``{"type": "claimed"}``,
* the PID index is populated at register and cleaned up at teardown,
* the stub Register payload carries the ``ancestor_pids`` chain, and
* the ``claim`` sender module (``mcp_gateway.claim``) round-trips against a
  real unix socket and no-ops safely without its preconditions.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import claim as claim_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway import transport

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_PID = 424242
#: Simulated runtime ancestry, nearest first: kiro-cli-chat → kiro-cli →
#: sandbox wrapper. A claim naming ANY of these must hit — the live bug was
#: the gateway claiming with the sandbox-wrapper PID (top of the tree) while
#: the index only held the stub's immediate parent.
_ANCESTORS = [_PID, 424241, 424240]
_WRAPPER_PID = _ANCESTORS[-1]


def _register(
    session_key: str, ancestor_pids: list[int] | None = None
) -> dict[str, Any]:
    return {
        "type": "register",
        "stub_uuid": "cp-stub-0001",
        "server_name": "echo-mcp",
        "agent_name": "cp-agent",
        "command_args_hash": "0" * 64,
        "effective_env_hash": "1" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "2" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "channel_id": "C_CP",
        "config_snapshot_hash": "3" * 64,
        "parent_pid": (ancestor_pids or _ANCESTORS)[0],  # legacy field, first ancestor
        "ancestor_pids": ancestor_pids if ancestor_pids is not None else _ANCESTORS,
        "session_key": session_key,
        "session_type": "unknown" if not session_key else "dashboard",
        "principal_id": "",
    }


def _claim(pid: Any, session_key: str) -> dict[str, Any]:
    return {
        "type": "claim",
        "pid": pid,
        "caller": {
            "session_key": session_key,
            "session_type": "dashboard",
            "principal_id": "cp",
            "channel_id": "C_CP",
        },
    }


_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}


class _QueueReader:
    """Reader fed dynamically so a test can interleave a claim between frames."""

    def __init__(self) -> None:
        self._q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

    def feed(self, frame: dict[str, Any]) -> None:
        self._q.put_nowait((json.dumps(frame) + "\n").encode())

    def eof(self) -> None:
        self._q.put_nowait(None)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        item = await self._q.get()
        if item is None:
            raise asyncio.IncompleteReadError(b"", None)
        return item


class _RecordingWriter:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, b: bytes) -> None:
        for line in b.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, _name: str, default: Any = None) -> Any:
        return default


class _FakeBackend:
    supports_caller_identity = True
    quarantined = False

    def __init__(self) -> None:
        self.callers: list[Any] = []
        self.forwarded = asyncio.Event()
        self._pending_requests: dict = {}

    async def attach_stub(self, _uuid: str) -> "asyncio.Queue[bytes]":
        return asyncio.Queue()

    async def detach_stub(self, _uuid: str) -> int:
        return 0

    async def cancel_in_flight_for_stub(self, _stub_uuid: str) -> list[str]:
        return []

    async def recycle_if_idle(self) -> bool:
        return False

    async def forward_from_stub(self, _uuid: str, _msg: dict, caller: Any = None) -> None:
        self.callers.append(caller)
        self.forwarded.set()


class _FakePool:
    """Minimal pool double: the fork's lazy-spawn attach releases its hand-out
    reservation via ``pool.unreserve`` in a ``finally`` (absent upstream at this
    commit), so a bare ``object()`` would raise AttributeError. Nothing else on
    the pool is reached (hot_keys=None skips ``pool.get``)."""

    def unreserve(self, _key: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_index() -> Any:
    gw._CONN_INDEX.clear()
    yield
    gw._CONN_INDEX.clear()


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeBackend, list[dict[str, Any]]]:
    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH
    )
    fake_backend = _FakeBackend()
    sel_calls: list[dict[str, Any]] = []

    class _FakeSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            sel_calls.append(kwargs)

    async def _fake_acquire(_pool: Any, _key: Any, _resolver: Any, **_kw: Any):
        return fake_backend, True

    async def _fake_drain(_inbox: Any, _writer: Any, _stub_uuid: str = "") -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
    monkeypatch.setattr(gw, "_acquire_backend", _fake_acquire)
    monkeypatch.setattr(gw, "_drain_inbox_to_stub", _fake_drain)
    return fake_backend, sel_calls


def _claim_events(sel_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in sel_calls if e.get("operation") == "mcp-gateway.caller-claim"]


async def _handle(reader: Any, writer: Any) -> None:
    await asyncio.wait_for(
        gw._handle_connection(
            reader, writer, pool=_FakePool(), resolver=object(),
            socket_path=Path("/tmp/cp.sock"), hot_keys=None,
        ),
        timeout=5.0,
    )


@pytest.mark.asyncio
async def test_claim_retargets_live_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key-less register forwards caller=None; after a claim for its runtime
    tree the very next forwarded call carries the claimed identity. The claim
    deliberately names the TOP ancestor (sandbox-wrapper PID) while the stub's
    immediate parent is a different PID — the exact live topology
    (sandbox wrapper → kiro-cli → kiro-cli-chat → stub) where a single-level
    index made every claim miss."""
    fb, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register(""))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    assert fb.callers == [None]

    ack = gw._apply_claim(_claim(_WRAPPER_PID, "dashboard:chat-CP-1"))
    assert ack["type"] == "claimed" and ack["updated"] == 1

    fb.forwarded.clear()
    reader.feed(_CALL)
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    reader.feed({"type": "unregister"})
    await task

    assert len(fb.callers) == 2
    assert fb.callers[1] is not None
    assert fb.callers[1].session_key == "dashboard:chat-CP-1"
    events = _claim_events(sel)
    assert len(events) == 1 and events[0]["outcome"] == "allowed"
    assert events[0]["caller"] == "dashboard:chat-CP-1"


@pytest.mark.asyncio
async def test_claim_matches_host_pid_for_pidns_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub inside a PID-namespace sandbox self-reports NAMESPACE-local
    ancestor pids while the gateway's claim frame carries the runtime's HOST
    pid — without host-chain indexing the claim silently updates zero
    connections and the stub stays identity-less for life (orphan subagents,
    lost completion events — Mesh ticket 8abcd9fe). gatewayd must index the
    connection under the HOST ancestor chain resolved from the SO_PEERCRED
    peer pid so the claim still matches."""
    fb, sel = _patch_env(monkeypatch)
    ns_pids = [43, 2]  # namespace-local: can never equal a host pid
    host_chain = [9100, 9050, 9020]  # stub → kiro-cli → sandbox launcher (host)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: host_chain[0])
    monkeypatch.setattr(
        gw, "_resolve_peer_identity", lambda _pid: ("", list(host_chain))
    )

    reader = _QueueReader()
    reader.feed(_register("", ancestor_pids=ns_pids))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    assert fb.callers == [None]
    # The connection must be claim-indexed under BOTH pid sets.
    for pid in host_chain + ns_pids:
        assert pid in gw._CONN_INDEX, f"pid {pid} missing from claim index"

    # The gateway claims with the HOST launcher pid (top of the host chain).
    ack = gw._apply_claim(_claim(host_chain[-1], "dashboard:chat-NS-1"))
    assert ack["type"] == "claimed" and ack["updated"] == 1

    fb.forwarded.clear()
    reader.feed(_CALL)
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    reader.feed({"type": "unregister"})
    await task

    assert len(fb.callers) == 2
    assert fb.callers[1] is not None
    assert fb.callers[1].session_key == "dashboard:chat-NS-1"
    # Teardown removes every indexed pid (host + namespace alike).
    for pid in host_chain + ns_pids:
        assert pid not in gw._CONN_INDEX


@pytest.mark.asyncio
async def test_register_resolves_identity_from_peer_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session_pid file already exists at register time (claim
    happened first), the register path adopts the peer-resolved identity and
    the very first forwarded call carries it."""
    fb, sel = _patch_env(monkeypatch)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: 9100)
    monkeypatch.setattr(
        gw, "_resolve_peer_identity",
        lambda _pid: ("dashboard:chat-PRE-1", [9100, 9020]),
    )

    reader = _QueueReader()
    reader.feed(_register("", ancestor_pids=[43, 2]))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    reader.feed({"type": "unregister"})
    await task

    assert fb.callers and fb.callers[0] is not None
    assert fb.callers[0].session_key == "dashboard:chat-PRE-1"
    assert fb.callers[0].session_type == "peer-resolved"


@pytest.mark.asyncio
async def test_no_host_indexing_when_peer_pid_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deny-by-default: without a kernel-attested peer pid (SO_PEERCRED
    structurally unavailable — the connection proceeds on the owner-only
    filesystem gate), the host ancestry is never resolved nor indexed — a
    claim naming a host pid updates nothing. Non-MATCH uids never reach this
    code: they are rejected at connection level before Register."""
    fb, sel = _patch_env(monkeypatch)
    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", False)
    monkeypatch.setattr(socketsec, "socket_owner_only", lambda _p: True)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: None)
    resolve_calls: list[int] = []

    def _spy_resolve(pid: int) -> tuple[str, list[int]]:
        resolve_calls.append(pid)
        return "", [9100, 9020]

    monkeypatch.setattr(gw, "_resolve_peer_identity", _spy_resolve)

    reader = _QueueReader()
    reader.feed(_register("", ancestor_pids=[43, 2]))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)

    assert resolve_calls == []  # never resolved without a kernel-attested pid
    assert 9100 not in gw._CONN_INDEX and 9020 not in gw._CONN_INDEX
    ack = gw._apply_claim(_claim(9020, "dashboard:chat-NS-2"))
    assert ack["updated"] == 0

    reader.feed({"type": "unregister"})
    await task


def test_resolve_peer_identity_returns_key_and_full_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One walk yields the session key AND the complete host ancestor chain
    (walk continues past the key match so claims at any level hit)."""
    session_key = "dashboard:chat-7-xyz"
    (tmp_path / "session_pid_50.txt").write_text(session_key, encoding="utf-8")

    def mock_parent_pid(pid: int) -> int:
        # stub(100) → kiro-cli(50, has file) → launcher(20) → init
        return {100: 50, 50: 20, 20: 1}.get(pid, 0)

    monkeypatch.setattr(gw, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(gw, "_ppid_fn", mock_parent_pid)
    key, chain = gw._resolve_peer_identity(100)
    assert key == session_key
    assert chain == [100, 50, 20]


def test_resolve_peer_identity_no_file_still_returns_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register-before-claim ordering: no session_pid file exists yet, but
    the host chain must still come back so the connection gets claim-indexed."""
    def mock_parent_pid(pid: int) -> int:
        return {300: 250, 250: 240, 240: 1}.get(pid, 0)

    monkeypatch.setattr(gw, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(gw, "_ppid_fn", mock_parent_pid)
    key, chain = gw._resolve_peer_identity(300)
    assert key == ""
    assert chain == [300, 250, 240]


def test_resolve_peer_identity_config_dir_error_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_dir() raising degrades to ("", []) — no partial state."""
    def _boom() -> Path:
        raise RuntimeError("boom")

    monkeypatch.setattr(gw, "_config_dir", _boom)
    assert gw._resolve_peer_identity(999) == ("", [])


def test_claim_zero_connections_warns_and_audits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim naming a pid with no indexed connection must leave a loud
    trail (WARN + SEL noop event + distinct ``claim-noop`` ack) instead of a
    silent {"updated": 0} — the silence is what hid the orphan-subagent bug
    for three days."""
    sel_calls: list[dict[str, Any]] = []

    class _FakeSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            sel_calls.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
    gw._CONN_INDEX.clear()
    with caplog.at_level("WARNING", logger="kiro_crew.mcp_gateway.gatewayd"):
        ack = gw._apply_claim(_claim(777777, "dashboard:chat-GHOST"))
    assert ack == {"type": "claim-noop", "updated": 0, "connections": 0}
    assert any("ZERO connections" in r.message for r in caplog.records)
    noop = [
        e for e in sel_calls
        if e.get("operation") == "mcp-gateway.caller-claim"
        and e.get("outcome") == "noop"
    ]
    assert len(noop) == 1


@pytest.mark.asyncio
async def test_claim_replaces_existing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-claim: unlike the stub recaller (deny-by-default), a gateway claim
    REPLACES an existing identity so a re-claimed pool runtime is never stale."""
    fb, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register("dashboard:old-session"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)

    ack = gw._apply_claim(_claim(_PID, "dashboard:new-session"))
    assert ack["updated"] == 1

    fb.forwarded.clear()
    reader.feed(_CALL)
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    reader.feed({"type": "unregister"})
    await task

    assert fb.callers[0].session_key == "dashboard:old-session"
    assert fb.callers[1].session_key == "dashboard:new-session"
    events = _claim_events(sel)
    assert len(events) == 1 and events[0]["outcome"] == "allowed"
    assert "dashboard:old-session" in (events[0].get("error") or "")


@pytest.mark.asyncio
async def test_claim_idempotent_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claiming the identity a connection already has is a no-op (no audit spam)."""
    fb, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register("dashboard:same-1"))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)

    ack = gw._apply_claim(_claim(_PID, "dashboard:same-1"))
    assert ack["type"] == "claimed" and ack["updated"] == 0 and ack["connections"] == 1
    reader.feed({"type": "unregister"})
    await task
    assert _claim_events(sel) == []


@pytest.mark.asyncio
async def test_claim_malformed_rejected_and_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-by-default validation: bad pid types and empty keys update nothing."""
    _, sel = _patch_env(monkeypatch)
    for bad in (
        _claim(0, "dashboard:x"),          # pid out of range
        _claim("42", "dashboard:x"),       # pid wrong type
        _claim(True, "dashboard:x"),       # bool is not a pid
        _claim(_PID, ""),                  # empty session key
    ):
        ack = gw._apply_claim(bad)
        assert ack["type"] == "claim-rejected", bad
    events = _claim_events(sel)
    assert len(events) == 4
    assert all(e["outcome"] == "denied" for e in events)


@pytest.mark.asyncio
async def test_claim_first_frame_connection_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-shot claim connection is answered with the ack frame and closes."""
    _patch_env(monkeypatch)
    writer = _RecordingWriter()
    reader = _QueueReader()
    reader.feed(_claim(_PID, "dashboard:chat-CP-9"))
    await _handle(reader, writer)
    assert writer.frames and writer.frames[0]["type"] == "claim-noop"
    assert writer.frames[0]["updated"] == 0  # nothing registered under _PID


# --- PID-recycle guard (claim start-token verification) ----------------------


def _fake_sel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sel_calls: list[dict[str, Any]] = []

    class _FakeSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            sel_calls.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
    return sel_calls


def _indexed_conn(pid: int, token: Optional[str], session_key: str = "") -> gw._StubConn:
    """Register-shaped connection: indexed under ``pid`` with a recorded
    start token, carrying an optional existing caller identity."""
    caller = None
    if session_key:
        caller = gw._caller_from_register(_claim(pid, session_key))
    conn = gw._StubConn("rt-stub", [pid], "rt-pool", caller, {pid: token})
    gw._conn_index_add(conn)
    return conn


def _claim_with_token(pid: int, session_key: str, token: Optional[str]) -> dict[str, Any]:
    frame = _claim(pid, session_key)
    frame["pid_start_id"] = token
    return frame


def test_claim_skips_recycled_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The core defect scenario: the register-time owner of PID P exited, the
    OS recycled P to a different session's runtime, and the claim for the NEW
    process must not retarget the STALE connection — previously it silently
    re-attributed every call (issue #1018). Definite token mismatch → skip,
    WARN, denied audit."""
    sel = _fake_sel(monkeypatch)
    conn = _indexed_conn(_PID, "111", "dashboard:original-owner")
    with caplog.at_level("WARNING", logger="kiro_crew.mcp_gateway.gatewayd"):
        ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:new-owner", "222"))
    assert ack == {"type": "claimed", "updated": 0, "connections": 1, "skipped": 1}
    assert conn.caller is not None
    assert conn.caller.session_key == "dashboard:original-owner"  # unchanged
    assert any("recycled" in r.message for r in caplog.records)
    denied = [e for e in _claim_events(sel) if e["outcome"] == "denied"]
    assert len(denied) == 1
    assert "recycled" in denied[0]["error"]


def test_claim_applies_on_matching_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same token on both sides — the register-time process is still alive —
    keeps the existing replace behavior."""
    sel = _fake_sel(monkeypatch)
    conn = _indexed_conn(_PID, "111", "dashboard:old-session")
    ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:new-session", "111"))
    assert ack == {"type": "claimed", "updated": 1, "connections": 1, "skipped": 0}
    assert conn.caller is not None and conn.caller.session_key == "dashboard:new-session"
    events = _claim_events(sel)
    assert len(events) == 1 and events[0]["outcome"] == "allowed"


@pytest.mark.parametrize(
    ("frame_token", "recorded_token"),
    [
        (None, None),  # neither side knows — Windows both ends / legacy frame
        (None, "111"),  # legacy claim frame without the field
        ("111", None),  # register-time token unreadable (Windows, /proc denied)
        ("111", "111"),  # both known and equal
    ],
)
def test_claim_unknown_token_is_match(
    monkeypatch: pytest.MonkeyPatch,
    frame_token: Optional[str],
    recorded_token: Optional[str],
) -> None:
    """``None`` on either side means "identity unknown" and MUST be treated
    as a match — otherwise Windows (where get_process_start_id is always
    None) and legacy claim frames would reject every claim."""
    _fake_sel(monkeypatch)
    conn = _indexed_conn(_PID, recorded_token)
    ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-TOK-1", frame_token))
    assert ack["updated"] == 1 and ack["skipped"] == 0
    assert conn.caller is not None
    assert conn.caller.session_key == "dashboard:chat-TOK-1"


def test_claim_mixed_bucket_retargets_only_matching_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index buckets stay keyed on the raw int PID, so a bucket may mix a
    stale (pre-recycle) connection with a live one — the per-connection token
    check is what disambiguates: only the matching conn is retargeted."""
    sel = _fake_sel(monkeypatch)
    stale = _indexed_conn(_PID, "111", "dashboard:original-owner")
    live = _indexed_conn(_PID, "222")
    ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:new-owner", "222"))
    assert ack == {"type": "claimed", "updated": 1, "connections": 2, "skipped": 1}
    assert stale.caller is not None
    assert stale.caller.session_key == "dashboard:original-owner"
    assert live.caller is not None and live.caller.session_key == "dashboard:new-owner"
    outcomes = sorted(e["outcome"] for e in _claim_events(sel))
    assert outcomes == ["allowed", "denied"]


def test_claim_non_string_token_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage (non-string) ``pid_start_id`` never becomes a mismatch: it is
    normalized to "unknown" so a malformed field cannot deny valid claims."""
    _fake_sel(monkeypatch)
    conn = _indexed_conn(_PID, "111")
    frame = _claim(_PID, "dashboard:chat-G-1")
    frame["pid_start_id"] = 12345  # wrong type
    ack = gw._apply_claim(frame)
    assert ack["updated"] == 1 and ack["skipped"] == 0
    assert conn.caller is not None and conn.caller.session_key == "dashboard:chat-G-1"


def test_stubconn_legacy_constructor_defaults_to_empty_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-guard constructor shape (no ``pid_start_ids``) keeps working:
    the mapping defaults to empty, every lookup is "unknown", and claims
    still apply."""
    _fake_sel(monkeypatch)
    conn = gw._StubConn("legacy-stub", [_PID], "legacy-pool", None)
    assert conn.pid_start_ids == {}
    gw._conn_index_add(conn)
    ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-L-1", "999"))
    assert ack["updated"] == 1 and ack["skipped"] == 0
    assert conn.caller is not None and conn.caller.session_key == "dashboard:chat-L-1"


@pytest.mark.asyncio
async def test_register_records_start_tokens_and_claim_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full wiring through the register handler: tokens are snapshotted for
    every indexed PID at register time, and a later claim carrying a
    different token for that PID is skipped."""
    fb, sel = _patch_env(monkeypatch)
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: "111")
    reader = _QueueReader()
    reader.feed(_register(""))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    (conn,) = gw._CONN_INDEX[_PID]
    assert conn.pid_start_ids == {p: "111" for p in _ANCESTORS}

    ack = gw._apply_claim(_claim_with_token(_PID, "dashboard:chat-W-1", "222"))
    assert ack["updated"] == 0 and ack["skipped"] == 1

    fb.forwarded.clear()
    reader.feed(_CALL)
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    reader.feed({"type": "unregister"})
    await task
    assert fb.callers == [None, None]  # identity never misattributed
    denied = [e for e in _claim_events(sel) if e["outcome"] == "denied"]
    assert len(denied) == 1


def test_build_claim_frame_includes_pid_start_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sender resolves the claimed runtime's start token and puts it on
    the frame verbatim (including None passthrough on platforms without a
    token)."""
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: f"tok-{pid}")
    frame = claim_mod.build_claim_frame(777, "dashboard:chat-F-1", None)
    assert frame["pid_start_id"] == "tok-777"

    monkeypatch.setattr(pc, "get_process_start_id", lambda _pid: None)
    frame = claim_mod.build_claim_frame(777, "dashboard:chat-F-1", None)
    assert frame["pid_start_id"] is None


@pytest.mark.asyncio
async def test_conn_index_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register indexes the connection under EVERY ancestor PID; teardown
    cleans all of them up."""
    fb, _ = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register(""))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await asyncio.wait_for(fb.forwarded.wait(), timeout=5.0)
    for pid in _ANCESTORS:
        assert pid in gw._CONN_INDEX and len(gw._CONN_INDEX[pid]) == 1
    reader.eof()
    await task
    for pid in _ANCESTORS:
        assert pid not in gw._CONN_INDEX


def test_register_pids_legacy_and_garbage() -> None:
    """_register_pids accepts the ancestor list, falls back to legacy
    single parent_pid, and drops garbage entries deny-by-default."""
    assert gw._register_pids({"ancestor_pids": [10, 20, 30]}) == [10, 20, 30]
    assert gw._register_pids({"parent_pid": 42}) == [42]
    assert gw._register_pids({"ancestor_pids": [0, 1, True, "9", 77]}) == [77]
    assert gw._register_pids({}) == []


def test_stub_register_payload_carries_ancestor_pids() -> None:
    """The Register payload names the stub's full ancestor chain (nearest
    first) so gatewayd can index every level of the runtime process tree."""
    args = stub_mod._parse_args(
        ["--server", "echo-mcp", "--agent", "cp-agent",
         "--target-command", "/bin/true", "--work-dir", "/tmp"]
    )
    payload = stub_mod.build_register_payload(args)
    chain = payload["ancestor_pids"]
    assert isinstance(chain, list) and chain, chain
    assert chain[0] == os.getppid()
    assert all(isinstance(p, int) and p > 1 for p in chain)
    # Chain walks upward: on Linux the second entry (when present) must be
    # the parent of the first.
    assert len(set(chain)) == len(chain)  # no cycles


def test_stub_register_payload_keeps_legacy_user_identity_key() -> None:
    """Wire-compat ratchet (#3604): ``user_identity`` was deleted as a
    PoolKey dimension, but the register payload must keep sending the key.
    The manager adopts a running daemon with no version handshake, so a
    daemon predating the deletion can serve new stubs — and its
    ``PoolKey.from_register`` hard-requires the field, rejecting a payload
    without it and silently un-pooling every session until the daemon
    restarts. Drop this only when no pre-#3604 daemon can be adopted."""
    args = stub_mod._parse_args(
        ["--server", "echo-mcp", "--agent", "cp-agent",
         "--target-command", "/bin/true", "--work-dir", "/tmp"]
    )
    payload = stub_mod.build_register_payload(args)
    assert "user_identity" in payload
    assert isinstance(payload["user_identity"], str) and payload["user_identity"]


def test_classify_session_type() -> None:
    assert claim_mod.classify_session_type("dashboard:chat-1") == "dashboard"
    assert claim_mod.classify_session_type("cron:job-1") == "cron"
    assert claim_mod.classify_session_type("hook:h-1") == "hook"
    assert claim_mod.classify_session_type("slack:123.456") == "slack-thread"
    assert claim_mod.classify_session_type("") == "unknown"


def _endpoint_dir() -> str | None:
    """Where to put a test endpoint.

    On POSIX these tests bind a real socket, and ``AF_UNIX`` caps ``sun_path``
    at ~104 bytes -- pytest's ``tmp_path`` blows past that on macOS -- so they
    bind under ``/tmp``. A Windows named pipe has neither the length limit nor a
    ``/tmp``, so the platform default applies there.
    """
    return None if pc.IS_WINDOWS else "/tmp"


@pytest.mark.asyncio
async def test_send_claim_roundtrip(short_sock_dir) -> None:
    """The sender round-trips a claim frame over a real unix socket and treats
    a ``claimed`` ack as success, anything else as failure."""
    received: list[dict[str, Any]] = []
    sock = short_sock_dir / "gw.sock"

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        received.append(json.loads(raw))
        writer.write(json.dumps({"type": "claimed", "updated": 3}).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await transport.serve(sock, _serve, limit=1 << 16)
    try:
        ok = await claim_mod.send_claim(str(sock), 777, "dashboard:chat-RT-1", "C1")
    finally:
        server.close()
        await server.wait_closed()
    assert ok is True
    assert received[0]["type"] == "claim" and received[0]["pid"] == 777
    assert received[0]["caller"]["session_key"] == "dashboard:chat-RT-1"
    assert received[0]["caller"]["session_type"] == "dashboard"


@pytest.mark.asyncio
async def test_send_claim_failure_paths(short_sock_dir) -> None:
    """A missing socket or a non-ack response returns False without raising."""
    base = short_sock_dir
    assert await claim_mod.send_claim(str(base / "absent.sock"), 7, "dashboard:x") is False

    sock = base / "nak.sock"

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(json.dumps({"type": "claim-rejected", "reason": "test"}).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await transport.serve(sock, _serve, limit=1 << 16)
    try:
        assert await claim_mod.send_claim(str(sock), 7, "dashboard:x") is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_send_claim_aggregate_timeout_bound(monkeypatch: pytest.MonkeyPatch, short_sock_dir) -> None:
    """A gatewayd that accepts but never responds is bounded by ONE aggregate
    budget — not one budget per phase (connect/drain/readline), which would
    triple the worst-case stall (review-bot finding f-d76c6f17)."""
    monkeypatch.setattr(claim_mod, "_CLAIM_TIMEOUT_SECS", 0.3)
    sock = short_sock_dir / "stall.sock"
    stalled = asyncio.Event()

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()  # accept the frame, then stall forever
        await stalled.wait()
        writer.close()

    server = await transport.serve(sock, _serve, limit=1 << 16)
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await claim_mod.send_claim(str(sock), 7, "dashboard:x") is False
        elapsed = loop.time() - start
        # Single aggregate bound: well under 2x the budget, never 3x.
        assert elapsed < claim_mod._CLAIM_TIMEOUT_SECS * 2
    finally:
        stalled.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_schedule_claim_preconditions() -> None:
    """schedule_claim silently no-ops when socket/pid/key are missing — the
    legitimate non-gateway and test contexts must never error or leak tasks."""
    claim_mod.schedule_claim(None, 42, "dashboard:x")
    claim_mod.schedule_claim("/tmp/x.sock", None, "dashboard:x")
    claim_mod.schedule_claim("/tmp/x.sock", 0, "dashboard:x")
    claim_mod.schedule_claim("/tmp/x.sock", 42, "")
    assert claim_mod._PENDING == set()


@pytest.mark.asyncio
async def test_connection_rejected_when_owner_only_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback gate must actually deny.

    Where no peer-principal mechanism is wired (macOS), admission rests entirely
    on the endpoint being owner-only. Every other test in this file asserts the
    allow side by stubbing that gate to ``True``; this one asserts the deny side,
    so a regression that turns the fallback into a pass-through cannot land
    silently. The handler must return before the Register frame is consumed --
    no backend acquired, nothing forwarded, nothing written back.
    """
    fb, _sel = _patch_env(monkeypatch)
    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", False)
    monkeypatch.setattr(socketsec, "socket_owner_only", lambda _p: False)

    reader = _QueueReader()
    reader.feed(_register("dashboard:chat-NS-1"))
    reader.feed(_CALL)
    writer = _RecordingWriter()
    await _handle(reader, writer)

    assert writer.frames == [], f"rejected connection replied: {writer.frames}"
    assert not fb.forwarded.is_set(), "a denied connection reached the backend"


@pytest.mark.asyncio
async def test_connection_rejected_when_peer_principal_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positively-confirmed foreign principal is denied, and so is a lookup
    that could not confirm one: only MATCH proceeds."""
    for outcome in (
        socketsec.PeerCredResult.MISMATCH,
        socketsec.PeerCredResult.UNVERIFIABLE,
    ):
        fb, _sel = _patch_env(monkeypatch)
        monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
        monkeypatch.setattr(socketsec, "check_peer_is_self", lambda _w, o=outcome: o)

        reader = _QueueReader()
        reader.feed(_register("dashboard:chat-NS-1"))
        reader.feed(_CALL)
        writer = _RecordingWriter()
        await _handle(reader, writer)

        assert writer.frames == [], f"{outcome.value}: replied {writer.frames}"
        assert not fb.forwarded.is_set(), f"{outcome.value}: reached the backend"
