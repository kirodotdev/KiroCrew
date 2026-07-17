"""Unit tests for the ``kirocrew workspace`` CLI subcommand group.

Tests cover argparse subparser structure, dispatch routing, list output
format, and error handling for create/update/delete operations.

Requirements: 5.1, 5.4, 5.5, 5.7, 5.9, 5.10, 5.11, 6.1, 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.cli import main


@pytest.fixture(autouse=True)
def _mock_sel():
    """Mock SEL logging for all workspace CLI tests."""
    with unittest.mock.patch("kiro_crew.sel.sel"):
        yield


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a config.json to *tmp_path* and return the path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _base_config() -> dict:
    """Return a minimal valid config with default workspace and agent."""
    return {
        "workspaces": {
            "default": {"dir": "workspace"},
            "staging": {"dir": "workspace-staging"},
        },
        "default_workspace": "default",
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
        },
        "default_agent": "default",
        "memory_stores": {"default": {}},
    }


# ── Argparse structure (Req 6.1, 6.2, 6.3, 6.4) ──


class TestWorkspaceArgparse:
    """Verify the workspace subparser exists with correct subcommands."""

    def test_workspace_subparser_exists(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 6.1: workspace subparser with list/create/update/delete."""
        cfg_path = _write_config(tmp_path, _base_config())
        # Verify each subcommand is accepted by argparse (no SystemExit(2)).
        for subcmd, argv in [
            ("list", ["kirocrew", "workspace", "list"]),
            ("create", ["kirocrew", "workspace", "create", "--name", "newtest"]),
            ("update", ["kirocrew", "workspace", "update", "staging"]),
            ("delete", ["kirocrew", "workspace", "delete", "staging"]),
        ]:
            with (
                unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
                unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
                unittest.mock.patch("sys.argv", argv),
            ):
                # Should not raise SystemExit(2) (argparse error)
                main()
            # Re-write config since delete/create mutate it
            _write_config(tmp_path, _base_config())

    def test_create_requires_name_flag(self) -> None:
        """Req 6.2: create subcommand requires --name."""
        with (
            unittest.mock.patch("sys.argv", ["kirocrew", "workspace", "create"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        # argparse exits with code 2 for missing required args
        assert exc_info.value.code == 2

    def test_create_accepts_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Req 6.2: create accepts --dir."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "workspace",
                    "create",
                    "--name",
                    "newws",
                    "--dir",
                    "custom-dir",
                ],
            ),
        ):
            main()
        out = capsys.readouterr().out
        assert "Created workspace: newws" in out

    def test_create_accepts_copy_from(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 6.2: create accepts --copy-from."""
        cfg_path = _write_config(tmp_path, _base_config())
        # Create the source workspace directory so copytree has something to copy
        (tmp_path / "workspace-staging").mkdir(parents=True, exist_ok=True)
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "workspace",
                    "create",
                    "--name",
                    "copied",
                    "--copy-from",
                    "staging",
                ],
            ),
        ):
            main()
        out = capsys.readouterr().out
        assert "Created workspace: copied" in out

    def test_update_accepts_positional_name_and_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 6.3: update accepts positional name and --dir."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "update", "staging", "--dir", "new-path"],
            ),
        ):
            main()
        out = capsys.readouterr().out
        assert "Updated workspace: staging" in out

    def test_delete_accepts_positional_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 6.4: delete accepts positional name."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "delete", "staging"],
            ),
        ):
            main()
        out = capsys.readouterr().out
        assert "Deleted workspace: staging" in out


# ── Dispatch routing (Req 6.5) ──


class TestWorkspaceDispatch:
    """Verify args.command == 'workspace' routes to _handle_workspace."""

    def test_dispatch_routes_to_handle_workspace(self, tmp_path: Path) -> None:
        """Req 6.5: workspace command dispatches to _handle_workspace."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "workspace", "list"]),
            unittest.mock.patch("kiro_crew.cli._handle_workspace") as mock_handler,
        ):
            main()
        mock_handler.assert_called_once()


# ── List output (Req 5.1) ──


class TestWorkspaceList:
    """Test ``kirocrew workspace list`` output format."""

    def test_list_shows_header_and_default_marker(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.1: formatted table with * marker for default workspace."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "workspace", "list"]),
        ):
            main()

        out = capsys.readouterr().out
        # Header row
        assert "NAME" in out
        assert "DIR" in out
        # Default workspace marked with *
        assert "default *" in out or "default*" in out
        # Non-default workspace present without marker
        assert "staging" in out
        assert "workspace-staging" in out


# ── Create errors (Req 5.4, 5.5) ──


class TestWorkspaceCreate:
    """Test ``kirocrew workspace create`` error paths."""

    def test_create_duplicate_name_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.4: duplicate name → stderr + exit 1."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_create_missing_copy_from_source_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.5: copy_from with non-existent source → stderr + exit 1."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "workspace",
                    "create",
                    "--name",
                    "newws",
                    "--copy-from",
                    "nonexistent",
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err


# ── Update errors (Req 5.7) ──


class TestWorkspaceUpdate:
    """Test ``kirocrew workspace update`` error paths."""

    def test_update_nonexistent_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.7: non-existent name → stderr + exit 1."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "update", "nonexistent", "--dir", "/x"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err


# ── Delete errors (Req 5.9, 5.10, 5.11) ──


class TestWorkspaceDelete:
    """Test ``kirocrew workspace delete`` error paths."""

    def test_delete_default_workspace_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.9: delete default workspace → stderr + exit 1."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "delete", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "cannot delete default workspace" in err

    def test_delete_agent_referenced_workspace_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.10: agent-referenced workspace → stderr + exit 1 with agent names."""
        data = _base_config()
        # Add a non-default workspace referenced by an agent
        data["workspaces"]["oncall"] = {"dir": "workspace-oncall"}
        data["agents"]["oncall-agent"] = {
            "kiro_agent": "kirocrew",
            "workspace": "oncall",
            "memory_store": "default",
        }
        cfg_path = _write_config(tmp_path, data)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "delete", "oncall"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "referenced by agents" in err
        assert "oncall-agent" in err

    def test_delete_nonexistent_workspace_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Req 5.11: non-existent workspace → stderr + exit 1."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "delete", "nonexistent"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err
