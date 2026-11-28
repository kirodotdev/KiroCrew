"""A codex helper session is titled before its first turn, a chat is not.

codex-acp generates a title after a session's first turn on an ephemeral
``thread/start`` that carries none of the session's config, so the helper's
``CODEX_CONFIG`` disable override does not reach it and codex starts the user's
``config.toml`` servers for that thread. A session with an explicit title skips
the generator, so ``CodexHarness.after_session_start`` sends codex-acp's
``/rename`` command for a helper process before ``create_session`` returns its
handle, which is before any turn can run.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_acp_codex_harness import (
    _codex_runtime,
    _codex_session_new_response,
    _ControlPlane,
    _runtime_for,
)

from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.types import ACP_BACKEND_KIRO, METHOD_PROMPT, METHOD_SESSION_CLOSE

_RENAME = {
    "sessionId": "sid-codex",
    "prompt": [{"type": "text", "text": f"/rename {CodexHarness.HELPER_SESSION_TITLE}"}],
}


class _RenameRefusingPlane(_ControlPlane):
    """codex-acp answers the ``/rename`` prompt with an error."""

    def install(self, rt, stack):
        super().install(rt, stack)
        recorded = rt._send_and_await

        async def _send_and_await(method, params, timeout=None):
            result = await recorded(method, params, timeout=timeout)
            if method == METHOD_PROMPT:
                raise RuntimeError("rename refused")
            return result

        rt._send_and_await = _send_and_await


async def _create(*, helper: bool, agent_version: str = "2.3.0", plane=None):
    rt = _codex_runtime()
    rt._agent_version = agent_version
    if helper:
        # What resolve_spawn records for a background or tool-less spawn.
        rt._harness._helper_spawn = (Path("/home"), "/w")
    plane = plane or _ControlPlane(_codex_session_new_response())
    with ExitStack() as stack:
        plane.install(rt, stack)
        await rt.create_session(cwd="/w", agent="kirocrew")
    return plane


@pytest.mark.asyncio
async def test_a_helper_session_is_renamed_before_create_session_returns():
    plane = await _create(helper=True)
    assert plane.params_for(METHOD_PROMPT) == [_RENAME]


@pytest.mark.asyncio
async def test_a_chat_session_keeps_its_generated_title():
    plane = await _create(helper=False)
    assert plane.params_for(METHOD_PROMPT) == []


@pytest.mark.asyncio
async def test_an_adapter_without_the_title_generator_is_sent_no_model_turn():
    plane = await _create(helper=True, agent_version="1.7.0")
    assert plane.params_for(METHOD_PROMPT) == []


@pytest.mark.asyncio
async def test_an_adapter_of_unknown_version_is_renamed():
    plane = await _create(helper=True, agent_version="")
    assert plane.params_for(METHOD_PROMPT) == [_RENAME]


@pytest.mark.asyncio
async def test_a_refused_rename_closes_the_helper_session():
    plane = _RenameRefusingPlane(_codex_session_new_response())
    with pytest.raises(RuntimeError, match="rename refused"):
        await _create(helper=True, plane=plane)
    assert METHOD_SESSION_CLOSE in plane.methods


@pytest.mark.asyncio
async def test_a_kiro_session_start_awaits_no_harness_check():
    """harness-parity H13: the Kiro path gains no awaited adapter step."""
    rt = _runtime_for(ACP_BACKEND_KIRO, pid=4243)
    rt._harness.after_session_start = AsyncMock()
    plane = _ControlPlane({"sessionId": "sid-kiro"})
    with ExitStack() as stack:
        plane.install(rt, stack)
        await rt.create_session(cwd="/w", agent="kirocrew")
    rt._harness.after_session_start.assert_not_awaited()
