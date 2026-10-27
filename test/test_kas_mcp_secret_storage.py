"""Remote MCP sign-ins kept across KAS sessions (``agent.kas_persist_mcp_sign_in``).

With the key on, the KAS handshake declares ``_meta.kiro.secretStorage`` and the
engine stores each MCP server's OAuth state through ``_kiro/secret/*`` requests.
These tests pin that the key is off by default and then declares nothing, that
``client`` and ``tokens`` persist in a dedicated vault per authenticated user
that is neither the user Secrets vault nor agent-readable, that a process with
no single bound user persists nothing, that one user's grant is never served to
another's session, that ``verifier`` and ``discovery`` stay in
memory, that only the engine's MCP key namespace is served, and that no stored
value reaches a log or the raw-frame recorder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import pytest

from kiro_crew import mcp_grant_principals as principals
from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.acp import runtime_spawn_launch
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, KAS_CLIENT_CAPABILITIES
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.testing.wait import async_wait_until

_HASH = "ab" * 32
_TOKENS_KEY = f"kiro.mcp.{_HASH}.tokens"
_CLIENT_KEY = f"kiro.mcp.{_HASH}.client"
_VERIFIER_KEY = f"kiro.mcp.{_HASH}.verifier"
_DISCOVERY_KEY = f"kiro.mcp.{_HASH}.discovery"
_TOKEN_VALUE = (
    '{"access_token":"tok-SECRET-4f1c","token_type":"Bearer","refresh_token":"rt-SECRET-9a2e"}'
)


_OWNER_KEY = "chat-owner"


@pytest.fixture(autouse=True)
def _bound_owner_session():
    """Every test starts with one session key bound to the gateway owner."""
    principals.reset_for_tests()
    principals.bind(_OWNER_KEY, principals.OWNER_PRINCIPAL)
    yield
    principals.reset_for_tests()


def _server(
    tmp_path, principal: str = principals.OWNER_PRINCIPAL, key: str = _OWNER_KEY
) -> kss.SecretStorageServer:
    return kss.SecretStorageServer(principal, key, kss.McpGrantStore(tmp_path, principal))


def _vault_dir(root, principal: str = principals.OWNER_PRINCIPAL):
    return root / "kas" / "mcp-grants" / "principals" / principals.principal_dir_name(principal)


# ── the server: what persists, what stays in memory, what is refused ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [_TOKENS_KEY, _CLIENT_KEY])
async def test_client_and_tokens_outlive_the_engine_process(tmp_path, key):
    first = _server(tmp_path)
    assert await first.answer(kss.METHOD_SECRET_GET, {"key": key}) == {}
    assert await first.answer(kss.METHOD_SECRET_STORE, {"key": key, "value": _TOKEN_VALUE}) == {}

    # A new engine process gets a new server; the grant is still there.
    second = _server(tmp_path)
    assert await second.answer(kss.METHOD_SECRET_GET, {"key": key}) == {"value": _TOKEN_VALUE}

    assert await second.answer(kss.METHOD_SECRET_DELETE, {"key": key}) == {}
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": key}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [_VERIFIER_KEY, _DISCOVERY_KEY])
async def test_verifier_and_discovery_stay_in_this_process(tmp_path, key):
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": key, "value": "pkce-verifier"})
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": key}) == {"value": "pkce-verifier"}

    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": key}) == {}
    assert not (_vault_dir(tmp_path) / ".vault" / "secrets.enc").exists()

    await server.answer(kss.METHOD_SECRET_DELETE, {"key": key})
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": key}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"key": "kiro.auth.refreshToken"},
        {"key": f"kiro.mcp.{_HASH}.other"},
        {"key": f"kiro.mcp.{'AB' * 32}.tokens"},
        {"key": f"kiro.mcp.{'ab' * 31}.tokens"},
        {"key": f"kiro.mcp.{_HASH}.tokens\n"},
        {"key": f"x.kiro.mcp.{_HASH}.tokens"},
        {"key": 7},
        {},
        "not-an-object",
    ],
)
@pytest.mark.parametrize("method", sorted(kss.SECRET_METHODS))
async def test_keys_outside_the_mcp_namespace_are_refused(tmp_path, params, method):
    if isinstance(params, dict) and method == kss.METHOD_SECRET_STORE:
        params = {**params, "value": "v"}
    with pytest.raises(kss.SecretStorageRefused):
        await _server(tmp_path).answer(method, params)
    assert not (tmp_path / "kas").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [_TOKENS_KEY, _VERIFIER_KEY])
async def test_an_oversized_or_non_string_value_is_refused(tmp_path, key):
    server = _server(tmp_path)
    too_big = "x" * (kss.MAX_VALUE_CHARS + 1)
    for value in (too_big, None, 5, {"access_token": "t"}):
        with pytest.raises(kss.SecretStorageRefused) as exc:
            await server.answer(kss.METHOD_SECRET_STORE, {"key": key, "value": value})
        assert "x" * 20 not in str(exc.value)
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": key}) == {}
    exact = "x" * kss.MAX_VALUE_CHARS
    await server.answer(kss.METHOD_SECRET_STORE, {"key": key, "value": exact})
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": key}) == {"value": exact}


@pytest.mark.asyncio
async def test_a_stale_process_cannot_delete_a_grant_stored_since(tmp_path):
    """Process A read the grant, B refreshed it, A's refresh fails and A clears."""
    a, b = _server(tmp_path), _server(tmp_path)
    await a.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v1"})
    assert await a.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {"value": "v1"}
    assert await b.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {"value": "v1"}
    await b.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v2"})

    await a.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {
        "value": "v2"
    }

    # B holds the current revision, so its own clear does remove it.
    await b.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


@pytest.mark.asyncio
async def test_a_process_that_read_nothing_cannot_delete_a_grant_stored_since(tmp_path):
    a, b = _server(tmp_path), _server(tmp_path)
    assert await a.answer(kss.METHOD_SECRET_GET, {"key": _CLIENT_KEY}) == {}
    await b.answer(kss.METHOD_SECRET_STORE, {"key": _CLIENT_KEY, "value": "reg"})
    await a.answer(kss.METHOD_SECRET_DELETE, {"key": _CLIENT_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _CLIENT_KEY}) == {
        "value": "reg"
    }


@pytest.mark.asyncio
async def test_a_key_this_process_never_touched_is_deleted(tmp_path):
    await _server(tmp_path).answer(kss.METHOD_SECRET_STORE, {"key": _CLIENT_KEY, "value": "reg"})
    await _server(tmp_path).answer(kss.METHOD_SECRET_DELETE, {"key": _CLIENT_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _CLIENT_KEY}) == {}


@pytest.mark.asyncio
async def test_a_repeat_delete_cannot_erase_a_grant_stored_since(tmp_path):
    a, b = _server(tmp_path), _server(tmp_path)
    await a.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v1"})
    await b.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v2"})
    await a.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    await a.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {
        "value": "v2"
    }


@pytest.mark.asyncio
async def test_an_overlapping_get_cannot_record_an_older_revision_than_a_store(tmp_path):
    """A get and a store on one key, answered in parallel tasks, stay ordered."""
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v1"})
    await asyncio.gather(
        server.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}),
        server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "v2"}),
    )
    # Whatever order the two ran in, the recorded revision is the stored entry's,
    # so this process's own clear removes it.
    await server.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


@pytest.mark.parametrize("method", [[], {}, {"m": 1}, None, 7, ["_kiro/secret/get"]])
def test_a_non_string_method_is_never_a_secret_method(method):
    assert kss.is_secret_method(method) is False


def test_the_three_secret_methods_are_recognised():
    assert all(kss.is_secret_method(m) for m in kss.SECRET_METHODS)
    assert not kss.is_secret_method("_kiro/secret/list")


# ── where the grant lives ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_grant_vault_is_not_the_secrets_vault_and_holds_no_plaintext(tmp_path):
    from kiro_crew.secrets import SecretVault

    await _server(tmp_path).answer(
        kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE}
    )
    store = _vault_dir(tmp_path) / ".vault" / "secrets.enc"
    assert store.is_file()
    # The directory is a digest of the principal, never the subject itself.
    assert "owner" not in str(store.relative_to(tmp_path))
    raw = store.read_text(encoding="utf-8")
    assert "SECRET" not in raw and "access_token" not in raw
    # Neither the user Secrets vault (Settings -> Secrets, cron grants) nor the
    # Kiro sign-in vault lists or holds it.
    assert SecretVault(tmp_path).list_names() == []
    assert SecretVault(tmp_path / "kas").list_names() == []
    assert not (tmp_path / ".vault").exists()
    assert not (tmp_path / "kas" / ".vault").exists()
    # Nor the gateway-wide vault an earlier build wrote.
    assert not (tmp_path / "kas" / "mcp-grants" / ".vault").exists()


@pytest.mark.asyncio
async def test_get_with_revision_names_the_entry_delete_if_revision_matches(tmp_path):
    from kiro_crew.secrets import SecretVault

    vault = SecretVault(tmp_path)
    assert vault.get_with_revision("n") is None
    written = await vault.set_with_revision("n", "v")
    value, revision = vault.get_with_revision("n")
    assert (value.reveal(), revision) == ("v", written)
    await vault.set("n", "v")  # same value, new ciphertext: a new revision
    assert await vault.delete_if_revision("n", revision) is False
    assert vault.get("n").reveal() == "v"


def test_the_grant_vault_is_on_the_agent_floor():
    from kiro_crew.security.paths import is_sensitive_path

    root = _vault_dir(data_home(), "user:telegram-42") / ".vault"
    assert is_sensitive_path(str(root / "secrets.enc"))
    assert is_sensitive_path(str(root / ".vault_key"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
@pytest.mark.asyncio
async def test_a_linked_grant_directory_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "kas").mkdir()
    (tmp_path / "kas" / "mcp-grants").symlink_to(elsewhere)
    with pytest.raises(kss.SecretStorageRefused):
        await _server(tmp_path).answer(
            kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE}
        )
    assert list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_a_vault_failure_is_a_value_free_refusal(tmp_path, monkeypatch, caplog):
    store = kss.McpGrantStore(tmp_path, principals.OWNER_PRINCIPAL)

    async def boom(key, value):
        raise ValueError(f"cannot write {tmp_path} {value}")

    monkeypatch.setattr(store, "store", boom)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(kss.SecretStorageRefused) as exc:
        await kss.SecretStorageServer(principals.OWNER_PRINCIPAL, _OWNER_KEY, store).answer(
            kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE}
        )
    assert str(exc.value) == "MCP grant store error"
    assert "SECRET" not in caplog.text and str(tmp_path) not in caplog.text


# ── the opt-in key and the handshake ──────────────────────────────────────────


def test_the_key_is_off_by_default_and_read_from_config(tmp_path, monkeypatch):
    from kiro_crew.config import loader as L
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    assert KiroCrewConfig.load().agent.kas_persist_mcp_sign_in is False
    assert kss.persistence_enabled() is False
    (tmp_path / "config.json").write_text(json.dumps({"agent": {"kas_persist_mcp_sign_in": True}}))
    assert KiroCrewConfig.load().agent.kas_persist_mcp_sign_in is True
    assert kss.persistence_enabled() is True


@pytest.mark.parametrize(
    "document",
    [
        {"agent": {"kas_persist_mcp_sign_in": "true"}},
        {"agent": {"kas_persist_mcp_sign_in": 1}},
        {"agent": {"kas_persist_mcp_sign_in": None}},
        {"agent": {"kas_persist_mcp_sign_in": [True]}},
        {"agent": {"kas_persist_mcp_sign_in": {"on": True}}},
        {"agent": ["kas_persist_mcp_sign_in"]},
        {"agent": "kas_persist_mcp_sign_in"},
    ],
)
def test_a_non_boolean_setting_reads_as_off(tmp_path, monkeypatch, document):
    from kiro_crew.config import loader as L

    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(document))
    assert kss.persistence_enabled() is False


def test_an_unreadable_config_reads_as_off(tmp_path, monkeypatch):
    from kiro_crew.config import loader as L

    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{ not json")
    assert kss.persistence_enabled() is False
    monkeypatch.setattr(
        L.KiroCrewConfig, "load", classmethod(lambda cls: (_ for _ in ()).throw(OSError()))
    )
    assert kss.persistence_enabled() is False


def test_the_shipped_declaration_never_carries_secret_storage():
    assert "secretStorage" not in KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]
    before = json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True)
    out = kss.with_secret_storage(KAS_CLIENT_CAPABILITIES)
    assert out["_meta"]["kiro"]["secretStorage"] is True
    assert out["_meta"]["kiro"]["openExternalUrl"] is True
    assert json.dumps(KAS_CLIENT_CAPABILITIES, sort_keys=True) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "enabled", "declared"),
    [
        (ACP_BACKEND_KAS, False, False),
        (ACP_BACKEND_KAS, True, True),
        (ACP_BACKEND_KIRO, True, False),
    ],
)
async def test_the_handshake_declares_only_when_asked_on_a_sign_in_host(
    monkeypatch, backend, enabled, declared
):
    monkeypatch.setattr(kss, "persistence_enabled", lambda: enabled)
    runtime = AcpRuntime(acp_backend=backend, mcp_grant_key=_OWNER_KEY)
    runtime._tool_search = None
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    meta = caps.get("_meta", {}).get("kiro", {})
    assert ("secretStorage" in meta) is declared
    assert (runtime._mcp_secret_server is not None) is declared
    assert "secretStorage" not in KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]


@pytest.mark.asyncio
async def test_a_handshake_with_the_key_off_drops_an_earlier_server(monkeypatch):
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS, mcp_grant_key=_OWNER_KEY)
    runtime._tool_search = None
    monkeypatch.setattr(kss, "persistence_enabled", lambda: True)
    await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert runtime._mcp_secret_server is not None
    monkeypatch.setattr(kss, "persistence_enabled", lambda: False)
    await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert runtime._mcp_secret_server is None


# ── end to end through a KAS-shaped stub ──────────────────────────────────────

#: After session/new the stub stores a token grant (unless told to only read),
#: then reads it back, and records what the client answered each time.
_STUB_AGENT = """
import json, os, sys

CAPTURE = os.environ["KIROCREW_TEST_SECRET_CAPTURE"]
MODE = os.environ["KIROCREW_TEST_SECRET_MODE"]
KEY = %(key)r
VALUE = %(value)r
seen = {}

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        seen["clientMeta"] = params.get("clientCapabilities", {}).get("_meta")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-stub-session", "modes": {"currentModeId": "default"}}})
        # Frames whose method is not a string: the reader must survive them
        # for the secret calls below to be answered at all.
        send({"jsonrpc": "2.0", "method": []})
        send({"jsonrpc": "2.0", "id": 899, "method": {"m": 1}})
        if MODE == "write":
            send({"jsonrpc": "2.0", "id": 901, "method": "_kiro/secret/store",
                  "params": {"key": KEY, "value": VALUE}})
        else:
            send({"jsonrpc": "2.0", "id": 902, "method": "_kiro/secret/get",
                  "params": {"key": KEY}})
    elif mid == 901 and method is None:
        seen["store"] = {k: msg[k] for k in ("result", "error") if k in msg}
        send({"jsonrpc": "2.0", "id": 902, "method": "_kiro/secret/get",
              "params": {"key": KEY}})
    elif mid == 902 and method is None:
        seen["get"] = {k: msg[k] for k in ("result", "error") if k in msg}
        seen["done"] = True
    elif mid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    # Published whole: the test polls this file, so it must never see a
    # truncated one.
    with open(CAPTURE + ".tmp", "w") as fh:
        json.dump(seen, fh)
    os.replace(CAPTURE + ".tmp", CAPTURE)
""" % {"key": _TOKENS_KEY, "value": _TOKEN_VALUE}

_STUB_AGENT_SPEC = {
    "name": "kirocrew",
    "description": "secret storage stub",
    "prompt": "You are a test agent.",
    "tools": [],
    "allowedTools": [],
}


@pytest.fixture
def kas_secret_stub(tmp_path, monkeypatch):
    script = tmp_path / "kas_stub.py"
    script.write_text(_STUB_AGENT)
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("KIROCREW_TEST_SECRET_CAPTURE", str(capture))
    from kiro_crew.agent import kiro_agents_dir_path

    for agents_dir in {kiro_agents_dir(), kiro_agents_dir_path()}:
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{_STUB_AGENT_SPEC['name']}.json").write_text(
            json.dumps(_STUB_AGENT_SPEC), encoding="utf-8"
        )
    return capture


async def _run_stub(
    tmp_path, capture, monkeypatch, mode: str, session_key: str = _OWNER_KEY
) -> dict:
    monkeypatch.setenv("KIROCREW_TEST_SECRET_MODE", mode)
    if capture.exists():
        capture.unlink()
    runtime = AcpRuntime(
        work_dir=tmp_path / "ws",
        sandbox_mode="off",
        acp_backend=ACP_BACKEND_KAS,
        mcp_grant_key=session_key,
    )
    runtime.recording_allowed = True
    try:
        await runtime.spawn()
        await runtime.create_session(cwd=tmp_path / "ws")
        await async_wait_until(
            lambda: capture.exists() and json.loads(capture.read_text()).get("done"),
            off_loop=True,
            describe=lambda: capture.read_text() if capture.exists() else "no capture yet",
        )
        return json.loads(capture.read_text())
    finally:
        await runtime.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
async def test_a_new_kas_process_reuses_the_stored_grant(
    kas_secret_stub, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(kss, "persistence_enabled", lambda: True)
    recorded: list[dict] = []

    async def record(backend, frame, wire_bytes=0):
        recorded.append(frame)

    monkeypatch.setattr("kiro_crew.acp.runtime.record_frame", record)
    caplog.set_level(logging.DEBUG)

    first = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "write")
    assert first["clientMeta"]["kiro"]["secretStorage"] is True
    assert first["store"] == {"result": {}}
    assert first["get"] == {"result": {"value": _TOKEN_VALUE}}

    second = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "read")
    assert second["get"] == {"result": {"value": _TOKEN_VALUE}}

    assert recorded, "the recorder saw no frames at all"
    assert not [f for f in recorded if kss.is_secret_method(f.get("method"))]
    assert "SECRET" not in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
async def test_with_the_key_off_nothing_is_declared_or_stored(
    kas_secret_stub, tmp_path, monkeypatch
):
    monkeypatch.setattr(kss, "persistence_enabled", lambda: False)
    seen = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "write")
    assert "secretStorage" not in seen["clientMeta"]["kiro"]
    assert seen["store"]["error"]["code"] == -32601
    assert seen["get"]["error"]["code"] == -32601
    assert not (data_home() / "kas" / "mcp-grants").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
async def test_a_second_users_kas_process_never_reads_the_first_users_grant(
    kas_secret_stub, tmp_path, monkeypatch
):
    """A signs in; B's session must authorize itself; A's next session reuses A's grant."""
    monkeypatch.setattr(kss, "persistence_enabled", lambda: True)
    principals.bind("chat-b", "user:telegram-42")

    a_first = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "write")
    assert a_first["store"] == {"result": {}}

    b_seen = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "read", "chat-b")
    assert b_seen["clientMeta"]["kiro"]["secretStorage"] is True
    assert b_seen["get"] == {"result": {}}

    a_later = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "read", _OWNER_KEY)
    assert a_later["get"] == {"result": {"value": _TOKEN_VALUE}}


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", ["", "slack:1700000000.0001", "cron:nightly"])
async def test_a_process_with_no_bound_user_persists_nothing(
    kas_secret_stub, tmp_path, monkeypatch, session_key
):
    monkeypatch.setattr(kss, "persistence_enabled", lambda: True)
    await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "write")  # the owner's grant
    seen = await _run_stub(tmp_path, kas_secret_stub, monkeypatch, "read", session_key)
    assert "secretStorage" not in seen["clientMeta"]["kiro"]
    assert seen["get"]["error"]["code"] == -32601


# ── per-user isolation of the server and the binding ─────────────────────────


@pytest.mark.asyncio
async def test_user_b_gets_nothing_from_user_a_and_a_keeps_its_own(tmp_path):
    principals.bind("chat-b", "user:telegram-42")
    a = _server(tmp_path)
    await a.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})

    b = _server(tmp_path, "user:telegram-42", "chat-b")
    assert await b.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}
    await b.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "b-grant"})
    # B's clear is confined to B's vault.
    await b.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})

    assert await _server(tmp_path).answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {
        "value": _TOKEN_VALUE
    }
    assert _vault_dir(tmp_path) != _vault_dir(tmp_path, "user:telegram-42")


@pytest.mark.asyncio
async def test_a_second_user_on_a_session_stops_its_vault_use(tmp_path):
    a = _server(tmp_path)
    await a.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})

    principals.bind(_OWNER_KEY, "user:telegram-42")  # B drove A's session
    assert principals.principal_for(_OWNER_KEY) is None
    # The live process stops reading or writing the vault: it serves memory only.
    assert await a.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}
    await a.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "from-b"})
    await a.answer(kss.METHOD_SECRET_DELETE, {"key": _TOKENS_KEY})
    # The grant A stored is untouched; the conflict outlives the original user returning.
    principals.bind(_OWNER_KEY, principals.OWNER_PRINCIPAL)
    assert principals.principal_for(_OWNER_KEY) is None
    store = kss.McpGrantStore(tmp_path, principals.OWNER_PRINCIPAL)
    assert store.get(_TOKENS_KEY)[0] == _TOKEN_VALUE


@pytest.mark.asyncio
async def test_a_conflict_during_a_vault_read_discards_the_value(tmp_path, monkeypatch):
    store = kss.McpGrantStore(tmp_path, principals.OWNER_PRINCIPAL)
    await store.store(_TOKENS_KEY, _TOKEN_VALUE)
    real_get = store.get

    def get_then_conflict(key):
        found = real_get(key)
        principals.bind(_OWNER_KEY, "user:telegram-42")  # B drives the session mid-read
        return found

    monkeypatch.setattr(store, "get", get_then_conflict)
    server = kss.SecretStorageServer(principals.OWNER_PRINCIPAL, _OWNER_KEY, store)
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


@pytest.mark.asyncio
async def test_a_request_queued_behind_the_key_lock_rechecks_its_binding(tmp_path):
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})
    lock = server._locks[_TOKENS_KEY]
    await lock.acquire()
    queued = asyncio.ensure_future(server.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}))
    await asyncio.sleep(0)
    principals.bind(_OWNER_KEY, "user:telegram-42")
    lock.release()
    assert await queued == {}


@pytest.mark.asyncio
async def test_an_evicted_binding_never_reopens_a_live_processs_vault(tmp_path, monkeypatch):
    """A evicted, B drives memory-only, B evicted, A re-binds: the process stays memory-only."""
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})
    monkeypatch.setattr(principals, "MAX_BINDINGS", 1)
    principals.bind("flood-1", "user:b")  # evicts the owner's binding
    principals.bind(_OWNER_KEY, "user:b")  # B drives the session, fresh
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "b-token"})
    principals.bind("flood-2", "user:b")  # evicts B's binding
    principals.bind(_OWNER_KEY, principals.OWNER_PRINCIPAL)  # A binds again
    assert principals.principal_for(_OWNER_KEY) == principals.OWNER_PRINCIPAL
    assert await server.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {"value": "b-token"}
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "b-token-2"})
    store = kss.McpGrantStore(tmp_path, principals.OWNER_PRINCIPAL)
    assert store.get(_TOKENS_KEY)[0] == _TOKEN_VALUE  # B's tokens never reached A's vault


@pytest.mark.asyncio
async def test_an_unobserved_evict_and_rebind_never_reauthorizes_a_process(tmp_path, monkeypatch):
    """No secret request runs while A is evicted, B binds, B is evicted and A binds again."""
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})
    monkeypatch.setattr(principals, "MAX_BINDINGS", 1)
    principals.bind("flood-1", "user:b")
    principals.bind(_OWNER_KEY, "user:b")
    principals.bind("flood-2", "user:b")
    principals.bind(_OWNER_KEY, principals.OWNER_PRINCIPAL)
    assert principals.principal_for(_OWNER_KEY) == principals.OWNER_PRINCIPAL
    # The first request after all that is B's pending sign-in being stored.
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": "b-token"})
    store = kss.McpGrantStore(tmp_path, principals.OWNER_PRINCIPAL)
    assert store.get(_TOKENS_KEY)[0] == _TOKEN_VALUE


def test_a_rebinding_is_a_new_generation_even_for_the_same_user(monkeypatch):
    principals.reset_for_tests()
    principals.bind("k", "owner")
    first = principals.binding_for("k")
    principals.bind("k", "owner")
    assert principals.binding_for("k") == first  # the same live binding
    monkeypatch.setattr(principals, "MAX_BINDINGS", 1)
    principals.bind("other", "owner")  # evicts k
    principals.bind("k", "owner")
    assert principals.binding_for("k") != first


@pytest.mark.asyncio
async def test_a_vault_value_is_withheld_when_the_session_conflicts_before_sending(tmp_path):
    server = _server(tmp_path)
    await server.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE})
    params = {"key": _TOKENS_KEY}
    result = await server.answer(kss.METHOD_SECRET_GET, params)
    assert result == {"value": _TOKEN_VALUE}
    assert server.release(params, result) == result  # still bound: sent as read
    principals.bind(_OWNER_KEY, "user:telegram-42")
    assert server.release(params, result) == {}
    verifier = {"key": _VERIFIER_KEY}
    assert server.release(verifier, {"value": "pkce"}) == {"value": "pkce"}


@pytest.mark.asyncio
async def test_a_conflicted_session_spawns_without_storage(monkeypatch):
    monkeypatch.setattr(kss, "persistence_enabled", lambda: True)
    principals.bind(_OWNER_KEY, "user:telegram-42")
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS, mcp_grant_key=_OWNER_KEY)
    runtime._tool_search = None
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert "secretStorage" not in caps.get("_meta", {}).get("kiro", {})
    assert runtime._mcp_secret_server is None


@pytest.mark.asyncio
async def test_the_gateway_wide_vault_of_an_earlier_build_is_never_served(tmp_path):
    from kiro_crew.secrets import SecretVault

    await SecretVault(tmp_path / "kas" / "mcp-grants").set(_TOKENS_KEY, _TOKEN_VALUE)
    for principal, key in ((principals.OWNER_PRINCIPAL, _OWNER_KEY), ("user:x", "chat-x")):
        principals.bind(key, principal)
        server = _server(tmp_path, principal, key)
        assert await server.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


def test_a_store_needs_a_principal(tmp_path):
    for principal in ("", None):
        with pytest.raises(kss.SecretStorageRefused):
            kss.McpGrantStore(tmp_path, principal)


def test_subjects_map_to_principals():
    assert principals.principal_for_subject("local-app", is_owner=True) == "owner"
    assert principals.principal_for_subject("U123", is_owner=True) == "owner"
    assert principals.principal_for_subject("42", is_owner=False) == "user:42"
    assert principals.principal_for_subject("", is_owner=False) is None
    assert principals.principal_for_subject(None, is_owner=False) is None


def test_bindings_are_bounded_and_eviction_leaves_a_key_unbound(monkeypatch):
    monkeypatch.setattr(principals, "MAX_BINDINGS", 2)
    principals.reset_for_tests()
    for key in ("k1", "k2", "k3"):
        principals.bind(key, "owner")
    assert principals.principal_for("k1") is None
    assert principals.principal_for("k3") == "owner"


def test_eviction_never_frees_a_conflicted_key_while_a_binding_can_go(monkeypatch):
    """B cannot flood the registry to clear the conflict B put on A's session."""
    monkeypatch.setattr(principals, "MAX_BINDINGS", 3)
    principals.reset_for_tests()
    principals.bind("a-session", "owner")
    principals.bind("a-session", "user:b")
    for i in range(10):
        principals.bind(f"b-flood-{i}", "user:b")
    principals.bind("a-session", "owner")
    assert principals.principal_for("a-session") is None
    # Conflicting an unbound key (a Slack thread, an app slot) adds no entry, so
    # any number of them can never fill the table and block real users.
    principals.reset_for_tests()
    for i in range(10):
        principals.conflict(f"slack:{i}")
    principals.bind("fresh", "owner")
    assert principals.principal_for("fresh") == "owner"
    assert principals.principal_for("slack:0") is None
    # A table holding only conflicts drops the oldest rather than refusing a user.
    principals.reset_for_tests()
    for key in ("c1", "c2", "c3"):
        principals.bind(key, "owner")
        principals.conflict(key)
    principals.bind("fresh", "owner")
    assert principals.principal_for("fresh") == "owner"
    assert all(principals.principal_for(k) is None for k in ("c1", "c2", "c3"))
    for key, principal in (("", "owner"), ("k4", ""), (None, "owner"), ("k5", None)):
        principals.bind(key, principal)
    assert principals.principal_for("k4") is None and principals.principal_for("k5") is None
