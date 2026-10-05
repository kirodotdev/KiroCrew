"""Regression tests: an approval bar must never outlive its approval future.

The bug: paths that resolved the approval future *in-process* — a stop/interrupt
(``_reject_pending_approvals``) and the chat runner's own 2h timeout / Slack
delivery-failure auto-reject — dropped the future without marking the
``permission`` message resolved. The UI keys its approval bar off that message,
so the bar survived a history reload while the future it pointed at was gone:
every button (Allow once, Trust, Reject) answered ``404 no pending approval``
and the card could not be dismissed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import MagicMock, patch

import pytest
from turn_harness import (
    APPROVED,
    APPROVED_TRUST_READS,
    CANCEL_TURN,
    REJECTED,
    REJECTED_ONCE,
    STOP,
    TurnRecord,
    TurnScript,
    run_turn,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)


class _FakeSlot:
    """Minimal ChatSlot stand-in carrying approval futures + messages."""

    def __init__(self) -> None:
        self.key = "test-slot"
        self.agent = "kirocrew"
        self.messages: list[dict] = []
        self._dirty = False
        self._approval_futures: dict[str, asyncio.Future] = {}
        self._approval_stopped: set[str] = set()

    def add_pending_approval(self, request_id: str) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._approval_futures[request_id] = fut
        self.messages.append(
            {
                "role": "permission",
                "content": "Running: gh pr view 297",
                "cls": json.dumps({"request_id": request_id}),
                "ts": "1",
            }
        )
        return fut

    def resolved_for(self, request_id: str) -> str | None:
        for msg in self.messages:
            if msg.get("role") != "permission":
                continue
            cls = json.loads(msg["cls"])
            if cls.get("request_id") == request_id:
                return cls.get("resolved")
        return None


class TestRejectPendingApprovalsMarksMessage:
    """Stop/interrupt path — chat_handlers._reject_pending_approvals."""

    @pytest.mark.asyncio
    async def test_a_stop_records_which_approvals_it_rejected(self) -> None:
        """The stop's provenance is recorded, because the future cannot carry it.

        A stop resolves the approval with an ordinary ``"rejected"`` and raises
        nothing, so the runner sees the same value a person's Reject produces. The
        ledger reads an unattributed decision as a person's answer, so without this
        mark a stop is written into an append-only file as a human refusal. Marked
        BEFORE the future resolves, since resolving can wake the runner at once.
        """
        from kiro_crew.dashboard.chat_handlers import _reject_pending_approvals

        slot = _FakeSlot()
        slot.add_pending_approval("ap-1")
        slot.add_pending_approval("ap-2")

        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            _reject_pending_approvals(slot)  # type: ignore[arg-type]

        assert slot._approval_stopped == {"ap-1", "ap-2"}
        source = inspect.getsource(_reject_pending_approvals)
        marked = source.index("_approval_stopped.add(aid)")
        resolved = source.index('fut.set_result("rejected")')
        assert marked < resolved, "the mark must precede the resolution that wakes the reader"

    @pytest.mark.asyncio
    async def test_marks_permission_resolved(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _reject_pending_approvals

        slot = _FakeSlot()
        fut = slot.add_pending_approval("ap-1")

        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            _reject_pending_approvals(slot)  # type: ignore[arg-type]

        assert fut.done() and fut.result() == "rejected"
        # Without this the card outlives the future and 404s on every click.
        assert slot.resolved_for("ap-1") == "rejected"
        # The periodic flush skips non-dirty slots, so the mark must set it or
        # the orphan returns after a restart.
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_marks_every_pending_approval(self) -> None:
        from kiro_crew.dashboard.chat_handlers import _reject_pending_approvals

        slot = _FakeSlot()
        slot.add_pending_approval("ap-1")
        slot.add_pending_approval("ap-2")

        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            _reject_pending_approvals(slot)  # type: ignore[arg-type]

        assert slot.resolved_for("ap-1") == "rejected"
        assert slot.resolved_for("ap-2") == "rejected"

    @pytest.mark.asyncio
    async def test_already_resolved_future_untouched(self) -> None:
        """A future the user already answered keeps its recorded decision."""
        from kiro_crew.dashboard.chat_handlers import _reject_pending_approvals
        from kiro_crew.dashboard.state import _mark_permission_resolved

        slot = _FakeSlot()
        fut = slot.add_pending_approval("ap-1")
        fut.set_result("approved")
        _mark_permission_resolved(slot.messages, "ap-1", "trust")
        slot._dirty = False

        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            _reject_pending_approvals(slot)  # type: ignore[arg-type]

        assert slot.resolved_for("ap-1") == "trust"
        assert slot._dirty is False


_PROMPT = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST, request_id="req-1", title="fs_write", tool_kind="edit"
)
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _prompt_turn(*, answers: dict[str, str]) -> TurnScript:
    after = AcpEvent(kind=EVENT_TEXT_CHUNK, text="after the prompt")
    return TurnScript(events=[_PROMPT, after, _DONE], answers=answers)


def _card_resolution(record: TurnRecord) -> str | None:
    """The resolved marker on the req-1 permission card a history reload serves."""
    [card] = [row for row in record.window if row.get("role") == "permission"]
    return json.loads(card["cls"]).get("resolved")


class TestRunnerBackstopContract:
    """The runner-side backstop for futures it consumes itself, through one real turn.

    Every exit from a prompt's wait -- an answer, an expiry, a Stop, the turn
    being cancelled out from under it -- converges on one ``finally`` that marks
    the card resolved, tells the clients, and records who decided. These drive
    the real ``_run_chat`` approval branch (``turn_harness.run_turn``) down each
    exit and read what reached the clients and the crew log.
    """

    @pytest.mark.asyncio
    async def test_a_prompt_cancelled_mid_wait_still_retires_its_card(self) -> None:
        """Slot deletion cancels the turn while the prompt is open.

        The ``finally`` reads the outcome on that path too, so it must have been
        bound before the await: otherwise the cancellation becomes an
        ``UnboundLocalError`` and the card is never retired -- the orphan.
        """
        record = await run_turn(_prompt_turn(answers={"req-1": CANCEL_TURN}))
        assert record.frames("approval_resolved")[-1].payload == {
            "id": "req-1",
            "approved": False,
            "slot": "chat-1",
        }
        assert _card_resolution(record) == "rejected"
        assert record.stop_reason == "failed: CancelledError"

    @pytest.mark.asyncio
    async def test_a_host_cancelled_prompt_is_not_recorded_as_a_person(self) -> None:
        """A turn cancelled mid-prompt attributes its decision to the host.

        The ledger reads an empty ``by`` as a person's answer, and a cancelled
        prompt sets no deny cause. The cancellation must also still propagate:
        swallowed, the turn would reject the tool on the wire and keep streaming.
        """
        record = await run_turn(_prompt_turn(answers={"req-1": CANCEL_TURN}))
        [decision] = record.approval_decisions
        assert decision["decision"] == "rejected"
        assert decision["by"] == "host"
        assert not decision["cause"]
        assert record.calls("reject_tool") == []
        assert not any("after the prompt" in row["content"] for row in record.history_rows)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "recorded"),
        [
            (APPROVED, "approved"),
            (APPROVED_TRUST_READS, "approved"),
            (REJECTED_ONCE, "rejected_once"),
            (REJECTED, "rejected"),
        ],
    )
    async def test_a_decision_is_folded_to_the_schema_enum(
        self, answer: str, recorded: str
    ) -> None:
        """The ``approval/decided`` closer carries only values the registry lists.

        The future can resolve to ``approved_trust_reads``, which the schema does
        not list: emitted raw, the closer is refused and the request stays open
        in a file nothing rewrites. ``rejected_once`` IS listed and keeps its own
        value. Checked against the registry as well as the expected value.
        """
        from kiro_crew.crew_log.entry_types import SESSION_ENTRY_TYPES

        record = await run_turn(_prompt_turn(answers={"req-1": answer}))
        [decision] = record.approval_decisions
        assert decision["decision"] == recorded
        spec = SESSION_ENTRY_TYPES["approval/decided"]
        allowed = set(next(f for f in spec.fields if f.name == "decision").enum)
        assert decision["decision"] in allowed

    @pytest.mark.asyncio
    async def test_a_stopped_approval_is_not_recorded_as_a_person(self) -> None:
        """A stop's rejection is attributed to the host, and read exactly once.

        A stop resolves the future with a plain ``"rejected"``, so only the mark
        the stop leaves on the slot tells the host's decision apart from a
        person's. The mark is consumed by the decision it names: a later human
        rejection of the same request id must not inherit it. And no deny cause
        is borrowed for a stop -- that vocabulary renders user-facing text.
        """
        again = AcpEvent(kind=EVENT_TEXT_CHUNK, text="between the two prompts")
        record = await run_turn(
            TurnScript(
                events=[_PROMPT, again, _PROMPT, _DONE],
                answers={"req-1": [STOP, REJECTED]},
            )
        )
        stopped, refused = record.approval_decisions
        assert (stopped["decision"], stopped["by"], stopped["cause"]) == ("rejected", "host", "")
        assert (refused["decision"], refused["by"], refused["cause"]) == ("rejected", "", "")
        assert [call.args for call in record.calls("reject_tool")] == [("req-1",), ("req-1",)]

    def test_timeout_marks_rejected_when_pending(self) -> None:
        from kiro_crew.dashboard.state import _mark_permission_resolved

        slot = _FakeSlot()
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: gh pr view 297",
                "cls": json.dumps({"request_id": "ap-1"}),
                "ts": "1",
            }
        )
        wrote = _mark_permission_resolved(slot.messages, "ap-1", "rejected", only_if_pending=True)
        assert wrote is True
        assert slot.resolved_for("ap-1") == "rejected"

    def test_backstop_does_not_clobber_http_decision(self) -> None:
        """HTTP slot-approve already recorded "yolo"/"trust" — keep it."""
        from kiro_crew.dashboard.state import _mark_permission_resolved

        slot = _FakeSlot()
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: gh pr view 297",
                "cls": json.dumps({"request_id": "ap-1", "resolved": "yolo"}),
                "ts": "1",
            }
        )
        wrote = _mark_permission_resolved(slot.messages, "ap-1", "approved", only_if_pending=True)
        assert wrote is False
        assert slot.resolved_for("ap-1") == "yolo"


class TestResolveApprovalFlushes:
    """state.resolve_approval must flag the slot dirty when it marks."""

    def test_every_call_site_flags_dirty(self) -> None:
        """Kept as a source pin: the mark-then-flag invariant is a convention at
        every call site, including ones no scripted turn reaches.

        `_flush_dirty_slots` skips slots whose `_dirty` is False, so a call site
        that marks without flagging can lose the write on restart and resurrect
        the card. Guard every current site so a new one is caught in review; the
        ``chat_turn`` owners the runner composes are scanned too, so a call that
        moves into one keeps the guard. Counted in ``test_source_pin_budget.py``.
        """
        import re
        from pathlib import Path

        import kiro_crew.dashboard as pkg

        root = Path(pkg.__file__).parent
        sources = [root / name for name in ("chat_handlers.py", "chat_runner.py", "state.py")]
        sources += sorted((root / "chat_turn").glob("*.py"))
        for path in sources:
            src = path.read_text(encoding="utf-8")
            for match in re.finditer(r"_mark_permission_resolved\(", src):
                # Skip the definition itself.
                if src[: match.start()].rstrip().endswith("def"):
                    continue
                window = src[match.start() : match.start() + 600]
                assert "_dirty = True" in window, (
                    f"{path.name}: a _mark_permission_resolved call site does not set "
                    "_dirty — the periodic flush will skip it"
                )

    @pytest.mark.asyncio
    async def test_marks_slot_dirty(self) -> None:
        from kiro_crew.dashboard.state import DashboardState

        slot = _FakeSlot()
        fut = slot.add_pending_approval("ap-1")

        state = MagicMock(spec=DashboardState)
        state._slots = {"test-slot": slot}
        state._approval_futures = {}
        state.resolve_state_approval = MagicMock(return_value=False)
        state._audit_and_broadcast_approval = MagicMock()
        state.push_slots_update = MagicMock()

        assert DashboardState.resolve_approval(state, "ap-1", True) is True
        assert fut.result() == "approved"
        assert slot.resolved_for("ap-1") == "approved"
        assert slot._dirty is True
