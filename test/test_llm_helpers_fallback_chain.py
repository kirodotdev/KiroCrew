"""The ``agent.fallback_model`` chain: which candidate is next, and the walk that applies it.

The candidate selector skips the active, unadvertised and malformed entries and
folds a namespace-qualified entry to its advertised spelling; the configured
chain derives its walk order from ``agent.fallback_model``. A ``set_model`` that
did not move the model is a no-op, never a transition, the usage-attribution
guard reads the sticky marker, and the walk seeds an empty active model as
``auto``. ``stream_and_collect`` walks the chain only once the same-model
transient budget is spent and nothing was streamed or mutated, gives each
candidate ``FALLBACK_CANDIDATE_ATTEMPTS`` tries, and surfaces the original error
class when the chain is exhausted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import ACP_BACKEND_CODEX
from kiro_crew.llm_helpers import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    TURN_FALLBACK_ATTR,
    FallbackState,
    next_fallback_candidate,
    stream_and_collect,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent


class TestNextFallbackCandidate:
    """Unit matrix for the throttle-fallback candidate selector."""

    _ADV = ["claude-opus-5", "claude-opus-4.8", "claude-opus-4.7"]

    def test_first_usable_candidate(self) -> None:
        chain = ["claude-opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-fable-5", self._ADV) == "claude-opus-5"

    def test_skips_active_model(self) -> None:
        chain = ["claude-opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-opus-5", self._ADV) == "claude-opus-4.8"

    def test_skips_active_model_case_insensitive(self) -> None:
        chain = ["Claude-Opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-opus-5", self._ADV) == "claude-opus-4.8"

    def test_skips_unadvertised_ids(self) -> None:
        chain = ["not-served-model", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "x", self._ADV) == "claude-opus-4.8"

    def test_empty_chain_returns_none(self) -> None:
        assert next_fallback_candidate([], "x", self._ADV) is None

    def test_exhausted_chain_returns_none(self) -> None:
        assert next_fallback_candidate(["not-served"], "x", self._ADV) is None

    def test_skips_garbage_entries(self) -> None:
        chain = ["", "  ", None, 42, "claude-opus-5"]  # type: ignore[list-item]
        assert next_fallback_candidate(chain, "x", self._ADV) == "claude-opus-5"

    def test_auto_is_a_candidate_when_advertised(self) -> None:
        # "auto" (the backend's availability-aware routing) is the default
        # fallback — it must be selectable like any other advertised id.
        adv = [*self._ADV, "auto"]
        assert next_fallback_candidate(["auto"], "claude-fable-5", adv) == "auto"

    def test_auto_skipped_when_not_advertised(self) -> None:
        # The fallthrough: a partition that does not serve "auto" skips it
        # (the original error then surfaces) rather than sending a no-op swap.
        assert next_fallback_candidate(["auto"], "claude-fable-5", self._ADV) is None

    def test_fails_open_on_empty_advertised(self) -> None:
        # Entitlement unknown is not entitlement denied (model_is_unusable's
        # stance); the substitute set_model re-validates against the live list.
        assert next_fallback_candidate(["some-model"], "x", []) == "some-model"
        assert next_fallback_candidate(["some-model"], "x", None) == "some-model"

    def test_namespace_qualified_entry_folds_to_advertised_bare_id(self) -> None:
        # A persisted chain entry can carry a stale ``<namespace>::<bare-id>``
        # qualifier from the catalog that advertised it, while the session
        # advertises the bare id. The walk must recognize it as
        # served instead of skipping toward exhaustion — and must return the
        # CHAIN's own spelling (``FallbackState.next_candidate`` locates the
        # applied candidate with ``remaining.index``).
        chain = ["openrouter::z-ai/glm-5.3-flash"]
        adv = ["z-ai/glm-5.3-flash", *self._ADV]
        assert next_fallback_candidate(chain, "claude-fable-5", adv) == (
            "openrouter::z-ai/glm-5.3-flash"
        )

    def test_qualified_entry_peeling_to_active_model_is_skipped(self) -> None:
        # Post-fold active skip: a qualified entry that resolves to the
        # currently-failing model cannot help and must still be skipped.
        chain = ["ns::model-x", "claude-opus-4.8"]
        adv = ["model-x", *self._ADV]
        assert next_fallback_candidate(chain, "model-x", adv) == "claude-opus-4.8"

    def test_entry_absent_under_both_spellings_still_skipped(self) -> None:
        # Deny-parity: the fold must not weaken the advertised filter — an id
        # the backend serves under NEITHER the full nor the peeled spelling
        # stays skipped.
        assert next_fallback_candidate(["ns::not-served"], "x", self._ADV) is None

    def test_verbatim_advertised_qualified_id_not_peeled(self) -> None:
        # An id advertised WITH its qualifier matches on the full id first;
        # peeling never rewrites a verbatim match.
        chain = ["ns::model-y"]
        adv = ["ns::model-y", "model-y"]
        assert next_fallback_candidate(chain, "x", adv) == "ns::model-y"

    def test_bare_pair_id_entry_folds_on_a_pair_id_backend(self) -> None:
        # A pair-id harness (codex) stores a chain entry in its BARE spelling,
        # while the session advertises ``<model>[effort]`` rows. With the
        # backend known, the bare entry widens to an advertised row and is
        # selected (returning the chain's own bare spelling for walk
        # bookkeeping) instead of being skipped as "not advertised".
        chain = ["openai.gpt-6-astra"]
        adv = ["openai.gpt-6-astra[high]", "openai.gpt-6-astra[max]"]
        assert (
            next_fallback_candidate(chain, "x", adv, backend=ACP_BACKEND_CODEX)
            == "openai.gpt-6-astra"
        )

    def test_bare_pair_id_entry_without_backend_keeps_verbatim_filter(self) -> None:
        # The empty-backend contract: with no backend, the generic fold answers
        # "" for the bare id against bracketed rows, so the entry is skipped --
        # exactly the pre-fix behaviour, unchanged for non-wire callers.
        chain = ["openai.gpt-6-astra"]
        adv = ["openai.gpt-6-astra[high]", "openai.gpt-6-astra[max]"]
        assert next_fallback_candidate(chain, "x", adv) is None


class TestConfiguredFallbackChain:
    """agent.fallback_model -> walk-order derivation (the one shared derivation)."""

    def _chain_for(self, value: str) -> tuple[str, ...]:
        from kiro_crew.llm_helpers import configured_fallback_chain

        cfg = MagicMock()
        cfg.agent.fallback_model = value
        with patch("kiro_crew.llm_helpers.KiroCrewConfig") as kc:
            kc.load.return_value = cfg
            return configured_fallback_chain()

    def test_empty_disables(self) -> None:
        # ROLLBACK PIN: fallback_model "" == pre-feature behavior everywhere.
        assert self._chain_for("") == ()

    def test_auto_default_yields_auto_only(self) -> None:
        assert self._chain_for("auto") == ("auto",)

    def test_concrete_id_yields_id_then_auto(self) -> None:
        # Fallthrough order: selected -> auto (-> backend default via
        # set_model's own resolve when auto is unserved).
        assert self._chain_for("claude-opus-4.8") == ("claude-opus-4.8", "auto")

    def test_load_failure_disables(self) -> None:
        from kiro_crew.llm_helpers import configured_fallback_chain

        with patch("kiro_crew.llm_helpers.KiroCrewConfig") as kc:
            kc.load.side_effect = RuntimeError("boom")
            assert configured_fallback_chain() == ()


class TestSetModelWitness:
    """A non-raising set_model that did not move the model is a NO-OP, not a
    transition — never published (walk) and never treated as restored (probe).
    """

    def _provider(self, model: str) -> MagicMock:
        provider = MagicMock()
        provider._model = model
        provider.served_model = None
        provider.available_models = None
        provider.set_model = AsyncMock()  # no side effect: _model never moves
        setattr(provider, TURN_FALLBACK_ATTR, None)
        return provider

    @pytest.mark.asyncio
    async def test_unwitnessed_swap_is_not_published(self) -> None:
        # set_model("claude-opus-4.8") silently no-ops (resolve collapsed it):
        # the walk must skip the candidate instead of publishing a marker for
        # a model that never took over.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider("primary-model")
        fb = FallbackState(chain=("claude-opus-4.8",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand is None
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_witnessed_swap_is_published(self) -> None:
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider("primary-model")

        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        fb = FallbackState(chain=("claude-opus-4.8",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand == "claude-opus-4.8"
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_noop_restore_keeps_the_marker(self) -> None:
        # An "auto" primary that resolves to "" restores nothing: the session
        # is still on the fallback, so the marker must survive for the next
        # probe (clearing it re-opens the backfill permanent-pin door).
        from kiro_crew.llm_helpers import probe_fallback_restore

        provider = self._provider("fallback-model")
        setattr(provider, TURN_FALLBACK_ATTR, ("auto", "fallback-model"))
        await probe_fallback_restore(provider, surface="test")
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("auto", "fallback-model")

    @pytest.mark.asyncio
    async def test_witnessed_restore_clears_the_marker(self) -> None:
        from kiro_crew.llm_helpers import probe_fallback_restore

        provider = self._provider("fallback-model")

        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fallback-model"))
        await probe_fallback_restore(provider, surface="test")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None


class TestProviderFallbackActive:
    """The shared usage-attribution guard reads the sticky marker."""

    def test_true_while_marker_present(self) -> None:
        from kiro_crew.llm_helpers import provider_fallback_active

        provider = MagicMock()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fallback-model"))
        assert provider_fallback_active(provider) is True

    def test_false_without_marker_or_malformed(self) -> None:
        from kiro_crew.llm_helpers import provider_fallback_active

        provider = MagicMock(spec=[])  # no marker attribute at all
        assert provider_fallback_active(provider) is False
        provider2 = MagicMock()
        setattr(provider2, TURN_FALLBACK_ATTR, "not-a-tuple")
        assert provider_fallback_active(provider2) is False
        provider3 = MagicMock()
        setattr(provider3, TURN_FALLBACK_ATTR, ("only-one",))
        assert provider_fallback_active(provider3) is False


class TestAdvanceFallbackCandidateAutoPrimary:
    """The shared walk seeds an empty active-model read as "auto".

    ``provider_active_model`` deliberately filters the ``"auto"`` sentinel, so
    an auto-routed session reads as ``""``. Left unseeded, that empty primary
    (a) let the dashboard's stale-clear arm skip the slot heal — a temporary
    fallback became a silent permanent pin — and (b) allowed an auto->auto
    no-op swap announced by a lying notice card.
    """

    def _provider(self) -> MagicMock:
        provider = MagicMock()
        provider._model = "auto"
        provider.served_model = None  # not a str -> ignored by the reader
        provider.available_models = None  # advertised unknown -> fail-open

        # Successful set_model syncs _model (real-provider behavior) so the
        # witness observes the switch.
        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        # No surviving marker: the getattr must yield a non-tuple.
        setattr(provider, TURN_FALLBACK_ATTR, None)
        return provider

    @pytest.mark.asyncio
    async def test_auto_chain_on_auto_session_is_exhausted_not_noop(self) -> None:
        # chain ("auto",) on an auto-routed session: nothing to fall back to —
        # the walk must exhaust (original error surfaces), never "swap" to the
        # model already serving.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        fb = FallbackState(chain=("auto",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand is None
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concrete_candidate_seeds_auto_primary(self) -> None:
        # chain (id, "auto") on an auto-routed session: the concrete candidate
        # applies, and the recorded primary is "auto" — the restore probe then
        # re-enters auto routing instead of tripping the empty-primary arm.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        fb = FallbackState(chain=("claude-opus-4.8", "auto"))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand == "claude-opus-4.8"
        assert fb.primary == "auto"
        marker = getattr(provider, TURN_FALLBACK_ATTR)
        assert marker == ("auto", "claude-opus-4.8")


class TestAdvanceFallbackCandidateNamespacedChain:
    """A qualified chain entry is applied under its ADVERTISED spelling.

    The chain keeps its own spelling for ``FallbackState`` bookkeeping
    (``remaining.index``), but everything later compared against the SERVED
    model — the substitute ``set_model`` call, the swap witness,
    ``fb_state.active``/``walked``, and the ``TURN_FALLBACK_ATTR`` marker the
    restore probe reads — must carry the advertised spelling. A qualified
    spelling there desynchronizes the restore probe: it reads the session as
    having moved off the fallback and clears the sticky state, so the slot
    heal never runs.
    """

    _BARE = "z-ai/glm-5.3-flash"
    _QUALIFIED = "openrouter::z-ai/glm-5.3-flash"

    def _provider(self) -> MagicMock:
        provider = MagicMock()
        provider._model = "claude-fable-5"
        provider.served_model = None  # not a str -> ignored by the reader
        provider.available_models = MagicMock(
            return_value=[{"modelId": self._BARE}, {"modelId": "claude-fable-5"}]
        )

        # Successful set_model syncs _model (real-provider behavior) so the
        # witness observes the switch.
        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        # No surviving marker: the getattr must yield a non-tuple.
        setattr(provider, TURN_FALLBACK_ATTR, None)
        return provider

    @pytest.mark.asyncio
    async def test_qualified_entry_applies_under_advertised_spelling(self) -> None:
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        fb = FallbackState(chain=(self._QUALIFIED, "auto"))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        # The walk applies the entry (no exhaustion), the wire carries the
        # advertised spelling, and every served-model comparison record agrees.
        assert cand == self._BARE
        provider.set_model.assert_awaited_once_with(self._BARE)
        assert fb.active == self._BARE
        assert fb.walked == [self._BARE]
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("claude-fable-5", self._BARE)
        # Bookkeeping still advanced past the CHAIN entry itself.
        assert fb.pos == 1

    @pytest.mark.asyncio
    async def test_qualified_entry_peeling_to_failing_model_is_skipped(self) -> None:
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        # The chain names the currently-failing model under a qualifier; the
        # walk must skip it (retrying it is what the walk exists to escape)
        # and exhaust rather than announce a no-op swap.
        fb = FallbackState(chain=("openrouter::claude-fable-5",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand is None
        provider.set_model.assert_not_awaited()


class TestFallbackState:
    """Chain-walk state: monotonic position, no candidate revisited."""

    def test_walks_in_order_and_exhausts(self) -> None:
        st = FallbackState(("m1", "m2", "m3"))
        adv = ["m1", "m2", "m3"]
        assert st.next_candidate("active", adv) == "m1"
        assert st.next_candidate("active", adv) == "m2"
        assert st.next_candidate("active", adv) == "m3"
        assert st.next_candidate("active", adv) is None
        assert st.next_candidate("active", adv) is None  # stays exhausted

    def test_skip_advances_past_unusable(self) -> None:
        st = FallbackState(("active-model", "unadvertised", "m2"))
        assert st.next_candidate("active-model", ["active-model", "m2"]) == "m2"
        assert st.next_candidate("active-model", ["active-model", "m2"]) is None


class TestStreamAndCollectThrottleFallback:
    """Case 2.75: throttle-exhaustion fallback chain (agent.fallback_model)."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @staticmethod
    def _provider(stream, advertised=("fb-1", "fb-2")):
        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = stream
        provider.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])
        provider.served_model = "primary-model"
        provider._model = "primary-model"

        # Mirror the real substitute set_model: a successful send syncs the
        # provider's model attrs (both AcpClient and AcpSessionProvider do).
        # The witness in advance_fallback_candidate/probe_fallback_restore
        # reads this to distinguish a real switch from a silent no-op.
        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_fallback_serves_turn_after_budget_exhaustion(self) -> None:
        """Same-model budget exhausts, the first advertised candidate is set
        and serves the turn; the sticky marker is published on the provider."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb-result"
        assert call_count == _TRANSIENT_RETRIES + 2
        provider.set_model.assert_awaited_once_with("fb-1")
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-1")

    @pytest.mark.asyncio
    async def test_two_attempts_per_candidate_then_advance(self) -> None:
        """A failing candidate gets exactly FALLBACK_CANDIDATE_ATTEMPTS
        attempts, then the chain advances to the next candidate."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0
        fail_until = _TRANSIENT_RETRIES + 1 + FALLBACK_CANDIDATE_ATTEMPTS

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= fail_until:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb2-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb2-result"
        # 4 same-model + 2 on fb-1 + 1 on fb-2 (success)
        assert call_count == fail_until + 1
        assert [c.args[0] for c in provider.set_model.await_args_list] == ["fb-1", "fb-2"]
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-2")

    @pytest.mark.asyncio
    async def test_chain_exhaustion_surfaces_original_error_class(self) -> None:
        """Every candidate fails: the ORIGINAL error class propagates, carrying
        the chain's story for the delivering surface."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        # 4 same-model + 2 per candidate × 2 candidates
        assert call_count == (_TRANSIENT_RETRIES + 1) + 2 * FALLBACK_CANDIDATE_ATTEMPTS
        story = getattr(ei.value, "_kc_fallback_story", "")
        assert "fb-1" in story and "fb-2" in story
        assert "primary-model" in story

    @pytest.mark.asyncio
    async def test_empty_chain_is_todays_behavior(self) -> None:
        """REGRESSION PIN: with no chain configured, behavior is byte-for-byte
        the pre-feature error surface — same attempt count, no set_model."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test")

        assert call_count == _TRANSIENT_RETRIES + 1
        provider.set_model.assert_not_awaited()
        assert not hasattr(ei.value, "_kc_fallback_story")

    @pytest.mark.asyncio
    async def test_non_transient_error_mid_chain_propagates_immediately(self) -> None:
        """An auth/validation error raised by a candidate fails fast — the
        chain is for transient throttles only."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            raise AcpError(self._AUTH)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert call_count == _TRANSIENT_RETRIES + 2  # one attempt on fb-1, then fatal
        assert "authentication" in str(ei.value)
        provider.set_model.assert_awaited_once_with("fb-1")

    @pytest.mark.asyncio
    async def test_partial_output_never_falls_back(self) -> None:
        """Streamed tokens block ALL retry paths, fallback included — a re-run
        would duplicate the already-emitted output."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
            raise AcpError(self._TRANSIENT)

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", fallback_models=["fb-1"])

        assert call_count == 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unadvertised_candidates_are_skipped(self) -> None:
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream, advertised=("fb-2",))
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "ok"
        provider.set_model.assert_awaited_once_with("fb-2")

    @pytest.mark.asyncio
    async def test_retry_transient_false_disables_fallback(self) -> None:
        """A caller that owns the outer transient loop also owns fallback
        policy — the inner chain must not fire."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(
                provider, "test", retry_transient=False, fallback_models=["fb-1"]
            )

        assert call_count == 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_completed_tool_call_disables_the_chain(self) -> None:
        """A textless tool mutation blocks same-model and fallback replays."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="delete customer records",
                tool_input={"record_ids": ["record-123"], "confirm": True},
            )
            raise AcpError(self._TRANSIENT)

        provider = self._provider(_stream)
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert call_count == 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marker_seeded_primary_survives_second_walk(self) -> None:
        """A session already sitting on a
        fallback (marker P->F1, restore failing) that exhausts again must
        keep P as the primary — never record F1 as the primary — and must
        not re-try the currently-failing F1."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb2-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _set_model(model_id):
            if model_id == "true-primary":
                # The primary is still throttled: the entry restore probe fails.
                raise AcpError(self._TRANSIENT)
            # Successful switch syncs the model attrs (real-provider behavior)
            # so the walk witness observes it.
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_set_model)
        setattr(provider, TURN_FALLBACK_ATTR, ("true-primary", "fb-1"))

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb2-result"
        # Restore probe tried the true primary; the walk then went straight to
        # fb-2 (skipping the currently-failing fb-1) and PRESERVED the true
        # primary in the marker — never fb-1.
        assert [c.args[0] for c in provider.set_model.await_args_list] == [
            "true-primary",
            "fb-2",
        ]
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("true-primary", "fb-2")
