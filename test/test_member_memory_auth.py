"""Private APIs need an actual process/session proof, never a shared local secret."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import member_proof as _member_proof
from member_memory_helpers import request

from kiro_crew import member_memory_auth as auth
from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import _shared, memory, memory_member
from kiro_crew.mcp_gateway import socketsec

env = _member_env
member_proof = _member_proof


def _proof_key_path(env) -> Path:
    return env.home / "memory_stores" / ".member-api-key"


def test_proof_key_stages_owner_only_durable_bytes_before_no_replace_publish(env, monkeypatch):
    path = _proof_key_path(env)
    real_atomic_write = auth.atomic_write
    real_fsync_dir = auth.fsync_dir
    writes = []
    synced = []

    def write(staged, content, **kwargs):
        writes.append((Path(staged), content, kwargs))
        return real_atomic_write(staged, content, **kwargs)

    def sync(directory):
        synced.append(Path(directory))
        return real_fsync_dir(directory)

    monkeypatch.setattr(auth, "atomic_write", write)
    monkeypatch.setattr(auth, "fsync_dir", sync)

    key = auth._proof_key(create=True)

    assert key is not None and len(key) == 32
    assert path.read_bytes() == key
    assert len(writes) == 1
    staged, content, kwargs = writes[0]
    assert staged.parent == path.parent
    assert staged.name.startswith("..member-api-key.") and staged.name.endswith(".tmp")
    assert content == key
    assert kwargs == {"fsync": True, "restrict_to_owner": True}
    assert synced == [path.parent]
    assert not staged.exists()


def test_interrupted_proof_key_stage_never_creates_the_final_name(env, monkeypatch):
    path = _proof_key_path(env)
    real_atomic_write = auth.atomic_write
    staged_paths = []

    def interrupt(staged, _content, **_kwargs):
        staged = Path(staged)
        staged_paths.append(staged)
        staged.write_bytes(b"partial")
        raise OSError("interrupted staged key")

    monkeypatch.setattr(auth, "atomic_write", interrupt)
    with pytest.raises(OSError, match="interrupted staged key"):
        auth._proof_key(create=True)

    assert not path.exists()
    assert staged_paths and not staged_paths[0].exists()

    monkeypatch.setattr(auth, "atomic_write", real_atomic_write)
    key = auth._proof_key(create=True)
    assert key is not None and path.read_bytes() == key


def test_interrupted_proof_key_publish_leaves_no_partial_final(env, monkeypatch):
    path = _proof_key_path(env)
    real_link = auth.os.link
    staged_paths = []

    def interrupt(staged, destination):
        assert Path(destination) == path
        staged_paths.append(Path(staged))
        assert Path(staged).read_bytes()
        raise OSError("interrupted before key publication")

    monkeypatch.setattr(auth.os, "link", interrupt)
    with pytest.raises(OSError, match="interrupted before key publication"):
        auth._proof_key(create=True)

    assert not path.exists()
    assert staged_paths and not staged_paths[0].exists()

    monkeypatch.setattr(auth.os, "link", real_link)
    key = auth._proof_key(create=True)
    assert key is not None and path.read_bytes() == key


def test_competing_proof_key_creator_preserves_and_adopts_the_first_winner(env, monkeypatch):
    path = _proof_key_path(env)
    real_link = auth.os.link
    winner = []

    def publish_competitor_first(staged, destination):
        monkeypatch.setattr(auth.os, "link", real_link)
        winner.append(auth._proof_key(create=True))
        return real_link(staged, destination)

    monkeypatch.setattr(auth.os, "link", publish_competitor_first)
    adopted = auth._proof_key(create=True)

    assert winner[0] is not None
    assert adopted == winner[0] == path.read_bytes()
    assert auth._proof_key(create=True) == winner[0]


def test_valid_proof_key_identity_is_reused_without_publication(env, monkeypatch):
    path = _proof_key_path(env)
    key = b"k" * 32
    path.write_bytes(key)
    publish = mock.Mock(side_effect=AssertionError("valid keys are never republished"))
    monkeypatch.setattr(auth.os, "link", publish)

    assert auth._proof_key(create=True) == key
    assert auth._proof_key(create=False) == key
    assert path.read_bytes() == key
    publish.assert_not_called()


@pytest.mark.parametrize("corrupt", [b"", b"x" * 31, b"x" * 33])
def test_corrupt_committed_proof_key_is_never_rotated(env, monkeypatch, corrupt):
    path = _proof_key_path(env)
    path.write_bytes(corrupt)
    before = path.stat()
    publish = mock.Mock(side_effect=AssertionError("corrupt keys must fail closed"))
    monkeypatch.setattr(auth.os, "link", publish)

    assert auth._proof_key(create=True) is None
    assert auth._proof_key(create=False) is None
    assert path.read_bytes() == corrupt
    assert path.stat().st_ino == before.st_ino
    publish.assert_not_called()


def test_interrupted_private_binding_write_leaves_no_authoritative_record(env, monkeypatch):
    key = "dashboard:interrupted-before-claim"
    real_fsync = auth.os.fsync
    monkeypatch.setattr(auth.os, "fsync", mock.Mock(side_effect=OSError("interrupted")))

    with pytest.raises(OSError, match="interrupted"):
        auth.bind_private_session_store(key, "member-alice")

    assert auth.read_private_session_store(key) is None
    assert not auth._session_binding_path(key).parent.exists()

    monkeypatch.setattr(auth.os, "fsync", real_fsync)
    auth.bind_private_session_store(key, "member-alice")
    assert auth.read_private_session_store(key) == "member-alice"


def test_complete_staging_directory_is_inert_when_final_publication_is_interrupted(
    env, monkeypatch
):
    key = "dashboard:interrupted-before-directory"
    final_parent = auth._session_binding_path(key).parent
    staged_copy = final_parent.parent / "captured-inert-stage"
    real_publish = auth._publish_private_binding_dir

    def interrupt_publish(staging, destination):
        assert Path(destination) == final_parent
        shutil.copytree(staging, staged_copy)
        raise OSError("interrupted before final directory")

    monkeypatch.setattr(auth, "_publish_private_binding_dir", interrupt_publish)
    with pytest.raises(OSError, match="interrupted before final directory"):
        auth.bind_private_session_store(key, "member-alice")

    assert not final_parent.exists()
    assert (staged_copy / "memory.json").is_file()
    assert auth.read_private_session_store(key) is None

    monkeypatch.setattr(auth, "_publish_private_binding_dir", real_publish)
    auth.bind_private_session_store(key, "member-alice")
    assert auth.read_private_session_store(key) == "member-alice"
    assert auth._session_binding_path(key).is_file()


def test_no_replace_publication_preserves_competing_first_owner(env, monkeypatch):
    key = "dashboard:competing-first-owner"
    real_publish = auth._publish_private_binding_dir

    def publish_competitor_first(staging, destination):
        monkeypatch.setattr(auth, "_publish_private_binding_dir", real_publish)
        auth.bind_private_session_store(key, "member-bob")
        return real_publish(staging, destination)

    monkeypatch.setattr(auth, "_publish_private_binding_dir", publish_competitor_first)
    with pytest.raises(ValueError, match="already bound"):
        auth.bind_private_session_store(key, "member-alice")

    assert auth.read_private_session_store(key) == "member-bob"


def test_unsupported_no_replace_publish_refuses_without_rename_fallback(monkeypatch, tmp_path):
    source = tmp_path / "stage"
    destination = tmp_path / "final"
    rename = mock.Mock(side_effect=NotImplementedError("unsupported"))
    fallback = mock.Mock()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "rename_noreplace", rename)
    monkeypatch.setattr(auth.os, "open", mock.Mock(return_value=41))
    monkeypatch.setattr(auth.os, "close", mock.Mock())
    monkeypatch.setattr(auth.os, "rename", fallback)

    with pytest.raises(NotImplementedError, match="unsupported"):
        auth._publish_private_binding_dir(source, destination)

    fallback.assert_not_called()


def test_deleted_committed_binding_still_fails_closed(env):
    key = "dashboard:deleted-committed-binding"
    auth.bind_private_session_store(key, "member-alice")
    auth._session_binding_path(key).unlink()

    with pytest.raises(ValueError, match="missing or unreadable"):
        auth.read_private_session_store(key)
    with pytest.raises(ValueError, match="missing or unreadable"):
        auth.bind_private_session_store(key, "member-bob")


@pytest.mark.parametrize(
    "key",
    [
        "dashboard:user-session",
        "memory-consolidation:member-alice:not-a-uuid",
        "memory-consolidation:member-bob:0123456789abcdef0123456789abcdef",
    ],
)
def test_transient_cleanup_refuses_non_generated_or_wrong_store_keys(env, key):
    original = "dashboard:user-session"
    auth.bind_private_session_store(original, "member-alice")
    with pytest.raises(ValueError, match="consolidation session key|does not match"):
        auth.retire_memory_consolidation_binding(key, "member-alice")
    assert auth.read_private_session_store(original) == "member-alice"


def test_transient_cleanup_preserves_a_mismatched_committed_binding(env):
    key = "memory-consolidation:member-alice:0123456789abcdef0123456789abcdef"
    auth.bind_private_session_store(key, "member-bob")

    with pytest.raises(ValueError, match="already bound"):
        auth.retire_memory_consolidation_binding(key, "member-alice")
    assert auth.read_private_session_store(key) == "member-bob"


@pytest.mark.asyncio
async def test_real_middleware_accepts_mcp_recall_secret_but_still_checks_member_proof(
    env, member_proof
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="test-mcp-secret",
            )
        ]
    )
    app["state"] = env.state
    env.state._slots["bob"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    env.bind_session("dashboard:bob", "member-bob")
    app.router.add_get("/api/memory/recall", memory_member.api_memory_recall)
    async with TestClient(TestServer(app)) as client:
        headers = {
            "X-Internal-Secret": "test-mcp-secret",
            "X-Session-Key": "dashboard:alice",
            auth.PROOF_HEADER: member_proof,
        }
        response = await client.get("/api/memory/recall?q=database", headers=headers)
        assert response.status == 200, await response.text()
        headers["X-Session-Key"] = "dashboard:bob"
        response = await client.get("/api/memory/recall?q=database", headers=headers)
        assert response.status == 403, await response.text()
        assert (await response.json())["code"] == "member_session_unverified"


@pytest.mark.parametrize(
    "platform,requested,effective,backend,kiro,delegates,expected",
    [
        ("win32", "standard", "standard", "namespace", True, False, False),
        ("linux", "off", "off", "namespace", True, False, False),
        ("linux", "standard", "standard", "none", True, False, False),
        ("linux", "standard", "standard", "namespace", True, False, True),
        ("linux", "off", "strict", "namespace", True, False, True),
        ("darwin", "standard", "standard", "sandbox-exec", True, True, False),
        ("darwin", "standard", "standard", "sandbox-exec", True, False, True),
        ("darwin", "standard", "standard", "sandbox-exec", False, True, True),
    ],
)
def test_private_execution_requires_the_enforced_outer_sandbox(
    monkeypatch, platform, requested, effective, backend, kiro, delegates, expected
):
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    config = SimpleNamespace(
        agent=SimpleNamespace(sandbox=requested, acp_backend="kas", member_acp_backend="kas")
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    clamp = mock.Mock(return_value=effective)
    detect = mock.Mock(return_value=backend)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", clamp)
    monkeypatch.setattr(sandbox, "detect_backend", detect)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: delegates)
    monkeypatch.setattr(
        "kiro_crew.acp_backends.ACP_BACKENDS_INTERNAL_SANDBOX", {"kas"} if kiro else set()
    )
    assert auth.private_memory_execution_supported() is expected
    if platform == "win32":
        clamp.assert_not_called()
        detect.assert_not_called()
    else:
        clamp.assert_called_once_with(requested)
        if expected:
            detect.assert_called_once_with(config_mode=effective)


@pytest.mark.parametrize("default,member,expected", [("claude", "", False), ("", "kas", True)])
def test_macos_member_guard_uses_the_members_actual_backend(monkeypatch, default, member, expected):
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    config = SimpleNamespace(
        agent=SimpleNamespace(sandbox="standard", acp_backend=default, member_acp_backend=member)
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kw: "sandbox-exec")
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
    assert auth.private_memory_execution_supported(session_key="dashboard:member-alice") is expected


@pytest.mark.parametrize("platform,mechanism", [("linux", "namespace"), ("darwin", "sandbox-exec")])
@pytest.mark.parametrize("backend", ["", "claude", "kas", "codex", "future-harness"])
@pytest.mark.parametrize("session_key", ["dashboard:member-alice", "subagent:private-task"])
def test_private_execution_requires_direct_mcp_tools(
    monkeypatch, platform, mechanism, backend, session_key
):
    from kiro_crew import sandbox
    from kiro_crew.agent_sdk import backends
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import UnknownMemoryStore

    if backend == "future-harness":
        monkeypatch.setattr(backends, "_baseline", set(backends._baseline))
        monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
        monkeypatch.setattr(backends, "ACP_BACKENDS_KNOWN", backends.ACP_BACKENDS_KNOWN | {backend})
        backends.register_selectable_backend(backend)
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform=platform))
    config = SimpleNamespace(
        agent=SimpleNamespace(
            sandbox="standard",
            acp_backend=backend,
            member_acp_backend=backend,
        )
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda mode: mode)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kw: mechanism)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
    if backend in {"codex", "future-harness"}:
        with pytest.raises(
            UnknownMemoryStore, match="cannot run private member MCP tools.*Use Kiro"
        ):
            auth.require_private_memory_execution(session_key=session_key)
    else:
        auth.require_private_memory_execution(session_key=session_key)


def test_selectable_backend_does_not_inherit_private_mcp_authority(monkeypatch):
    from kiro_crew.agent_sdk import backends
    from kiro_crew.memory_stores import UnknownMemoryStore

    monkeypatch.setattr(backends, "_baseline", set(backends._baseline))
    monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
    monkeypatch.setattr(
        backends, "ACP_BACKENDS_KNOWN", backends.ACP_BACKENDS_KNOWN | {"future-harness"}
    )
    backends.register_selectable_backend("future-harness")
    assert "future-harness" in backends.selectable_backends()
    with pytest.raises(UnknownMemoryStore, match="Global Memory V1 was not used"):
        auth.require_private_memory_mcp_backend("future-harness")
    for backend in ("", "claude", "kas"):
        auth.require_private_memory_mcp_backend(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("weak_marker", [False, True])
async def test_shared_secret_and_session_header_cannot_read_private_memory(env, weak_marker):
    req = request(env, query={"q": "database"}, internal=True)
    req["peer_verified"] = weak_marker
    response = await memory_member.api_memory_recall(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_valid_proof_cannot_be_replayed_as_another_member(env, member_proof):
    env.bind_session("dashboard:bob", "member-bob")
    req = request(env, session="dashboard:bob", internal=True, proof=member_proof)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "" and refusal.status == 403
    assert json.loads(refusal.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_same_member_proof_authorizes_the_lesson_write_target(env, member_proof):
    req = request(env, internal=True, proof=member_proof)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "member-alice" and refusal is None


@pytest.mark.asyncio
async def test_global_v1_internal_lessons_keep_existing_authorization(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: os.getpid())
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    req = request(env, session="dashboard:legacy", internal=True)
    name, refusal = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    assert name == "" and refusal is None


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["", "dashboard:ui", "dashboard:legacy"])
@pytest.mark.parametrize("forward_proof", [False, True])
@pytest.mark.parametrize("route", ["lessons", "global_list", "consolidate"])
async def test_member_cannot_downgrade_to_global_or_omit_its_identity(
    env, member_proof, monkeypatch, declared, forward_proof, route
):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: os.getpid())
    req = request(
        env,
        session=declared,
        internal=True,
        proof=member_proof if forward_proof else "",
        body={"key": "dashboard:legacy"} if route == "consolidate" else None,
    )
    if route == "lessons":
        _, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.create")
    elif route == "global_list":
        _, response = await _shared.resolve_requested_memory_store(req, env.state, "memory.list")
    else:
        env.state.consolidator = mock.Mock()
        response = await memory.api_memory_consolidate(req)
        env.state.consolidator._consolidate.assert_not_called()
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_correct_member_header_cannot_select_implicit_global_list(env, member_proof):
    req = request(env, internal=True, proof=member_proof)
    _, response = await _shared.resolve_requested_memory_store(req, env.state, "memory.list")
    assert response.status == 403


@pytest.mark.asyncio
async def test_unverifiable_peer_cannot_use_global_when_private_members_exist(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: None)
    req = request(env, session="", internal=True)
    _, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.list")
    assert response.status == 403


@pytest.mark.asyncio
async def test_pure_v1_installation_keeps_legacy_internal_access(env, monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: None)
    monkeypatch.setattr(auth, "private_memory_boundaries_active", lambda: False)
    req = request(env, session="", internal=True)
    name, response = await _shared.resolve_lesson_memory_store(req, env.state, "lessons.list")
    assert name == "" and response is None


@pytest.mark.asyncio
async def test_member_cannot_consolidate_a_different_members_session(env, member_proof):
    env.bind_session("dashboard:bob", "member-bob")
    env.state.consolidator = mock.Mock()
    req = request(env, body={"key": "dashboard:bob"}, internal=True, proof=member_proof)
    response = await memory.api_memory_consolidate(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"
    env.state.consolidator._consolidate.assert_not_called()


def test_proof_is_invalid_after_process_rekey(env, member_proof):
    assert auth.verify_member_session_proof(member_proof, "dashboard:alice")
    auth.publish_member_session_pid(os.getpid(), "dashboard:bob", memory_store="member-bob")
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_global_rekey_does_not_publish_a_private_identity(env, member_proof, monkeypatch):
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    auth.publish_member_session_pid(os.getpid(), "dashboard:first-global", memory_store="")
    assert auth.protected_member_session_for_pid(os.getpid()) is None
    auth.publish_member_session_pid(os.getpid(), "dashboard:second-global", memory_store="")
    assert auth.protected_member_session_for_pid(os.getpid()) is None
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["private", "corrupt", "unverifiable", "host"])
async def test_member_cannot_promote_local_secret_to_owner_token(
    env, member_proof, monkeypatch, identity
):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import core

    req = request(env)
    req.app["local_secret"] = "test-local-secret"
    req = make_mocked_request(
        "GET", "/api/token/local", app=req.app, headers={"X-Local-Secret": "test-local-secret"}
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda value: True)
    monkeypatch.setattr(
        auth,
        "_request_peer_pid",
        lambda request: None if identity == "unverifiable" else os.getpid(),
    )
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    if identity == "host":
        auth._binding_path(os.getpid(), env.home).unlink()
    elif identity == "corrupt":
        auth._binding_path(os.getpid(), env.home).write_text("{broken", encoding="utf-8")
    issue = mock.Mock(return_value="owner-token")
    monkeypatch.setattr(core, "generate_token", issue)
    response = await core.api_token_local(req)
    assert response.status == (200 if identity == "host" else 403)
    if identity != "host":
        issue.assert_not_called()
        assert json.loads(response.text)["code"] == "member_owner_token_refused"


@pytest.mark.parametrize("strict", [False, True])
def test_private_mcp_uses_protected_ancestry_without_legacy_pid_files(
    env, member_proof, monkeypatch, strict
):
    from kiro_crew import mcp_core

    monkeypatch.setattr(mcp_core, "current_caller", lambda: None)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:forged-global")
    resolve = mcp_core._resolve_session_key_strict if strict else mcp_core._resolve_session_key
    assert resolve() == "dashboard:alice"


def test_proof_is_invalid_after_process_incarnation_changes(env, member_proof, monkeypatch):
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "recycled")
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_recycled_private_pid_record_does_not_poison_global_ancestry(env, monkeypatch):
    peer, ancestor = 12345, 12346
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "live")
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: ancestor if pid == peer else 1)
    monkeypatch.setattr(platform_compat, "process_namespaces_match", lambda *args: True)
    monkeypatch.setattr(platform_compat, "process_can_read_under_sandbox", lambda *args: True)
    auth.publish_member_session_pid(peer, "dashboard:old-member", home=env.home, memory_store="old")
    path = auth._binding_path(peer, env.home)
    record = json.loads(path.read_text())
    record["process_start"] = "dead-incarnation"
    path.write_text(json.dumps(record))
    auth.publish_member_session_pid(ancestor, "dashboard:global", home=env.home, memory_store="")
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("dashboard:global", "")
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("", "")


@pytest.mark.skipif(auth.sys.platform != "linux", reason="Linux namespace provenance")
def test_global_sandbox_requires_exact_live_published_namespace_pair(env, monkeypatch):
    from pathlib import Path

    peer, launcher = 12345, 12346
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "live")
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: launcher if pid == peer else 1)
    monkeypatch.setattr(platform_compat, "process_namespaces_match", lambda *args: False)
    auth.publish_member_session_pid(launcher, "dashboard:global", home=env.home, memory_store="")
    path = auth._binding_path(launcher, env.home).with_suffix(".namespace.json")
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("", "")
    real_stat = Path.stat

    def namespace_stat(target, *args, **kwargs):
        if str(target) == f"/proc/{peer}/ns/user":
            return SimpleNamespace(st_dev=1, st_ino=2)
        if str(target) == f"/proc/{peer}/ns/mnt":
            return SimpleNamespace(st_dev=1, st_ino=3)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", namespace_stat)
    row = {"process_start": "live", "namespaces": [[1, 2], [1, 3]], "private_memory": False}
    path.write_text(json.dumps(row))
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("dashboard:global", "")
    row["namespaces"][1] = [1, 99]
    path.write_text(json.dumps(row))
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("", "")
    row["namespaces"][1] = [1, 3]
    row["process_start"] = "earlier-launcher"
    path.write_text(json.dumps(row))
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("", "")
    row.update(process_start="live", private_memory=True)
    path.write_text(json.dumps(row))
    assert auth._protected_member_binding_for_pid(peer, home=env.home) == ("", "")


def test_process_proof_survives_a_long_tool_but_not_tampering(env, member_proof, monkeypatch):
    assert not auth.verify_member_session_proof(member_proof + "0", "dashboard:alice")
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 24 * 60 * 60)
    assert auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_legacy_proof_keeps_its_original_expiry(env, member_proof, monkeypatch):
    import base64
    import hashlib
    import hmac

    now = int(auth.time.time())
    body = {
        "v": 1,
        "s": "dashboard:alice",
        "p": os.getpid(),
        "i": f"test-start-{os.getpid()}",
        "e": now + 60,
    }
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    signing_key = auth._proof_key(create=False)
    assert signing_key is not None
    proof = payload + "." + hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
    monkeypatch.setattr(auth.time, "time", lambda: now + 1)
    assert auth.verify_member_session_proof(proof, "dashboard:alice")
    monkeypatch.setattr(auth.time, "time", lambda: now + 60)
    assert not auth.verify_member_session_proof(proof, "dashboard:alice")


@pytest.mark.parametrize(
    "change", ["dead", "store", "session_record", "isolation", "unknown_isolation"]
)
def test_process_proof_requires_its_original_live_authority(env, member_proof, monkeypatch, change):
    assert auth.verify_member_session_proof(member_proof, "dashboard:alice")
    if change == "dead":
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: None)
    elif change == "store":
        # The session name and process incarnation survive, but the protected
        # runtime now names another member. The original proof cannot follow it.
        auth.publish_member_session_pid(os.getpid(), "dashboard:alice", memory_store="member-bob")
    elif change == "session_record":
        auth._session_binding_path("dashboard:alice").unlink()
    elif change == "isolation":
        monkeypatch.setattr(auth, "_proof_process_scope", lambda _pid: ["different-isolation"])
    else:
        monkeypatch.setattr(auth, "_proof_process_scope", lambda _pid: None)
    assert not auth.verify_member_session_proof(member_proof, "dashboard:alice")


def test_lifetime_proof_cannot_be_issued_for_a_changed_durable_store(env, member_proof):
    auth.publish_member_session_pid(os.getpid(), "dashboard:alice", memory_store="member-bob")
    assert auth.issue_member_session_proof("dashboard:alice", os.getpid()) == ""


@pytest.mark.parametrize("rekey_session", ["dashboard:alice", "dashboard:bob"])
def test_verified_request_cannot_pick_up_a_rekeyed_store(env, member_proof, rekey_session):
    req = request(env, internal=True, proof=member_proof)
    assert auth.memory_request_identity(req) == ("dashboard:alice", True)
    assert auth.memory_request_bound_store(req) == "member-alice"
    # Model the independent await between authenticating the request and
    # resolving its target. The prior proof must not acquire the new store.
    auth.publish_member_session_pid(os.getpid(), rekey_session, memory_store="member-bob")
    assert auth.memory_request_bound_store(req) is None


def test_verified_request_refuses_an_unreadable_binding_at_target_resolution(env, member_proof):
    req = request(env, internal=True, proof=member_proof)
    assert auth.memory_request_identity(req) == ("dashboard:alice", True)
    auth._binding_path(os.getpid(), env.home).write_text("{broken", encoding="utf-8")
    assert auth.memory_request_bound_store(req) is None


def test_linux_proof_scope_captures_both_kernel_namespaces(monkeypatch):
    original_stat = Path.stat
    identities = {"user": (7, 11), "mnt": (7, 13)}

    def namespace_stat(path, *args, **kwargs):
        if str(path).replace("\\", "/").startswith("/proc/456/ns/"):
            device, inode = identities[path.name]
            return SimpleNamespace(st_dev=device, st_ino=inode)
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(auth.sys, "platform", "linux")
        patcher.setattr(Path, "stat", namespace_stat)
        assert auth._proof_process_scope(456) == ["linux", [7, 11], [7, 13]]
        identities["mnt"] = (7, 17)
        assert auth._proof_process_scope(456) == ["linux", [7, 11], [7, 17]]


@pytest.mark.parametrize("sandboxed", [True, False, None])
def test_macos_proof_scope_requires_a_known_inherited_sandbox_state(monkeypatch, sandboxed):
    with monkeypatch.context() as patcher:
        patcher.setattr(auth.sys, "platform", "darwin")
        patcher.setattr(platform_compat, "process_is_sandboxed", lambda _pid: sandboxed)
        assert auth._proof_process_scope(456) == (
            None if sandboxed is None else ["darwin", sandboxed]
        )


def test_writable_legacy_pid_mapping_does_not_grant_a_member(env, monkeypatch):
    pid = os.getpid()
    (env.home / f"session_pid_{pid}.txt").write_text("dashboard:bob", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 1)
    assert auth.verified_member_session_for_pid(pid) == ""
    assert auth.issue_member_session_proof("dashboard:bob", pid) == ""


def test_corrupt_nearest_identity_cannot_borrow_an_ancestor(env, member_proof, monkeypatch):
    pid = os.getpid()
    path = auth._binding_path(pid, env.home)
    path.write_text("{corrupt", encoding="utf-8")
    parent_probe = mock.Mock(return_value=pid + 1)
    monkeypatch.setattr(platform_compat, "get_ppid", parent_probe)
    assert auth.verified_member_session_for_pid(pid) == ""
    parent_probe.assert_not_called()


@pytest.mark.parametrize("declared", ["dashboard:alice", "dashboard:bob"])
def test_unix_peer_uses_protected_real_process_identity(env, member_proof, monkeypatch, declared):
    req = request(env, session=declared, internal=True)
    sock = object()
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: sock)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: os.getpid())
    assert auth.private_memory_request_verified(req) is (declared == "dashboard:alice")


@pytest.mark.parametrize("peer_pid", [None, 0])
def test_unverifiable_unix_peer_does_not_use_header(env, member_proof, monkeypatch, peer_pid):
    req = request(env, internal=True)
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: object())
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: peer_pid)
    assert not auth.private_memory_request_verified(req)


def test_tcp_proof_uses_exact_kernel_endpoints_and_protected_identity(
    env, member_proof, monkeypatch
):
    endpoints = {"sockname": ("127.0.0.1", 9001), "peername": ("127.0.0.1", 42010)}
    req = SimpleNamespace(
        headers={"X-Session-Key": "dashboard:alice"},
        get=lambda name: name == "internal_auth",
        transport=SimpleNamespace(get_extra_info=endpoints.get),
    )
    monkeypatch.setattr("kiro_crew.dashboard.token_auth._unix_request_socket", lambda req: None)
    resolve = mock.Mock(return_value=os.getpid())
    monkeypatch.setattr(platform_compat, "get_tcp_peer_pid", resolve)
    assert auth.private_memory_request_verified(req)
    resolve.assert_called_once_with(endpoints["sockname"], endpoints["peername"])
    endpoints["peername"] = ("203.0.113.1", 42010)
    resolve.reset_mock()
    assert not auth.private_memory_request_verified(req)
    resolve.assert_not_called()
