"""A sub-agent run that kiro-cli moved onto another model, end to end.

The run path is the real one (``SubagentManager.spawn`` -> ``_run`` ->
``_run_inner_impl``) with only the ACP session provider faked, as in
``test_subagent_stop_reason_consistency.py``. These pin that the run is
classified by its terminal, not by the promotion, and that the model the engine
ran is what the panel shows and what ``state.json`` keeps, on every ending.
The model is persisted when the engine's notice arrives, while the run is live.
A refusal hop's refused text leaves the delivered result unless nothing answered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from subagent_stop_reason_helpers import (
    _complete,
    _done_event,
    _manager,
    _mock_sessions,
    _single_turn,
    _spawn_and_wait,
    _spy_events,
    _text,
)

import kiro_crew.subagent_persistence as sp
from kiro_crew.acp.types import (
    EVENT_MODEL_FALLBACK,
    EVENT_TOOL_CALL,
    STOP_CLASS_FAILED,
    STOP_CLASS_SUCCEEDED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
)
from kiro_crew.subagent import _TRANSIENT_CONTINUE_MSG

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


def _with_engine_walk(sessions: MagicMock, engine: str) -> None:
    """Give the fake session the walk an engine leaves after refusing model-a.

    The engine moved to model-b and promoted it. The provider is the object
    ``tracker_of`` finds the tracker on, exactly as on a real session handle.
    """
    from kiro_crew.acp.engine_fallback import (
        TRACKER_ATTR,
        EngineFallbackTracker,
        ModelFallbackNotice,
    )

    tracker = EngineFallbackTracker()
    owner = SimpleNamespace(_resolved_model_id="model-a", _model="model-a")
    tracker.apply(
        owner,
        [
            ModelFallbackNotice("model-a", "model-b", "refused", engine=engine),  # type: ignore[arg-type]
            ModelFallbackNotice(
                "model-a", "model-b", "refused", promoted=True, engine=engine  # type: ignore[arg-type]
            ),
        ],
    )
    setattr(sessions._provider, TRACKER_ATTR, tracker)


@pytest.mark.asyncio
async def test_a_v2_recovery_is_a_completed_run_served_by_the_target():
    # The ACP layer delivers a recovered V2 turn as end_turn (a V2 refusal hop
    # unfolds the refusal it moved off). The run completes, and the panel names
    # the model that answered rather than the one that refused.
    factory, _calls = _single_turn(STOP_REASON_END_TURN, chunks=("the answer",))
    sessions = _mock_sessions(factory)
    _with_engine_walk(sessions, "v2")
    mgr = _manager(sessions)
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed" and info.error == ""
    assert info.stop_class == STOP_CLASS_SUCCEEDED and info.partial is False
    assert info.resolved_model == "model-b"
    done = _done_event(events)
    assert done["outcome"] == "completed" and done["model"] == "model-b"


@pytest.mark.asyncio
async def test_a_promotion_with_a_refusal_terminal_is_still_a_refused_run():
    # The promoted target refused too, so the terminal is refusal. A promotion is
    # not an answer (KAS pins at move time), and the chip still names the model
    # that ran last.
    factory, _calls = _single_turn(STOP_REASON_REFUSAL, chunks=("I can't help with that.",))
    sessions = _mock_sessions(factory)
    _with_engine_walk(sessions, "kas")
    mgr = _manager(sessions)
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.outcome != "completed"
    assert info.stop_class == STOP_CLASS_FAILED and info.partial is True
    assert info.resolved_model == "model-b"
    assert _done_event(events)["model"] == "model-b"


def _moved_turn(stop_reason: str):
    """A stream on which the engine's promotion notice arrives mid-run."""
    from kiro_crew.acp.engine_fallback import ModelFallbackNotice

    notice = ModelFallbackNotice("model-a", "model-b", "refused", promoted=True)

    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            yield _text("the answer")
            yield SimpleNamespace(
                kind=EVENT_MODEL_FALLBACK, model_fallback=notice, runtime_global=False
            )
            yield _complete(stop_reason)

        return _gen()

    return stream_factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason, completed",
    [(STOP_REASON_END_TURN, True), (STOP_REASON_REFUSAL, False)],
    ids=["answered", "refused"],
)
async def test_the_engine_model_is_persisted_while_the_run_is_live(
    stop_reason: str, completed: bool
):
    # Written when the notice arrives, before any ending: a live run holds its
    # conversation, so no continue or release can land a `keep` that a later
    # whole-file write would roll back. Every ending, answered or not, has it.
    sessions = _mock_sessions(_moved_turn(stop_reason))
    _with_engine_walk(sessions, "kas")
    mgr = _manager(sessions)
    real_write = mgr._write_state_off_loop
    seen: list[tuple[bool, dict]] = []

    async def _spy(info, what, **fields):
        if what == "engine fallback model":
            seen.append((info.done or info._ending_claimed, fields))
        return await real_write(info, what, **fields)

    mgr._write_state_off_loop = _spy  # type: ignore[method-assign]
    info = await _spawn_and_wait(mgr)
    assert (info.outcome == "completed") is completed
    assert info.resolved_model == "model-b"
    assert seen == [(False, {"resolved_model": "model-b"})]


_REFUSED = "I can't help with that."


def _refusal_hop_turn(*after):
    """The refused model's text, the V2 refusal hop, then *after* (the wire order of
    ``test/fixtures/acp_frames/kiro/refusal-fallback-live.jsonl``)."""
    from kiro_crew.acp.engine_fallback import ModelFallbackNotice

    hop = ModelFallbackNotice("model-a", "model-b", "refused", engine="v2")

    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_REFUSED)
            yield SimpleNamespace(
                kind=EVENT_MODEL_FALLBACK, model_fallback=hop, runtime_global=False
            )
            for event in after:
                yield event

        return _gen()

    return stream_factory


@pytest.mark.asyncio
async def test_a_refusal_hop_drops_the_refused_text_from_the_result():
    # The text before the hop is the refused model's, not the run's answer: the
    # parent receives the fallback's answer alone, as the dashboard and Slack show.
    sessions = _mock_sessions(
        _refusal_hop_turn(_text("Here is the answer."), _complete(STOP_REASON_END_TURN))
    )
    info = await _spawn_and_wait(_manager(sessions))
    assert info.outcome == "completed"
    assert "Here is the answer." in info.result
    assert _REFUSED not in info.result


@pytest.mark.asyncio
async def test_a_refusal_the_fallback_did_not_recover_keeps_the_providers_words():
    sessions = _mock_sessions(_refusal_hop_turn(_complete(STOP_REASON_REFUSAL)))
    info = await _spawn_and_wait(_manager(sessions))
    assert info.outcome != "completed"
    assert _REFUSED in info.result
    # What spawn_status reads for a done run, which the hop had cut.
    assert (sp._agent_dir(info.id) / "result.txt").read_text(encoding="utf-8") == _REFUSED


class _TransientError(Exception):
    """Duck-typed AcpError carrying the structured transient verdict. No adapter
    classifies it as a dependency condition, so it takes the in-turn continue."""

    transient = True


@pytest.mark.asyncio
async def test_a_refusal_hop_on_a_continue_prompt_keeps_the_preserved_partial():
    # The continue after a post-token transient error is refused, then the hop: only
    # the continue's refused text leaves, never the partial the continue preserved.
    from kiro_crew.acp.engine_fallback import ModelFallbackNotice

    hop = ModelFallbackNotice("model-a", "model-b", "refused", engine="v2")
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text("Partial answer. ")
                raise _TransientError("backend hiccup")
            yield _text(_REFUSED)
            yield SimpleNamespace(
                kind=EVENT_MODEL_FALLBACK, model_fallback=hop, runtime_global=False
            )
            yield _text("the rest.")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)
    assert calls[1] == _TRANSIENT_CONTINUE_MSG
    assert info.outcome == "completed"
    assert "Partial answer." in info.result and "the rest." in info.result
    assert _REFUSED not in info.result


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_TOOL_CALL,
        title="read",
        tool_kind="read",
        tool_call_id="t1",
        tool_input="{}",
        text="",
        runtime_global=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_hop, stop_reason",
    [
        # The fallback wrote, called a tool, then its next call was refused bare.
        ((_text("Let me check the file."), _tool_call()), STOP_REASON_REFUSAL),
        # The fallback answered with tool work alone.
        ((_tool_call(),), STOP_REASON_END_TURN),
    ],
    ids=["answered_then_refused", "tool_only_answer"],
)
async def test_a_refusal_hop_the_fallback_answered_does_not_put_the_refused_text_back(
    after_hop: tuple, stop_reason: str
):
    sessions = _mock_sessions(_refusal_hop_turn(*after_hop, _complete(stop_reason)))
    info = await _spawn_and_wait(_manager(sessions))
    assert _REFUSED not in info.result
