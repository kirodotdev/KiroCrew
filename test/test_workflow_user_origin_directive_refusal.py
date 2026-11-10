"""A workflow agent step refuses a directive only a person's turn may issue, out loud.

No session applier reads a workflow step's stream, so the refusal is what tells
the step's reader that ``set_project`` changed nothing, and its SEL row records
it. Both executors are covered: the cold per-call path (``agent_exec``) and the
warm pool (``agent_pool``), including its ``session=`` bypass.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
from kiro_crew import session_directive
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent

pytestmark = pytest.mark.asyncio

_REQUESTED = "Project change requested for this session."

_MARKER = session_directive.encode(
    "set_project", {"project": "/srv/other", "clear": False}, _REQUESTED
)
# kiro-cli names a core-MCP call by the trusted ``_meta.kiro`` pair; KAS by the
# wire title alone, with the result re-serialised so no marker decodes.
_SHAPES = {
    "kiro-cli": (
        {"tool_name": "set_project", "mcp_server_name": session_directive.CORE_MCP_SERVER},
        _MARKER,
    ),
    "kas": (
        {"wire_title": f"Running: @{session_directive.CORE_MCP_SERVER}/set_project"},
        json.dumps({"response": _MARKER, "imageBase64Urls": [], "message": _MARKER}),
    ),
}


class _Provider:
    def __init__(self, key: str) -> None:
        self.key = key

    async def new_conversation(self) -> None:
        return None

    def is_process_alive(self) -> bool:
        return True

    def set_delete_delay(self, seconds: float) -> None:
        return None


class _Sessions:
    async def get_or_create(self, key: str, **_kw: Any) -> tuple[_Provider, bool, bool]:
        return _Provider(key), False, False

    def release(self, key: str, *, cleanup: bool = False) -> None:
        return None

    async def reset(self, key: str, *, ends_conversation: bool = False) -> None:
        return None

    async def end_children_for(self, key: str) -> None:
        return None

    async def run_identity_check(self) -> None:
        return None

    def is_retiring(self, key: str) -> bool:
        return False

    async def destroy(self, key: str) -> None:
        return None


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    class _SelSpy:
        def log_tool_invocation(self, **kw: Any) -> None:
            seen.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _SelSpy())
    return seen


def _stream_calling(shape: str) -> Any:
    call, output = _SHAPES[shape]

    async def stream(_provider: Any, _msg: str, *, on_event: Any = None, **_k: Any) -> str:
        if on_event is not None:
            await on_event(AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", **call))
            await on_event(
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-1",
                    tool_output=output,
                    tool_final=True,
                )
            )
        return _REQUESTED

    return stream


def _assert_refused(text: str, key: str, audits: list[dict[str, Any]]) -> None:
    assert text.startswith(
        "Error: set_project only works from a user-facing session (dashboard or a "
        "messaging channel); headless callers such as cron jobs and sub-agents are "
        f"refused (this turn is {key!r}). Nothing was changed."
    ), text
    assert text.endswith(_REQUESTED)
    directive_audits = [a for a in audits if a.get("source") == "mcp-directive"]
    assert [(a["session_key"], a["tool_name"], a["outcome"]) for a in directive_audits] == [
        (key, "set_project", "denied")
    ]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
async def test_cold_step_set_project_is_refused_and_audited_denied(
    shape: str, audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_exec, "stream_and_collect", _stream_calling(shape))
    fn = agent_exec.build_agent_fn(_Sessions(), run_id="wf_r")

    text = await fn("call set_project", {})

    _assert_refused(text, "wf:wf_r:0", audits)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
async def test_pooled_step_set_project_is_refused_and_audited_denied(
    shape: str, audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_pool, "stream_and_collect", _stream_calling(shape))
    fn, pool = agent_pool.build_pooled_agent_fn(_Sessions(), run_id="wf_p", max_workers=1)
    try:
        text = await fn("call set_project", {})
    finally:
        await pool.shutdown()

    _assert_refused(text, "wf-pool:wf_p:0", audits)


async def test_named_session_step_set_project_is_refused(
    audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_pool, "stream_and_collect", _stream_calling("kiro-cli"))
    fn, pool = agent_pool.build_pooled_agent_fn(_Sessions(), run_id="wf_n", max_workers=1)
    try:
        text = await fn("call set_project", {"session": "chain-A"})
    finally:
        await pool.shutdown()

    _assert_refused(text, "chain-A", audits)


async def test_a_step_without_a_person_only_directive_is_unchanged(
    audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stream(_provider: Any, _msg: str, *, on_event: Any = None, **_k: Any) -> str:
        if on_event is not None:
            await on_event(
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", tool_name="read_file")
            )
            await on_event(AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_final=True))
        return "plain reply"

    monkeypatch.setattr(agent_exec, "stream_and_collect", stream)
    fn = agent_exec.build_agent_fn(_Sessions(), run_id="wf_q")

    assert await fn("read a file", {}) == "plain reply"
    assert [a for a in audits if a.get("source") == "mcp-directive"] == []


def _stream_answering(answer: str) -> Any:
    stream = _stream_calling("kiro-cli")

    async def stream_answer(provider: Any, msg: str, **kw: Any) -> str:
        await stream(provider, msg, **kw)
        return answer

    return stream_answer


@pytest.mark.parametrize("executor", ["cold", "pooled"])
async def test_a_scalar_schema_step_keeps_its_answer_and_still_audits_the_refusal(
    executor: str, audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.workflows.schema import run_with_schema

    schema = {"type": "boolean"}
    opts = {"schema": schema}
    if executor == "cold":
        monkeypatch.setattr(agent_exec, "stream_and_collect", _stream_answering("true"))
        fn = agent_exec.build_agent_fn(_Sessions(), run_id="wf_s")
        key = "wf:wf_s:0"
        value = await run_with_schema(lambda p: fn(p, opts), "call set_project", schema)
    else:
        monkeypatch.setattr(agent_pool, "stream_and_collect", _stream_answering("true"))
        fn, pool = agent_pool.build_pooled_agent_fn(_Sessions(), run_id="wf_s", max_workers=1)
        key = "wf-pool:wf_s:0"
        try:
            value = await run_with_schema(lambda p: fn(p, opts), "call set_project", schema)
        finally:
            await pool.shutdown()

    # One attempt: the refusal never reached the text the schema parses.
    assert value is True
    directive_audits = [a for a in audits if a.get("source") == "mcp-directive"]
    assert [(a["session_key"], a["tool_name"], a["outcome"]) for a in directive_audits] == [
        (key, "set_project", "denied")
    ]


async def test_a_pooled_plain_step_after_a_schema_step_still_leads_with_its_refusal(
    audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_pool, "stream_and_collect", _stream_calling("kiro-cli"))
    fn, pool = agent_pool.build_pooled_agent_fn(_Sessions(), run_id="wf_m", max_workers=1)
    try:
        await fn("structured", {"schema": {"type": "boolean"}})
        text = await fn("plain", {})
    finally:
        await pool.shutdown()

    assert text.startswith("Error: set_project only works from a user-facing session")
