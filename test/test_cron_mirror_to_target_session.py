"""A cron job that names a dashboard session gets its result mirrored there.

The dashboard delivery leg routes by job id alone -- every result lands in
``cron-{job.id}``, whoever the job was created for -- while the channel leg
already routes by ORIGIN off ``job.session_key``. These pin the missing rung, and
pin that it is a MIRROR: the job keeps its own tab and its own session, so a
person's conversation cannot be rebound to the cron or hydrated with its history.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from chat_test_helpers import _make_state  # noqa: E402

from kiro_crew.cron import CronJob  # noqa: E402
from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard  # noqa: E402
from kiro_crew.session_surface import set_dashboard_surfaced  # noqa: E402

_SLOT = "my-chat"
_TARGET = f"dashboard:{_SLOT}"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes to the process-global dashboard-surface registry;

    reset it so keys from these states never leak into other tests. Same fixture
    `test_cron_first_run_tab.py` carries for the same reason.
    """
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _job(**overrides):
    kwargs = {"id": "j1", "name": "nightly", "message": "summarize the queue"}
    kwargs.update(overrides)
    return CronJob(**kwargs)


def _contents(slot):
    return [m.get("content", "") for m in slot.messages]


def _results(slot):
    return [c for c in _contents(slot) if c.startswith("# Cron Job Result:")]


@pytest.fixture
def state(tmp_path):
    st = _make_state(tmp_path)
    st.get_or_create_slot(name=_SLOT)
    return st


class TestMirroredIntoTheTargetSession:
    def test_result_reaches_the_named_session(self, state):
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        target = state.get_slot(_SLOT)
        assert len(_results(target)) == 1
        assert "queue is empty" in _results(target)[0]

    def test_the_job_still_gets_its_own_tab(self, state):
        """A mirror, not a redirect: the cron's own transcript is unchanged."""
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        own = state.get_slot("cron-j1")
        assert own is not None
        assert len(_results(own)) == 1

    def test_the_prompt_row_is_not_mirrored(self, state):
        """The prompt row is the cron tab's run boundary.

        In someone else's conversation it would read as a turn they typed, so the
        target gets the assistant result alone.
        """
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        target = state.get_slot(_SLOT)
        assert not [c for c in _contents(target) if c.startswith("# Cron Run:")]
        # The job's own tab still gets the pair.
        assert [c for c in _contents(state.get_slot("cron-j1")) if c.startswith("# Cron Run:")]

    def test_the_target_session_is_not_rebound_to_the_cron(self, state):
        """The load-bearing one.

        Binding the target would set ``linked_session_key`` to ``cron:{id}`` and
        hydrate the job's prior runs into a live conversation, so the person's next
        turn would run AS the job.
        """
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert state.get_slot(_SLOT).linked_session_key == ""
        # ...while the job's own tab is linked, as it always was.
        assert state.get_slot("cron-j1").linked_session_key == "cron:j1"

    def test_the_mirror_writes_no_transcript_of_its_own(self, state):
        """Persistence belongs to the slot that owns the conversation.

        A deferred, off-loop write aimed at a session this job does not own had two
        opposed failures with no local fix — creating the transcript resurrected a
        deleted conversation, refusing to create it dropped the row for a session
        with nothing on disk yet. The mirror therefore writes only the live row.
        """
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert len(_results(state.get_slot(_SLOT))) == 1
        assert state.conversation_log.read_messages(_TARGET) == []

    def test_the_row_is_flagged_for_the_periodic_slot_save(self, state):
        """Which is what makes the live-only append durable in the ordinary way.

        `flush_slot_now` skips a slot whose `_dirty` is false, so the flag is the
        whole mechanism by which the mirrored row reaches disk with the rest of the
        window.
        """
        state.get_slot(_SLOT)._dirty = False
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert state.get_slot(_SLOT)._dirty is True

    def test_the_flush_persists_the_mirrored_row(self, state):
        """End to end: the ordinary save path carries the mirror's row to disk."""
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        state.flush_slot_now(state.get_slot(_SLOT))
        rows = state.conversation_log.read_messages(_TARGET)
        assert [r for r in rows if "queue is empty" in r.get("content", "")]

    def test_a_refire_of_the_same_result_does_not_stack(self, state):
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert len(_results(state.get_slot(_SLOT))) == 1


class TestNotMirrored:
    def test_a_job_with_no_target_reaches_only_its_own_tab(self, state):
        inject_cron_result_to_dashboard(state, _job(), "queue is empty", history=None)
        assert _results(state.get_slot(_SLOT)) == []
        assert len(_results(state.get_slot("cron-j1"))) == 1

    def test_a_channel_origin_job_is_left_to_the_channel_leg(self, state):
        """Mirroring a resolved tab match would double-deliver the channel post."""
        job = _job(session_key="slack:1788203585.923439")
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert _results(state.get_slot(_SLOT)) == []

    def test_a_deleted_target_costs_a_copy_not_a_run(self, state):
        job = _job(session_key="dashboard:gone")
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert len(_results(state.get_slot("cron-j1"))) == 1

    def test_a_tab_linked_to_another_session_is_skipped(self, state):
        """`cron adopt` can record a target the create path would have refused."""
        state.get_slot(_SLOT).linked_session_key = "slack:1788203585.923439"
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert _results(state.get_slot(_SLOT)) == []

    def test_a_replay_does_not_re_deliver(self, state):
        """`/to-chat` re-surfaces an older result and passes include_prompt=False.

        The in-memory dedup only sees the target slot's bounded buffer, so a replay
        of a result outside that window — or one whose buffer a restart rebuilt —
        appends the same run to someone's conversation a second time.
        """
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        # Simulate a window that does not hold it, as a restart or 5000 rows leaves.
        state.get_slot(_SLOT).messages.clear()
        inject_cron_result_to_dashboard(
            state, job, "queue is empty", include_prompt=False, history=None
        )
        assert _results(state.get_slot(_SLOT)) == []

    def test_a_deleted_transcript_is_never_recreated(self, state):
        """A run after a deletion must not bring the conversation back.

        Writing no transcript makes this structural rather than guarded: there is
        no code path here that can create the file, so the deletion stands whatever
        the timing between the run and the delete.
        """
        state.conversation_log.append(_TARGET, "user", "hello")
        job = _job(session_key=_TARGET)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        state.conversation_log.delete_session(_TARGET)
        state.get_slot(_SLOT).messages.clear()
        inject_cron_result_to_dashboard(state, job, "second run", history=None)
        assert state.conversation_log.read_messages(_TARGET) == []

    def test_targeting_the_jobs_own_tab_does_not_double_deliver(self, state):
        job = _job(session_key="dashboard:cron-j1")
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        assert len(_results(state.get_slot("cron-j1"))) == 1

    @pytest.mark.parametrize("corrupt", [123, 1.5, True, ["dashboard:x"], {"k": "v"}])
    def test_a_non_string_stored_key_degrades_instead_of_raising(self, state, corrupt):
        """`crons.json` round-trips this field without coercion.

        A hand-edited or corrupt store can hand back a non-string, and here that
        would crash the injection of a run that had already succeeded. Same
        degrade-rather-than-raise contract `_cron_origin_key` states for the field.
        """
        job = _job()
        object.__setattr__(job, "session_key", corrupt)
        inject_cron_result_to_dashboard(state, job, "queue is empty", history=None)
        # The run still lands in the job's own tab, and nothing is mirrored.
        assert len(_results(state.get_slot("cron-j1"))) == 1
        assert _results(state.get_slot(_SLOT)) == []
