"""In-process functional tests for the warm-pool caller-repair recaller path.

Drives the real ``gatewayd._handle_connection`` loop with a fake backend that
records the caller passed to ``forward_from_stub`` (and a fake SEL capturing
audit events), so we verify actual loop behaviour AND the audit trail:

* a key-less register leaves forwarded calls caller-less, and a ``recaller``
  frame flips the injected caller to the real session (emitting an ``allowed``
  audit),
* a connection that already carries a session identity REJECTS a recaller
  claiming a different session (deny-by-default pivot; ``denied`` audit), and
* an empty/malformed recaller is ignored (``denied`` audit) — all recaller
  outcomes land on the SEL trail.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.member_memory_auth import PROOF_META_KEY

pytestmark = pytest.mark.xdist_group("mcp_gateway")


@pytest.mark.asyncio
async def test_member_proof_uses_kernel_peer_for_each_tool_listing_and_call(monkeypatch):
    issued = []
    loop_thread = threading.get_ident()

    def issue(session, peer_pid):
        assert threading.get_ident() != loop_thread
        issued.append((session, peer_pid))
        return f"issued-{len(issued)}"

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "protected_member_session_for_pid", lambda _pid: "dashboard:reviewer")
    monkeypatch.setattr(gw, "issue_member_session_proof", issue)
    register = _register("dashboard:reviewer")
    register.update(ancestor_pids=[9999], memberMemoryProof="forged-register")
    backend, audit = await _run(
        [
            register,
            {**_CALL, "method": "tools/list"},
            _CALL,
            {**_CALL, "method": "resources/list"},
            _CALL,
        ],
        monkeypatch,
    )
    assert issued == [("dashboard:reviewer", 8001)] * 3
    assert [caller.member_memory_proof for caller in backend.callers] == [
        "issued-1",
        "issued-2",
        "",
        "issued-3",
    ]
    assert all(caller.session_key == "dashboard:reviewer" for caller in backend.callers)
    assert "issued-" not in json.dumps(audit)
    assert "issued-" not in repr(backend.callers)


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_pid", [None, 8001])
async def test_forged_member_register_never_supplies_private_authority(monkeypatch, peer_pid):
    issued = []

    def refuse(session, pid):
        issued.append((session, pid))
        return ""

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: peer_pid)
    monkeypatch.setattr(gw, "issue_member_session_proof", refuse)
    register = _register("dashboard:victim")
    register.update(ancestor_pids=[7777], memberMemoryProof="forged-register")
    register["caller"] = {"session_key": "dashboard:victim", PROOF_META_KEY: "nested-forgery"}
    backend, _audit = await _run([register, _CALL, _CALL], monkeypatch)
    assert all(caller.member_memory_proof == "" for caller in backend.callers)
    assert issued == ([] if peer_pid is None else [("dashboard:victim", 8001)] * 2)


@pytest.mark.asyncio
async def test_member_issuer_failure_refuses_authority_without_leaking_error(monkeypatch, caplog):
    def fail(_session, _pid):
        raise OSError("secret-proof-material")

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "issue_member_session_proof", fail)
    backend, _audit = await _run([_register("dashboard:reviewer"), _CALL], monkeypatch)
    assert backend.callers[0].member_memory_proof == ""
    assert "member memory caller verification unavailable" in caplog.text
    assert "secret-proof-material" not in caplog.text


def _register(session_key: str) -> dict[str, Any]:
    return {
        "type": "register",
        "stub_uuid": "rc-stub-0001",
        "server_name": "echo-mcp",
        "agent_name": "rc-agent",
        "command_args_hash": "0" * 64,
        "effective_env_hash": "1" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "2" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "channel_id": "C_RC",
        "config_snapshot_hash": "3" * 64,
        "session_key": session_key,
        "session_type": "unknown" if not session_key else "dashboard",
        "principal_id": "",
    }


_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}


class _FakeReader:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._q = [(json.dumps(f) + "\n").encode() for f in frames]

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if not self._q:
            raise asyncio.IncompleteReadError(b"", None)
        return self._q.pop(0)


class _FakeWriter:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, _b: bytes) -> None:
        self.frames.extend(json.loads(line) for line in _b.decode().splitlines() if line)

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
        self._pending_requests: dict = {}

    async def attach_stub(self, _uuid: str) -> "asyncio.Queue[bytes]":
        return asyncio.Queue()

    async def detach_stub(self, _uuid: str) -> int:
        return 0

    async def cancel_in_flight_for_stub(self, _stub_uuid: str) -> list[str]:
        return []

    async def recycle_if_idle(self) -> bool:
        return False

    async def forward_from_stub(
        self, _uuid: str, _msg: dict, caller: Any = None, tenant_nonce: str = ""
    ) -> None:
        self.callers.append(caller)


class _FakePool:
    """Minimal pool double: the fork's lazy-spawn attach releases its hand-out
    reservation via ``pool.unreserve`` in a ``finally`` (absent upstream at this
    commit), so a bare ``object()`` would raise AttributeError. Nothing else on
    the pool is reached (hot_keys=None skips ``pool.get``)."""

    def unreserve(self, _key: object) -> None:
        pass


def _rekey_events(sel_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SEL events for the caller-rekey permission decision (accept + reject),
    excluding the register-time ``mcp-gateway.connect`` accept."""
    return [e for e in sel_calls if e.get("operation") == "mcp-gateway.caller-rekey"]


async def _run(
    frames: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    writer: _FakeWriter | None = None,
) -> tuple[_FakeBackend, list[dict[str, Any]]]:
    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self",
        lambda _w: socketsec.PeerCredResult.MATCH,
    )
    monkeypatch.setattr(socketsec, "socket_owner_only", lambda _path: True)
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

    await asyncio.wait_for(
        gw._handle_connection(
            _FakeReader(frames),
            writer or _FakeWriter(),
            pool=_FakePool(),
            resolver=object(),
            socket_path=Path("/tmp/rc.sock"),
            hot_keys=None,
        ),
        timeout=5.0,
    )
    return fake_backend, sel_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
@pytest.mark.parametrize(
    "claimed,protected",
    [
        ("dashboard:global", "dashboard:member-reviewer"),
        ("", "dashboard:member-reviewer"),
        ("dashboard:member-reviewer", ""),
        ("", ""),
    ],
)
async def test_protected_peer_cannot_downgrade_to_v1_or_omit_its_caller(
    monkeypatch, claimed, protected, method
):
    issued = []
    loop_thread = threading.get_ident()

    def resolve(peer_pid):
        assert peer_pid == 8001
        assert threading.get_ident() != loop_thread
        return protected

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", []))
    monkeypatch.setattr(gw, "protected_member_session_for_pid", resolve)
    monkeypatch.setattr(gw, "issue_member_session_proof", lambda *args: issued.append(args) or "")
    writer = _FakeWriter()
    backend, _audit = await _run(
        [_register(claimed), {**_CALL, "method": method}], monkeypatch, writer=writer
    )
    assert backend.callers == []
    assert issued == []
    refusal = next(frame for frame in writer.frames if frame.get("id") == _CALL["id"])
    assert refusal["error"]["message"] == (
        "The caller does not match its protected runtime identity. Reopen the member conversation and retry."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
@pytest.mark.parametrize("failure", ["blank", "exception"])
async def test_a_protected_peer_never_continues_without_its_proof(monkeypatch, failure, method):
    def issue(_session, _pid):
        if failure == "exception":
            raise OSError("private-key-material")
        return ""

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "protected_member_session_for_pid", lambda _pid: "dashboard:reviewer")
    monkeypatch.setattr(gw, "issue_member_session_proof", issue)
    writer = _FakeWriter()
    backend, _audit = await _run(
        [_register("dashboard:reviewer"), {**_CALL, "method": method}], monkeypatch, writer=writer
    )
    assert backend.callers == []
    assert "protected runtime proof is unavailable" in writer.frames[-1]["error"]["message"]
    assert "private-key-material" not in json.dumps(writer.frames)


@pytest.mark.asyncio
async def test_a_binding_read_failure_cannot_be_treated_as_an_unowned_v1_caller(monkeypatch):
    def unreadable(_pid):
        raise OSError("protected-private-path")

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "protected_member_session_for_pid", unreadable)
    writer = _FakeWriter()
    backend, _audit = await _run([_register("dashboard:global"), _CALL], monkeypatch, writer=writer)
    assert backend.callers == []
    assert "Cannot verify the protected runtime identity" in writer.frames[-1]["error"]["message"]
    assert "protected-private-path" not in json.dumps(writer.frames)


@pytest.mark.asyncio
async def test_protected_identity_is_rechecked_after_a_runtime_rekey(monkeypatch):
    bindings = iter(["dashboard:reviewer", "dashboard:writer"])
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "protected_member_session_for_pid", lambda _pid: next(bindings))
    monkeypatch.setattr(gw, "issue_member_session_proof", lambda _session, _pid: "current.proof")
    writer = _FakeWriter()
    backend, _audit = await _run(
        [_register("dashboard:reviewer"), _CALL, {**_CALL, "id": 2}], monkeypatch, writer=writer
    )
    assert len(backend.callers) == 1
    assert backend.callers[0].member_memory_proof == "current.proof"
    assert writer.frames[-1]["id"] == 2
    assert "does not match" in writer.frames[-1]["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
async def test_an_unowned_legacy_v1_caller_keeps_working_without_a_proof(monkeypatch, method):
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _writer: 8001)
    monkeypatch.setattr(gw, "protected_member_session_for_pid", lambda _pid: None)
    monkeypatch.setattr(gw, "issue_member_session_proof", lambda _session, _pid: "")
    backend, _audit = await _run(
        [_register("dashboard:global"), {**_CALL, "method": method}], monkeypatch
    )
    assert backend.callers[0].session_key == "dashboard:global"
    assert backend.callers[0].member_memory_proof == ""


@pytest.mark.asyncio
async def test_recaller_flips_injected_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key-less register → forwarded call has caller=None; after a recaller the
    next forwarded call carries the real identity, emitting an 'allowed' audit."""
    fb, sel = await _run(
        [
            _register(""),
            _CALL,
            {"type": "recaller", "session_key": "dashboard:chat-RC-1",
             "session_type": "dashboard", "principal_id": "rc", "channel_id": "C_RC"},
            _CALL,
            {"type": "unregister"},
        ],
        monkeypatch,
    )
    assert len(fb.callers) == 2, fb.callers
    before, after = fb.callers
    assert before is None
    assert after is not None and after.session_key == "dashboard:chat-RC-1"
    rekey = _rekey_events(sel)
    assert len(rekey) == 1 and rekey[0]["outcome"] == "allowed"
    assert rekey[0]["caller"] == "dashboard:chat-RC-1"


@pytest.mark.asyncio
async def test_recaller_denied_when_caller_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-by-default: a connection that already has a session identity rejects
    a recaller claiming a DIFFERENT session (no pivot). Every forward keeps the
    original identity and a 'denied' SEL event is emitted."""
    fb, sel = await _run(
        [
            _register("dashboard:orig-1"),
            _CALL,
            {"type": "recaller", "session_key": "dashboard:evil-2",
             "session_type": "dashboard", "principal_id": "x", "channel_id": "C_RC"},
            _CALL,
            {"type": "unregister"},
        ],
        monkeypatch,
    )
    assert len(fb.callers) == 2, fb.callers
    for c in fb.callers:
        assert c is not None and c.session_key == "dashboard:orig-1", fb.callers
    rekey = _rekey_events(sel)
    assert len(rekey) == 1 and rekey[0]["outcome"] == "denied"
    assert "dashboard:evil-2" in rekey[0]["error"]


@pytest.mark.asyncio
async def test_recaller_malformed_key_ignored_and_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/malformed recaller is ignored (caller stays key-less) and the
    rejection is audited — all recaller outcomes hit the SEL trail."""
    fb, sel = await _run(
        [
            _register(""),
            {"type": "recaller", "session_key": ""},
            _CALL,
            {"type": "unregister"},
        ],
        monkeypatch,
    )
    assert fb.callers == [None], fb.callers
    rekey = _rekey_events(sel)
    assert len(rekey) == 1 and rekey[0]["outcome"] == "denied"
    assert "empty/malformed" in rekey[0]["error"]


# --- Warm-pool recaller frame contract -------------------------------------


def test_caller_from_register_parses_recaller_frame() -> None:
    """The warm-pool ``recaller`` frame reuses the Register caller wire shape,
    so ``_caller_from_register`` must parse it in both the flat and nested
    forms and — critically — return ``None`` for an empty/missing session_key
    so a bogus recaller can never clobber a good caller in
    ``_handle_connection``."""
    from kiro_crew.mcp_gateway.gatewayd import _caller_from_register

    # Flat shape — exactly what stub._recaller_loop emits.
    flat = {
        "type": "recaller",
        "session_key": "dashboard:chat-5-9",
        "session_type": "dashboard",
        "principal_id": "alice",
        "channel_id": "C1",
    }
    ctx = _caller_from_register(flat)
    assert ctx is not None
    assert ctx.session_key == "dashboard:chat-5-9"
    assert ctx.channel_id == "C1"
    assert ctx.from_gateway is True

    # Nested caller dict is also accepted (parity with Register).
    nested = {
        "type": "recaller",
        "caller": {
            "session_key": "cron:job-7",
            "session_type": "cron",
            "principal_id": "",
            "channel_id": "",
        },
    }
    ctx2 = _caller_from_register(nested)
    assert ctx2 is not None
    assert ctx2.session_key == "cron:job-7"
    assert ctx2.session_type == "cron"

    # Empty / missing session_key → None (the no-clobber guard).
    assert _caller_from_register({"type": "recaller"}) is None
    assert _caller_from_register({"type": "recaller", "session_key": ""}) is None


def test_caller_rekey_emits_sel_audit_event(monkeypatch: "pytest.MonkeyPatch") -> None:
    """The mid-connection caller re-bind (recaller) is a security-relevant
    authorization change, so it MUST emit a SEL audit event — mirroring the
    register-time accept audit. Verify _audit_caller_rekey records the
    identity transition via SecurityEventLog.log_api_access."""
    from kiro_crew.mcp_gateway import gatewayd as gw

    calls: list[dict[str, Any]] = []

    class _FakeSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
    gw._audit_caller_rekey("dashboard:chat-QA-1", "kirocrew:kirocrew-core")

    assert len(calls) == 1
    ev = calls[0]
    assert ev["caller"] == "dashboard:chat-QA-1"
    assert ev["operation"] == "mcp-gateway.caller-rekey"
    assert ev["outcome"] == "allowed"
    assert ev["source"] == "gateway"
    assert ev["resources"] == "kirocrew:kirocrew-core"


def test_recaller_rejected_emits_denied_sel_audit_event(monkeypatch: "pytest.MonkeyPatch") -> None:
    """A rejected recaller pivot (connection already identified) is a
    security-relevant permission decision, so it MUST emit a denied SEL audit
    event capturing the attempted target — mirroring _audit_peer_denied."""
    from kiro_crew.mcp_gateway import gatewayd as gw

    calls: list[dict[str, Any]] = []

    class _FakeSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
    gw._audit_recaller_rejected(
        "dashboard:orig-1", "kirocrew:kirocrew-core",
        "recaller pivot attempt to session_key=dashboard:evil-2",
    )

    assert len(calls) == 1
    ev = calls[0]
    assert ev["caller"] == "dashboard:orig-1"
    assert ev["operation"] == "mcp-gateway.caller-rekey"
    assert ev["outcome"] == "denied"
    assert ev["source"] == "gateway"
    assert ev["resources"] == "kirocrew:kirocrew-core"
    assert ev["error"] == "recaller pivot attempt to session_key=dashboard:evil-2"
