"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.paths import CWD_CLEARED
from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


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


def _saveable_slot(tmp_path, monkeypatch):
    """A slot on a real ``DashboardState`` whose metadata line can be written and read back."""
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello")
    return state, slot


def _save(state, slot) -> None:
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    # closed=False: a closed session is skipped by rehydrate, so it could never round-trip.
    _save_slot_to_history(state, slot, closed=False)


def _restore(state):
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    return _rehydrate_slot_from_history(state, "s1")


class TestProjectClearedMarker:
    """``project_cleared`` distinguishes a REMOVED project from one never set.

    ``project=""`` carries both states, and only the removal invalidates a stored cwd, so
    the marker is what a consumer reads. These cover the two production writers and the
    round-trip through disk -- a marker no path writes, or one that dies at restart, leaves
    the distinction unreachable however carefully a consumer reads it.
    """

    @pytest.mark.asyncio
    async def test_clearing_over_http_records_the_clear(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project == ""
        assert slot.project_cleared is True
        assert slot.claim_cwd == CWD_CLEARED

    @pytest.mark.asyncio
    async def test_clearing_over_http_schedules_the_clear_for_disk(self, tmp_path):
        """A 200 to the clear must not leave it in memory only.

        The periodic flush writes a slot's metadata line only while ``_dirty`` is set, and a
        clear arrives between turns, where nothing else on this path sets it -- so unmarked, a
        crash before the next save restores the project the user removed, and the slot they
        watched go blank comes back bound to it.
        """
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        slot._dirty = False
        assert slot._dirty is False, "the slot starts marked, so the assertion below is vacuous"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_clearing_an_already_empty_project_over_http_tears_the_session_down(self):
        """A slot with no project can still be RUNNING in one, so the clear must reset it.

        A resumed session restores its own stored cwd whenever the caller states none, so a
        slot whose project is empty may still be bound to a directory on disk. Clearing is
        the statement that invalidates that binding -- but the text does not move, so a gate
        watching only the project would leave the live session in the removed directory and
        answer from it on the next turn.
        """
        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = False
        assert (
            slot._pending_reset_history_key is None
        ), "the flag starts armed, so the assertion below would pass without the clear"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key == effective_session_key(slot)

    @pytest.mark.asyncio
    async def test_clearing_an_already_cleared_project_stays_a_no_op(self):
        """The marker gate must not cost a cold start when nothing actually changed.

        Re-clearing a slot that is already cleared moves neither the project nor the marker,
        so the reset stays unarmed -- otherwise every repeated clear tears down a session for
        no reason, which is what the original text-only gate was protecting.
        """
        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = True
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_the_clear_directive_also_tears_down_an_already_empty_project(self):
        """The in-turn ``/project clear`` directive is the second writer of the marker.

        It reaches the same stale binding by the same route, so gating its reset on the
        project text alone leaves the identical live session in the removed directory.
        """
        from kiro_crew.dashboard.session_directive_apply import _set_project

        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = False
        assert (
            slot._pending_reset_history_key is None
        ), "the flag starts armed, so the assertion below would pass without the clear"
        state = _mock_state(slot)
        await _set_project(state, slot, {"clear": True})
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key == effective_session_key(slot)

    @pytest.mark.asyncio
    async def test_naming_a_project_over_http_retracts_the_clear(self, tmp_path):
        """The retraction half. Left set, the marker would outlive the state it describes and
        ``claim_cwd`` -- which reads the marker BEFORE the project -- would keep answering
        "cleared" for a slot that now has a directory."""
        slot = _ChatSlot("test")
        slot.project_cleared = True
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(tmp_path)}
                )
                assert resp.status == 200
        assert slot.project_cleared is False
        assert slot.claim_cwd == str(tmp_path)

    @pytest.mark.asyncio
    async def test_the_directive_clear_records_the_clear(self, tmp_path):
        """The OTHER production writer. The two clear paths are reached differently -- the
        directive in-process, the endpoint over loopback HTTP -- so covering one says nothing
        about the other."""
        from kiro_crew.dashboard.session_directive_apply import _set_project

        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        await _set_project(state, slot, {"clear": True})
        assert slot.project == ""
        assert slot.project_cleared is True
        assert slot.claim_cwd == CWD_CLEARED

    def test_the_marker_survives_a_restart_and_outranks_the_stale_project(
        self, tmp_path, monkeypatch
    ):
        """The reason the marker has to reach disk at all.

        The metadata upsert cannot DELETE a key, so a slot cleared after a project was written
        comes back carrying BOTH keys. Rehydrating ``project`` alone hands that directory back
        as though it were still chosen -- which is the resurrection the marker exists to stop.
        Driven through the real save and the real restore, because an in-memory assertion
        cannot tell a persisted marker from one that merely never left the process.
        """
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        _save(state, slot)
        # Cleared AFTER the project reached disk: the line now carries both keys.
        slot.project_cleared = True
        _save(state, slot)
        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("project") == str(tmp_path), (
            "the stale project key is gone from the line, so this no longer reproduces the "
            "condition the marker exists for"
        )
        assert meta.get("project_cleared") is True

        del state._slots["s1"]
        restored = _restore(state)
        assert restored is not None
        assert restored.project_cleared is True, "the clear did not survive the restore"
        assert restored.project == "", (
            f"the restored slot's raw project is {restored.project!r}; a reader taking the "
            f"field rather than claim_cwd spawns its turn in the removed directory"
        )
        assert restored.claim_cwd == CWD_CLEARED, (
            f"the restored slot claims {restored.claim_cwd!r}; the stale project key won over "
            f"the marker, so the cleared directory is live again"
        )

    def test_a_restored_slot_with_no_marker_keeps_its_project(self, tmp_path, monkeypatch):
        """Negative control for the restore above: the same path must NOT report a clear for a
        slot that never had one, or every restored slot would read as cleared and the
        assertion above would pass for the wrong reason."""
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        _save(state, slot)
        assert "project_cleared" not in state.conversation_log._read_metadata("dashboard:s1")

        del state._slots["s1"]
        restored = _restore(state)
        assert restored is not None
        assert restored.project_cleared is False
        assert restored.claim_cwd == str(tmp_path)

    def test_the_empty_window_merge_persists_the_clear(self, tmp_path, monkeypatch):
        """The other writer of the metadata line, reached when the window holds no new rows.

        A clear arrives between turns, so the save that follows it often has nothing to
        append and the empty-window merge is the only writer the marker ever sees. Skipping
        it there loses the clear on a restart inside that gap -- and because the upsert cannot
        delete the stale ``project``, the slot comes back pointing at the directory the user
        removed. The field is written unconditionally here, like the sibling clearable
        ``memory_store``, so ``test_session_control``'s slot-owned drift guard pins it.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        # The merge only ever updates a record that exists, so the line has to be
        # materialized by a full save before the empty window can reconcile it.
        _save_slot_to_history(state, slot, closed=False, force=True)

        slot.project_cleared = True
        slot.messages.clear()
        _save_slot_to_history(state, slot, closed=False, force=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("project") == str(tmp_path), (
            "the stale project key is absent, so this no longer reproduces the condition that "
            "makes losing the marker a resurrection rather than a no-op"
        )
        assert meta.get("project_cleared") is True, (
            "the empty-window merge dropped the clear; a restart in the gap before the next "
            "row restores the stale project as though it were still chosen"
        )

    def test_naming_a_project_again_erases_the_marker_on_disk(self, tmp_path, monkeypatch):
        """Ownership, proven at the line rather than at the set: a carried key would survive
        the retraction and the slot would come back cleared after the user chose a directory."""
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project_cleared = True
        slot.drain()
        _save(state, slot)
        assert state.conversation_log._read_metadata("dashboard:s1").get("project_cleared") is True

        slot.project = str(tmp_path)
        slot.project_cleared = False
        _save(state, slot)
        assert "project_cleared" not in state.conversation_log._read_metadata("dashboard:s1")

    def test_the_marker_is_slot_owned_so_absence_retracts_it(self):
        """Owned, not carried. ``carry_unowned_metadata`` forwards an unowned key forever, so
        a carried marker would make the clear un-erasable: naming a project again would write
        no key and the forwarded ``true`` would keep the slot reading as cleared."""
        from kiro_crew.history import SLOT_OWNED_META_KEYS, carry_unowned_metadata

        assert "project_cleared" in SLOT_OWNED_META_KEYS
        rebuilt = {"project": "/now/chosen"}
        merged = carry_unowned_metadata(rebuilt, {"project_cleared": True}, SLOT_OWNED_META_KEYS)
        assert "project_cleared" not in merged


class TestTheBindingItselfRetractsTheMarker:
    """Enforced by the ``project`` setter rather than by each writer remembering the pairing.

    Ten call sites across seven modules bind a project. One that forgets to retract leaves the
    slot answering ``CWD_CLEARED``, so its side turns spawn in the default workspace instead of
    the directory just chosen -- the same wrong-tree harm this marker exists to prevent, and
    silent. The cases above cover the writers that exist today; these cover the enforcement
    point, which is what a writer added later inherits.
    """

    def test_binding_a_project_retracts_a_standing_clear(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project_cleared = True

        slot.project = str(tmp_path)

        assert slot.project_cleared is False
        assert slot.claim_cwd == str(tmp_path)

    def test_emptying_the_project_leaves_the_marker_to_its_writer(self):
        """Negative control, and the reason the retraction is keyed on a TRUTHY write: the clear
        paths empty the project and then state the marker, so retracting on an empty write would
        erase the very clear being recorded."""
        slot = _ChatSlot("test")
        slot.project_cleared = True

        slot.project = ""

        assert slot.project_cleared is True
        assert slot.claim_cwd == CWD_CLEARED

    def test_a_field_named_at_runtime_is_bound_through_the_same_setter(self, tmp_path):
        """The slot-control handler writes whichever field the request names, so the binding
        arrives without ``project`` appearing in the source at all -- a shape no writer-by-writer
        pairing can cover."""
        slot = _ChatSlot("test")
        slot.project_cleared = True
        field = "project"

        setattr(slot, field, str(tmp_path))

        assert slot.project_cleared is False

    def test_no_writer_retracts_the_marker_behind_the_setter(self):
        """A writer that retracts on its own answers "has a project" for a slot that has none.

        The binding a writer assigns is not always truthy -- an agent with no workspace
        preference and no folder, or ``default_project_dir`` on a missing workspace directory,
        both resolve to empty -- so an unconditional retraction beside the assignment states
        the opposite of the clear it just walked past. The setter is keyed on a truthy write
        and is therefore the only place the retraction is correct for every binding.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        setter = src / "dashboard" / "state.py"
        offenders: list[str] = []
        for path in src.rglob("*.py"):
            if path == setter:
                continue
            # Pinned: an unspecified encoding decodes with the locale's, and the
            # Windows runner's cp1252 has no mapping for the high bytes many of these
            # files carry, so the sweep raises instead of reporting offenders.
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "project_cleared = False" in stripped:
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these writers retract the cleared marker themselves instead of leaving it to the "
            f"``project`` setter, which only retracts for a truthy binding: {offenders}"
        )

    def test_no_hydration_path_restores_a_project_the_marker_retired(self):
        """A restore that pairs the stale ``project`` with the marker resurrects it anyway.

        Only ``claim_cwd`` consults the marker, so a consumer reading the field directly
        spawns its turn in the removed directory -- and a metadata upsert cannot delete a
        key, so every cleared slot's line carries both. Each hydration path therefore has
        to decide between them, which is what the ``elif`` spelling records.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # See the sweep above for why the encoding is pinned.
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if line.strip() != 'slot.project = meta["project"]':
                    continue
                guard = next(
                    (
                        prior.strip()
                        for prior in reversed(lines[: number - 1])
                        if prior.strip() and not prior.strip().startswith("#")
                    ),
                    "",
                )
                if not guard.startswith("elif "):
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these hydration paths restore the persisted project without first deciding "
            f"whether the cleared marker retired it: {offenders}"
        )


class TestEverySpawnSiteResolvesTheClaim:
    """The cleared sentinel is empty, so no spawn site may hand it straight over."""

    @pytest.mark.asyncio
    async def test_a_cleared_claim_resolves_to_the_session_default(self):
        from kiro_crew.config.loader import resolved_claim_cwd, session_default_cwd

        resolved = await resolved_claim_cwd(CWD_CLEARED, "slot:cleared")
        assert resolved == str(session_default_cwd("slot:cleared")), (
            "a cleared claim has to name the SAME directory the provider factory binds "
            "for this session, or the turn compares against one no provider uses"
        )
        # The point of resolving at all: what reaches the spawn is not falsy, so it
        # cannot land on the factory's "no cwd stated" branch.
        assert resolved

    @pytest.mark.asyncio
    async def test_a_chosen_directory_and_a_never_set_slot_pass_through(self):
        from kiro_crew.config.loader import resolved_claim_cwd

        assert await resolved_claim_cwd("/picked/by/user", "slot:set") == "/picked/by/user"
        # None states no requirement, which is what keeps the warm pool usable for a
        # slot that never had a project. Resolving it would bind every such slot.
        assert await resolved_claim_cwd(None, "slot:never") is None

    def test_no_spawn_site_hands_over_the_raw_project_field(self):
        """The main-chat spawns read the claim seam, not ``slot.project``.

        ``claim_cwd`` answers the cleared sentinel where the raw field answers an empty
        string, and the two are indistinguishable to a spawn: both are falsy. A site
        passing the field cannot tell "the user removed this directory" from "no
        directory was ever chosen", and only the first must refuse the stored cwd.

        Matched on the ATTRIBUTE ACCESS under any receiver, not the one spelling
        ``slot.project``: a site holding the slot in another variable reintroduces the
        collapse while a receiver-pinned sweep still reads clean. Scoped to the ``cwd``
        argument, because the same coalesce is CORRECT elsewhere -- ``claim_cwd`` is
        built from it, and the UI payloads carry it -- so a bare attribute match would
        flag a dozen sites that state no spawn directory.
        """
        import pathlib
        import re

        coalesce = re.compile(r"cwd\s*=\s*\w+\.project\s+or\s+None\b")
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # Encoding pinned for the same reason as the sweep above.
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if coalesce.search(stripped):
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these spawn sites state the raw project field as their cwd, so a cleared "
            f"slot is indistinguishable from one that never had a project: {offenders}"
        )

    def test_no_site_coalesces_the_claim_away(self):
        """The cleared sentinel is falsy, so coalescing a claim rebuilds the original bug.

        The defect this change fixes WAS a falsy-coalesce (``slot.project or None``), and
        ``claim_cwd`` answers a cleared slot with another falsy value. A site written
        ``slot.claim_cwd or None`` therefore collapses "removed" back into "never set" and
        lands on the stored-cwd branch again -- passing every behavioural test, because the
        two are indistinguishable once coalesced. The sentinel stays empty (that is the
        spelling persisted on the session line, so a truthy one would disagree with disk),
        which is what makes this the load-bearing pin instead.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        coalesced: list[str] = []
        readers: set[str] = set()
        resolvers: set[str] = set()
        for path in sorted(src.rglob("*.py")):
            # Encoding pinned for the same reason as the sweeps above.
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "claim_cwd or " in stripped:
                    coalesced.append(f"{path.relative_to(src)}:{number}")
                if ".claim_cwd" in stripped:
                    readers.add(str(path.relative_to(src)))
                if "resolved_claim_cwd(" in stripped:
                    resolvers.add(str(path.relative_to(src)))

        assert coalesced == [], (
            "these sites coalesce the claim, which turns a cleared slot back into one that "
            f"never had a project -- the exact collapse under repair: {coalesced}"
        )
        assert readers <= resolvers, (
            "these modules read the claim seam without ever handing it to the resolver, so "
            f"an unresolved cleared claim can reach a spawn: {sorted(readers - resolvers)}"
        )
