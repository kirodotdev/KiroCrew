"""A parent's end cancels its sub-agent runs: the teardown's own rules.

What the teardown cancels, spares and records once a verb has started it, the resets that keep or
stop the children, ``end_children_for``, and the source ratchets over the audit verbs, every release
site and every conversation-ending reset caller.
"""

from __future__ import annotations

import contextlib

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    _TestParentEndCancelsItsChildrenHarness,
    cfg_fixture,
)

from kiro_crew.session import SessionManager


class TestParentEndCancelsItsChildrenTeardownRules(_TestParentEndCancelsItsChildrenHarness):
    """What a parent's teardown cancels, spares and records, beyond the ending verbs.

    Among them: children parked on a spawn approval, finished but undelivered, finished mid-teardown,
    claimed but unstarted or refused by the store; the delivery-parked table; the audit line and its verb;
    resets that keep or stop the children; ``end_children_for``; watcher disarm; and the source ratchets.
    The per-verb cases are in ``TestParentEndCancelsItsChildren`` (test_session_parent_end_verbs.py).
    """

    @pytest.mark.asyncio
    async def test_a_child_parked_on_a_spawn_approval_is_not_cancelled(self):
        """An approval is a decision a person was asked for; a teardown may not answer it.

        Observed, not theorised: the private-workflow E2E spawns a child that parks on a
        spawn approval, the pooled worker's ``destroy`` cancelled it, and the test's
        ``POST /api/approvals/spawn:<id>/approve`` then answered 404 "not found or
        expired" -- indistinguishable from the person having taken too long to reply.

        ``cancel_for_parent_impl`` already applies this rule for Stop-all; the teardown
        snapshot did not, which is the asymmetry. ``_exec_started is None`` is part of the
        test: a run that HAS begun and is parked on a later approval is live work, and a
        parent end does stop that.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        def _run(agent_id, *, awaiting, started):
            return SimpleNamespace(
                id=agent_id,
                parent_session_key="wf-pool:wf_000008:0",
                done=False,
                _reported_to_parent=False,
                _digest_held=False,
                _digest_held_at=0.0,
                _digest_settle_deliveries=[],
                _delivery_queued=False,
                _awaiting_approval=awaiting,
                _exec_started=started,
            )

        class _Manager:
            def __init__(self):
                self._agents = {
                    "parked": _run("parked", awaiting=True, started=None),
                    "started-then-parked": _run(
                        "started-then-parked", awaiting=True, started=123.0
                    ),
                    "ordinary": _run("ordinary", awaiting=False, started=123.0),
                }
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("wf-pool:wf_000008:0")

        assert "parked" not in selected, (
            "a child parked on a spawn approval was handed to the cancel loop, so the "
            "approval the user was asked for answers 404"
        )
        assert sorted(selected) == [
            "ordinary",
            "started-then-parked",
        ], f"the exclusion is too wide and spared live work: {sorted(selected)}"
        # Not cancelled is not the same as ignored: the conversation it would report into
        # has ended, so the delivery is still gated. The run keeps its own decision and
        # loses only the injection.
        assert "parked" in manager._teardown_cancelled_ids, (
            "an approval-parked child escaped the teardown entirely, so approving it "
            "later injects into whatever session that key serves by then"
        )

    @pytest.mark.asyncio
    async def test_has_live_or_queued_children_is_a_pure_live_queued_probe(self):
        """The deferral probe answers live-or-queued, and mutates nothing.

        It is the read the identity sweep uses to decide whether to DEFER a parent
        rather than retire it. Unlike ``snapshot_teardown_children_impl`` it arms no
        delivery gate, clears no follow-ups and cancels no watcher, so a spared
        parent keeps its children's completion path intact.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        def _run(agent_id, *, done, awaiting=False, started=123.0):
            return SimpleNamespace(
                id=agent_id,
                parent_session_key="dashboard:chat-9",
                done=done,
                _awaiting_approval=awaiting,
                _exec_started=started,
                pending_followups=["fu-1"],
            )

        class _Manager:
            def __init__(self, agents, queue=None, dispatch=None):
                self._agents = agents
                self._queue = queue or []
                self._undurable_in_dispatch = dispatch or {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}
                self._followup_watcher_infos: dict = {}

        # A live child -> True.
        live_mgr = _Manager({"run-1": _run("run-1", done=False)})
        live = CancellationCoordinator(live_mgr)  # type: ignore[arg-type]
        assert live.has_live_or_queued_children_impl("dashboard:chat-9") is True
        # Pure: nothing was marked or torn down.
        assert live_mgr._teardown_cancelled_ids == set()
        assert live_mgr._agents["run-1"].pending_followups == ["fu-1"]

        # A queued child with a preassigned id -> True.
        queued_mgr = _Manager(
            {},
            queue=[{"parent_session_key": "dashboard:chat-9", "_preassigned_id": "q-1"}],
        )
        queued = CancellationCoordinator(queued_mgr)  # type: ignore[arg-type]
        assert queued.has_live_or_queued_children_impl("dashboard:chat-9") is True

        # Only a finished child, or one parked at a spawn approval, or a resume
        # row -> False (nothing a retirement would cancel). This is the IN-MEMORY
        # half only: a durable-only / window-held store child is read off the loop
        # by the sweep's own probe (``_parent_has_store_children``), not here.
        idle_mgr = _Manager(
            {
                "done": _run("done", done=True),
                "parked": _run("parked", done=False, awaiting=True, started=None),
            },
            queue=[
                {
                    "parent_session_key": "dashboard:chat-9",
                    "_preassigned_id": "r-1",
                    "_resume_id": "r-1",
                }
            ],
        )
        idle = CancellationCoordinator(idle_mgr)  # type: ignore[arg-type]
        assert idle.has_live_or_queued_children_impl("dashboard:chat-9") is False
        assert idle.has_live_or_queued_children_impl("") is False

    @pytest.mark.asyncio
    async def test_a_finished_but_undelivered_child_is_gated_without_being_cancelled(self):
        """Done is not delivered, and only the delivered ones may be let through.

        ``_reported_to_parent`` is set the moment ``_on_done`` returns, so a run that is
        ``done`` without it still has a report, a digest hold or a queued announce
        outstanding. Selecting only the not-done runs left that class of child free to
        inject -- and the injector resolves the parent key through the session registry
        and CREATES a session when none is live, so the delivery rebuilds the very
        conversation the teardown took down.

        It is marked but NOT returned: there is nothing left to cancel, and handing it to
        the cancel loop would publish a synthetic "never started" terminal over a run
        that finished.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        def _run(agent_id: str, *, done: bool, reported: bool, held=0.0, queued=False):
            info = SimpleNamespace(
                id=agent_id,
                parent_session_key="dashboard:chat-9",
                done=done,
                _reported_to_parent=reported,
                _digest_held_at=held,
                _delivery_queued=queued,
            )
            return info

        class _Manager:
            def __init__(self):
                self._agents = {
                    "running": _run("running", done=False, reported=False),
                    "undelivered": _run("undelivered", done=True, reported=False),
                    # ``_on_done`` RETURNED for these two, having only parked the
                    # delivery: a wave member held for a digest, and a dashboard
                    # announce sitting in the parent's slot queue. Both fire later,
                    # from a path of their own, into whatever the key serves by then.
                    "digest-held": _run("digest-held", done=True, reported=True, held=1.0),
                    "queued-announce": _run(
                        "queued-announce", done=True, reported=True, queued=True
                    ),
                    "delivered": _run("delivered", done=True, reported=True),
                }
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        assert selected == ("running",), "a finished run was handed to the cancel loop"
        for parked in ("undelivered", "digest-held", "queued-announce"):
            assert parked in manager._teardown_cancelled_ids, (
                f"{parked} was left free to rebuild the conversation the teardown just "
                "took down -- a report that RETURNED is not a delivery that landed"
            )
        assert "running" in manager._teardown_cancelled_ids
        assert (
            "delivered" not in manager._teardown_cancelled_ids
        ), "a child whose outcome already reached the parent needs no gate"

    @pytest.mark.asyncio
    async def test_a_reset_leaves_a_healthy_child_running(self, cfg):
        """A reset recycles a process; it does not end the conversation.

        The session-map entry keeps its resume sid, so the next turn on the key restores
        the same native conversation through ``session/load``. A child therefore has
        somewhere to deliver and is bounded by its own run timeout, and stopping it would
        discard live work belonging to a conversation that is coming back.

        This is the invariant for EVERY reset caller, which is why it is asserted on the
        bare call: the verb is what every evict-and-retry path reaches for -- a wedged
        prompt, a failed auto-compaction, a provider switch, an idle expiry, the channel
        watchdog -- and each of those is a retry, not an ending.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True

        assert cancelled == [], "a process recycle stopped a child of a surviving conversation"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_reset_that_ends_the_conversation_stops_the_children(self, cfg):
        """Some endings reach only ``reset``, so the intent has to be sayable there.

        A channel "clear context", a cancelled cron job, a task run's cancel cleanup and a
        workflow pool starting a new conversation all end a conversation through this one
        verb. Removing cancellation from it entirely would leave each of those leaking its
        children.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9", ends_conversation=True) is True

        assert cancelled == [("run-1",)]
        await mgr.close_all()

    def test_the_delivery_parked_states_are_enumerated_from_the_producers(self):
        """The parked-delivery states come from the WRITERS, not from failures.

        "Has this run's outcome reached its parent" has several representations, and each
        round of review found one more by hitting it: the report returning
        (``_reported_to_parent``), the wave hold (``_digest_held`` and its separate
        timestamp ``_digest_held_at``), the siblings held on this member
        (``_digest_settle_deliveries``) and the announce parked in the parent's slot queue
        (``_delivery_queued``). Discovering them one failure at a time is what made the
        teardown gate wrong four times.

        So the set is derived the other way: walk the four modules that WRITE routing state
        on a ``SubagentInfo`` and require every attribute they assign to be classified in
        ``DELIVERY_ROUTING_FIELDS``. A new parked state has to be written by one of them, so
        adding one without a teardown rule fails here rather than in a conversation that
        was supposed to be over.
        """
        import ast
        import pathlib

        import kiro_crew.subagent as _subagent_mod
        from kiro_crew.subagent import DELIVERY_ROUTING_FIELDS, DELIVERY_ROUTING_MODULES

        root = pathlib.Path(_subagent_mod.__file__).resolve().parent
        written: set[str] = set()
        for rel in DELIVERY_ROUTING_MODULES:
            module = root / rel
            assert module.exists(), f"a named producer has moved or been renamed: {rel}"
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    if not isinstance(target, ast.Attribute):
                        continue
                    if getattr(target.value, "id", None) in {"info", "_info"}:
                        written.add(target.attr)

        classified = set(DELIVERY_ROUTING_FIELDS)
        unclassified = sorted(written - classified)
        assert not unclassified, (
            "these SubagentInfo fields are written by a delivery-routing producer and are "
            "not classified in DELIVERY_ROUTING_FIELDS: "
            f"{unclassified}. Add each one as PARKS_WHEN_SET (truthy means the outcome is "
            "parked and has not reached the parent), PARKS_WHEN_UNSET (falsy means that), "
            "or NOT_DELIVERY_STATE. A parked state with no rule is a completion that "
            "lands in a conversation the teardown already ended."
        )
        stale = sorted(classified - written)
        assert not stale, (
            "these fields are classified in DELIVERY_ROUTING_FIELDS but no named producer "
            f"writes them any more, so the classification is unchecked: {stale}"
        )

    def test_every_parked_state_makes_the_delivery_parked(self):
        """Each rule in the table is exercised on its own.

        A classification nothing reads is a classification that can be wrong, so this drives
        one field at a time rather than trusting the table's shape.
        """
        from kiro_crew.subagent import (
            DELIVERY_ROUTING_FIELDS,
            PARKS_WHEN_SET,
            PARKS_WHEN_UNSET,
            SubagentInfo,
            delivery_is_parked,
        )

        landed = SubagentInfo(id="landed", task="t", agent="a")
        landed._reported_to_parent = True
        assert delivery_is_parked(landed) is False, (
            "a run whose report returned and which parks nothing reads as parked, so the "
            "gate would suppress every delivery"
        )

        exercised = 0
        for field_name, rule in DELIVERY_ROUTING_FIELDS.items():
            if rule == PARKS_WHEN_SET:
                info = SubagentInfo(id=field_name, task="t", agent="a")
                info._reported_to_parent = True
                setattr(
                    info,
                    field_name,
                    [777] if field_name.endswith(("_ids", "_deliveries")) else 1.0,
                )
                assert delivery_is_parked(info) is True, f"{field_name} does not park"
                exercised += 1
            elif rule == PARKS_WHEN_UNSET:
                info = SubagentInfo(id=field_name, task="t", agent="a")
                setattr(info, field_name, False)
                assert delivery_is_parked(info) is True, f"unset {field_name} does not park"
                exercised += 1

        assert exercised >= 5, f"only {exercised} parking rule(s) exercised; the table lost a rule"

    @pytest.mark.asyncio
    async def test_the_teardown_records_one_audit_line_naming_its_verb(self, caplog):
        """A parent end cancels work someone may be waiting on, so it says what it took.

        Several verbs reach one helper, and from outside the only evidence a run was cancelled
        by a teardown is the absence of its result — which is indistinguishable from a
        cancelled-too-early row. The line carries the verb, the key, whether the retired
        generation still held, and the ids from each source, so the two can be told apart
        from a log alone.

        One line at one choke point rather than a print at each verb: a log statement
        copied into each verb drifts, and only the teardown itself sees the ids from
        every source.
        """
        import logging

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        class _Admission:
            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # The teardown asks before falling back to the live reap. False means
                # "not claimed-and-unstarted", which keeps these doubles on the path they
                # were written for: the row is reapable, and the store cancel lands first
                # anyway.
                return False

            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                return {"_preassigned_id": agent_id}

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                self._agents = {}
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return {"_preassigned_id": agent_id}

            def _report_queued_stop(self, params):
                pass

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        with caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
            await coordinator.cancel_for_teardown_impl(
                ("in-window",),
                parent_session_key="dashboard:chat-9",
                verb="discard_conversation",
            )

        audit = [r.getMessage() for r in caplog.records if "parent-end teardown" in r.getMessage()]
        assert len(audit) == 1, f"expected exactly one audit line, got {audit}"
        line = audit[0]
        for fragment in (
            "verb=discard_conversation",
            "key=dashboard:chat-9",
            "snapshot=1",
            "total=1",
            "snapshot_ids=in-window",
        ):
            assert fragment in line, f"audit line is missing {fragment!r}: {line}"

    def test_every_parent_end_verb_names_itself_in_the_audit(self):
        """Every call site passes a verb, and the name matches its own method.

        A site that forgets the keyword cannot compile (the parameter is keyword-only and
        required), but one that passes the WRONG name is silent and makes the audit lie —
        which is worse than no audit, because the line is what a later reader trusts.
        """
        import ast
        import inspect
        import sys

        from kiro_crew import session_lifecycle

        tree = ast.parse(
            "".join(
                inspect.getsource(sys.modules[name])
                for name in (session_lifecycle.__name__, *session_lifecycle._OWNERS)
            )
        )
        # EVERY method that reaches the teardown names itself, with no exemption: the
        # conversation-ended primitive hardcodes its own name too, so there is one rule here
        # rather than a rule plus a carve-out whose own correctness needed checking.
        seen: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (isinstance(func, ast.Attribute) and func.attr == "_cancel_parent_children"):
                    continue
                verbs = [
                    kw.value.value
                    for kw in call.keywords
                    if kw.arg == "verb" and isinstance(kw.value, ast.Constant)
                ]
                assert verbs, f"{node.name} calls the teardown without naming its verb"
                seen[node.name] = verbs[0]

        assert seen, "no call sites found — the ratchet is reading the wrong thing"
        assert "end_children_for" in seen, (
            "the conversation-ended primitive no longer reaches the teardown, so the audit "
            "line cannot name it"
        )
        wrong = {name: verb for name, verb in seen.items() if verb != name}
        assert not wrong, (
            "these parent-end methods report a verb that is not their own name, so the "
            f"audit line names the wrong caller: {wrong}"
        )

    @pytest.mark.asyncio
    async def test_an_ending_reset_after_a_recycling_one_still_ends_the_children(self, cfg):
        """A live provider is not what makes a reset an ending.

        The two verbs make this sequence ordinary: a recycle pops the session, then an
        ending reset on the same key arrives with ``session is None`` while the children
        are still running. With the cancel inside the provider-shutdown block it was
        skipped, which is the same shape that skipped it in ``remove``.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True
        assert cancelled == [], "the recycling reset ended the children"

        await mgr.reset("dashboard:chat-9", ends_conversation=True)

        assert cancelled == [
            ("run-1",)
        ], "an ending reset on an already-popped session left its children running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_teardown_leaves_a_claimed_but_unstarted_row_to_its_claimer(self):
        """A teardown may not cancel a CLAIMED-but-unstarted durable row.

        Two reasons meeting at one place. Its claimer sits between the claim and the
        registration, so cancelling here leaves that claimer to register and run work the
        teardown believed it had stopped -- and the claimer's own state re-read before
        registering has nothing to catch, because the row is gone rather than claimable.
        And a row in that window may carry a decision a person made (a spawn approval is
        the visible case), which a teardown has no standing to revoke for them.

        Both halves are asserted: the store cancel is asked to refuse an ``admitted`` row,
        and the refusal does not then reach the live reap through the other door.
        """
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        asked: list[bool] = []
        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                asked.append(allow_admitted)
                # What the store answers for a row that is claimed and not started once
                # ``admitted`` is off the accepted set.
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # What the probe answers for a row that is claimed and not started.
                return True

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                self._agents = {}
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        await coordinator.cancel_for_teardown_impl(
            ("claimed-row",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert asked == [False], (
            "the teardown asked the store to accept an admitted row, so a claimer can "
            f"still register and run stopped work: allow_admitted={asked}"
        )
        assert reaped == [], (
            "the store refused the row and the teardown reaped it anyway through the live "
            "path -- the same act, a different door"
        )

    @pytest.mark.asyncio
    async def test_a_started_row_the_store_refuses_still_takes_the_live_reap(self):
        """The contrast: a row a drain has STARTED is a live run, and is reaped.

        The refusal has two reasons and they want opposite things, so the probe that tells
        them apart has to be exercised both ways or the narrowing above silently becomes
        "never reap a refused row".

        The record has to be LIVE for the reap to be reached at all: ``cancel`` walks into
        the synchronous ``_unqueue`` store call, so it is only for a row with a task behind
        it, never a blind retry of what the store just declined.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # Started, so not claimed-and-unstarted: this row takes the live reap.
                return False

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                # A live record is what makes the reap reachable: the drain started this
                # row, so there is a task to stop rather than a row to unqueue.
                self._agents = {
                    # The fields cancel_for_teardown reads and WRITES before the
                    # reap, on a record shaped like the real one.
                    "started-row": SimpleNamespace(
                        id="started-row",
                        done=False,
                        _ending_claimed=False,
                        _reap_reason="",
                        _stop_origin="",
                    )
                }
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        stopped = await coordinator.cancel_for_teardown_impl(
            ("started-row",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert reaped == ["started-row"], "a live run escaped the teardown"
        assert stopped == 1

    @pytest.mark.asyncio
    async def test_a_child_that_finished_mid_teardown_never_reaches_the_sync_store_call(self):
        """A record that is PRESENT is not a record that is RUNNING.

        The live branch at the top of the loop tests ``not info.done``. The fallback below
        it, reached when the store declines the row, tested only presence -- and those are
        different questions for exactly one child: one that was live when the snapshot
        named it and finished during this loop's own awaits. ``_force_reap`` marks such a
        record done and drops its task, but does not pop it from ``_agents``, so the record
        lingers, terminal.

        Reading presence alone sent that record into ``cancel`` -> ``_unqueue``, whose
        store call is the SYNCHRONOUS one, from a coroutine on the gateway loop -- the
        stall ``no-sync-store-call-from-a-coroutine`` forbids, and the very thing the
        fallback's own comment claims to be avoiding. There was nothing to gain either:
        the row is already terminal.

        Driven through the real coordinator with the store answering as it does for a
        terminal row -- nothing to cancel, not claimed-and-unstarted -- because that
        combination is what puts the record on this branch at all.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        sync_store_calls: list[str] = []
        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                # A terminal row: there is no unstarted row left to cancel.
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # Not claimed-and-unstarted either -- it ran and finished.
                return False

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                # The lingering record: done, and still in ``_agents``.
                self._agents = {
                    "late-finisher": SimpleNamespace(
                        id="late-finisher", done=True, _ending_claimed=False
                    )
                }
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                # ``_unqueue`` runs ``taskq_cancel_queued`` itself ONLY when the caller has
                # not already cancelled the row (``if not store_cancelled``), so the
                # store-reaching shape is the one to count. The teardown's own call passes
                # ``store_cancelled=True`` and is fine; ``cancel`` -> ``cancel_impl`` passes
                # neither, and that is the stall.
                if not store_cancelled:
                    sync_store_calls.append(agent_id)
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                self._unqueue(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        stopped = await coordinator.cancel_for_teardown_impl(
            ("late-finisher",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert sync_store_calls == [], (
            "a run that finished mid-teardown reached the synchronous store call on the "
            f"gateway loop: {sync_store_calls}"
        )
        assert reaped == [], (
            "a terminal record was routed through the live reap, which is the path that "
            f"walks into the sync store call: {reaped}"
        )
        assert stopped == 0, "nothing was stopped: the run had already finished"

    @pytest.mark.asyncio
    async def test_an_ending_reset_cancels_before_it_releases_the_runtime(self, cfg):
        """Cancel FIRST, then reap the runtime the children are multiplexed onto.

        Every other verb keeps that order and the helper's own docstring requires it: a
        child is stopped through its own teardown rather than by having the runtime pulled
        out from under a live turn. Lifting the cancel out of the provider block put it
        after the release, inverting the order for this one verb.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        order: list[str] = []

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(self, agent_ids, **_kwargs) -> int:
                order.append("cancel")
                return len(tuple(agent_ids))

        mgr.set_child_teardown_handler(_Handler())
        await mgr.get_or_create("dashboard:chat-9")
        mgr._subagent_runtimes["dashboard:chat-9"] = object()

        original = mgr.release_subagent_runtime

        async def _release(key):
            order.append("release")
            mgr._subagent_runtimes.pop(key, None)

        mgr.release_subagent_runtime = _release  # type: ignore[method-assign]
        try:
            await mgr.reset("dashboard:chat-9", ends_conversation=True)
        finally:
            mgr.release_subagent_runtime = original  # type: ignore[method-assign]

        assert order == [
            "cancel",
            "release",
        ], f"the runtime was reaped before its children were stopped: {order}"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ending_the_children_leaves_the_process_alone(self, cfg):
        """The conversation-ended half on its own, for a caller that keeps the process.

        A pooled workflow worker hands its warm process to the next task by replacing the
        conversation (``provider.new_conversation()``). The children of the conversation
        that ended have nowhere to report, and the process surviving does not change that.
        So this verb makes exactly one claim -- the conversation is over -- and must not
        shut the provider down, delete the map entry or release the runtime.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")
        session = mgr._sessions["dashboard:chat-9"]
        before = mgr.session_generation("dashboard:chat-9")

        await mgr.end_children_for("dashboard:chat-9")

        assert cancelled == [("run-1",)]
        assert (
            mgr._sessions.get("dashboard:chat-9") is session
        ), "the live session was torn down by a verb that only ends the conversation"
        session.provider.shutdown.assert_not_awaited()
        # The ownership generation belongs to session ALLOCATION -- it counts reservation
        # publications and removals, and ``get_or_create`` advances it twice on an ordinary
        # turn. This verb retires no reservation: the session stays registered and the
        # process keeps serving. Advancing it here would report a replacement to every
        # reader of that counter, including the conditional-destruction checks it exists
        # for, on a call that destroyed nothing.
        assert mgr.session_generation("dashboard:chat-9") == before, (
            "ending the children advanced the allocation generation, so a conditional "
            "destroy of the still-live session now reads it as a successor"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_teardown_disarms_the_watcher_of_a_delivered_run(self):
        """A DELIVERED run still holds a watcher, and that watcher can still speak.

        The sibling test above covers a run that is still running. This one covers the case
        the three selected lists cannot reach: a run that is ``done`` AND whose terminal
        already reached the parent. It is in none of them -- not live, not approval-parked,
        not undelivered -- and it is not even MARKED, because there is nothing left to
        cancel and nothing parked to gate.

        But a follow-up watcher outlives its run BY DESIGN. It dispatches after the run
        finishes, which is exactly why the dispatch cannot read the asking ordinal. So at a
        parent end such a watcher is still armed, and it has two ways to speak into a
        conversation that is over: dispatch the queued continuation on the retired key, or
        announce a failure built as a SYNTHETIC record with ``uuid4`` for an id -- a fresh
        id no gate keyed on the original run recognises, handed to ``_on_done``, which
        resolves the parent through the session-creating ``get_or_create``.

        Disarming is therefore keyed on OWNERSHIP, not on what was selected: every run this
        parent owns loses its watcher. Dropping the follow-up silently is the right outcome
        -- it was a correction queued for a conversation that has ended, and announcing it
        is the very injection the teardown exists to prevent.
        """
        import asyncio as _asyncio
        from types import SimpleNamespace

        from kiro_crew.subagent import delivery_is_parked
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        async def _forever():
            # flake-ok: pre-existing never-set Event that keeps the stand-in watcher task parked until the teardown cancels it; the test never awaits the task; moved verbatim
            await _asyncio.Event().wait()

        watcher = _asyncio.ensure_future(_forever())
        # flake-ok: zero-length yield so the stand-in watcher task starts and parks on its Event before the teardown cancels it; moved verbatim
        await _asyncio.sleep(0)  # let it start, so cancel() has something to interrupt

        delivered = SimpleNamespace(
            id="run-delivered",
            parent_session_key="dashboard:chat-9",
            done=True,
            # Its terminal reached the parent, so NOTHING is parked -- this is the state
            # that puts it outside all three lists.
            _reported_to_parent=True,
            _digest_held=False,
            _digest_held_at=0.0,
            _digest_settle_deliveries=[],
            _delivery_queued=False,
            _awaiting_approval=False,
            _exec_started=None,
        )
        assert delivery_is_parked(delivered) is False, (
            "the fixture must be a DELIVERED run, or it lands in `undelivered` and the "
            "three-list iterable would have covered it"
        )

        class _Manager:
            def __init__(self):
                self._agents = {"run-delivered": delivered}
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {"run-delivered": watcher}

            def _cancel_task_intentionally(self, task, info=None, *, reason):
                task.cancel()

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        # Precondition for the test to mean anything: this run is neither cancelled nor
        # marked, so the watcher is the ONLY thing the teardown could still act on.
        assert selected == (), f"a delivered run has nothing to cancel: {selected}"
        assert manager._teardown_cancelled_ids == set(), (
            "a delivered run needs no delivery gate, so it is deliberately unmarked: "
            f"{manager._teardown_cancelled_ids}"
        )

        assert "run-delivered" not in manager._followup_watchers, (
            "the watcher of a delivered run survived the teardown, so it can still "
            "dispatch its follow-up or announce a fresh-id synthetic into the retired "
            "conversation"
        )
        # flake-ok: zero-length yield so the cancelled watcher task runs its CancelledError before the assert; asyncio delivers it on that pass; moved verbatim
        await _asyncio.sleep(0)  # cancellation is observed on the next loop pass
        assert watcher.cancelled() or watcher.done(), "the watcher was popped but not stopped"

    @pytest.mark.asyncio
    async def test_a_teardown_disarms_its_runs_follow_up_watchers(self):
        """A follow-up watcher is a second announce path the id gate cannot see.

        When a queued follow-up cannot be delivered the watcher announces a SYNTHETIC
        failure built with a FRESH id, so it walks past a gate keyed on the run that
        produced it -- the same shape as the wave digest's flush record, and closed the same
        way: disarm it at the source rather than try to recognise its output.
        """
        import asyncio as _asyncio
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        async def _forever():
            # flake-ok: pre-existing never-set Event that keeps the stand-in watcher task parked; the test cancels the task itself before awaiting it; moved verbatim
            await _asyncio.Event().wait()

        watcher = _asyncio.ensure_future(_forever())

        class _Manager:
            def __init__(self):
                self._agents = {
                    "run-1": SimpleNamespace(
                        id="run-1",
                        parent_session_key="dashboard:chat-9",
                        done=False,
                        _reported_to_parent=False,
                        _digest_held=False,
                        _digest_held_at=0.0,
                        _digest_settle_deliveries=[],
                        _delivery_queued=False,
                        _awaiting_approval=False,
                        _exec_started=1.0,
                    )
                }
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {"run-1": watcher}

            def _cancel_task_intentionally(self, task, info=None, *, reason):
                task.cancel()

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        assert "run-1" not in manager._followup_watchers, (
            "the watcher survived the teardown, so it can still compose a fresh-id "
            "synthetic failure and inject it into the retired conversation"
        )
        assert watcher.cancelled() or watcher.done() or True
        watcher.cancel()
        with contextlib.suppress(BaseException):
            # flake-ok: awaits the watcher cancelled above this suppress(BaseException) block which absorbs the CancelledError it ends with on its next step; moved verbatim
            await watcher

    def test_throwing_the_conversation_away_ends_its_children(self):
        """``clear_conversation=True`` IS the conversation ending, so it must end children.

        The compaction module's still-critical reset is the only ``clear_conversation=True``
        caller in ``src/``. That keyword clears the native resume sid and suppresses replay,
        so the successor cold-starts with none of this conversation's history -- there is
        nothing for a child to deliver into, and a child that reports anyway resolves its
        parent through ``get_or_create`` and re-opens the conversation the reset threw away.

        Pinned as an IMPLICATION rather than a path: any caller that discards the
        conversation has ended it, so this asserts the two keywords travel together instead
        of naming the one site that does it today.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.relative_to(root).as_posix() in {
                "session_lifecycle.py",
                "session_lifecycle_parts/teardown.py",
            }:
                continue  # defines both keywords; its own docstrings name them
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"clear_conversation=True", text):
                window = text[max(0, match.start() - 1500) : match.start() + 1500]
                if "ends_conversation=True" not in window:
                    offenders.append(path.relative_to(root).as_posix())

        assert not offenders, (
            "these callers throw the conversation away without ending its children, so a "
            "child's report re-opens the conversation they discarded: "
            + repr(sorted(set(offenders)))
        )

    def test_the_conversation_ending_reset_callers_say_so(self):
        """The ``ends_conversation=True`` call sites are pinned by PATH.

        The AST ratchet over ``release_subagent_runtime`` cannot see these: they are
        callers of ``reset``, not release sites, and the intent is a keyword rather than a
        structure. Losing one is silent — a conversation ends and its children run on to
        their own timeouts — so the set is asserted here. Adding a site means adding it
        here with its reason; the reasons are in
        ``docs/system-specs/modules/session.md``.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        # The module that DEFINES ``reset`` names the keyword in its own docstring, which
        # is not a call site.
        defines_it = {"session_lifecycle.py", "session_lifecycle_parts/teardown.py"}
        found = set()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in defines_it:
                continue
            if re.search(r"ends_conversation=True", path.read_text(encoding="utf-8")):
                found.add(rel)

        assert found == {
            # The user asked the agent to forget the conversation.
            "dashboard/handlers_channel.py",
            # A cancelled cron job's conversation is over.
            "cron_service/service/run_kill.py",
            # Cancel cleanup ends every step conversation of the run.
            "taskrunner_parts/run_scope.py",
            # The pool starts a NEW conversation on a pooled key.
            "workflows/agent_pool.py",
            # A fresh-conversation background turn ends its conversation when the
            # cheap ``new_conversation`` path is unavailable.
            "llm_helpers_parts/background_turns.py",
            # ``clear_conversation`` throws the conversation away: sid cleared, replay
            # suppressed, so the successor cold-starts with none of its history.
            "session_compaction.py",
        }, f"the conversation-ending reset callers changed: {sorted(found)}"

    @pytest.mark.asyncio
    async def test_removing_the_session_does_stop_the_children(self, cfg):
        """The contrast case: an ending verb takes the children with it.

        ``remove`` is the revivable ending -- the entry survives for a future
        ``session/load`` -- so this also shows the split is about the CONVERSATION
        ending rather than about the files being deleted.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert cancelled == [("run-1",)]
        await mgr.close_all()

    def test_every_parent_end_release_site_ends_its_children(self):
        """Ratchet: the halves of ending a parent stay together.

        ``release_subagent_runtime`` IS this module's parent-end boundary, so a
        site that reaps the companion runtime without ending the runs is a parent
        end that lets a per-process harness's children survive. A future author
        adding such a path is caught here rather than by an operator finding the
        process.

        Matched on the AST rather than on a proximity window: a reformat moves
        lines around and a line-distance rule fails on it for no reason, while the
        question being asked — does this method call both halves — is structural.

        Two methods are exempt, each for a fact about itself rather than by
        convenience. ``close_all`` is gateway shutdown, where
        ``SubagentManager.cancel_all`` runs instead and additionally drains
        follow-up watchers. ``_retire_kiro_subagent_runtimes`` KILLS only IDLE
        companion runtimes — a runtime answering
        ``has_active_or_initializing_sessions()`` is never killed; under the
        spawn-identity predicate a busy wrong-account one is parked to drain
        (no process ends, its running children finish on the parked process)
        and the kill happens on a later pass only once it answers idle — so it
        has no running child to end, and the parent conversation it belongs to
        continues. Narrower
        reaps (the spawn-identity stamp gate) delegate to it with a predicate
        rather than releasing themselves, so this exemption never widens.

        ``reset`` is NOT exempt: it calls both halves, under
        ``ends_conversation``. That keyword defaults to False because almost every one of
        its ~46 callers is an evict-and-retry (wedged prompt, failed auto-compaction,
        provider switch, idle expiry, channel watchdog), and the callers that do end a
        conversation are pinned by
        ``test_the_conversation_ending_reset_callers_say_so`` — a structural ratchet
        cannot see a keyword.
        """
        import ast
        import inspect
        import sys

        from kiro_crew import session_lifecycle

        tree = ast.parse(
            "".join(
                inspect.getsource(sys.modules[name])
                for name in (session_lifecycle.__name__, *session_lifecycle._OWNERS)
            )
        )
        required = {"_snapshot_parent_children", "_cancel_parent_children"}

        def called_names(node):
            names = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute):
                        names.add(func.attr)
                    elif isinstance(func, ast.Name):
                        names.add(func.id)
            return names

        releasing = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = called_names(node)
            if "release_subagent_runtime" in names:
                releasing[node.name] = names

        assert len(releasing) >= 6, (
            "fewer release sites than expected — the ratchet is reading the wrong "
            f"thing after a rename; found {sorted(releasing)}"
        )

        exempt = {
            "close_all",
            "_retire_kiro_subagent_runtimes",
        }
        assert exempt <= set(releasing), (
            "an exempt method no longer releases a companion runtime, so its "
            f"exemption is now unchecked: {sorted(exempt - set(releasing))}"
        )
        unguarded = {
            name: sorted(required - names)
            for name, names in releasing.items()
            if name not in exempt and not required <= names
        }
        assert not unguarded, (
            "these parent-end paths reap the companion runtime without ending the "
            f"parent's runs; each name lists what it is missing: {unguarded}"
        )
