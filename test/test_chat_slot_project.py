"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project, api_chat_slot_workspace
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session_allocation import RetireArm


def _armed_cwd(boundary, folded: str) -> str | None:
    """The directory an arm names, or ``None`` when nothing is armed for this key."""
    arm = boundary._arm_if_any(folded)
    return arm.cwd if arm is not None else None


def _is_armed(boundary, folded: str) -> bool:
    """True while this key still has an arm to honour, whatever its record's history."""
    arm = boundary._arm_if_any(folded)
    return arm is not None and (arm.cwd is not None or arm.agent is not None)


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # Resolve-only helper: async, and must return a real path string for the arm sites.
    state.sessions.resolve_arm_cwd = AsyncMock(side_effect=lambda key, cwd: cwd or "/w/_default")
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


class TestWorkspaceSwitchAdvancesTheGeneration:
    """A workspace switch commits a new project, so it must advance the generation.

    The retry site prefers a live arm over the cwd its caller stated, which is correct
    while the arm is the newest statement about the project. A switch that commits a new
    project without advancing the generation leaves the earlier arm live, so it outranks
    the selection and the first turn's relative writes land in the project the user left.
    """

    @staticmethod
    def _factory(seen: list):
        def factory(session_key=None, agent=None, channel_id=None, cwd=None, **kwargs):
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.shutdown = AsyncMock()
            provider.cwd = cwd if cwd else "/unset"
            provider.context_usage_pct = MagicMock(return_value=0.0)
            provider.is_alive = MagicMock(return_value=True)
            provider.is_process_alive = MagicMock(return_value=True)
            provider.has_active_turn = MagicMock(return_value=False)
            provider.runtime_info = MagicMock(return_value=(None, None))
            seen.append(provider)
            return provider

        return factory

    @pytest.mark.asyncio
    async def test_the_retried_turn_binds_the_newly_selected_workspace(self, tmp_path):
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.session import SessionManager

        beta = tmp_path / "beta"
        gamma = tmp_path / "gamma"
        for d in (beta, gamma):
            d.mkdir()

        seen: list = []
        mgr = SessionManager(KiroCrewConfig(), provider_factory=self._factory(seen))
        slot = _ChatSlot("test")
        slot.project = str(beta)
        state = _mock_state(slot)
        state.sessions = mgr

        # The key the slot's turns actually run on, which is what the handler advances.
        session_key = effective_session_key(slot)
        # The eager spawn for beta armed the key; nothing has satisfied it yet.
        mgr.mark_retire_on_next_claim(session_key, str(beta))

        with (
            patch(
                "kiro_crew.dashboard.chat_handlers._reset_slot_session_or_warn",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.default_project_dir",
                new=MagicMock(return_value=str(gamma)),
            ),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/workspace", json={"workspace": "gamma-ws"}
                )
                assert resp.status == 200, await resp.text()

        assert slot.project == str(gamma), "precondition: the switch committed gamma"

        # The first turn after the switch. It states gamma; the retry site must not
        # substitute the arm's beta for it.
        provider, _, _ = await mgr.get_or_create(session_key, cwd=str(gamma))
        assert provider.cwd == str(gamma), (
            "the turn after a workspace switch must bind the selected project; binding "
            f"{provider.cwd!r} writes into the workspace the switch left"
        )
        await mgr.close_all()


class TestChatSlotProject:
    @pytest.mark.asyncio
    async def test_set_project(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["project"] == str(tmp_path)
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_clear_project(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": ""},
            )
            assert resp.status == 200
            assert slot.project == ""

    @pytest.mark.asyncio
    async def test_a_failed_workspace_resolution_rolls_the_project_commit_back(self, tmp_path):
        """An unresolvable default workspace must not leave the slot committed but unarmed.

        The commit lands before the arm target is resolved, so a raise there leaves
        `slot.project` on the new value with NO deferred reset and NO retirement arm -- and a
        later cwd-less channel claim then reuses the session still rooted at the OLD project.
        The commit is rolled back on its own token identity and the caller is told, rather
        than the change half-applying.
        """
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        state.sessions.resolve_arm_cwd = AsyncMock(
            side_effect=OSError("default workspace unavailable")
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert (
                resp.status == 503
            ), f"a resolution failure must be reported, not swallowed; got {resp.status}"
            assert slot.project == str(tmp_path), (
                "and the commit must be ROLLED BACK -- a slot left on the new project with no "
                f"arm serves the old session to the next cwd-less claim; got {slot.project!r}"
            )
            assert (
                slot._pending_reset_history_key is None
            ), "nothing may be armed either, or the rollback and the arm disagree"

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_400(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": "/nonexistent_xyz_123"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sensitive_path_returns_403(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_data_home_overlap_returns_actionable_400(self, tmp_path, monkeypatch):
        """Pre-flight: a workspace containing the voice runtime is refused
        at the endpoint with the actionable message, before any session spawn."""
        import kiro_crew.sandbox as sandbox_mod

        # The pre-flight is darwin-gated to match the spawn-time guards it
        # mirrors, so pin the platform for the refusal path.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": str(tmp_path)},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "workspace_overlaps_data_home"
            assert "protected voice runtime" in data["error"]
            # The guard message embeds paths with !r, so on Windows the
            # backslashes are repr-escaped — assert the repr form, which is the
            # exact token the formatter emits on every platform.
            assert repr(str(runtime)) in data["error"]
            assert "Pick a project subdirectory" in data["error"]
            assert slot.project != str(tmp_path)

    @pytest.mark.asyncio
    async def test_can_change_mid_session(self, tmp_path):
        """Unlike workspace, project can be changed after messages are sent."""
        slot = _ChatSlot("test")
        slot.total_messages = 5
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/project",
                json={"project": "/tmp"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_change_defers_session_reset(self, tmp_path):
        """Endpoint sets the deferred-reset flag instead of resetting inline,
        because an inline reset would killpg the MCP-core child that called it.
        chat_runner consumes the flag so the next message picks up the new CWD."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        # Reset is deferred — endpoint must NOT call it inline.
        state.sessions.reset.assert_not_awaited()
        # Flag is set on the slot so chat_runner can consume it at the turn boundary.
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_the_arm_is_raised_before_the_endpoint_returns(self, tmp_path):
        """The arm cannot wait for the deferred consumer -- there is a window before it.

        The RESET is deferred because this endpoint is reachable over loopback from inside
        the kiro-cli process group, so an inline teardown would killpg the caller. The ARM
        is not: it is in-memory bookkeeping. Leaving it to `_consume_pending_reset` puts it
        behind the eager task's 1.5s debounce, and a channel turn arriving in that window
        states NO cwd -- so the claim-time directory check cannot fire and the arm is the
        only thing that would refuse the pre-change session. Raised here, the window is
        closed by the time the caller gets its 200.
        """
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1234567890.123456"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200

        # Nothing has consumed the deferral yet -- no eager task was allowed to run.
        assert slot._pending_reset_history_key == "slack:1234567890.123456"
        state.sessions.mark_retire_on_next_claim.assert_called_once_with(
            "slack:1234567890.123456", str(tmp_path)
        )

    @pytest.mark.asyncio
    async def test_channel_linked_slot_defers_reset_on_its_channel_session(self, tmp_path):
        """A channel-born slot runs its turns on the channel's own session, so the
        deferred teardown has to name THAT session. The ``dashboard:`` prefix is
        unconditional, so deriving the key from the slot key instead would name a
        nonexistent ``dashboard:slack:<ts>``, the teardown would miss the live
        session, and a later turn would reuse it with the pre-change directory."""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1234567890.123456"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "slack:1234567890.123456"
        assert "dashboard:" not in slot._pending_reset_history_key

    @pytest.mark.asyncio
    async def test_unchanged_does_not_set_pending_reset(self, tmp_path):
        """No-op when project doesn't change: no inline reset and no flag set."""
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None


class TestFolderProjectDirOverlapPreflight:
    """The folder ``project_dir`` write path is the third
    user-driven project chokepoint — it must refuse a data-home overlap at the
    moment of choice with the SAME message as the endpoint and set_project.
    The check lives in ``_folder_project_overlap_denied`` (run off-loop
    by the create/update handlers), NOT in ``_validate_project_dir``, which the
    slot-create read path re-runs against stored values."""

    def _pin_runtime(self, tmp_path, monkeypatch):
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        return runtime

    def test_folder_overlap_denied_with_guard_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        runtime = self._pin_runtime(tmp_path, monkeypatch)
        err = _folder_project_overlap_denied(str(tmp_path))
        assert err is not None
        # Byte-identical family: same formatter as endpoint + spawn guard.
        assert "protected voice runtime" in err
        assert repr(str(runtime)) in err
        assert "Pick a project subdirectory" in err

    def test_folder_overlap_check_accepts_non_overlapping_dir(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        self._pin_runtime(tmp_path, monkeypatch)
        clean = tmp_path / "clean"
        clean.mkdir()
        assert _folder_project_overlap_denied(str(clean)) is None


class TestClaimCwdTriState:
    """A slot with no project must be distinguishable from one whose project was cleared.

    Stating the cleared sentinel for both makes EVERY project-less slot -- the default
    configuration -- bypass the warm pool and skip its stored-cwd resume override, while the
    staleness the sentinel exists to refuse only arises once a binding has actually changed.
    """

    def test_a_slot_that_never_had_a_project_states_no_cwd(self):
        slot = _ChatSlot("test")
        assert slot.project == ""
        assert slot.claim_cwd is None, (
            "a default-configuration slot states the cleared sentinel, so it loses the warm "
            f"pool and its resume override; got {slot.claim_cwd!r}"
        )

    def test_a_cleared_project_still_states_the_cleared_sentinel(self):
        from kiro_crew.config.paths import CWD_CLEARED

        slot = _ChatSlot("test")
        slot.project_cleared = True
        assert slot.claim_cwd == CWD_CLEARED

    def test_a_set_project_states_that_directory(self):
        slot = _ChatSlot("test")
        slot.project = "/workspace/thing"
        slot.project_cleared = False
        assert slot.claim_cwd == "/workspace/thing"

    def test_the_cleared_marker_beats_a_project_that_outlived_its_clear(self):
        """A retained on-disk project must NOT resurrect the former working directory.

        The metadata merge is an upsert that cannot delete a key, so a slot whose project was
        set and then cleared still carries the old directory in its record. Reading the project
        first honored that value, and relative writes landed silently in the former project.
        """
        from kiro_crew.config.paths import CWD_CLEARED

        slot = _ChatSlot("test")
        # The shape persistence can hand back: a retained value beside the clear marker.
        slot.project = "/workspace/old"
        slot.project_cleared = True

        assert slot.claim_cwd == CWD_CLEARED, (
            "the slot states the former project, so its next claim binds a directory the user "
            f"cleared and writes land there unannounced; claim_cwd={slot.claim_cwd!r}"
        )

    def test_a_cleared_project_survives_a_restart(self):
        """The cleared state must round-trip through the persisted slot metadata.

        A cleared project writes no ``project`` key at all, so unless the flag is persisted in
        its own right the restored slot is indistinguishable from one that never had a project
        -- and the resume then restores the very directory the clear was meant to abandon.

        Asserted at the metadata contract rather than by booting a second process: every
        persist site must emit the key and every restore site must read it, and a slot rebuilt
        from that metadata must state the same cwd as the live one.
        """
        import json
        from pathlib import Path

        from kiro_crew.dashboard import chat_persistence

        src = Path(chat_persistence.__file__).read_text(encoding="utf-8")
        writes = src.count('"project_cleared"] = bool(')
        reads = src.count('meta.get("project_cleared")')
        assert writes >= 2, (
            f"only {writes} persist site(s) emit the cleared flag; a slot saved by the other "
            "path loses it, so the clear does not survive a restart"
        )
        assert reads >= 2, (
            f"only {reads} restore site(s) read the cleared flag; a slot loaded by the other "
            "path comes back looking as though it never had a project"
        )

        live = _ChatSlot("test")
        live.project_cleared = True
        meta: dict = {}
        if live.project:
            meta["project"] = live.project
        if getattr(live, "project_cleared", False):
            meta["project_cleared"] = True

        restored = _ChatSlot("test")
        reloaded = json.loads(json.dumps(meta))
        if reloaded.get("project"):
            restored.project = reloaded["project"]
        if reloaded.get("project_cleared"):
            restored.project_cleared = True

        assert restored.claim_cwd == live.claim_cwd, (
            "the restored slot states a different cwd than the live one, so the next claim "
            f"binds elsewhere; live={live.claim_cwd!r} restored={restored.claim_cwd!r}"
        )


class TestArmRetractionIsScopedToItsOwnGeneration:
    """A producer unwinding its own arm must not drop an arm another producer just wrote.

    `supersede_arm_for_new_slot` serves two callers. At slot mint and final teardown the slot
    is gone, so every arm on its key is void whoever wrote it and the unconditional drop is
    right. On a producer's DENIAL path it is instead a RETRACTION of that producer's own work,
    and the awaits before it are real yield points -- so a second producer can have armed the
    same key in between. Dropping that arm leaves its session reusable at the old cwd, which
    is a silent cross-project write.
    """

    def test_a_retraction_leaves_a_concurrent_producers_arm_in_place(self, tmp_path):
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        mgr = SessionManager(KiroCrewConfig())
        key = "chat-arm-1"
        mine, theirs = tmp_path / "mine", tmp_path / "theirs"
        mine.mkdir()
        theirs.mkdir()

        # Producer A arms, then yields: the handler awaits a thread and a resolve here.
        generation_a = mgr.mark_retire_on_next_claim(key, str(mine))
        assert isinstance(
            generation_a, int
        ), "the arm reports no generation, so a producer cannot name its own arm to retract"

        # Producer B arms the SAME key inside A's window, superseding A's arm.
        mgr.mark_retire_on_next_claim(key, str(theirs))

        # A is denied and unwinds, and must retract only what it wrote.
        mgr.supersede_arm_for_new_slot(key, only_generation=generation_a)

        boundary = mgr._allocation_boundary()
        folded = mgr._fold_key(key)
        assert _armed_cwd(boundary, folded) == str(theirs), (
            "the denial dropped a concurrent producer's arm, so its session stays reusable "
            f"at the old cwd; arm={_armed_cwd(boundary, folded)!r}"
        )

    def test_a_retraction_still_drops_the_arm_it_owns(self, tmp_path):
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        mgr = SessionManager(KiroCrewConfig())
        key = "chat-arm-2"
        mine = tmp_path / "mine"
        mine.mkdir()

        generation = mgr.mark_retire_on_next_claim(key, str(mine))
        mgr.supersede_arm_for_new_slot(key, only_generation=generation)

        boundary = mgr._allocation_boundary()
        assert not _is_armed(boundary, mgr._fold_key(key)), (
            "the producer's own arm survived its retraction, so a denied request still "
            "retires a session it was refused permission to touch"
        )

    def test_an_unscoped_supersede_still_drops_every_arm(self, tmp_path):
        """Slot mint and final teardown keep the unconditional drop."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        mgr = SessionManager(KiroCrewConfig())
        key = "chat-arm-3"
        other = tmp_path / "other"
        other.mkdir()

        mgr.mark_retire_on_next_claim(key, str(other))
        mgr.supersede_arm_for_new_slot(key)

        boundary = mgr._allocation_boundary()
        assert not _is_armed(boundary, mgr._fold_key(key))


class TestAClearedProjectNeverResurrectsTheOldDirectory:
    """The empty-slot merge retains `project`, so every restore path must honor the marker.

    `update_metadata_if` is an upsert: it cannot delete a key. A slot that had `/old` and was then
    cleared therefore keeps `"project": "/old"` in its record, and any restore path that reads the
    project without the marker resumes writing into a directory the user cleared.
    """

    def test_a_record_carrying_both_resumes_as_cleared_on_every_path(self):
        from kiro_crew.config.paths import CWD_CLEARED
        from kiro_crew.dashboard.state import _ChatSlot

        # The record a pre-clear save leaves behind.
        meta = {"project": "/workspace/old", "project_cleared": True}

        # The pair both the History resume and the channel surfacing perform, in their order.
        for label in ("history-resume", "channel-surfacing"):
            slot = _ChatSlot("test")
            if meta.get("project"):
                slot.project = meta["project"]
            if meta.get("project_cleared"):
                slot.project_cleared = True
            assert (
                slot.claim_cwd == CWD_CLEARED
            ), f"{label} resumes {slot.claim_cwd!r}, the directory the clear was meant to drop"


class TestEveryProducerOfTheClearedMarkerKeepsIt:
    """The clear reaches `claim_cwd` only if each producer writes or restores the marker.

    `claim_cwd` reading the marker first is exercised directly above. These pin the three places
    that must PUT it there, because dropping any of them silently returns the slot to the
    former-directory behaviour with every other test still green.
    """

    def test_the_empty_slot_merge_writes_project_unconditionally(self):
        """An upsert cannot delete a key, so a truthy-only write retains the old directory."""
        import inspect
        import re

        from kiro_crew.dashboard import chat_persistence

        src = inspect.getsource(chat_persistence._save_slot_to_history)
        assert re.search(r'^\s*fields\["project"\] = slot\.project\s*$', src, re.M), (
            "the empty-slot merge no longer writes `project` unconditionally, so a cleared slot "
            "keeps its pre-clear directory in the record"
        )
        assert not re.search(
            r'if slot\.project:\s*\n\s*fields\["project"\]', src
        ), "the merge writes `project` only when truthy again"

    def test_both_restore_paths_read_the_cleared_marker(self):
        """History resume and channel surfacing each set `slot.project` from metadata."""
        import inspect

        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_resume

        for label, obj in (
            ("channel surfacing", channel_slots.surface_channel_session),
            ("history resume", api_chat_slot_resume),
        ):
            src = inspect.getsource(obj)
            assert (
                'slot.project = meta["project"]' in src
            ), f"{label} no longer restores the project here; re-point this control"
            assert 'meta.get("project_cleared")' in src, (
                f"{label} restores the project without the cleared marker, so the slot reads as "
                "never-scoped and its next claim binds the directory the clear dropped"
            )


class TestAStaleClearedMarkerCannotOverrideANewProject:
    """A marker retained from an earlier clear must not outrank a project selected after it.

    Sequence: set a project, clear it, persist, select a NEW project, persist, restart. The merge
    is an upsert, so the clear's `project_cleared: True` is still in the record when the second
    save lands. If that save omits the key, the restored slot reads as cleared and its next claim
    binds the default workspace, so relative writes miss the project the user just chose.
    """

    def test_the_second_save_overwrites_the_marker_rather_than_omitting_it(self):
        import inspect
        import re

        from kiro_crew.dashboard import chat_persistence

        src = inspect.getsource(chat_persistence._save_slot_to_history)
        assert re.search(r'^\s*fields\["project_cleared"\] = bool\(', src, re.M), (
            "the merge writes `project_cleared` only when True again, so a clear's marker "
            "survives a later project selection and overrides it"
        )
        assert not re.search(
            r'if getattr\(slot, "project_cleared", False\):\s*\n\s*fields\["project_cleared"\]',
            src,
        ), "the conditional write is back"

    def test_a_reselected_project_wins_after_the_record_carried_a_clear(self):
        from kiro_crew.dashboard.state import _ChatSlot

        # The record after: set /old, clear, persist.
        record = {"project": "", "project_cleared": True}

        # The user now selects /new; the save merges these fields over that record.
        slot = _ChatSlot("test")
        slot.project = "/workspace/new"
        slot.project_cleared = False
        merged = dict(record)
        merged["project"] = slot.project
        merged["project_cleared"] = bool(getattr(slot, "project_cleared", False))

        # Restart: the real restore pair, then the real tri-state.
        resumed = _ChatSlot("test")
        if merged.get("project"):
            resumed.project = merged["project"]
        if merged.get("project_cleared"):
            resumed.project_cleared = True

        assert resumed.claim_cwd == "/workspace/new", (
            "the resumed slot ignores the project just selected and states "
            f"{resumed.claim_cwd!r}, so relative writes land in the default workspace"
        )


class TestReopeningAClearedSlotBindsTheProjectItIsGiven:
    """Re-scoping a cleared slot must drop the marker the reopen carried forward.

    `claim_cwd` reads the marker before the project on purpose: a project value can outlive its
    clear in a record an upsert cannot delete. That makes any site which ASSIGNS a project
    responsible for retiring the marker, and the create/reopen fallback did not.
    """

    def test_the_reopen_path_retires_the_marker_when_it_assigns_a_project(self):
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat_slot_create)
        assign = src.find("slot.project = cfg_proj or default_project_dir(workspace)")
        retire = src.find("slot.project_cleared = False")
        assert assign != -1, "the default assignment moved; re-point this control"
        assert retire != -1, (
            "the reopen path assigns a project without retiring the cleared marker, so the "
            "next turn binds the fallback workspace instead of the project just assigned"
        )
        assert assign < retire, "the marker is retired before the assignment that needs it"

    def test_a_reassigned_project_wins_over_the_carried_marker(self):
        from kiro_crew.dashboard.state import _ChatSlot

        # State after: set a project, clear it, reopen -- the restore carries the marker.
        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = True

        # What the create/reopen fallback now does when it re-scopes the slot.
        slot.project = "/workspace/assigned"
        if slot.project:
            slot.project_cleared = False

        assert slot.claim_cwd == "/workspace/assigned", (
            "the reopened slot states {!r} rather than the project it was just assigned, so "
            "its turn runs in the fallback workspace".format(slot.claim_cwd)
        )


class TestARebindInsideTheClearedResolveDoesNotStrandTheArm:
    """A rebind landing inside the cleared-cwd resolve must not strand the arm.

    The handler read the effective key ONCE and then awaited ``resolve_arm_cwd``;
    the transfer and the deferred reset both used that pre-await snapshot. A slot
    that rebound during the resolve was therefore armed on the key it had already
    abandoned, and the next turn on the live key bound the pre-change directory
    with no arm to correct it.
    """

    @pytest.mark.asyncio
    async def test_the_arm_follows_the_key_the_slot_holds_after_the_resolve(self, tmp_path):
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.dashboard.chat_handlers import effective_session_key
        from kiro_crew.session import SessionManager

        alpha, beta = tmp_path / "alpha", tmp_path / "beta"
        alpha.mkdir()
        beta.mkdir()

        slot = _ChatSlot("test")
        slot.project = str(alpha)
        state = _mock_state(slot)
        seen: list = []
        mgr = SessionManager(
            KiroCrewConfig(),
            provider_factory=TestWorkspaceSwitchAdvancesTheGeneration._factory(seen),
        )
        state.sessions = mgr

        stale_key = "slack:rebind-first"
        rebound_key = "slack:rebind-second"

        original_resolve = mgr.resolve_arm_cwd

        def save_then_rebind_and_clear(_project):
            # Two lock-free writers land in this await: the in-turn set_project directive
            # clears the project, and a linked_session_key writer rebinds the slot.
            slot.linked_session_key = stale_key
            slot.project = ""

        async def resolve_then_rebind(key, cwd):
            resolved = await original_resolve(key, cwd)
            # THE WINDOW: a second rebind lands while this resolve is suspended, which is
            # precisely what a key captured before the await cannot observe.
            if slot.linked_session_key == stale_key:
                slot.linked_session_key = rebound_key
            return resolved

        with (
            patch.object(mgr, "resolve_arm_cwd", new=resolve_then_rebind),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", new=MagicMock()),
            patch(
                "kiro_crew.dashboard.chat_handlers._save_recent_project",
                new=MagicMock(side_effect=save_then_rebind_and_clear),
            ),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(beta)}
                )
                assert resp.status == 409, await resp.text()
                assert "session_rebound" in await resp.text()

        assert effective_session_key(slot) == rebound_key, "precondition: the rebind did not land"

        boundary = mgr._allocation_boundary()
        stale_arm = _armed_cwd(boundary, mgr._fold_key(stale_key))
        assert stale_arm is None, (
            "the arm was left on the key the slot abandoned during the resolve, so the live "
            f"session keeps the pre-change directory; stale arm={stale_arm!r}"
        )
        assert slot._pending_reset_history_key != stale_key, (
            "the deferred reset still names the abandoned key, so it tears down a session "
            "nobody is on and leaves the live one at the old cwd"
        )

        # Behavioural half: the refusal committed nothing, so the claim on the LIVE key must
        # not have been armed toward the project this request was refused for.
        provider, _, _ = await mgr.get_or_create(effective_session_key(slot))
        assert provider.cwd != str(beta), (
            "a refused switch still steered the live session at the project it declined to "
            f"commit; cwd={provider.cwd!r}"
        )


class TestTheArmRecordKeepsWhatSpendingMustNotDrop:
    """The generation outlives the arm, which is the whole reason the two live in one record."""

    def test_spending_drops_the_arm_and_keeps_the_generation(self):
        """A spent arm names nothing, but a start still compares against the counter.

        Dropping the generation here would make a start that began BEFORE the change read as
        current and be served, which is the staleness the counter exists to catch.
        """
        arm = RetireArm(generation=7, cwd="/projects/beta", agent="research")

        arm.spend()

        assert arm.generation == 7, "spending dropped the counter a stale start is judged against"
        assert arm.cwd is None
        assert arm.agent is None
