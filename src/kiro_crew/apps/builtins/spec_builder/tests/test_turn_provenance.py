"""What a Spec Builder turn hands the host runner: its provenance, never more.

The host's ``_run_chat`` reads two marks a dispatcher sets. ``_directive_user_origin``
(with ``_directive_self_wake``) is the positive producer mark the directive consumer
needs before it admits a self-arm, and ``_message_is_typed`` decides whether the
person's words reach the MCP sign-in trigger. The host half of each rule is pinned in
core's ``test_session_directive_self_arm_provenance.py`` and
``test_chat_runner_mcp_sign_in_ask.py``. These pin the app's half: what
``runtime._dispatch_turn`` passes, and that a decision answer is sent as composed text.
"""

from __future__ import annotations

import ast
import inspect
from unittest import mock

import pytest

from kiro_crew.apps.builtins.spec_builder.backend import runtime
from kiro_crew.apps.builtins.spec_builder.backend.orchestration import decision_outbox
from kiro_crew.apps.builtins.spec_builder.tests.routes_coverage_helpers import _Slot, _State
from kiro_crew.testing.wait import finished

TEXT = "Pull the latest frame from Figma."


async def _run_chat_kwargs(**dispatch_kwargs) -> dict:
    """The keyword arguments ``_dispatch_turn`` hands the host runner for one turn."""
    slot = _Slot("spec-worker")
    state = _State(**{"spec-worker": slot})
    captured: list[dict] = []

    async def _fake_run_chat(_state, _slot, _message, **kwargs):
        captured.append(kwargs)

    async def _passthrough(coro, *, state, slot):
        return await coro

    with (
        mock.patch("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat),
        mock.patch("kiro_crew.dashboard.turn_dispatch.bounded_chat_turn", new=_passthrough),
    ):
        task = runtime._dispatch_turn(state, slot, TEXT, **dispatch_kwargs)
        assert task is not None
        await finished(task, "the spec turn", timeout=30)
    (kwargs,) = captured
    return kwargs


@pytest.mark.asyncio
async def test_the_dispatch_sets_no_positive_producer_mark():
    """The app relays app-composed seed, task and handoff turns with no actor tag.
    That must grant nothing: the dispatch passes no human origin and no wake."""
    kwargs = await _run_chat_kwargs()
    assert kwargs.get("_directive_user_origin") is False
    assert not kwargs.get("_directive_self_wake")
    assert not kwargs.get("_turn_actor")


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
async def test_the_dispatch_forwards_whether_its_text_was_typed(typed):
    kwargs = await _run_chat_kwargs(directive_user_origin=True, message_is_typed=typed)
    assert kwargs["_message_is_typed"] is typed
    assert kwargs["_directive_user_origin"] is True


def test_a_decision_answer_is_dispatched_as_composed_text():
    # The answer is built from the agent's own decision title and options; the
    # person only picked one, so it must never start a sign-in.
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(decision_outbox)))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_dispatch_turn"
    ]
    assert calls
    for call in calls:
        typed = {kw.arg: kw.value for kw in call.keywords}.get("message_is_typed")
        assert isinstance(typed, ast.Constant) and typed.value is False
