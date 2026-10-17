"""Background one-liners recycle the shared ``_bg`` conversation.

On a backend outside ``_bg_runtime_backends()`` (claude, opencode, pi, goose,
deepseek) ``get_bg_session()`` hands out a ``_ProviderBgSession`` over the ONE
persistent ``BACKGROUND_KEY`` entry, and its ``destroy()`` only releases the
turn semaphore. If ``run_bg_oneliner`` stopped there, titles, nav labels,
folder icons and summaries would append to that conversation for the whole
gateway uptime with no recycle criterion ever evaluated, until the provider
refused it as prompt-too-long -- and the reported percentage would still be the
last successful turn's, so even a recycle check would keep that conversation.

These cases pin: a one-liner on the shared path runs ``recycle_background``;
a prompt-too-long refusal forces the recycle there whether the caller saw the
error (one-liner) or swallowed it (``stream_and_collect``, which consolidation
uses); and the runtime-capable backends keep exactly their old behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.config import KiroCrewConfig
from kiro_crew.llm_helpers import is_context_overflow_error, run_bg_oneliner, stream_and_collect
from kiro_crew.session import _BG_BLIND_RECYCLE_PROMPTS, BACKGROUND_KEY, SessionManager
from kiro_crew.session_background import mark_context_overflowed

# The claude adapter's refusal as Crew formats it: the phrase sits in the JSON-RPC
# ``message``, which the raise-time classifier does not read, so no
# ``context_overflow`` tag is set on it.
_CLAUDE_TOO_LONG = (
    "Prompt error: {'code': -32603, 'message': 'Internal error: Prompt is too long "
    "· automatic compaction failed'}"
)


def _event(kind, text=""):
    e = MagicMock()
    e.kind = kind
    e.text = text
    return e


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        # Far under every threshold: only the criterion a case arms may fire.
        m.context_usage_pct = lambda: 3.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.client._pid = None
        m.raise_on_stream = None

        async def _stream(_message, *, allow_image=True):
            if m.raise_on_stream is not None:
                raise m.raise_on_stream
            yield _event(EVENT_TEXT_CHUNK, "ok")
            yield _event(EVENT_COMPLETE)

        m.stream = _stream
        return m

    return factory


async def _manager(backend: str) -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    cfg.agent.acp_backend = backend
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.start_pool()
    # The background session starts on first use; these tests act on a live one.
    await mgr._ensure_background()
    return mgr


class TestOneLinersBoundTheSharedConversation:
    @pytest.mark.asyncio
    async def test_one_liners_reach_the_blind_backstop_and_recycle(self):
        """Without the recycle, prompt_count never advances on one-liner traffic."""
        mgr = await _manager("claude")
        first = mgr._sessions[BACKGROUND_KEY].provider
        for _ in range(_BG_BLIND_RECYCLE_PROMPTS):
            assert await run_bg_oneliner(mgr, "title this") == "ok"

        first.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not first
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_single_one_liner_is_counted(self):
        mgr = await _manager("claude")
        await run_bg_oneliner(mgr, "title this")
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_prompt_too_long_one_liner_forces_the_recycle(self):
        """At 3% reported, only the refusal itself can retire the conversation."""
        mgr = await _manager("claude")
        first = mgr._sessions[BACKGROUND_KEY].provider
        first.raise_on_stream = AcpError(_CLAUDE_TOO_LONG, transient=False)

        with pytest.raises(AcpError):
            await run_bg_oneliner(mgr, "title this")

        first.shutdown.assert_awaited_once()
        replacement = mgr._sessions[BACKGROUND_KEY].provider
        assert replacement is not first
        # The next one-liner runs on the fresh conversation instead of failing.
        assert await run_bg_oneliner(mgr, "title this") == "ok"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unrelated_failure_does_not_force_a_recycle(self):
        mgr = await _manager("claude")
        first = mgr._sessions[BACKGROUND_KEY].provider
        first.raise_on_stream = AcpError("backend boom", transient=False)

        with pytest.raises(AcpError):
            await run_bg_oneliner(mgr, "title this")

        first.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is first
        await mgr.close_all()


class TestSwallowedOverflowStillRecycles:
    @pytest.mark.asyncio
    async def test_stream_and_collect_leaves_the_verdict_for_the_recycle(self):
        """Consolidation catches its own error, so the provider must carry it."""
        mgr = await _manager("claude")
        first = mgr._sessions[BACKGROUND_KEY].provider
        first.raise_on_stream = AcpError(_CLAUDE_TOO_LONG, transient=False)

        with pytest.raises(AcpError):
            await stream_and_collect(first, "consolidate")
        await mgr.recycle_background()

        first.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not first
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_mark_keeps_the_threshold_verdict(self):
        """Control for the case above: the same 3% without the mark is kept."""
        mgr = await _manager("claude")
        first = mgr._sessions[BACKGROUND_KEY].provider
        await mgr.recycle_background()
        first.shutdown.assert_not_awaited()
        await mgr.close_all()


class TestRuntimeBackendsUnchanged:
    @pytest.mark.asyncio
    # ``""`` is kiro's backend id (``ACP_BACKEND_KIRO``).
    @pytest.mark.parametrize("backend", ["", "kas", "codex"])
    async def test_an_overflow_mark_does_not_recycle_a_runtime_backend(self, backend):
        """kiro/kas/codex keep the threshold criteria they had on ``_bg``."""
        mgr = await _manager(backend)
        provider = mgr._sessions[BACKGROUND_KEY].provider
        mark_context_overflowed(provider)

        await mgr.recycle_background(context_overflowed=True)

        provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_runtime_handle_is_not_followed_by_a_recycle(self):
        """A handle without the marker owns an ephemeral session: nothing to bound."""
        handle = MagicMock()
        handle.served_model = ""
        handle.last_prompt_stats = None
        handle.destroy = AsyncMock()

        async def _prompt(_p, *, allow_image=True):
            yield _event(EVENT_TEXT_CHUNK, "ok")
            yield _event(EVENT_COMPLETE)

        handle.prompt = _prompt
        del handle.shares_background_conversation
        sessions = MagicMock()
        sessions.get_bg_session = AsyncMock(return_value=handle)
        sessions.recycle_background = AsyncMock()

        assert await run_bg_oneliner(sessions, "title this") == "ok"
        handle.destroy.assert_awaited_once()
        sessions.recycle_background.assert_not_awaited()


class TestIsContextOverflowError:
    def test_the_classifier_tag(self):
        exc = AcpError("anything")
        exc.context_overflow = True
        assert is_context_overflow_error(exc)

    @pytest.mark.parametrize(
        "text",
        [
            _CLAUDE_TOO_LONG,
            "context window overflowed",
            "This model's maximum context length is 1048576 tokens",
        ],
    )
    def test_the_backend_spellings(self, text):
        assert is_context_overflow_error(AcpError(text))

    def test_other_errors_and_non_acp_exceptions(self):
        assert not is_context_overflow_error(AcpError("backend boom"))
        assert not is_context_overflow_error(RuntimeError("Prompt is too long"))
