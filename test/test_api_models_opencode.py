"""Tests for the OpenCode model catalog exposed by ``GET /api/models``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import opencode
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents


def _request(*providers: object) -> MagicMock:
    state = SimpleNamespace(
        sessions=SimpleNamespace(active_providers=lambda: list(providers)),
    )
    request = MagicMock()
    request.app = {"state": state}
    return request


def _config() -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_OPENCODE))


def _body(response: object) -> object:
    return json.loads(response.body)  # type: ignore[attr-defined]


def test_opencode_model_rows_accepts_provider_ids_and_deduplicates() -> None:
    rows = opencode.model_rows(
        b"anthropic/claude-sonnet-4\nopenai/gpt-5\nanthropic/claude-sonnet-4\n"
    )

    assert [row["model_name"] for row in rows] == [
        "anthropic/claude-sonnet-4",
        "openai/gpt-5",
    ]
    assert all(row["display_name"] == row["model_name"] for row in rows)


@pytest.mark.parametrize("stdout", [b"not-qualified\n", b"provider/\n", b"/model\n"])
def test_opencode_model_rows_rejects_malformed_ids(stdout: bytes) -> None:
    with pytest.raises(ValueError, match="invalid model id"):
        opencode.model_rows(stdout)


def test_opencode_model_rows_rejects_empty_output() -> None:
    with pytest.raises(ValueError, match="empty"):
        opencode.model_rows(b"\n  \n")


@pytest.mark.asyncio
async def test_api_models_prefers_active_opencode_session() -> None:
    provider = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_OPENCODE),
        available_models=lambda: [
            {"modelId": "anthropic/claude-sonnet-4", "name": "Sonnet 4"},
        ],
    )
    request = _request(provider)
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_config()),
        patch.object(agents, "_cold_opencode_models", AsyncMock()) as cold,
        patch(
            "kiro_crew.acp.client._resolve_kiro_bin_for_spawn",
            AsyncMock(side_effect=AssertionError("OpenCode must not resolve kiro-cli")),
        ),
    ):
        response = await agents.api_models(request)

    assert response.status == 200
    assert _body(response) == [
        {"model_name": "auto", "display_name": "Auto", "description": ""},
        {
            "model_name": "anthropic/claude-sonnet-4",
            "display_name": "Sonnet 4",
            "description": "",
        },
    ]
    cold.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_models_uses_cold_opencode_catalog_before_first_session() -> None:
    request = _request()
    cold_rows = [
        {
            "model_name": "openai/gpt-5",
            "display_name": "openai/gpt-5",
            "description": "",
        }
    ]
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_config()),
        patch.object(agents, "_cold_opencode_models", AsyncMock(return_value=cold_rows)) as cold,
        patch(
            "kiro_crew.acp.client._resolve_kiro_bin_for_spawn",
            AsyncMock(side_effect=AssertionError("OpenCode must not resolve kiro-cli")),
        ),
    ):
        response = await agents.api_models(request)

    assert response.status == 200
    assert [row["model_name"] for row in _body(response)] == ["auto", "openai/gpt-5"]
    cold.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_api_models_reports_opencode_catalog_failure() -> None:
    request = _request()
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_config()),
        patch.object(
            agents,
            "_cold_opencode_models",
            AsyncMock(side_effect=RuntimeError("catalog unavailable")),
        ),
    ):
        response = await agents.api_models(request)

    assert response.status == 503
    assert _body(response) == {
        "error": "opencode model list unavailable",
        "code": "opencode_model_list_unavailable",
    }


@pytest.mark.asyncio
async def test_api_models_ignores_other_harness_advertisements() -> None:
    provider = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_KIRO),
        available_models=lambda: [{"modelId": "kiro-only-id", "name": "Kiro only"}],
    )
    request = _request(provider)
    cold_rows = [{"model_name": "provider/model", "display_name": "Model", "description": ""}]
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_config()),
        patch.object(agents, "_cold_opencode_models", AsyncMock(return_value=cold_rows)) as cold,
    ):
        response = await agents.api_models(request)

    assert response.status == 200
    assert [row["model_name"] for row in _body(response)] == ["auto", "provider/model"]
    cold.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_cold_opencode_catalog_uses_the_sdk_driver(tmp_path) -> None:
    request = _request()
    with (
        patch.object(agents, "active_project_dir", return_value=tmp_path),
        patch.object(agents, "_read_session_key", return_value="test-session"),
        patch.object(agents, "query_opencode_models", AsyncMock(return_value=[])) as query,
    ):
        assert await agents._cold_opencode_models(request) == []
    query.assert_awaited_once_with(work_dir=str(tmp_path))
