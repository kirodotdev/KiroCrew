"""KAS moves off an ``auto`` default its own ``model`` select does not list.

KAS sends no ``models`` object on ``session/new``; its list is only the
``configOptions`` ``model`` select, and it defaults a new session to ``auto``.
On an account that does not serve ``auto`` every prompt then fails. These tests
lock in the narrow fix: the select answers only "is this ``auto`` unlisted?",
and never becomes the advertised list the picker and pick guard narrow by.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.acp.session_handle import (
    AcpSessionHandle,
    WatchdogSettings,
    model_select_envelope,
    models_from_config_options,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kas" / "session.jsonl"


def _make_handle(backend: str = ACP_BACKEND_KAS) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    runtime.send_notification = AsyncMock()
    handle = AcpSessionHandle(
        session_id="sess-kas",
        queue=asyncio.Queue(),
        runtime=runtime,
        watchdog=WatchdogSettings(),
    )
    handle.set_config_option = AsyncMock()  # type: ignore[method-assign]
    return handle


def _kas_session_new(values: list[str], current: str) -> dict:
    """A KAS ``session/new`` result: no ``models``, list only in the select."""
    return {
        "sessionId": "sess-kas",
        "configOptions": [
            {
                "type": "select",
                "id": "model",
                "name": "Model",
                "category": "model",
                "currentValue": current,
                "options": [{"value": v, "name": v, "description": ""} for v in values],
            }
        ],
    }


def _recorded_session_new() -> dict:
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        result = frame.get("result") if isinstance(frame, dict) else None
        if isinstance(result, dict) and "sessionId" in result:
            return result
    raise AssertionError("no session/new result in the KAS fixture")


def _stored(handle: AcpSessionHandle, resp: dict) -> AcpSessionHandle:
    handle.store_session_config(resp)
    asyncio.run(handle.ensure_served_default())
    return handle


def test_recorded_kas_session_is_left_on_its_listed_auto() -> None:
    resp = _recorded_session_new()
    assert resp.get("models") is None
    handle = _stored(_make_handle(), resp)
    assert handle._kas_auto_fallback == ""
    assert handle.available_models == []
    handle.set_config_option.assert_not_awaited()  # type: ignore[attr-defined]


def test_unlisted_auto_default_moves_to_a_listed_model() -> None:
    handle = _stored(_make_handle(), _kas_session_new(["claude-sonnet-4.5", "x"], "auto"))
    handle.set_config_option.assert_awaited_once_with(  # type: ignore[attr-defined]
        "model", "claude-sonnet-4.5"
    )
    # Intent stays "inherit"; only what the session runs changed.
    assert handle._model in ("", "auto")


def test_select_never_becomes_the_advertised_list() -> None:
    handle = _stored(_make_handle(), _kas_session_new(["claude-sonnet-4.5"], "auto"))
    assert handle.available_models == []
    assert handle._advertised_model_ids() == []
    assert models_from_config_options(_kas_session_new(["a"], "a"), ACP_BACKEND_KAS) is None


def test_concrete_default_is_never_judged_against_the_select() -> None:
    handle = _stored(_make_handle(), _kas_session_new(["claude-sonnet-4.5"], "other-model"))
    assert handle._kas_auto_fallback == ""
    # No switch was made: the session still runs whatever the backend chose,
    # and that choice is what it reports serving.
    assert handle._resolved_model_id == "other-model"
    handle.set_config_option.assert_not_awaited()  # type: ignore[attr-defined]


def test_kiro_ignores_the_raw_select() -> None:
    handle = _make_handle(ACP_BACKEND_KIRO)
    handle.store_session_config(_kas_session_new(["claude-sonnet-4.5"], "auto"))
    assert handle.available_models == []
    resolved_before = handle._resolved_model_id
    handle._runtime.send_request.reset_mock()  # type: ignore[attr-defined]
    asyncio.run(handle.ensure_served_default())
    assert handle._resolved_model_id == resolved_before
    handle.set_config_option.assert_not_awaited()  # type: ignore[attr-defined]
    handle._runtime.send_request.assert_not_awaited()  # type: ignore[attr-defined]


def test_model_select_envelope_reads_values_and_current() -> None:
    env = model_select_envelope(_kas_session_new(["a", "b"], "b"))
    assert env is not None
    assert [m["modelId"] for m in env["availableModels"]] == ["a", "b"]
    assert env["currentModelId"] == "b"
    assert model_select_envelope({"configOptions": []}) is None


def test_refused_switch_does_not_fail_the_session_start() -> None:
    from kiro_crew.acp.session_handle import AcpError

    handle = _make_handle()
    handle.set_config_option = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpError("refused")
    )
    handle.store_session_config(_kas_session_new(["claude-sonnet-4.5"], "auto"))
    intent = handle._model
    asyncio.run(handle.ensure_served_default())  # must not raise
    handle.set_config_option.assert_awaited_once()  # type: ignore[attr-defined]
    # The refused switch leaves the session on its backend default and its intent intact.
    assert handle._resolved_model_id == "auto"
    assert handle.model_pin_refused == ""
    assert handle._model == intent


def test_auto_listed_late_in_a_long_select_is_still_seen() -> None:
    values = [f"m{i}" for i in range(400)] + ["auto"]
    handle = _stored(_make_handle(), _kas_session_new(values, "auto"))
    assert handle._kas_auto_fallback == ""
    handle.set_config_option.assert_not_awaited()  # type: ignore[attr-defined]


def test_non_string_select_ids_are_skipped() -> None:
    resp = _kas_session_new(["ok"], "auto")
    resp["configOptions"][0]["options"].insert(0, {"value": 7, "name": "n"})
    handle = _make_handle()
    handle.store_session_config(resp)
    assert handle._kas_auto_fallback == "ok"


# ── A KAS turn is recorded with the model it ran on ──


def test_new_kas_session_on_auto_serves_auto() -> None:
    from kiro_crew.dashboard.handlers.usage import _resolve_model, read_turn_model

    handle = _stored(_make_handle(), _kas_session_new(["auto", "claude-sonnet-4.5"], "auto"))
    assert handle.served_model == "auto"
    assert read_turn_model(handle) == "auto"
    # The caller-supplied blank model is what a default chat turn passes.
    assert _resolve_model("", handle) == "auto"


def test_kas_session_on_a_concrete_model_records_that_model() -> None:
    from kiro_crew.dashboard.handlers.usage import _resolve_model

    handle = _stored(_make_handle(), _kas_session_new(["claude-sonnet-4.5"], "claude-sonnet-4.5"))
    assert handle.served_model == "claude-sonnet-4.5"
    assert _resolve_model("", handle) == "claude-sonnet-4.5"


def test_recorded_kas_session_reports_its_current_model() -> None:
    resp = _recorded_session_new()
    select = model_select_envelope(resp) or {}
    current = select.get("currentModelId")
    assert isinstance(current, str) and current
    handle = _stored(_make_handle(), resp)
    assert handle.served_model == current


def test_unlisted_auto_records_the_model_it_switched_to() -> None:
    handle = _stored(_make_handle(), _kas_session_new(["claude-sonnet-4.5", "x"], "auto"))
    assert handle.served_model == "claude-sonnet-4.5"


def test_kas_models_block_still_wins_over_the_select() -> None:
    resp = _kas_session_new(["claude-sonnet-4.5"], "auto")
    resp["models"] = {
        "currentModelId": "claude-opus-4.5",
        "availableModels": [{"modelId": "claude-opus-4.5", "name": "Opus"}],
    }
    handle = _make_handle()
    handle.store_session_config(resp)
    assert handle.served_model == "claude-opus-4.5"


def test_kiro_does_not_adopt_the_raw_select_as_served() -> None:
    handle = _make_handle(ACP_BACKEND_KIRO)
    handle.store_session_config(_kas_session_new(["claude-sonnet-4.5"], "claude-sonnet-4.5"))
    assert handle.served_model == ""
