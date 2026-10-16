"""Credential-safe structured Coder CLI client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import kiro_crew.coder.client as client_mod
from kiro_crew import platform_compat
from kiro_crew.coder.client import (
    CoderClient,
    CoderClientError,
    CoderWorkspace,
)


@pytest.mark.asyncio
async def test_create_keeps_token_out_of_argv_and_reconciles_structured_record(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append((argv, env))
        if argv[1] == "create":
            return b""
        return json.dumps(
            [
                {
                    "id": "workspace-uuid",
                    "name": "crew-abc123",
                    "owner_id": "owner-immutable-uuid",
                    "owner_name": "kyleseaman",
                    "template_name": "kirocrew-arm",
                    "latest_build": {"status": "running"},
                    "last_used_at": "2026-08-25T12:00:00Z",
                }
            ]
        ).encode()

    client = CoderClient(
        coder_bin="/opt/coder",
        url="https://coder.example",
        token="secret-token-canary",
        cwd=tmp_path,
        runner=run,
    )

    workspace = await client.create_workspace(
        name="crew-abc123",
        template="kirocrew-arm",
        preset="arm-small",
        stop_after_minutes=30,
    )

    assert workspace == CoderWorkspace(
        uuid="workspace-uuid",
        name="crew-abc123",
        owner="owner-immutable-uuid",
        template="kirocrew-arm",
        status="running",
        last_used_at="2026-08-25T12:00:00Z",
    )
    flattened = json.dumps([argv for argv, _env in calls])
    assert "secret-token-canary" not in flattened
    assert all(env["CODER_SESSION_TOKEN"] == "secret-token-canary" for _argv, env in calls)
    assert calls[0][0] == [
        "/opt/coder",
        "create",
        "crew-abc123",
        "--template",
        "kirocrew-arm",
        "--preset",
        "arm-small",
        "--stop-after",
        "30m",
        "--yes",
        "--use-parameter-defaults",
    ]


@pytest.mark.asyncio
async def test_get_workspace_returns_none_for_absent_record(tmp_path: Path) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return b""

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert await client.get_workspace("crew-missing") is None


@pytest.mark.asyncio
async def test_stop_workspace_uses_noninteractive_coder_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        if argv[1] == "stop":
            return b""
        return json.dumps(
            [
                {
                    "id": "workspace-uuid",
                    "name": "crew-abc123",
                    "owner_id": "owner-immutable-uuid",
                    "template_name": "kirocrew-arm",
                    "latest_build": {"status": "stopped"},
                    "last_used_at": "2026-08-25T12:00:00Z",
                }
            ]
        ).encode()

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    workspace = await client.stop_workspace("crew-abc123")

    assert workspace.status == "stopped"
    assert calls[0] == ["/opt/coder", "stop", "crew-abc123", "--yes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "start", "delete"])
async def test_mutating_workspace_commands_reject_unsafe_names(
    tmp_path: Path,
    operation: str,
) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        raise AssertionError(f"unsafe command reached runner: {argv}")

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    with pytest.raises(CoderClientError, match="workspace name"):
        if operation == "create":
            await client.create_workspace(
                name="crew;bad",
                template="kirocrew-arm",
                preset="",
                stop_after_minutes=30,
            )
        elif operation == "start":
            await client.start_workspace("crew;bad")
        else:
            await client.delete_workspace("crew;bad")


@pytest.mark.asyncio
async def test_probe_verifies_identity_and_template_without_creating_compute(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        calls.append(argv)
        if argv[1] == "whoami":
            return json.dumps(
                [
                    {
                        "user_id": "0d5bd32b-20ba-4937-9f89-a6726be46ba1",
                        "username": "kyleseaman",
                    }
                ]
            ).encode()
        if argv[1:3] == ["templates", "list"]:
            return json.dumps([{"Template": {"name": "kirocrew-arm"}}]).encode()
        raise AssertionError(argv)

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    assert await client.probe(template="kirocrew-arm", preset="") == {
        "owner": "kyleseaman",
        "template": "kirocrew-arm",
    }
    assert calls[0] == ["/opt/coder", "whoami", "--output", "json"]
    assert all("create" not in call for call in calls)


@pytest.mark.asyncio
async def test_probe_refuses_a_template_the_deployment_does_not_offer(tmp_path: Path) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        if argv[1] == "whoami":
            return json.dumps([{"user_id": "user-uuid", "username": "kyleseaman"}]).encode()
        return json.dumps([{"name": "some-other-template"}]).encode()

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    with pytest.raises(CoderClientError, match="template is unavailable"):
        await client.probe(template="kirocrew-arm", preset="")


@pytest.mark.asyncio
async def test_workspace_query_refuses_a_record_with_an_invalid_shape(tmp_path: Path) -> None:
    async def run(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return json.dumps([{"id": "workspace-uuid", "name": 42}]).encode()

    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path, run)

    with pytest.raises(CoderClientError, match="invalid shape"):
        await client.list_workspaces()


def _mock_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes | None,
    stderr: bytes,
    returncode: int,
) -> AsyncMock:
    """Install a fake ``create_subprocess_limited``; no real process is spawned."""
    stdout_stream = asyncio.StreamReader()
    if stdout is not None:
        stdout_stream.feed_data(stdout)
        stdout_stream.feed_eof()
    stderr_stream = asyncio.StreamReader()
    stderr_stream.feed_data(stderr)
    stderr_stream.feed_eof()
    process = AsyncMock()
    process.pid = 4242
    process.returncode = returncode
    process.stdout = stdout_stream
    process.stderr = stderr_stream
    process.wait = AsyncMock(return_value=returncode)
    monkeypatch.setattr(
        client_mod,
        "create_subprocess_limited",
        AsyncMock(return_value=process),
    )
    return process


@pytest.mark.asyncio
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    diagnostic: str,
) -> None:
    canary = "synthetic-secret-canary"
    _mock_process(
        monkeypatch,
        stdout=b"",
        stderr=diagnostic.format(canary=canary).encode() + b"x" * 8192,
        returncode=7,
    )
    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path)

    with (
        caplog.at_level(logging.WARNING, logger=client_mod.__name__),
        pytest.raises(CoderClientError, match=r"failed \(exit 7\)") as error,
    ):
        await client._run(["/opt/coder", "list"], client._env(), tmp_path)

    assert canary not in str(error.value)
    records = [record for record in caplog.records if record.name == client_mod.__name__]
    assert records
    assert all(canary not in record.getMessage() for record in records)
    assert sum(len(record.getMessage()) for record in records) < 4096


@pytest.mark.asyncio
async def test_oversized_stdout_is_refused_rather_than_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod, "_OUTPUT_MAX_BYTES", 8)
    _mock_process(monkeypatch, stdout=b"x" * 64, stderr=b"", returncode=0)
    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path)

    with pytest.raises(CoderClientError, match="too much data"):
        await client._run(["/opt/coder", "list"], client._env(), tmp_path)


@pytest.mark.asyncio
async def test_a_hung_command_times_out_and_reaps_the_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[object] = []

    async def fake_kill_and_reap(proc, *, timeout=None):
        reaped.append(proc)

    monkeypatch.setattr(client_mod, "_COMMAND_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(platform_compat, "kill_and_reap", fake_kill_and_reap)
    process = _mock_process(monkeypatch, stdout=None, stderr=b"", returncode=0)
    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path)

    with pytest.raises(CoderClientError, match="timed out"):
        await client._run(["/opt/coder", "list"], client._env(), tmp_path)

    # The whole tree is killed and reaped through the shared helper.
    assert reaped == [process]


@pytest.mark.asyncio
async def test_cancellation_reaps_the_process_tree_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[object] = []

    async def fake_kill_and_reap(proc, *, timeout=None):
        reaped.append(proc)

    monkeypatch.setattr(platform_compat, "kill_and_reap", fake_kill_and_reap)
    process = _mock_process(monkeypatch, stdout=None, stderr=b"", returncode=0)
    client = CoderClient("/opt/coder", "https://coder.example", "token", tmp_path)

    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await asyncio.Event().wait()

    process.wait.side_effect = waiting
    task = asyncio.ensure_future(client._run(["/opt/coder", "list"], client._env(), tmp_path))
    try:
        await asyncio.wait_for(entered.wait(), 30)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert reaped == [process]


@pytest.mark.asyncio
async def test_environment_carries_the_token_and_no_unrelated_host_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-unrelated-host-secret")
    for inherited in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(inherited, raising=False)
    client = CoderClient("/opt/coder", "https://coder.example", "session-token", tmp_path)

    env = client._env()

    assert env == {
        "PATH": "/usr/bin",
        "CODER_URL": "https://coder.example",
        "CODER_SESSION_TOKEN": "session-token",
    }
