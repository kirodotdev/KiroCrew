"""An MCP elicitation through the REAL runtime: reader loop, session queue, classifier,
handle, answer -- against the fake ACP backend as a child process, on both launch
shapes (the kiro-cli spawn sends ``elicitation/create``; the KAS relay sends
``_kiro/mcp/elicitation``).

The fake echoes the host's answer as its reply text, so each test reads exactly
what went out on the wire.
"""

from __future__ import annotations

import json
import sys

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import skill_projection
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.runtime_spawn_launch import SpawnPlan
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_ELICITATION_REQUEST,
    EVENT_TEXT_CHUNK,
)
from kiro_crew.testing import fake_acp_backend

posix_only = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="the fake backend is launched as a POSIX child here"
)

_FAKE = [sys.executable, fake_acp_backend.__file__, "acp", "--agent", "kirocrew"]
_FAKE_KAS = [*_FAKE, fake_acp_backend.KAS_RELAY_FLAG, "v3"]


def _work_dir(tmp_path):
    return tmp_path / "wd"


def _runtime(tmp_path, monkeypatch, backend: str) -> AcpRuntime:
    rt = AcpRuntime(
        work_dir=_work_dir(tmp_path), agent="kirocrew", sandbox_mode="off", acp_backend=backend
    )
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0
    argv = _FAKE_KAS if backend == ACP_BACKEND_KAS else _FAKE

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=argv)

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", lambda _wd: None)
    return rt


async def _run(rt: AcpRuntime, tmp_path, *, accepts: bool, mode: str, answer):
    """One prompt on a fresh session; returns (elicitation events, echoed answer)."""
    await rt.spawn()
    try:
        # The session opens on the workspace the runtime is bound to: a delegated
        # macOS runtime refuses any other one.
        handle = await rt.create_session(cwd=_work_dir(tmp_path), mcp_servers=[])
        handle.accepts_elicitation = accepts
        events = []
        text: list[str] = []
        async for ev in handle.prompt(f"[[ELICIT:{mode}]]", timeout=30):
            if ev.kind == EVENT_ELICITATION_REQUEST:
                events.append(ev)
                await answer(handle, ev)
            elif ev.kind == EVENT_TEXT_CHUNK:
                text.append(ev.text)
        reply = "".join(text)
        assert reply.startswith(fake_acp_backend.ELICIT_ANSWER_PREFIX), reply
        echoed = json.loads(reply[len(fake_acp_backend.ELICIT_ANSWER_PREFIX) :])
        assert handle._pending_elicitations == {}
        return events, echoed
    finally:
        if rt._process is not None:
            await rt.kill(expected=True, reason="test teardown")


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_a_form_answer_reaches_the_engine_on_either_wire(tmp_path, monkeypatch, backend):
    async def _answer(handle, ev):
        assert ev.elicitation["mode"] == "form"
        assert [f["name"] for f in ev.elicitation["fields"]] == ["confirm", "reason"]
        assert ev.tool_call_id == "fake-tool-1"
        await handle.answer_elicitation(
            ev.request_id, "accept", {"confirm": True, "reason": "runtime test"}
        )

    events, echoed = await _run(
        _runtime(tmp_path, monkeypatch, backend),
        tmp_path,
        accepts=True,
        mode="form",
        answer=_answer,
    )
    assert len(events) == 1
    assert echoed == {"action": "accept", "content": {"confirm": True, "reason": "runtime test"}}


@posix_only
@pytest.mark.asyncio
async def test_a_url_ask_is_accepted_without_content(tmp_path, monkeypatch):
    async def _answer(handle, ev):
        assert ev.elicitation["mode"] == "url"
        assert ev.elicitation["url"] == "https://example.com/confirm"
        await handle.answer_elicitation(ev.request_id, "accept", {"ignored": 1})

    _events, echoed = await _run(
        _runtime(tmp_path, monkeypatch, ACP_BACKEND_KAS),
        tmp_path,
        accepts=True,
        mode="url",
        answer=_answer,
    )
    assert echoed == {"action": "accept"}


@posix_only
@pytest.mark.asyncio
async def test_a_consumer_that_cannot_ask_a_person_declines_without_an_event(tmp_path, monkeypatch):
    """No event is yielded and the engine hears ``decline`` -- not a -32601, which
    the KAS engine would turn into a cancel, and not a hang."""

    async def _answer(handle, ev):
        raise AssertionError("an elicitation event reached a consumer that never opted in")

    events, echoed = await _run(
        _runtime(tmp_path, monkeypatch, ACP_BACKEND_KAS),
        tmp_path,
        accepts=False,
        mode="form",
        answer=_answer,
    )
    assert events == []
    assert echoed == {"action": "decline"}


@posix_only
@pytest.mark.asyncio
async def test_a_stop_over_an_open_ask_answers_it_cancel_on_the_wire(tmp_path, monkeypatch):
    """The person stops the turn instead of answering: the engine, which holds the
    tool call open until the ask is answered, hears ``cancel`` for it."""

    async def _answer(handle, ev):
        await handle.cancel()

    events, echoed = await _run(
        _runtime(tmp_path, monkeypatch, ACP_BACKEND_KAS),
        tmp_path,
        accepts=True,
        mode="form",
        answer=_answer,
    )
    assert len(events) == 1
    assert echoed == {"action": "cancel"}
