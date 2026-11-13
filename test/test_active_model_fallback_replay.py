"""A retry that switches the model re-sends a prompt naming the model that answers it.

A goose prompt carries an ``[ACTIVE MODEL]`` line naming the model the session
served when the prompt was built. ``stream_and_collect`` and the sub-agent ladder
re-send that same prompt after a pre-token fallback swap, and ``stream_and_collect``
also after its start-of-turn restore of the primary, and each replay must name the
model that answers it. These tests drive each switch and read the prompt each
attempt actually sent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _complete_event,
    _manager,
    _mock_sessions,
    _spawn_and_wait,
    _text_event,
    _TransientError,
)

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import ACP_BACKEND_GOOSE, PROVIDER_LABEL_DEFAULT
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly.sections import active_model_block, restate_active_model
from kiro_crew.learn import LessonStore
from kiro_crew.llm_helpers import (
    _TRANSIENT_RETRIES,
    TRANSIENT_RETRIES,
    TURN_FALLBACK_ATTR,
    ActiveModelLine,
    stream_and_collect,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.prompt_segments import (
    SOURCE_INJECTED,
    SOURCE_USER,
    PromptSegment,
    SegmentedPrompt,
    segments_of,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

PRIMARY = "primary-model"
FALLBACK = "fb-1"
_TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

# ``SubagentManager.spawn`` refuses while the host looks short of memory.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _prompt(model: str, request: str = "what model are you?") -> str:
    return "[SESSION CONTEXT]\n" + active_model_block(model) + f"[CURRENT USER REQUEST]\n{request}"


def _model_lines(message: str) -> list[str]:
    return [line for line in message.splitlines() if line.startswith("[ACTIVE MODEL]")]


class TestRestateActiveModel:
    def test_names_the_served_model(self) -> None:
        out = restate_active_model(_prompt(PRIMARY), PRIMARY, FALLBACK)
        assert _model_lines(out) == [f"[ACTIVE MODEL] {FALLBACK}"]
        assert out == _prompt(FALLBACK)

    def test_an_unreported_model_drops_the_line(self) -> None:
        out = restate_active_model(_prompt(PRIMARY), PRIMARY, "")
        assert _model_lines(out) == []
        assert out.endswith("[CURRENT USER REQUEST]\nwhat model are you?")

    def test_a_prompt_without_the_line_is_unchanged(self) -> None:
        text = "[CURRENT USER REQUEST]\nwhat model are you?"
        assert restate_active_model(text, PRIMARY, FALLBACK) is text

    def test_a_line_for_another_model_is_left_alone(self) -> None:
        """Only the block rendered for the stated id is rewritten."""
        text = _prompt("some-other-model")
        assert restate_active_model(text, PRIMARY, FALLBACK) is text

    def test_a_segmented_prompt_keeps_its_provenance(self) -> None:
        """Only injected text is rewritten; the user's own words are never touched,
        even when they quote the line."""
        user = active_model_block(PRIMARY)
        prompt = SegmentedPrompt(
            [(SOURCE_INJECTED, _prompt(PRIMARY, "")), (SOURCE_USER, user), (SOURCE_INJECTED, "\n")]
        )
        out = restate_active_model(prompt, PRIMARY, FALLBACK)
        assert isinstance(out, SegmentedPrompt)
        assert out.segments == (
            PromptSegment(SOURCE_INJECTED, _prompt(FALLBACK, "")),
            PromptSegment(SOURCE_USER, user),
            PromptSegment(SOURCE_INJECTED, "\n"),
        )

    def test_a_segmented_prompt_with_the_line_only_in_user_text_is_unchanged(self) -> None:
        prompt = SegmentedPrompt([(SOURCE_INJECTED, "ctx\n"), (SOURCE_USER, _prompt(PRIMARY))])
        assert restate_active_model(prompt, PRIMARY, FALLBACK) is prompt


class TestActiveModelLine:
    def test_unchanged_while_the_served_model_holds(self) -> None:
        provider = SimpleNamespace(served_model=PRIMARY)
        text = _prompt(PRIMARY)
        line = ActiveModelLine(text, provider)
        assert line.current() is text

    def test_follows_each_switch(self) -> None:
        provider = SimpleNamespace(served_model=PRIMARY)
        line = ActiveModelLine(_prompt(PRIMARY), provider)
        provider.served_model = FALLBACK
        assert line.current() == _prompt(FALLBACK)
        provider.served_model = "fb-2"
        assert line.current() == _prompt("fb-2")

    def test_an_unreported_attempt_does_not_lose_the_line_for_later_ones(self) -> None:
        """A restore to ``auto`` reads as no model and drops the line; a swap back
        to a named model on the next attempt names it again."""
        provider = SimpleNamespace(served_model=FALLBACK)
        line = ActiveModelLine(_prompt(FALLBACK), provider)
        provider.served_model = ""
        assert _model_lines(line.current()) == []
        provider.served_model = FALLBACK
        assert line.current() == _prompt(FALLBACK)
        provider.served_model = "fb-2"
        assert line.current() == _prompt("fb-2")

    def test_restate_tail_rewrites_the_built_tail_under_a_prepend(self) -> None:
        """The dashboard prepends to the built prompt before the restore probe runs;
        the tail is restated and the prepend kept."""
        provider = SimpleNamespace(served_model=FALLBACK)
        built = SegmentedPrompt(
            [(SOURCE_INJECTED, _prompt(FALLBACK, "")), (SOURCE_USER, "what model are you?")]
        )
        line = ActiveModelLine(built, provider)
        sent = "[Hook context]\nhi\n[End hook context]\n\n" + built
        provider.served_model = PRIMARY
        restated = line.restate_tail(sent)
        assert restated is not None
        new_sent, new_built = restated
        assert new_sent == "[Hook context]\nhi\n[End hook context]\n\n" + _prompt(PRIMARY)
        assert new_sent.endswith(new_built)
        assert segments_of(new_built)[-1] == PromptSegment(SOURCE_USER, "what model are you?")

    def test_restate_tail_is_none_when_nothing_moved(self) -> None:
        provider = SimpleNamespace(served_model=PRIMARY)
        built = SegmentedPrompt([(SOURCE_INJECTED, _prompt(PRIMARY))])
        assert ActiveModelLine(built, provider).restate_tail("pre " + built) is None

    def test_restate_tail_is_none_when_the_sent_tail_differs(self) -> None:
        provider = SimpleNamespace(served_model=PRIMARY)
        built = SegmentedPrompt([(SOURCE_INJECTED, _prompt(PRIMARY))])
        line = ActiveModelLine(built, provider)
        provider.served_model = FALLBACK
        assert line.restate_tail(built + " rewritten after assembly") is None

    def test_a_provider_without_a_readable_model_rewrites_nothing(self) -> None:
        provider = MagicMock()  # served_model reads as a mock, not a str
        text = _prompt(PRIMARY)
        assert ActiveModelLine(text, provider).current() is text

    def test_a_real_goose_prompt_is_restated(self, tmp_path, close_skills_loaders) -> None:
        """The prompt the context builder sends a goose turn, not a hand-made one."""

        class _Provider(AcpProvider):
            def __init__(self) -> None:
                self._client = SimpleNamespace(
                    backend=ACP_BACKEND_GOOSE, cwd=str(tmp_path), _resolved_model_id=PRIMARY
                )

        provider = _Provider()
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        message, _ = builder.build_message(
            "what model are you?",
            True,
            session_key="dashboard:chat-goose-1",
            provider_type=PROVIDER_LABEL_DEFAULT,
            runtime_source="dashboard",
            context_provider=provider,
        )
        assert _model_lines(message) == [f"[ACTIVE MODEL] {PRIMARY}"]
        line = ActiveModelLine(message, provider)
        provider._client._resolved_model_id = FALLBACK
        restated = line.current()
        assert _model_lines(restated) == [f"[ACTIVE MODEL] {FALLBACK}"]
        assert restated.replace(FALLBACK, PRIMARY) == message


def _switching_provider(stream, *, served: str = PRIMARY):
    provider = AsyncMock()
    provider.cancel = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.stream = stream
    provider.available_models = MagicMock(
        return_value=[{"modelId": m} for m in (PRIMARY, FALLBACK)]
    )
    provider.served_model = served
    provider._model = served

    async def _move(model_id: str) -> None:
        provider._model = model_id
        provider.served_model = model_id

    provider.set_model = AsyncMock(side_effect=_move)
    return provider


class TestStreamAndCollectReplay:
    @pytest.mark.asyncio
    async def test_the_fallback_replay_names_the_fallback(self) -> None:
        sent: list[str] = []

        async def _stream(msg):
            sent.append(msg)
            if len(sent) <= _TRANSIENT_RETRIES + 1:
                raise AcpError(_TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = _switching_provider(_stream)
        with patch("kiro_crew.llm_helpers.transient_retry_delay", return_value=0.0):
            result = await stream_and_collect(
                provider, _prompt(PRIMARY), fallback_models=[FALLBACK]
            )

        assert result == "ok"
        provider.set_model.assert_awaited_once_with(FALLBACK)
        # Every attempt on the primary named the primary; the replay on the
        # fallback names the fallback and keeps every other byte.
        assert sent[:-1] == [_prompt(PRIMARY)] * (_TRANSIENT_RETRIES + 1)
        assert sent[-1] == _prompt(FALLBACK)

    @pytest.mark.asyncio
    async def test_a_restored_primary_is_named(self) -> None:
        """The prompt was built while the fallback served; the start-of-turn
        restore moves the session back to the primary before it streams."""
        sent: list[str] = []

        async def _stream(msg):
            sent.append(msg)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = _switching_provider(_stream, served=FALLBACK)
        setattr(provider, TURN_FALLBACK_ATTR, (PRIMARY, FALLBACK))

        assert await stream_and_collect(provider, _prompt(FALLBACK)) == "ok"
        provider.set_model.assert_awaited_once_with(PRIMARY)
        assert sent == [_prompt(PRIMARY)]

    @pytest.mark.asyncio
    async def test_a_prompt_without_the_line_is_sent_as_built(self) -> None:
        sent: list[str] = []
        text = "[CURRENT USER REQUEST]\nhello"

        async def _stream(msg):
            sent.append(msg)
            if len(sent) <= _TRANSIENT_RETRIES + 1:
                raise AcpError(_TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = _switching_provider(_stream)
        with patch("kiro_crew.llm_helpers.transient_retry_delay", return_value=0.0):
            await stream_and_collect(provider, text, fallback_models=[FALLBACK])

        assert sent == [text] * (_TRANSIENT_RETRIES + 2)


class _Provider:
    """Sub-agent provider double whose ``set_model`` moves ``served_model``."""

    def __init__(self, stream_factory) -> None:
        self._stream_factory = stream_factory
        self.backend = ""
        self.cwd = ""
        self.last_infra_error = None
        self.session_id = ""
        self.served_model = PRIMARY
        self._model = PRIMARY
        self.approve_tool = AsyncMock()
        self.reject_tool = AsyncMock()
        self.available_models = MagicMock(return_value=[{"modelId": FALLBACK}])

        async def _move(model_id: str) -> None:
            self._model = model_id
            self.served_model = model_id

        self.set_model = AsyncMock(side_effect=_move)

    def stream(self, message: str):
        return self._stream_factory(message)

    def context_usage_pct(self) -> float:
        return 0.0

    def context_window_tokens(self) -> int:
        return 0

    def context_used_tokens(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_the_subagent_fallback_replay_names_the_fallback() -> None:
    sent: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        sent.append(msg)

        async def _gen():
            if len(sent) <= 1 + TRANSIENT_RETRIES:
                raise _TransientError("backend throttle")
            yield _text_event("fb result")
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = _Provider(stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions._provider = provider
    mgr = _manager(sessions)
    mgr._ctx_builder.build_message = MagicMock(return_value=(_prompt(PRIMARY, "do work"), None))
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=(FALLBACK,)),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    provider.set_model.assert_awaited_once_with(FALLBACK)
    assert sent[:-1] == [_prompt(PRIMARY, "do work")] * (1 + TRANSIENT_RETRIES)
    assert sent[-1] == _prompt(FALLBACK, "do work")
