"""Per-project MCP backends: the session record, the pool key, and who may use it.

``daemon.project_scope`` keys a per-project backend on the project root and
sandbox plan the owning gateway recorded for a session, never on a Register
frame. These tests pin who may record one, what key a connection gets, when it
falls back to the stub's own exec, and that the backend answers ``roots/list``
with its own root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp_gateway_backend_coverage_helpers import _frames, _line, _make_backend

from kiro_crew import platform_compat
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.daemon import project_scope
from kiro_crew.mcp_gateway.daemon.admission_protocol import _classify_rejection
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

OWNER = 4100


@pytest.fixture(autouse=True)
def _owner_and_sandbox(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(gw, "_OWNER_PID", OWNER)
    _floor_monkeypatch.setattr(gw, "SecurityEventLog", lambda: MagicMock())
    _floor_monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", lambda _o, _p: False)
    _floor_monkeypatch.setattr("kiro_crew.sandbox.detect_backend", lambda _mode="auto": "namespace")


def _sender(monkeypatch: pytest.MonkeyPatch, pid: int | None) -> None:
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: pid)


def _key(name: str = "files") -> PoolKey:
    return PoolKey(name, "cmd", "env", "/data/workspace/_default", "1.0", 1000)


def _conn(session_key: str, *, attested: bool = True) -> Any:
    return SimpleNamespace(
        caller=CallerContext(session_key=session_key, session_type="t", from_gateway=True),
        session_attested=attested,
    )


async def _record(
    monkeypatch: pytest.MonkeyPatch, session: str, project: Path, **extra: Any
) -> dict:
    _sender(monkeypatch, OWNER)
    frame = {"type": "session-project", "session_key": session, "root": str(project)}
    frame.setdefault("sandbox_mode", "standard")
    frame.update(extra)
    return await project_scope.apply_session_project(frame, object())


# ── the pool key ────────────────────────────────────────────────────────────


def test_key_without_project_hashes_as_before() -> None:
    key = _key()
    fields = {k: v for k, v in asdict(key).items() if k != "sandbox_hash"}
    old = hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert key.stable_hash() == old


def test_register_frame_cannot_name_a_sandbox_hash() -> None:
    frame = {**asdict(_key()), "sandbox_hash": "forged"}
    assert PoolKey.from_register(frame).sandbox_hash == ""


# ── who may record a session's project ──────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_owner_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sender(monkeypatch, OWNER + 1)
    reply = await project_scope.apply_session_project(
        {
            "type": "session-project",
            "session_key": "s",
            "root": str(tmp_path),
            "sandbox_mode": "standard",
        },
        object(),
    )
    assert reply["type"] == "session-project-rejected"
    assert project_scope.project_binding_for_session("s") is None


@pytest.mark.asyncio
async def test_unreadable_sender_pid_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sender(monkeypatch, None)
    reply = await project_scope.apply_session_project(
        {
            "type": "session-project",
            "session_key": "s",
            "root": str(tmp_path),
            "sandbox_mode": "standard",
        },
        object(),
    )
    assert reply["type"] == "session-project-rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"sandbox_mode": "off"},
        {"sandbox_mode": "none"},
        {"root": "relative/dir"},
        {"hidden_dirs": ["not/absolute"]},
        {"expose_files": "/not/a/list"},
    ],
)
async def test_bad_records_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: dict
) -> None:
    reply = await _record(monkeypatch, "s", tmp_path, **extra)
    assert reply["type"] == "session-project-rejected"
    assert project_scope.project_binding_for_session("s") is None


@pytest.mark.asyncio
async def test_record_then_clear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert (await _record(monkeypatch, "s", tmp_path))["type"] == "session-project-set"
    assert project_scope.project_binding_for_session("s").root == str(tmp_path)
    cleared = await _record(monkeypatch, "s", tmp_path, root="")
    assert cleared["type"] == "session-project-cleared"
    assert project_scope.project_binding_for_session("s") is None
    key = PoolKey(
        "files",
        "c",
        "e",
        str(tmp_path),
        "1",
        1,
        project_scope.ProjectBinding(str(tmp_path), "standard").sandbox_hash(),
    )
    assert project_scope.project_plan_for_key(key) is None


@pytest.mark.asyncio
async def test_a_plan_stays_while_another_session_uses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _record(monkeypatch, "s1", tmp_path)
    await _record(monkeypatch, "s2", tmp_path)
    key = await project_scope.project_scoped_key(BackendPool(4), _key(), _conn("s1"))
    await _record(monkeypatch, "s1", tmp_path, root="")
    assert project_scope.project_plan_for_key(key) is not None
    await _record(monkeypatch, "s2", tmp_path, root="")
    assert project_scope.project_plan_for_key(key) is None


# ── which key a connection gets ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_project_shares_and_other_project_splits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    hidden_dir = str(tmp_path / ".aws")
    for session, root in (("a1", a), ("a2", a), ("b1", b)):
        await _record(monkeypatch, session, root, hidden_dirs=[hidden_dir])
    pool = BackendPool(4)
    ka1 = await project_scope.project_scoped_key(pool, _key(), _conn("a1"))
    ka2 = await project_scope.project_scoped_key(pool, _key(), _conn("a2"))
    kb1 = await project_scope.project_scoped_key(pool, _key(), _conn("b1"))
    assert ka1 == ka2
    assert ka1.work_dir == str(a) and kb1.work_dir == str(b)
    assert ka1.stable_hash() != kb1.stable_hash()
    assert project_scope.project_plan_for_key(ka1).hidden_dirs == (hidden_dir,)


@pytest.mark.asyncio
async def test_other_sandbox_plan_splits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    await _record(monkeypatch, "s1", tmp_path, sandbox_mode="standard")
    await _record(monkeypatch, "s2", tmp_path, sandbox_mode="strict")
    pool = BackendPool(4)
    k1 = await project_scope.project_scoped_key(pool, _key(), _conn("s1"))
    k2 = await project_scope.project_scoped_key(pool, _key(), _conn("s2"))
    assert k1.work_dir == k2.work_dir and k1.sandbox_hash != k2.sandbox_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["unattested", "unrecorded", "no_sandbox", "control_plane"])
async def test_falls_back_to_the_stubs_own_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, why: str
) -> None:
    await _record(monkeypatch, "s", tmp_path)
    conn, key = _conn("s"), _key()
    if why == "unattested":
        conn = _conn("s", attested=False)
    elif why == "unrecorded":
        conn = _conn("other")
    elif why == "no_sandbox":
        monkeypatch.setattr("kiro_crew.sandbox.detect_backend", lambda _m="auto": "none")
    else:
        key = _key("kirocrew-core")
    with pytest.raises(project_scope.ProjectScopeUnavailable) as exc:
        await project_scope.project_scoped_key(BackendPool(4), key, conn)
    verdict = _classify_rejection(exc.value, exclusive=False)
    assert verdict is not None and verdict.fallback is True


@pytest.mark.asyncio
async def test_cap_per_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(project_scope, "PROJECT_INSTANCES_PER_SERVER", 1)
    await _record(monkeypatch, "s", tmp_path)
    pool = BackendPool(4)
    running = SimpleNamespace(pool_key=PoolKey("files", "c", "e", "/x", "1", 1, "other"))
    monkeypatch.setattr(pool, "live_pooled_backends", lambda: [running])
    with pytest.raises(project_scope.ProjectScopeUnavailable):
        await project_scope.project_scoped_key(pool, _key(), _conn("s"))


@pytest.mark.asyncio
async def test_backend_serves_only_its_own_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    await _record(monkeypatch, "a1", a)
    await _record(monkeypatch, "b1", b)
    key = await project_scope.project_scoped_key(BackendPool(4), _key(), _conn("a1"))
    backend = SimpleNamespace(pool_key=key)
    assert project_scope.backend_serves_project(backend, _conn("a1"))
    assert not project_scope.backend_serves_project(backend, _conn("b1"))
    assert not project_scope.backend_serves_project(backend, _conn("a1", attested=False))
    assert project_scope.backend_serves_project(SimpleNamespace(pool_key=_key()), _conn("b1"))


# ── the backend itself ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roots_list_is_answered_with_the_project_root(tmp_path: Path) -> None:
    backend = _make_backend()
    backend.pool_key = replace(backend.pool_key, work_dir=str(tmp_path), sandbox_hash="h")
    inbox = await backend.attach_stub("s1")
    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": 7, "method": "roots/list"}))
    assert inbox.empty()
    (reply,) = [f for f in _frames(backend) if f.get("id") == 7]
    assert reply["result"]["roots"] == [{"uri": tmp_path.as_uri(), "name": tmp_path.name}]


def test_spawn_wraps_in_the_sessions_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], mode: str, **kw: Any) -> tuple[list[str], dict, None]:
        seen.update(kw, argv=argv, mode=mode)
        kw["forward_ssh_auth_sock_out"].append(kw["forward_ssh_auth_sock"])
        return ["launcher", *argv], dict(kw["env"]), None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", fake)
    plan = project_scope.ProjectBinding(str(tmp_path), "cc", ("/h/.aws",), ("/h/.aws/config",))
    argv, _env, _ = backend_mod._project_sandbox_argv(["srv"], {"A": "1"}, plan, tmp_path / "t")
    assert argv == ["launcher", "srv"]
    assert seen["mode"] == "cc"
    assert seen["project_folder"] == str(tmp_path)
    assert seen["extra_hidden_dirs"] == ("/h/.aws",)
    assert seen["extra_expose_files"] == ("/h/.aws/config",)
    assert seen["extra_writable_dirs"] == (str(tmp_path / "t"),)


# ── the connection handler ──────────────────────────────────────────────────


class _CapturingWriter:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        for raw in data.decode().splitlines():
            if raw.strip():
                self.frames.append(json.loads(raw))

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


async def _drive_project_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple:
    """A project-scoped stub whose session key is only the one its frame names."""
    from test_mcp_gateway_exclusive_backend import (
        _FrameReader,
        _HandlerBackend,
        _RecordingPool,
        _register_frame,
    )

    from kiro_crew.mcp_gateway import socketsec
    from kiro_crew.testing.wait import finished

    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH)
    monkeypatch.setattr(socketsec, "socket_owner_only", lambda _path: True)
    register = {**_register_frame(poolable=True), "project_scoped": True}
    # The gateway recorded a project for the session the frame names.
    await _record(monkeypatch, register["session_key"], tmp_path)
    pool = _RecordingPool()

    async def _fake_acquire(_pool: Any, key: Any, _resolver: Any, **kw: Any):
        pool.acquire_kwargs.append({"key": key, **kw})
        return _HandlerBackend(), True

    monkeypatch.setattr(gw, "_acquire_backend", _fake_acquire)
    writer = _CapturingWriter()
    ensure = {"type": "ensure_backend"}
    await finished(
        gw._handle_connection(
            _FrameReader([register, ensure]),
            writer,
            pool=pool,
            resolver=object(),
            socket_path=tmp_path / "ex.sock",
            hot_keys=None,
        ),
        "the connection handler draining its frames",
    )
    return pool, writer


@pytest.mark.asyncio
async def test_a_frame_named_session_cannot_pick_a_project_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Register frame's own session key is not attested, so no project key is
    built from it: the stub is told to start the server itself."""
    pool, writer = await _drive_project_scoped(monkeypatch, tmp_path)
    assert pool.acquire_kwargs == []
    (rejected,) = [f for f in writer.frames if f.get("type") == "rejected"]
    assert rejected["fallback"] is True and rejected["class"] == "isolation"


@pytest.mark.parametrize("kept", [True, False])
def test_spawn_env_strips_python_env_and_keeps_only_a_granted_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kept: bool
) -> None:
    def fake(argv: list[str], _mode: str, **kw: Any) -> tuple[list[str], dict, None]:
        kw["forward_ssh_auth_sock_out"].append(kept)
        return argv, {"PATH": "/bin"}, None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", fake)
    env = {"PATH": "/bin", "PYTHONPATH": "/x", "SSH_AUTH_SOCK": "/run/a.sock"}
    plan = project_scope.ProjectBinding(str(tmp_path), "standard")
    _argv, out, _ = backend_mod._project_sandbox_argv(["srv"], env, plan, None)
    assert "PYTHONPATH" not in out
    assert ("SSH_AUTH_SOCK" in out) is kept


@pytest.mark.parametrize("ambient", [None, "inherited", "declared"])
def test_a_declared_credential_the_launcher_clears_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ambient: str | None
) -> None:
    """Merged env cannot distinguish a declaration equal to an inherited value."""
    called: list[Any] = []
    monkeypatch.setattr(
        "kiro_crew.sandbox_spawn.sandboxed_spawn_argv", lambda *a, **k: called.append(a)
    )
    if ambient is None:
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    else:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", ambient)
    env = {"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": ambient or "declared"}
    plan = project_scope.ProjectBinding(str(tmp_path), "standard")
    with pytest.raises(project_scope.ProjectScopeUnavailable, match="AWS_SECRET_ACCESS_KEY") as exc:
        backend_mod._project_sandbox_argv(
            ["srv"], env, plan, None, ("AWS_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
        )
    verdict = _classify_rejection(exc.value, exclusive=False)
    assert verdict is not None and verdict.fallback is True
    assert called == []


def test_an_inherited_credential_is_dropped_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Daemon env kept for a messaging channel must not push every server per-chat."""
    from kiro_crew.sandbox_spawn import scrub_env

    def fake(argv: list[str], _mode: str, **kw: Any) -> tuple[list[str], dict, None]:
        kw["forward_ssh_auth_sock_out"].append(False)
        return argv, scrub_env(kw["env"]), None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", fake)
    env = {"PATH": "/bin", "SLACK_BOT_TOKEN": "xoxb", "KIROCREW_OWNER_ID": "U1", "A": "1"}
    plan = project_scope.ProjectBinding(str(tmp_path), "standard")
    _argv, out, _ = backend_mod._project_sandbox_argv(["srv"], env, plan, None, ("A",))
    assert out["A"] == "1"
    assert "SLACK_BOT_TOKEN" not in out and "KIROCREW_OWNER_ID" not in out


@pytest.mark.asyncio
async def test_spawn_backend_judges_declared_and_secret_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_argv(_argv: Any, _env: Any, _plan: Any, _tmp: Any, declared: tuple[str, ...]) -> Any:
        seen.append(declared)
        raise project_scope.ProjectScopeUnavailable("stop")

    monkeypatch.setattr(backend_mod, "_project_sandbox_argv", fake_argv)
    monkeypatch.setattr(
        backend_mod, "allocate_backend_tmp", MagicMock(side_effect=OSError("no temp"))
    )
    plan = project_scope.ProjectBinding(str(tmp_path), "standard")
    with pytest.raises(project_scope.ProjectScopeUnavailable):
        await backend_mod.spawn_backend(
            _key(),
            "srv",
            [],
            {"PATH": "/bin"},
            str(tmp_path),
            secret_env_keys=("TOKEN",),
            sandbox=plan,
            declared_env_keys=("AWS_SECRET_ACCESS_KEY",),
        )
    assert seen == [("AWS_SECRET_ACCESS_KEY", "TOKEN")]


@pytest.mark.asyncio
async def test_a_project_backend_records_no_roots_hazard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _make_backend()
    backend.pool_key = replace(backend.pool_key, work_dir=str(tmp_path), sandbox_hash="h")
    recorded: list[str] = []
    monkeypatch.setattr(
        type(backend), "_record_hazard", lambda _s, code, **_k: recorded.append(code)
    )
    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": 8, "method": "roots/list"}))
    assert recorded == []


# ── which sessions count as attested ────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_owners_current_claim_marks_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = {"type": "claim", "pid": 777, "stub_session_token": "tok", "pid_start_id": "s1"}
    _sender(monkeypatch, OWNER + 1)
    await project_scope.note_owner_claim(object(), frame, "B")
    assert not project_scope.owner_claimed("tok", "B", 777, "s1")
    _sender(monkeypatch, OWNER)
    await project_scope.note_owner_claim(object(), frame, "B")
    assert project_scope.owner_claimed("tok", "B", 777, "s1")
    assert not project_scope.owner_claimed("tok", "A", 777, "s1")
    # A token bound to another runtime is outside the owner's claim.
    assert not project_scope.owner_claimed("tok", "B", 888, "s9")
    # A later claim from anyone else revokes the owner's record.
    _sender(monkeypatch, OWNER + 1)
    await project_scope.note_owner_claim(object(), {**frame, "pid": 888}, "B")
    assert not project_scope.owner_claimed("tok", "B", 777, "s1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signed_key", "project_scoped", "attested"),
    [("B", True, True), ("", True, False), ("C", True, False), ("B", False, False)],
)
async def test_a_peer_walk_attests_only_when_the_signed_walk_agrees(
    monkeypatch: pytest.MonkeyPatch, signed_key: str, project_scoped: bool, attested: bool
) -> None:
    """The lenient walk reads pid mappings a local process can plant, so a
    project-scoped stub is attested only when the HMAC-checked walk names the
    same session."""
    from kiro_crew.mcp_gateway import socketsec
    from kiro_crew.mcp_gateway.daemon.identity import (
        _conn_index_discard,
        _resolve_register_identity,
    )

    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: 4321)
    monkeypatch.setattr(socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("B", [4321]))
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: None)
    monkeypatch.setattr(
        gw,
        "_resolve_signed_peer_identity",
        lambda _pid: (signed_key, [4321]),
    )
    register = {**asdict(_key()), "stub_uuid": "u-1"}
    if project_scoped:
        register["project_scoped"] = True
    conn = await _resolve_register_identity(register, object(), "u-1", _key())
    try:
        assert conn.caller is not None and conn.caller.session_key == "B"
        assert conn.session_attested is attested
    finally:
        _conn_index_discard(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner_pid_claimed", "attested"), [(777, True), (888, False)])
async def test_a_token_attests_only_under_the_owners_claim(
    monkeypatch: pytest.MonkeyPatch, owner_pid_claimed: int, attested: bool
) -> None:
    """A same-user process that rebinds the token to its own runtime keeps the
    caller identity the token gives, but not the right to pick a project backend."""
    from kiro_crew.mcp_gateway import socketsec
    from kiro_crew.mcp_gateway.daemon import identity

    monkeypatch.setattr(identity, "_TOKEN_BINDINGS", type(identity._TOKEN_BINDINGS)())
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: 4321)
    monkeypatch.setattr(socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", [4321, 777]))
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: None)
    _sender(monkeypatch, OWNER)
    await project_scope.note_owner_claim(
        object(), {"pid": owner_pid_claimed, "stub_session_token": "tok"}, "B"
    )
    identity._bind_token("tok", CallerContext(session_key="B", session_type="t"), 777, None)
    register = {**asdict(_key()), "stub_uuid": "u-2", "stub_session_token": "tok"}
    conn = await identity._resolve_register_identity(register, object(), "u-2", _key())
    try:
        assert conn.caller is not None and conn.caller.session_key == "B"
        assert conn.session_attested is attested
    finally:
        identity._conn_index_discard(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"sandbox_mode": "off"},
        {"root": "relative"},
        {"root": "/missing-project-root-for-test"},
        {"hidden_dirs": ["relative"]},
        {"session_key": ""},
    ],
)
async def test_every_refused_record_is_audited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: dict
) -> None:
    sink = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", lambda: sink)
    monkeypatch.setattr(project_scope.os.path, "isdir", lambda path: path == str(tmp_path))
    reply = await _record(monkeypatch, "s", tmp_path, **extra)
    assert reply["type"] == "session-project-rejected"
    (call,) = sink.log_api_access.call_args_list
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["operation"] == "mcp-gateway.session_project"
    assert call.kwargs["error"] == reply["reason"]


@pytest.mark.asyncio
async def test_a_claim_to_another_session_drops_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.mcp_gateway.daemon import identity
    from kiro_crew.mcp_gateway.daemon.control import _apply_claim

    await _record(monkeypatch, "A", tmp_path)
    await _record(monkeypatch, "B", tmp_path)
    pool = BackendPool(4)
    conn = identity._StubConn(
        "u-3", [4321], "lbl", CallerContext(session_key="A", session_type="t"), {}, "", ""
    )
    conn.session_attested = True
    identity._conn_index_add(conn)
    try:
        key = await project_scope.project_scoped_key(pool, _key(), conn)
        backend = SimpleNamespace(pool_key=key)
        await _apply_claim({"type": "claim", "pid": 4321, "caller": {"session_key": "A"}})
        assert conn.session_attested is True
        await _apply_claim({"type": "claim", "pid": 4321, "caller": {"session_key": "B"}})
        assert conn.caller.session_key == "B" and conn.session_attested is False
        assert not project_scope.backend_serves_project(backend, conn)
        with pytest.raises(project_scope.ProjectScopeUnavailable):
            await project_scope.project_scoped_key(pool, _key(), conn)
    finally:
        identity._conn_index_discard(conn)


@pytest.mark.asyncio
async def test_shared_guard_resets_all_project_registries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from conftest_guards import SUITE
    from conftest_resets_mcp import _fresh_mcp_project_bindings

    assert any(_fresh_mcp_project_bindings in segment for segment in SUITE.sealed_segments())

    async def populate() -> None:
        await _record(monkeypatch, "s", tmp_path)
        await project_scope.note_owner_claim(
            object(), {"pid": 777, "stub_session_token": "tok"}, "s"
        )
        assert project_scope._PROJECT_BINDINGS
        assert project_scope._PROJECT_PLANS
        assert project_scope._OWNER_CLAIMED_TOKENS

    def assert_empty() -> None:
        assert not project_scope._PROJECT_BINDINGS
        assert not project_scope._PROJECT_PLANS
        assert not project_scope._OWNER_CLAIMED_TOKENS

    await populate()
    guard = _fresh_mcp_project_bindings()
    try:
        next(guard)
        assert_empty()
        await populate()
    finally:
        guard.close()
    assert_empty()
