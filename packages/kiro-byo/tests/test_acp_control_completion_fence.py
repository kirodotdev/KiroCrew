"""ACP terminal replies must never overtake the session configuration lock.

This is a regression for intermittent model->effort -32002 races on a real
full-duplex ACP peer. Every frame boundary is observed by the host client.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.protocol import EFFORT_CONFIG_ID


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path / "home"))
    model = ModelSpec(
        provider_id="test", provider_name="Test", model_id="wire-model", name="Demo",
        effort_supported_levels=("low", "high", "max"), effort_default_level="high",
        reasoning_mode="levels", reasoning_supported=True,
    )
    provider = ProviderSpec(
        id="test", name="Test", type="openai-compatible",
        base_url="https://example.invalid/v1", api_key="", models=(model,),
    )
    cfg = KiroByoConfig(providers=(provider,), default_model="Demo")
    s = AcpServer()
    s._config = lambda: cfg
    session = Session("control-1", str(tmp_path), "Demo")
    s._put_session(session)
    seen: list[tuple[dict[str, Any], bool]] = []
    def observe(frame: dict[str, Any]):
        seen.append((frame, session.turn_lock.locked()))
    s.send = observe
    yield s, seen, session
    s._executor.shutdown(wait=True)


def _response(seen, req_id):
    replies = [(frame, lock) for frame, lock in seen if frame.get("id") == req_id]
    assert len(replies) == 1
    return replies[0]


def test_successful_effort_response_is_sent_after_lock_release(server):
    s, seen, _ = server
    s._handle_session_set_config_option_request(1, {
        "sessionId": "control-1", "configId": EFFORT_CONFIG_ID, "value": "max",
    })
    reply, locked = _response(seen, 1)
    assert not locked
    assert next(option for option in reply["result"]["configOptions"]
                if option["id"] == EFFORT_CONFIG_ID)["currentValue"] == "max"


def test_invalid_effort_error_is_sent_after_lock_release(server):
    s, seen, _ = server
    s._handle_session_set_config_option_request(2, {
        "sessionId": "control-1", "configId": EFFORT_CONFIG_ID, "value": "unadvertised",
    })
    reply, locked = _response(seen, 2)
    assert not locked
    assert reply["error"]["code"] == -32602


def test_legacy_model_select_reply_is_sent_after_lock_release(server):
    s, seen, _ = server
    s._handle_session_set_model_request(3, {"sessionId": "control-1", "modelId": "Demo"})
    reply, locked = _response(seen, 3)
    assert reply["result"] == {}
    assert not locked


def test_invalid_legacy_model_error_is_sent_after_lock_release(server):
    s, seen, _ = server
    s._handle_session_set_model_request(4, {"sessionId": "control-1", "modelId": "not-a-model"})
    reply, locked = _response(seen, 4)
    assert reply["error"]["code"] == -32602
    assert not locked
