"""A spawn pinned to a model its session will not serve fails instead of running on the default.

A session withholds a startup pin the account is not served and stays on the backend
default without raising, recording the id in ``model_pin_refused``. A spawn that named
that model would otherwise answer as the default while its caller reads the answer as
the named model's, so a local "review on model X" became a second review by the
parent's own model. The run now stops before its prompt with ``model_unavailable``.
A configured role pin keeps the old behaviour: only the spawn's own pin is judged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager, _mock_sessions

from kiro_crew.subagent import SubagentInfo, _refused_spawn_pin

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers


def _info(model: str | None) -> SubagentInfo:
    return SubagentInfo(id="sub-1", task="review", model=model)


@pytest.mark.parametrize(
    ("model", "refused", "expected"),
    [
        ("gpt-6.1-sol", "gpt-6.1-sol", "gpt-6.1-sol"),
        ("gpt-6.1-sol", "", ""),
        (None, "claude-opus-5.5", ""),
        ("auto", "auto", ""),
        ("GPT-6.1-Sol", "gpt-6.1-sol", "gpt-6.1-sol"),
        ("kiro::gpt-6.1-sol", "gpt-6.1-sol", "gpt-6.1-sol"),
        (
            "claude-opus-4.8",
            "global.anthropic.claude-opus-4-8[1m]",
            "global.anthropic.claude-opus-4-8[1m]",
        ),
        ("claude-opus-5.5", "gpt-5.4-codex", ""),
        ("opus", "claude-opus-4.8", "claude-opus-4.8"),
        ("claude-sonnet-5.5[high]", "claude-sonnet-5.5", "claude-sonnet-5.5"),
        ("claude-opus-5.5", "claude-sonnet-5.5", ""),
        ("unknown-model-x", "unknown-model-y", ""),
    ],
    ids=[
        "pinned-and-refused",
        "pinned-and-served",
        "unpinned",
        "auto",
        "another-case",
        "namespace-qualified",
        "another-harness-spelling",
        "a-fallback-model-refused",
        "a-family-alias",
        "an-effort-suffix",
        "another-registered-model",
        "two-unregistered-models",
    ],
)
def test_only_a_refused_spawn_pin_is_reported(model, refused, expected):
    client = SimpleNamespace(model_pin_refused=refused)
    assert _refused_spawn_pin(_info(model), client) == expected


@pytest.mark.parametrize("client", [object(), SimpleNamespace(model_pin_refused=None)])
def test_a_client_without_a_string_answer_reports_nothing(client):
    assert _refused_spawn_pin(_info("gpt-6.1-sol"), client) == ""


async def _run(model: str | None, refused: str) -> tuple[SubagentInfo, object]:
    sessions = _mock_sessions()
    provider = sessions.get_or_create.return_value[0]
    provider.model_pin_refused = refused
    manager = _manager(sessions)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("review the diff", model=model)
        assert info is not None and not info.error, info.error
        await manager._tasks[info.id]
    return info, provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "refused"),
    [
        ("gpt-6.1-sol", "gpt-6.1-sol"),
        ("claude-opus-4.8", "global.anthropic.claude-opus-4-8[1m]"),
    ],
    ids=["same-spelling", "another-harness-spelling"],
)
async def test_a_refused_spawn_pin_fails_the_run_before_its_prompt(model, refused):
    info, provider = await _run(model, refused)

    assert info.done
    assert f"model_unavailable: model '{refused}' is not served" in info.error, info.error
    assert info.error_code == "model_unavailable"
    provider.stream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "refused"),
    [("gpt-5.6-sol", ""), (None, "claude-opus-5.5"), ("claude-opus-5.5", "gpt-5.4-codex")],
    ids=["served-pin", "refused-role-pin", "refused-fallback-of-a-foreign-pin"],
)
async def test_a_served_pin_or_an_unpinned_spawn_still_runs(model, refused):
    info, provider = await _run(model, refused)

    assert not info.error, info.error
    provider.stream.assert_called_once()


@pytest.mark.asyncio
async def test_a_runtime_session_refreshes_its_model_list_with_a_forced_probe():
    """The warm-pool claim asks this before it refuses a named model."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp.session_provider import AcpSessionProvider

    rows = [{"modelId": "gpt-6.1-sol"}]
    handle = SimpleNamespace(refresh_available_models=AsyncMock(return_value=rows))

    async def guarded(awaitable):
        return await awaitable

    fake = SimpleNamespace(_handle=handle, _guarded=guarded)
    assert await AcpSessionProvider.refresh_available_models(fake) == rows
    handle.refresh_available_models.assert_awaited_once_with(force=True)
