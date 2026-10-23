"""Credential-safe structured Coder CLI client."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

import kiro_crew.coder.client as client_mod
from kiro_crew import platform_compat
from kiro_crew.coder.client import (
    CoderClient,
    CoderClientError,
    CoderWorkspace,
)

pytestmark = pytest.mark.asyncio


def _client(tmp_path, run=None, token="token"):
    return CoderClient("/opt/coder", "https://coder.example", token, tmp_path, run)


def _listing(status, **extra) -> bytes:
    """``coder list`` output holding one ``crew-abc123`` record."""
    record = {
        "id": "workspace-uuid",
        "name": "crew-abc123",
        "owner_id": "owner-immutable-uuid",
        **extra,
        "template_name": "kirocrew-arm",
        "latest_build": {"status": status},
        "last_used_at": "2026-08-25T12:00:00Z",
    }
    return json.dumps([record]).encode()


def _run_list(client, tmp_path):
    return client._run(["/opt/coder", "list"], client._env(), tmp_path)


@pytest.fixture
def reaped(monkeypatch) -> list[object]:
    """Processes passed to a stubbed ``platform_compat.kill_and_reap``."""
    seen: list[object] = []

    async def fake_kill_and_reap(proc, *, timeout=None):
        seen.append(proc)

    monkeypatch.setattr(platform_compat, "kill_and_reap", fake_kill_and_reap)
    return seen


async def test_create_keeps_token_out_of_argv_and_reconciles_structured_record(tmp_path):
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(argv, env, cwd):
        calls.append((argv, env))
        return b"" if argv[1] == "create" else _listing("running", owner_name="kyleseaman")

    client = _client(tmp_path, run, token="secret-token-canary")
    workspace = await client.create_workspace(
        name="crew-abc123", template="kirocrew-arm", preset="arm-small", stop_after_minutes=30
    )
    assert workspace == CoderWorkspace(
        "workspace-uuid",
        "crew-abc123",
        "owner-immutable-uuid",
        "kirocrew-arm",
        "running",
        "2026-08-25T12:00:00Z",
    )
    flattened = json.dumps([argv for argv, _env in calls])
    assert "secret-token-canary" not in flattened
    assert all(env["CODER_SESSION_TOKEN"] == "secret-token-canary" for _argv, env in calls)
    expected = "create crew-abc123 --template kirocrew-arm --preset arm-small --stop-after 30m"
    assert calls[0][0] == ["/opt/coder", *expected.split(), "--yes", "--use-parameter-defaults"]


async def test_get_workspace_returns_none_for_absent_record(tmp_path):
    async def run(argv, env, cwd):
        return b""

    assert await _client(tmp_path, run).get_workspace("crew-missing") is None


async def test_stop_workspace_uses_noninteractive_coder_command(tmp_path):
    calls: list[list[str]] = []

    async def run(argv, env, cwd):
        calls.append(argv)
        return b"" if argv[1] == "stop" else _listing("stopped")

    workspace = await _client(tmp_path, run).stop_workspace("crew-abc123")
    assert workspace.status == "stopped"
    assert calls[0] == ["/opt/coder", "stop", "crew-abc123", "--yes"]


@pytest.mark.parametrize("operation", ["create", "start", "delete"])
async def test_mutating_workspace_commands_reject_unsafe_names(tmp_path, operation):
    async def run(argv, env, cwd):
        raise AssertionError(f"unsafe command reached runner: {argv}")

    client = _client(tmp_path, run)
    with pytest.raises(CoderClientError, match="workspace name"):
        if operation == "create":
            await client.create_workspace(
                name="crew;bad", template="kirocrew-arm", preset="", stop_after_minutes=30
            )
        elif operation == "start":
            await client.start_workspace("crew;bad")
        else:
            await client.delete_workspace("crew;bad")


async def test_probe_verifies_identity_and_template_without_creating_compute(tmp_path):
    calls: list[list[str]] = []

    async def run(argv, env, cwd):
        calls.append(argv)
        if argv[1] == "whoami":
            user = {"user_id": "0d5bd32b-20ba-4937-9f89-a6726be46ba1", "username": "kyleseaman"}
            return json.dumps([user]).encode()
        if argv[1:3] == ["templates", "list"]:
            return json.dumps([{"Template": {"name": "kirocrew-arm"}}]).encode()
        raise AssertionError(argv)

    assert await _client(tmp_path, run).probe(template="kirocrew-arm", preset="") == {
        "owner": "kyleseaman",
        "template": "kirocrew-arm",
    }
    assert calls[0] == ["/opt/coder", "whoami", "--output", "json"]
    assert all("create" not in call for call in calls)


async def test_probe_refuses_a_template_the_deployment_does_not_offer(tmp_path):
    async def run(argv, env, cwd):
        if argv[1] == "whoami":
            return json.dumps([{"user_id": "user-uuid", "username": "kyleseaman"}]).encode()
        return json.dumps([{"name": "some-other-template"}]).encode()

    with pytest.raises(CoderClientError, match="template is unavailable"):
        await _client(tmp_path, run).probe(template="kirocrew-arm", preset="")


async def test_workspace_query_refuses_a_record_with_an_invalid_shape(tmp_path):
    async def run(argv, env, cwd):
        return json.dumps([{"id": "workspace-uuid", "name": 42}]).encode()

    with pytest.raises(CoderClientError, match="invalid shape"):
        await _client(tmp_path, run).list_workspaces()


def _mock_process(monkeypatch, *, stdout: bytes | None, stderr: bytes, returncode: int):
    """Install a fake ``create_subprocess_limited``; no real process is spawned."""
    stdout_stream = asyncio.StreamReader()
    if stdout is not None:
        stdout_stream.feed_data(stdout)
        stdout_stream.feed_eof()
    stderr_stream = asyncio.StreamReader()
    stderr_stream.feed_data(stderr)
    stderr_stream.feed_eof()
    process = AsyncMock(pid=2**32 + 1, returncode=returncode, stdout=stdout_stream)
    process.stderr = stderr_stream
    process.wait = AsyncMock(return_value=returncode)
    monkeypatch.setattr(client_mod, "create_subprocess_limited", AsyncMock(return_value=process))
    return process


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Authorization: Bearer {canary}",
        "KIRO_API_KEY={canary}",
        "CODER_SESSION_TOKEN={canary}",
        "remote command printed {canary}",
    ],
)
async def test_subprocess_failure_does_not_publish_untrusted_diagnostics(
    tmp_path, monkeypatch, caplog, diagnostic
):
    canary = "synthetic-secret-canary"
    _mock_process(
        monkeypatch,
        stdout=b"",
        stderr=diagnostic.format(canary=canary).encode() + b"x" * 8192,
        returncode=7,
    )
    client = _client(tmp_path)
    with (
        caplog.at_level(logging.WARNING, logger=client_mod.__name__),
        pytest.raises(CoderClientError, match=r"failed \(exit 7\)") as error,
    ):
        await _run_list(client, tmp_path)

    assert canary not in str(error.value)
    records = [record for record in caplog.records if record.name == client_mod.__name__]
    assert records
    assert all(canary not in record.getMessage() for record in records)
    assert sum(len(record.getMessage()) for record in records) < 4096


async def test_oversized_stdout_is_refused_rather_than_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "_OUTPUT_MAX_BYTES", 8)
    _mock_process(monkeypatch, stdout=b"x" * 64, stderr=b"", returncode=0)
    with pytest.raises(CoderClientError, match="too much data"):
        await _run_list(_client(tmp_path), tmp_path)


async def test_a_hung_command_times_out_and_reaps_the_process_tree(tmp_path, monkeypatch, reaped):
    monkeypatch.setattr(client_mod, "_COMMAND_TIMEOUT_SECS", 0.05)
    process = _mock_process(monkeypatch, stdout=None, stderr=b"", returncode=0)
    with pytest.raises(CoderClientError, match="timed out"):
        await _run_list(_client(tmp_path), tmp_path)

    # The whole tree is killed and reaped through the shared helper.
    assert reaped == [process]


async def test_cancellation_reaps_the_process_tree_and_propagates(tmp_path, monkeypatch, reaped):
    process = _mock_process(monkeypatch, stdout=None, stderr=b"", returncode=0)
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await asyncio.Event().wait()

    process.wait.side_effect = waiting
    task = asyncio.ensure_future(_run_list(_client(tmp_path), tmp_path))
    try:
        await asyncio.wait_for(entered.wait(), 30)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert reaped == [process]


async def test_environment_carries_the_token_and_no_unrelated_host_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-unrelated-host-secret")
    for inherited in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(inherited, raising=False)

    env = _client(tmp_path, token="session-token")._env()
    assert env == {
        "PATH": "/usr/bin",
        "CODER_URL": "https://coder.example",
        "CODER_SESSION_TOKEN": "session-token",
    }
