"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

import asyncio
import errno
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.sandbox import IDENTITY_UNAVAILABLE


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
        with patch(
            "kiro_crew.dashboard.chat_folders.is_sensitive_resolved_path", return_value=True
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unc", [r"\\evil\share\proj", "//evil/share/proj", r"\\?\UNC\evil\share\proj"]
    )
    async def test_a_unc_project_is_refused_before_realpath(self, monkeypatch, unc):
        """The project endpoint is the HTTP twin of the ``set_project`` directive:
        request-body path text reaching ``realpath``, which on a Windows gateway
        opens an SMB connection to a UNC host. It runs the folder endpoint's own
        lexical UNC refusal first (one helper across the admission sites), in
        the folder validator's 400 shape, audited; nothing is probed or set."""
        monkeypatch.setattr("kiro_crew.dashboard.chat_folders.unc_probe_allowed", lambda raw: False)
        touched = MagicMock(side_effect=AssertionError("filesystem touched for a UNC project"))
        monkeypatch.setattr("os.path.realpath", touched)
        monkeypatch.setattr("os.path.isdir", touched)
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/project", json={"project": unc})
                body = await resp.json()
        assert resp.status == 400, body
        assert body == {
            "error": "Project directory must not be a network (UNC) path",
            "code": "project_unc_path",
        }
        touched.assert_not_called()
        assert slot.project == ""
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "chat_slot_project"
        assert kwargs["outcome"] == "denied"
        assert "UNC" in kwargs["error"]

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

    @pytest.mark.asyncio
    async def test_a_same_spelling_rebind_of_a_replaced_directory_defers_a_reset(self, tmp_path):
        """GPT-caught: the deferred reset fired on a SPELLING change only while the
        identity record was refreshed unconditionally, so re-binding a directory
        replaced at its name (``rm -rf`` and re-clone) recorded the new
        ``(st_dev, st_ino)`` and armed nothing -- a live provider rooted in the old
        directory kept serving turns there, the identity being compared only at
        spawn. Same spelling, different identity: the reset flag is armed exactly
        as a spelling change arms it (deferred, never inline)."""
        from kiro_crew.dashboard.state import record_project_identity, spawn_project_identity

        proj = tmp_path / "proj"
        proj.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(proj)
        info = os.stat(proj)
        record_project_identity(slot, (info.st_dev, info.st_ino))  # the record a bind made
        state = _mock_state(slot)
        # The directory is replaced at its name: a sibling made while it stood
        # (another inode by construction) renamed over it.
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)
        replaced = os.stat(proj)
        assert (replaced.st_dev, replaced.st_ino) != spawn_project_identity(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(proj)},
                )
                assert resp.status == 200
        assert slot.project == str(proj)
        assert spawn_project_identity(slot) == (replaced.st_dev, replaced.st_ino)
        state.sessions.reset.assert_not_awaited()  # deferred, as for a spelling change
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_a_same_spelling_rebind_with_the_same_identity_stays_quiet(self, tmp_path):
        """An ordinary re-pin -- same spelling, the same identity read back -- arms
        no reset and resets nothing inline."""
        from kiro_crew.dashboard.state import record_project_identity, spawn_project_identity

        proj = tmp_path / "proj"
        proj.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(proj)
        info = os.stat(proj)
        record_project_identity(slot, (info.st_dev, info.st_ino))
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(proj)},
                )
                assert resp.status == 200
        assert spawn_project_identity(slot) == (info.st_dev, info.st_ino)
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


class TestTheProjectIdentityLivesForTheGatewayProcess:
    """``project_identity`` is recorded IN MEMORY for the gateway process's
    lifetime and never persisted: ``st_dev`` is reassigned across mounts on
    anonymous-device filesystems (a stored pair would refuse every spawn of an
    unchanged directory after a reboot) and the transcript store is
    agent-writable (a stored pair could be forged to pass the swap check). A
    project with no record in this process -- every slot after a restart -- is
    re-pinned at its first spawn by the same fenced pin the bind uses
    (``spawn_project_identity_repinned``); the record applies only while its
    spelling is the slot's current project (``spawn_project_identity``)."""

    def test_the_record_applies_to_its_spelling_and_nothing_else(self):
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            record_project_identity,
            spawn_project_identity,
        )

        slot = _ChatSlot("dashboard:src")
        slot.project = "/srv/proj"
        record_project_identity(slot, (64769, 1310725))
        assert slot.project_identity == ("/srv/proj", 64769, 1310725)
        assert spawn_project_identity(slot) == (64769, 1310725)
        slot.project = "/srv/other"  # re-pointed without a record: no record in this process
        assert spawn_project_identity(slot) is None
        record_project_identity(slot, (5, 6))
        assert slot.project_identity == ("/srv/other", 5, 6)
        record_project_identity(slot, None)
        assert slot.project_identity is None

    @pytest.mark.asyncio
    async def test_a_restart_re_pins_at_the_first_spawn_and_a_later_swap_is_refused(self, tmp_path):
        """After a restart the slot carries no record: the first spawn re-pins
        the bound path as it stands and passes on an unchanged directory; the
        record then holds for the process, so a swap after it is refused at the
        next spawn. A directory the re-pin cannot open (a link planted at the
        name across the restart) REFUSES the spawn; only a slot with no binding
        is left unexamined."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            spawn_project_identity,
            spawn_project_identity_repinned,
        )

        proj = tmp_path / "proj"
        proj.mkdir()
        info = os.stat(proj)
        slot = _ChatSlot("dashboard:restarted")
        slot.project = str(proj)  # restored from the transcript: the path only
        assert spawn_project_identity(slot) is None
        pinned = await spawn_project_identity_repinned(slot)
        assert pinned == (info.st_dev, info.st_ino)
        assert slot.project_identity == (str(proj), info.st_dev, info.st_ino)
        real, fd = sandbox.verify_agent_workspace_for_spawn(str(proj), pinned)
        sandbox.release_agent_workspace_fd(fd)
        assert os.path.realpath(real) == os.path.realpath(proj)
        # The swap, after the in-process pin: refused at the next spawn.
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)
        assert await spawn_project_identity_repinned(slot) == pinned  # the record holds
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="different directory"):
            sandbox.verify_agent_workspace_for_spawn(str(proj), pinned)
        # A LINK planted at the bound name across the restart: the re-pin
        # REFUSES (the governed reason and the remedy), never answers None --
        # a None would skip the check for every slot after a restart.
        linked = _ChatSlot("dashboard:linked")
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
            linked.project = str(link)
            expected_reason = "link or not a directory"
        except (OSError, NotImplementedError):
            linked.project = str(tmp_path / "gone")  # no link granted: a missing leaf
            expected_reason = "missing"
        with pytest.raises(sandbox.WorkspacePinFailed, match="re-bind the project directory") as ei:
            await spawn_project_identity_repinned(linked)
        assert expected_reason in str(ei.value) and "could not be re-pinned" in str(ei.value)
        assert linked.project_identity is None
        # A slot with no binding at all is the ONE shape that is not examined.
        unbound = _ChatSlot("dashboard:unbound")
        unbound.project = ""
        assert await spawn_project_identity_repinned(unbound) is None

    @pytest.mark.asyncio
    async def test_a_project_re_pointed_while_the_re_pin_ran_pins_the_new_spelling(
        self, tmp_path, monkeypatch
    ):
        """The re-pin runs off the loop; a directive that re-points ``project``
        in between must not leave a record naming the OLD spelling for the new
        directory: the new spelling is pinned instead."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        slot = _ChatSlot("dashboard:repointed")
        slot.project = str(first)
        real_pin = sandbox.directory_identity_pinned
        calls: list[str] = []

        def _pin_then_repoint(path):
            calls.append(str(path))
            identity = real_pin(path)
            if len(calls) == 1:
                slot.project = str(second)  # the re-point, mid-pin
            return identity

        monkeypatch.setattr(sandbox, "directory_identity_pinned", _pin_then_repoint)
        assert state_mod is not None
        info = os.stat(second)
        assert await spawn_project_identity_repinned(slot) == (info.st_dev, info.st_ino)
        assert slot.project_identity == (str(second), info.st_dev, info.st_ino)
        assert calls == [str(first), str(second)]

    def test_a_forged_transcript_line_is_ignored_by_the_restore(self, tmp_path):
        """Nothing identity-bearing is read back from the transcript store: a
        ``project_identity`` line an in-sandbox writer planted beside ``project``
        restores nothing, so the first spawn re-pins the directory as it stands
        instead of trusting the planted pair."""
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import (
            _apply_recent_session,
            _rehydrate_slot_from_history,
        )
        from kiro_crew.dashboard.state import spawn_project_identity

        state = _make_state(tmp_path)
        meta = {"project": str(tmp_path), "project_identity": [str(tmp_path), 7, 11]}
        _apply_recent_session(
            state,
            "chat-1-1700000000",
            "restored",
            {},
            meta,
            [],
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=None,
        )
        slot = state._slots["restored"]
        assert slot.project == str(tmp_path)
        assert slot.project_identity is None
        assert spawn_project_identity(slot) is None
        assert _rehydrate_slot_from_history is not None  # the other reader: same contract


def test_the_unavailable_identity_is_a_recorded_state_in_this_process() -> None:
    """A binding on a volume that reports no inode records
    ``IDENTITY_UNAVAILABLE`` -- a state the spawn opens but does not compare --
    never ``None``, for the process's lifetime; ``None`` stays what it is: no
    record in this process, which the first spawn re-pins."""
    from kiro_crew.dashboard.state import (
        _ChatSlot,
        record_project_identity,
        spawn_project_identity,
    )

    slot = _ChatSlot.__new__(_ChatSlot)
    slot.project = "/work/on-a-share"
    slot.project_identity = None
    record_project_identity(slot, IDENTITY_UNAVAILABLE)
    assert slot.project_identity == ("/work/on-a-share", 0, 0)
    assert spawn_project_identity(slot) == IDENTITY_UNAVAILABLE
    record_project_identity(slot, None)
    assert slot.project_identity is None and spawn_project_identity(slot) is None


class TestEveryPathWithoutAVerifiedIdentityRefuses:
    """THE CLASS, stated once: every path that cannot produce a verified
    identity REFUSES -- (a) a pin failure at bind, (b) an identity mismatch at
    spawn, (c) a failed re-pin at restart -- none answers ``None``, none logs
    and continues. ``None`` (the check skipped) is legal ONLY for a slot with no
    binding at all. One pin per path, named for the path."""

    def test_pin_failure_at_bind_refuses(self, tmp_path):
        """(a) The bind's pin of a missing leaf, a file, or a link raises
        ``WorkspacePinFailed``; nothing is recorded (the arms surface it)."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, record_project_identity

        slot = _ChatSlot("dashboard:bind")
        slot.project = str(tmp_path / "gone")
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be pinned"):
            record_project_identity(slot, sandbox.directory_identity_pinned(slot.project))
        assert slot.project_identity is None
        afile = tmp_path / "file"
        afile.write_text("x")
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(afile)

    def test_identity_mismatch_at_spawn_refuses(self, tmp_path):
        """(b) A different directory at the bound name refuses the spawn; the
        governed error names the remedy."""
        from kiro_crew import sandbox

        proj = tmp_path / "proj"
        proj.mkdir()
        info = os.stat(proj)
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)
        with pytest.raises(
            sandbox.AgentWorkspacePinRefused, match="different directory now sits"
        ) as ei:
            sandbox.verify_agent_workspace_for_spawn(str(proj), (info.st_dev, info.st_ino))
        assert "re-bind the project directory" in str(ei.value)

    @pytest.mark.asyncio
    async def test_failed_repin_at_restart_refuses(self, tmp_path):
        """(c) A bound directory that cannot be re-pinned after a restart -- a
        missing leaf here, a planted link in the runner pin -- raises the
        governed refusal; nothing is recorded, nothing is skipped."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        slot = _ChatSlot("dashboard:restarted")
        slot.project = str(tmp_path / "gone")
        slot.project_identity = None  # what a restart leaves
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned") as ei:
            await spawn_project_identity_repinned(slot)
        assert "re-bind the project directory" in str(ei.value)
        assert slot.project_identity is None

    @pytest.mark.asyncio
    async def test_unbound_slot_skips_the_check(self, tmp_path):
        """A slot with no binding at all is the ONE shape that is not examined:
        no re-pin, ``cwd_identity`` ``None``, and the spawn check passes the
        name through untouched."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        slot = _ChatSlot("dashboard:unbound")
        slot.project = ""
        assert await spawn_project_identity_repinned(slot) is None
        assert sandbox.verify_agent_workspace_for_spawn(str(tmp_path), None) == (
            str(tmp_path),
            None,
        )

    @pytest.mark.asyncio
    async def test_concurrent_restart_re_pins_keep_the_first_record(self, tmp_path, monkeypatch):
        """Two first spawns of one restored slot re-pin at the same time (the main
        chat and its side panel): both pass the no-record check, the first pin
        records, and the second -- which read the directory an agent replaced in
        between -- returns the established record instead of overwriting it, so
        the second spawn verifies against the original directory and refuses
        the replacement."""
        import threading

        from kiro_crew import sandbox
        from kiro_crew.config import loader
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        proj = tmp_path / "proj"
        proj.mkdir()
        slot = _ChatSlot("dashboard:restarted-twice")
        slot.project = str(proj)  # restored from the transcript: no record in this process
        monkeypatch.setattr(loader, "default_project_dir", lambda workspace=None: "")
        original, replacement = (11, 1001), (22, 2002)
        pins: list[str] = []
        started = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        def pin(path):
            index = len(pins)
            pins.append(path)
            started[index].set()
            assert release[index].wait(5), "the test never released this pin"
            return original if index == 0 else replacement

        monkeypatch.setattr(sandbox, "directory_identity_pinned", pin)
        first = asyncio.create_task(spawn_project_identity_repinned(slot))
        assert await asyncio.to_thread(started[0].wait, 5)
        second = asyncio.create_task(spawn_project_identity_repinned(slot))
        assert await asyncio.to_thread(started[1].wait, 5)  # both passed the no-record check
        release[0].set()
        assert await first == original
        assert slot.project_identity == (str(proj), *original)
        release[1].set()  # the second pin read the replaced directory
        assert await second == original, "the second re-pin overwrote the established identity"
        assert slot.project_identity == (str(proj), *original)
        assert pins == [str(proj), str(proj)]

    @pytest.mark.asyncio
    async def test_the_workspace_default_is_not_a_binding(self, tmp_path, monkeypatch):
        """The directory configuration hands a slot that chose none -- the
        workspace default, which is also the warm pool's own directory -- records
        nothing and is not examined: the re-pin answers ``None`` and nothing is
        pinned, so the spawn carries no identity and keeps the warm path."""
        from kiro_crew import sandbox
        from kiro_crew.config import loader
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        default = tmp_path / "workspace-default"
        default.mkdir()
        monkeypatch.setattr(loader, "default_project_dir", lambda workspace=None: str(default))
        pinned: list[str] = []
        monkeypatch.setattr(
            sandbox,
            "directory_identity_pinned",
            lambda path: pinned.append(str(path)) or (1, 1),
        )
        slot = _ChatSlot("dashboard:default")
        slot.project = str(default)  # chat_handlers' fallback for a chat with no project
        assert await spawn_project_identity_repinned(slot) is None
        assert slot.project_identity is None
        assert pinned == [], "the workspace default was pinned"


class TestBindRepinAndSpawnShareOneIdentityFunction:
    """Bind, restart re-pin and spawn verification share ONE identity function
    per platform (``sandbox.open_pinned_directory``): Windows, the root-first
    no-reparse component walk; POSIX, the ``O_NOFOLLOW | O_DIRECTORY`` leaf open
    + ``fstat``. So the three paths cannot disagree about a link: a junction at
    an ANCESTOR of the bound spelling is refused by all three on Windows without
    being resolved (review-caught: the restart re-pin opened the leaf by name and
    let the kernel resolve the ancestors), a leaf link is refused by all three on
    POSIX, and an unchanged directory passes all three with one identity."""

    @staticmethod
    def _windows_arm(monkeypatch, tmp_path):
        """The Windows arm on every host: on the Windows shard a real junction
        (``_winapi.CreateJunction``) and the real seams; elsewhere the arm through
        its ``platform_compat`` seams -- the no-follow open of a real directory,
        the attribute and tag reads answering the mount-point tag."""
        from kiro_crew import pinned_fs, sandbox

        pc = sandbox.platform_compat
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        real = tmp_path / "real"
        (real / "inside").mkdir(parents=True)
        junction = tmp_path / "junction"
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(real), str(junction))
        else:
            (junction / "inside").mkdir(parents=True)  # the junction object, as a real dir
            monkeypatch.setattr(pc, "IS_POSIX", False)
            monkeypatch.setattr(
                pc, "_win_open_without_following", lambda path: os.open(str(path), os.O_RDONLY)
            )
            reparse = pc._WIN_FILE_ATTRIBUTE_DIRECTORY | pc._WIN_FILE_ATTRIBUTE_REPARSE_POINT
            plain = pc._WIN_FILE_ATTRIBUTE_DIRECTORY
            junction_id = os.stat(junction).st_ino
            monkeypatch.setattr(
                pc,
                "_win_file_attributes",
                lambda fd: reparse if os.fstat(fd).st_ino == junction_id else plain,
            )
            monkeypatch.setattr(pc, "_win_reparse_tag", lambda fd: 0xA0000003)  # MOUNT_POINT
            ident = lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)  # noqa: E731
            monkeypatch.setattr(pc, "handle_identity", ident)
            monkeypatch.setattr(pinned_fs, "handle_identity", ident)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(pinned_fs, "_windows_handle_pin_available", lambda: True)
        return real / "inside", junction / "inside"

    @pytest.mark.asyncio
    async def test_a_windows_junction_ancestor_is_refused_at_bind_repin_and_spawn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        unchanged, under_junction = self._windows_arm(monkeypatch, tmp_path)
        # bind
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(under_junction)
        # restart re-pin
        slot = _ChatSlot("dashboard:win-repin")
        slot.project = str(under_junction)
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned"):
            await spawn_project_identity_repinned(slot)
        assert slot.project_identity is None
        # spawn
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="component above it"):
            sandbox.verify_agent_workspace_for_spawn(str(under_junction), (7, 11))
        # the unchanged directory passes all three with ONE identity
        identity = sandbox.directory_identity_pinned(unchanged)
        assert identity not in (None, sandbox.IDENTITY_UNAVAILABLE)
        good = _ChatSlot("dashboard:win-good")
        good.project = str(unchanged)
        assert await spawn_project_identity_repinned(good) == identity
        real, hold = sandbox.verify_agent_workspace_for_spawn(str(unchanged), identity)
        assert real == str(unchanged)
        # The spawn verification keeps the pinned chain as its hold (open,
        # root-first) until the child exists; bind and re-pin released theirs.
        assert isinstance(hold, list) and len(hold) > 1
        for fd in hold:
            os.fstat(fd)
        sandbox.release_agent_workspace_fd(hold)
        for fd in hold:
            with pytest.raises(OSError):
                os.fstat(fd)

    @pytest.mark.asyncio
    async def test_a_posix_leaf_link_is_refused_at_bind_repin_and_spawn(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity_repinned

        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # No link granted on this host: the kernel's answer to a no-follow
            # open of a link (ELOOP), handed to THE identity read.
            link.mkdir()
            monkeypatch.setattr(
                sandbox,
                "open_pinned_directory",
                lambda path: (_ for _ in ()).throw(OSError(errno.ELOOP, "link", path)),
            )
        # bind
        with pytest.raises(sandbox.WorkspacePinFailed, match="link or not a directory"):
            sandbox.directory_identity_pinned(link)
        # restart re-pin
        slot = _ChatSlot("dashboard:posix-repin")
        slot.project = str(link)
        with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned"):
            await spawn_project_identity_repinned(slot)
        # spawn
        with pytest.raises(sandbox.AgentWorkspacePinRefused, match="now a link"):
            sandbox.verify_agent_workspace_for_spawn(str(link), (7, 11))
        # the unchanged directory passes all three with ONE identity
        monkeypatch.undo()
        identity = sandbox.directory_identity_pinned(target)
        info = os.stat(target)
        assert identity == (info.st_dev, info.st_ino)
        good = _ChatSlot("dashboard:posix-good")
        good.project = str(target)
        assert await spawn_project_identity_repinned(good) == identity
        real, fd = sandbox.verify_agent_workspace_for_spawn(str(target), identity)
        sandbox.release_agent_workspace_fd(fd)
        assert os.path.realpath(real) == os.path.realpath(target)


class TestEverySpawnOfABoundSlotVerifiesThroughOneSeam:
    """Every spawn of a bound slot -- the main chat, the side panel, a crewmate
    thread, and any producer written later -- verifies the project identity
    through ONE seam: the allocation body behind ``SessionManager.get_or_create``
    resolves a ``cwd`` handed over with no ``cwd_identity`` against the slots
    bound to it (``DashboardState.wire_session_cwd_identity_resolver`` ->
    ``state.bound_project_identity_for_spawn``) once the directory the spawn
    enters is final, before the warm-pool decision and before the provider
    factory sees it. Bound directory + ``None`` -> the slot's identity (derived;
    re-pinned in this process when the slot carries none) or the governed
    refusal; a directory no slot is bound to + ``None`` -> ``None``, not examined
    (review-caught: two spawn sites passed ``cwd`` with no identity and entered a
    swapped directory the main chat refused)."""

    @staticmethod
    def _manager(captured: dict):
        """A real ``SessionManager`` over a factory that records its kwargs."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            return m

        return SessionManager(cfg, provider_factory=factory)

    @staticmethod
    def _bound(tmp_path, key: str, name: str = "proj"):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, record_project_identity

        proj = tmp_path / name
        proj.mkdir(exist_ok=True)
        slot = _ChatSlot(key)
        slot.project = str(proj)
        record_project_identity(slot, sandbox.directory_identity_pinned(proj))
        return slot, proj

    @pytest.mark.asyncio
    async def test_a_bound_directory_and_no_identity_is_derived_at_the_seam(self, tmp_path):
        """A producer that names a bound directory and passes NO identity gets
        the bound slot's record derived at the manager: the factory -- and so
        the spawn -- receives it, not ``None``."""
        from kiro_crew.dashboard.state import (
            bound_project_identity_for_spawn,
            spawn_project_identity,
        )

        slot, proj = self._bound(tmp_path, "chat-1-1")
        state = _mock_state(slot)
        captured: dict = {}
        mgr = self._manager(captured)
        mgr.set_cwd_identity_resolver(
            lambda key, cwd: bound_project_identity_for_spawn(state, key, cwd)
        )
        try:
            await mgr.get_or_create("side:chat-1-1:g1", cwd=str(proj))  # no cwd_identity
        finally:
            await mgr.close_all()
        recorded = spawn_project_identity(slot)
        assert recorded is not None
        assert captured["cwd_identity"] == recorded

    @pytest.mark.asyncio
    async def test_a_record_less_bound_directory_is_re_pinned_at_the_seam(self, tmp_path):
        """After a restart the bound slot carries no record: the seam re-pins the
        directory as it stands (the same fenced pin the bind uses) and records
        it on the slot -- the spawn receives a real identity."""
        from kiro_crew.dashboard.state import bound_project_identity_for_spawn

        slot, proj = self._bound(tmp_path, "chat-1-2")
        slot.project_identity = None  # what a restart leaves
        info = os.stat(proj)
        state = _mock_state(slot)
        captured: dict = {}
        mgr = self._manager(captured)
        mgr.set_cwd_identity_resolver(
            lambda key, cwd: bound_project_identity_for_spawn(state, key, cwd)
        )
        try:
            await mgr.get_or_create("thread:chat-1-2:m1", cwd=str(proj))
        finally:
            await mgr.close_all()
        assert captured["cwd_identity"] == (info.st_dev, info.st_ino)
        assert slot.project_identity == (str(proj), info.st_dev, info.st_ino)

    @pytest.mark.asyncio
    async def test_a_bound_directory_that_cannot_be_pinned_is_refused_at_the_seam(self, tmp_path):
        """Bound slot + ``None`` + a directory the pin refuses (a file -- or a
        link -- now at the bound name, the slot record-less): the seam RAISES the
        governed refusal before anything is allocated; the factory never runs."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, bound_project_identity_for_spawn

        planted = tmp_path / "planted"
        planted.write_text("not a directory")
        slot = _ChatSlot("chat-1-3")
        slot.project = str(planted)
        slot.project_identity = None
        state = _mock_state(slot)
        captured: dict = {}
        mgr = self._manager(captured)
        mgr.set_cwd_identity_resolver(
            lambda key, cwd: bound_project_identity_for_spawn(state, key, cwd)
        )
        try:
            with pytest.raises(sandbox.WorkspacePinFailed, match="could not be re-pinned") as ei:
                await mgr.get_or_create("side:chat-1-3:g1", cwd=str(planted))
        finally:
            await mgr.close_all()
        assert "re-bind the project directory" in str(ei.value)
        assert captured == {}, "the factory ran for a refused spawn"
        assert mgr.get_provider("side:chat-1-3:g1") is None

    @pytest.mark.asyncio
    async def test_an_unbound_directory_and_no_identity_passes_through_unexamined(self, tmp_path):
        """A directory no slot is bound to is the ONE shape the seam leaves
        alone: ``cwd_identity`` stays ``None`` and the spawn is not examined."""
        from kiro_crew.dashboard.state import bound_project_identity_for_spawn

        slot, _proj = self._bound(tmp_path, "chat-1-4")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        state = _mock_state(slot)
        captured: dict = {}
        mgr = self._manager(captured)
        mgr.set_cwd_identity_resolver(
            lambda key, cwd: bound_project_identity_for_spawn(state, key, cwd)
        )
        try:
            await mgr.get_or_create("cron:job-1", cwd=str(elsewhere))
        finally:
            await mgr.close_all()
        assert captured["cwd_identity"] is None

    @pytest.mark.asyncio
    async def test_another_chats_binding_of_the_workspace_default_fences_only_that_chat(
        self, tmp_path, monkeypatch
    ):
        """The workspace default is configuration, not a binding, so a record ONE
        chat put on it -- it named the default directory explicitly through the
        project endpoint -- is consulted for that chat's own spawns (the main
        chat, its side panel, its threads) and for nobody else's: every other
        chat standing on the default with no record, and every producer that
        spawns there, resolves ``None`` and stays unexamined (review-caught: the
        resolver matched every slot on the spelling, so one explicit binding of
        the default made every default chat verify a stranger's identity and
        cold-start past the warm pool)."""
        from kiro_crew.config import loader
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            bound_project_identity_for_spawn,
            spawn_project_identity,
        )

        default = tmp_path / "workspace-default"
        default.mkdir()
        monkeypatch.setattr(loader, "default_project_dir", lambda workspace=None: str(default))
        bound, _ = self._bound(tmp_path, "chat-40-1", name="workspace-default")  # explicit
        plain = _ChatSlot("chat-40-2")
        plain.project = str(default)  # chat_handlers' fallback: records nothing
        state = _mock_state(bound)
        state._slots[plain.key] = plain
        own = spawn_project_identity(bound)
        assert own is not None
        for key in ("chat-40-1", "side:chat-40-1", "side:chat-40-1:g1", "thread:chat-40-1:m1"):
            assert await bound_project_identity_for_spawn(state, key, str(default)) == own, key
        for key in ("chat-40-2", "side:chat-40-2:g1", "thread:chat-40-2:m1", "cron:job-1"):
            assert await bound_project_identity_for_spawn(state, key, str(default)) is None, key
        assert plain.project_identity is None, "the default chat was pinned"

    @pytest.mark.asyncio
    async def test_an_identity_the_producer_passes_is_the_one_the_spawn_gets(self, tmp_path):
        """The producers pass the identity themselves (``chat_runner``, the side
        panel, the thread turn); the seam is the safety net under them and never
        overrides an identity handed over."""
        from kiro_crew.dashboard.state import bound_project_identity_for_spawn

        slot, proj = self._bound(tmp_path, "chat-1-5")
        state = _mock_state(slot)
        captured: dict = {}
        mgr = self._manager(captured)
        consulted: list[str] = []

        async def _resolver(key, cwd):
            consulted.append(key)
            return await bound_project_identity_for_spawn(state, key, cwd)

        mgr.set_cwd_identity_resolver(_resolver)
        try:
            await mgr.get_or_create("chat-1-5", cwd=str(proj), cwd_identity=(7, 11))
        finally:
            await mgr.close_all()
        assert captured["cwd_identity"] == (7, 11)
        assert consulted == []

    @pytest.mark.asyncio
    async def test_disagreeing_bindings_of_one_directory_refuse(self, tmp_path):
        """Two slots bound to one spelling whose records disagree -- the
        directory was replaced between their bindings, so one is stale -- refuse
        a spawn that names no identity: the seam cannot tell whose spawn it is,
        and the remedy is the re-bind."""
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import (
            _ChatSlot,
            bound_project_identity_for_spawn,
            record_project_identity,
        )

        first, proj = self._bound(tmp_path, "chat-2-1")
        other = tmp_path / ".proj.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)  # replaced between the two bindings
        second = _ChatSlot("chat-2-2")
        second.project = str(proj)
        record_project_identity(second, sandbox.directory_identity_pinned(proj))
        assert first.project_identity != second.project_identity
        state = _mock_state(first)
        state._slots[second.key] = second
        with pytest.raises(sandbox.WorkspacePinFailed, match="records disagree") as ei:
            await bound_project_identity_for_spawn(state, "side:chat-2-1:g1", str(proj))
        assert "re-bind the project directory" in str(ei.value)

    @pytest.mark.asyncio
    async def test_the_dashboard_state_installs_the_seam_when_it_is_built(self, tmp_path):
        """The seam is wired in ``DashboardState.__init__`` -- both the gateway
        and the standalone dashboard build the state there, so no boot path can
        leave the door open -- and the resolver it installs answers from the
        state's own slots: a bound slot's record, ``None`` for an unbound
        directory."""
        from chat_test_helpers import _make_state

        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import record_project_identity, spawn_project_identity

        state = _make_state(tmp_path)
        state.sessions.set_cwd_identity_resolver.assert_called_once()
        resolver = state.sessions.set_cwd_identity_resolver.call_args.args[0]
        proj = tmp_path / "proj"
        proj.mkdir()
        slot = state.get_or_create_slot("chat-1")
        slot.project = str(proj)
        record_project_identity(slot, sandbox.directory_identity_pinned(proj))
        assert await resolver("side:chat-1:g1", str(proj)) == spawn_project_identity(slot)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert await resolver("cron:x", str(elsewhere)) is None


class TestABoundSlotNeverReceivesAPooledProcess:
    """A bound slot never receives a pre-spawned or pooled process; its spawn
    always passes identity verification.

    The warm pool pre-spawns children into the pool's own directory
    (``default_project_dir()``) with no binding to verify against, and a claim
    hands one out without a spawn -- so the seam, which sits at the spawn, never
    saw it (review-caught: a slot bound to the pool's directory claimed a pooled
    child; with the pool refilled after a swap, no detection and no recovery). The
    allocation body therefore resolves the identity BEFORE the pool decision and a
    spawn carrying one never claims (``bypass_cwd_identity``): a bound directory
    is entered by a cold-started child that verifies it, or refused; an unbound
    slot, which carries no identity, keeps the warm path. The two other paths
    that hand out a process without a producer naming the directory are pinned
    here too: the eager respawn after a hard stop (``_eager_respawn`` names no
    ``cwd``; the body restores the session map's and resolves on it) and the task
    run's shared-runtime bootstrap (the one factory call outside
    ``get_or_create``, resolved through the same resolver).
    """

    @staticmethod
    def _manager(captured: dict, *, pool_cwd: str, pooled):
        """A real ``SessionManager`` with the warm pool ENABLED and one pooled child.

        The factory records its kwargs, and the provider it builds verifies the
        working directory at ``start()`` exactly as the ACP spawn sites do
        (``sandbox.verify_agent_workspace_for_spawn`` against the identity the
        factory received) -- a stand-in for the check pinned on the real sites in
        ``test_sandbox_argv.py`` / ``test_acp_client.py``, so a cold start that
        reaches the factory is a VERIFIED start and a claim that skips it is not.
        """
        import time

        from kiro_crew import sandbox
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()

            async def _start(*_a, **_k):
                _real, hold = sandbox.verify_agent_workspace_for_spawn(
                    kwargs["cwd"], kwargs.get("cwd_identity")
                )
                # The real sites release the hold (the POSIX descriptor, the Windows
                # chain) once the child exists; this stand-in spawns no child, so it
                # releases right after the check -- a held Windows chain would
                # otherwise keep the directory from being renamed or removed by the
                # test that swaps it.
                sandbox.release_agent_workspace_fd(hold)

            m.start = AsyncMock(side_effect=_start)
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        mgr._pool_size = 2
        mgr._pool_cwd = pool_cwd
        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        return mgr

    @staticmethod
    def _pooled():
        m = AsyncMock()
        m.is_alive.return_value = True
        m.context_usage_pct = lambda: 0.0
        m.shutdown = AsyncMock()
        return m

    @staticmethod
    def _bound(tmp_path, key: str):
        from kiro_crew import sandbox
        from kiro_crew.dashboard.state import _ChatSlot, record_project_identity

        proj = tmp_path / "pool-project"
        proj.mkdir(exist_ok=True)
        slot = _ChatSlot(key)
        slot.project = str(proj)
        record_project_identity(slot, sandbox.directory_identity_pinned(proj))
        return slot, proj

    @staticmethod
    def _install(mgr, state):
        from kiro_crew.dashboard.state import bound_project_identity_for_spawn

        mgr.set_cwd_identity_resolver(
            lambda key, cwd: bound_project_identity_for_spawn(state, key, cwd)
        )

    @staticmethod
    def _swap(proj):
        other = proj.parent / ".pool-project.other"
        other.mkdir()
        proj.rmdir()
        other.rename(proj)  # a different directory now sits at the bound name

    @pytest.mark.asyncio
    async def test_a_bound_slot_with_the_pool_enabled_and_a_swapped_directory_is_refused(
        self, tmp_path
    ):
        """Pool enabled, the slot bound to the pool's own directory, the directory
        swapped since the binding: the spawn does NOT claim the pooled child (the
        old path handed it out with no check) -- it cold-starts through the seam
        and the verification refuses it; the pooled child stays in the pool."""
        from kiro_crew import sandbox

        slot, proj = self._bound(tmp_path, "chat-39-1")
        state = _mock_state(slot)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, key: decisions.append(decision)
        self._swap(proj)
        try:
            with pytest.raises(sandbox.AgentWorkspacePinRefused, match="re-bind"):
                await mgr.get_or_create("chat-39-1", cwd=str(proj))
            warm = mgr.warm_providers()
        finally:
            await mgr.close_all()
        assert decisions == ["bypass_cwd_identity"]
        assert captured["cwd_identity"] is not None, "the cold start carried no identity"
        assert mgr.get_provider("chat-39-1") is None
        assert warm == [pooled], "the pooled child was handed out or dropped"

    @pytest.mark.asyncio
    async def test_a_bound_slot_with_the_pool_enabled_and_an_intact_directory_cold_starts(
        self, tmp_path
    ):
        """Same slot, directory unchanged: the spawn cold-starts VERIFIED (the
        factory receives the slot's identity and the start passes) and the pool is
        untouched -- the pooled child is neither claimed nor discarded."""
        from kiro_crew.dashboard.state import spawn_project_identity

        slot, proj = self._bound(tmp_path, "chat-39-2")
        state = _mock_state(slot)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, key: decisions.append(decision)
        try:
            provider, is_new, _ = await mgr.get_or_create("chat-39-2", cwd=str(proj))
            warm = mgr.warm_providers()
        finally:
            await mgr.close_all()
        assert decisions == ["bypass_cwd_identity"]
        assert is_new and provider is not pooled
        assert captured["cwd_identity"] == spawn_project_identity(slot)
        provider.start.assert_awaited_once()
        assert warm == [pooled], "the pooled child was claimed or discarded"

    @pytest.mark.asyncio
    async def test_an_unbound_slot_with_the_pool_enabled_is_served_the_warm_claim(self, tmp_path):
        """No slot is bound to the pool's directory: the spawn carries no identity
        and the warm claim is served exactly as before -- the pooled child, no
        factory call."""
        proj = tmp_path / "pool-project"
        proj.mkdir()
        state = _mock_state(None)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, key: decisions.append(decision)
        try:
            provider, _, _ = await mgr.get_or_create("chat-39-3", cwd=str(proj))
            warm = mgr.warm_providers()
        finally:
            await mgr.close_all()
        assert decisions == ["hit"]
        assert provider is pooled
        assert captured == {}, "the warm claim ran the factory"
        assert warm == []

    @pytest.mark.asyncio
    async def test_a_chat_on_the_workspace_default_is_served_the_warm_claim(
        self, tmp_path, monkeypatch
    ):
        """A new chat with no project stands on the workspace default, which is
        the pool's own directory, with no record: that is configuration, not a
        binding, so the seam resolves no identity for it and the warm claim is
        served -- the pooled child, no factory call, nothing pinned."""
        from kiro_crew.config import loader
        from kiro_crew.dashboard.state import _ChatSlot

        proj = tmp_path / "pool-project"
        proj.mkdir()
        monkeypatch.setattr(loader, "default_project_dir", lambda workspace=None: str(proj))
        slot = _ChatSlot("chat-39-5")
        slot.project = str(proj)  # chat_handlers' fallback: records nothing
        state = _mock_state(slot)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, key: decisions.append(decision)
        try:
            provider, _, _ = await mgr.get_or_create("chat-39-5", cwd=str(proj))
            warm = mgr.warm_providers()
        finally:
            await mgr.close_all()
        assert decisions == ["hit"]
        assert provider is pooled
        assert captured == {}, "the warm claim ran the factory"
        assert warm == []
        assert slot.project_identity is None, "the workspace default was pinned"

    @pytest.mark.asyncio
    async def test_another_chats_binding_of_the_pool_directory_leaves_the_default_chat_warm(
        self, tmp_path, monkeypatch
    ):
        """One chat binds the workspace default -- the pool's own directory --
        explicitly, so its own spawn verifies and cold-starts; a second chat
        standing on the default with no record is a stranger to that binding:
        the seam resolves no identity for it and the warm claim is served
        (review-caught: the stranger's record was matched, so one explicit
        binding of the default cold-started every default chat)."""
        from kiro_crew.config import loader
        from kiro_crew.dashboard.state import _ChatSlot, spawn_project_identity

        bound, proj = self._bound(tmp_path, "chat-40-1")  # the pool directory, bound explicitly
        monkeypatch.setattr(loader, "default_project_dir", lambda workspace=None: str(proj))
        plain = _ChatSlot("chat-40-2")
        plain.project = str(proj)  # chat_handlers' fallback: records nothing
        state = _mock_state(bound)
        state._slots[plain.key] = plain
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, key: decisions.append(decision)
        try:
            provider, _, _ = await mgr.get_or_create("chat-40-2", cwd=str(proj))
            assert provider is pooled, "the default chat was cold-started"
            assert captured == {}, "the default chat's warm claim ran the factory"
            await mgr.get_or_create("chat-40-1", cwd=str(proj))
        finally:
            await mgr.close_all()
        assert decisions == ["hit", "bypass_cwd_identity"]
        assert captured["cwd_identity"] == spawn_project_identity(bound)
        assert plain.project_identity is None, "the default chat was pinned"

    @pytest.mark.asyncio
    async def test_the_eager_respawn_is_resolved_on_the_directory_the_session_map_restores(
        self, tmp_path
    ):
        """``_eager_respawn`` (after a hard stop) names no ``cwd``: the allocation
        body restores the session map's directory for the resume, and the
        identity is resolved on THAT directory -- the factory receives the bound
        slot's identity, and with the directory swapped since the binding the
        respawn is refused (logged) and no session stands."""
        from kiro_crew.dashboard.state import spawn_project_identity

        slot, proj = self._bound(tmp_path, "chat-39-4")
        state = _mock_state(slot)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        # A non-default backend label: the default label's entry is pruned when
        # no transcript file stands on disk, and this test owns no kiro-cli
        # transcript. The label does not matter to the seam; the restored ``cwd`` does.
        mgr.seed_conversation("chat-39-4", "sid-39-4", provider="kas", cwd=str(proj))
        try:
            await mgr._eager_respawn("chat-39-4")
            assert captured["cwd"] == str(proj)
            assert captured["cwd_identity"] == spawn_project_identity(slot)
            assert mgr.has_session("chat-39-4")
            await mgr.reset("chat-39-4")
            captured.clear()
            # The stored conversation again (the mock provider's label made the
            # first start clear the sid as a backend switch), then the swap.
            mgr.seed_conversation("chat-39-4", "sid-39-4b", provider="kas", cwd=str(proj))
            self._swap(proj)
            await mgr._eager_respawn("chat-39-4")  # refused inside, logged, never raised
            respawned = mgr.has_session("chat-39-4")
            warm = mgr.warm_providers()
        finally:
            await mgr.close_all()
        assert captured["cwd_identity"] == spawn_project_identity(slot)
        assert not respawned, "the swapped directory was respawned into"
        assert warm == [pooled]

    @pytest.mark.asyncio
    async def test_the_run_runtime_bootstrap_carries_a_bound_directorys_identity(self, tmp_path):
        """The task run's shared-runtime bootstrap is the one factory call outside
        ``get_or_create`` that spawns into a caller-named directory: a work
        directory a slot is bound to reaches the factory with that binding's
        identity; a directory no slot is bound to reaches it as before (no
        ``cwd_identity`` at all)."""
        from kiro_crew.dashboard.state import spawn_project_identity

        slot, proj = self._bound(tmp_path, "chat-39-5")
        state = _mock_state(slot)
        captured: dict = {}
        pooled = self._pooled()
        mgr = self._manager(captured, pool_cwd=str(proj), pooled=pooled)
        self._install(mgr, state)
        elsewhere = tmp_path / "run-dir"
        elsewhere.mkdir()
        try:
            await mgr._get_or_bootstrap_run_runtime("taskrunner:run-1", cwd=str(proj))
            assert captured["cwd_identity"] == spawn_project_identity(slot)
            captured.clear()
            await mgr._get_or_bootstrap_run_runtime("taskrunner:run-2", cwd=str(elsewhere))
            assert "cwd_identity" not in captured
        finally:
            await mgr.close_all()
