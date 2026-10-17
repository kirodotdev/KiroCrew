"""Task routes do not load the optional queue during dashboard route setup."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


@pytest.mark.parametrize("module", ["kiro_crew.dashboard.handlers", "kiro_crew.subagent_manager"])
def test_import_does_not_load_task_queue(tmp_path, module):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["KIROCREW_HOME"] = str(tmp_path / "crew")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import importlib, sys; importlib.import_module(sys.argv[1]); "
            "assert 'kiro_crew.dashboard.handlers.tasks' not in sys.modules; "
            "assert 'kiro_crew.taskq' not in sys.modules; "
            "assert 'kiro_crew.taskq.store' not in sys.modules",
            module,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "api_tasks_list",
        "api_tasks_summary",
        "api_task_detail",
        "api_task_cancel",
        "api_task_action",
    ],
)
async def test_task_route_wrapper_calls_real_handler(monkeypatch, name):
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.handlers import tasks

    request = make_mocked_request("GET", "/api/tasks")
    response = web.json_response({"ok": True})
    handler = AsyncMock(return_value=response)
    monkeypatch.setattr(tasks, name, handler)
    assert await getattr(handlers, name)(request) is response
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_lazy_task_list_serves_disabled_response():
    from kiro_crew.dashboard import handlers

    app = web.Application()
    app["state"] = SimpleNamespace(subagents=None)
    response = await handlers.api_tasks_list(make_mocked_request("GET", "/api/tasks", app=app))
    assert response.status == 200
    assert json.loads(response.text)["available"] is False


def test_lazy_slack_probe_keeps_the_original_binding(monkeypatch):
    from unittest.mock import Mock

    from kiro_crew.dashboard.handlers import files
    from kiro_crew.slack import handler

    probe = Mock(return_value=True)
    monkeypatch.setattr(handler, "is_tracked_channel", probe)
    assert files.is_tracked_channel("test-channel") is True
    probe.assert_called_once_with("test-channel")
