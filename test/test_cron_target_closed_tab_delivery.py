"""Delivery to a target session whose TAB is closed but whose conversation is not.

Closing a dashboard tab archives it: the slot is popped while the transcript stays
on disk and the conversation reopens from that file. Mirroring into the live slot
therefore covers only the window in which the tab happens to be open -- which is
the opposite of what naming a target is usually for, since "remind me in this
thread later" implies the thread is not on screen when the job fires.

These pin the durable leg, and pin the two things it must refuse: it never creates
a transcript (so a deleted conversation stays deleted) and never writes into a
different conversation that merely reuses the slot name.
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
_TAB = "tab0123456789"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes to the process-global dashboard-surface registry.

    Reset it so keys from these states never leak into other tests -- the same
    fixture `test_cron_first_run_tab.py` and the live-mirror suite carry.
    """
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _job(**overrides):
    kwargs = {"id": "j1", "name": "nightly", "message": "summarize the queue"}
    kwargs.update(overrides)
    return CronJob(**kwargs)


def _persisted_results(state, key=_TARGET):
    """Assistant rows the TRANSCRIPT holds -- the only copy a closed tab has."""
    rows = state.conversation_log.read_messages(key)
    return [
        r.get("content", "") for r in rows if r.get("content", "").startswith("# Cron Job Result:")
    ]


@pytest.fixture
def closed_tab(tmp_path):
    """A conversation that exists on disk with its tab closed.

    Built the way the product does it: open the tab, log a turn under the tab's
    identity so the transcript carries it, then pop the slot exactly as the close
    handler does -- leaving the transcript behind.
    """
    st = _make_state(tmp_path)
    slot = st.get_or_create_slot(name=_SLOT)
    slot._tab_id = _TAB
    st.conversation_log.append(_TARGET, "user", "what shipped?", tab_id=_TAB)
    st._slots.pop(_SLOT, None)
    return st


class TestDeliveryIntoAClosedTab:
    def test_the_result_lands_in_the_archived_conversation(self, closed_tab):
        """The regression the destination-lifetime gap called for.

        Create a job targeting a session, close its tab, fire, then read the
        target's transcript. A delivery branch that gives up on the absent slot
        leaves the row reaching nobody while the job still reports that session
        as its destination -- advertised destination, no delivery.
        """
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        results = _persisted_results(closed_tab)
        assert len(results) == 1
        assert "queue is empty" in results[0]

    def test_the_job_still_gets_its_own_tab(self, closed_tab):
        """Still a mirror: the closed target does not take over the cron's tab."""
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        own = closed_tab.get_slot("cron-j1")
        assert own is not None
        assert [m for m in own.messages if m.get("content", "").startswith("# Cron Job Result:")]

    def test_only_the_result_row_is_persisted(self, closed_tab):
        """The prompt row would read as a turn the person typed."""
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        rows = closed_tab.conversation_log.read_messages(_TARGET)
        assert not [r for r in rows if r.get("content", "").startswith("# Cron Run:")]

    def test_a_refire_does_not_stack_a_second_copy(self, closed_tab):
        """`append_if_absent` holds the check and the write in one section."""
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        assert len(_persisted_results(closed_tab)) == 1


class TestWhatTheDurableLegRefuses:
    def test_a_deleted_conversation_is_not_resurrected(self, tmp_path):
        """No transcript, no write.

        Creating the file here would bring a conversation the user deleted back as
        a chat holding nothing but one orphaned cron result.
        """
        st = _make_state(tmp_path)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert not st.conversation_log.has_log(_TARGET)

    def test_a_session_recreated_under_the_same_name_does_not_inherit_it(self, tmp_path):
        """A slot name is reusable; a tab identity is not.

        The conversation that was named is gone and a different one occupies the
        name, so delivering here would drop a stranger's scheduled result into it.
        """
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = "tabaaaaaaaaaa"
        st.conversation_log.append(_TARGET, "user", "unrelated chat", tab_id="tabaaaaaaaaaa")
        st._slots.pop(_SLOT, None)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert _persisted_results(st) == []

    def test_the_job_keeps_reporting_its_own_result_when_delivery_is_refused(self, tmp_path):
        """A refused copy is not a failed run."""
        st = _make_state(tmp_path)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        own = st.get_slot("cron-j1")
        assert [m for m in own.messages if m.get("content", "").startswith("# Cron Job Result:")]


class TestASessionWithMemoryWritesDisabled:
    """Incognito/temporary targets keep nothing on disk, and this leg may not either.

    The live leg honours the mode by never persisting from the slot, so a durable
    row written here would be the one thing that outlives the conversation -- and
    the person who chose the mode is not the one who scheduled the job.
    """

    @pytest.mark.parametrize(
        "mode",
        [
            "incognito",
            "temporary",
            "INCOGNITO",
            # Whitespace-padded and mixed-case spellings a hand-edited or
            # partially written header can carry. The shared membership predicate
            # deliberately does not strip, and reads anything unrecognised as
            # NOT private, so testing the raw field here would treat these as
            # unrestricted and put a scheduled result on disk under a session
            # that keeps none. The mode has to be resolved through an allowlist.
            "incognito ",
            " temporary",
            "Incognito\t",
            # Unrecognised entirely -> unknown -> refuse, never "persistent".
            "not-a-mode",
        ],
    )
    def test_no_durable_row_is_written(self, tmp_path, mode):
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        st.conversation_log.append(_TARGET, "user", "private", tab_id=_TAB)
        st.conversation_log.update_metadata(_TARGET, {"memory_mode": mode})
        st._slots.pop(_SLOT, None)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert _persisted_results(st) == []

    def test_a_transcript_with_no_metadata_header_is_refused(self, tmp_path):
        """Fail closed when the mode cannot be established.

        `append` writes the metadata line at creation, before any message, so a
        first line that is not a metadata object did not come from a normal
        session and is no evidence that writes are allowed. Reading absence as
        "not incognito" would hand the write to exactly the transcripts whose
        privacy cannot be confirmed.
        """
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        st.conversation_log.append(_TARGET, "user", "real", tab_id=_TAB)
        st._slots.pop(_SLOT, None)
        path = st.conversation_log._path(_TARGET)
        rows = path.read_text(encoding="utf-8").splitlines()
        # Drop the header, leaving a file whose first line is message content.
        path.write_text("\n".join(rows[1:]) + "\n", encoding="utf-8")
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert _persisted_results(st) == []

    def test_a_damaged_header_is_refused(self, tmp_path):
        """A corrupt first line cannot establish the mode either."""
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        st.conversation_log.append(_TARGET, "user", "real", tab_id=_TAB)
        st._slots.pop(_SLOT, None)
        path = st.conversation_log._path(_TARGET)
        rows = path.read_text(encoding="utf-8").splitlines()
        path.write_text("{not json\n" + "\n".join(rows[1:]) + "\n", encoding="utf-8")
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert _persisted_results(st) == []

    def test_a_persistent_target_is_unaffected(self, tmp_path):
        """The guard must key on the mode, not merely on metadata being present."""
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        st.conversation_log.append(_TARGET, "user", "ordinary", tab_id=_TAB)
        st.conversation_log.update_metadata(_TARGET, {"memory_mode": "persistent"})
        st._slots.pop(_SLOT, None)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert len(_persisted_results(st)) == 1


class TestTheDeleteRace:
    """A session deleted after its header is read must not be recreated by the write.

    The existence probe and the header read both refuse a transcript that is gone,
    so the surviving window is narrower than it looks: it sits between reading the
    header and performing the append. A delete landing there leaves this path
    holding a header for a file that is already gone, and the append -- which
    creates the transcript when it is missing -- would bring the conversation back
    holding one orphaned cron result. Simulated by making the header read report a
    valid persistent session for a file that is not there, which is exactly the
    state that delete produces.
    """

    def _state_whose_header_read_lies(self, tmp_path):
        st = _make_state(tmp_path)
        log = st.conversation_log
        log.has_log = lambda key: True  # type: ignore[method-assign]
        log.get_metadata_status = lambda key: (  # type: ignore[method-assign]
            {"_type": "metadata", "memory_mode": "persistent", "tab_id": _TAB},
            True,
        )
        return st

    def test_the_conversation_is_not_recreated(self, tmp_path):
        st = self._state_whose_header_read_lies(tmp_path)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert list(tmp_path.rglob("dashboard_my-chat.jsonl")) == []

    def test_the_job_still_records_its_own_result(self, tmp_path):
        """Refusing the copy is not failing the run."""
        st = self._state_whose_header_read_lies(tmp_path)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        own = st.get_slot("cron-j1")
        assert [m for m in own.messages if m.get("content", "").startswith("# Cron Job Result:")]


class TestTargetsCarryingNoIdentity:
    def test_an_adopted_job_delivers_on_existence(self, closed_tab):
        """`cron adopt` addresses a session by key and nothing else.

        Requiring a stamp would silently drop delivery for every adopted job,
        which is adopt's own contract rather than something this leg may narrow.
        """
        job = _job(session_key=_TARGET)  # no session_tab_id
        inject_cron_result_to_dashboard(closed_tab, job, "queue is empty", history=None)
        assert len(_persisted_results(closed_tab)) == 1

    def test_a_transcript_predating_tab_identity_still_receives(self, tmp_path):
        """Unknown is "cannot disprove", not "mismatch".

        Refusing on an absent stamp would drop delivery for every conversation
        older than the field -- the failure this leg exists to fix.
        """
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        st.conversation_log.append(_TARGET, "user", "old conversation")  # no tab_id
        st._slots.pop(_SLOT, None)
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        assert len(_persisted_results(st)) == 1


class TestTheLiveTabIsUnchanged:
    def test_an_open_tab_still_takes_the_in_memory_mirror(self, tmp_path):
        """The durable leg is the CLOSED-tab case only.

        An open tab is persisted by the slot that owns the conversation, on its
        ordinary dirty-slot window, so writing from here as well would be a second
        writer for one row.
        """
        st = _make_state(tmp_path)
        slot = st.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        job = _job(session_key=_TARGET, session_tab_id=_TAB)
        inject_cron_result_to_dashboard(st, job, "queue is empty", history=None)
        live = [
            m.get("content", "")
            for m in st.get_slot(_SLOT).messages
            if m.get("content", "").startswith("# Cron Job Result:")
        ]
        assert len(live) == 1
