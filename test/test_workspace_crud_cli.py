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
            # cli_commands binds config_dir at import, so the loader patch above
            # does not reach it. Create now materializes its directory, so that
            # binding has to point at a real one.
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
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

    def test_create_materializes_the_workspace_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A registered workspace whose directory is absent is a latent outage.

        The V2 private-memory layout resolves EVERY declared workspace strictly
        and refuses to start ANY private member when one is missing, so a create
        that writes only the config entry breaks members unrelated to it.
        """
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "newws"],
            ),
        ):
            main()
        assert "Created workspace: newws" in capsys.readouterr().out
        assert (tmp_path / "workspace-newws").is_dir(), "config entry without a directory"

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
            # cli_commands binds config_dir at import, so the loader patch above
            # does not reach it -- and this create now does real filesystem work.
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
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
        # The destination must exist: an update REFUSES a dir that is not there,
        # because a declared-but-missing workspace refuses every private member.
        (tmp_path / "new-path").mkdir()
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "update", "staging", "--dir", "new-path"],
            ),
        ):
            main()
        out = capsys.readouterr().out
        assert "Updated workspace: staging" in out

    def test_update_refuses_a_dir_that_is_missing_or_not_a_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rebinding to an unusable path arms a fleet-wide private-memory refusal."""
        cfg_path = _write_config(tmp_path, _base_config())
        (tmp_path / "a-file").write_text("not a directory", encoding="utf-8")
        for target in ("never-created", "a-file"):
            with (
                unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
                unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
                unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
                unittest.mock.patch(
                    "sys.argv",
                    ["kirocrew", "workspace", "update", "staging", "--dir", target],
                ),
                pytest.raises(SystemExit) as exc_info,
            ):
                main()
            assert exc_info.value.code == 1, target
            assert "create it first" in capsys.readouterr().err, target

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
            unittest.mock.patch("kiro_crew.cli_commands._handle_workspace") as mock_handler,
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

    def test_failed_copy_create_leaves_the_installed_tree_in_place(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed config write after the copied tree is installed leaves it, and says so.

        Same rule as the dashboard handler and the plain-create directory: a
        concurrent create can already have adopted and registered the destination,
        so deleting it is the unsafe option. The retained path is named on stderr.
        """
        (tmp_path / "workspace-staging").mkdir()
        (tmp_path / "workspace-staging" / "notes.md").write_text("source", encoding="utf-8")
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "kiro_crew.config.loader.write_config_atomically",
                side_effect=OSError("no space left on device"),
            ),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "copied", "--copy-from", "staging"],
            ),
            pytest.raises(OSError),
        ):
            main()
        assert (
            tmp_path / "workspace-copied" / "notes.md"
        ).is_file(), "the installed tree was deleted after the config write failed"
        assert "copied" not in json.loads(cfg_path.read_text(encoding="utf-8"))["workspaces"]
        err = capsys.readouterr().err
        assert "leaving" in err and str(tmp_path / "workspace-copied") in err

    def test_create_materializes_a_tilde_dir_where_the_validator_judged_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``~``-spelled dir under the data home is created where it was validated.

        ``_ws_dir_resolves_inside_home`` expands ``~`` before judging containment.
        Composing the destination as ``config_dir() / ws_dir`` afterwards would
        materialize ``<home>/~/...`` instead -- a path validation never looked at --
        and refuse a dir the validator had just accepted.
        """
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        cfg_path = _write_config(crew, _base_config())
        with (
            # Windows ``expanduser`` reads USERPROFILE, not HOME (test_agent_spec_hardened_reads).
            unittest.mock.patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)}),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=crew),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=crew),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "workspace",
                    "create",
                    "--name",
                    "tilde",
                    "--dir",
                    "~/.kiro/crew/workspace-tilde",
                ],
            ),
        ):
            main()
        assert "Created workspace: tilde" in capsys.readouterr().out
        assert (crew / "workspace-tilde").is_dir(), "the tilde dir was not created where validated"
        assert not (crew / "~").exists(), "a literal '~' directory was created"

    def test_copy_from_absent_source_materializes_a_tilde_dir_where_validated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The copy-from branch composes its destination like the plain branch.

        A source workspace whose directory is absent stages nothing, so the
        create falls through to the plain materialization. With a ``~``-spelled
        ``--dir`` that fallback must land on the directory validation resolved,
        not on a literal ``~`` under the data home while the tilde string is
        registered -- an entry every private member would then fail on.
        """
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        cfg_path = _write_config(crew, _base_config())  # 'staging' has no directory
        with (
            # Windows ``expanduser`` reads USERPROFILE, not HOME (test_agent_spec_hardened_reads).
            unittest.mock.patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)}),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=crew),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=crew),
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
                    "--dir",
                    "~/.kiro/crew/workspace-copied",
                ],
            ),
        ):
            main()
        assert "Created workspace: copied" in capsys.readouterr().out
        assert (crew / "workspace-copied").is_dir(), "the fallback did not create the validated dir"
        assert not (crew / "~").exists(), "a literal '~' directory was created"


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


class TestWorkspaceDirContainmentMessage:
    """`--dir` is confined to the data home; the refusal must say WHY and WHAT to pass.

    The containment itself is a deliberate boundary (a workspace is agent-writable
    state, so it must not be aimable at ``~/.ssh`` / the keystone policy files).
    What was wrong was the diagnostics: help text advertised ``--dir /path/to/dir``
    and the refusal printed a bare "invalid directory path", so a user following
    the docs got a dead end with no way to tell what was acceptable.
    """

    @pytest.mark.parametrize("bad_dir", ["/tmp/outside", "../../etc", "~/elsewhere"])
    def test_create_rejects_escaping_dir_with_actionable_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_dir: str
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "ws1", "--dir", bad_dir],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        # Names the boundary, echoes the rejected value, and shows the accepted shape.
        assert "data home" in err
        assert bad_dir in err
        assert "relative directory name" in err

    def test_update_rejects_escaping_dir_with_actionable_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "update", "staging", "--dir", "/etc"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "data home" in err and "/etc" in err

    def test_relative_dir_still_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The fix is diagnostics-only — a legitimate relative name must still work."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "ws2", "--dir", "workspace-ws2"],
            ),
        ):
            main()

        assert "Created workspace: ws2" in capsys.readouterr().out

    @pytest.mark.parametrize("bad_dir", ["~nosuchuser12345/x", "~nosuchuser12345"])
    def test_unknown_tilde_user_refuses_instead_of_crashing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_dir: str
    ) -> None:
        """`~unknownuser/...` must refuse, not raise.

        `Path.expanduser()` raises RuntimeError when the tilde names a user with
        no resolvable home. Since this PR's whole point is replacing tracebacks
        with clean refusals, the containment guard itself must fail closed.
        """
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "tildews", "--dir", bad_dir],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "data home" in err
        assert "Traceback" not in err and "RuntimeError" not in err

    def test_copy_from_unknown_tilde_user_refuses_instead_of_crashing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The copy-from branch validates an unknown tilde user before expanding it."""
        bad_dir = "~nosuchuser_kc/x"
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
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
                    "--dir",
                    bad_dir,
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "data home" in err
        assert "Traceback" not in err and "RuntimeError" not in err

    def test_data_home_root_refused_in_both_forms(self, monkeypatch, tmp_path: Path) -> None:
        """The root is refused whether written absolute OR as a tilde path.

        Regression: the per-call-site "cannot use config root" checks compare
        ``config_dir() / ws_dir`` WITHOUT expanding ``~``, so `~/.kiro/crew`
        became `<home>/~/.kiro/crew` there — unequal to the root, so it slipped
        through while the plain absolute form was refused. Containment is now a
        STRICT descendant test, decided in the one place that expands.
        """
        from kiro_crew import cli_commands as cc

        monkeypatch.setattr(cc, "config_dir", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path.parent))

        # Absolute form of the root.
        assert cc._ws_dir_resolves_inside_home(str(tmp_path)) is None
        # Tilde form resolving to the same root (the bypass).
        assert cc._ws_dir_resolves_inside_home(f"~/{tmp_path.name}") is None
        # Self-referential relative forms that also land on the root.
        assert cc._ws_dir_resolves_inside_home(".") is None
        assert cc._ws_dir_resolves_inside_home("") is None
        # A real descendant is still fine.
        assert cc._ws_dir_resolves_inside_home("workspace-ok") is not None
        assert cc._ws_dir_resolves_inside_home("ws/nested/ok") is not None

    def test_root_dir_cli_refuses_with_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end: `--dir <data-home root>` exits 1, no traceback."""
        cfg_path = _write_config(tmp_path, _base_config())
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "rootws", "--dir", str(tmp_path)],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err

    @pytest.mark.parametrize(
        "keystone",
        [
            "profiles",
            "profiles/nested",
            "security_policy.json",
            "admission_policy.json",
            "denied_commands.json",
            "computer_use.json",
            ".env",
            "sel_hmac.key",
        ],
    )
    def test_keystone_paths_refused_as_workspace_dir(self, monkeypatch, keystone: str) -> None:
        """Inside the data home is NOT automatically safe.

        ``--copy-from`` uses ``copytree(dirs_exist_ok=True)``, so a workspace dir
        of ``profiles`` would OVERWRITE the governance ceiling the agent must never
        write — the mechanism that makes that ceiling un-disableable. Verified
        exploitable before the fix: a planted ``workspace/locked.json`` replaced
        ``profiles/locked.json``.

        Uses the REAL default home shape because ``is_sensitive_path()`` matches
        the literal ``~/.kiro/crew`` / ``~/.kirocrew`` prefixes rather than
        ``config_dir()``, so a tmp_path home would not exercise the gate at all.
        """
        from kiro_crew import cli_commands as cc

        home = Path.home() / ".kiro" / "crew"
        monkeypatch.setattr(cc, "config_dir", lambda: home)
        assert cc._ws_dir_resolves_inside_home(keystone) is None

    @pytest.mark.parametrize("ok_dir", ["workspace", "workspace-ok", "ws/nested/ok"])
    def test_ordinary_workspace_dirs_still_allowed(self, monkeypatch, ok_dir: str) -> None:
        """The keystone screen must not block legitimate workspace dirs."""
        from kiro_crew import cli_commands as cc

        home = Path.home() / ".kiro" / "crew"
        monkeypatch.setattr(cc, "config_dir", lambda: home)
        assert cc._ws_dir_resolves_inside_home(ok_dir) is not None

    def test_guard_fails_closed_on_unresolvable_path(self, monkeypatch, tmp_path: Path) -> None:
        """Any resolution failure returns False (deny), never propagates."""
        from kiro_crew import cli_commands as cc

        monkeypatch.setattr(cc, "config_dir", lambda: tmp_path)

        class _Boom:
            def expanduser(self):
                raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(cc, "Path", lambda _s: _Boom())
        assert cc._ws_dir_resolves_inside_home("~whoever/x") is None

    def test_absolute_dir_inside_home_is_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The rule is CONTAINMENT, not an absolute-path ban.

        An absolute path landing inside the data home resolves exactly where the
        relative form would, so there is nothing to refuse. Locked in so the
        message/help wording keeps describing containment rather than claiming
        "absolute paths are rejected".
        """
        cfg_path = _write_config(tmp_path, _base_config())
        inside = str(tmp_path / "ws-inside")
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "insidews", "--dir", inside],
            ),
        ):
            main()

        assert "Created workspace: insidews" in capsys.readouterr().out

    def test_help_does_not_advertise_absolute_paths(self) -> None:
        """The help text must not tell users to pass `/path/to/dir` when that is refused."""
        with (
            unittest.mock.patch("sys.argv", ["kirocrew", "workspace", "create", "--help"]),
            pytest.raises(SystemExit),
        ):
            main()


# ── CLI CRUD writes are locked deltas, not whole-document saves ──


class TestCliCrudIsLockedDelta:
    """The CLI CRUD commands persist via ``update_config_locked`` deltas.

    The old load -> mutate dataclass ->
    ``cfg.save()`` shape re-serialized the command's stale snapshot, so a
    change another process landed between the load and the save was silently
    erased. A delta on the document read inside the flock cannot lose it.
    """

    def _run(self, argv: list[str], cfg_path: Path, tmp_path: Path) -> None:
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            # cli_commands binds config_dir at import, so the loader patch above
            # does not reach it. Create now materializes its directory, so that
            # binding has to point at a real one.
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch("sys.argv", ["kirocrew", *argv]),
        ):
            main()

    def test_workspace_create_preserves_a_concurrently_landed_key(self, tmp_path: Path) -> None:
        """A document key the CLI's snapshot never saw survives the write.

        ``KiroCrewConfig`` drops unknown keys on re-serialization, so under
        the old ``cfg.save()`` path the marker below was erased; the delta
        write rewrites only the workspaces section and keeps it.
        """
        data = _base_config()
        data["zz_concurrent_marker"] = {"landed": True}
        cfg_path = _write_config(tmp_path, data)
        self._run(
            ["workspace", "create", "--name", "fresh", "--dir", "workspace-fresh"],
            cfg_path,
            tmp_path,
        )
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert doc["workspaces"]["fresh"]["dir"] == "workspace-fresh"
        assert doc.get("zz_concurrent_marker") == {"landed": True}, (
            "the CLI create rewrote the whole document from its stale "
            "snapshot and erased a concurrently landed change"
        )

    def test_agent_update_preserves_a_concurrently_landed_key(self, tmp_path: Path) -> None:
        data = _base_config()
        data["zz_concurrent_marker"] = {"landed": True}
        cfg_path = _write_config(tmp_path, data)
        self._run(
            ["agent", "update", "default", "--workspace", "staging"],
            cfg_path,
            tmp_path,
        )
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert doc["agents"]["default"]["workspace"] == "staging"
        assert doc.get("zz_concurrent_marker") == {"landed": True}

    def test_workspace_create_recheck_conflicts_in_lock(self, tmp_path: Path) -> None:
        """The in-lock re-check refuses a name the pre-check snapshot missed."""
        cfg_path = _write_config(tmp_path, _base_config())
        real_load = json.loads

        # Simulate a racer: the name is free in the CLI's snapshot but taken
        # by the time the locked read runs. Patch the loader's snapshot load
        # to hide the workspace from the pre-check only.
        import kiro_crew.cli_commands as cli_commands_module

        original_mutator_runner = cli_commands_module._locked_config_write

        def _inject_racer_then_run(mutate, **kwargs):
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            doc["workspaces"]["fresh"] = {"dir": "workspace-racer"}
            cfg_path.write_text(json.dumps(doc), encoding="utf-8")
            original_mutator_runner(mutate, **kwargs)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch.object(
                cli_commands_module, "_locked_config_write", _inject_racer_then_run
            ),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "fresh", "--dir", "workspace-f2"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        doc = real_load(cfg_path.read_text(encoding="utf-8"))
        assert (
            doc["workspaces"]["fresh"]["dir"] == "workspace-racer"
        ), "the CLI overwrote a workspace the racer created first"


# ── in-lock default re-checks + staged copy_from install ──


class TestCliRound8Hardening:
    def _base_with_defaults(self) -> dict:
        data = _base_config()
        return data

    def test_workspace_delete_refuses_a_concurrently_selected_default(self, tmp_path: Path) -> None:
        """A racer makes the workspace the default between snapshot and lock."""
        data = _base_config()
        cfg_path = _write_config(tmp_path, data)

        import kiro_crew.cli_commands as cc

        original = cc._locked_config_write

        def _racer_selects_default_then_run(mutate, **kwargs):
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            doc["default_workspace"] = "staging"
            cfg_path.write_text(json.dumps(doc), encoding="utf-8")
            original(mutate, **kwargs)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch.object(cc, "_locked_config_write", _racer_selects_default_then_run),
            unittest.mock.patch("sys.argv", ["kirocrew", "workspace", "delete", "staging"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert (
            "staging" in doc["workspaces"]
        ), "the delete removed a workspace a racer had just made the default"

    def test_agent_delete_refuses_top_level_default(self, tmp_path: Path) -> None:
        """The authoritative default_agent key is TOP-LEVEL; the in-lock
        re-check must read it there, not only the migration-era agent section."""
        data = _base_config()
        data["agents"]["spare"] = dict(data["agents"]["default"])
        data["default_agent"] = "spare"
        cfg_path = _write_config(tmp_path, data)
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "delete", "spare"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "spare" in doc["agents"]

    def test_losing_copy_from_race_leaves_winner_untouched(self, tmp_path: Path) -> None:
        """The copy is STAGED: when the locked check refuses (racer took the
        name), the winner's directory contains none of the loser's files and
        no staging residue remains."""
        src = tmp_path / "workspace"
        src.mkdir()
        (src / "loser-file.md").write_text("x", encoding="utf-8")
        cfg_path = _write_config(tmp_path, _base_config())

        import kiro_crew.cli_commands as cc

        original = cc._locked_config_write

        def _racer_takes_name_then_run(mutate, **kwargs):
            winner_dir = tmp_path / "workspace-copied"
            winner_dir.mkdir()
            (winner_dir / "winner-file.md").write_text("w", encoding="utf-8")
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            doc["workspaces"]["copied"] = {"dir": "workspace-copied"}
            cfg_path.write_text(json.dumps(doc), encoding="utf-8")
            original(mutate, **kwargs)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            unittest.mock.patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            unittest.mock.patch.object(cc, "_locked_config_write", _racer_takes_name_then_run),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "workspace", "create", "--name", "copied", "--copy-from", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        winner_dir = tmp_path / "workspace-copied"
        assert sorted(p.name for p in winner_dir.iterdir()) == [
            "winner-file.md"
        ], "the losing create's copied files leaked into the winner's workspace"
        leftovers = [p.name for p in tmp_path.iterdir() if ".staging-" in p.name]
        assert leftovers == [], f"staging residue: {leftovers}"
