"""#8560 — a fallback-served turn must attribute the row to the model that ran.

``chat_runner``'s post-turn persist blanks the caller-side model while a
throttle fallback is active and leaves the row's attribution entirely to
``persist_token_record_async``'s ``model_source`` walk. That walk succeeds for a
shallow provider, which is why the row usually carries the served id — but
``_wrapper_chain`` collects at most 8 nodes, so a session whose provider has
accumulated wrapper layers (session sharing, a channel link, a subagent
companion) hides its model-bearing node past the cap. The walk then reports
nothing, and the row lands blank: the turn's credits become unattributable even
though the slot knew exactly which model served them.

The served id is already on the slot as ``_active_fallback_model``, written only
after ``advance_fallback_candidate`` witnessed the ``set_model``, and cleared by
the start-of-turn restore probe once the primary is back. Passing it as the
caller-side model makes the row correct by construction instead of by a
coincidence in another module's traversal.
"""

import json

import pytest

# Aliased with a leading underscore so pytest does not COLLECT these borrowed
# classes a second time under this module (a bare `import Test...` name is
# collectible, which would re-run both suites here).
from test_dashboard_chat import TestRunChatModelFallback as _FallbackSuite
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite


def _deep_chain_client(stream, *, primary="primary-model", advertised=("fallback-model",)):
    """A provider whose model state sits BEYOND ``_wrapper_chain``'s 8-node cap.

    Mirrors the real nesting ``_wrapper_chain``'s docstring documents —
    ``AcpProvider`` -> ``client`` -> ``AcpSessionProvider`` -> ``_handle`` ->
    ``AcpSessionHandle`` -> ``_runtime`` — with the extra wrapper layers a
    long-lived shared session accumulates. ``set_model`` writes the handle's
    ``_model``/``_resolved_model_id`` exactly as ``AcpSessionHandle.set_model``
    does (``session_handle.py:1379-1384``); the cap is what hides them.
    """

    class _Runtime:
        def __init__(self):
            self._model = "process-level-arg"

    class _Handle:
        def __init__(self):
            self._model = primary
            self._resolved_model_id = primary
            self._runtime = _Runtime()

    class _SessionProvider:
        def __init__(self, handle):
            self._handle = handle
            self._runtime = handle._runtime

    class _Wrapper:
        def __init__(self, inner):
            self._client = inner
            self.client = inner

    handle = _Handle()
    node = _SessionProvider(handle)
    for _ in range(6):
        node = _Wrapper(node)

    class _Provider:
        def __init__(self, inner):
            self._client = inner
            self.client = inner
            self.stream = stream
            self.stream_command = stream
            # The public served_model seam the poisoned-conversation canary and
            # the fallback witness read; delegates to the handle like the real
            # AcpProvider.served_model property does.
            self._handle_ref = handle

        @property
        def served_model(self):
            return self._handle_ref._model or self._handle_ref._resolved_model_id

        def available_models(self):
            return [{"modelId": m} for m in advertised]

        def context_usage_pct(self):
            return 0.0

        def context_used_tokens(self):
            return 0

        def context_window_tokens(self):
            return 0

        async def set_model(self, model_id):
            handle._model = model_id
            handle._resolved_model_id = model_id

    return _Provider(node), handle


class TestFallbackServedTurnAttribution:
    _TRANSIENT = _FallbackSuite._TRANSIENT
    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)

    @staticmethod
    def _rows(shard_dir):
        if not shard_dir.exists():
            return []
        return [
            json.loads(line)
            for shard in sorted(shard_dir.glob("*.jsonl"))
            for line in shard.read_text().splitlines()
            if line.strip()
        ]

    @pytest.mark.asyncio
    async def test_row_names_the_fallback_that_served_when_the_chain_is_capped(
        self, tmp_path, monkeypatch
    ):
        """A fallback-served turn on a deeply-wrapped provider still attributes
        its credits to the model that ran, instead of landing in ``unknown``."""
        from unittest.mock import AsyncMock, patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )

        calls = 0

        async def _stream(msg):
            nonlocal calls
            calls += 1
            if calls <= TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=12.5))

        state = self._make_state(tmp_path, monkeypatch)
        client, handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        # Preconditions: the fallback really served the turn, and the walk that
        # the persist site relies on really cannot see it.
        assert slot._active_fallback_model == "fallback-model"
        assert handle._model == "fallback-model"
        assert usage_mod.read_turn_model(client) == "", (
            "precondition: the wrapper-chain walk must be blind here, otherwise "
            "this test is not exercising the capped-chain case"
        )

        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["credits"] == 12.5
        assert rows[0]["model"] == "fallback-model", (
            "a fallback-served turn's credits must name the model that ran; "
            f"got {rows[0]['model']!r} (read time renders a blank as 'unknown')"
        )

    @pytest.mark.asyncio
    async def test_non_fallback_turn_still_records_the_slot_model(self, tmp_path, monkeypatch):
        """REGRESSION PIN: with no fallback active the row keeps recording the
        slot's own model — the change must touch only the fallback branch."""
        from unittest.mock import AsyncMock, patch

        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=4.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = "pinned-model"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert slot._active_fallback_model == ""
        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["model"] == "pinned-model", rows[0]

    @pytest.mark.asyncio
    async def test_stale_sticky_fallback_is_not_billed_after_a_resume(self, tmp_path, monkeypatch):
        """GPT review finding on 3f55ca11f: the sticky candidate can OUTLIVE the
        swap it records.

        It deliberately survives a landed turn (`chat_runner.py:10941-10944` --
        the session stays on the fallback until the start-of-turn restore probe
        succeeds), that probe is skipped for a synthetic recovery message
        (`:7317-7320`), and a reset resumes the session by re-sending
        `slot.model` as a set_model override (`:1156-1159`). So a recovery turn
        can run on the PRIMARY with `_active_fallback_model` still set. The row
        must name what the live provider is serving, not the stale candidate.
        """
        from unittest.mock import AsyncMock, patch

        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=7.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot.model = "primary-model"
        # The post-resume state: the live session is back on the primary while
        # the sticky candidate still names the fallback, and the restore probe
        # has not run to clear it.
        slot._active_fallback_model = "fallback-model"
        slot._fallback_primary_model = "primary-model"
        slot._fallback_candidate_idx = 1  # non-zero: keeps the restore probe off
        assert handle._model == "primary-model", "precondition: session is on the primary"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        rows = self._rows(shard_dir)
        assert len(rows) == 1, rows
        assert rows[0]["model"] == "primary-model", (
            "a stale sticky fallback must not be billed: the live provider is "
            f"serving 'primary-model', got {rows[0]['model']!r}"
        )
