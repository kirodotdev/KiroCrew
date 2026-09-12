"""An adopted daemon must acknowledge explicit session binding before use."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd, stub


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "reconnect"])
@pytest.mark.parametrize("poolable", [False, True])
@pytest.mark.parametrize(
    "session_bound,capabilities,accepted",
    [
        (True, ["poolable_ack"], False),
        (True, ["poolable_ack", "ensure_backend"], False),
        (True, ["poolable_ack", "session_bound_ack"], True),
        (True, None, False),
        (True, "session_bound_ack", False),
        (False, ["poolable_ack"], True),
    ],
)
async def test_binding_capability_gates_traffic(
    monkeypatch, tmp_path, phase, poolable, session_bound, capabilities, accepted
) -> None:
    """No legacy generation may receive a bound caller's MCP traffic or replay."""
    payload = {
        "type": "register",
        "stub_uuid": "binding-probe",
        "session_key": "subagent:child" if session_bound else "",
        "session_bound": session_bound,
    }
    reader = asyncio.StreamReader()
    reader.feed_data(
        json.dumps({"type": "registered", "capabilities": capabilities}).encode() + b"\n"
    )
    reader.feed_eof()
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    monkeypatch.setattr(stub.transport, "connect", AsyncMock(return_value=(reader, writer)))
    fallback = MagicMock()
    monkeypatch.setattr(stub, "fallback_exec", fallback)
    replay = AsyncMock(return_value=(stub._REPLAY_OK, None, "compatible"))
    monkeypatch.setattr(stub, "_replay_initialize", replay)
    bridge = AsyncMock(return_value=0)
    monkeypatch.setattr(stub, "run_bridge", bridge)
    audit = AsyncMock()
    monkeypatch.setattr(stub, "alog_fallback", audit)

    if phase == "initial":
        monkeypatch.setattr(stub, "build_register_payload", lambda _args: payload)
        monkeypatch.setattr(stub, "configure_default_executor", lambda: None)
        monkeypatch.setattr(stub, "_install_signal_handlers", lambda *_args: None)
        argv = [
            "--server",
            "kirocrew-core",
            "--agent",
            "probe",
            "--target-command",
            "unused",
            "--socket",
            str(tmp_path / "unused"),
            "--work-dir",
            str(tmp_path),
        ]
        if poolable:
            argv.append("--poolable")
        result = await asyncio.wait_for(stub._amain(argv), timeout=5)
        assert result == (0 if accepted else 1)
        assert bridge.await_count == int(accepted)
        assert fallback.call_count == int(not accepted)
        assert audit.await_count == int(not accepted)
    else:
        result = await asyncio.wait_for(
            stub._reconnect(
                str(tmp_path / "unused"),
                payload,
                stub.StubSession(),
                asyncio.Event(),
                poolable=poolable,
                pool_label="probe:core",
            ),
            timeout=5,
        )
        assert (result is not None) is accepted
        assert replay.await_count == int(accepted)
        fallback.assert_not_called()

    # A rejection closes the connection before any MCP frame or replay; it is
    # terminal on reconnect, so no second registration is attempted either.
    writer.write.assert_called_once()
    assert json.loads(writer.write.call_args.args[0])["type"] == "register"
    assert writer.close.call_count == int(not accepted)


def test_current_daemon_advertises_session_binding() -> None:
    assert "session_bound_ack" in gatewayd.REGISTERED_CAPABILITIES
