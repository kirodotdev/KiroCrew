"""What each error arm of a dashboard turn leaves behind, one table row per arm.

``chat_runner._run_chat`` routes a failed turn through ordered ``except`` arms
(sign-in required, process death, prompt-busy exhaustion, the ``AcpError`` ladder
with its busy, pipe, lost-session and transient rungs, a closing session manager,
an unloaded app agent, and the generic arm). Each arm decides the card the slot
shows, whether the turn is requeued (and verbatim or as a continuation), whether
the session is reset, whether the circuit breaker is charged, which retry budget
is spent and what label the crew-log closer carries.

Every row drives a real turn through ``turn_harness.run_turn`` with its budgets
seeded on the slot in ``setup``, and asserts the persisted rows, the recorded
hand-off, the session calls and every retry budget on the slot afterwards, so an
arm that spends a budget it should not reddens its row. Nothing reads the
runner's source.

The sign-in, prompt-busy and ``AcpError`` arms close the turn with an empty error
label: they do not name what they caught. Those rows pin the empty label.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from turn_harness import Do, Raise, SlotSpec, Step, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew import runtime_death
from kiro_crew.acp.transport_errors import AcpAuthRequired, AcpError, AcpProcessDied
from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config.sections import ResolvedBindings
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import (
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_RETRY_TEXT,
)
from kiro_crew.dashboard.chat_utils import (
    AUTH_REQUIRED_KIND,
    RETRY_INVITED_KIND,
    TRANSIENT_NOTICE_META_KEY,
    TRANSIENT_NOTICE_RESUMING,
    TRANSIENT_NOTICE_RETRYING,
    TRANSIENT_RETRY_KIND,
)
from kiro_crew.dashboard.recovery_replays import ReplayFamily
from kiro_crew.dashboard.restart_resume import RETRYABLE_ERROR_KINDS
from kiro_crew.llm_helpers import TRANSIENT_RETRIES, PromptBusyExhaustedError
from kiro_crew.recovery.ladder import SESSION_RECOVERY_MAX_ATTEMPTS
from kiro_crew.session_allocation import SessionClosingError

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_SLOT_KEY = "chat-1"
#: The prompt-busy arms re-queue a top-level turn up to three times; the budget
#: is the arm's own literal, with no constant to read it from.
_PROMPT_BUSY_BUDGET = 3
#: Every retry budget and one-shot an error arm can spend, as a fresh slot holds
#: them. Each row's ``after`` names only what differs; the rest must stay here.
_UNSPENT: Mapping[str, Any] = {
    "_acp_pipe_death_retries": 0,
    "_prompt_busy_retries": 0,
    "_transient_5xx_retries": 0,
    "_infra_retries": 0,
    "_prestream_exhausted_cycles": 0,
    "_session_not_found_retry_used": False,
    "_posttoken_retry_used": False,
    "_poisoned_reset_used": False,
    "_model_access_fallback_used": False,
    "_last_turn_auth_required": False,
}


@pytest.fixture
def fresh_runtime_deaths() -> Iterator[None]:
    """No process death recorded and no shared-death streak, before and after."""
    runtime_death._reset_for_tests()
    yield
    runtime_death._reset_for_tests()


class _SignInLatch:
    """The prerequisite service the auth arm latches signed-out."""

    def __init__(self) -> None:
        self.signed_out = 0

    def mark_signed_out(self) -> None:
        self.signed_out += 1


def _shared_runtime_died(ctx: TurnContext) -> None:
    # The process carried another tenant when it died, so the death is nobody's
    # single fault; recorded the way the runtime records it where it detects it.
    runtime_death.announce(ctx.provider, reason="exit", expected=False, leases=2, acp_sessions=2)


#: The closer label of an arm that does not name what it caught.
_UNNAMED: tuple[str, ...] = ("",)


@dataclasses.dataclass(frozen=True)
class _Arm:
    #: The provider stream, built fresh for every run so no exception outlives it.
    events: Callable[[], Sequence[Step]] = lambda: ()
    seed: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    shared_streak: int = 0
    run_kwargs: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    allocation_error: Callable[[], BaseException] | None = None
    #: ``(role, meta.kind)`` of every persisted row after the user's.
    rows: Sequence[tuple[str, str | None]] = ()
    #: Text the last error row carries: the exception's own words, or a verdict
    #: whose wording is the only difference a user sees between two arms.
    card: str = ""
    #: A card the arm shows verbatim from a named constant.
    card_text: str = ""
    notice: str = ""
    #: ``None``: no hand-off; ``True``: the message requeued verbatim; ``False``: a
    #: continuation requeued in its place.
    requeued: bool | None = None
    reset: bool = False
    breaker_charged: bool = False
    #: The crew-log closer's accepted error labels; ``None`` when it never opened.
    label: tuple[str, ...] | None = ()
    #: Budgets that differ from ``_UNSPENT`` after the turn.
    after: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: Whether restart-resume's error pass retries the turn from this arm's LAST
    #: persisted row with nobody present: that row is an ``error`` whose
    #: ``meta.kind`` is in ``restart_resume.RETRYABLE_ERROR_KINDS``. The pass fails
    #: closed on every other kind, so a writer that forgets the tag is silently
    #: never resumed; every arm states its answer here and the pin test holds the
    #: two together. ``None`` is "not chosen yet" and fails that test.
    auto_resumed: bool | None = None


_RETRY = ("error", TRANSIENT_RETRY_KIND)
#: The ladder's terminal "please retry" row: tagged so the restart-resume error
#: pass can retry it without reading its words.
_INVITED = ("error", RETRY_INVITED_KIND)
_PLAIN = ("error", None)

_ARMS: dict[str, _Arm] = {
    "auth-required": _Arm(
        events=lambda: [Raise(AcpAuthRequired("Not signed in"))],
        rows=[_PLAIN],
        card="Not signed in",
        reset=True,
        label=_UNNAMED,
        after={"_last_turn_auth_required": True},
        auto_resumed=False,
    ),
    "auth-required-host-sign-in": _Arm(
        events=lambda: [Raise(AcpAuthRequired("Not signed in", backend=ACP_BACKEND_KAS))],
        rows=[("error", AUTH_REQUIRED_KIND)],
        card="Not signed in",
        reset=True,
        label=_UNNAMED,
        after={"_last_turn_auth_required": True},
        auto_resumed=False,
    ),
    "process-died-own-fault": _Arm(
        events=lambda: [Raise(AcpProcessDied("process exited"))],
        rows=[_RETRY],
        requeued=True,
        reset=True,
        label=("AcpProcessDied",),
        after={"_acp_pipe_death_retries": 1},
        auto_resumed=False,
    ),
    "process-died-after-text": _Arm(
        events=lambda: [_REPLY, Raise(AcpProcessDied("process exited"))],
        rows=[("assistant", None), _RETRY],
        requeued=False,
        reset=True,
        label=("AcpProcessDied",),
        after={"_acp_pipe_death_retries": 1},
        auto_resumed=False,
    ),
    "process-died-own-fault-exhausted": _Arm(
        events=lambda: [Raise(AcpProcessDied("process exited"))],
        seed={"_acp_pipe_death_retries": SESSION_RECOVERY_MAX_ATTEMPTS},
        rows=[_PLAIN],
        # Its own runtime keeps dying: a fresh chat is the advice.
        card="Session stuck",
        reset=True,
        label=("AcpProcessDied",),
        after={"_acp_pipe_death_retries": SESSION_RECOVERY_MAX_ATTEMPTS + 1},
        auto_resumed=False,
    ),
    "process-died-shared-runtime": _Arm(
        events=lambda: [Do(_shared_runtime_died), Raise(AcpProcessDied("process exited"))],
        rows=[_RETRY],
        requeued=True,
        reset=True,
        label=("AcpProcessDied",),
        auto_resumed=False,
    ),
    "process-died-shared-runtime-exhausted": _Arm(
        events=lambda: [Do(_shared_runtime_died), Raise(AcpProcessDied("process exited"))],
        shared_streak=SESSION_RECOVERY_MAX_ATTEMPTS,
        rows=[_INVITED],
        # The process it shares keeps dying: retrying the healthy chat is the advice.
        card="shares kept restarting",
        reset=True,
        label=("AcpProcessDied",),
        auto_resumed=True,
    ),
    "prompt-busy-exhausted-outer": _Arm(
        events=lambda: [Raise(PromptBusyExhaustedError())],
        rows=[_RETRY],
        requeued=True,
        reset=True,
        label=_UNNAMED,
        after={"_prompt_busy_retries": 1},
        auto_resumed=False,
    ),
    "prompt-busy-exhausted-nested": _Arm(
        events=lambda: [Raise(PromptBusyExhaustedError())],
        run_kwargs={"_prompt_depth": 1},
        rows=[_INVITED],
        card="Session busy — please retry.",
        reset=True,
        label=_UNNAMED,
        after={"_prompt_busy_retries": 1},
        auto_resumed=True,
    ),
    "acp-busy-under-budget": _Arm(
        events=lambda: [Raise(AcpError("prompt already in progress"))],
        rows=[_RETRY],
        requeued=True,
        reset=True,
        label=_UNNAMED,
        after={"_prompt_busy_retries": 1},
        auto_resumed=False,
    ),
    "acp-busy-exhausted": _Arm(
        events=lambda: [Raise(AcpError("prompt already in progress"))],
        seed={"_prompt_busy_retries": _PROMPT_BUSY_BUDGET},
        rows=[_PLAIN],
        reset=True,
        label=_UNNAMED,
        after={"_prompt_busy_retries": _PROMPT_BUSY_BUDGET + 1},
        auto_resumed=False,
    ),
    "acp-pipe-death": _Arm(
        events=lambda: [Raise(AcpError("process exited"))],
        rows=[_RETRY],
        requeued=True,
        reset=True,
        label=_UNNAMED,
        after={"_acp_pipe_death_retries": 1},
        auto_resumed=False,
    ),
    "session-not-found-once": _Arm(
        events=lambda: [Raise(AcpError("Session not found"))],
        rows=[_RETRY],
        card_text=SESSION_NOT_FOUND_RETRY_TEXT,
        requeued=True,
        reset=True,
        label=_UNNAMED,
        after={"_session_not_found_retry_used": True},
        auto_resumed=False,
    ),
    "session-not-found-again": _Arm(
        events=lambda: [Raise(AcpError("Session not found"))],
        seed={"_session_not_found_retry_used": True},
        rows=[_PLAIN],
        card_text=SESSION_NOT_FOUND_GIVE_UP_TEXT,
        reset=True,
        label=_UNNAMED,
        after={"_session_not_found_retry_used": True},
        auto_resumed=False,
    ),
    "pre-token-transient-under-budget": _Arm(
        events=lambda: [Raise(AcpError("Internal server error", transient=True))],
        rows=[_RETRY],
        notice=TRANSIENT_NOTICE_RETRYING,
        requeued=True,
        label=_UNNAMED,
        after={"_transient_5xx_retries": 1},
        auto_resumed=False,
    ),
    "pre-token-transient-exhausted": _Arm(
        events=lambda: [Raise(AcpError("Internal server error", transient=True))],
        seed={"_transient_5xx_retries": TRANSIENT_RETRIES},
        rows=[_PLAIN],
        card="Internal server error",
        label=_UNNAMED,
        after={"_prestream_exhausted_cycles": 1},
        auto_resumed=False,
    ),
    "post-token-transient": _Arm(
        events=lambda: [_REPLY, Raise(AcpError("Internal server error", transient=True))],
        rows=[("assistant", None), _RETRY],
        notice=TRANSIENT_NOTICE_RESUMING,
        requeued=False,
        label=_UNNAMED,
        after={"_posttoken_retry_used": True},
        auto_resumed=False,
    ),
    "terminal-acp-error": _Arm(
        events=lambda: [Raise(AcpError("bad request", transient=False))],
        rows=[_PLAIN],
        card="bad request",
        label=_UNNAMED,
        auto_resumed=False,
    ),
    "session-closing-at-allocation": _Arm(
        allocation_error=lambda: SessionClosingError("closing"),
        label=None,
        auto_resumed=False,
    ),
    "session-closing-mid-stream": _Arm(
        events=lambda: [_REPLY, Raise(SessionClosingError("closing"))],
        label=("SessionClosingError",),
        auto_resumed=False,
    ),
    "generic-exception": _Arm(
        events=lambda: [Raise(RuntimeError("boom"))],
        rows=[_PLAIN],
        card="boom",
        breaker_charged=True,
        label=("RuntimeError",),
        auto_resumed=False,
    ),
}


async def _run_arm(arm: _Arm, latch: _SignInLatch) -> tuple[TurnRecord, Any]:
    seen: dict[str, Any] = {}

    def _setup(ctx: TurnContext) -> None:
        seen["slot"] = ctx.slot
        ctx.state.kiro_prerequisite_service = latch
        for name, value in arm.seed.items():
            setattr(ctx.slot, name, value)
        for _ in range(arm.shared_streak):
            runtime_death.note_shared_death(ctx.slot.key)

    record = await run_turn(
        TurnScript(
            events=arm.events(),
            setup=_setup,
            run_kwargs=arm.run_kwargs,
            allocation_error=arm.allocation_error() if arm.allocation_error else None,
        )
    )
    return record, seen["slot"]


def _sessions(record: TurnRecord) -> list[str]:
    return [call.name for call in record.session_calls]


class TestEachErrorArm:
    @pytest.mark.parametrize("name", list(_ARMS))
    def test_turn_error_arm_declares_whether_the_error_pass_resumes_it(self, name: str) -> None:
        """Each arm says whether restart-resume's error pass retries its last row.

        The pass retries only an ``error`` row whose ``meta.kind`` is in
        ``RETRYABLE_ERROR_KINDS`` and fails closed on every other kind, so a new
        "please retry" writer that forgets the tag would be silently never resumed.
        The table states the answer beside each arm's rows and this holds the two
        together: a new arm has to choose, and a kind added to or dropped from the
        set shows up here as the arms it moves.
        """
        arm = _ARMS[name]
        assert arm.auto_resumed is not None, f"{name}: choose auto_resumed"
        last = arm.rows[-1] if arm.rows else None
        retried = last is not None and last[0] == "error" and last[1] in RETRYABLE_ERROR_KINDS
        assert retried is arm.auto_resumed, (name, last, sorted(RETRYABLE_ERROR_KINDS))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(_ARMS))
    async def test_turn_error_arm_card_hand_off_reset_breaker_and_label(
        self, name: str, fresh_runtime_deaths: None
    ) -> None:
        arm = _ARMS[name]
        latch = _SignInLatch()
        record, slot = await _run_arm(arm, latch)

        # Every turn leaves the session Stop count it read at its entry on the slot:
        # the restart-resume error pass measures the count after the teardown
        # against it (GPT review, rev 65). The harness's manager answers 0.
        assert slot._turn_entry_stop_count == 0
        rows = [
            (row["role"], (row.get("meta") or {}).get("kind")) for row in record.history_rows[1:]
        ]
        assert rows == list(arm.rows)
        if arm.card:
            assert arm.card in record.rows("error")[-1]["content"]
        if arm.card_text:
            assert record.rows("error")[-1]["content"] == arm.card_text
        if arm.notice:
            assert record.rows("error")[-1]["meta"][TRANSIENT_NOTICE_META_KEY] == arm.notice
        if arm.requeued is None:
            assert record.successors == ()
        else:
            [successor] = record.successors
            assert successor.kwargs["_synthetic_recovery_turn"] is True
            assert (successor.args[0] == "hello") is arm.requeued
            assert successor.kwargs["_synthetic_payload"] is (not arm.requeued)
        assert ("reset" in _sessions(record)) is arm.reset
        assert ("record_failure" in _sessions(record)) is arm.breaker_charged
        if arm.label is None:
            assert record.crew_log == ()
        else:
            [failed] = record.crew("on_turn_failed")
            assert failed.kwargs["error"] in arm.label
        budgets = {field: getattr(slot, field) for field in _UNSPENT}
        assert budgets == {**_UNSPENT, **arm.after}
        # Only the sign-in arm latches the prerequisite service signed-out.
        assert latch.signed_out == int(name.startswith("auth-required"))


class TestTheTeardownHold:
    """GPT review, rev 72: the runner holds the chat's key armed from before it
    releases the failed turn's session until the error pass has decided, so a
    channel Stop landing in the reset's teardown awaits is recorded and the pass
    arms nothing (``restart_resume.stops_recordable_through_teardown``). Pinned
    on the session manager's call order: the hold's arm precedes the reset, and
    its disarm follows the pass's own count read at the cycle end."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["process-died-shared-runtime-exhausted", "prompt-busy-exhausted-outer"]
    )
    async def test_the_key_is_held_from_before_the_reset_until_the_pass_has_read(
        self, name: str, fresh_runtime_deaths: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import restart_resume

        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: True)
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (False, 5, 3600))
        arm = _ARMS[name]
        assert arm.reset  # a turn whose session the teardown pops

        record, _ = await _run_arm(arm, _SignInLatch())

        names = _sessions(record)
        assert "arm_resume" in names and "reset" in names
        assert names.index("arm_resume") < names.index("reset")
        last_read = len(names) - 1 - names[::-1].index("stop_generation")
        assert names.index("disarm_resume") > last_read
        # Balanced by the end of the record: the hold is counted, the pass's own arm
        # (when it arms) releases with its task.
        assert names.count("arm_resume") == names.count("disarm_resume")

    @pytest.mark.asyncio
    async def test_nothing_is_held_while_the_switch_is_off(
        self, fresh_runtime_deaths: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import restart_resume

        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: False)

        record, _ = await _run_arm(_ARMS["prompt-busy-exhausted-outer"], _SignInLatch())

        names = _sessions(record)
        assert "reset" in names
        assert "arm_resume" not in names and "disarm_resume" not in names


class TestArmSpecifics:
    @pytest.mark.asyncio
    async def test_turn_error_arm_shared_death_charges_the_shared_streak(
        self, fresh_runtime_deaths: None
    ) -> None:
        record, slot = await _run_arm(_ARMS["process-died-shared-runtime"], _SignInLatch())

        assert runtime_death.shared_deaths(_SLOT_KEY) == 1
        assert slot._acp_pipe_death_retries == 0
        assert len(record.successors) == 1

    @pytest.mark.asyncio
    async def test_turn_error_arm_lost_session_replays_under_its_own_family(
        self, fresh_runtime_deaths: None
    ) -> None:
        record, _ = await _run_arm(_ARMS["session-not-found-once"], _SignInLatch())

        [successor] = record.successors
        assert ReplayFamily.SESSION_NOT_FOUND in successor.kwargs["_replay"]

    @pytest.mark.asyncio
    async def test_turn_error_arm_pre_token_transient_backs_off_before_requeueing(
        self, fresh_runtime_deaths: None
    ) -> None:
        record, _ = await _run_arm(_ARMS["pre-token-transient-under-budget"], _SignInLatch())

        # The backoff is a timer on the turn's loop, so it costs virtual time only.
        [successor] = record.successors
        assert successor.at > 0
        assert record.calls("stream") and record.calls("stream")[0].at == 0.0

    @pytest.mark.asyncio
    async def test_turn_error_arm_closing_session_mid_stream_saves_no_partial_reply(
        self, fresh_runtime_deaths: None
    ) -> None:
        record, _ = await _run_arm(_ARMS["session-closing-mid-stream"], _SignInLatch())

        # A quiet shutdown abort: no card, no saved reply, no crew-log message.
        assert [row["role"] for row in record.history_rows] == ["user"]
        assert record.crew("on_message_sent") == []
        assert record.stop_reason == "failed: SessionClosingError"
        assert [frame.kind for frame in record.ws_frames].count("chat_done") == 1

    @pytest.mark.asyncio
    async def test_turn_error_arm_unloaded_app_agent_fails_loud_without_a_charge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cold = ResolvedBindings(
            workspace_dir=tmp_path / "workspace",
            kiro_agent="kirocrew",
            resolved_alias="default",
            memory_store_name="default",
            effective_memory_config={},
            model="",
            requested_resolved=False,
            selection_kind="member",
        )
        monkeypatch.setattr(chat_runner, "resolve_agent_bindings", lambda *_a, **_k: cold)

        def _app_agent(ctx: TurnContext) -> None:
            ctx.slot.agent = "my-app-agent"

        record = await run_turn(
            TurnScript(events=[_REPLY], setup=_app_agent), slot=SlotSpec(app="myapp")
        )

        # The card names the agent that is missing...
        [card] = [row for row in record.window if row["role"] == "error"]
        assert "my-app-agent" in card["content"]
        # ...the default agent is never dispatched in its place...
        assert record.allocations == []
        assert record.calls("stream") == []
        # ...and, unlike the generic arm, nothing is charged or requeued.
        assert "record_failure" not in _sessions(record)
        assert record.successors == ()
        assert record.crew_log == ()
        assert [frame.kind for frame in record.ws_frames].count("chat_done") == 1
