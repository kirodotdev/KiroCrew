"""The ACP-layer pieces Kiro Workflows reaches through the agent SDK."""

from __future__ import annotations

import pytest

from kiro_crew.acp import elicitation as el
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.agent_sdk import kas_workflows_driver as driver
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_query_runtime_is_a_session_less_kas_process(monkeypatch):
    spawned: list[object] = []

    async def _spawn(self):
        spawned.append(self)

    monkeypatch.setattr(runtime_mod.AcpRuntime, "spawn", _spawn)
    runtime = await driver.spawn_query_runtime("workspace-write")
    assert spawned == [runtime]
    assert runtime.acp_backend == ACP_BACKEND_KAS
    assert runtime._expect_mcp_reports is False


def test_the_user_input_method_and_its_no_answer_replies():
    assert driver.user_input_method() == el.METHOD_KAS_USER_INPUT
    assert driver.user_input_no_answer(person=True) == {
        "action": "answered",
        "answer": el.USER_INPUT_DECLINED_ANSWER,
    }
    assert driver.user_input_no_answer(person=False) == {
        "action": "answered",
        "answer": el.USER_INPUT_NO_PERSON_ANSWER,
    }
