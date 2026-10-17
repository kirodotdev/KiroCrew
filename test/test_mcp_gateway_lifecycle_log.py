"""MCP backend lifecycle log lines carry join keys.

Each lifecycle line names its backend process (``backend_generation_id``), its
launch attempt (``attempt_id``) and a bounded ``event`` / ``reason``, so the
lines of one backend can be joined without parsing their free text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway import lifecycle_log
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import PoolKey

LOGGER = "kiro_crew.mcp_gateway.backend"
LOST_SESSION = {"code": -32602, "message": "Invalid request parameters", "data": ""}
INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "kiro-cli", "version": "1.0"},
}
_HEX_ID = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture(autouse=True)
def _no_real_metrics_file(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)


def _key() -> PoolKey:
    return PoolKey(
        server_name="example-mcp",
        command_args_hash="c",
        effective_env_hash="e",
        work_dir="/nonexistent",
        binary_version="1",
        os_uid=1000,
    )


def _proc() -> MagicMock:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    proc.wait = AsyncMock(return_value=0)
    proc.stderr = None
    return proc


def _make_backend() -> Backend:
    proc = _proc()
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=_key(),
        process=cast(Any, proc),
        stdin=cast(Any, stdin),
        stdout=cast(Any, MagicMock()),
        created_at=now,
        last_used_at=now,
    )


def _frames(backend: Backend) -> list[dict]:
    out: list[dict] = []
    for call in cast(Any, backend.stdin).write.call_args_list:
        out.extend(json.loads(line) for line in call.args[0].decode().splitlines() if line)
    return out


def _line(obj: Any) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _fields(message: str) -> dict[str, str]:
    return dict(re.findall(r"\b(event|reason|backend_generation_id|attempt_id)=(\S+)", message))


def _lines_with(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, str]]:
    found = [_fields(r.getMessage()) for r in caplog.records if r.name == LOGGER]
    return [f for f in found if f.get("event") == event]


async def _send_initialize(backend: Backend, stub: str = "s1") -> str:
    await backend.attach_stub(stub)
    await backend.forward_from_stub(
        stub, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT_PARAMS}
    )
    return cast(str, _frames(backend)[0]["id"])


def test_fields_stay_inside_the_bounded_names() -> None:
    assert lifecycle_log.lifecycle_fields("initialize_started") == "event=initialize_started"
    assert (
        lifecycle_log.lifecycle_fields("not-an-event", "not-a-reason")
        == "event=unknown reason=unknown"
    )
    assert (
        lifecycle_log.lifecycle_fields("backend_gone", "deadline", generation="g1", attempt="a1")
        == "event=backend_gone reason=deadline backend_generation_id=g1 attempt_id=a1"
    )


def test_every_call_site_names_a_bounded_event_and_reason() -> None:
    # The renderer logs a typo as ``unknown`` instead of raising, so the
    # literal names at the call sites are checked here.
    root = Path(lifecycle_log.__file__).parent
    call = re.compile(
        r'(?:lifecycle_fields\(\s*|log_event=)"([a-z_]+)"(?:,\s*(?:log_reason=)?"([a-z_]+)")?'
    )
    seen: set[str] = set()
    for path in root.rglob("*.py"):
        for event, reason in call.findall(path.read_text(encoding="utf-8")):
            assert event in lifecycle_log.EVENTS, (path.name, event)
            assert not reason or reason in lifecycle_log.REASONS, (path.name, reason)
            seen.add(event)
    assert seen == lifecycle_log.EVENTS


@pytest.mark.parametrize(
    ("state", "returncode", "reason"),
    [
        ("wedged", None, "deadline"),
        ("gone", 1, "process_exit"),
        ("gone", None, "transport_closed"),
    ],
)
def test_a_heartbeat_recycle_names_why_the_backend_went(
    state: str, returncode: Optional[int], reason: str
) -> None:
    assert lifecycle_log.heartbeat_recycle_reason(state, returncode) == reason
    assert reason in lifecycle_log.REASONS


def test_every_backend_gets_its_own_random_generation_and_attempt() -> None:
    first, second = _make_backend(), _make_backend()
    ids = {
        first.backend_generation_id,
        first.attempt_id,
        second.backend_generation_id,
        second.attempt_id,
    }
    assert len(ids) == 4
    assert all(_HEX_ID.match(i) for i in ids)


@pytest.mark.asyncio
async def test_spawn_line_carries_the_ids_the_backend_keeps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    proc = _proc()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> MagicMock:
        return proc

    def _no_tmp(_digest: str) -> Any:
        raise OSError("no temp in this test")

    monkeypatch.setattr(backend_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(backend_mod, "allocate_backend_tmp", _no_tmp)
    monkeypatch.setattr(backend_mod.platform_compat, "get_process_start_id", lambda _pid: None)
    caplog.set_level(logging.INFO, logger=LOGGER)

    backend = await backend_mod.spawn_backend(
        pool_key=_key(), command="example-mcp", args=[], env={}, work_dir="/nonexistent"
    )

    [started] = _lines_with(caplog, "initialize_started")
    assert "reason" not in started
    assert started["backend_generation_id"] == backend.backend_generation_id
    assert started["attempt_id"] == backend.attempt_id


@pytest.mark.asyncio
async def test_initialize_success_names_the_same_generation_and_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    init_fid = await _send_initialize(backend)
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init_fid, "result": {"capabilities": {}}})
    )

    [ok] = _lines_with(caplog, "initialize_succeeded")
    assert ok == {
        "event": "initialize_succeeded",
        "reason": "success",
        "backend_generation_id": backend.backend_generation_id,
        "attempt_id": backend.attempt_id,
    }


@pytest.mark.asyncio
async def test_initialize_deadline_is_logged_as_a_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    await _send_initialize(backend)
    backend._cancel_init_deadline()

    await backend._init_deadline(0)

    [timed_out] = _lines_with(caplog, "initialize_timed_out")
    assert timed_out["reason"] == "deadline"
    assert timed_out["backend_generation_id"] == backend.backend_generation_id
    assert timed_out["attempt_id"] == backend.attempt_id


@pytest.mark.asyncio
async def test_a_resent_initialize_is_a_new_attempt_on_the_same_generation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    init_fid = await _send_initialize(backend)
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init_fid, "result": {"capabilities": {}}})
    )
    generation, first_attempt = backend.backend_generation_id, backend.attempt_id
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "t"}},
    )
    call_fid = _frames(backend)[-1]["id"]

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": call_fid, "error": LOST_SESSION})
    )

    # The spawn line is not captured here, so the only start is the re-send.
    [restarted] = _lines_with(caplog, "initialize_started")
    assert "reason" not in restarted
    assert restarted["backend_generation_id"] == generation
    assert restarted["attempt_id"] == backend.attempt_id != first_attempt


@pytest.mark.asyncio
async def test_a_shutdown_past_its_deadline_is_logged_as_a_forced_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    killed = asyncio.Event()

    async def _wait() -> int:
        # Exits only once the escalation has sent its kill.
        await killed.wait()
        return 0

    async def _kill(*_args: Any) -> None:
        killed.set()

    cast(Any, backend.process).wait = _wait
    monkeypatch.setattr(backend_mod.platform_compat, "kill_process_tree_async", _kill)

    await backend.shutdown(timeout=0)

    [forced] = _lines_with(caplog, "forced_exit")
    assert forced["reason"] == "deadline"
    assert forced["backend_generation_id"] == backend.backend_generation_id


@pytest.mark.asyncio
async def test_a_refused_resent_initialize_names_the_new_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    init_fid = await _send_initialize(backend)
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init_fid, "result": {"capabilities": {}}})
    )
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "t"}},
    )
    call_fid = _frames(backend)[-1]["id"]
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": call_fid, "error": LOST_SESSION})
    )
    resent_init_fid = _frames(backend)[-3]["id"]

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": resent_init_fid, "error": LOST_SESSION})
    )

    [refused] = _lines_with(caplog, "initialize_failed")
    assert refused["reason"] == "unknown"
    assert refused["attempt_id"] == backend.attempt_id
    assert refused["backend_generation_id"] == backend.backend_generation_id


@pytest.mark.asyncio
async def test_a_refused_resent_initialize_names_its_own_attempt_not_a_later_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    init_fid = await _send_initialize(backend)
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init_fid, "result": {"capabilities": {}}})
    )
    await backend.attach_stub("s2")
    call_fids = []
    for stub in ("s1", "s2"):
        await backend.forward_from_stub(
            stub,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "t"}},
        )
        call_fids.append(_frames(backend)[-1]["id"])

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": call_fids[0], "error": LOST_SESSION})
    )
    first_attempt = backend.attempt_id
    first_resent_init = _frames(backend)[-3]
    assert first_resent_init["method"] == "initialize"
    first_resent_init_fid = first_resent_init["id"]
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": call_fids[1], "error": LOST_SESSION})
    )
    assert backend.attempt_id != first_attempt

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": first_resent_init_fid, "error": LOST_SESSION})
    )

    [refused] = _lines_with(caplog, "initialize_failed")
    assert refused["attempt_id"] == first_attempt
    assert refused["backend_generation_id"] == backend.backend_generation_id


@pytest.mark.asyncio
async def test_a_wedged_backend_is_logged_as_gone_on_its_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    backend.refcount = 1
    started = time.monotonic()
    backend._pending_requests["gw-1"] = backend_mod._PendingRequest(
        stub_uuid="s1", original_id=1, method="tools/call", t_start_ms=started * 1000.0
    )
    now = started + backend_mod.HARD_WEDGE_CEILING_SECS + 1.0
    backend._last_ping_response_mono = now

    assert await backend._heartbeat_once(now) == "wedged"

    [gone] = _lines_with(caplog, "backend_gone")
    assert gone["reason"] == "deadline"
    assert gone["backend_generation_id"] == backend.backend_generation_id


@pytest.mark.asyncio
async def test_the_direct_handshake_logs_its_success_with_the_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    backend = _make_backend()
    reply = _line(
        {"jsonrpc": "2.0", "id": backend_mod._GATEWAY_INIT_ID, "result": {"capabilities": {}}}
    )
    cast(Any, backend.stdout).readuntil = AsyncMock(return_value=reply)

    await backend_mod.send_initialize(backend, timeout=5)

    [ok] = _lines_with(caplog, "initialize_succeeded")
    assert ok["reason"] == "success"
    assert ok["attempt_id"] == backend.attempt_id
