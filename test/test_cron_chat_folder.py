"""``chat_folder_id``: filing a scheduled job's RUNS into a chat folder.

A recurring job's output lands loose — in dashboard notifications, in a Slack
DM, or appended onto one long-lived tab nobody can find. ``chat_folder_id``
names a folder in the chat sidebar's own tree and a PERSISTENT job's single
``cron-{id}`` tab is filed there; its run stamps and markers already make that
tab the job's timeline. A stateless job has no job-wide tab to file, so the
store refuses the pair at save time.

What these tests hold, and why each one can fail:

* **The field is its own field.** ``folder_id`` already exists on ``CronJob`` and
  means something else entirely (it groups the job's ROW on the Schedule page).
  The two must never be read off each other, so the persistence and REST tests
  assert one moves without the other.
* **Filing SUPPLEMENTS delivery.** The feature must not become a fourth delivery
  mode that replaces notification/Slack/origin delivery, so the delivery-shape
  tests assert the result row is written and the caller's return value is
  unchanged whether or not a folder is configured.
* **A deleted folder costs the placement and nothing else.** A folder can be
  deleted while a job still names it. The run must still deliver, its session
  must land unfiled, and the skip must be recorded once.
* **A rename is a no-op.** The job stores the folder's id, so there is nothing to
  do — pinned so a future "resolve by name" shortcut cannot be added silently.
* **Back-compat.** A job with no ``chat_folder_id`` behaves exactly as it did:
  the mock state's ``_folders`` is empty in those tests, which would surface any
  accidental unconditional folder lookup as an exception rather than a pass.

The state fake mirrors ``test_cron_first_run_tab.py``'s, including the reason its
``get_slot``/``has_slot`` are REAL functions over one dict: on a bare
``MagicMock`` both auto-return truthy mocks, which would satisfy every
short-circuit under test and make the assertions unable to fail.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob, CronService
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.cron_inject import (
    chat_folder_exists,
    chat_folder_for_minted_tab,
    inject_cron_result_to_dashboard,
    move_cron_job_tab,
)
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons, api_crons_create
from kiro_crew.session_surface import set_dashboard_surfaced

FOLDER = "f00dcafe"
OTHER_FOLDER = "beadfeed"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes into the process-global dashboard-surface registry."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(folders=(FOLDER,), history_messages=None):
    """A mock DashboardState whose slot accessors are real functions."""
    state = MagicMock()
    slots: dict[str, MagicMock] = {}
    state._slots = slots
    state._folders = [{"id": fid, "name": f"folder-{fid}"} for fid in folders]

    def get_or_create_slot(name=None, agent="", origin=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""
            slot.folder_id = ""

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
    # A REAL function over the same dict: on a bare MagicMock this returns a mock,
    # and `mock >= 500` raises rather than answering, so the ceiling branch would
    # never be exercised either way.
    state.live_slot_count = lambda: len(slots)
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    # A REAL set: the filing registers its write task here to keep it from being
    # collected mid-flight, and a MagicMock would accept `.add` without holding a
    # reference -- so the assertion that the write happens could pass vacuously.
    state._background_tasks = set()
    return state


def _job(**over) -> CronJob:
    fields = {"id": "job42", "name": "standup brief", "message": "Write the brief."}
    fields.update(over)
    job = CronJob(**fields)
    job.set_run_result("the brief")
    return job


def _deliver(state, job, text, **kw):
    """Inject *text* as a run of *job* and return the job's tab."""
    inject_cron_result_to_dashboard(state, job, text, **kw)
    return state.get_slot(f"cron-{job.id}")


def _inject(state, job, text, **kw):
    """Run the injection ON A LOOP, as every production caller does, and let the
    folder placement land.

    The folder is decided at the mint, inside ``_bind_cron_slot``, and the
    placement -- assignment, write and broadcast -- is scheduled as a task, so a
    bare call with no running loop is not a shape production ever takes. The write
    itself is answered ``True`` here; the tests about the write's own verdict patch
    it themselves.
    """
    import kiro_crew.dashboard.chat_persistence as persistence

    async def _go():
        original = persistence.save_slot_off_loop

        async def _save(state, slot, *a, **k):
            return True

        persistence.save_slot_off_loop = _save
        try:
            slot = _deliver(state, job, text, **kw)
            if state._background_tasks:
                await asyncio.gather(*list(state._background_tasks))
        finally:
            persistence.save_slot_off_loop = original
        return slot

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# 1. Persistence: the field survives a save/load round trip on its own
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_the_field_round_trips_through_the_store(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        reloaded = CronService(base_dir=tmp_path)
        found = next(j for j in reloaded.list_jobs(include_disabled=True) if j.id == job.id)
        assert found.chat_folder_id == FOLDER

    def test_it_is_written_to_disk_under_its_own_key(self, tmp_path) -> None:
        """Not folded into ``folder_id``: the two name folders in different trees."""
        svc = CronService(base_dir=tmp_path)
        svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        record = json.loads((tmp_path / "crons.json").read_text())["jobs"][0]
        assert record["chat_folder_id"] == FOLDER
        assert record["folder_id"] == ""

    def test_an_absent_key_reads_as_not_filed(self, tmp_path) -> None:
        """A store written before the field existed keeps working."""
        (tmp_path / "crons.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "legacy01",
                            "name": "old",
                            "message": "go",
                            "schedule": {"kind": "every", "every_secs": 60},
                        }
                    ]
                }
            )
        )
        svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs(include_disabled=True)[0].chat_folder_id == ""

    def test_update_moves_it_without_touching_the_schedule_folder(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            "brief", "Write it.", every_secs=3600, folder_id="sched1", chat_folder_id=FOLDER
        )
        svc.update_job(job.id, chat_folder_id=OTHER_FOLDER)

        moved = next(j for j in svc.list_jobs(include_disabled=True) if j.id == job.id)
        assert moved.chat_folder_id == OTHER_FOLDER
        assert moved.folder_id == "sched1"

    def test_update_clears_it_with_an_empty_value(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        svc.update_job(job.id, chat_folder_id="")
        cleared = next(j for j in svc.list_jobs(include_disabled=True) if j.id == job.id)
        assert cleared.chat_folder_id == ""

    def test_the_store_reports_the_folder_it_replaced(self, tmp_path) -> None:
        """The dashboard moves the job's tab out of the OLD folder, so it needs the
        old value read atomically with the write that replaces it -- a separate query
        is a read-then-write race two concurrent folder edits both lose."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        sink: dict[str, str] = {}
        moved = svc.update_job(job.id, chat_folder_id=OTHER_FOLDER, chat_folder_transition_out=sink)
        assert moved is not None and moved.chat_folder_id == OTHER_FOLDER
        assert sink == {"chat_folder_was": FOLDER}

    def test_clearing_reports_the_folder_it_replaced(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink: dict[str, str] = {}
        svc.update_job(job.id, chat_folder_id="", chat_folder_transition_out=sink)
        assert sink == {"chat_folder_was": FOLDER}

    def test_an_unchanged_folder_reports_nothing(self, tmp_path) -> None:
        """The form submits the field on every save, so "unchanged" must be
        distinguishable from "cleared" or an unrelated edit moves a tab."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink: dict[str, str] = {}
        svc.update_job(job.id, chat_folder_id=FOLDER, chat_folder_transition_out=sink)
        assert sink == {}

    def test_an_edit_that_never_names_the_field_reports_nothing(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink: dict[str, str] = {}
        svc.update_job(job.id, name="renamed", chat_folder_transition_out=sink)
        assert sink == {}

    def test_filing_an_unfiled_job_reports_the_empty_prior_value(self, tmp_path) -> None:
        """Unfiled -> folder is a transition too: the job's existing tab has to move
        INTO the folder at the save, because delivery files only a tab it has just
        minted. Presence of the key is the signal; its value is the empty prior."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600)
        sink: dict[str, str] = {}
        svc.update_job(job.id, chat_folder_id=FOLDER, chat_folder_transition_out=sink)
        assert sink == {"chat_folder_was": ""}

    def test_two_callers_cannot_see_each_other_s_transition(self, tmp_path) -> None:
        """The whole reason the report is an out-parameter instead of a field on the
        job: ``self._jobs`` holds ONE object per job, so a field would be shared
        state and each caller's answer would be visible to -- and clobberable by --
        the others. Each sink belongs to exactly one call."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)

        first: dict[str, str] = {}
        svc.update_job(job.id, chat_folder_id=OTHER_FOLDER, chat_folder_transition_out=first)
        # A later, unrelated update cannot reach into the earlier caller's answer.
        second: dict[str, str] = {}
        svc.update_job(job.id, name="renamed", chat_folder_transition_out=second)

        assert first == {"chat_folder_was": FOLDER}
        assert second == {}

    def test_the_sink_is_optional(self, tmp_path) -> None:
        """Every other caller of update_job passes none, and must be unaffected."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        moved = svc.update_job(job.id, chat_folder_id=OTHER_FOLDER)
        assert moved is not None and moved.chat_folder_id == OTHER_FOLDER

    def test_the_transition_is_never_written_to_disk(self, tmp_path) -> None:
        """It is a report about one call, not a field of the job."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink: dict[str, str] = {}
        svc.update_job(job.id, chat_folder_id=OTHER_FOLDER, chat_folder_transition_out=sink)

        record = json.loads((tmp_path / "crons.json").read_text())["jobs"][0]
        assert "chat_folder_was" not in record
        assert "chat_folder_transition_out" not in record

    def test_the_store_refuses_a_folder_on_a_stateless_job(self, tmp_path) -> None:
        """A stateless job has no job-wide tab to file, so the pair is a setting
        with no observable effect; refusing it at save time is where a person is
        present to be told."""
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="persistent session"):
            svc.add_job(
                "brief",
                "Write it.",
                every_secs=3600,
                persistent_session=False,
                chat_folder_id=FOLDER,
            )
        job = svc.add_job("brief", "Write it.", every_secs=3600, persistent_session=False)
        with pytest.raises(ValueError, match="persistent session"):
            svc.update_job(job.id, chat_folder_id=FOLDER)
        assert job.chat_folder_id == ""

    def test_filing_and_un_persisting_in_one_update_is_refused(self, tmp_path) -> None:
        """The request itself names a folder for a job it leaves stateless."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600)
        with pytest.raises(ValueError, match="persistent session"):
            svc.update_job(job.id, persistent_session=False, chat_folder_id=FOLDER)
        assert job.persistent_session is True and job.chat_folder_id == ""

    def test_clearing_persistence_clears_the_folder_and_reports_it(self, tmp_path) -> None:
        """MCP/CLI ``cron_update`` exposes ``persistent_session`` but not
        ``chat_folder_id``, so a refusal here would be a dead end for that caller.
        The folder goes with the tab it filed, and the sink reports the clear exactly
        as an explicit one, so the dashboard moves the tab through the same path."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink: dict[str, str] = {}
        left = svc.update_job(job.id, persistent_session=False, chat_folder_transition_out=sink)
        assert left is not None
        assert left.persistent_session is False and left.chat_folder_id == ""
        assert sink == {"chat_folder_was": FOLDER}
        # Clearing both in one update lands in the same place.
        other = svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=FOLDER)
        sink = {}
        svc.update_job(
            other.id, persistent_session=False, chat_folder_id="", chat_folder_transition_out=sink
        )
        assert other.persistent_session is False and other.chat_folder_id == ""
        assert sink == {"chat_folder_was": FOLDER}

    def test_un_persisting_an_unfiled_job_reports_nothing(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600)
        sink: dict[str, str] = {}
        svc.update_job(job.id, persistent_session=False, chat_folder_transition_out=sink)
        assert job.chat_folder_id == "" and sink == {}

    def test_the_guard_reads_the_flag_the_way_the_store_writes_it(self, tmp_path) -> None:
        """A PATCH carrying the flag as a string is coerced at assignment, so the
        cross-field guard reads it through the same coercion: the two cannot disagree
        about whether the job this update leaves behind is persistent."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("brief", "Write it.", every_secs=3600)
        svc.update_job(job.id, persistent_session="false", chat_folder_id=FOLDER)

        left = next(j for j in svc.list_jobs(include_disabled=True) if j.id == job.id)
        assert left.persistent_session is True
        assert left.chat_folder_id == FOLDER

    def test_the_store_refuses_a_non_string(self, tmp_path) -> None:
        """The table-driven chokepoint gate covers the new field like its siblings."""
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError):
            svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id=["nope"])

    def test_the_store_refuses_an_over_cap_string(self, tmp_path) -> None:
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError):
            svc.add_job("brief", "Write it.", every_secs=3600, chat_folder_id="x" * 5000)


# ---------------------------------------------------------------------------
# 2. Folder existence
# ---------------------------------------------------------------------------


class TestFolderExistence:
    def test_a_known_id_exists(self) -> None:
        assert chat_folder_exists(_make_state(), FOLDER) is True

    def test_an_unknown_id_does_not(self) -> None:
        assert chat_folder_exists(_make_state(), OTHER_FOLDER) is False

    def test_an_empty_id_is_never_a_folder(self) -> None:
        assert chat_folder_exists(_make_state(), "") is False


# ---------------------------------------------------------------------------
# 3. Filing: the job-wide tab at its mint, and the deleted folder
# ---------------------------------------------------------------------------


class TestFilingAtTheMint:
    """A tab is filed by the call that MINTS it, and by nothing later.

    "Unfiled" is deliberately not the test. A reader who drags the job-wide tab
    out to the root leaves ``slot.folder_id == ""`` -- indistinguishable from a
    tab that was never filed -- so a rule that filed unfiled tabs would drag
    theirs back on every delivery. A freshly minted tab has no placement history,
    so filing it cannot undo anyone's.
    """

    def test_the_job_wide_tab_is_filed_when_it_is_minted(self) -> None:
        state = _make_state()
        slot = _inject(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot.folder_id == FOLDER

    def test_a_job_with_no_folder_leaves_the_tab_unfiled(self) -> None:
        state = _make_state(folders=())
        slot = _inject(state, _job(), "the brief", history=[])
        assert slot.folder_id == ""

    def test_a_tab_the_reader_dragged_to_the_root_is_never_dragged_back(self) -> None:
        """The exact failure the previous rule had: root-drag leaves the tab unfiled,
        and the next delivery re-asserted the job's folder."""
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = _inject(state, job, "run one", history=[])
        assert slot.folder_id == FOLDER

        slot.folder_id = ""  # the reader drags it out to the root
        again = _inject(state, job, "run two", history=[])
        assert again is slot
        assert slot.folder_id == ""
        assert any("run two" in m["content"] for m in slot.messages)

    def test_a_tab_the_reader_filed_elsewhere_is_never_dragged_back(self) -> None:
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        job = _job(chat_folder_id=FOLDER)
        slot = _inject(state, job, "run one", history=[])
        slot.folder_id = OTHER_FOLDER

        _inject(state, job, "run two", history=[])
        assert slot.folder_id == OTHER_FOLDER

    def test_changing_the_job_folder_does_not_re_point_at_delivery(self) -> None:
        """That transition is applied once, at the save, where the prior folder is
        known -- not re-asserted on every run."""
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = _inject(state, _job(chat_folder_id=FOLDER), "run one", history=[])
        assert slot.folder_id == FOLDER

        _inject(state, _job(chat_folder_id=OTHER_FOLDER), "run two", history=[])
        assert slot.folder_id == FOLDER

    def test_an_existing_unfiled_tab_is_not_filed_by_delivery(self) -> None:
        """Filing a job that already has its tab is the save's job
        (``move_cron_job_tab`` with an empty prior), not delivery's."""
        state = _make_state()
        state.get_or_create_slot(name="cron-job42")  # existed before the folder was set
        slot = _inject(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot.folder_id == ""

    def test_a_rename_needs_no_work_because_the_id_is_stored(self) -> None:
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        slot = _inject(state, job, "run one", history=[])
        state._folders[0]["name"] = "Renamed entirely"
        _inject(state, job, "run two", history=[])
        assert slot.folder_id == FOLDER

    def test_the_decision_assigns_nothing(self) -> None:
        """The folder is a VALUE the placement commit holds against its write; the
        decision itself leaves the tab exactly as it found it."""
        state = _make_state()
        tab = state.get_or_create_slot(name="cron-job42")
        assert chat_folder_for_minted_tab(state, _job(chat_folder_id=FOLDER)) == FOLDER
        assert chat_folder_for_minted_tab(state, _job()) == ""
        assert tab.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_pre_created_tab_is_filed_by_the_pre_create(self, monkeypatch) -> None:
        """For a persistent job the mint is usually the run-START pre-create, so the
        filing has to ride on that call -- and delivery then finds the tab in place
        and writes nothing about its placement."""
        import kiro_crew.dashboard.chat_persistence as persistence
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        job = _job(chat_folder_id=FOLDER)
        await ensure_cron_slot(state, job)
        await asyncio.gather(*list(state._background_tasks))
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == FOLDER
        assert saved == ["cron-job42"]

        delivered = _deliver(state, job, "the brief", history=[])
        await asyncio.gather(*list(state._background_tasks))
        assert delivered is slot and slot.folder_id == FOLDER
        assert saved == ["cron-job42"]


class TestDeletedFolder:
    def test_the_run_still_delivers_and_lands_unfiled(self) -> None:
        state = _make_state(folders=())  # the folder was deleted after the save
        slot = _inject(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot.folder_id == ""
        # The result is still there — the placement is the only thing lost.
        assert any("the brief" in m["content"] for m in slot.messages)

    def test_the_skip_is_recorded_once(self, monkeypatch) -> None:
        """ "Why is this run not in my folder?" must have an answer with no repro."""
        import kiro_crew.dashboard.cron_inject as mod

        logged: list[dict] = []
        monkeypatch.setattr(
            mod,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: logged.append(kw)),
        )
        state = _make_state(folders=())
        _inject(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert [row["tool_name"] for row in logged] == ["cron_chat_folder_missing"]
        assert logged[0]["outcome"] == "skipped"

    def test_a_sel_failure_cannot_fail_the_run(self, monkeypatch) -> None:
        import kiro_crew.dashboard.cron_inject as mod

        def _boom():
            raise RuntimeError("no audit today")

        monkeypatch.setattr(mod, "sel", _boom)
        state = _make_state(folders=())
        slot = _inject(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot.folder_id == ""
        assert any("the brief" in m["content"] for m in slot.messages)


# ---------------------------------------------------------------------------
# 4. Supplements, never replaces
# ---------------------------------------------------------------------------


class TestTheFilingIsSynchronous:
    """The placement must not put a suspension point between a run's result and
    its notification: a cancellation landing there escapes `except Exception`, so
    a torn-down run would lose the bell and the Slack post it would otherwise
    still have sent. So the whole placement -- the assignment, the metadata write
    and the broadcast -- rides on a background task, and the delivery returns
    without waiting for it."""

    @pytest.mark.asyncio
    async def test_the_delivery_does_not_wait_for_the_filing(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        started = asyncio.Event()
        release = asyncio.Event()

        async def _save(state, slot, *a, **kw):
            started.set()
            await release.wait()
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        # The write is deliberately parked, so a delivery that AWAITED the
        # placement could not return at all -- which is what makes this meaningful.
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        # The tab is unfiled while the write is in flight, so nothing this call
        # published claims a placement the write has not confirmed.
        assert slot.folder_id == ""
        release.set()
        await asyncio.gather(*list(state._background_tasks))
        assert slot.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_the_synchronous_publish_carries_no_provisional_folder(self, monkeypatch) -> None:
        """The delivery broadcasts the slot table before the write can commit, so a
        folder assigned on that path would advertise a placement that is not on
        disk. The assignment belongs to the commit, whose own broadcast carries the
        verdict."""
        import kiro_crew.dashboard.chat_persistence as persistence

        release = asyncio.Event()

        async def _save(state, slot, *a, **kw):
            await release.wait()
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        assert state.push_slots_update.call_count == 1  # the injection's own push
        assert slot.folder_id == ""  # what that push carried

        release.set()
        await asyncio.gather(*list(state._background_tasks))
        assert state.push_slots_update.call_count == 2
        assert slot.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_the_write_is_pinned_to_the_slot_it_authorized(self, monkeypatch) -> None:
        """A forced metadata write is open-shaped, so an unpinned save landing after
        the reader closed and archived the tab would erase its ``closed`` metadata
        and the tab resurfaces at the next restart."""
        import kiro_crew.dashboard.chat_persistence as persistence

        seen: list[dict] = []

        async def _save(state, slot, *a, **kw):
            seen.append(kw)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        await asyncio.gather(*list(state._background_tasks))

        assert len(seen) == 1
        assert seen[0]["expected_slot_name"] == "cron-job42"
        assert seen[0]["expected_history_key"] == slot_history_key(slot)

    @pytest.mark.asyncio
    async def test_the_write_is_tracked_so_it_cannot_be_collected(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert len(state._background_tasks) == 1
        await asyncio.gather(*list(state._background_tasks))
        assert saved == ["cron-job42"]
        # The done callback discards it, so the set does not grow per run.
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_the_placement_is_broadcast_only_after_it_is_on_disk(self, monkeypatch) -> None:
        """Telling an open sidebar the tab is filed before the metadata line lands
        advertises a placement a failed write would erase, with nothing saying so."""
        import kiro_crew.dashboard.chat_persistence as persistence

        release = asyncio.Event()

        async def _save(state, slot, *a, **kw):
            await release.wait()
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert state.push_slots_update.call_count == 1  # the injection's own push
        release.set()
        await asyncio.gather(*list(state._background_tasks))
        assert state.push_slots_update.call_count == 2

    @pytest.mark.asyncio
    async def test_a_failed_write_retracts_the_placement(self, monkeypatch) -> None:
        """So memory and disk agree on "not filed" rather than disagreeing."""
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        await asyncio.gather(*list(state._background_tasks))
        assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_refused_write_retracts_too(self, monkeypatch) -> None:
        """`save_slot_off_loop` can decline WITHOUT raising -- it returns False when
        the slot's routing does not resolve to the authorized transcript key. The
        absence of an exception is therefore not confirmation, and treating it as one
        published placements that were never written."""
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            return False

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        await asyncio.gather(*list(state._background_tasks))
        assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_write_asks_for_confirmation(self, monkeypatch) -> None:
        """`best_effort=False` is the documented way to get a verdict: the save still
        runs off-loop, but a real failure propagates instead of being swallowed
        behind a retry flag this caller cannot observe."""
        import kiro_crew.dashboard.chat_persistence as persistence

        seen: list[dict] = []

        async def _save(state, slot, *a, **kw):
            seen.append(kw)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        await asyncio.gather(*list(state._background_tasks))
        assert seen and seen[0].get("best_effort") is False
        assert seen[0].get("force") is True

    @pytest.mark.asyncio
    async def test_a_retraction_never_moves_a_tab_the_reader_moved(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        started = asyncio.Event()
        release = asyncio.Event()

        async def _save(state, slot, *a, **kw):
            started.set()
            await release.wait()
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        await started.wait()
        slot.folder_id = OTHER_FOLDER  # the reader drags it while the write is parked
        release.set()
        await asyncio.gather(*list(state._background_tasks))
        assert slot.folder_id == OTHER_FOLDER

    @pytest.mark.asyncio
    async def test_a_failed_write_cannot_fail_the_run(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        assert slot is not None
        await asyncio.gather(*list(state._background_tasks))
        assert any("the brief" in m["content"] for m in slot.messages)

    @pytest.mark.asyncio
    async def test_the_placement_holds_the_slot_metadata_lock(self, monkeypatch) -> None:
        """The whole span -- assignment, write, rollback, broadcast -- runs under the
        state-wide slot-metadata transaction lock, the one the sidebar's folder
        endpoint takes, so the two writers take turns: a manual move landing in this
        window is not overwritten by a save that started before it."""
        import kiro_crew.dashboard.chat_persistence as persistence
        from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock

        save = AsyncMock(return_value=True)
        monkeypatch.setattr(persistence, "save_slot_off_loop", save)
        state = _make_state()
        async with _slot_meta_txn_lock(state):
            slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
            assert slot is not None
            for _ in range(5):
                await asyncio.sleep(0)
            # Held by this test, so the placement cannot have assigned or written.
            save.assert_not_awaited()
            assert slot.folder_id == ""
        await asyncio.gather(*list(state._background_tasks))
        save.assert_awaited_once()
        assert slot.folder_id == FOLDER


class TestSupplementsDelivery:
    @pytest.mark.asyncio
    async def test_the_result_row_is_written_whether_or_not_a_folder_is_set(
        self, monkeypatch
    ) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        monkeypatch.setattr(persistence, "save_slot_off_loop", AsyncMock(return_value=True))
        filed_state, plain_state = _make_state(), _make_state(folders=())

        filed = _deliver(filed_state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        plain = _deliver(plain_state, _job(), "the brief", history=[])
        await asyncio.gather(*list(filed_state._background_tasks))

        assert [m["role"] for m in filed.messages] == [m["role"] for m in plain.messages]
        assert filed.folder_id == FOLDER
        assert plain.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_placement_is_persisted_so_the_folder_survives_a_restart(
        self, monkeypatch
    ) -> None:
        """The folder's timeline is read from the on-disk session list, so an
        in-memory-only assignment shows nothing after a restart."""
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[tuple[str, bool]] = []

        async def _save(state, slot, *a, **kw):
            saved.append((slot.key, bool(kw.get("force"))))
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        inject_cron_result_to_dashboard(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        await asyncio.gather(*list(state._background_tasks))
        assert saved == [("cron-job42", True)]

    @pytest.mark.asyncio
    async def test_no_metadata_write_when_nothing_moved(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state(folders=())
        inject_cron_result_to_dashboard(state, _job(), "the brief", history=[])
        await asyncio.gather(*list(state._background_tasks))
        assert saved == []

    @pytest.mark.asyncio
    async def test_a_failed_persist_cannot_fail_a_completed_run(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(chat_folder_id=FOLDER), "the brief", history=[])
        await asyncio.gather(*list(state._background_tasks))
        assert any("the brief" in m["content"] for m in slot.messages)


class TestMovingTheTabAtTheSave:
    """Every change of the field -- clearing it, switching folders, filing an
    unfiled job -- moves the job's EXISTING tab at the save, but only on a real
    transition, and only when the tab is still where the setting last left it.

    The second condition is the safety property: a reader who dragged the tab
    somewhere else has said where they want it, and their placement outranks a
    setting they are editing."""

    @staticmethod
    async def _filed_tab(state, job):
        """Mint the job's tab on the running loop and let its folder write land."""
        import kiro_crew.dashboard.chat_persistence as persistence

        original = persistence.save_slot_off_loop
        persistence.save_slot_off_loop = AsyncMock(return_value=True)
        try:
            slot = _deliver(state, job, "brief", history=[])
            await asyncio.gather(*list(state._background_tasks))
        finally:
            persistence.save_slot_off_loop = original
        return slot

    @pytest.mark.asyncio
    async def test_filing_an_unfiled_job_moves_its_existing_tab(self, monkeypatch) -> None:
        """Delivery files only a tab it has just minted, so a job that already has
        its tab would otherwise show nothing until that tab was recreated."""
        import kiro_crew.dashboard.chat_persistence as persistence

        monkeypatch.setattr(persistence, "save_slot_off_loop", AsyncMock(return_value=True))
        state = _make_state()
        slot = await self._filed_tab(state, _job())
        assert slot.folder_id == ""

        await move_cron_job_tab(state, _job(chat_folder_id=FOLDER), "", FOLDER)
        assert slot.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_filing_leaves_a_tab_the_reader_already_placed(self) -> None:
        """Prior "" is checked against the tab like any other prior: a tab the
        reader dragged into some folder is not sitting where the setting left it."""
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job())
        slot.folder_id = OTHER_FOLDER

        await move_cron_job_tab(state, _job(chat_folder_id=FOLDER), "", FOLDER)
        assert slot.folder_id == OTHER_FOLDER

    @pytest.mark.asyncio
    async def test_the_job_wide_tab_is_unfiled(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[tuple[str, bool]] = []

        async def _save(state, slot, *a, **kw):
            saved.append((slot.key, bool(kw.get("force"))))
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        assert slot.folder_id == FOLDER

        await move_cron_job_tab(state, _job(), FOLDER, "")
        assert slot.folder_id == ""
        assert saved == [("cron-job42", True)]

    @pytest.mark.asyncio
    async def test_the_move_is_pinned_to_the_slot_it_authorized(self, monkeypatch) -> None:
        """The move takes the same forced, open-shaped write as the filing, so it
        carries the same two pins: a tab closed and archived while the write is in
        flight refuses it rather than having its ``closed`` metadata erased."""
        import kiro_crew.dashboard.chat_persistence as persistence

        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))

        seen: list[dict] = []

        async def _save(state, slot, *a, **kw):
            seen.append(kw)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        await move_cron_job_tab(state, _job(chat_folder_id=OTHER_FOLDER), FOLDER, OTHER_FOLDER)

        assert slot.folder_id == OTHER_FOLDER
        assert len(seen) == 1
        assert seen[0]["expected_slot_name"] == "cron-job42"
        assert seen[0]["expected_history_key"] == slot_history_key(slot)

    @pytest.mark.asyncio
    async def test_no_transition_means_nothing_to_do(self) -> None:
        """Same prior and next is not a change, whatever the tab is doing."""
        state = _make_state()
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        slot.folder_id = ""
        await move_cron_job_tab(state, _job(), "", "")
        assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_tab_the_reader_moved_themselves_is_left_alone(self) -> None:
        """The reader dragged it to another folder. Their placement outranks the
        setting being cleared -- unfiling here would lose work they did by hand."""
        state = _make_state()
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        slot.folder_id = OTHER_FOLDER

        await move_cron_job_tab(state, _job(), FOLDER, "")
        assert slot.folder_id == OTHER_FOLDER

    @pytest.mark.asyncio
    async def test_an_unfiled_tab_needs_no_write(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        saved: list[str] = []

        async def _save(state, slot, *a, **kw):
            saved.append(slot.key)
            return True

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = _deliver(state, _job(), "brief", history=[])
        slot.folder_id = OTHER_FOLDER  # not where the setting left it
        await move_cron_job_tab(state, _job(), FOLDER, "")
        assert saved == []

    @pytest.mark.asyncio
    async def test_a_job_with_no_tab_at_all_is_a_no_op(self) -> None:
        await move_cron_job_tab(_make_state(), _job(), FOLDER, "")

    @pytest.mark.asyncio
    async def test_a_refused_write_restores_the_prior_folder(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        monkeypatch.setattr(persistence, "save_slot_off_loop", AsyncMock(return_value=False))
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        state.push_slots_update.reset_mock()

        await move_cron_job_tab(state, _job(chat_folder_id=OTHER_FOLDER), FOLDER, OTHER_FOLDER)
        assert slot.folder_id == FOLDER
        state.push_slots_update.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_raising_write_restores_the_prior_folder(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        monkeypatch.setattr(
            persistence, "save_slot_off_loop", AsyncMock(side_effect=OSError("disk full"))
        )
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        state.push_slots_update.reset_mock()

        await move_cron_job_tab(state, _job(chat_folder_id=OTHER_FOLDER), FOLDER, OTHER_FOLDER)
        assert slot.folder_id == FOLDER
        state.push_slots_update.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_retraction_never_moves_a_tab_the_reader_moved(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        started = asyncio.Event()
        release = asyncio.Event()

        async def _save(state, slot, *a, **kw):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        state.push_slots_update.reset_mock()
        move = asyncio.create_task(move_cron_job_tab(state, _job(), FOLDER, ""))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert slot.folder_id == ""
            state.push_slots_update.assert_not_called()
            slot.folder_id = OTHER_FOLDER
        finally:
            release.set()
            await asyncio.wait_for(move, timeout=5)
        assert slot.folder_id == OTHER_FOLDER
        state.push_slots_update.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_move_asks_for_a_confirmed_write(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        save = AsyncMock(return_value=True)
        monkeypatch.setattr(persistence, "save_slot_off_loop", save)
        state = _make_state()
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))

        await move_cron_job_tab(state, _job(), FOLDER, "")
        save.assert_awaited_once_with(
            state,
            slot,
            force=True,
            best_effort=False,
            expected_history_key=slot_history_key(slot),
            expected_slot_name="cron-job42",
        )
        assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_failed_persist_cannot_fail_the_save(self, monkeypatch) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence

        async def _save(state, slot, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(persistence, "save_slot_off_loop", _save)
        state = _make_state()
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))
        await move_cron_job_tab(state, _job(), FOLDER, "")
        assert slot.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_the_move_holds_the_slot_metadata_lock(self, monkeypatch) -> None:
        """The move takes the same state-wide slot-metadata transaction lock the
        filing and the sidebar's folder endpoint take, so a save's move and a manual
        one take turns rather than rolling each other back."""
        import kiro_crew.dashboard.chat_persistence as persistence
        from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock

        state = _make_state(folders=(FOLDER, OTHER_FOLDER))
        slot = await self._filed_tab(state, _job(chat_folder_id=FOLDER))

        save = AsyncMock(return_value=True)
        monkeypatch.setattr(persistence, "save_slot_off_loop", save)
        async with _slot_meta_txn_lock(state):
            move = asyncio.create_task(
                move_cron_job_tab(state, _job(chat_folder_id=OTHER_FOLDER), FOLDER, OTHER_FOLDER)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            save.assert_not_awaited()
            assert slot.folder_id == FOLDER
        await asyncio.wait_for(move, timeout=5)
        save.assert_awaited_once()
        assert slot.folder_id == OTHER_FOLDER


# ---------------------------------------------------------------------------
# 5. The REST surface
# ---------------------------------------------------------------------------


def _app(handler, route: str, *, folders=(FOLDER,), **store) -> web.Application:
    app = web.Application()
    # The clear path reads the job's STORED folder before the update, to tell a real
    # transition from the empty value every unrelated submit also carries. Default to
    # an unfiled job so a test that is not about clearing needs no stub, and a REAL
    # AsyncMock rather than a bare attribute: an auto-mock would return a coroutine
    # nobody awaits and the prior value would read as a mock.
    store.setdefault("get_job_async", AsyncMock(return_value=_job()))
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        _folders=[{"id": fid, "name": f"folder-{fid}"} for fid in folders],
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        # The clear path asks for the job's own tab; no tab is the ordinary case
        # for a handler test, and a REAL function keeps it from answering a mock.
        get_slot=lambda name: None,
        push_slots_update=MagicMock(),
    )
    app.router.add_route("*", route, handler)
    return app


_BODY = {"name": "brief", "message": "Write it.", "every": 3600}


@pytest.mark.asyncio
class TestRestCreate:
    async def test_the_field_reaches_the_store(self) -> None:
        add = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": FOLDER})
            assert resp.status == 200
        assert add.await_args.kwargs["chat_folder_id"] == FOLDER

    async def test_the_store_s_refusal_of_a_stateless_pair_reaches_the_client(self) -> None:
        """The message is shown verbatim under Save, so it has to name the rule."""
        from kiro_crew.cron import _CHAT_FOLDER_NEEDS_PERSISTENT

        add = AsyncMock(side_effect=ValueError(_CHAT_FOLDER_NEEDS_PERSISTENT))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons", json={**_BODY, "persistent_session": False, "chat_folder_id": FOLDER}
            )
            assert resp.status == 400
            assert "persistent session" in (await resp.json())["error"]

    async def test_an_absent_field_creates_an_unfiled_job(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/crons", json=_BODY)).status == 200
        assert add.await_args.kwargs["chat_folder_id"] == ""

    async def test_an_unknown_folder_is_refused_at_save_time(self) -> None:
        """A save is the one moment a person is present to be told."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": "nope1234"})
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "unknown_chat_folder"
            # The form shows this text verbatim, so it has to carry the next step.
            assert "pick another" in body["error"]
        add.assert_not_awaited()

    async def test_a_non_string_is_refused(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": 7})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_chat_folder_id"

    async def test_null_means_unfiled_rather_than_a_refusal(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons", json={**_BODY, "chat_folder_id": None})
            assert resp.status == 200
        assert add.await_args.kwargs["chat_folder_id"] == ""

    async def test_the_schedule_folder_is_unaffected(self) -> None:
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/crons", json={**_BODY, "chat_folder_id": FOLDER, "folder_id": "sched9"}
            )
        assert add.await_args.kwargs["folder_id"] == "sched9"
        assert add.await_args.kwargs["chat_folder_id"] == FOLDER


@pytest.mark.asyncio
class TestRestUpdate:
    async def test_the_field_reaches_the_store(self) -> None:
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": FOLDER})
            assert resp.status == 200
        assert update.await_args.kwargs["chat_folder_id"] == FOLDER

    async def test_an_unknown_folder_is_refused(self) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": "nope1234"})
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "unknown_chat_folder"
            # The form shows this text verbatim, so it has to carry the next step.
            assert "pick another" in body["error"]
        update.assert_not_awaited()

    async def test_null_clears_the_field(self) -> None:
        update = AsyncMock(return_value=_job())
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": None})
            assert resp.status == 200
        assert update.await_args.kwargs["chat_folder_id"] == ""

    @staticmethod
    def _capture_unfile(monkeypatch):
        """Record (job id, prior folder) for each unfile the handler performs."""
        import kiro_crew.dashboard.handlers.cron as handler_mod

        calls: list[tuple[str, str]] = []

        async def _move(state, job, previous_folder_id, new_folder_id):
            calls.append((job.id, previous_folder_id, new_folder_id))

        monkeypatch.setattr(handler_mod, "move_cron_job_tab", _move)
        return calls

    @staticmethod
    def _update_reporting(was: str | None, committed):
        """An `update_job_async` that fills the caller's sink, as the store does:
        the prior value -- "" included -- on a real change, nothing otherwise."""

        async def _update(job_id, **kwargs):
            sink = kwargs.get("chat_folder_transition_out")
            if sink is not None and was is not None:
                sink["chat_folder_was"] = was
            return committed

        return AsyncMock(side_effect=_update)

    async def test_clearing_a_filed_job_undoes_the_placement(self, monkeypatch) -> None:
        """The transition arrives in a sink this request owns, filled inside the
        store's lock -- not from a query of the handler's own (a read-then-write
        race) and not from a field on the shared job object (clobberable by any
        concurrent update)."""
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting(FOLDER, _job()),
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"chat_folder_id": ""})
            assert resp.status == 200
        # The prior folder rides along so the move can check it against the tab.
        assert calls == [("job42", FOLDER, "")]

    async def test_un_persisting_a_filed_job_undoes_the_placement(self, monkeypatch) -> None:
        """The store clears the folder when persistence is turned off, so a request
        that names only the flag still gets a sink and the tab still moves."""
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting(FOLDER, _job(persistent_session=False)),
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/crons/job42", json={"persistent_session": False})
            assert resp.status == 200
        assert calls == [("job42", FOLDER, "")]

    async def test_the_handler_makes_no_extra_read_of_its_own(self, monkeypatch) -> None:
        """The store reports the transition, so a second query would only re-open the
        race the sink exists to close."""
        self._capture_unfile(monkeypatch)
        get_job = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting(FOLDER, _job()),
            get_job_async=get_job,
        )
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": ""})
        get_job.assert_not_awaited()

    async def test_the_handler_passes_a_sink_of_its_own(self, monkeypatch) -> None:
        """One sink per request is the property that makes concurrent folder edits
        safe, so it is asserted rather than assumed."""
        self._capture_unfile(monkeypatch)
        seen: list[object] = []

        async def _update(job_id, **kwargs):
            seen.append(kwargs.get("chat_folder_transition_out"))
            return _job()

        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=AsyncMock(side_effect=_update),
        )
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": ""})
            await client.patch("/api/crons/job42", json={"chat_folder_id": ""})
        assert len(seen) == 2 and all(isinstance(x, dict) for x in seen)
        assert seen[0] is not seen[1]

    async def test_an_unrelated_edit_never_unfiles(self, monkeypatch) -> None:
        """The form sends the field on EVERY submit, so an empty value in the
        request is true of a rename too and is evidence of nothing. Gating on the
        request alone unfiled a tab on any edit -- the regression this pins. The
        store leaves the sink EMPTY for an unchanged field, which is what makes the
        two distinguishable."""
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting(None, _job()),  # sink stays empty
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/crons/job42", json={"name": "renamed", "chat_folder_id": ""}
            )
            assert resp.status == 200
        assert calls == []

    async def test_setting_a_folder_on_an_unfiled_job_moves_its_tab(self, monkeypatch) -> None:
        """The store reports the empty prior, and the handler acts on the key's
        PRESENCE: delivery files only a tab it has just minted, so an existing tab
        is filed here or nowhere."""
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting("", _job(chat_folder_id=FOLDER)),
        )
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": FOLDER})
        assert calls == [("job42", "", FOLDER)]

    async def test_changing_the_folder_re_points_the_tab(self, monkeypatch) -> None:
        """Delivery never re-points a filed tab, so a folder change is applied here
        or nowhere."""
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=self._update_reporting(FOLDER, _job(chat_folder_id=OTHER_FOLDER)),
            folders=(FOLDER, OTHER_FOLDER),
        )
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": OTHER_FOLDER})
        assert calls == [("job42", FOLDER, OTHER_FOLDER)]

    async def test_resaving_the_same_folder_moves_nothing(self, monkeypatch) -> None:
        calls = self._capture_unfile(monkeypatch)
        app = _app(
            api_cron_update,
            "/api/crons/{job_id}",
            update_job_async=AsyncMock(return_value=_job(chat_folder_id=FOLDER)),
        )
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"chat_folder_id": FOLDER})
        assert calls == []

    async def test_a_field_absent_from_the_request_does_not_unfile(self, monkeypatch) -> None:
        calls = self._capture_unfile(monkeypatch)
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"name": "renamed"})
        assert calls == []

    async def test_an_untouched_field_is_not_sent_to_the_store(self) -> None:
        """Editing any other setting must not rewrite the placement."""
        update = AsyncMock(return_value=_job(chat_folder_id=FOLDER))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/crons/job42", json={"silent": True})
        assert "chat_folder_id" not in update.await_args.kwargs


@pytest.mark.asyncio
class TestRestList:
    async def test_the_payload_carries_the_field(self) -> None:
        """Without it the form control defaults on load and the next save of any
        unrelated change silently unfiles the job."""
        job = _job(chat_folder_id=FOLDER)
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}

        rows = json.loads((await api_crons(request)).body)["jobs"]
        assert rows[0]["chat_folder_id"] == FOLDER
        assert rows[0]["folder_id"] == ""
