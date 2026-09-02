"""Admission-boundary tests for coordinator-authoritative HTTP commands."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers.messaging import (
    _validated_command_identity,
    api_spawn,
    api_spawn_command_lookup,
    api_spawn_delete,
    api_spawn_steer,
)
from kiro_crew.subagent_command_authority import CommandIdentity


class _RequestContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _Request:
    def __init__(
        self,
        subagents: object,
        body: object,
        *,
        agent_id: str = "",
        can_read_body: bool = True,
    ) -> None:
        self.app = {
            "state": SimpleNamespace(
                subagents=subagents,
                sessions=SimpleNamespace(_pool_cwd=""),
            )
        }
        self._body = body
        raw_body = b"{" if isinstance(body, BaseException) else json.dumps(body).encode("utf-8")
        self.content = _RequestContent(raw_body)
        self.content_length = len(raw_body)
        self.charset = None
        self.match_info = {"agent_id": agent_id}
        self.can_read_body = can_read_body

    async def json(self) -> object:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


def _response_json(response: object) -> dict[str, object]:
    raw = getattr(response, "body")
    return json.loads(raw.decode("utf-8"))


def _identified(operation: str, body: dict[str, object]) -> dict[str, object]:
    payload_json = json.dumps(
        {"operation": operation, **body}, separators=(",", ":"), sort_keys=True
    )
    return {
        **body,
        "command_id": "a" * 32,
        "idempotency_key": "b" * 32,
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    }


def test_old_caller_without_identity_stays_on_compatibility_path() -> None:
    assert _validated_command_identity({"task": "legacy"}, "spawn", require_run_id=True) is None


def test_command_identity_recomputes_canonical_payload_hash() -> None:
    body = _identified("spawn", {"task": "work", "run_id": "c" * 8})

    identity = _validated_command_identity(body, "spawn", require_run_id=True)

    assert identity is not None
    assert identity[:3] == ("a" * 32, "b" * 32, body["payload_hash"])
    assert identity[3] == '{"operation":"spawn","run_id":"cccccccc","task":"work"}'


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"run_id": "c" * 8, "command_id": "a" * 32}, "incomplete_command_identity"),
        (
            {
                "run_id": "not-hex!",
                "command_id": "a" * 32,
                "idempotency_key": "b" * 32,
                "payload_hash": "c" * 64,
            },
            "invalid_run_id",
        ),
    ],
)
def test_invalid_command_identity_fails_closed(body: dict[str, object], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        _validated_command_identity(body, "spawn", require_run_id=True)


@pytest.mark.parametrize(
    ("operation", "require_run_id", "body"),
    [
        ("spawn", True, {"task": "work", "command_id": ""}),
        (
            "steer",
            False,
            {
                "message": "adjust",
                "command_id": "",
                "idempotency_key": "",
                "payload_hash": "",
            },
        ),
        ("spawn", True, {"task": "work", "run_id": ""}),
    ],
)
def test_empty_reserved_identity_values_never_select_legacy_execution(
    operation: str,
    require_run_id: bool,
    body: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _validated_command_identity(body, operation, require_run_id=require_run_id)


def test_changed_payload_is_rejected_even_with_claimed_hash() -> None:
    body = _identified("steer", {"message": "first", "mode": "interrupt"})
    body["message"] = "changed"

    with pytest.raises(ValueError, match="payload_hash_mismatch"):
        _validated_command_identity(body, "steer", require_run_id=False)


@pytest.mark.asyncio
async def test_keyed_spawn_uses_authority_and_never_calls_legacy_spawn() -> None:
    manager = MagicMock()
    manager.command_authority.spawn = AsyncMock(
        return_value=SimpleNamespace(id="c" * 8, done=False, error="")
    )
    body = _identified("spawn", {"task": "work", "agent": "", "run_id": "c" * 8})

    response = await api_spawn(_Request(manager, body))  # type: ignore[arg-type]

    assert response.status == 200
    assert _response_json(response)["id"] == "c" * 8
    manager.spawn.assert_not_called()
    identity = manager.command_authority.spawn.await_args.args[0]
    assert identity == CommandIdentity("c" * 8, "a" * 32, "b" * 32)


@pytest.mark.parametrize("counted", [False, True])
@pytest.mark.asyncio
async def test_legacy_spawn_rejection_redacts_credentials(counted: bool) -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    manager = MagicMock()
    manager.spawn.return_value = SimpleNamespace(
        id="rejected",
        done=True,
        error=f"unknown agent {secret}",
        counted=counted,
    )

    response = await api_spawn(
        _Request(manager, {"task": "work", "agent": secret})  # type: ignore[arg-type]
    )

    assert response.status == 400
    payload = _response_json(response)
    assert payload["code"] == "spawn_rejected"
    assert (payload.get("counted") is True) is counted
    assert secret not in str(payload["error"])


@pytest.mark.asyncio
async def test_keyed_steer_uses_authority_and_returns_identifiers() -> None:
    manager = MagicMock()
    manager.command_authority.steer = AsyncMock(return_value=(True, "ok"))
    body = _identified("steer", {"message": "correct it", "mode": "interrupt"})

    response = await api_spawn_steer(
        _Request(manager, body, agent_id="run123")  # type: ignore[arg-type]
    )

    assert response.status == 200
    payload = _response_json(response)
    assert payload["command_id"] == "a" * 32
    manager.steer_run.assert_not_called()


@pytest.mark.asyncio
async def test_command_lookup_returns_durable_response_without_manager_effect() -> None:
    manager = MagicMock()
    manager.command_authority.lookup_response = AsyncMock(
        return_value={"found": True, "id": "c" * 8, "status": "spawned"}
    )
    request = _Request(manager, {})
    request.match_info = {"idempotency_key": "b" * 32}

    response = await api_spawn_command_lookup(request)  # type: ignore[arg-type]

    assert response.status == 200
    assert _response_json(response)["found"] is True
    manager.command_authority.lookup_response.assert_awaited_once_with("b" * 32)


@pytest.mark.asyncio
async def test_keyed_cancel_uses_authority_for_unregistered_target() -> None:
    manager = MagicMock()
    manager._agents = {}
    manager.command_authority.cancel = AsyncMock(return_value=True)
    body = _identified("cancel", {})

    response = await api_spawn_delete(
        _Request(manager, body, agent_id="queued-run")  # type: ignore[arg-type]
    )

    assert response.status == 200
    assert _response_json(response)["cancelled"] is True
    manager.cancel.assert_not_called()


@pytest.mark.parametrize("body", [ValueError("malformed JSON"), ["not", "an", "object"]])
@pytest.mark.asyncio
async def test_malformed_cancel_body_does_not_execute_legacy_cancel(body: object) -> None:
    manager = MagicMock()
    manager._agents = {"queued-run": object()}
    manager.cancel = AsyncMock(return_value=True)

    response = await api_spawn_delete(
        _Request(manager, body, agent_id="queued-run")  # type: ignore[arg-type]
    )

    assert response.status == 400
    assert _response_json(response)["code"] in {"invalid_json", "body_not_object"}
    manager.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_cancel_body_stays_on_legacy_cancel_path() -> None:
    manager = MagicMock()
    manager._agents = {"queued-run": object()}
    manager.cancel = AsyncMock(return_value=True)

    response = await api_spawn_delete(
        _Request(manager, {}, agent_id="queued-run", can_read_body=False)  # type: ignore[arg-type]
    )

    assert response.status == 200
    assert _response_json(response)["cancelled"] is True
    manager.cancel.assert_awaited_once_with("queued-run")
