"""A workspace ``dir`` the filesystem cannot resolve is a caller error, not a 500.

A JSON string passes the string-field check, but ``Path.resolve()`` still raises
on some strings: an embedded NUL raises ``ValueError``, a ``~user`` naming no
account raises ``RuntimeError``, and a path the host refuses raises ``OSError``.
The requested ``dir`` answers ``400 Invalid directory path`` with no write.

A STORED entry that cannot resolve is skipped by the collision check, the way
``_ws_dir_in_use`` already documents: it cannot name the requested folder, so it
must not stop a valid create or update of an unrelated workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_workspace_crud_handlers import (
    _CFGDIR,
    _DATAHOME,
    _LOAD,
    _SEL,
    _cfg,
    _read_doc,
    _req,
    _seed_file,
)

from kiro_crew.config.loader import WorkspaceConfig
from kiro_crew.dashboard.handlers import api_workspaces_create, api_workspaces_update

_GONE_USER = "~no-such-user-kc32845"
_UNRESOLVABLE = ["bad\x00dir", f"{_GONE_USER}/ws"]


@pytest.fixture(autouse=True)
def _gone_user_has_no_home(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """``~<gone>`` raises from ``expanduser`` on every host, as it does on POSIX.

    Windows' ``expanduser`` guesses a sibling profile for any ``~user`` instead
    of raising, so the account-gone case is pinned the way
    ``test_member_workspace`` pins it. Through ``_floor_monkeypatch`` (D11), so a
    test's own ``monkeypatch.undo()`` cannot lift it.
    """
    import os

    real = os.path.expanduser

    def expand(path):
        if str(path).startswith(_GONE_USER):
            raise RuntimeError("Could not determine home directory.")
        return real(path)

    _floor_monkeypatch.setattr("os.path.expanduser", expand)


async def _call(handler, request):
    """Run *handler*; a resolve error escaping it is the bug, so fail on it."""
    try:
        return await handler(request)
    except (RuntimeError, OSError, ValueError) as exc:
        pytest.fail(f"an unresolvable dir escaped the handler: {exc!r}")


def _patched(tmp_path: Path, cfg):
    return (
        patch(_LOAD, return_value=cfg),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
        patch(_SEL),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _UNRESOLVABLE)
@pytest.mark.parametrize("copy", [False, True])
async def test_create_with_an_unresolvable_dir_is_400_and_writes_nothing(
    tmp_path: Path, bad: str, copy: bool
) -> None:
    cfg = _cfg()
    seeded = _seed_file(tmp_path, cfg).read_text(encoding="utf-8")
    body = {"name": "fresh", "dir": bad}
    if copy:
        (tmp_path / "workspace").mkdir()
        body["copy_from"] = "default"
    a, b, c, d = _patched(tmp_path, cfg)
    with a, b, c, d:
        resp = await _call(api_workspaces_create, _req(body))
    assert resp.status == 400, resp.body
    assert json.loads(resp.body)["error"] == "Invalid directory path"
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == seeded


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _UNRESOLVABLE)
async def test_update_with_an_unresolvable_dir_is_400_and_writes_nothing(
    tmp_path: Path, bad: str
) -> None:
    cfg = _cfg()
    seeded = _seed_file(tmp_path, cfg).read_text(encoding="utf-8")
    a, b, c, d = _patched(tmp_path, cfg)
    with a, b, c, d:
        resp = await _call(
            api_workspaces_update, _req({"dir": bad}, match_info={"name": "default"})
        )
    assert resp.status == 400, resp.body
    assert json.loads(resp.body)["error"] == "Invalid directory path"
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == seeded


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _UNRESOLVABLE)
async def test_an_unresolvable_stored_dir_does_not_block_a_valid_create(
    tmp_path: Path, bad: str
) -> None:
    cfg = _cfg(
        workspaces={
            "default": WorkspaceConfig(dir="workspace"),
            "stale": WorkspaceConfig(dir=bad),
        }
    )
    _seed_file(tmp_path, cfg)
    (tmp_path / "workspace").mkdir()
    (tmp_path / "valid").mkdir()
    a, b, c, d = _patched(tmp_path, cfg)
    with a, b, c, d:
        resp = await _call(api_workspaces_create, _req({"name": "fresh", "dir": "valid"}))
    assert resp.status == 200, resp.body
    doc = _read_doc(tmp_path)
    assert doc["workspaces"]["fresh"]["dir"] == "valid"
    assert doc["workspaces"]["stale"]["dir"] == bad, "the stored entry was rewritten"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _UNRESOLVABLE)
async def test_an_unresolvable_stored_dir_does_not_block_a_valid_update(
    tmp_path: Path, bad: str
) -> None:
    cfg = _cfg(
        workspaces={
            "default": WorkspaceConfig(dir="workspace"),
            "stale": WorkspaceConfig(dir=bad),
        }
    )
    _seed_file(tmp_path, cfg)
    (tmp_path / "workspace").mkdir()
    (tmp_path / "valid").mkdir()
    a, b, c, d = _patched(tmp_path, cfg)
    with a, b, c, d:
        resp = await _call(
            api_workspaces_update, _req({"dir": "valid"}, match_info={"name": "default"})
        )
    assert resp.status == 200, resp.body
    assert _read_doc(tmp_path)["workspaces"]["default"]["dir"] == "valid"


@pytest.mark.asyncio
async def test_a_stored_dir_naming_the_requested_folder_still_collides(tmp_path: Path) -> None:
    """Skipping unresolvable entries must not skip the resolvable ones."""
    cfg = _cfg(
        workspaces={
            "default": WorkspaceConfig(dir="workspace"),
            "stale": WorkspaceConfig(dir="bad\x00dir"),
            "taken": WorkspaceConfig(dir="valid"),
        }
    )
    _seed_file(tmp_path, cfg)
    (tmp_path / "workspace").mkdir()
    (tmp_path / "valid").mkdir()
    a, b, c, d = _patched(tmp_path, cfg)
    with a, b, c, d:
        resp = await _call(api_workspaces_create, _req({"name": "fresh", "dir": "./valid"}))
    assert resp.status == 409, resp.body
    assert "already used" in json.loads(resp.body)["error"]
