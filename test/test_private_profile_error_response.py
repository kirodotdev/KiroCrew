"""Private profile failures keep diagnostic text out of HTTP responses."""

import json
import logging
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew.dashboard.handlers import memory as handlers
from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
from kiro_crew.memory_stores import UnknownMemoryStore

env = _member_env


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
@pytest.mark.parametrize("error_type", [UnknownMemoryStore, PermissionError])
@pytest.mark.parametrize("path", ["/srv/private/member-a", r"C:\private\member-a"])
async def test_private_profile_failure_is_stable_and_keeps_safe_log_diagnostics(
    env, monkeypatch, caplog, document, error_type, path
):
    memory = await markdown_memory_for_store(env.state, "member-alice")
    before = memory.read_preferences(), memory.read_projects()
    credential = "AKIAIOSFODNN7EXAMPLE"
    writer = MagicMock(side_effect=error_type(f"identity unavailable at {path}: {credential}"))
    monkeypatch.setattr(memory, "write_private_profile_validated", writer)
    req = request(
        env,
        owner=True,
        session="dashboard:ui",
        query={"store": "member-alice"},
        body={"content": "Valid ordinary guidance"},
    ).clone(method="PUT")

    with caplog.at_level(logging.WARNING, logger=handlers.__name__):
        response = await getattr(handlers, f"api_memory_{document}")(req)

    assert response.status == 503
    assert json.loads(response.text) == {
        "error": "Private memory profile is unavailable. Check the gateway log before retrying.",
        "code": "store_unavailable",
    }
    assert path not in response.text
    assert credential not in response.text
    assert credential not in caplog.text
    assert "identity unavailable" in caplog.text
    assert error_type.__name__ in caplog.text
    assert "member-alice" in caplog.text
    writer.assert_called_once()
    assert (memory.read_preferences(), memory.read_projects()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
async def test_nonowner_cannot_reach_private_profile_writer(env, monkeypatch, document):
    memory = await markdown_memory_for_store(env.state, "member-alice")
    before = memory.read_preferences(), memory.read_projects()
    writer = MagicMock(side_effect=AssertionError("untrusted caller reached private writer"))
    monkeypatch.setattr(memory, "write_private_profile_validated", writer)
    req = request(
        env,
        query={"store": "member-alice"},
        body={"content": "Valid ordinary guidance"},
    ).clone(method="PUT")

    response = await getattr(handlers, f"api_memory_{document}")(req)

    assert response.status == 403
    writer.assert_not_called()
    assert (memory.read_preferences(), memory.read_projects()) == before
