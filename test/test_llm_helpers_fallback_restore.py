"""After a fallback served a turn: restoring the primary, and the walk's budget and story.

The sticky-restore probe makes one ``set_model(primary)`` at the next turn start,
keeps the fallback when the restore fails or does not move the model, and offers
the parameter seams the dashboard's slot probe wraps. The per-candidate retry
budget and the chain-exhausted story are each built in one place; the story is
redacted and capped there, and its consumers, the fallback-served annotation
and the probe never raise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.llm_helpers import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    TURN_FALLBACK_ATTR,
    FallbackState,
    probe_fallback_restore,
    stream_and_collect,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent


class TestProbeFallbackRestore:
    """Sticky-restore probe: one set_model(primary) at the next turn start."""

    @staticmethod
    def _provider(served="fb-1"):
        provider = AsyncMock()
        provider.served_model = served
        provider._model = served

        # Successful set_model syncs the model attrs (real-provider behavior);
        # the restore witness reads this to confirm the session moved.
        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_restores_primary_and_clears_marker(self) -> None:
        provider = self._provider(served="fb-1")
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        provider.set_model.assert_awaited_once_with("primary-model")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_failed_restore_keeps_fallback(self) -> None:
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock(side_effect=AcpError("throttled again"))
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-1")

    @pytest.mark.asyncio
    async def test_stale_marker_cleared_without_touching_model(self) -> None:
        """The session moved off our fallback by other means (explicit user
        pick / reset): drop the marker, never override the newer choice."""
        provider = self._provider(served="user-picked-model")
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        provider.set_model.assert_not_awaited()
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_no_marker_is_a_noop(self) -> None:
        provider = self._provider()
        setattr(provider, TURN_FALLBACK_ATTR, None)
        await probe_fallback_restore(provider)
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_and_collect_probes_restore_at_entry(self) -> None:
        """A provider carrying the sticky marker gets one restore attempt
        BEFORE the turn streams."""

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _move(model_id):
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok"
        provider.set_model.assert_awaited_once_with("primary-model")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None


class TestFallbackRetryBudgetSingleBody:
    """The per-candidate retry budget lives in ONE place."""

    def test_no_active_candidate_never_retries(self) -> None:
        st = FallbackState(("m1",))
        assert st.should_retry_active() is False
        assert st.attempts == 0

    def test_consumes_attempts_up_to_the_budget(self) -> None:
        st = FallbackState(("m1",), active="m1", attempts=1)
        # attempts=1 is how advance_fallback_candidate seeds a fresh candidate.
        for expected in range(2, FALLBACK_CANDIDATE_ATTEMPTS + 1):
            assert st.should_retry_active() is True
            assert st.attempts == expected
        assert st.should_retry_active() is False
        assert st.attempts == FALLBACK_CANDIDATE_ATTEMPTS

    def test_dashboard_rewind_derives_from_the_same_constant(self) -> None:
        """The interactive ladder's counter rewind grants a fresh candidate
        exactly FALLBACK_CANDIDATE_ATTEMPTS - 1 further same-model passes —
        the same budget the unattended surfaces consume via
        should_retry_active."""
        from kiro_crew.llm_helpers import (
            TRANSIENT_RETRIES,
            fallback_rewound_transient_budget,
        )

        assert fallback_rewound_transient_budget() == TRANSIENT_RETRIES - (
            FALLBACK_CANDIDATE_ATTEMPTS - 1
        )


class TestFallbackExhaustionStory:
    """The chain-exhausted story is built in ONE place (FallbackState)."""

    def test_story_names_primary_and_every_walked_candidate(self) -> None:
        st = FallbackState(("a", "b"), primary="primary-m", walked=["a", "b"])
        assert st.exhaustion_story() == ("primary-m throttled; fallbacks a, b also unavailable")

    def test_unwalked_chain_has_no_story(self) -> None:
        """Nothing actually ran (all candidates skipped) ⇒ the error surfaces
        exactly as before the feature — no story."""
        assert FallbackState(("a",), primary="p").exhaustion_story() is None

    def test_unknown_primary_uses_placeholder(self) -> None:
        story = FallbackState(("a",), walked=["a"]).exhaustion_story()
        assert story is not None and story.startswith("the selected model throttled")


class TestFallbackStoryConsumer:
    """append_fallback_story is THE reader of the story attr."""

    def _exc_with_story(self, story: str = "primary-m throttled; fallbacks fb-1 also unavailable"):
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR

        exc = AcpError("Internal server error")
        setattr(exc, FALLBACK_STORY_ATTR, story)
        return exc

    def test_appends_story_to_terminal_error_text(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        out = append_fallback_story("AcpError: Internal server error", self._exc_with_story())
        assert out == (
            "AcpError: Internal server error "
            "[primary-m throttled; fallbacks fb-1 also unavailable]"
        )

    def test_no_story_returns_text_byte_for_byte(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("AcpError: boom", AcpError("boom")) == "AcpError: boom"

    def test_empty_text_returns_bare_story(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("", self._exc_with_story("s")) == "s"

    def test_non_string_story_attr_is_ignored(self) -> None:
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, ["not", "a", "string"])
        assert append_fallback_story("t", exc) == "t"

    def test_stream_and_collect_sets_the_attr_this_reads(self) -> None:
        """DRIFT PIN: the producer (Case 2.75) and this consumer agree on the
        attribute — fallback_story_of reads back what the walk attached."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, fallback_story_of

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "the story")
        assert fallback_story_of(exc) == "the story"
        assert fallback_story_of(AcpError("boom")) == ""


class TestAnnotateModelFallbackSharedBody:
    """There is one spelling of the fallback-served warning."""

    def test_prefixes_warning_from_marker(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))
        out = annotate_model_fallback("body text", provider)
        assert out.startswith("⚠️ Model 'primary-m' throttled")
        assert "fallback 'fb-m'" in out
        assert out.endswith("\n\nbody text")

    def test_empty_text_yields_bare_warning_line(self) -> None:
        """The sub-agent call site can pass an empty result — the warning
        stands alone rather than trailing a blank separator."""
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))
        out = annotate_model_fallback("", provider)
        assert out.startswith("⚠️ Model 'primary-m' throttled")
        assert "\n\n" not in out

    def test_no_marker_and_malformed_marker_are_noops(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        assert annotate_model_fallback("text", SimpleNamespace()) == "text"
        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("only-one",))
        assert annotate_model_fallback("text", provider) == "text"


class TestProbeFallbackRestoreSlotSeams:
    """The parameter seams the dashboard's slot probe wraps.

    The slot probe (chat_runner._probe_fallback_restore_for_slot_locked) is a
    thin adapter over this single body; these tests pin the seams it depends
    on: slot-held state, the extra staleness flag, the replacement clear, and
    the pre-clear heal hook.
    """

    @staticmethod
    def _provider(served: str = "fb-1"):
        provider = AsyncMock()
        provider.served_model = served
        provider._model = served

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_state_override_restores_and_runs_hooks_in_order(self) -> None:
        provider = self._provider(served="fb-1")
        order: list[str] = []
        await probe_fallback_restore(
            provider,
            surface="dashboard",
            state=("primary-model", "fb-1"),
            clear=lambda: order.append("clear"),
            on_restored=lambda: order.append("heal"),
            log_suffix=", slot=s1",
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        # The heal must see the pre-clear state: heal strictly before clear.
        assert order == ["heal", "clear"]

    @pytest.mark.asyncio
    async def test_stale_flag_clears_without_restoring(self) -> None:
        """The dashboard's explicit-pick generation check rides in as `stale`:
        an explicit pick must never be overridden by a restore."""
        provider = self._provider(served="fb-1")
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            stale=True,
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_unknown_primary_clears_without_restoring(self) -> None:
        provider = self._provider(served="fb-1")
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("", "fb-1"),
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_empty_candidate_state_is_a_noop(self) -> None:
        """Matches the slot probe's no-active-fallback early return: nothing
        restored, nothing cleared."""
        provider = self._provider()
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", ""),
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == []

    @pytest.mark.asyncio
    async def test_failed_restore_keeps_state_and_skips_hooks(self) -> None:
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock(side_effect=AcpError("throttled again"))
        called: list[str] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=lambda: called.append("clear"),
            on_restored=lambda: called.append("heal"),
        )
        assert called == []

    @pytest.mark.asyncio
    async def test_silent_noop_restore_keeps_state_and_skips_hooks(self) -> None:
        """A non-raising set_model that did not move the session (witness
        check) must keep the sticky state — clearing would let the backfill
        pin the fallback permanently."""
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock()  # returns without syncing the model
        called: list[str] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=lambda: called.append("clear"),
            on_restored=lambda: called.append("heal"),
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        assert called == []


class TestCase275RoutesThroughSharedBudgetBody:
    """Case 2.75 must consult should_retry_active.

    Mutation check in reverse: forcing the shared body to refuse retries
    changes this surface's attempt count — proof the budget is not re-encoded
    locally. The sub-agent ladder has the mirror pin in
    test_subagent_turn_resilience.py.
    """

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

    @pytest.mark.asyncio
    async def test_forcing_the_shared_body_to_refuse_drops_candidate_retries(self) -> None:
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.available_models = MagicMock(
            return_value=[{"modelId": "fb-1"}, {"modelId": "fb-2"}]
        )
        provider.served_model = "primary-model"
        provider._model = "primary-model"

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)

        with (
            # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(FallbackState, "should_retry_active", return_value=False),
            pytest.raises(AcpError),
        ):
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        # Budget refused ⇒ each candidate gets only the single post-advance
        # attempt: 4 same-model + 1 per candidate (vs 2 each normally).
        assert call_count == (_TRANSIENT_RETRIES + 1) + 2


class TestFallbackStorySafety:
    """Round-2 review pins: the story is redacted+capped CENTRALLY, and the
    consumers/annotator never raise."""

    def test_story_is_capped_centrally(self) -> None:
        """Walked ids originate in config (advertised check fails open), so an
        arbitrarily long story must be bounded before riding any surface."""
        from kiro_crew.llm_helpers import (
            _FALLBACK_STORY_CAP,
            FALLBACK_STORY_ATTR,
            fallback_story_of,
        )

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "m" * (_FALLBACK_STORY_CAP * 3))
        assert len(fallback_story_of(exc)) == _FALLBACK_STORY_CAP

    def test_story_is_redacted_centrally(self) -> None:
        """A credential-shaped token in the story never reaches a consumer —
        heartbeat logs the story with no redaction of its own."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, fallback_story_of

        exc = AcpError("boom")
        setattr(
            exc,
            FALLBACK_STORY_ATTR,
            "AKIAIOSFODNN7EXAMPLE throttled; fallbacks fb-1 also unavailable",
        )
        story = fallback_story_of(exc)
        assert "AKIAIOSFODNN7EXAMPLE" not in story
        assert "also unavailable" in story

    def test_story_read_survives_hostile_exception_attr(self) -> None:
        """The attribute read itself is guarded — a raising property on an
        exotic exception type must not break the failure handler reading it."""
        from kiro_crew.llm_helpers import append_fallback_story

        class _HostileExc(Exception):
            @property
            def _kc_fallback_story(self):  # FALLBACK_STORY_ATTR
                raise RuntimeError("hostile attr")

        assert append_fallback_story("t", _HostileExc()) == "t"

    def test_annotate_never_raises_on_hostile_marker(self) -> None:
        """An annotation failure must not turn a successful run into a failed
        one — the un-annotated text comes back instead (the sub-agent call
        site relies on this; its old inline guard is gone)."""
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        class _Boom:
            def __str__(self) -> str:  # str(primary) raises inside the helper
                raise RuntimeError("hostile id")

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, (_Boom(), "fb-m"))
        assert annotate_model_fallback("the result", provider) == "the result"

    @pytest.mark.asyncio
    async def test_probe_survives_raising_hooks(self) -> None:
        """probe_fallback_restore promises 'never raises' — caller-supplied
        heal/clear hooks are contained, and a failed heal does not block the
        clear (the two writes are one logical record)."""
        provider = AsyncMock()
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)

        cleared: list[bool] = []

        def _bad_heal() -> None:
            raise RuntimeError("heal boom")

        def _clear() -> None:
            cleared.append(True)

        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=_clear,
            on_restored=_bad_heal,
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_probe_survives_raising_clear_on_stale_path(self) -> None:
        provider = AsyncMock()
        provider.served_model = "other-model"
        provider._model = "other-model"
        provider.set_model = AsyncMock()

        def _bad_clear() -> None:
            raise RuntimeError("clear boom")

        # Moved-off session: the stale path runs the (raising) clear — must
        # not propagate.
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=_bad_clear,
        )
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_malformed_state_is_a_noop(self) -> None:
        provider = AsyncMock()
        provider.set_model = AsyncMock()
        await probe_fallback_restore(provider, state=("only-one",))  # type: ignore[arg-type]
        provider.set_model.assert_not_awaited()

    def test_rewound_budget_is_clamped_at_zero(self) -> None:
        """A future FALLBACK_CANDIDATE_ATTEMPTS above TRANSIENT_RETRIES + 1
        cannot be expressed by the counter encoding — it clamps to zero (the
        full same-model budget) instead of going negative."""
        import kiro_crew.llm_helpers as lh

        with patch.object(lh, "FALLBACK_CANDIDATE_ATTEMPTS", lh.TRANSIENT_RETRIES + 5):
            assert lh.fallback_rewound_transient_budget() == 0

    def test_budgeted_append_keeps_the_story_over_the_error_tail(self) -> None:
        """With a budget, the ERROR text is trimmed to leave the story room —
        a verbose exception must never push the walk out of the composite —
        and the total stays within the budget."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-1 also unavailable")
        out = append_fallback_story("E" * 5000, exc, budget=2000)
        assert len(out) <= 2000
        assert out.endswith("[primary-m throttled; fallbacks fb-1 also unavailable]")

    def test_budgeted_append_without_story_just_caps(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("E" * 5000, AcpError("x"), budget=2000) == "E" * 2000

    def test_negative_and_zero_budgets_mean_empty_not_unbounded(self) -> None:
        """A negative slice drops characters from the END — a negative budget
        must tighten the bound to nothing, never loosen it."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "s" * 50)
        assert append_fallback_story("E" * 100, exc, budget=0) == ""
        assert append_fallback_story("E" * 100, exc, budget=-5) == ""
        assert append_fallback_story("E" * 100, AcpError("x"), budget=-5) == ""

    def test_oversized_story_cannot_evict_the_error_text(self) -> None:
        """The error text keeps a floor of half the budget even when the story
        alone fills the whole budget (possible when a caller's cap equals
        _FALLBACK_STORY_CAP): past the floor the story tail truncates, the
        error never vanishes from the alert."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "s" * 500)
        out = append_fallback_story("E" * 400, exc, budget=500)
        assert len(out) == 500
        assert out.startswith("E" * 250)  # error floor = budget // 2
        assert "sss" in out  # the story is still present, truncated

    @pytest.mark.asyncio
    async def test_probe_never_raises_on_hostile_marker_read(self) -> None:
        """The marker property itself can raise (exotic providers) — the
        probe skips instead of propagating."""

        class _HostileProvider:
            served_model = "fb-1"
            _model = "fb-1"

            @property
            def _kc_active_fallback(self):  # TURN_FALLBACK_ATTR
                raise RuntimeError("hostile marker")

        await probe_fallback_restore(_HostileProvider())  # must not raise

    @pytest.mark.asyncio
    async def test_probe_never_raises_on_hostile_candidate_str(self) -> None:
        class _Boom:
            def __str__(self) -> str:
                raise RuntimeError("hostile id")

            def __bool__(self) -> bool:
                return True

        provider = AsyncMock()
        provider.served_model = "fb-1"
        provider._model = "fb-1"
        provider.set_model = AsyncMock()
        await probe_fallback_restore(provider, state=("primary-model", _Boom()))
        provider.set_model.assert_not_awaited()
