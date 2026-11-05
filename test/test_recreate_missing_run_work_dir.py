"""A session start re-creates a Crew-derived one-run cwd that was reclaimed.

A provider marks its one-run directory once, in ``start()``, and its shutdown
reclaim removes it. A runtime started or resumed again for that session after
the reclaim used to hand the peer a deleted cwd, and Node's ``spawn`` reports a
missing cwd as ``spawn <command> ENOENT``: every managed MCP server failed,
each naming a binary that exists. These tests pin the guard in front of
``session/new`` / ``session/load`` and the hint on the readiness failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import runtime_session_setup
from kiro_crew.acp.session_handle_mcp import _missing_cwd_hint
from kiro_crew.session_work_dir import RUN_DIR_MARKER

RUN_DIR_NAME = "cron_ae54b0d8_4e105517"


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(root))
    return root.resolve()


def test_missing_derived_run_dir_is_recreated_with_its_marker(workspace: Path) -> None:
    run_dir = workspace / RUN_DIR_NAME
    assert not run_dir.exists()

    runtime_session_setup._ensure_unbound_session_workspace(run_dir)

    assert run_dir.is_dir()
    # The same provenance the start path writes, so reclaim and sweep still apply.
    assert (run_dir / RUN_DIR_MARKER).is_file()


def test_missing_caller_cwd_is_never_created(workspace: Path) -> None:
    project = workspace / "my-project"

    runtime_session_setup._ensure_unbound_session_workspace(project)

    assert not project.exists()


def test_run_dir_shape_outside_the_workspace_root_is_never_created(
    workspace: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "other" / RUN_DIR_NAME
    elsewhere.parent.mkdir()

    runtime_session_setup._ensure_unbound_session_workspace(elsewhere)

    assert not elsewhere.exists()


def test_existing_cwd_is_left_as_it_is(workspace: Path) -> None:
    existing = workspace / "my-project"
    existing.mkdir()

    runtime_session_setup._ensure_unbound_session_workspace(existing)

    assert existing.is_dir()
    assert not (existing / RUN_DIR_MARKER).exists()


def test_empty_path_is_a_no_op() -> None:
    runtime_session_setup._ensure_unbound_session_workspace("")
    runtime_session_setup._ensure_unbound_session_workspace(None)


def test_unbound_session_work_dir_recreates_before_returning(workspace: Path) -> None:
    run_dir = workspace / RUN_DIR_NAME
    runtime = SimpleNamespace(_bound_workspace_fd=None, _work_dir=str(run_dir))

    resolved = asyncio.run(runtime_session_setup._session_work_dir(runtime))

    assert resolved == str(run_dir)
    assert run_dir.is_dir()


def test_unbound_session_work_dir_prefers_the_explicit_cwd(workspace: Path) -> None:
    explicit = workspace / "my-project"
    explicit.mkdir()
    runtime = SimpleNamespace(_bound_workspace_fd=None, _work_dir=str(workspace / RUN_DIR_NAME))

    resolved = asyncio.run(runtime_session_setup._session_work_dir(runtime, explicit))

    assert resolved == explicit
    assert not (workspace / RUN_DIR_NAME).exists()


SPAWN_FAILURE = "kirocrew-cron: failed (spawn /opt/bin/python3.12 ENOENT)"


def test_hint_names_a_missing_session_cwd(tmp_path: Path) -> None:
    gone = tmp_path / RUN_DIR_NAME
    handle = SimpleNamespace(_bound_cwd=str(gone))

    hint = _missing_cwd_hint(handle, SPAWN_FAILURE)

    assert str(gone) in hint
    assert "does not exist" in hint


def test_hint_is_empty_when_the_cwd_exists(tmp_path: Path) -> None:
    handle = SimpleNamespace(_bound_cwd=str(tmp_path))

    assert _missing_cwd_hint(handle, SPAWN_FAILURE) == ""


def test_hint_is_empty_for_a_failure_that_is_not_enoent(tmp_path: Path) -> None:
    handle = SimpleNamespace(_bound_cwd=str(tmp_path / "gone"))

    assert _missing_cwd_hint(handle, "kirocrew-cron: failed (timeout)") == ""


def test_hint_is_empty_without_a_bound_cwd() -> None:
    assert _missing_cwd_hint(SimpleNamespace(), SPAWN_FAILURE) == ""
