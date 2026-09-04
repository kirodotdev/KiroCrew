"""Unit tests for workspace CRUD HTTP handlers.

Focused on covering the handler code paths that Coverlay flagged as uncovered.
Tests call the actual async handler functions with mock requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
)
from kiro_crew.dashboard.handlers import (
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

# ── Helpers ──


def _req(body: dict | None = None, match_info: dict | None = None) -> web.Request:
    """Build a mocked aiohttp.web.Request carrying real body bytes.

    The three handlers are owner-gated, and the gate reads ``request.app["state"]``
    plus the claims the token-auth middleware normally populates. Without them
    every test here would answer on the gate rather than on the branch it names,
    so the request is dressed as the owner. The gate's own refusal behaviour is
    covered at route level in ``test_workspace_member_thread_owner_gate``.
    """
    # ``body=None`` means "malformed JSON": the capped read parses the bytes
    # itself, so the malformed case is expressed as malformed bytes.
    raw = json.dumps(body).encode() if body is not None else b"{bad json"
    app = web.Application()
    app["state"] = NoConfiguredOwner()
    request = make_mocked_request(
        "POST",
        "/api/workspaces",
        match_info=match_info or {},
        headers={"Content-Length": str(len(raw))},
        payload=BodyStreamPayload(raw),
        app=app,
    )
    request["user"] = "local-app"
    request["app"] = ""
    return request


def _cfg(**kw: object) -> KiroCrewConfig:
    defaults: dict = {
        "workspaces": {"default": WorkspaceConfig(dir="workspace")},
        "default_workspace": "default",
        "agents": {},
    }
    defaults.update(kw)
    return KiroCrewConfig(**defaults)


_SEL = "kiro_crew.sel.sel"
_LOAD = "kiro_crew.config.loader.KiroCrewConfig.load"
_CFGDIR = "kiro_crew.config.loader.config_dir"
_DATAHOME = "kiro_crew.dashboard.handlers.files.data_home"


def _seed_file(directory: Path, cfg: KiroCrewConfig) -> Path:
    """Write *cfg*'s document shape as the on-disk config.

    The handlers persist via a delta read-modify-write on the RAW document
    inside ``update_config_locked`` (#4767), so the authoritative post-call
    state is the FILE at ``config_dir()/config.json`` -- not the ``cfg``
    object handed to the handler, which is only its pre-await snapshot.
    """
    from dataclasses import asdict

    doc = {
        "workspaces": {n: asdict(w) for n, w in cfg.workspaces.items()},
        "default_workspace": cfg.default_workspace,
        "agents": {n: asdict(a) for n, a in cfg.agents.items()},
    }
    path = directory / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _read_doc(directory: Path) -> dict:
    return json.loads((directory / "config.json").read_text(encoding="utf-8"))


# ── Create handler ──


class TestCreateHandler:
    """Cover api_workspaces_create code paths."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        resp = await api_workspaces_create(_req(body=None))
        assert resp.status == 400
        assert b"invalid JSON" in resp.body

    @pytest.mark.asyncio
    async def test_empty_name_returns_400(self) -> None:
        resp = await api_workspaces_create(_req({"name": "  "}))
        assert resp.status == 400
        assert b"required" in resp.body

    @pytest.mark.asyncio
    async def test_invalid_name_returns_400(self) -> None:
        resp = await api_workspaces_create(_req({"name": "../bad"}))
        assert resp.status == 400
        assert b"Invalid workspace name" in resp.body

    @pytest.mark.asyncio
    async def test_duplicate_name_returns_409(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_create(_req({"name": "default"}))
        assert resp.status == 409
        assert b"already exists" in resp.body

    @pytest.mark.asyncio
    async def test_copy_from_missing_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_create(_req({"name": "new1", "copy_from": "nonexistent"}))
        assert resp.status == 404
        assert b"not found" in resp.body

    @pytest.mark.asyncio
    async def test_create_success(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_create(_req({"name": "staging"}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["name"] == "staging"
        doc = _read_doc(tmp_path)
        assert doc["workspaces"]["staging"]["dir"] == "workspace-staging"
        assert "default" in doc["workspaces"], "the delta write dropped an unrelated entry"
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_with_copy_from(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        src_dir = tmp_path / "workspace"
        src_dir.mkdir()
        (src_dir / "data.txt").write_text("hello")
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert resp.status == 200
        assert "copied" in _read_doc(tmp_path)["workspaces"]
        # Verify the copy happened (symlinks=True means it's a real copy)
        dst = tmp_path / "workspace-copied" / "data.txt"
        assert dst.exists()
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staged copy not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_create_path_traversal_rejected(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
        ):
            resp = await api_workspaces_create(_req({"name": "evil", "dir": "../../etc"}))
        assert resp.status == 400
        assert b"Invalid directory" in resp.body


# ── Update handler ──


class TestUpdateHandler:
    """Cover api_workspaces_update code paths."""

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_update(
                _req({"dir": "x"}, match_info={"name": "nonexistent"})
            )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_update(_req(body=None, match_info={"name": "default"}))
        assert resp.status == 400
        assert b"invalid JSON" in resp.body

    @pytest.mark.asyncio
    async def test_update_dir_success(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_update(
                _req({"dir": "new-dir"}, match_info={"name": "default"})
            )
        assert resp.status == 200
        assert _read_doc(tmp_path)["workspaces"]["default"]["dir"] == "new-dir"
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_path_traversal_rejected(self, tmp_path: Path) -> None:
        cfg = _cfg()
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
        ):
            resp = await api_workspaces_update(
                _req({"dir": "../../etc"}, match_info={"name": "default"})
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_no_dir_is_noop(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_update(
                _req({"other": "field"}, match_info={"name": "default"})
            )
        assert resp.status == 200
        assert _read_doc(tmp_path)["workspaces"]["default"]["dir"] == "workspace"


# ── Delete handler ──


class TestDeleteHandler:
    """Cover api_workspaces_delete code paths."""

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "nonexistent"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_default_returns_409(self) -> None:
        cfg = _cfg()
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "default"}))
        assert resp.status == 409
        assert b"default workspace" in resp.body

    @pytest.mark.asyncio
    async def test_delete_referenced_returns_409(self) -> None:
        cfg = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
            agents={"bot": KiroCrewAgentConfig(workspace="staging")},
        )
        with patch(_LOAD, return_value=cfg):
            resp = await api_workspaces_delete(_req(match_info={"name": "staging"}))
        assert resp.status == 409
        assert b"referenced by agents" in resp.body

    @pytest.mark.asyncio
    async def test_delete_success(self, tmp_path: Path) -> None:
        cfg = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
        )
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_SEL) as mock_sel,
        ):
            resp = await api_workspaces_delete(_req(match_info={"name": "staging"}))
        assert resp.status == 200
        doc = _read_doc(tmp_path)
        assert "staging" not in doc["workspaces"]
        assert "default" in doc["workspaces"], "the delta write dropped an unrelated entry"
        mock_sel().log_api_access.assert_called_once()


# ── In-lock precondition re-check (#4767 review rounds 1-3) ──


class TestInLockRecheck:
    """The state-dependent preconditions are re-decided against FRESH state.

    The handlers validate on a snapshot loaded before their awaits (create's
    window straddles an awaited copytree that can run for seconds). Two
    overlapping owner requests could both pass those stale checks; the delta
    mutate inside update_config_locked must therefore re-run the decisive
    checks against the DOCUMENT as read inside the sidecar lock, and surface
    a conflict instead of persisting a duplicate directory or a dangling
    reference. Each test hands the handler a stale snapshot that passes and
    seeds the on-disk document with fresh state that must refuse -- red
    against a mutate that re-applies the mutation without re-checking.
    """

    @pytest.mark.asyncio
    async def test_create_rechecks_name_collision_inside_the_lock(self, tmp_path: Path) -> None:
        stale = _cfg()  # no "staging" -> handler checks pass
        fresh = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),  # concurrent create won
            },
        )
        _seed_file(tmp_path, fresh)
        with (
            patch(_LOAD, return_value=stale),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "staging"}))
        assert resp.status == 409
        assert b"already exists" in resp.body
        assert _read_doc(tmp_path)["workspaces"]["staging"]["dir"] == "workspace-staging"

    @pytest.mark.asyncio
    async def test_create_rechecks_dir_collision_inside_the_lock(self, tmp_path: Path) -> None:
        stale = _cfg()
        fresh = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "other": WorkspaceConfig(dir="workspace-staging"),  # same dir, other name
            },
        )
        _seed_file(tmp_path, fresh)
        with (
            patch(_LOAD, return_value=stale),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(
                _req({"name": "staging", "dir": "workspace-staging"})
            )
        assert resp.status == 409
        assert b"already used" in resp.body
        assert "staging" not in _read_doc(tmp_path)["workspaces"]

    @pytest.mark.asyncio
    async def test_update_surfaces_a_concurrent_delete_as_404(self, tmp_path: Path) -> None:
        stale = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
        )
        fresh = _cfg()  # "staging" deleted concurrently
        _seed_file(tmp_path, fresh)
        with (
            patch(_LOAD, return_value=stale),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_update(
                _req({"dir": "workspace-new"}, match_info={"name": "staging"})
            )
        assert resp.status == 404
        assert "staging" not in _read_doc(tmp_path)["workspaces"]

    @pytest.mark.asyncio
    async def test_delete_rechecks_agent_references_inside_the_lock(self, tmp_path: Path) -> None:
        stale = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
        )
        fresh = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),
            },
            agents={"bot": KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="staging")},
        )
        _seed_file(tmp_path, fresh)
        with (
            patch(_LOAD, return_value=stale),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_delete(_req(match_info={"name": "staging"}))
        assert resp.status == 409
        assert b"referenced by agents" in resp.body
        assert "staging" in _read_doc(tmp_path)["workspaces"]

    @pytest.mark.asyncio
    async def test_conflicting_copy_from_create_leaves_no_destination_residue(
        self, tmp_path: Path
    ) -> None:
        """The copy is STAGED after all validation and only installed inside
        the locked mutate once its checks pass: a create that loses the race
        must not have mutated the destination directory, and must clean its
        staged tree off the loop (review rounds 2-3)."""
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("source content", encoding="utf-8")
        stale = _cfg()  # "staging" absent -> handler checks pass, copy runs
        fresh = _cfg(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "staging": WorkspaceConfig(dir="workspace-staging"),  # concurrent create won
            },
        )
        _seed_file(tmp_path, fresh)
        with (
            patch(_LOAD, return_value=stale),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "staging", "copy_from": "default"}))
        assert resp.status == 409
        assert not (tmp_path / "workspace-staging").exists(), (
            "the losing create wrote into the destination before its in-lock " "check refused it"
        )
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staged copy not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_failed_validation_after_copy_request_leaves_no_staging(
        self, tmp_path: Path
    ) -> None:
        """Staging happens strictly AFTER path validation: a copy_from create
        whose dir fails the sensitive/traversal checks must return 4xx with
        zero staged residue (review round 3: staging-before-validation)."""
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(
                _req({"name": "evil", "copy_from": "default", "dir": "../../etc"})
            )
        assert resp.status == 400
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"validation failure left a staged copy behind: {leftovers}"


# ── Round-4 hardening: degraded sections, install rollback, occupied dst ──


class TestDeltaMutatorHardening:
    @pytest.mark.asyncio
    async def test_degraded_workspaces_section_is_coerced_not_crashed(self, tmp_path: Path) -> None:
        """A malformed `"workspaces": []` on disk must not 500 the raw delta
        mutate: the section is replaced with a dict, matching what the
        validated load already presents (review round 4)."""
        (tmp_path / "config.json").write_text(
            json.dumps({"workspaces": [], "default_workspace": "default"}), encoding="utf-8"
        )
        cfg = _cfg(workspaces={})
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "staging"}))
        assert resp.status == 200
        doc = _read_doc(tmp_path)
        assert isinstance(doc["workspaces"], dict)
        assert doc["workspaces"]["staging"]["dir"] == "workspace-staging"

    @pytest.mark.asyncio
    async def test_failed_config_write_rolls_back_the_installed_tree(self, tmp_path: Path) -> None:
        """ENOSPC-class failure AFTER the staged tree is installed must not
        leave an unregistered workspace directory behind (review round 4)."""
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(
                "kiro_crew.config.loader.write_config_atomically",
                side_effect=OSError("no space left on device"),
            ),
            patch(_SEL),
        ):
            with pytest.raises(OSError):
                await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert not (tmp_path / "workspace-copied").exists(), (
            "the installed destination was not rolled back after the config "
            "write failed -- an unregistered workspace directory remains"
        )
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staged copy not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_occupied_destination_is_refused_not_merged(self, tmp_path: Path) -> None:
        """A copy_from create whose destination already exists non-empty is
        refused: merging into it cannot be rolled back if the config write
        then fails, so the un-rollbackable branch is eliminated (round 4)."""
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        occupied = tmp_path / "workspace-copied"
        occupied.mkdir()
        (occupied / "precious.txt").write_text("keep me", encoding="utf-8")
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "workspace_dir_occupied"
        assert (occupied / "precious.txt").read_text(encoding="utf-8") == "keep me"
        assert "copied" not in _read_doc(tmp_path)["workspaces"]
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staged copy not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_preexisting_empty_destination_is_refused_and_survives(
        self, tmp_path: Path
    ) -> None:
        """Round 5: even an EMPTY pre-existing destination is refused -- its
        inode and metadata are not ours to replace, and a later rollback
        could otherwise delete a directory this request did not create."""
        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        empty_dst = tmp_path / "workspace-copied"
        empty_dst.mkdir()
        cfg = _cfg()
        _seed_file(tmp_path, cfg)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            resp = await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "workspace_dir_occupied"
        assert empty_dst.is_dir(), "the pre-existing empty destination was destroyed"
        assert "copied" not in _read_doc(tmp_path)["workspaces"]
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staged copy not cleaned up: {leftovers}"

    @pytest.mark.asyncio
    async def test_cancellation_after_persist_success_does_not_roll_back(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Round 5 (Opus): run_config_write drains the worker to completion
        before re-raising a cancellation, so a CancelledError out of the
        persist means the install AND the config registration LANDED --
        rolling back would leave config.json pointing at a deleted tree."""
        import asyncio as _asyncio

        from kiro_crew.dashboard.handlers import files as files_module

        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        cfg = _cfg()
        _seed_file(tmp_path, cfg)

        real_ucl = files_module.update_config_locked

        async def _drained_then_cancelled(fn, /, *args, **kwargs):
            # Mirror run_config_write's contract in the cancel case: the
            # worker ran to completion; the cancellation is re-raised after.
            fn(*args, **kwargs)
            raise _asyncio.CancelledError

        monkeypatch.setattr(files_module, "run_config_write", _drained_then_cancelled)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            with pytest.raises(_asyncio.CancelledError):
                await api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
        assert real_ucl is files_module.update_config_locked  # patch scoped to run_config_write
        assert (tmp_path / "workspace-copied" / "notes.md").exists(), (
            "the installed tree of a SUCCESSFULLY persisted create was rolled "
            "back on cancellation -- config.json now points at a deleted dir"
        )
        assert "copied" in _read_doc(tmp_path)["workspaces"]

    @pytest.mark.asyncio
    async def test_cancellation_during_staging_copy_leaves_no_residue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Round 6 (GPT): a cancellation during the staging copytree must not
        race the cleanup rmtree -- the copy worker is drained to completion
        first, then the staged tree is removed, so no partial ``.staging-*``
        residue survives a gateway shutdown mid-copy."""
        import asyncio as _asyncio
        import shutil as _shutil
        import threading as _threading

        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "notes.md").write_text("x", encoding="utf-8")
        cfg = _cfg()
        _seed_file(tmp_path, cfg)

        started = _threading.Event()
        release = _threading.Event()
        real_copytree = _shutil.copytree

        def _slow_copytree(src, dst, **kwargs):
            started.set()
            assert release.wait(30), "test orchestration stalled"
            return real_copytree(src, dst, **kwargs)

        monkeypatch.setattr(_shutil, "copytree", _slow_copytree)
        with (
            patch(_LOAD, return_value=cfg),
            patch(_CFGDIR, return_value=tmp_path),
            patch(_DATAHOME, return_value=tmp_path),
            patch(_SEL),
        ):
            task = _asyncio.ensure_future(
                api_workspaces_create(_req({"name": "copied", "copy_from": "default"}))
            )
            try:
                await _asyncio.to_thread(started.wait, 30)
                task.cancel()
                # Give the cancellation a chance to land at the drained await
                # while the copy thread is still blocked -- the exact race the
                # finding describes.
                await _asyncio.sleep(0.05)
            finally:
                release.set()
            with pytest.raises(_asyncio.CancelledError):
                await task
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"partial staging residue survived: {leftovers}"
        assert not (tmp_path / "workspace-copied").exists()
        assert "copied" not in _read_doc(tmp_path)["workspaces"]
