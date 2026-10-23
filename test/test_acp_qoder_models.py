"""Qoder's picker is its advertised service catalog, not Crew's registry.

The two halves are measured separately: `session/new` carries the labels and
current id, while `session/set_config_option("model", ...)` is the write channel.
These tests deliberately use the reduced historical capture for the two-model
shape and the supported 1.1.67 capture for the live response/write shape.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, refresh_exclusive_catalog
from kiro_crew.acp.transport_errors import AcpModelUnavailable
from kiro_crew.acp.types import METHOD_PROMPT  # noqa: F401
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_QODER,
    ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents

QODER_CORPUS = Path(__file__).parents[1] / "test/fixtures/acp_frames/qoder"


@pytest.fixture(autouse=True)
def _cold_qoder_model_cache(_floor_monkeypatch):
    """Advertised ids are per-account; a cache left by another test is not evidence."""
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODEL_ROWS", {})
    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS_OBSERVED_AT", {})
    _floor_monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _session_response(fixture: str, request_id: int = 3) -> dict:
    frames = [
        json.loads(line)
        for line in (QODER_CORPUS / fixture).read_text(encoding="utf-8").splitlines()
    ]
    response = next(
        frame["result"]
        for frame in frames
        if frame.get("id") == request_id and "method" not in frame
    )
    assert response["models"]["currentModelId"] in {
        model["modelId"] for model in response["models"]["availableModels"]
    }
    return response


def _qoder_client(tmp_path, fixture: str, *, request_id: int = 3) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._session_id = "qoder-session-1"
    client._model = ""
    client._capture_available_models(_session_response(fixture, request_id))
    return client


def _request(*providers) -> MagicMock:
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    }
    return request


def _provider(client: AcpClient) -> MagicMock:
    provider = MagicMock()
    provider.capabilities = capabilities_for(ACP_BACKEND_QODER)
    provider.available_models = MagicMock(return_value=client.available_models())
    return provider


def _names(rows: list[dict]) -> list[str]:
    return [row["model_name"] for row in rows]


def test_qoder_is_an_exclusive_advertised_model_member() -> None:
    assert ACP_BACKEND_QODER in ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE
    caps = capabilities_for(ACP_BACKEND_QODER)
    assert caps.resolves_model_from_advertised_list is True
    assert caps.advertised_models_are_exclusive is True
    assert model_registry_namespace(ACP_BACKEND_QODER) == "qoder"
    assert model_registry_namespace(ACP_BACKEND_KIRO) != "qoder"


def test_picker_uses_the_historical_two_model_catalog_with_labels(tmp_path) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    rows = agents._advertised_backend_models(_request(_provider(client)), ACP_BACKEND_QODER)

    assert _names(rows) == ["qmodel_38max", "qfmodel"]
    assert [row["display_name"] for row in rows] == [
        "Qwen3.8-Max (default)",
        "Qwen3.8-Flash",
    ]
    assert all(row["advertised_models_exclusive"] is True for row in rows)
    assert rows[0]["description"] == "Reasoning · Vision · New · 0.20x Credit"
    assert rows[1]["description"] == "Reasoning · Vision · New · 0.00x Credit"


def test_v167_capture_serves_current_model_and_its_own_cache(tmp_path) -> None:
    client = _qoder_client(tmp_path, "v167-handshake-live.jsonl")

    assert client._resolved_model_id == "qfmodel"
    assert model_registry.advertised_models("qoder") == ["qfmodel"]
    assert model_registry.advertised_models("acp") == []

    rows = agents._advertised_backend_models(_request(_provider(client)), ACP_BACKEND_QODER)
    assert _names(rows) == ["qfmodel"]


def test_qoder_capture_of_an_unchanged_catalog_requests_a_stamp_persist(
    tmp_path, monkeypatch
) -> None:
    client = _qoder_client(tmp_path, "v167-catalog-live.jsonl", request_id=2)
    client._advertised_models_changed = False
    monkeypatch.setitem(model_registry._ADVERTISED_MODELS_OBSERVED_AT, "qoder", 1)

    client._capture_available_models(_session_response("v167-catalog-live.jsonl", 2))

    assert client._advertised_models_changed is True
    assert model_registry.advertised_models_observed_at("qoder") > 1


def test_cold_exclusive_picker_offers_only_cached_service_rows() -> None:
    rows = agents._advertised_backend_models(
        _request(), ACP_BACKEND_QODER, configured_default="opus-4.8"
    )
    assert rows == []

    model_registry.refresh_advertised_model_rows(
        "qoder",
        [{"modelId": "qfmodel", "name": "Qwen3.8-Flash", "description": "0.00x Credit"}],
        observed_at=1,
    )
    rows = agents._advertised_backend_models(
        _request(), ACP_BACKEND_QODER, configured_default="opus-4.8"
    )
    assert _names(rows) == ["qfmodel"]
    assert rows[0]["display_name"] == "Qwen3.8-Flash"
    assert rows[0]["description"] == "0.00x Credit"


def test_exclusive_live_session_decides_the_ids_and_the_rows_supply_labels() -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder",
        [
            {"modelId": "qfmodel", "name": "Qwen3.8-Flash", "description": "0.00x Credit"},
            {"modelId": "stale", "name": "Stale", "description": "gone"},
        ],
        observed_at=1,
    )
    provider = MagicMock()
    provider.capabilities = capabilities_for(ACP_BACKEND_QODER)
    provider.available_models = MagicMock(
        return_value=[{"modelId": "qfmodel"}, {"modelId": "fresh-id"}]
    )

    rows = agents._advertised_backend_models(_request(provider), ACP_BACKEND_QODER)

    assert _names(rows) == ["qfmodel", "fresh-id"]
    assert rows[0]["display_name"] == "Qwen3.8-Flash"
    assert rows[0]["description"] == "0.00x Credit"
    assert rows[1]["display_name"] == "fresh-id"
    assert rows[1]["description"] == ""


def test_non_exclusive_cold_picker_keeps_the_id_as_label_despite_persisted_names() -> None:
    namespace = model_registry_namespace(ACP_BACKEND_CODEX)
    model_registry.refresh_advertised_model_rows(
        namespace,
        [{"modelId": "gpt-5.5", "name": "GPT-5.5", "description": "Flagship"}],
        observed_at=1,
    )

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_CODEX)

    row = next(row for row in rows if row["model_name"] == "gpt-5.5")
    assert row["display_name"] == "gpt-5.5"
    assert row["description"] == ""


@pytest.mark.asyncio
async def test_set_model_sends_the_measured_config_option(tmp_path) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client._send_request = AsyncMock(return_value=11)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock()  # type: ignore[method-assign]

    await client.set_model("qfmodel")

    client._send_request.assert_awaited_once_with(
        "session/set_config_option",
        {
            "sessionId": "qoder-session-1",
            "configId": "model",
            "value": "qfmodel",
        },
    )
    assert client._model == "qfmodel"
    assert client._resolved_model_id == "qfmodel"


@pytest.mark.asyncio
async def test_unknown_registry_model_is_refused_before_the_wire(tmp_path) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client.refresh_available_models = AsyncMock(
        return_value=[
            {"modelId": "qmodel_38max", "name": "Qwen3.8-Max", "description": ""},
            {"modelId": "qfmodel", "name": "Qwen3.8-Flash", "description": ""},
        ]
    )  # type: ignore[method-assign]
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as refused:
        await client.set_model("opus-4.8-1m")

    assert refused.value.advertised == ["qmodel_38max", "qfmodel"]
    client._send_request.assert_not_awaited()
    client.refresh_available_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_on_the_served_default_does_not_write(tmp_path) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    await client.set_model("auto")

    client._send_request.assert_not_awaited()
    assert client._model == "auto"
    assert client._resolved_model_id == "qmodel_38max"


@pytest.mark.asyncio
async def test_auto_after_an_explicit_switch_writes_the_response_default_once(tmp_path) -> None:
    """``auto`` restores session/new's model, not the most recently picked id."""
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client._send_request = AsyncMock(return_value=11)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock()  # type: ignore[method-assign]

    await client.set_model("qfmodel")
    await client.set_model("auto")

    assert [call.args[1]["value"] for call in client._send_request.await_args_list] == [
        "qfmodel",
        "qmodel_38max",
    ]
    assert client._model == "qmodel_38max"
    assert client._resolved_model_id == "qmodel_38max"
    assert client._qoder_default_model_id == "qmodel_38max"


@pytest.mark.asyncio
async def test_startup_applies_an_advertised_qoder_pin_through_the_config_option(
    tmp_path,
) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client._model = "qfmodel"
    client._send_request = AsyncMock(return_value=12)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock()  # type: ignore[method-assign]

    await client._apply_startup_model()

    client._send_request.assert_awaited_once_with(
        "session/set_config_option",
        {
            "sessionId": "qoder-session-1",
            "configId": "model",
            "value": "qfmodel",
        },
    )


@pytest.mark.asyncio
async def test_startup_ignores_a_registry_pin_absent_from_qoders_catalog(
    tmp_path,
) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client._model = "opus-4.8-1m"
    client.refresh_available_models = AsyncMock(return_value=client.available_models())  # type: ignore[method-assign]
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    await client._apply_startup_model()

    client._send_request.assert_not_awaited()
    assert client._model == "auto"
    # model_scope withheld this as another harness's pin before the exclusive
    # guard ran; it is already treated as inherit-the-default, not a refusal.
    assert client.model_pin_refused == ""
    assert client._resolved_model_id == "qmodel_38max"


@pytest.mark.asyncio
async def test_startup_does_not_forward_a_concrete_pin_without_a_qoder_catalog(
    tmp_path,
) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._session_id = "qoder-session-1"
    client._model = "unknown-but-real"
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    await client._apply_startup_model()

    client._send_request.assert_not_awaited()
    assert client._model == "auto"
    assert client.model_pin_refused == "unknown-but-real"


@pytest.mark.asyncio
async def test_explicit_switch_fails_closed_without_an_advertised_catalog(
    tmp_path,
) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._session_id = "qoder-session-1"
    client.refresh_available_models = AsyncMock(return_value=[])  # type: ignore[method-assign]
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable):
        await client.set_model("unknown-but-real")

    client.refresh_available_models.assert_awaited_once()

    client._send_request.assert_not_awaited()


@pytest.fixture(autouse=True)
def _cold_qoder_catalog_refresh(_floor_monkeypatch):
    """One catalog-refresh attempt per test; no state leaks between endpoint calls."""
    _floor_monkeypatch.setattr(
        agents, "_QODER_CATALOG_REFRESH_STATE", {"task": None, "started_at": 0.0}
    )


def test_live_v167_catalog_has_17_labelled_rows_and_exactly_one_auto(tmp_path) -> None:
    client = _qoder_client(tmp_path, "v167-catalog-live.jsonl", request_id=2)
    rows = agents._advertised_backend_models(_request(_provider(client)), ACP_BACKEND_QODER)

    assert len(rows) == 17
    assert _names(rows).count("auto") == 1
    auto = rows[0]
    assert auto["display_name"] == "Auto (default)"
    assert "0.50x Credit" in auto["description"]
    flash = next(row for row in rows if row["model_name"] == "qfmodel")
    assert flash["display_name"] == "Qwen3.8-Flash"
    assert flash["description"] == "Reasoning · Vision · New · 0.00x Credit"


@pytest.mark.asyncio
async def test_unknown_id_refreshes_once_and_accepts_the_newly_advertised_id(tmp_path) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client.refresh_available_models = AsyncMock(
        return_value=[
            {"modelId": "qmodel_38max", "name": "Max"},
            {"modelId": "qfmodel", "name": "Flash"},
            {"modelId": "newmodel", "name": "New"},
        ]
    )  # type: ignore[method-assign]
    client._send_request = AsyncMock(return_value=11)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock()  # type: ignore[method-assign]

    await client.set_model("newmodel")

    client.refresh_available_models.assert_awaited_once()
    client._send_request.assert_awaited_once_with(
        "session/set_config_option",
        {"sessionId": "qoder-session-1", "configId": "model", "value": "newmodel"},
    )


@pytest.mark.asyncio
async def test_unknown_id_refreshes_once_then_refuses_when_the_answer_does_not_name_it(
    tmp_path,
) -> None:
    client = _qoder_client(tmp_path, "handshake-live.jsonl")
    client.refresh_available_models = AsyncMock(
        return_value=[{"modelId": "qfmodel", "name": "Flash"}]
    )  # type: ignore[method-assign]
    client._send_request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as refused:
        await client.set_model("removedmodel")

    assert refused.value.advertised == ["qmodel_38max", "qfmodel"]
    client.refresh_available_models.assert_awaited_once()
    client._send_request.assert_not_awaited()


def _probe_client(tmp_path: Path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, model=None, acp_backend=ACP_BACKEND_QODER)
    client._spawn = AsyncMock()  # type: ignore[method-assign]
    client._send_request = AsyncMock(side_effect=[1, 2])  # type: ignore[method-assign]
    init = {
        "protocolVersion": 1,
        "agentInfo": {"version": "1.1.67"},
        "agentCapabilities": {"loadSession": True},
    }
    session = {
        "sessionId": "probe-session",
        "modes": {"currentModeId": "default"},
        "models": {"availableModels": [{"modelId": "newmodel", "name": "New"}]},
    }
    client._wait_for_response = AsyncMock(side_effect=[init, session])  # type: ignore[method-assign]
    client._verify_qoder_version = MagicMock()  # type: ignore[method-assign]
    client._verify_qoder_routing = MagicMock()  # type: ignore[method-assign]
    client.shutdown = AsyncMock()  # type: ignore[method-assign]
    client._entitlement_probe_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_qoder_catalog_probe_reads_session_new_without_prompt_or_teardown(
    tmp_path,
) -> None:
    client = _probe_client(tmp_path)

    fresh, _answered_at = await client._probe_advertised_models()

    assert [row["modelId"] for row in fresh] == ["newmodel"]
    methods = [call.args[0] for call in client._send_request.await_args_list]
    assert methods == ["initialize", "session/new"]
    assert "authenticate" not in methods
    assert "session/prompt" not in methods
    assert "session/close" not in methods
    client._verify_qoder_version.assert_called_once_with()
    client._verify_qoder_routing.assert_called_once()
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_exclusive_catalog_ingests_nonempty_and_persists(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        AcpClient,
        "_entitlement_probe_answer",
        AsyncMock(return_value=([{"modelId": "newmodel", "name": "New"}], 1.0)),
    )
    shutdown = AsyncMock()
    monkeypatch.setattr(AcpClient, "shutdown", shutdown)
    persisted: list[bool] = []

    def persist() -> None:
        persisted.append(True)

    monkeypatch.setattr(model_registry, "persist_advertised_models", persist)
    rows = await refresh_exclusive_catalog(ACP_BACKEND_QODER, timeout=1.0)

    assert [row["modelId"] for row in rows] == ["newmodel"]
    assert model_registry.advertised_models("qoder") == ["newmodel"]
    assert model_registry.advertised_model_rows("qoder")[0]["name"] == "New"
    assert model_registry.advertised_models_observed_at("qoder") > 0
    assert persisted == [True]
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_of_an_unchanged_catalog_persists_its_new_stamp(
    monkeypatch, tmp_path
) -> None:
    """A TTL probe that re-reads the same rows still moves the stamp, and must persist it."""
    rows = [{"modelId": "newmodel", "name": "New"}]
    model_registry.refresh_advertised_model_rows("qoder", rows, observed_at=1)
    monkeypatch.setattr(AcpClient, "_entitlement_probe_answer", AsyncMock(return_value=(rows, 2.0)))
    monkeypatch.setattr(AcpClient, "shutdown", AsyncMock())
    persisted: list[bool] = []
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: persisted.append(True))

    await refresh_exclusive_catalog(ACP_BACKEND_QODER, timeout=1.0)

    assert persisted == [True]
    assert model_registry.advertised_models_observed_at("qoder") > 1


@pytest.mark.asyncio
async def test_refresh_failure_keeps_the_old_snapshot(monkeypatch, tmp_path) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder", [{"modelId": "old", "name": "Old"}], observed_at=1
    )
    monkeypatch.setattr(
        AcpClient, "_entitlement_probe_answer", AsyncMock(side_effect=RuntimeError("boom"))
    )
    shutdown = AsyncMock()
    monkeypatch.setattr(AcpClient, "shutdown", shutdown)

    rows = await refresh_exclusive_catalog(ACP_BACKEND_QODER, timeout=1.0)

    assert rows == []
    assert model_registry.advertised_models("qoder") == ["old"]
    assert model_registry.advertised_models_observed_at("qoder") == 1
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_timeout_kills_the_probe_child_and_keeps_the_snapshot(
    monkeypatch, tmp_path
) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder", [{"modelId": "old", "name": "Old"}], observed_at=1
    )

    _held_probe = asyncio.Event()

    async def slow_probe() -> tuple[list[dict[str, str]], float]:
        await _held_probe.wait()
        return [{"modelId": "new", "name": "New"}], 2.0

    monkeypatch.setattr(AcpClient, "_entitlement_probe_answer", AsyncMock(side_effect=slow_probe))
    shutdown = AsyncMock()
    monkeypatch.setattr(AcpClient, "shutdown", shutdown)

    rows = await refresh_exclusive_catalog(ACP_BACKEND_QODER, timeout=0.01)

    assert rows == []
    assert model_registry.advertised_models("qoder") == ["old"]
    shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_picker_ttl_refreshes_stale_catalog_and_retry_gate_prevents_a_second_probe(
    monkeypatch,
) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder", [{"modelId": "old", "name": "Old"}], observed_at=1
    )

    async def refresh(_backend: str, **_kwargs) -> list[dict[str, str]]:
        rows = [{"modelId": "new", "name": "New"}]
        model_registry.refresh_advertised_model_rows("qoder", rows, observed_at=2)
        return rows

    refresh = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(agents, "refresh_exclusive_catalog", refresh)

    await agents._maybe_refresh_qoder_catalog(ACP_BACKEND_QODER)
    assert model_registry.advertised_models("qoder") == ["new"]
    refresh.assert_awaited_once()

    # A TTL-fresh answer is a no-op, even after the prior attempt gate cleared.
    model_registry._ADVERTISED_MODELS_OBSERVED_AT["qoder"] = time.time_ns()
    await agents._maybe_refresh_qoder_catalog(ACP_BACKEND_QODER)
    refresh.assert_awaited_once()


def test_api_models_answers_from_stale_rows_when_refresh_fails(monkeypatch) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder", [{"modelId": "old", "name": "Old"}], observed_at=1
    )

    async def fail(*_args, **_kwargs) -> list[dict[str, str]]:
        return []

    agents._QODER_CATALOG_REFRESH_STATE.update({"task": None, "started_at": 0.0})
    monkeypatch.setattr(agents, "refresh_exclusive_catalog", AsyncMock(side_effect=fail))
    _ = asyncio.run(agents._maybe_refresh_qoder_catalog(ACP_BACKEND_QODER))
    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_QODER)

    assert _names(rows) == ["old"]
    assert rows[0]["display_name"] == "Old"


@pytest.mark.asyncio
async def test_failed_refresh_gate_defers_the_next_probe(monkeypatch) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder", [{"modelId": "old", "name": "Old"}], observed_at=1
    )
    refresh = AsyncMock(return_value=[])
    monkeypatch.setattr(agents, "refresh_exclusive_catalog", refresh)

    await agents._maybe_refresh_qoder_catalog(ACP_BACKEND_QODER)
    await agents._maybe_refresh_qoder_catalog(ACP_BACKEND_QODER)

    assert model_registry.advertised_models("qoder") == ["old"]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_removes_a_model_that_the_service_no_longer_advertises(
    monkeypatch, tmp_path
) -> None:
    model_registry.refresh_advertised_model_rows(
        "qoder",
        [{"modelId": "old", "name": "Old"}, {"modelId": "kept", "name": "Kept"}],
        observed_at=1,
    )
    monkeypatch.setattr(
        AcpClient,
        "_entitlement_probe_answer",
        AsyncMock(return_value=([{"modelId": "kept", "name": "Kept"}], 2.0)),
    )
    monkeypatch.setattr(AcpClient, "shutdown", AsyncMock())
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)

    rows = await refresh_exclusive_catalog(ACP_BACKEND_QODER, timeout=1.0)

    assert model_registry.advertised_models("qoder") == ["kept"]
    assert [row["modelId"] for row in rows] == ["kept"]


def test_qoder_rows_report_unknown_context_window(tmp_path) -> None:
    client = _qoder_client(tmp_path, "v167-catalog-live.jsonl", request_id=2)
    rows = agents._advertised_backend_models(_request(_provider(client)), ACP_BACKEND_QODER)

    assert len(rows) == 17
    assert [row["context_window"] for row in rows] == [None] * 17


def test_non_exclusive_harness_keeps_a_positive_context_window() -> None:
    model_registry.refresh_advertised_models(
        model_registry_namespace(ACP_BACKEND_CODEX), ["gpt-5.5"]
    )

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_CODEX)

    assert _names(rows) == ["auto", "gpt-5.5"]
    assert all(row["context_window"] > 0 for row in rows)
