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


class TestRunnerBackstopContract:
    """The runner-side backstop for futures it consumes itself.

    Exercising the real ``_run_chat`` approval branch needs a full ACP stream
    harness; these lock the contract the backstop depends on so a refactor that
    breaks the guard fails here rather than silently reintroducing the orphan.
    """

    def test_outcome_is_preseeded_before_the_await(self) -> None:
        """`outcome` must be assigned BEFORE the try, not only inside it.

        The `finally` reads `outcome`, and it runs on every exit from the await
        — including `CancelledError`, which slot deletion / cleanup endpoints
        raise by cancelling `slot.task`. If the only assignments were inside
        `try`/`except asyncio.TimeoutError`, that path would raise
        `UnboundLocalError` from the `finally`: the cancellation would be
        replaced by a spurious exception, and everything after the read —
        the message marking AND the pre-existing Slack prompt cleanup — would
        be skipped, reintroducing the orphan card on exactly that path.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        # The pre-seed must appear before the guarded await that follows it.
        # Matched on the call SHAPE, not on the timeout value: the window is
        # configurable (``agent.tool_approval_timeout_secs``), and pinning its
        # literal made an unrelated retune of the window fail this test, which
        # is about assignment ORDER and nothing else.
        preseed = src.index('outcome = "rejected"')
        await_idx = src.index("await asyncio.wait_for(fut, timeout=")
        assert preseed < await_idx, (
            "outcome must be pre-seeded before the approval await so the "
            "finally backstop is total over cancellation"
        )

    def test_a_decision_is_folded_to_the_schema_enum(self) -> None:
        """The ``approval/decided`` closer carries only values the registry lists.

        The approval future can resolve to ``approved_trust_reads`` (a scoped-trust
        approval), which the ``approval/decided`` schema does not list. Emitting the
        raw ``outcome`` there is schema-refused and drops the closer, leaving the
        request open in a file nothing rewrites. So the site folds first -- and it
        folds only where it must: ``rejected_once`` IS in the enum and reaches the
        ledger as itself.

        Checked against the registry rather than against expected text, so adding a
        value at the site without declaring it fails here instead of at write time.
        """
        import inspect
        import re

        from kiro_crew.crew_log.entry_types import SESSION_ENTRY_TYPES
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert (
            "decision=_crew_log_decision" in src
        ), "on_approval_decided must emit the folded value, not the raw outcome"
        assert "decision=outcome" not in src, (
            "raw outcome (e.g. approved_trust_reads) is not in the schema enum "
            "and would be refused, dropping the approval closer"
        )
        assigned = set(re.findall(r'_crew_log_decision = "([a-z_]+)"', src))
        assert assigned, "the fold must assign literal decision values"
        spec = SESSION_ENTRY_TYPES["approval/decided"]
        allowed = set(next(f for f in spec.fields if f.name == "decision").enum)
        assert (
            assigned <= allowed
        ), f"the site emits {sorted(assigned - allowed)}, which the registry refuses"
        assert "rejected_once" in assigned, (
            "rejected_once is in the registry enum and the UI-mark keeps that "
            "outcome distinct, so the ledger must not fold it into rejected"
        )
        assert (
            'outcome in ("approved", "approved_trust_reads")' in src
        ), "the scoped-trust approval must fold to approved"

    def test_a_host_cancelled_prompt_is_not_recorded_as_a_person(self) -> None:
        """A turn cancelled mid-prompt attributes its decision to the host.

        The ledger reads an empty ``by`` as a person's answer. A turn cancelled
        out from under an open prompt sets no deny cause, so without a cancel
        flag the closer would name nobody and read as a human rejection the
        person never made. The cancel handler must flag it and the emit must let
        that flag set ``by=host``.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        # The cancel path flags a host cancellation and re-raises.
        cancel = src.index("except asyncio.CancelledError:")
        flag = src.index("_host_cancelled = True")
        reraise = src.index("raise", flag)
        assert (
            cancel < flag < reraise
        ), "the CancelledError handler must flag _host_cancelled then re-raise"
        # The emit attributes to the host on an auto-decline, a cancel, or a stop.
        assert "_host_deny_cause or _host_cancelled or _host_stopped" in src

    def test_a_stopped_approval_is_not_recorded_as_a_person(self) -> None:
        """A stop's rejection is attributed to the host, and read exactly once.

        A stop resolves the future with an ordinary ``"rejected"`` and raises
        nothing, so neither host flag can see it and the decision would be written
        as a person's answer. The slot carries the ids a stop rejected; this site
        reads one and DISCARDS it, because leaving it would let a later human
        rejection on the same slot inherit the host attribution.
        """
        import inspect as _inspect

        from kiro_crew.dashboard import chat_runner

        src = _inspect.getsource(chat_runner._run_chat)
        read = src.index("_host_stopped = _approval_id in slot._approval_stopped")
        discard = src.index("slot._approval_stopped.discard(_approval_id)")
        emit = src.index("crew_log_emit.on_approval_decided(")
        assert (
            read < discard < emit
        ), "the stop mark must be read and discarded before the decision is emitted"
        # No cause is borrowed for it: that vocabulary renders user-facing text.
        assert "cause=_host_deny_cause," in src

    @pytest.mark.asyncio
    async def test_cancellation_shape_marks_and_reraises(self) -> None:
        """Mirror of the runner's try/finally under cancellation.

        Proves the pre-seeded shape both marks the message and lets the
        CancelledError propagate — the two things the unbound read broke.
        """
        from kiro_crew.dashboard.state import _mark_permission_resolved

        slot = _FakeSlot()
        fut = slot.add_pending_approval("ap-1")

        async def approval_branch() -> None:
            outcome = "rejected"
            try:
                # Value is irrelevant to the shape under test; the real site
                # resolves it from config. Long enough that cancellation, not
                # the deadline, is what ends the await.
                outcome = await asyncio.wait_for(fut, timeout=600.0)
            except asyncio.TimeoutError:
                outcome = "rejected"
            finally:
                slot._approval_futures.pop("ap-1", None)
                approved = outcome in ("approved", "approved_trust_reads")
                if _mark_permission_resolved(
                    slot.messages,
                    "ap-1",
                    "approved" if approved else "rejected",
                    only_if_pending=True,
                ):
                    slot._dirty = True

        task = asyncio.ensure_future(approval_branch())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert slot.resolved_for("ap-1") == "rejected"
        assert slot._dirty is True
        assert "ap-1" not in slot._approval_futures

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
        """The mark-then-flag invariant is convention, so assert it structurally.

        `_flush_dirty_slots` skips slots whose `_dirty` is False, so a call site
        that marks without flagging can lose the write on restart and resurrect
        the card. Guard every current site so a new one is caught in review.
        """
        import re
        from pathlib import Path

        import kiro_crew.dashboard as pkg

        root = Path(pkg.__file__).parent
        for name in ("chat_handlers.py", "chat_runner.py", "state.py"):
            src = (root / name).read_text(encoding="utf-8")
            for match in re.finditer(r"_mark_permission_resolved\(", src):
                # Skip the definition itself.
                if src[: match.start()].rstrip().endswith("def"):
                    continue
                window = src[match.start() : match.start() + 600]
                assert "_dirty = True" in window, (
                    f"{name}: a _mark_permission_resolved call site does not set "
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
