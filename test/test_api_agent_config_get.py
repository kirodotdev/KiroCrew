"""GET /api/agent/config: which file it serves, and that it reads it off the loop.

The handler prefers the installed spec (``~/.kiro/agents/kirocrew.json``, what
kiro-cli reads), falls back to ``agents/defaults.json``, and answers ``{}`` when
neither exists. The stats (including resolving the defaults path) and the read
run in a worker thread so a slow disk cannot stall the gateway's event loop.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import agents, api_agent_config


async def _get(installed: Path, defaults: Path) -> Any:
    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
    ):
        response = await api_agent_config(make_mocked_request("GET", "/api/agent/config"))
    assert response.status == 200
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_get_serves_the_installed_spec_when_present(tmp_path: Path) -> None:
    spec = {"name": "kirocrew", "tools": ["a"], "mcpServers": {"x": {"command": "x"}}}
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps(spec), encoding="utf-8")
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"name": "defaults"}), encoding="utf-8")

    assert await _get(installed, defaults) == spec


@pytest.mark.asyncio
async def test_get_falls_back_to_defaults_without_an_installed_spec(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"name": "defaults", "tools": ["b"]}), encoding="utf-8")

    assert await _get(tmp_path / "missing.json", defaults) == {"name": "defaults", "tools": ["b"]}


@pytest.mark.asyncio
async def test_get_answers_empty_when_neither_file_exists(tmp_path: Path) -> None:
    assert await _get(tmp_path / "missing.json", tmp_path / "also-missing.json") == {}


@pytest.mark.asyncio
async def test_get_reads_the_spec_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    real_loads = agents.loads_user_json
    readers: list[int] = []

    def _spy(text: str) -> Any:
        readers.append(threading.get_ident())
        return real_loads(text)

    monkeypatch.setattr(agents, "loads_user_json", _spy)

    assert await _get(installed, tmp_path / "defaults.json") == {"name": "kirocrew"}
    assert readers and threading.get_ident() not in readers


@pytest.mark.asyncio
async def test_get_resolves_the_defaults_path_off_the_event_loop_thread(tmp_path: Path) -> None:
    # Resolving defaults.json stats the KIROCREW_PROJECT_DIR override, so it
    # belongs in the worker thread too -- and only when the installed spec is missing.
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"name": "defaults"}), encoding="utf-8")
    resolvers: list[int] = []

    def _resolve() -> Path:
        resolvers.append(threading.get_ident())
        return defaults

    with (
        patch(
            "kiro_crew.dashboard.handlers._installed_agent_config",
            return_value=tmp_path / "missing.json",
        ),
        patch("kiro_crew.dashboard.handlers._find_agent_config", side_effect=_resolve),
    ):
        response = await api_agent_config(make_mocked_request("GET", "/api/agent/config"))

    assert json.loads(response.body) == {"name": "defaults"}
    assert resolvers and threading.get_ident() not in resolvers


@pytest.mark.asyncio
async def test_get_answers_a_coded_503_when_the_spec_stays_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows a writer's os.replace makes the open fail with PermissionError.
    # Once the retry budget is spent the GET answers a retryable 503 instead of
    # a bare 500 from the uncaught exception.
    from kiro_crew import atomic_write

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")

    def _contended(path: Any, **kwargs: Any) -> bytes:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(atomic_write, "read_bytes_with_retry", _contended)
    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch(
            "kiro_crew.dashboard.handlers._find_agent_config",
            return_value=tmp_path / "defaults.json",
        ),
    ):
        response = await api_agent_config(make_mocked_request("GET", "/api/agent/config"))

    assert response.status == 503
    assert json.loads(response.body)["code"] == "config_busy"
