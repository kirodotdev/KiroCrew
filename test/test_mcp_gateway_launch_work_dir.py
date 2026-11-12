"""The cwd of a pooled backend is the daemon's own, not the register frame's.

``PoolKey.work_dir`` comes from a stub's register frame, which any same-user
process on the gateway socket can send. The launch approval covers command,
arguments and env, so the daemon must not let the frame choose the directory an
approved launch runs in. These tests pin that the default resolver always names
the gateway's own default work dir, ``workspace_root()/_default``, and that the
gateway pins that directory in the daemon's env.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest
from mcp_launch_approval_helpers import (
    _APPROVED_CMD,
    _approval_module,
    _approvals_for,
    _pool_key,
    _target_spec,
)

from kiro_crew.config.loader_locations import workspace_root

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

#: The env key gatewayd reads its backend work dir from. Spelled out here, and
#: checked against ``manager.BACKEND_WORK_DIR_ENV`` below, so the module imports
#: on a tree without it and each test reports its own result.
BACKEND_WORK_DIR_ENV = "KIROCREW_MCP_BACKEND_WORK_DIR"


@pytest.fixture
def approved_srv(tmp_path, monkeypatch):
    """An approved ``srv`` launch, with a workspace root this test owns."""
    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace" / "_default").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    mod._write(mod.approvals_path(), mod._document(approvals, {}))
    return str(workspace_root(create=False) / "_default")


def _key_with_work_dir(work_dir: str):
    return dataclasses.replace(_pool_key("srv"), work_dir=work_dir)


def test_the_rewriters_default_work_dir_is_the_spawn_cwd(approved_srv):
    from kiro_crew.mcp_gateway import gatewayd

    target = gatewayd.env_target_resolver(_key_with_work_dir(approved_srv))

    assert target is not None
    assert target[3] == approved_srv


def test_a_register_frame_cannot_choose_the_spawn_cwd(approved_srv, tmp_path):
    from kiro_crew.mcp_gateway import gatewayd

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    target = gatewayd.env_target_resolver(_key_with_work_dir(str(elsewhere)))

    assert target is not None
    assert target[0] == _APPROVED_CMD
    assert target[3] == approved_srv


def test_a_frame_naming_another_work_dir_is_logged(approved_srv, tmp_path, caplog):
    from kiro_crew.mcp_gateway import gatewayd

    other = str(tmp_path / "elsewhere")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway"):
        gatewayd.env_target_resolver(_key_with_work_dir(other))

    # The path is logged with %r: on Windows its backslashes come out doubled.
    assert any(
        "registered work dir" in record.getMessage() and repr(other) in record.getMessage()
        for record in caplog.records
    )


def test_the_default_work_dir_is_not_logged(approved_srv, caplog):
    from kiro_crew.mcp_gateway import gatewayd

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway"):
        gatewayd.env_target_resolver(_key_with_work_dir(approved_srv))

    assert not any("registered work dir" in r.getMessage() for r in caplog.records)


def test_the_gateways_pinned_work_dir_wins_over_a_second_resolution(
    approved_srv, tmp_path, monkeypatch
):
    """The daemon spawns in the directory the rewrite used, even where resolving differs.

    A saved root behind a file is one such case: the rewrite resolves with
    ``create=True``, whose ``mkdir`` fails on the blocked chain and falls back to
    the default base, while a non-creating resolution names the blocked root.
    The gateway pins the rewrite's directory in the daemon's env, and that wins.
    """
    from kiro_crew.config import loader, loader_locations
    from kiro_crew.mcp_gateway import gatewayd

    monkeypatch.delenv("KIROCREW_WORKSPACE")
    # Patch through the facade: it mirrors the change onto every module that holds the name.
    monkeypatch.setattr(loader, "_default_workspace_base", lambda: tmp_path / "base")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    saved = tmp_path / "workspace_dir"
    saved.write_text(f"{blocker / 'ws'}\n", encoding="utf-8")
    monkeypatch.setattr(loader, "_workspace_dir_file", lambda: saved)
    rewriters = str(loader_locations._session_work_dir(None))
    assert str(loader_locations.workspace_root(create=False) / "_default") != rewriters
    monkeypatch.setenv(BACKEND_WORK_DIR_ENV, rewriters)

    target = gatewayd.env_target_resolver(_key_with_work_dir(rewriters))

    assert target is not None
    assert target[3] == rewriters
    # The daemon's own setting is not handed on to the backend's env.
    assert BACKEND_WORK_DIR_ENV not in target[2]


class _SpawnCaptured(RuntimeError):
    pass


def _daemon_env(monkeypatch, tmp_path, spec_work_dir: str) -> dict:
    """The env ``GatewayManager._spawn_once`` hands gatewayd."""
    from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec

    captured: dict = {}

    async def _fake_spawn(*_argv, env=None, **_kwargs):
        captured["env"] = dict(env or {})
        raise _SpawnCaptured

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    manager = GatewayManager(
        GatewaySpec(socket_path=tmp_path / "gateway.sock", backend_work_dir=spec_work_dir)
    )
    monkeypatch.setattr(manager, "_gatewayd_log_path", lambda: tmp_path / "gatewayd.stdout")
    monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])
    with pytest.raises(_SpawnCaptured):
        asyncio.run(manager._spawn_once())
    return captured["env"]


def test_the_manager_hands_gatewayd_the_rewrites_work_dir(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import manager

    assert manager.BACKEND_WORK_DIR_ENV == BACKEND_WORK_DIR_ENV
    rewriters = str(tmp_path / "workspace" / "_default")
    monkeypatch.setenv(BACKEND_WORK_DIR_ENV, str(tmp_path / "inherited"))

    assert _daemon_env(monkeypatch, tmp_path, rewriters)[BACKEND_WORK_DIR_ENV] == rewriters
    # Without one in the spec, an inherited value is not passed on either.
    assert BACKEND_WORK_DIR_ENV not in _daemon_env(monkeypatch, tmp_path, "")


def test_an_unresolvable_work_dir_falls_back_to_the_frames(approved_srv, tmp_path, monkeypatch):
    from kiro_crew.config import loader_locations
    from kiro_crew.mcp_gateway import gatewayd

    def _unresolvable(*, create: bool = True):
        raise OSError("no workspace")

    monkeypatch.setattr(loader_locations, "workspace_root", _unresolvable)
    frames = str(tmp_path / "frame-dir")

    target = gatewayd.env_target_resolver(_key_with_work_dir(frames))

    # As on main: the spawn goes ahead, in the directory the frame named.
    assert target is not None
    assert target[3] == frames
