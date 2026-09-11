"""Tests for the first-run cron tab pre-create (ensure_cron_slot).

Result injection is otherwise the ONLY creator site for a ``cron-{job.id}``
dashboard slot, and it runs after a turn completes — so without a pre-create a
brand-new job's FIRST run has no tab. Session-control caller identity walks the
live slot table (``caller_slot_key``), so every verb a first run calls would
refuse with ``caller_unidentified``; the dashboard-surface registry has the same
first-run hole. These tests pin the pre-create path and the invariant: the
hydration must MOVE with the link, so the injection's unlink guard stays an
idempotent no-op after a pre-create.

The state/job fakes mirror test_dashboard_cron_to_chat.py's, plus ``get_slot``
and ``has_slot`` defined as REAL functions over the same dict: on a bare
MagicMock both auto-return truthy mocks, which would silently satisfy the
short-circuit under test and make every assertion here unable to fail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.cron_inject import (
    ensure_cron_slot,
    inject_cron_result_to_dashboard,
)
from kiro_crew.dashboard.session_control import caller_slot_key
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes to the process-global dashboard-surface registry;
    reset it so keys from these mock states never leak into other tests."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(history_messages=None):
    """A mock DashboardState whose slot accessors are real functions.

    ``get_slot``/``has_slot`` must NOT be MagicMock attributes: the pre-create
    short-circuits on ``get_slot(...).linked_session_key``, and an auto-mock
    returns a truthy mock for BOTH — turning every test into a no-op pass.
    """
    state = MagicMock()
    slots: dict[str, MagicMock] = {}
    state._slots = slots

    def get_or_create_slot(name=None, agent="", origin=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True, meta=None, mint_mid=True):
                supplied = meta.get("mid") if isinstance(meta, dict) else None
                stored_meta = dict(meta) if isinstance(meta, dict) else {}
                if mint_mid and not supplied:
                    stored_meta["mid"] = f"m-test-{len(slot.messages)}"
                msg = {
                    "role": role,
                    "content": content,
                    "cls": cls,
                    **({"meta": stored_meta} if stored_meta else {}),
                }
                slot.messages.append(msg)
                return msg

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.get_slot = lambda name: slots.get(name)
    state.has_slot = lambda name: name in slots
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _make_job(job_id="job42", name="nightly-sweep", persistent=True, hidden=False):
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.agent_id = ""
    job.member_id = ""
    job.memory_store = ""
    job.persistent_session = persistent
    job.hide_in_chat = hidden
    return job


class TestEnsureCronSlot:
    @pytest.mark.asyncio
    async def test_member_identity_is_available_at_start_and_retained_at_delivery(self):
        state = _make_state()
        create_slot = state.get_or_create_slot
        state.get_or_create_slot = MagicMock(wraps=create_slot)
        job = CronJob(
            id="member-job",
            name="daily report",
            message="report",
            agent_id="kirocrew",
            member_id="writer",
            memory_store="writer-private",
        )

        await ensure_cron_slot(state, job)

        slot = state.get_slot("cron-member-job")
        assert slot.memory_store == "writer-private"
        assert slot.linked_session_key == "cron:member-job"
        assert state.get_or_create_slot.call_args.kwargs["agent"] == "writer"
        inject_cron_result_to_dashboard(state, job, "report ready", history=None)
        assert state.get_slot("cron-member-job") is slot
        assert slot.memory_store == "writer-private"
        state.conversation_log.read_messages.assert_called_once_with("cron:member-job")

    @pytest.mark.asyncio
    async def test_first_run_tab_exists_with_identity_before_any_result(self):
        """Tab + link + registry row exist at run start."""
        state = _make_state()
        job = _make_job()

        await ensure_cron_slot(state, job)

        slot = state.get_slot("cron-job42")
        assert slot is not None, "pre-create must mint the tab before any result"
        assert slot.linked_session_key == "cron:job42"
        assert slot.title == "Cron: nightly-sweep"
        # The registry consumers (sub-agent routing, widget/question/approval
        # delivery) read the process-global surface set the bind publishes.
        assert has_dashboard_surface("cron:job42")

    @pytest.mark.asyncio
    async def test_caller_identity_resolves_on_first_run(self):
        """The exact refusal from the issue: caller_slot_key must resolve the
        presented ``cron:{id}`` once the pre-create has bound the link —
        before it, the walk finds nothing and session-control denies with
        caller_unidentified."""
        state = _make_state()
        job = _make_job()
        assert caller_slot_key(state, "cron:job42") == "", "control: unresolvable before"

        await ensure_cron_slot(state, job)

        assert caller_slot_key(state, "cron:job42") == "cron-job42"

    @pytest.mark.asyncio
    async def test_hydration_moves_with_the_link_and_injection_does_not_rehydrate(self):
        """The invariant the issue names: pre-creating WITH the link must also
        hydrate, and the later injection's unlink guard must then no-op — a
        pre-create that linked without hydrating would leave a follow-up turn
        with no memory of prior runs, silently."""
        prior = [
            {"role": "user", "content": "run the sweep"},
            {"role": "assistant", "content": "swept 3 queues"},
        ]
        state = _make_state(history_messages=prior)
        job = _make_job()

        await ensure_cron_slot(state, job)

        slot = state.get_slot("cron-job42")
        hydrated = [m["content"] for m in slot.messages]
        assert "swept 3 queues" in hydrated, "hydration must happen at the pre-create"
        rows_after_bind = len(slot.messages)

        # Delivery after the run: must not re-hydrate (no duplicated history),
        # must still append the result pair.
        inject_cron_result_to_dashboard(state, _inject_shaped(job), "fresh result", history=prior)
        contents = [m["content"] for m in slot.messages]
        assert contents.count("swept 3 queues") == 1, "re-hydration duplicated history"
        assert any("fresh result" in c for c in contents)
        assert len(slot.messages) > rows_after_bind

    @pytest.mark.asyncio
    async def test_silent_first_run_gate_condition_now_holds(self):
        """The adjacent first-run gap: the silent/dedup delivery paths gate
        injection on ``has_slot`` — False on a first run before this fix, so a
        silent job's first result never reached its tab. Pre-create makes the
        gate's condition true; the gate expression itself lives in the
        executor and is exercised here verbatim."""
        state = _make_state()
        job = _make_job()
        gate = lambda: (  # noqa: E731 — the executor's own expression shape
            job.persistent_session and not job.hide_in_chat and state.has_slot(f"cron-{job.id}")
        )
        assert not gate(), "control: the silent-path gate is False before pre-create"

        await ensure_cron_slot(state, job)

        assert gate(), "pre-create must satisfy the silent/dedup injection gate"

    @pytest.mark.asyncio
    async def test_hidden_job_gets_no_tab(self):
        """hide_in_chat=True keeps the documented no-tab contract: the flag
        suppresses slot creation entirely, so the pre-create must not mint
        what the delivery would refuse to mint."""
        state = _make_state()
        await ensure_cron_slot(state, _make_job(hidden=True))
        assert state.get_slot("cron-job42") is None
        assert not has_dashboard_surface("cron:job42")

    @pytest.mark.asyncio
    async def test_ephemeral_job_gets_no_tab(self):
        """persistent_session=False means a per-run session key and no tab:
        no tab, no identity, no dispatch stays the fail-closed contract."""
        state = _make_state()
        await ensure_cron_slot(state, _make_job(persistent=False))
        assert state.get_slot("cron-job42") is None

    @pytest.mark.asyncio
    async def test_second_call_short_circuits_without_a_transcript_read(self):
        """Runs ≥2 must not pay the whole-transcript parse: an existing linked
        slot returns before any conversation_log I/O."""
        state = _make_state()
        job = _make_job()
        await ensure_cron_slot(state, job)
        state.conversation_log.read_messages.reset_mock()

        await ensure_cron_slot(state, job)

        state.conversation_log.read_messages.assert_not_called()
        # And the bind stayed stable — same link, no duplicate slot.
        assert state.get_slot("cron-job42").linked_session_key == "cron:job42"
        assert len(state._slots) == 1

    @pytest.mark.asyncio
    async def test_first_run_with_no_history_is_benign(self):
        """A genuinely new job has an empty cron:{id} transcript: the bind
        hydrates nothing and still produces a linked, published tab."""
        state = _make_state(history_messages=[])
        job = _make_job()
        await ensure_cron_slot(state, job)
        slot = state.get_slot("cron-job42")
        assert slot.linked_session_key == "cron:job42"
        assert slot.messages == []


def _inject_shaped(job):
    """Extend the pre-create fake job with the fields the injector reads.

    The injector writes the run's prompt/result pair, so it touches string
    fields the pre-create never reads; real values keep them out of the
    redactors as non-strings (same rationale as the sibling module's fake).
    """
    from kiro_crew.cron import CronJob

    job.message = "run the sweep"
    job.last_result_ts = 0.0
    job.timezone = "UTC"
    job.last_result_stamp = CronJob._render_run_stamp(job, 0.0)
    return job


class TestPreCreateGuard:
    """The pre-create must never cost the run — or the job — its life.

    A review lane convicted the bare ``await ensure_cron_slot(...)`` at the
    gateway callsite: ``_execute`` (cron.py) clears ``run_never_started``
    BEFORE the callback and its ``except`` arm never re-arms it, so an error
    propagating out of the pre-dispatch window reached the delete site with
    the retention marker down and a ``delete_after_run`` one-shot — the
    default at-scheduled shape — was consumed by a run that never dispatched.
    These pins hold ``_pre_create_cron_slot`` to the fire-time gate's proven
    contract: armed for exactly the await, ordinary failure contained,
    cancellation escaping WITH the marker, linear clear before dispatch.

    The delete-decision assertions mirror cron.py's own expression verbatim
    (``delete_owed = job.delete_after_run and not (job.fire_time_denied or
    job.run_never_started)``) so a drift in either side breaks a test, not a
    user's scheduled job.
    """

    @staticmethod
    def _delete_owed(job) -> bool:
        # Verbatim mirror of cron.py's delete site decision.
        return bool(job.delete_after_run and not (job.fire_time_denied or job.run_never_started))

    @staticmethod
    def _arm_lifecycle(job):
        """Mirror _execute's pre-callback state: marker cleared, one-shot owed."""
        job.run_never_started = False
        job.fire_time_denied = False
        job.delete_after_run = True
        return job

    @pytest.mark.asyncio
    async def test_attack_slot_failure_is_contained_and_run_proceeds(self):
        """A transcript/store error during the pre-create must NOT propagate:
        at the unguarded callsite it reached cron.py's except arm and the run
        died for an amenity. Contained, the run proceeds; having actually
        dispatched, a one-shot is then legitimately consumed."""
        from kiro_crew.slack.gateway import _pre_create_cron_slot

        state = _make_state()
        job = self._arm_lifecycle(_make_job())
        state.conversation_log.read_messages.side_effect = OSError("transcript unreadable")

        await _pre_create_cron_slot(state, job)  # must not raise

        assert job.run_never_started is False, "dispatch begins after the guard: marker must clear"
        assert self._delete_owed(job) is True, "a run that DID dispatch still consumes its one-shot"

    @pytest.mark.asyncio
    async def test_attack_cancel_at_await_retains_the_one_shot(self):
        """The wake deadline cancelling AT the pre-create await is the exact
        convicted window: CancelledError must escape (cancellation is not
        ours to swallow) AND leave run_never_started armed, so the delete
        site retains the unexecuted one-shot."""
        import asyncio

        from kiro_crew.slack.gateway import _pre_create_cron_slot

        state = _make_state()
        job = self._arm_lifecycle(_make_job())

        started = asyncio.Event()

        def _hang(*a, **k):
            # read_messages runs off-loop via prefetch; block it until cancelled.
            started.set()
            # A short sync sleep keeps the await pending long enough to cancel.
            import time as _time

            _time.sleep(0.05)
            return []

        state.conversation_log.read_messages.side_effect = _hang
        task = asyncio.ensure_future(_pre_create_cron_slot(state, job))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert (
            job.run_never_started is True
        ), "cancelled pre-dispatch: retention marker must survive"
        assert self._delete_owed(job) is False, "the unexecuted one-shot must be retained"

    @pytest.mark.asyncio
    async def test_control_normal_first_run_still_binds_through_the_guard(self):
        """The guard changes failure semantics only: the healthy path still
        mints the tab, binds identity, and clears the marker for dispatch."""
        from kiro_crew.slack.gateway import _pre_create_cron_slot

        state = _make_state()
        job = self._arm_lifecycle(_make_job())

        await _pre_create_cron_slot(state, job)

        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.linked_session_key == "cron:job42"
        assert caller_slot_key(state, "cron:job42") == "cron-job42"
        assert job.run_never_started is False
        assert self._delete_owed(job) is True, "healthy one-shot must still be consumed post-run"

    @pytest.mark.asyncio
    async def test_control_ineligible_job_untouched_no_new_deny(self):
        """Benign no-new-deny: the wrapper adds no refusal path of its own —
        eligibility still lives in ensure_cron_slot, an ineligible job gets
        no tab and no marker residue."""
        from kiro_crew.slack.gateway import _pre_create_cron_slot

        state = _make_state()
        job = self._arm_lifecycle(_make_job(hidden=True))

        await _pre_create_cron_slot(state, job)

        assert state.get_slot("cron-job42") is None
        assert job.run_never_started is False
        assert self._delete_owed(job) is True
