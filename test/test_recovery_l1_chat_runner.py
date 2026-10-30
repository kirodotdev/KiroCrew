"""L1 of the recovery ladder end to end: handle classifies, chat runner retries.

The ACP handle owns the classification (one place, at the protocol layer);
chat_runner reads ``client.last_infra_error`` at end of turn and re-queues ONE
continuation on the shared schedule. The verdict reaches that consumer through
the provider wrapper chain (AcpProvider -> AcpSessionProvider -> handle), which
is exercised on real objects here. The runner branch is pinned at source level
because its host function is the turn phase ``_turn_ending_chain`` (``turn_ending``).

The two transient-5xx recovery waits share the L1 arm's shape — guard, then a
multi-second sleep, then a re-queue — so their post-backoff re-reads are pinned
here too, on the same slot/state fixtures. What each arm re-reads DIFFERS by
arm, and the reason is the payload: a verbatim replay of an un-run user message
is owed to the user, a continuation of a half-finished turn is not.

Besides those, this file pins the structural-terminal slot flag the auto-nudge loop reads
and the failed-cycle charge at the runner's terminal call sites, with its absence on a
pre-dispatch fault and on a retried turn. ``TestRunnerBranch`` holds the source-level pins
on the FRESH first-turn history debt; its end-to-end tests, the two post-token recovery
tests those pins' comments name among them, live in
``test_recovery_l1_chat_runner_first_turn_history.py``. Both files drive ``_run_chat`` with
the slot, state and error builders (``_RecordingSlot``, ``_l1_state``, ``_transient_error``)
in ``recovery_l1_chat_runner_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from recovery_l1_chat_runner_helpers import (
    _l1_state,
    _RecordingSlot,
    _transient_error,
)

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, JsonRpcMessage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_health as sh
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import TRANSIENT_NOTICE_GIVE_UP, TRANSIENT_NOTICE_META_KEY
from kiro_crew.dashboard.session_control import containment_meta
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMEvent, LLMProvider
from kiro_crew.recovery import ladder as lad


class _Runtime:
    def __init__(self, queue: asyncio.Queue) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._queue = queue

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


def _handle() -> AcpSessionHandle:
    queue: asyncio.Queue = asyncio.Queue()
    return AcpSessionHandle("sA", queue, _Runtime(queue))


def _tool_call(tool_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": "call a tool",
                "kind": "other",
                "status": "in_progress",
            },
        },
    )


def _tool_result(tool_id: str, text: str, status: str = "failed") -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": status,
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            },
        },
    )


_CAPACITY = 'MCP error -32001: {"class": "capacity", "retry_after_secs": 9} — gateway at capacity'


class TestHandleClassifies:
    def test_capacity_refusal_is_recorded_with_its_retry_hint(self):
        h = _handle()
        assert h.last_infra_error is None
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        assert h.last_infra_error is not None
        assert h.last_infra_error.error_class == lad.CLASS_CAPACITY
        assert h.last_infra_error.retry_after_secs == 9.0

    def test_a_later_ordinary_result_clears_it(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "file contents here", status="completed"))
        assert h.last_infra_error is None

    def test_a_later_output_less_completion_clears_it_too(self):
        """A tool that completes with NO output emits no result event at all, so
        the verdict is retired when the next call is DISPATCHED — otherwise the
        turn's consumer re-issues a call an intervening one superseded."""
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        h._handle_update(_tool_call("t2"))
        assert h.last_infra_error is None
        h._handle_update(_tool_result("t2", "", status="completed"))
        assert h.last_infra_error is None

    def test_an_ordinary_failure_is_not_infra(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", "Permission denied: /etc/shadow"))
        assert h.last_infra_error is None

    def test_a_new_turn_starts_clean(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        assert h.last_infra_error is not None
        # The per-turn reset is the same one that clears the stop reason.
        src = "".join(
            inspect.getsource(inspect.getmodule(member))
            for member in (AcpSessionHandle._run_turn, AcpSessionHandle.__init__)
        )
        idx = src.index("self.last_infra_error = None")
        assert 'self._last_stop_reason = ""' in src[:idx]

    def test_gateway_recoverable_infra_marker(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", "BackendGone: the pooled backend exited"))
        assert h.last_infra_error is not None
        assert h.last_infra_error.error_class == lad.CLASS_RECOVERABLE_INFRA


class _BareProvider(LLMProvider):
    """Implements ONLY the abstract surface: every capability is the ABC default."""

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def stream(self, message: str):
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, request_id) -> None:
        return None

    def context_usage_pct(self) -> float:
        return 0.0


class TestVerdictReachesTheConsumer:
    """The handle's verdict must survive both provider wrapper hops.

    Real objects, not source strings: the chat and sub-agent consumers hold
    ``AcpProvider`` (whose inner client is an ``AcpSessionProvider`` on the kiro
    path), never the handle, so a verdict that stops at the handle leaves the L1
    branch unreachable.
    """

    def _provider_with_capacity_refusal(self) -> tuple[AcpSessionHandle, AcpSessionProvider]:
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        return h, AcpSessionProvider(h, h._runtime)

    def test_session_provider_reads_through_to_the_handle(self):
        h, provider = self._provider_with_capacity_refusal()
        assert provider.last_infra_error is h.last_infra_error
        assert provider.last_infra_error.error_class == lad.CLASS_CAPACITY

    def test_the_forward_is_not_cached(self):
        h, provider = self._provider_with_capacity_refusal()
        assert provider.last_infra_error is not None
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "file contents here", status="completed"))
        assert provider.last_infra_error is None

    def test_an_empty_output_leaves_no_stale_candidate(self):
        h, provider = self._provider_with_capacity_refusal()
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "", status="completed"))
        assert provider.last_infra_error is None

    def test_acp_provider_reads_through_its_inner_client(self):
        _, inner = self._provider_with_capacity_refusal()
        outer = object.__new__(AcpProvider)
        outer._client = inner
        assert outer.last_infra_error is inner.last_infra_error
        assert outer.last_infra_error.error_class == lad.CLASS_CAPACITY

    def test_a_client_that_never_classifies_answers_no_verdict(self):
        """The placeholder AcpClient before the kiro swap, and the claude seam."""
        outer = object.__new__(AcpProvider)
        outer._client = MagicMock(spec=AcpClient)
        assert outer.last_infra_error is None

    def test_the_base_provider_default_is_no_verdict(self):
        assert _BareProvider().last_infra_error is None

    def test_the_forward_is_read_only(self):
        """The classifying handle is the sole writer: nothing else may fabricate a
        verdict, which is what would force a tool re-issue."""
        _, inner = self._provider_with_capacity_refusal()
        outer = object.__new__(AcpProvider)
        outer._client = inner
        for holder in (inner, outer, _BareProvider()):
            with pytest.raises(AttributeError):
                holder.last_infra_error = lad.InfraError(lad.CLASS_CAPACITY)

    def test_an_l1_wait_reads_as_recovering_with_its_own_cause(self):
        slot = SimpleNamespace(key="chat-1-1", running=True, _infra_retries=1)
        snap = sh.snapshot_slot(slot, mono_now=1000.0)
        assert snap.recovery_kinds == ["infra_capacityx1"]


class TestRunnerBranch:
    """Source-level pins on the L1 branch of the turn loop."""

    @pytest.fixture(scope="class")
    def src(self) -> str:
        from pathlib import Path

        # A turn-state copy (``turn.x = x``, written after a store so a later phase reads
        # the current value) or reload (``x = turn.x``, a phase's entry read) is plumbing
        # between the runner and its phases, not branch content: the character windows
        # below measure the code without it. A name a phase reads off the turn
        # (``turn.x``) is the runner's ``x``, so the windows read it in that spelling.
        stripped = re.sub(
            r"(?m)^[ \t]*(?:turn\.(\w+) = \1|(\w+) = turn\.\2)\n",
            "",
            "\n".join(
                turn_file.read_text(encoding="utf-8")
                for turn_file in [
                    *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                    Path(chat_runner.__file__),
                ]
                if turn_file.name != "__init__.py"
            ),
        )
        return re.sub(r"(?<![\w.])turn\.(?=\w)", "", stripped)

    def test_reads_the_handles_verdict_not_a_regex(self, src):
        assert 'isinstance(getattr(client, "last_infra_error", None), InfraError)' in src
        assert "default_ladder().observe_failure(" in src
        assert "L1_TOOL_CALL," in src

    def test_uses_every_sibling_guard(self, src):
        start = src.index('isinstance(getattr(client, "last_infra_error", None), InfraError)')
        branch = src[start : start + 1200]
        for guard in (
            "_prompt_depth == 0",
            "_stop_reason == STOP_REASON_END_TURN",
            "not _should_suppress_requeue(slot)",
            "_stop_gen_turn_start",
            "not _has_user_queued_followup(slot)",
            "_pending_steers",
        ):
            assert guard in branch or guard in src[start - 600 : start], guard

    @staticmethod
    def _retry_arm(src: str) -> str:
        """The retry arm, bounded by the give-up arm that follows it.

        A fixed character window silently stops covering the arm as it grows (the
        post-backoff re-check pushed the re-queue out of a 2500-char slice).
        """
        start = src.index("_l1 = default_ladder().observe_failure(")
        return src[start : src.index("_l1_escalated = True", start)]

    def test_retry_waits_then_requeues_a_continuation_not_the_message(self, src):
        body = self._retry_arm(src)
        assert "await _recovery_delay(_l1.delay_secs)" in body
        assert "build_infra_retry_prompt(" in body
        assert "payload=RecoveryPayload.CONTINUATION" in body
        assert "build_recovery_requeue(" not in body  # never a verbatim replay
        assert "_recovering_infra = True" in body

    def test_un_landed_turn_is_excluded_from_success_accounting(self, src):
        assert src.count("and not _recovering_infra") >= 3

    def test_a_landed_turn_closes_the_l1_run(self, src):
        assert "default_ladder().observe_success(L1_TOOL_CALL, slot.key)" in src

    def test_an_escalated_run_is_forgotten_not_recorded_as_recovered(self, src):
        assert "_l1_escalated = True" in src
        assert "default_ladder().forget(L1_TOOL_CALL, slot.key)" in src
        # The give-up must NOT reuse _recovering_infra: that flag also suppresses
        # turn settlement, the other budget resets and consolidation.
        esc = src.index("_l1_escalated = True")
        assert "_recovering_infra = True" not in src[esc : esc + 400]

    def test_the_wait_spends_its_own_budget_not_the_transient_5xx_one(self, src):
        body = self._retry_arm(src)
        assert "slot._infra_retries += 1" in body
        assert "slot._transient_5xx_retries += 1" not in body

    def test_the_l1_count_is_reset_on_every_no_requeue_exit(self, src):
        # The happy-path reset only runs when a cycle COMPLETES, so each arm that
        # ENDS the turn clears it too — otherwise the slot reads "recovering" on
        # the health panel until some later turn happens to land.
        assert src.count("slot._infra_retries = 0") == src.count("slot._transient_5xx_retries = 0")

    def test_first_turn_history_debt_arms_only_on_a_fresh_non_resumed_claim(self, src):
        # Armed from the FRESH observation; a natively-resumed session replayed
        # its own transcript and must never arm (that would duplicate context).
        # Anchor on the mark call, then look back for its guard — a different
        # `is_new and not resumed:` block (model-stamp) appears earlier.
        mark = src.index("mark_first_turn_history_owed(session_key)")
        guard = src.rindex("if is_new and not resumed:", 0, mark)
        assert mark - guard < 500, "the FRESH guard must directly gate the arm"

    def test_first_turn_history_debt_clears_only_on_assembled_and_accepted(self, src):
        # The build-arm settle in the finally fires only when BOTH held: a
        # non-slash context-bearing turn ASSEMBLED its history
        # (_first_turn_history_assembled, set in the build arm) AND the turn's
        # TERMINAL was a normal end_turn the provider kept — a non-synthetic
        # STOP_REASON_END_TURN with no empty-response verdict — OR a COMPLETED
        # mid-turn compaction. The settle keys on the TERMINAL ITSELF, NOT on
        # _turn_landed: _turn_landed is a reliability-metric flag left False for
        # promise-only / leaked-tool / infra-recovery turns, which still ended
        # normally and were kept, so they MUST settle. A pre-output 5xx (no
        # terminal), a cancelled Stop (discarded) and an empty-response re-queue
        # all keep the debt armed; a STARTED-but-not-completed compaction is not
        # yet durable and also stays armed.
        delivered = src.index("_first_turn_history_delivered = (")
        dwin = src[delivered : delivered + 700]
        # The fix: the predicate must NOT gate on _turn_landed (that excluded the
        # promise-only / leaked-tool / infra-recovery terminals the reviewer named).
        assert "_turn_landed" not in dwin
        assert "_stop_reason == STOP_REASON_END_TURN" in dwin
        assert "not _terminal_synthetic" in dwin
        assert re.search(r"not (?:turn\.)?_had_empty_response_verdict", dwin)
        # B1 (buluoray / iamwhatever): an exit that KEEPS the session also settles.
        # Once output streamed (_turn_emitted) the live session already holds the
        # history, regardless of how the turn then ENDS (a non-transient AcpError /
        # timeout / generic except AFTER output takes the terminal else, sets no
        # end_turn and does NOT reset the session). Settle there too — excluded
        # only on a cancel (discarded) or an empty-response verdict (re-queued).
        assert "_turn_emitted" in dwin
        assert "_stop_reason != STOP_REASON_CANCELLED" in dwin
        # A completed mid-turn compaction keeps the summarized session and the
        # continuation runs on it, so it settles — the landed test cannot see it
        # because _recovering_compaction is itself a guard on the landed block.
        assert "_recovering_compaction and _compaction_completed" in dwin
        settle = re.search(
            r"if (?:turn\.)?_first_turn_history_assembled and _first_turn_history_delivered:", src
        ).start()
        window = src[settle : settle + 400]
        assert "consume_first_turn_history_owed(session_key)" in window
        # The rejected imprecise boundaries must be gone entirely.
        assert "if _turn_landed:" not in window
        assert not re.search(
            r"if (?:turn\.)?_first_turn_history_assembled and (?:turn\.)?_turn_emitted:", src
        )
        assert "(_turn_emitted or _saw_terminal_event)" not in src
        # The assembled flag is raised in BOTH the builder and the builder-less
        # fallback context-build arms (a standalone dashboard without a builder
        # must settle too, not re-prepend history every warm turn).
        arm = "if _context_is_new and not _provider_has_history:"
        flagged = [
            m.start()
            for m in re.finditer(re.escape(arm), src)
            if "_first_turn_history_assembled = True" in src[m.start() : m.start() + 1600]
        ]
        assert len(flagged) >= 2, flagged
        assert src.count("_first_turn_history_assembled = True") >= 2
        # The suppression arm pays the debt directly (a reset asked to forget).
        assert src.count("consume_replay_suppression(session_key)") >= 2
        # A confirmed native clear retires the debt beside the sibling replay lease
        # — otherwise a fresh /clear leaves it armed and the next prompt rebuilds
        # the history the user dropped.
        clear = src.index("async def _on_clear_status(")
        assert "consume_first_turn_history_owed(session_key)" in src[clear : clear + 1200]
        # Post-token same-session recovery settles the debt, but ONLY when the
        # turn assembled history (``_first_turn_history_assembled``): a slash first
        # turn streams native output and can reach this arm, yet it assembled
        # nothing, so it must not clear a debt it never paid. The arm was extracted
        # into ``_recover_posttoken_transient``; ``chat_runner`` passes the flag,
        # and the guarded consume sits BEFORE the recover/suppress branch (so a
        # follow-up takeover settles it too).
        assert "_first_turn_history_assembled=_first_turn_history_assembled" in src
        # The guarded post-token consume itself lives in
        # ``acp_recovery._recover_posttoken_transient`` (``if
        # _first_turn_history_assembled: consume_first_turn_history_owed(...)``
        # before the recover/suppress branch, so a follow-up takeover settles it
        # too). That is asserted BEHAVIOURALLY — not by reading the helper's
        # source — in ``test_posttoken_recovery_settles_the_debt`` and
        # ``test_posttoken_followup_takeover_still_settles_the_debt``.
        # It is read (not cleared) into the context-new composition.
        assert "first_turn_history_owed_pending(session_key) is True" in src
        assert "_context_is_new = is_new or _replay_pending or _first_turn_history_owed" in src


class TestContinuationPrompt:
    def test_opens_with_the_refusal_card_marker(self):
        msg = chat_runner.build_infra_retry_prompt("capacity", 9.0)
        assert msg.split("\n", 1)[0] == REFUSAL_RECOVERY_PREFIX
        assert "9s" in msg
        assert "same arguments" in msg
        assert "Do not repeat any earlier tool call" in msg

    def test_without_a_hint(self):
        msg = chat_runner.build_infra_retry_prompt("recoverable_infra", None)
        assert "pause" not in msg
        assert "recoverable_infra" in msg


def _notices(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


async def _turn_interrupted_during_the_backoff(tmp_path, key, deliver):
    """One end_turn turn whose last tool result was an infra refusal.

    ``deliver(state, slot)`` runs INSIDE the L1 backoff seam, so it stands in for
    an interrupt that arrives while the slot waits: the arm's guards were all
    clear when it decided to retry. Returns the slot, how many times the model was
    prompted (1 = the aborted turn was not re-prompted) and every outage duration
    the ladder measured (a run the retry never ran must measure none).
    """
    state, client = _l1_state(tmp_path)
    slot = _RecordingSlot(key)
    lad.default_ladder().forget(lad.L1_TOOL_CALL, slot.key)
    prompts: list[str] = []

    def _stream(message, *_a, **_kw):
        prompts.append(message)
        if len(prompts) > 1:
            # Only the first turn ends on the refusal; a recovery turn that ran
            # must not recurse into the same arm.
            client.last_infra_error = None

        async def _gen():
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="That call was refused for capacity.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    clear: dict[str, object] = {}

    async def _seam(secs):
        # What the arm saw when it committed to the retry, so the interrupt below
        # is the only thing that moved.
        clear["suppressed"] = chat_runner._should_suppress_requeue(slot)
        clear["stop_gen"] = slot._stop_generation
        clear["followup"] = chat_runner._has_user_queued_followup(slot)
        clear["steers"] = bool(slot._pending_steers)
        clear["infra_retries"] = slot._infra_retries
        deliver(state, slot)

    measured: list[float] = []

    def _histogram(name, value, *_a, **_kw):
        if name == lad.RECOVERY_DURATION_SECS:
            measured.append(value)

    try:
        with patch.object(chat_runner, "_recovery_delay", _seam):
            with patch.object(lad, "emit_histogram", _histogram):
                with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                    mock_sel.return_value = MagicMock()
                    await _run_chat(state, slot, "hello")
                    if slot.task:
                        await slot.task
    finally:
        lad.default_ladder().forget(lad.L1_TOOL_CALL, slot.key)

    assert clear == {
        "suppressed": False,
        "stop_gen": 0,
        "followup": False,
        "steers": False,
        # The wait is counted against the slot's OWN budget while it runs.
        "infra_retries": 1,
    }, "the arm must have entered the wait with every interrupt signal clear"
    return SimpleNamespace(slot=slot, prompts=len(prompts), measured=measured)


class TestAnInterruptDuringTheL1Backoff:
    """The arm re-reads stop / steer / follow-up AFTER the multi-second wait.

    Checking them only before it means a Stop, steer or follow-up that lands
    during the backoff is ignored and the abandoned turn is re-prompted anyway:
    the interrupt resolves while no prompt is active, and the dispatch-point
    purge covers the promise-only and post-compaction continuations only.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "press",
        [
            # Pressed and already resolved back to idle: invisible to the state
            # check, recorded by the monotonic generation.
            pytest.param(lambda slot: setattr(slot, "_stop_generation", 1), id="resolved"),
            pytest.param(lambda slot: setattr(slot, "_stop_state", "soft"), id="in-flight"),
        ],
    )
    async def test_a_stop_drops_the_requeue(self, tmp_path, press):
        run = await _turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-l1stop", lambda _state, slot: press(slot)
        )

        assert run.slot.recovery_inserts == []
        assert run.prompts == 1, "the stopped turn was re-prompted"
        # The attempt paid for a retry that never ran, so it is handed back —
        # a standing count shortens the next real ladder and reads as
        # "recovering" on the health panel for an idle slot.
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        # The run is dropped, not closed as recovered: no retry ran, so nothing
        # observed the dependency come back and no outage may be measured.
        assert run.measured == []
        # The "retrying it in Ns" card is persisted; it must not stand as the
        # last word on a retry that never happened.
        assert _notices(run.slot)[-1] == (
            "ℹ️ The capacity retry was cancelled — the turn was stopped, "
            "the call was not retried."
        )

    @pytest.mark.asyncio
    async def test_a_steer_drops_the_requeue(self, tmp_path):
        def _steer(_state, slot):
            text = "forget that — read the gateway log instead"
            slot._pending_steers = [text]
            # See the module-level `_steer`: the composer records provenance AND
            # the admission alongside the append, and without the admission the
            # requeued entry carries no containment key and the drain drops it on
            # its fail-closed floor instead of letting the person's message take
            # over.
            slot._steer_user_origin[text] = True
            slot._steer_admissions[text] = containment_meta(_state, slot)

        run = await _turn_interrupted_during_the_backoff(tmp_path, "chat-1-l1steer", _steer)

        assert run.slot.recovery_inserts == []
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        assert run.measured == []
        assert _notices(run.slot)[-1].endswith("your message takes over.")

    @pytest.mark.asyncio
    async def test_a_user_followup_drops_the_requeue(self, tmp_path):
        def _followup(state, slot):
            slot.queue_insert(
                0,
                "never mind — summarise what you have",
                meta=containment_meta(state, slot),
            )

        run = await _turn_interrupted_during_the_backoff(tmp_path, "chat-1-l1followup", _followup)

        # The continuation would have been inserted at index 0, ahead of the
        # message the user typed while the slot waited.
        assert run.slot.recovery_inserts == []
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        assert run.measured == []
        assert _notices(run.slot)[-1].endswith("your message takes over.")

    @pytest.mark.asyncio
    async def test_an_uninterrupted_backoff_still_requeues_the_continuation(self, tmp_path):
        run = await _turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-l1plain", lambda _state, _slot: None
        )

        assert len(run.slot.recovery_inserts) == 1
        assert run.slot.recovery_inserts[0].startswith(REFUSAL_RECOVERY_PREFIX)
        assert run.prompts == 2, "the retry never reached the model"
        assert not any("cancelled" in n for n in _notices(run.slot))
        # The retry landed, so THIS run closes as recovered and the outage is
        # measured — the contrast that makes the interrupted cases above mean
        # something.
        assert len(run.measured) == 1


@pytest.mark.asyncio
async def test_recovery_delay_seam_sleeps_only_for_positive_values(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(chat_runner.asyncio, "sleep", fake_sleep)
    await chat_runner._recovery_delay(0.0)
    await chat_runner._recovery_delay(0.05)
    assert slept == [0.05]


def _errors(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "error"]


def _give_up_rows(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if (m.get("meta") or {}).get(TRANSIENT_NOTICE_META_KEY) == TRANSIENT_NOTICE_GIVE_UP
    ]


async def _transient_turn_interrupted_during_the_backoff(tmp_path, key, deliver, *, post_token):
    """One turn that fails with a transient 5xx, interrupted INSIDE the backoff.

    ``post_token`` picks the arm: False streams nothing before the failure (the
    same-model verbatim-replay arm), True streams a partial first (the one-shot
    CONTINUE arm). ``deliver(state, slot)`` runs in the wait seam, so the arm had
    already decided to retry with every signal clear.
    """
    state, client = _l1_state(tmp_path)
    # Not an L1 turn: the infra verdict must not claim this failure.
    client.last_infra_error = None
    slot = _RecordingSlot(key)
    prompts: list[str] = []

    def _stream(message, *_a, **_kw):
        prompts.append(message)
        first = len(prompts) == 1

        async def _gen():
            if post_token:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the first half of the answer ")
            if first:
                raise _transient_error()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest of the answer")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    clear: dict[str, object] = {}

    async def _seam(secs):
        clear["suppressed"] = chat_runner._should_suppress_requeue(slot)
        clear["stop_gen"] = slot._stop_generation
        clear["followup"] = chat_runner._has_user_queued_followup(slot)
        clear["steers"] = bool(slot._pending_steers)
        clear["multi_second_wait"] = secs >= 2.0
        clear["counted_attempt"] = slot._transient_5xx_retries
        clear["one_shot_spent"] = slot._posttoken_retry_used
        deliver(state, slot)

    with patch.object(chat_runner, "_recovery_delay", _seam):
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

    assert clear == {
        "suppressed": False,
        "stop_gen": 0,
        "followup": False,
        "steers": False,
        "multi_second_wait": True,
        # The pre-wait state each arm owes back when it drops the re-queue: the
        # replay arm counted an attempt, the CONTINUE arm has not yet spent its
        # one-shot (it is consumed below the wait, which is the whole point).
        "counted_attempt": 1 if not post_token else 0,
        "one_shot_spent": False,
    }, "the arm must have entered the wait with every interrupt signal clear"
    return SimpleNamespace(slot=slot, prompts=len(prompts))


def _press_stop_resolved(_state, slot):
    """Pressed and already back to idle: only the monotonic counter records it."""
    slot._stop_generation = 1


def _press_stop_in_flight(_state, slot):
    slot._stop_state = "soft"


def _steer(_state, slot):
    text = "forget that — read the gateway log instead"
    slot._pending_steers.append(text)
    # What `steer_into_running_turn` records, and the reason it has to be recorded
    # here too: assigning `_pending_steers` skips the ONLY production writer of
    # that list (`chat_delivery.steer_into_running_turn`), which registers both of
    # these in lockstep with the append.
    #
    # `_steer_user_origin` is the provenance the requeue reads for
    # `directive_user_origin`; it defaults to False so a peer's `session_send`
    # steer cannot inherit the exemption a person typing into this session has.
    # `_steer_admissions` is the containment the send was authorized against;
    # absent, the requeued entry carries no containment key and the drain drops it
    # on its fail-closed floor. This helper simulates a COMPOSER steer, so it
    # reports both the way the composer does.
    slot._steer_user_origin[text] = True
    slot._steer_admissions[text] = containment_meta(_state, slot)


def _followup(state, slot):
    slot.queue_insert(0, "never mind — summarise what you have", meta=containment_meta(state, slot))


_STOPS = [
    pytest.param(_press_stop_resolved, id="resolved"),
    pytest.param(_press_stop_in_flight, id="in-flight"),
]


class TestAStopDuringTheSameModelTransientBackoff:
    """The verbatim-replay arm re-reads the STOP signals after its backoff.

    Its guard runs BEFORE a multi-second sleep, and a Stop pressed during that
    sleep resolves while no prompt is active: the dispatch-point purge only
    covers the promise-only and post-compaction continuations, so nothing else
    drops a replay the user has cancelled.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("press", _STOPS)
    async def test_a_stop_drops_the_replay(self, tmp_path, press):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxstop", press, post_token=False
        )

        assert run.slot.recovery_inserts == []
        assert run.prompts == 1, "the cancelled message was re-prompted anyway"
        # A NO-REQUEUE exit ends the turn, so it owes the same per-turn budget
        # refresh as the landed and terminal arms: an attempt left counted for a
        # retry that never ran shortens the next real ladder and inflates its
        # backoff seed, and a stale walk index suppresses the restore probe.
        assert run.slot._transient_5xx_retries == 0
        assert run.slot._infra_retries == 0
        assert run.slot._fallback_candidate_idx == 0
        assert run.slot._fallback_walked == []
        # The "retrying…" row is persisted and carries the PENDING token; without
        # a correction the card reads "retrying…" forever for a retry that never
        # happened. The give-up token is what returns the Continue affordance.
        assert _errors(run.slot)[-1] == "⟳ Connection unstable — please try again."
        assert len(_give_up_rows(run.slot)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deliver",
        [
            # Deliberately NOT re-read by this arm — see the class below.
            pytest.param(_steer, id="steer"),
            pytest.param(_followup, id="followup"),
        ],
    )
    async def test_an_uninterrupted_backoff_still_replays_the_message(self, tmp_path, deliver):
        """And so does a steer or a follow-up: this arm owes the user the replay."""
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxplain", deliver, post_token=False
        )
        assert run.slot.recovery_inserts == ["hello"]
        assert _give_up_rows(run.slot) == []

    @pytest.mark.asyncio
    async def test_no_interrupt_at_all_still_replays_the_message(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxnone", lambda _s, _slot: None, post_token=False
        )
        assert run.slot.recovery_inserts == ["hello"]
        assert run.prompts == 2, "the replay never reached the model"
        assert _give_up_rows(run.slot) == []
        assert not any("please try again" in t for t in _errors(run.slot))


class TestTheReplayArmDoesNotDropOnASteerOrAFollowUp:
    """The exclusion is deliberate, and pinned so a consistency sweep cannot add it.

    This arm runs on ``not _turn_emitted``: no token and no tool call landed, so
    there is nothing for a correction to contradict and the user's request is
    still entirely un-run. Dropping it would erase that request with no output
    anywhere in the transcript. Ordering makes both intents survive instead: the
    replay is inserted at the HEAD, so a follow-up typed during the backoff runs
    after it, and an unconsumed steer is degraded to a head card by
    ``_requeue_unconsumed_steers`` in the same finally, which dequeues FIRST.
    """

    @pytest.mark.asyncio
    async def test_a_followup_is_queued_behind_the_replay_not_instead_of_it(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxorder", _followup, post_token=False
        )

        # The replay goes in at the HEAD, ahead of the message typed during the
        # wait, so BOTH intents survive and run in the order the user sent them.
        # This ordering is the reason the arm may leave a follow-up un-checked;
        # were the replay queued behind, dropping it would be the only option.
        assert run.slot.queue_after_recovery == [["hello", "never mind — summarise what you have"]]

    @pytest.mark.asyncio
    async def test_an_unconsumed_steer_dequeues_ahead_of_the_replay(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxsteerorder", _steer, post_token=False
        )

        # At insert time the replay is alone at the head; the finally's
        # `_requeue_unconsumed_steers` then degrades the steer to a head card
        # ABOVE it, so the correction runs first and the replay follows.
        assert run.slot.queue_after_recovery == [["hello"]]
        assert run.slot.recovery_inserts == ["hello"]


class TestAnInterruptDuringThePostTokenBackoff:
    """The one-shot CONTINUE arm re-reads the FULL set after its backoff.

    Unlike the replay arm, what it re-queues is a continuation of a turn that
    already streamed: the partial is persisted and on screen before the wait, so
    a follow-up typed during it is the user answering that partial, and the
    continuation is inserted AHEAD of that message. Dropping costs nothing.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deliver",
        _STOPS
        + [
            pytest.param(_steer, id="steer"),
            pytest.param(_followup, id="followup"),
        ],
    )
    async def test_an_interrupt_drops_the_continuation(self, tmp_path, deliver):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-ptstop", deliver, post_token=True
        )

        assert run.slot.recovery_inserts == []
        # The one-shot is NOT spent: an allowance burned on a retry that never
        # ran silently disarms the NEXT genuine turn's only recovery.
        assert run.slot._posttoken_retry_used is False
        # Append-only: the streamed partial stays, and the "resuming…" row is
        # corrected rather than retracted.
        assert any("the first half of the answer" in m["content"] for m in run.slot.messages)
        assert _errors(run.slot)[-1] == "⟳ Connection unstable — please try again."
        assert len(_give_up_rows(run.slot)) == 1

    @pytest.mark.asyncio
    async def test_an_uninterrupted_backoff_still_queues_the_continuation(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-ptnone", lambda _s, _slot: None, post_token=True
        )

        assert len(run.slot.recovery_inserts) == 1
        assert run.slot.recovery_inserts[0] == chat_runner._POSTTOKEN_RECOVER_MSG
        assert run.prompts == 2, "the continuation never reached the model"
        # The one-shot IS spent when a recovery really is enqueued — the contrast
        # that makes the interrupted cases above mean something.
        assert run.slot._posttoken_retry_used is True
        assert _give_up_rows(run.slot) == []


class TestStructuralTerminalSlotFlag:
    """A malformed-request terminal turn must LEAVE a mark on the slot, and a
    genuine new turn must CLEAR it.

    The auto-nudge fire path reads ``slot._last_turn_structural_terminal`` to
    stop a self-prompting loop that would otherwise re-fire an identical context
    the backend rejected for its shape. The two ends of that contract live in
    chat_runner: the terminal-error branch SETS it from the exception's
    ``structural_terminal`` verdict, and genuine-turn-start CLEARS it so a human
    /clear-then-message re-arms the loop. Exercised end to end through
    ``_run_chat`` on the real slot/state fixtures.
    """

    def _malformed_state(self, tmp_path):
        state, client = _l1_state(tmp_path)
        # Straight to the terminal branch: no L1 infra verdict in play.
        client.last_infra_error = None

        # Build the exception with the structural tag set, exactly as
        # _raise_acp_error would for an "Improperly formed request" frame.
        def _raise_malformed(_message, *_a, **_kw):
            err = AcpError("The request was rejected as malformed.", transient=False)
            err.structural_terminal = True
            raise err

        client.stream = MagicMock(side_effect=_raise_malformed)
        return state, client

    @pytest.mark.asyncio
    async def test_malformed_terminal_turn_sets_the_flag(self, tmp_path):
        state, _client = self._malformed_state(tmp_path)
        slot = _RecordingSlot("chat-1-malformed")
        assert slot._last_turn_structural_terminal is False
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            # A self-driven nudge fire is the only producer that may set the
            # flag (the fire path passes _directive_self_wake=True); the loop id
            # is recorded so the verdict is scoped to that loop.
            await _run_chat(
                state,
                slot,
                "please do the thing",
                _directive_self_wake=True,
                _directive_loop_id="loop-42",
                _directive_loop_gen=7,
            )
            if slot.task:
                await slot.task
        assert (
            slot._last_turn_structural_terminal is True
        ), "a malformed self-wake turn left no signal for the nudge loop to read"
        assert (
            slot._last_turn_structural_terminal_loop_id == "loop-42"
        ), "the verdict was not scoped to the firing loop"
        assert (
            slot._last_turn_structural_terminal_loop_gen == 7
        ), "the verdict was not scoped to the firing loop's config generation"

    @pytest.mark.asyncio
    async def test_oversized_request_terminal_turn_sets_the_flag(self, tmp_path):
        """kiro-cli's own "too large to send" refusal must arm the guard too.

        Raised through the REAL classifier rather than a pre-tagged exception, so
        this pins the whole chain the loop stop depends on: the raw -32603 frame
        kiro-cli's ACP server emits for an irreducible oversized request (the
        sentence in ``data``, boilerplate in ``message``) -> ``_raise_acp_error``
        tags it structural -> the terminal branch records the verdict for the
        firing loop. Before the classifier knew this sentence the turn ended as a
        plain terminal error and the loop re-fired the same doomed context every
        interval until ``max_cycles``.
        """
        from kiro_crew.acp.transport_errors import _raise_acp_error

        state, client = _l1_state(tmp_path)
        client.last_infra_error = None
        oversized_frame = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "This message is too large to send, and it contains no text that can "
                "be shortened. Remove or reduce the attached content and try again."
            ),
        }

        def _raise_oversized(_message, *_a, **_kw):
            _raise_acp_error(oversized_frame)

        client.stream = MagicMock(side_effect=_raise_oversized)
        slot = _RecordingSlot("chat-1-oversized")
        assert slot._last_turn_structural_terminal is False
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(
                state,
                slot,
                "[auto-nudge cycle 12]\nartifact screenshot: shot.png",
                _directive_self_wake=True,
                _directive_loop_id="loop-oversized",
                _directive_loop_gen=3,
            )
            if slot.task:
                await slot.task
        assert (
            slot._last_turn_structural_terminal is True
        ), "an oversized self-wake turn left no signal for the nudge loop to read"
        assert slot._last_turn_structural_terminal_loop_id == "loop-oversized"
        assert slot._last_turn_structural_terminal_loop_gen == 3
        # The user sees kiro-cli's own sentence, not a retry suggestion.
        errors = [m["content"] for m in slot.messages if m.get("role") == "error"]
        assert errors, "the terminal turn appended no error row"
        assert "too large to send" in errors[-1]

    @pytest.mark.asyncio
    async def test_a_human_malformed_turn_does_not_set_the_flag(self, tmp_path):
        """A HUMAN turn that happens to be malformed must NOT arm the guard.

        The flag stops the slot's nudge loop, so it must reflect the LOOP's OWN
        cycle. A human message on a slot that also carries an active loop is not
        the loop firing; setting the flag there would stop a loop the human
        never drove. Only a self-wake turn (_directive_self_wake, the default is
        False for a human send) may set it.
        """
        state, _client = self._malformed_state(tmp_path)
        slot = _RecordingSlot("chat-1-human-malformed")
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "a human message that trips the parser")
            if slot.task:
                await slot.task
        assert (
            slot._last_turn_structural_terminal is False
        ), "a human malformed turn armed the nudge-loop stop guard"

    @pytest.mark.asyncio
    async def test_a_genuine_new_turn_clears_the_flag(self, tmp_path):
        """A fresh, non-synthetic turn is exactly the event that should re-arm
        the loop, so it must clear a stale structural verdict even before the
        turn's own outcome is known."""
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None

        def _clean_stream(_message, *_a, **_kw):
            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_clean_stream)
        slot = _RecordingSlot("chat-1-clears")
        # A prior malformed turn left the flag set.
        slot._last_turn_structural_terminal = True
        slot._last_turn_structural_terminal_loop_id = "loop-stale"
        slot._last_turn_structural_terminal_loop_gen = 5
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "a fresh human message")
            if slot.task:
                await slot.task
        assert (
            slot._last_turn_structural_terminal is False
        ), "a genuine new turn did not clear the stale structural verdict"
        assert (
            slot._last_turn_structural_terminal_loop_id == ""
        ), "a genuine new turn did not clear the scoped loop id"
        assert (
            slot._last_turn_structural_terminal_loop_gen == 0
        ), "a genuine new turn did not clear the scoped loop generation"


class _RecordingNudgeSvc:
    """Records ``notify_cycle_failed`` calls so a runner-level test can assert
    the real call-site (not the helper) charged or did not charge.

    Patched in as ``kiro_crew.autonudge.get_instance``'s return value, which is
    exactly what ``_note_cycle_failure`` resolves at call time.
    """

    def __init__(self) -> None:
        self.charges: list[tuple[str, str, int]] = []

    async def notify_cycle_failed(
        self, slot_key: str, *, loop_id: str, expected_generation: int
    ) -> None:
        self.charges.append((slot_key, loop_id, expected_generation))

    def __getattr__(self, _name: str):
        # The AcpError arm touches other service accessors in passing; this test
        # only cares about notify_cycle_failed, so answer the rest inertly.
        return MagicMock()


class TestTheRunnerCallSitesChargeTheFailedCycle:
    """The call sites, not the helper, are what these pin.

    The helper's own gate is unit-tested in test_autonudge_cycle_failures.py, but
    nothing exercised its two call sites, ``_on_acp_error_card`` and ``_on_error`` in
    ``chat_turn/turn_failures.py``: deleting either ``_note_cycle_failure`` call, or dropping
    the ``err_meta=_err_meta`` argument the generic arm passes, left the whole suite green.
    These drive ``_run_chat`` end to end into the generic terminal arm so a dispatched death
    DOES charge while a pre-dispatch ``memory_unavailable`` does NOT -- the latter is what
    the ``err_meta`` argument is for, so dropping it reddens the no-charge case.
    """

    @pytest.mark.asyncio
    async def test_a_dispatched_death_charges_but_a_pre_dispatch_fault_does_not(
        self, tmp_path
    ) -> None:
        # A plain dispatched death: no structural / start-failure / pre-dispatch
        # tag, so the generic arm resolves err_meta via _terminal_error_meta
        # (None) and the gate charges. This is the "dispatched timeout does"
        # half of the contrast.
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None

        def _raise_dispatched(_message, *_a, **_kw):
            raise RuntimeError("the model session died mid-turn")

        client.stream = MagicMock(side_effect=_raise_dispatched)
        svc = _RecordingNudgeSvc()
        slot = _RecordingSlot("chat-1-dispatched")
        with (
            patch("kiro_crew.dashboard.chat.sel") as mock_sel,
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
        ):
            mock_sel.return_value = MagicMock()
            await _run_chat(
                state,
                slot,
                "[auto-nudge cycle 7] check the PR",
                _directive_self_wake=True,
                _directive_loop_id="loop-dispatched",
                _directive_loop_gen=5,
            )
            if slot.task:
                await slot.task
        assert svc.charges == [
            ("chat-1-dispatched", "loop-dispatched", 5)
        ], "a self-wake dispatched death did not charge the loop's failed-cycle streak"

        # A pre-dispatch fault: _MemoryUnavailable resolves to err_meta
        # {"code": "memory_unavailable"} in the SAME generic arm, which the gate
        # drops -- the store never opened, so no session was reached and the
        # stand-down (whose notice blames a backend/tool/timeout) must not fire.
        # If the call site stops passing err_meta=_err_meta, the gate can no
        # longer see the code and this death wrongly charges.
        state2, client2 = _l1_state(tmp_path)
        client2.last_infra_error = None

        def _raise_pre_dispatch(_message, *_a, **_kw):
            raise chat_runner._MemoryUnavailable(
                "memory_unavailable: the member's store would not open"
            )

        client2.stream = MagicMock(side_effect=_raise_pre_dispatch)
        svc2 = _RecordingNudgeSvc()
        slot2 = _RecordingSlot("chat-1-predispatch")
        with (
            patch("kiro_crew.dashboard.chat.sel") as mock_sel,
            patch("kiro_crew.autonudge.get_instance", return_value=svc2),
        ):
            mock_sel.return_value = MagicMock()
            await _run_chat(
                state2,
                slot2,
                "[auto-nudge cycle 7] check the PR",
                _directive_self_wake=True,
                _directive_loop_id="loop-predispatch",
                _directive_loop_gen=5,
            )
            if slot2.task:
                await slot2.task
        assert (
            svc2.charges == []
        ), "a pre-dispatch memory_unavailable death wrongly charged the failed-cycle streak"

    @pytest.mark.asyncio
    async def test_a_dispatched_death_in_the_acp_error_arm_charges(self, tmp_path) -> None:
        """The OTHER call site: a terminal, non-transient, non-structural
        ``AcpError`` lands in the dedicated AcpError arm (not the generic
        ``except``) and must charge there too. Removing that arm's
        ``_note_cycle_failure`` call leaves the generic-arm test green, so this
        pins it independently.
        """
        state, client = _l1_state(tmp_path)
        client.last_infra_error = None

        def _raise_terminal_acp(_message, *_a, **_kw):
            # No transient flag (so it is not retried) and no structural_terminal
            # tag (so it is not the malformed path) -- a plain dispatched death
            # that reaches the AcpError arm's failed-cycle report.
            raise AcpError("the backend died after the turn dispatched", transient=False)

        client.stream = MagicMock(side_effect=_raise_terminal_acp)
        svc = _RecordingNudgeSvc()
        slot = _RecordingSlot("chat-1-acp-arm")
        with (
            patch("kiro_crew.dashboard.chat.sel") as mock_sel,
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
        ):
            mock_sel.return_value = MagicMock()
            await _run_chat(
                state,
                slot,
                "[auto-nudge cycle 7] check the PR",
                _directive_self_wake=True,
                _directive_loop_id="loop-acp-arm",
                _directive_loop_gen=6,
            )
            if slot.task:
                await slot.task
        assert svc.charges == [
            ("chat-1-acp-arm", "loop-acp-arm", 6)
        ], "a self-wake dispatched death in the AcpError arm did not charge the streak"

    @pytest.mark.asyncio
    async def test_an_l1_infra_requeue_does_not_charge(self, tmp_path) -> None:
        """The L1 ladder's infra re-queue: a turn whose last tool result was a
        CLASS_CAPACITY infra refusal (``client.last_infra_error``) is re-queued
        as a continuation (``_turn_ending_chain`` in ``turn_ending``) and never reaches a
        terminal arm, so it must not charge -- the cycle has not failed, it is
        being retried. The runner re-queues once and the turn ends clean, so
        neither call site runs.

        This covers the ``last_infra_error`` branch only; the ACP transient-5xx
        same-session retry (a different branch, ~16563) is pinned by
        ``test_an_acp_transient_5xx_same_session_retry_does_not_charge`` below.
        """
        state, client = _l1_state(tmp_path)
        # _l1_state's client already ends the turn on a CLASS_CAPACITY infra
        # refusal (client.last_infra_error), which the L1 arm re-queues once.
        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            if len(prompts) > 1:
                # The re-queued continuation runs clean -- the transient cleared.
                client.last_infra_error = None

            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        lad.default_ladder().forget(lad.L1_TOOL_CALL, "chat-1-transient")
        svc = _RecordingNudgeSvc()
        slot = _RecordingSlot("chat-1-transient")
        try:
            with (
                patch.object(chat_runner, "_recovery_delay", new=AsyncMock()),
                patch("kiro_crew.dashboard.chat.sel") as mock_sel,
                patch("kiro_crew.autonudge.get_instance", return_value=svc),
            ):
                mock_sel.return_value = MagicMock()
                await _run_chat(
                    state,
                    slot,
                    "[auto-nudge cycle 7] check the PR",
                    _directive_self_wake=True,
                    _directive_loop_id="loop-transient",
                    _directive_loop_gen=5,
                )
                if slot.task:
                    await slot.task
        finally:
            lad.default_ladder().forget(lad.L1_TOOL_CALL, "chat-1-transient")
        assert svc.charges == [], (
            "an L1 infra re-queue charged the failed-cycle streak -- a retried "
            "cycle has not failed"
        )

    @pytest.mark.asyncio
    async def test_an_acp_transient_5xx_same_session_retry_does_not_charge(self, tmp_path) -> None:
        """A transient backend 5xx raised as an ``AcpError`` is retried ONCE on
        the SAME live session (no reset) in ``_run_chat``'s ``except AcpError`` chain --
        ``not _turn_emitted and acp_error_is_transient(exc) and
        _transient_5xx_retries < TRANSIENT_RETRIES``. That arm, ``_on_acp_transient_retry``
        in ``chat_turn/acp_recovery.py``, re-queues the message and ends the turn without
        reaching a terminal arm, so the cycle has not failed and must NOT charge.

        This is a DIFFERENT branch from the L1 ``last_infra_error`` re-queue the
        test above covers: a charge added inside this ACP transient arm passed
        every prior test because nothing drove it. ``last_infra_error`` is None
        here so the L1 arm cannot fire; the retry is reached purely through the
        ACP transient path.
        """
        state, client = _l1_state(tmp_path)
        # Silence the L1 arm: with no infra verdict the only live retry path is
        # the ACP transient-5xx branch under test.
        client.last_infra_error = None
        prompts: list[str] = []

        def _stream(message, *_a, **_kw):
            prompts.append(message)
            if len(prompts) == 1:
                # First turn: a transient backend 5xx BEFORE any token streams
                # (not _turn_emitted stays True), classified via the structured
                # AcpError.transient flag that acp_error_is_transient reads.
                raise AcpError("the backend returned a transient 5xx", transient=True)

            # The re-queued continuation runs on the SAME session and completes
            # clean -- the transient cleared.
            async def _gen():
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)
        svc = _RecordingNudgeSvc()
        slot = _RecordingSlot("chat-1-acp-transient")
        with (
            patch.object(chat_runner, "_recovery_delay", new=AsyncMock()),
            patch("kiro_crew.dashboard.chat.sel") as mock_sel,
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
        ):
            mock_sel.return_value = MagicMock()
            await _run_chat(
                state,
                slot,
                "[auto-nudge cycle 7] check the PR",
                _directive_self_wake=True,
                _directive_loop_id="loop-acp-transient",
                _directive_loop_gen=5,
            )
            if slot.task:
                await slot.task
        # The transient arm must have fired (retry counted + re-prompted twice);
        # otherwise the assertion below is vacuous.
        assert len(prompts) == 2, (
            "the ACP transient-5xx arm did not retry on the same session "
            f"(prompted {len(prompts)}x) -- the no-charge assertion would be vacuous"
        )
        assert svc.charges == [], (
            "an ACP transient-5xx same-session retry charged the failed-cycle "
            "streak -- a retried cycle has not failed"
        )
