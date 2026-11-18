"""A job tab minted again after a close keeps the folder its transcript names.

Closing a ``cron-{id}`` tab saves its line with ``closed`` and keeps ``folder_id``.
The next run mints the tab again; before the mint read that folder back, the tab
came up unfiled and its first save wrote the line without the folder.
"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.session_surface import set_dashboard_surfaced

FOLDER = "fold0001"
JOB_FOLDER = "fold0002"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes into the process-global dashboard-surface registry."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


@pytest.fixture(autouse=True)
def _no_workspace_lookup(_floor_monkeypatch):
    """The tab's workspace is not under test; skip the config read."""
    _floor_monkeypatch.setattr("kiro_crew.dashboard.cron_inject.cron_tab_workspace", lambda job: "")


def _job(**over) -> CronJob:
    fields = {"id": "job42", "name": "standup brief", "message": "Write the brief."}
    fields.update(over)
    return CronJob(**fields)


def _closed_tab(tmp_path, monkeypatch, folders=(FOLDER, JOB_FOLDER)) -> DashboardState:
    """A state whose ``cron:job42`` line names FOLDER and is closed, with no tab."""
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state._folders = [{"id": fid, "name": f"folder-{fid}"} for fid in folders]
    slot = state.get_or_create_slot("cron-job42")
    slot.linked_session_key = "cron:job42"
    slot.append("assistant", "run one", "")
    slot.folder_id = FOLDER
    assert _save_slot_to_history(state, slot, force=True)
    assert _save_slot_to_history(state, slot, force=True, closed=True)
    state._slots.pop("cron-job42")
    meta = state.conversation_log.get_metadata("cron:job42")
    assert meta["folder_id"] == FOLDER and meta["closed"] is True
    return state


#: How long a scheduled placement may take before the test fails by name.
PLACEMENT_CEILING_SECS = 60.0


async def _settle(state: DashboardState) -> None:
    """Let the scheduled job-folder placement land, within PLACEMENT_CEILING_SECS."""
    pending = list(state._background_tasks)
    try:
        await asyncio.wait_for(asyncio.gather(*pending), PLACEMENT_CEILING_SECS)
    except asyncio.TimeoutError:
        pytest.fail(f"cron tab placement did not land within {PLACEMENT_CEILING_SECS}s")


def _save_run(state: DashboardState, text: str) -> None:
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    slot = state.get_slot("cron-job42")
    assert slot is not None
    slot.append("assistant", text, "")
    assert _save_slot_to_history(state, slot, force=True)


def _flush(state: DashboardState) -> bool:
    """A forced save of the job's tab, if it exists yet; True when it wrote."""
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    slot = state.get_slot("cron-job42")
    return slot is not None and bool(_save_slot_to_history(state, slot, force=True))


class TestRemintKeepsTheStoredFolder:
    @pytest.mark.asyncio
    async def test_run_start_files_the_tab_back_and_the_save_keeps_it(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = _closed_tab(tmp_path, monkeypatch)
        await ensure_cron_slot(state, _job())
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == FOLDER
        _save_run(state, "run two")
        assert state.conversation_log.get_metadata("cron:job42")["folder_id"] == FOLDER

    @pytest.mark.asyncio
    async def test_delivery_files_the_tab_back_and_the_save_keeps_it(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import (
            inject_cron_result_to_dashboard,
            stored_cron_folder,
        )

        state = _closed_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        inject_cron_result_to_dashboard(
            state,
            _job(),
            "run two",
            history=log.read_messages("cron:job42"),
            stored_folder=await stored_cron_folder(state, "job42"),
        )
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == FOLDER
        _save_run(state, "run two, saved")
        assert log.get_metadata("cron:job42")["folder_id"] == FOLDER

    @pytest.mark.asyncio
    async def test_the_jobs_own_folder_still_files_a_minted_tab(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = _closed_tab(tmp_path, monkeypatch)
        await ensure_cron_slot(state, _job(chat_folder_id=JOB_FOLDER))
        await _settle(state)
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == JOB_FOLDER
        assert state.conversation_log.get_metadata("cron:job42")["folder_id"] == JOB_FOLDER

    @pytest.mark.asyncio
    async def test_a_stored_folder_deleted_since_leaves_the_tab_unfiled(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = _closed_tab(tmp_path, monkeypatch)
        state._folders = []
        await ensure_cron_slot(state, _job())
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_job_folder_that_does_not_commit_rolls_back_to_the_stored_one(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.dashboard.chat_persistence as persistence
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        state = _closed_tab(tmp_path, monkeypatch)
        monkeypatch.setattr(persistence, "save_slot_off_loop", AsyncMock(return_value=False))
        await ensure_cron_slot(state, _job(chat_folder_id=JOB_FOLDER))
        await _settle(state)
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == FOLDER

    @pytest.mark.asyncio
    async def test_a_stored_folder_in_another_mates_home_leaves_the_tab_unfiled(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot
        from kiro_crew.dashboard.mate_folders import MATE_HOME_KEY

        state = _closed_tab(tmp_path, monkeypatch)
        state._folders = [{"id": FOLDER, MATE_HOME_KEY: True, "owner_app": "member:other"}]
        await ensure_cron_slot(state, _job())
        slot = state.get_slot("cron-job42")
        assert slot is not None and slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_an_unfiled_tab_found_in_the_table_is_not_filed(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.cron_inject import _bind_cron_slot

        state = _closed_tab(tmp_path, monkeypatch)
        found = state.get_or_create_slot("cron-job42")  # dragged out to the root
        slot = _bind_cron_slot(state, _job(), [], [], stored_folder=FOLDER)
        assert slot is found and slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_tab_found_in_the_table_reads_nothing(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.cron_inject import stored_cron_folder

        state = _closed_tab(tmp_path, monkeypatch)
        state.get_or_create_slot("cron-job42")
        assert await stored_cron_folder(state, "job42") == ""

    @pytest.mark.asyncio
    async def test_an_unreadable_line_answers_no_folder(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.cron_inject import stored_cron_folder

        state = _closed_tab(tmp_path, monkeypatch)
        log = state.conversation_log
        monkeypatch.setattr(log, "get_metadata_status", MagicMock(side_effect=OSError("disk")))
        assert await stored_cron_folder(state, "job42") == ""


class TestToChat:
    @pytest.mark.asyncio
    async def test_to_chat_hands_the_stored_folder_to_the_injection(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.cron import api_cron_to_chat

        state = _closed_tab(tmp_path, monkeypatch)
        job = _job()
        job.set_run_result("the brief")
        state.crons.list_jobs.return_value = [job]
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/crons/{job_id}/to-chat", api_cron_to_chat)
        with patch("kiro_crew.dashboard.handlers.cron.inject_cron_result_to_dashboard") as inject:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/crons/job42/to-chat")
                assert resp.status == 200
        inject.assert_called_once_with(
            state,
            job,
            "the brief",
            history=ANY,
            dismissed=ANY,
            stored_folder=FOLDER,
            include_prompt=False,
        )

    @pytest.mark.asyncio
    async def test_to_chat_for_a_deleted_job_files_the_tab_back(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.cron import api_cron_to_chat

        state = _closed_tab(tmp_path, monkeypatch)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/crons/{job_id}/to-chat", api_cron_to_chat)
        log = state.conversation_log
        read = log.get_metadata_status
        during_read: list[str] = []

        def _read_noting_the_tab(key):
            # What a periodic flush landing inside the handler's metadata read
            # would serialize: the folder the bound tab holds at that moment.
            slot = state.get_slot("cron-job42")
            if slot is not None and slot.linked_session_key:
                during_read.append(slot.folder_id)
            return read(key)

        monkeypatch.setattr(log, "get_metadata_status", _read_noting_the_tab)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/crons/job42/to-chat")
            assert resp.status == 200
        assert during_read == [FOLDER]
        monkeypatch.setattr(log, "get_metadata_status", read)
        assert _flush(state)
        assert log.get_metadata("cron:job42")["folder_id"] == FOLDER


class TestGatewayDelivery:
    @pytest.mark.asyncio
    async def test_the_run_delivery_hands_the_stored_folder_to_the_injection(
        self, monkeypatch
    ) -> None:
        from kiro_crew.slack import gateway

        state = MagicMock()
        state.get_slot = lambda name: None
        state.has_slot = lambda name: False
        state.conversation_log.read_messages.return_value = []
        state.conversation_log.get_metadata_status.return_value = ({"folder_id": FOLDER}, True)
        inject = MagicMock()
        monkeypatch.setattr(gateway, "inject_cron_result_to_dashboard", inject)
        gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
        gw.dashboard_state = state
        gw.slack = None
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)
        job = _job()
        job.set_run_result("the brief")
        await asyncio.wait_for(
            gateway.GatewayOrchestrator._deliver_cron_agent_result(
                gw, job, "cron:job42", MagicMock(), "the brief", "hash", None, None
            ),
            PLACEMENT_CEILING_SECS,
        )
        assert inject.call_args.kwargs["stored_folder"] == FOLDER
