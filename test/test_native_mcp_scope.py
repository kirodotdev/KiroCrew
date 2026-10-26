"""Host-only scope issuance cannot be turned into broader native broker authority."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import native_handles, native_scope
from kiro_crew.mcp_gateway.required import GatewayRequiredError
from kiro_crew.mcp_gateway.stub_fallback import fallback_exec


@pytest.fixture
def envelope(monkeypatch):
    monkeypatch.setattr(native_scope, "_key", lambda: b"synthetic-scope-key")
    register = {
        "type": "register",
        "stub_uuid": "client",
        "server_name": "kirocrew-core",
        "agent_name": "allowed-agent",
        "work_dir": "C:/synthetic",
        "poolable": False,
        "stub_session_token": "synthetic-session-token",
    }
    return register, native_scope.issue(register, "runtime-A", now=1000)


def test_scope_requires_runtime_session_and_freshness(envelope):
    register, scope = envelope
    assert native_scope.verify(scope, register, "runtime-A", now=1001)
    assert not native_scope.verify(scope, register, "runtime-B", now=1001)
    assert not native_scope.verify(scope, register, "runtime-A", now=87_400)
    assert not native_scope.verify(
        scope, {**register, "stub_session_token": "foreign-session"}, "runtime-A", now=1001
    )


@pytest.mark.parametrize("channel", [None, "dashboard:synthetic"])
def test_attach_signs_the_child_channel_instead_of_the_hosts(monkeypatch, tmp_path, channel):
    from kiro_crew.mcp_gateway import stub_register
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    monkeypatch.setattr(native_scope, "_key", lambda: b"synthetic-scope-key")
    monkeypatch.setenv("KIROCREW_CHANNEL_ID", "unrelated-host-channel")
    monkeypatch.setattr(
        stub_register,
        "build_register_payload",
        lambda *args, **kwargs: {
            "type": "register",
            "stub_uuid": "client",
            "stub_session_token": "synthetic",
            "channel_id": "unrelated-host-channel",
        },
    )
    (tmp_path / native_scope.DESCRIPTOR_NAME).write_text(
        json.dumps({"pid": 1, "start": 2, "request": 3, "response": 4, "instance": "runtime-A"}),
        encoding="ascii",
    )
    env = [{"name": STUB_SESSION_TOKEN_ENV, "value": "synthetic"}]
    if channel:
        env.append({"name": "KIROCREW_CHANNEL_ID", "value": channel})
    [entry] = native_scope.attach(
        [
            {
                "name": "kirocrew-core",
                "command": "python",
                "args": [
                    "-m",
                    "kiro_crew.mcp_gateway.stub",
                    "--server",
                    "kirocrew-core",
                    "--agent",
                    "synthetic",
                    "--target-command",
                    "python",
                    "--work-dir",
                    str(tmp_path),
                ],
                "env": env,
            }
        ],
        tmp_path,
    )
    child_env = {item["name"]: item["value"] for item in entry["env"]}
    body = json.loads(child_env[native_scope.SCOPE_ENV])["body"]
    assert body["fields"]["channel_id"] == channel
    assert child_env["PYTHONIOENCODING"] == "utf-8:backslashreplace"


def test_confined_runtime_uses_signed_host_generation_without_losing_binary_identity(envelope):
    register, _ = envelope
    register = {
        **register,
        "binary_version": "exe-hash+git-generation",
        "stub_code_fingerprint": "git-generation",
    }
    scope = native_scope.issue(register, "runtime-A", now=1000)
    version, fingerprint = native_scope.runtime_identity(
        scope, "exe-hash+mtime:confined-generation", "mtime:confined-generation"
    )
    assert (version, fingerprint) == ("exe-hash+git-generation", "git-generation")
    assert native_scope.verify(
        scope,
        {**register, "binary_version": version, "stub_code_fingerprint": fingerprint},
        "runtime-A",
        now=1001,
    )
    with pytest.raises(GatewayRequiredError):
        native_scope.runtime_identity(
            scope, "different-executable+mtime:confined-generation", "mtime:confined-generation"
        )

    forged = json.loads(scope)
    forged["body"]["fields"].update(
        {"binary_version": "exe-hash+forged", "stub_code_fingerprint": "forged"}
    )
    raw = json.dumps(forged)
    version, fingerprint = native_scope.runtime_identity(raw, "exe-hash+local", "local")
    assert not native_scope.verify(
        raw,
        {**register, "binary_version": version, "stub_code_fingerprint": fingerprint},
        "runtime-A",
        now=1001,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("server_name", "foreign-server"),
        ("agent_name", "admin"),
        ("work_dir", "C:/foreign"),
        ("poolable", True),
        ("trust_all_tools", True),
        ("command_args_hash", "changed"),
        ("effective_env_hash", "changed"),
        ("channel_id", "foreign"),
        ("approval_mode", "always"),
    ],
)
def test_scope_cannot_change_launch_authority(envelope, field, value):
    register, scope = envelope
    assert not native_scope.verify(scope, {**register, field: value}, "runtime-A", now=1001)


@pytest.mark.parametrize("verb", ["abort", "claim", "stand-down", "ping", "approve"])
def test_scoped_client_cannot_choose_first_frame_control(envelope, verb):
    register, scope = envelope
    hostile = {**register, "type": verb}
    assert not native_scope.verify(scope, hostile, "runtime-A", now=1001)
    assert native_scope.canonical_register(hostile)["type"] == "register"


def test_scope_rejects_unknown_control_fields_and_tampering(envelope):
    register, scope = envelope
    assert not native_scope.verify(
        scope, {**register, "caller_fingerprint": "stop-daemon"}, "runtime-A", now=1001
    )
    altered = json.loads(scope)
    altered["body"]["expires"] = 999999999
    assert not native_scope.verify(json.dumps(altered), register, "runtime-A", now=1001)
    assert not native_scope.verify("x" * 16385, register, "runtime-A", now=1001)


def test_host_register_cannot_carry_reported_identity(envelope):
    register, _ = envelope
    hostile = {
        **register,
        "caller": {"session_key": "foreign"},
        "ancestor_pids": [1, 2],
        "session_key": "foreign",
        "principal_id": "owner",
        "pids": [99],
    }
    frame = native_scope.canonical_register(hostile)
    assert frame["caller"] == {} and frame["ancestor_pids"] == []
    assert frame["session_key"] == "" and frame["principal_id"] == ""
    assert "pids" not in frame and frame["native_session"] is True


@pytest.mark.parametrize("server", ["kirocrew-core", "kirocrew-cron"])
def test_required_gateway_blocks_fallback_before_resolving_command(monkeypatch, server):
    monkeypatch.setenv(native_scope.REQUIRED_ENV, "1")
    # Deliberately no target command: refusal happens before even resolving it.
    with pytest.raises(GatewayRequiredError):
        fallback_exec(SimpleNamespace(server=server))


def test_nonprivileged_direct_fallback_policy_is_unchanged(monkeypatch):
    from kiro_crew.mcp_gateway.required import required

    monkeypatch.setenv(native_scope.REQUIRED_ENV, "1")
    assert not required(SimpleNamespace(server="operator-approved-third-party"))


def test_missing_key_cannot_issue_scope(monkeypatch, envelope):
    register, _ = envelope
    monkeypatch.setattr(native_scope, "_key", lambda: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        native_scope.issue(register, "runtime-A")
    assert not native_scope.verify(envelope[1], register, "runtime-A")


@pytest.mark.parametrize("winerror", [231, 5, 2])
def test_native_pipe_retries_only_busy_without_changing_access(monkeypatch, winerror):
    calls = []
    sleeps = []
    error = OSError("synthetic pipe refusal")
    error.winerror = winerror

    def connect(address):
        calls.append(address)
        if len(calls) == 1:
            raise error
        return 123

    monkeypatch.setattr(
        native_handles,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(ConnectPipe=connect, ERROR_PIPE_BUSY=231)
        ),
    )
    monkeypatch.setattr(
        native_handles, "time", SimpleNamespace(monotonic=lambda: 100.0, sleep=sleeps.append)
    )
    if winerror == 231:
        assert native_handles._connect_pipe("synthetic-owner-pipe") == 123
        assert calls == ["synthetic-owner-pipe"] * 2
        assert sleeps == [0.001]
    else:
        with pytest.raises(OSError) as caught:
            native_handles._connect_pipe("synthetic-owner-pipe")
        assert caught.value is error
        assert len(calls) == 1 and not sleeps


def test_native_pipe_busy_budget_is_bounded(monkeypatch):
    clock = iter([100.0, 100.5, 101.0])
    sleeps = []

    def connect(address):
        error = OSError("synthetic occupied pipe")
        error.winerror = 231
        raise error

    monkeypatch.setattr(
        native_handles,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(ConnectPipe=connect, ERROR_PIPE_BUSY=231)
        ),
    )
    monkeypatch.setattr(
        native_handles, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=sleeps.append)
    )
    with pytest.raises(TimeoutError, match="remained busy"):
        native_handles._connect_pipe("synthetic-owner-pipe")
    assert sleeps == [0.001]
