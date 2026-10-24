"""Tests for ACP client: compaction failure budgets, verdicts and context-stat resets."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient


class TestCompactionResetsContextStats:
    """A completed compaction must drop the stale context-usage counts.

    Without the drop, the pre-compaction
    counts carried an authoritative ``context_tokens_from_usage=True`` flag, so
    ``_track_metadata`` refused to apply any fresh percentage and every
    ``context_usage`` broadcast re-sent the old numbers — the dashboard bar
    never moved after a compact.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def _compaction_msg(self, status_type):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        return JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": status_type}, "summary": ""},
        )

    def test_completed_resets_counts_and_keeps_window(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        assert client.last_prompt_stats.context_pct == 75.0

        client._handle_compaction_status(self._compaction_msg("completed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        # The model did not change — the served window still holds.
        assert stats.context_window_tokens == 200_000

    def test_failed_keeps_counts(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("failed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 75.0
        assert stats.context_used_tokens == 150_000
        assert stats.context_tokens_from_usage is True

    def test_post_compaction_metadata_reapplies(self, tmp_path, monkeypatch):
        """Load-bearing: with the reset reverted, context_tokens_from_usage
        stays True and this fresh metadata percentage would be ignored,
        freezing the meter at the pre-compaction value forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("completed"))
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.0},
            )
        )

        assert client.last_prompt_stats.context_pct == 12.0

    def test_backfill_prefers_kept_served_window_over_registry(self, tmp_path, monkeypatch):
        """After the compaction reset the SERVED window survives (model
        unchanged) and can differ from the registry's static entry (opus
        served at 1M vs a 200K registry row). A metadata pct must derive
        against the kept served window, not clobber it with the registry."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))  # served 1M
        client._handle_compaction_status(self._compaction_msg("completed"))

        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 5.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 1_000_000  # served window kept
        assert stats.context_used_tokens == 50_000  # derived against it
        assert stats.context_pct == 5.0

    @pytest.mark.asyncio
    async def test_wait_for_compaction_grace_drains_post_metadata(self, tmp_path, monkeypatch):
        """Load-bearing: kiro emits the real post-compaction pct in a
        metadata frame ~1s AFTER the completed status (live-probe confirmed).
        wait_for_compaction must capture it so the dashboard broadcast
        reports accurate numbers instead of the unknown fallback."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        post_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed"), post_meta])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 5.0
        assert stats.context_window_tokens == 1_000_000
        assert stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_grace_drain_skips_metadata_without_percentage(self, tmp_path, monkeypatch):
        """A credits-only metadata frame (no contextUsagePercentage) must not
        end the grace drain — the real usage frame behind it would be
        stranded and the meter would fall back to the reset state."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        credits_only = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"meteringUsage": [{"unit": "credit", "amount": 0.1}]},
        )
        usage_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(
            side_effect=[self._compaction_msg("completed"), credits_only, usage_meta]
        )

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        assert client.last_prompt_stats.context_pct == 5.0
        assert client.last_prompt_stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_wait_for_compaction_without_post_metadata_leaves_reset(self, tmp_path):
        """No metadata within the grace window: the reset state stands (the
        meter shows unknown and self-corrects on the next turn)."""
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        # side_effect exhaustion after the completed status makes the grace
        # drain bail immediately instead of sleeping out the window.
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed")])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 200_000


class TestCompactionFailureTurnBudget:
    """A `failed` compaction must FAIL THE TURN, not hang it.

    kiro-cli can report compaction `failed` and then abandon the prompt it was
    compacting for: no session/prompt response and no end_turn ever arrive, so
    the read loop drained in silence to the caller's full prompt ceiling
    (hours) while the slot stayed occupied — the user waited it out or pressed
    Stop. The budget bounds that wait and the turn ends with
    STOP_REASON_COMPACTION_FAILED. No retry is attempted: compaction stays
    kiro-cli's, this only makes its failure fail cleanly.
    """

    def _failed_msg(self, params: dict | None = None):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        return JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params=params if params is not None else {"status": {"type": "failed"}},
        )

    def _silent_after(self, frames: list):
        """_read_message double: drain `frames`, then stay silent forever."""

        async def _read(timeout: float = 0.0):
            return frames.pop(0) if frames else None

        return _read

    @pytest.mark.asyncio
    async def test_silent_turn_after_failure_ends_at_budget(self, tmp_path, monkeypatch):
        """Armed by the failed status, the loop stops reading at the budget and
        marks the turn so the caller can terminate it explicitly."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        # Neither existing watchdog can save this turn: no text streamed and no
        # tool dispatched, so only the 2h prompt ceiling applied before the fix.
        client._stale_eligible = False
        client._tool_dispatched = False
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after([self._failed_msg()])

        actions = []
        t0 = time.monotonic()
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            actions.append(action)
            if action == "compaction":
                # What _dispatch_events does with the frame — the arming site.
                client._handle_compaction_status(msg)
        elapsed = time.monotonic() - t0

        assert actions == ["compaction"]
        assert client._compaction_failed_turn is True
        assert client._turn_done.is_set()
        # Prove the budget ended it, not the 30s outer deadline.
        # flake-ok: a lost-run ceiling over a 0.5 s budget, far above any scheduling pause it must tolerate; a virtual clock is tracked for a de-flake pass
        assert elapsed < 5.0, f"loop ran too long ({elapsed:.2f}s) — budget did not fire"

    @pytest.mark.asyncio
    async def test_failed_status_then_silence_completes_the_stream(self, tmp_path, monkeypatch):
        """Full path, the reported hang: a `failed` status arrives, the backend
        never answers the prompt, and the stream still terminates — with the
        compaction stop reason, well inside the 30s turn ceiling."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after(
            [self._failed_msg({"status": {"type": "failed", "error": "context too large"}})]
        )

        events: list[AcpEvent] = []
        t0 = time.monotonic()
        async for ev in client.stream_events("hello", timeout=30.0):
            events.append(ev)
        elapsed = time.monotonic() - t0

        assert [e.kind for e in events] == [EVENT_COMPACTION_STATUS, EVENT_COMPLETE]
        assert events[0].title == "context too large"
        assert events[-1].stop_reason == STOP_REASON_COMPACTION_FAILED
        # flake-ok: a lost-run ceiling over a 0.5 s budget, far above any scheduling pause it must tolerate; a virtual clock is tracked for a de-flake pass
        assert elapsed < 5.0, f"turn ran too long ({elapsed:.2f}s) — the hang is back"

    @pytest.mark.asyncio
    async def test_budget_is_disarmed_without_a_failure(self, tmp_path, monkeypatch):
        """No failed status → no budget: the loop runs to its own deadline, so
        an ordinary silent turn keeps its existing watchdog behavior."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.05)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._stale_eligible = False
        client._tool_dispatched = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True

        t0 = time.monotonic()
        async for _action, _msg in client._prompt_loop(req_id=1, timeout=0.4):
            pass
        elapsed = time.monotonic() - t0

        assert client._compaction_failed_turn is False
        assert elapsed >= 0.35, "loop exited early — the budget fired unarmed"

    @pytest.mark.asyncio
    async def test_completed_status_disarms_the_budget(self, tmp_path):
        """A retried compaction that succeeds must clear the armed failure, or
        the next silent stretch of a healthy turn would end it."""
        client = AcpClient(work_dir=tmp_path)
        client._handle_compaction_status(self._failed_msg())
        assert client._compaction_failed_at is not None

        client._handle_compaction_status(
            self._failed_msg({"status": {"type": "completed"}, "summary": "ok"})
        )
        assert client._compaction_failed_at is None

    @pytest.mark.asyncio
    async def test_frames_after_the_failure_defer_the_budget(self, tmp_path, monkeypatch):
        """The budget measures BACKEND SILENCE: a backend that keeps sending
        frames after a failed compaction is not reaped by it."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.3)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        client._handle_compaction_status(self._failed_msg())

        async def _steady_stream(timeout: float = 0.0):
            # flake-ok: pre-existing 50 ms pacing in the stubbed stream; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pacing inside the stubbed stream; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            return JsonRpcMessage(method=METHOD_METADATA, params={})

        client._read_message = _steady_stream

        actions = []
        async for action, _msg in client._prompt_loop(req_id=1, timeout=0.6):
            actions.append(action)

        # Ran to its own deadline while frames kept arriving.
        assert actions and all(a == "metadata" for a in actions)
        assert client._compaction_failed_turn is False

    @pytest.mark.asyncio
    async def test_dispatch_ends_turn_with_compaction_stop_reason(self):
        """The abandoned turn terminates with the explicit stop reason instead
        of raising the generic timeout, so the runner releases the slot."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # The budget fired: the loop stops reading with no `complete`.
            client._compaction_failed_turn = True
            if False:  # pragma: no cover - keeps this an async generator
                yield "complete", None

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_COMPACTION_FAILED
        # Single-shot: the marker must not leak into the next turn.
        assert client._compaction_failed_turn is False

    @pytest.mark.asyncio
    async def test_streamed_text_still_reports_compaction_failure(self):
        """A turn that streamed text before the failure must NOT be finalized as
        a normal end_turn by the stale-turn branch — the cause is the failure."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            client._stale_eligible = True
            client._compaction_failed_turn = True
            if False:  # pragma: no cover - keeps this an async generator
                yield "complete", None

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_COMPACTION_FAILED

    @pytest.mark.asyncio
    async def test_failed_event_title_carries_the_reason(self):
        """The notice reads AcpEvent.title. kiro-cli leaves `summary` empty on
        failure, which collapsed the row to "unknown error" — the raw
        notification's own reason now rides the title instead."""
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            METHOD_COMPACTION_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "status": {"type": "failed", "error": "context window exceeded"},
                "summary": "",
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert events[0].kind == EVENT_COMPACTION_STATUS
        assert events[0].text == "failed"
        assert events[0].title == "context window exceeded"

    @pytest.mark.asyncio
    async def test_consumer_park_is_not_charged_to_the_budget(self, tmp_path, monkeypatch):
        """A human approval parks the whole generator chain at _prompt_loop's
        single yield. That interval is CONSUMER time (mirrors the
        AcpSessionHandle park accounting): without the exclusion, the resume
        computes idle = the park length and reaps a live turn whose backend
        was only ever waiting for the approval answer."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._stale_eligible = False
        client._tool_dispatched = False
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after(
            [self._failed_msg(), JsonRpcMessage(method=METHOD_METADATA, params={})]
        )

        parked = False
        resumed_at = None
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            if action == "compaction":
                client._handle_compaction_status(msg)
            elif action == "metadata" and not parked:
                # The consumer holds this frame past the WHOLE budget — the
                # shape of a human answering an approval card slowly.
                parked = True
                # The assertion below measures from the end of this deliberate park.
                await asyncio.sleep(0.3)  # flake-ok: the consumer's park is the stimulus under test
                resumed_at = time.monotonic()
        ended_at = time.monotonic()

        # The budget still ends the (genuinely) silent turn — but measured
        # from the resume, not from the frame before the park.
        assert client._compaction_failed_turn is True
        assert parked and resumed_at is not None
        assert ended_at - resumed_at >= 0.15, (
            f"turn ended {ended_at - resumed_at:.2f}s after the consumer resumed - "
            "the park was charged to the backend-silence budget"
        )

    @pytest.mark.asyncio
    async def test_a_tool_in_flight_suspends_the_budget(self, tmp_path, monkeypatch):
        """kiro-cli can recover from a failed compaction and dispatch a tool. A
        legitimately silent long tool (a build, a spawned subagent) must NOT be
        reaped at the compaction budget — that would cancel valid work the
        tool-stall watchdog already governs on its own longer budget."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.05)
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after([self._failed_msg()])

        async def _drive(timeout: float) -> None:
            async for action, msg in client._prompt_loop(req_id=1, timeout=timeout):
                if action == "compaction":
                    client._handle_compaction_status(msg)
                    # The turn recovers and dispatches a tool, exactly as
                    # _dispatch_events would mark it.
                    client._tool_dispatched = True

        t0 = time.monotonic()
        await _drive(0.4)
        elapsed = time.monotonic() - t0

        # Ran to its own deadline: the budget stayed suspended for the whole of
        # the tool's silence, and the turn was not marked compaction-failed.
        assert client._compaction_failed_turn is False
        assert elapsed >= 0.35, f"loop exited at {elapsed:.2f}s — the budget reaped a live tool"
        # Still armed, so the budget re-fires once the tool resolves.
        assert client._compaction_failed_at is not None

    @pytest.mark.asyncio
    async def test_the_budget_re_arms_when_the_tool_resolves(self, tmp_path, monkeypatch):
        """Suspension is not a permanent disarm: once the tool resolves and the
        backend goes silent again, the abandoned turn is still ended."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.1)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        # A failed status, then one more frame standing in for the tool's own
        # traffic — after which the backend goes silent for good.
        client._read_message = self._silent_after(
            [self._failed_msg(), JsonRpcMessage(method=METHOD_METADATA, params={})]
        )

        t0 = time.monotonic()
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            if action == "compaction":
                client._handle_compaction_status(msg)
                client._tool_dispatched = True
            elif action == "metadata":
                # The tool resolved (what _dispatch_events does on its result).
                client._tool_dispatched = False
        elapsed = time.monotonic() - t0

        assert client._compaction_failed_turn is True
        # flake-ok: a lost-run ceiling over a 0.5 s budget, far above any scheduling pause it must tolerate; a virtual clock is tracked for a de-flake pass
        assert elapsed < 5.0, f"loop ran {elapsed:.2f}s — the budget did not re-arm"


class TestCompactionVerdictReachesTheConsumer:
    """The verdict is SET on the client/handle and READ off the provider the
    dashboard was handed, so every hop between them has to forward it. Without
    the forwarding the retry is dead code: the read falls to its default and a
    throttled compaction is indistinguishable from an overflowing one."""

    def test_the_abc_declares_the_capability_with_a_safe_default(self):
        """Declared on LLMProvider, not only on the Acp providers: the dashboard
        reads this off whatever provider it holds, so an adapter that never sets
        it must answer "permanent" from the contract rather than from a getattr
        default nobody wrote down. False is the safe value — it gives up the turn
        exactly as it did before the capability existed."""
        from kiro_crew.providers.base import LLMProvider

        assert isinstance(LLMProvider.last_compaction_transient, property)
        # Read through the descriptor rather than an instance: LLMProvider is
        # abstract, and what matters is the value the ABC itself answers with for
        # an adapter that does not override.
        assert LLMProvider.last_compaction_transient.fget(object()) is False
        # The VERDICT is the whole contract: the reason text is deliberately not
        # forwarded, because the chat row gets it from the compaction-status
        # event title and the log from each arming site's own WARNING.
        assert not hasattr(LLMProvider, "last_compaction_failure")

    def test_the_provider_forwards_the_verdict_from_its_inner_client(self):
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace(last_compaction_transient=True)
        assert prov.last_compaction_transient is True

    def test_the_session_provider_forwards_the_verdict_from_its_handle(self):
        from types import SimpleNamespace

        from kiro_crew.acp.session_provider import AcpSessionProvider

        sess = AcpSessionProvider.__new__(AcpSessionProvider)
        sess._handle = SimpleNamespace(last_compaction_transient=True)
        assert sess.last_compaction_transient is True

    def test_a_client_without_the_fields_reads_as_permanent(self):
        """A backend or a placeholder client that never set them must read as
        "not retryable" rather than raising — the fields are additive."""
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace()
        assert prov.last_compaction_transient is False

    def test_a_truthy_stand_in_is_not_a_verdict(self):
        """Coerced to a real bool at the boundary: an auto-created attribute (a
        Mock child, say) is truthy, and reading one as "transient" would replay
        a turn whose compaction genuinely could not succeed."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace(last_compaction_transient=MagicMock())
        assert prov.last_compaction_transient is False


class TestCompactionFailureIsTransient:
    """The verdict that splits "this will fail again identically" from "this had
    nothing wrong with it". Read from the payload, never the rendered notice —
    that text is truncated, redacted, and sometimes only a raw repr."""

    def test_a_throttled_summarization_call_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {
                "name": "ModelThrottleError",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
            }
        )

    def test_the_enum_spelling_of_the_reason_is_transient(self):
        """The machine reason arrives SCREAMING_SNAKE while the sentence beside
        it is prose, so a marker list matching only one spelling would classify
        the same fault differently depending on which field was filled."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient({"reason": "MODEL_TEMPORARILY_UNAVAILABLE"})

    def test_a_nested_5xx_status_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {"status": {"type": "failed"}, "error": {"cause": {"httpStatusCode": 503}}}
        )

    def test_a_429_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient({"error": {"httpStatusCode": 429}})

    def test_an_overflowing_conversation_is_not_transient(self):
        """The case the no-retry policy was written for: replaying it repeats
        the same overflow, so it must NOT be classified as retryable."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient(
            {"status": {"type": "failed", "reason": "context window exceeded"}}
        )

    def test_the_conversation_summary_cannot_flip_the_verdict(self):
        """Only reason-bearing keys are scanned. conversationSummary is
        backend-echoed, conversation-derived text riding in the very frame the
        KAS branch passes whole, so a summary that merely mentions a timeout must
        not upgrade a permanent overflow to transient — a control decision has to
        be unreachable from content the model wrote."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient(
            {
                "kind": "summarization_failed",
                "reason": "context window exceeded",
                "conversationSummary": (
                    "The user asked about a request timeout and rate limit "
                    "handling; we agreed the service unavailable path needs work."
                ),
            }
        )

    def test_a_reason_bearing_key_still_matches_beside_that_summary(self):
        """The scoping must not cost a real case: the same frame with a genuine
        throttle in a reason-bearing field is still transient."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {
                "kind": "summarization_failed",
                "conversationSummary": "An unrelated discussion of caching.",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
            }
        )

    @pytest.mark.parametrize(
        "frame",
        [
            # A validation or auth failure is answered by fixing the request,
            # not by sending it again.
            pytest.param({"error": {"httpStatusCode": 400}}, id="a_4xx_is_not_transient"),
            pytest.param({"status": {"type": "failed"}}, id="a_bare_failure_is_not_transient"),
            # ``bool`` is an ``int`` subclass, so a True under this key would
            # otherwise compare as 1 — and 1 is not a status code at all.
            pytest.param(
                {"error": {"httpStatusCode": True}}, id="a_boolean_is_not_read_as_a_status_code"
            ),
        ],
    )
    def test_not_transient(self, frame):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient(frame)

    def test_the_reason_and_the_verdict_read_the_same_frame(self):
        """Both readers are driven off one walker, so the reason a row DISPLAYS
        and the verdict that decides the RETRY cannot be derived from different
        views of the same frame. Separate calls rather than a paired helper,
        because only the verdict crosses the provider contract."""
        from kiro_crew.acp.client import (
            compaction_failure_detail,
            compaction_failure_is_transient,
        )

        frame = {
            "userFacingSessionErrorMessage": "High traffic — try another model.",
            "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
        }
        assert compaction_failure_detail(frame) == "High traffic — try another model."
        assert compaction_failure_is_transient(frame) is True
