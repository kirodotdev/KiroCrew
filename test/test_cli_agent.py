"""Unit tests for the ``kirocrew agent`` CLI subcommand group.

Tests cover list output format, create with defaults, create duplicate,
update non-existent, and delete default agent.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.cli import main
from kiro_crew.config.paths import data_home
from kiro_crew.validation import (
    _MODEL_NAME_RE,
    MODEL_NAME_HINT,
    TEMPLATE_NAME_HINT,
    TEMPLATE_NAME_RE,
)


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a config.json to *tmp_path* and return the path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _base_config() -> dict:
    """Return a minimal valid config with a default agent."""
    return {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {"default": {}},
    }


class TestAgentList:
    """Test ``kirocrew agent list`` output format."""

    def test_list_output_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
        ):
            main()

        out = capsys.readouterr().out
        # Header row
        assert "NAME" in out
        assert "KIRO_AGENT" in out
        assert "WORKSPACE" in out
        assert "MEMORY_STORE" in out
        # Default agent marked with *
        assert "default *" in out or "default*" in out

    def test_list_multiple_agents(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = _base_config()
        data["agents"]["oncall"] = {
            "kiro_agent": "oncall-agent",
            "workspace": "oncall-ws",
            "memory_store": "oncall-mem",
        }
        cfg_path = _write_config(tmp_path, data)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
        ):
            main()

        out = capsys.readouterr().out
        assert "oncall" in out
        assert "oncall-agent" in out


class TestAgentCreate:
    """Test ``kirocrew agent create``."""

    def test_create_with_defaults(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "research"],
            ),
        ):
            main()

        out = capsys.readouterr().out
        assert "Created agent: research" in out

        # Verify persisted to disk
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "research" in saved["agents"]
        assert saved["agents"]["research"]["kiro_agent"] == "kirocrew"
        store = saved["agents"]["research"]["memory_store"]
        assert store != "default"
        assert saved["memory_stores"][store]["owner_member"] == "research"
        assert saved["memory_stores"][store]["memory_version"] == 2
        # Its own workspace, published with the record and present on disk.
        assert saved["agents"]["research"]["workspace"] == "member-research"
        assert saved["workspaces"]["member-research"] == {"dir": "workspace-member-research"}
        assert (data_home() / "workspace-member-research").is_dir()
        assert "Workspace: member-research" in out

    def test_create_can_share_a_declared_workspace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "research", "--workspace", "default"],
            ),
        ):
            main()

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["workspace"] == "default"
        assert set(saved["workspaces"]) == {"default"}

    def test_a_named_workspace_is_stored_as_given(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The binding the CLI always stored; nothing is allocated for it.
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "research", "--workspace", "oncall"],
            ),
        ):
            main()

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["workspace"] == "oncall"
        assert set(saved["workspaces"]) == {"default"}
        assert not (data_home() / "workspace-oncall").exists()
        assert "Workspace: oncall" in capsys.readouterr().out

    def test_create_duplicate_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_create_free_form_name_keys_a_derived_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "Release Writer"],
            ),
        ):
            main()

        assert "Created agent: release-writer (display name: Release Writer)" in (
            capsys.readouterr().out
        )
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "Release Writer" not in saved["agents"]
        assert saved["agents"]["release-writer"]["display_name"] == "Release Writer"

    def test_create_display_name_flag_labels_an_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "create", "--name", "scribe", "--display-name", "Scribe"],
            ),
        ):
            main()

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["scribe"]["display_name"] == "Scribe"

    def test_create_rejects_free_form_template_identifier(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "agent",
                    "create",
                    "--name",
                    "dr. eggbot",
                    "--kiro-agent",
                    "dr. eggbot",
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        assert f"invalid kiro agent name ({TEMPLATE_NAME_HINT})" in capsys.readouterr().err

    def test_create_rejects_credential_shaped_member_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())
        name = "https://user:password@example.com"
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                [
                    "kirocrew",
                    "agent",
                    "create",
                    "--name",
                    name,
                    "--kiro-agent",
                    "kirocrew",
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        error = capsys.readouterr().err
        assert "invalid Crew Member name" in error
        assert name not in error


class TestAgentUpdate:
    """Test ``kirocrew agent update``."""

    def test_template_update_retains_a_concurrent_workspace_edit(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.memory_stores import persist_member_config

        data = _base_config()
        data["agents"]["research"] = {
            "kiro_agent": "kirocrew",
            "workspace": "default",
            "memory_store": "default",
        }
        cfg_path = _write_config(tmp_path, data)

        def publish_after_concurrent_edit(*args, **kwargs):
            def mutate(doc):
                doc["agents"]["research"]["workspace"] = "concurrent-workspace"
                return doc

            update_config_locked(mutate=mutate)
            return persist_member_config(*args, **kwargs)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "kiro_crew.cli_commands.persist_member_config",
                side_effect=publish_after_concurrent_edit,
            ),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "update", "research", "--kiro-agent", "new-template"],
            ),
        ):
            main()

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["kiro_agent"] == "new-template"
        assert saved["agents"]["research"]["workspace"] == "concurrent-workspace"
        assert saved["agents"]["research"]["memory_store"] == "default"

    def test_update_nonexistent_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "update", "nonexistent", "--kiro-agent", "x"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "not found" in err

    def test_update_rejects_free_form_template_identifier(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _base_config()
        data["agents"]["research"] = {
            "kiro_agent": "kirocrew",
            "workspace": "default",
            "memory_store": "default",
        }
        cfg_path = _write_config(tmp_path, data)

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "update", "research", "--kiro-agent", "dr. eggbot"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        assert f"invalid kiro agent name ({TEMPLATE_NAME_HINT})" in capsys.readouterr().err


def _run_agent_cli(cfg_path: Path, *argv: str) -> None:
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        unittest.mock.patch("sys.argv", ["kirocrew", "agent", *argv]),
    ):
        main()


def _labelled_config() -> dict:
    data = _base_config()
    data["agents"]["research"] = {
        "kiro_agent": "kirocrew",
        "workspace": "default",
        "memory_store": "default",
    }
    data["agents"]["scribe"] = {
        "kiro_agent": "kirocrew",
        "workspace": "default",
        "memory_store": "default",
        "display_name": "Release Writer",
    }
    return data


class TestAgentUpdateDisplayName:
    """``kirocrew agent update <name> --display-name``: relabel, never re-key."""

    def test_relabel_round_trips_through_agent_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["default"]

        _run_agent_cli(cfg_path, "update", "default", "--display-name", "  Scribe  ")

        assert "Updated agent: default (display name: Scribe)" in capsys.readouterr().out
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]
        # Only the label moved: the key and every other field are as they were.
        assert saved["default"] == {**before, "display_name": "Scribe"}
        _run_agent_cli(cfg_path, "list")
        (row,) = [line for line in capsys.readouterr().out.splitlines() if "default *" in line]
        assert row.split()[-1] == "Scribe"

    def test_empty_label_clears_back_to_the_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())

        _run_agent_cli(cfg_path, "update", "scribe", "--display-name", "")

        assert "Updated agent: scribe (display name: scribe)" in capsys.readouterr().out
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["scribe"]["display_name"] == ""

    def test_a_relabel_keeps_a_concurrent_workspace_edit(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.memory_stores import persist_member_config

        cfg_path = _write_config(tmp_path, _labelled_config())

        def publish_after_concurrent_edit(*args, **kwargs):
            assert kwargs["changed_fields"] == {"display_name"}

            def mutate(doc):
                doc["agents"]["research"]["workspace"] = "concurrent-workspace"
                return doc

            update_config_locked(mutate=mutate)
            return persist_member_config(*args, **kwargs)

        with unittest.mock.patch(
            "kiro_crew.cli_commands.persist_member_config",
            side_effect=publish_after_concurrent_edit,
        ):
            _run_agent_cli(cfg_path, "update", "research", "--display-name", "Researcher")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["research"]
        assert saved["display_name"] == "Researcher"
        assert saved["workspace"] == "concurrent-workspace"

    @pytest.mark.parametrize(
        ("label", "reason"),
        [
            ("Two\nLines", "line breaks"),
            ("x" * 1000, "at most"),
            ("..", "periods"),
        ],
    )
    def test_an_unusable_label_is_refused_and_nothing_is_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], label: str, reason: str
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_bytes()

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "research", "--display-name", label)

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "invalid display name" in err and reason in err
        assert cfg_path.read_bytes() == before

    def test_a_credential_shaped_label_is_refused_without_echoing_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_bytes()
        label = "https://user:password@example.com"

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "research", "--display-name", label)

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "credential-shaped" in err
        assert label not in err
        assert cfg_path.read_bytes() == before

    @pytest.mark.parametrize(
        ("name", "label"),
        [
            # Another crew's label.
            ("research", "Release Writer"),
            # Another crew's key, where that crew shows its key.
            ("scribe", "research"),
        ],
    )
    def test_a_name_another_crew_shows_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, label: str
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_bytes()

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", name, "--display-name", label)

        assert exc_info.value.code != 0
        assert "already shows that name" in capsys.readouterr().err
        assert cfg_path.read_bytes() == before

    def test_clearing_to_a_key_another_crew_shows_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _labelled_config()
        data["agents"]["research"]["display_name"] = "scribe"
        cfg_path = _write_config(tmp_path, data)

        with pytest.raises(SystemExit):
            _run_agent_cli(cfg_path, "update", "scribe", "--display-name", "")

        assert "agent 'research' already shows that name" in capsys.readouterr().err

    def test_keeping_its_own_label_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())

        _run_agent_cli(cfg_path, "update", "scribe", "--display-name", "Release Writer")

        assert "Updated agent: scribe (display name: Release Writer)" in capsys.readouterr().out

    def test_an_unknown_agent_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_bytes()

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "nobody", "--display-name", "Scribe")

        assert exc_info.value.code != 0
        assert "agent 'nobody' not found" in capsys.readouterr().err
        assert cfg_path.read_bytes() == before


class TestAgentListLabel:
    """The DISPLAY_NAME cell prints a label one line long, with no live controls."""

    def test_a_label_with_terminal_controls_prints_inert(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _labelled_config()
        data["agents"]["scribe"]["display_name"] = "Ops\x1b[2J\nCrew"
        cfg_path = _write_config(tmp_path, data)

        _run_agent_cli(cfg_path, "list")

        out = capsys.readouterr().out
        assert "\x1b" not in out
        (row,) = [line for line in out.splitlines() if line.startswith("scribe")]
        assert row.endswith("Ops\\x0aCrew")

    def test_a_credential_shaped_label_is_masked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _labelled_config()
        label = "https://user:password@example.com"
        data["agents"]["scribe"]["display_name"] = label
        cfg_path = _write_config(tmp_path, data)

        _run_agent_cli(cfg_path, "list")

        out = capsys.readouterr().out
        assert label not in out
        assert "(hidden: credential-shaped)" in out


class TestAgentDelete:
    """Test ``kirocrew agent delete``."""

    def test_delete_default_agent_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kirocrew", "agent", "delete", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "cannot delete default agent" in err


class TestAgentResetModel:
    """``kirocrew agent reset-model`` -- the explicit way back to the default.

    Goes through ``main()`` rather than calling the handler directly: the verb
    lives on the SAME ``agent`` subparser group as list/create/update/delete, and
    a second group registered under the same name raises
    ``conflicting subparser: agent`` at parser-build time, which breaks the whole
    CLI while a handler-level test still passes. Routing through main() is what
    pins the wiring.
    """

    def _isolated_spec(self, tmp_path: Path, monkeypatch, model: str | None) -> Path:
        """Install a kiro spec in a throwaway agents dir, via the override hook.

        Uses ``agent.KIRO_AGENTS_DIR`` rather than ``KIRO_HOME``: that hook is
        what ``kiro_agents_dir_path`` consults FIRST, so it short-circuits before
        the env-based resolver and is the seam the suite's own agent-spec
        isolation is built on. Setting only ``KIRO_HOME`` would be ignored here.
        The containment assert makes a lapse fail loudly rather than write into a
        real agents directory.
        """
        import kiro_crew.agent as agent_mod

        agents_dir = tmp_path / "kiro-agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
        resolved = agent_mod.kiro_agents_dir_path()
        assert resolved.is_relative_to(tmp_path), f"{resolved} escaped {tmp_path}"
        body: dict = {"name": "kirocrew", "tools": ["fs_read"]}
        if model is not None:
            body["model"] = model
        spec = agents_dir / "kirocrew.json"
        spec.write_text(json.dumps(body), encoding="utf-8")
        return spec

    def test_reset_clears_the_pin_and_keeps_the_rest_of_the_spec(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import agent_state

        spec = self._isolated_spec(tmp_path, monkeypatch, "claude-opus-4.8")

        with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "reset-model"]):
            main()

        written = json.loads(spec.read_text(encoding="utf-8"))
        assert "model" not in written
        assert written["tools"] == ["fs_read"], "reset must not regenerate the whole spec"
        assert agent_state.get_model_managed("kirocrew") is True
        out = capsys.readouterr().out
        assert "claude-opus-4.8" in out, "the cleared value is reported so it can be re-pinned"

    def test_reset_reports_a_spec_that_had_no_pin(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._isolated_spec(tmp_path, monkeypatch, None)

        with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "reset-model"]):
            main()

        assert "had no pinned model" in capsys.readouterr().out

    def test_reset_exits_nonzero_for_an_unknown_agent(self, tmp_path: Path, monkeypatch) -> None:
        self._isolated_spec(tmp_path, monkeypatch, "claude-opus-4.8")

        with unittest.mock.patch(
            "sys.argv", ["kirocrew", "agent", "reset-model", "--agent", "nope"]
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_the_existing_agent_verbs_still_route(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ratchet for the conflicting-subparser class: adding reset-model must
        not shadow or displace the group's original verbs."""
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
        ):
            main()

        assert "KIRO_AGENT" in capsys.readouterr().out


def _wrong_flavour_spelling() -> tuple[str, str]:
    """A registry spelling kiro-cli does not serve, and the id it maps to."""
    from kiro_crew import model_registry

    for entry in model_registry._REGISTRY.values():
        for alias in entry.get("aliases", []):
            correction = model_registry.acp_id_correction(alias)
            if correction:
                return alias, correction
    raise AssertionError("the registry carries no wrong-flavour spelling to test with")


class TestAgentModel:
    """``--model`` on ``agent create`` / ``agent update``, and the list column."""

    def test_create_stores_the_model_on_the_agent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        _run_agent_cli(cfg_path, "create", "--name", "oncall", "--model", "test-model-1")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["oncall"]["model"] == "test-model-1"
        assert "Model: test-model-1" in capsys.readouterr().out

    def test_create_without_a_model_inherits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        _run_agent_cli(cfg_path, "create", "--name", "oncall")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["oncall"]["model"] == ""
        assert "Model:" not in capsys.readouterr().out

    def test_create_stores_auto_as_no_pin(self, tmp_path: Path) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        _run_agent_cli(cfg_path, "create", "--name", "oncall", "--model", "auto")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["oncall"]["model"] == ""

    def test_update_changes_only_the_model(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["research"]

        _run_agent_cli(cfg_path, "update", "research", "--model", "test-model-1")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["research"]
        assert saved == {**before, "model": "test-model-1"}
        out = capsys.readouterr().out
        assert "Updated agent: research" in out
        assert "Model: test-model-1" in out

    def test_update_without_the_flag_keeps_the_model(self, tmp_path: Path) -> None:
        data = _labelled_config()
        data["agents"]["research"]["model"] = "test-model-1"
        cfg_path = _write_config(tmp_path, data)

        _run_agent_cli(cfg_path, "update", "research", "--workspace", "default")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["model"] == "test-model-1"

    def test_an_empty_model_clears_the_pin(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _labelled_config()
        data["agents"]["research"]["model"] = "test-model-1"
        cfg_path = _write_config(tmp_path, data)

        _run_agent_cli(cfg_path, "update", "research", "--model", "")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["model"] == ""
        assert "Model: (inherit)" in capsys.readouterr().out

    def test_a_model_update_keeps_a_concurrent_workspace_edit(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.memory_stores import persist_member_config

        cfg_path = _write_config(tmp_path, _labelled_config())

        def publish_after_concurrent_edit(*args, **kwargs):
            assert kwargs["changed_fields"] == {"model"}

            def mutate(doc):
                doc["agents"]["research"]["workspace"] = "elsewhere"
                return doc

            update_config_locked(mutate=mutate)
            return persist_member_config(*args, **kwargs)

        with unittest.mock.patch(
            "kiro_crew.cli_commands.persist_member_config",
            side_effect=publish_after_concurrent_edit,
        ):
            _run_agent_cli(cfg_path, "update", "research", "--model", "test-model-1")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["research"]
        assert saved["model"] == "test-model-1"
        assert saved["workspace"] == "elsewhere"

    def test_list_shows_the_pin_or_inherit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _labelled_config()
        data["agents"]["research"]["model"] = "test-model-1"
        cfg_path = _write_config(tmp_path, data)

        _run_agent_cli(cfg_path, "list")

        lines = capsys.readouterr().out.splitlines()
        column = lines[0].split().index("MODEL")
        rows = {line.split()[0]: line.split() for line in lines[1:]}
        assert rows["research"][column] == "test-model-1"
        # The default row's " *" marker is one extra token before MODEL.
        assert rows["default"][column + 1] == "(inherit)"

    @pytest.mark.parametrize("bad", ["-rf", "a b", "x" * 501, "../model"])
    def test_a_malformed_model_is_refused_without_echoing_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "research", f"--model={bad}")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert f"invalid model format ({MODEL_NAME_HINT})" in err
        assert bad not in err
        assert cfg_path.read_text(encoding="utf-8") == before

    def test_create_refuses_a_malformed_model_before_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "create", "--name", "oncall", "--model", "a b")

        assert exc_info.value.code == 1
        assert f"invalid model format ({MODEL_NAME_HINT})" in capsys.readouterr().err
        assert cfg_path.read_text(encoding="utf-8") == before

    def test_a_wrong_flavour_spelling_is_refused_with_the_served_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wrong, served = _wrong_flavour_spelling()
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "research", "--model", wrong)

        assert exc_info.value.code == 1
        assert repr(served) in capsys.readouterr().err
        assert cfg_path.read_text(encoding="utf-8") == before

    def test_a_pin_kiro_cli_has_no_id_for_is_refused_and_names_the_spelling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _labelled_config())
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "update", "research", "--model", "claude-fable-5-1")

        assert exc_info.value.code == 1
        assert "'claude-fable-5-1' is not a model kiro-cli serves" in capsys.readouterr().err
        assert cfg_path.read_text(encoding="utf-8") == before

    def test_a_pin_kiro_cli_has_no_id_for_is_stored_on_the_claude_backend(
        self, tmp_path: Path
    ) -> None:
        config = _labelled_config()
        config["agent"] = {"acp_backend": "claude", "member_acp_backend": "claude"}
        cfg_path = _write_config(tmp_path, config)

        _run_agent_cli(cfg_path, "update", "research", "--model", "claude-fable-5-1")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["research"]["model"] == "claude-fable-5-1"

    def test_a_display_only_canonical_key_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        from kiro_crew import model_registry

        # Every registered key also has a wrong-flavour mapping today, which is
        # checked first; take it away to reach the display-only refusal.
        key = next(k for k in model_registry._REGISTRY if k != "auto")
        monkeypatch.setattr(model_registry, "acp_id_correction", lambda _model: None)
        cfg_path = _write_config(tmp_path, _base_config())
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _run_agent_cli(cfg_path, "create", "--name", "oncall", "--model", key)

        assert exc_info.value.code == 1
        assert "display-only" in capsys.readouterr().err
        assert cfg_path.read_text(encoding="utf-8") == before

    def test_a_canonical_key_the_backend_translates_is_accepted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The display-only refusal is the crew editor's backend-aware gate, not a
        # bare registry lookup: on a harness whose namespace carries an id for the
        # key (claude maps fable-5-1m), the crew editor accepts it, so the CLI must.
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "acp_id_correction", lambda _model: None)
        config = _base_config()
        config["agent"] = {"acp_backend": "claude", "member_acp_backend": "claude"}
        cfg_path = _write_config(tmp_path, config)

        _run_agent_cli(cfg_path, "create", "--name", "oncall", "--model", "fable-5-1m")

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["oncall"]["model"] == "fable-5-1m"


class TestRefusalHintsMatchTheirGrammar:
    """The hint a refusal prints describes what its pattern actually admits."""

    @pytest.mark.parametrize("name", ["a", "a" * 63, "dr.eggbot", "my_agent-2"])
    def test_template_hint_admits(self, name: str) -> None:
        assert TEMPLATE_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["", "a" * 64, "-agent", "agent.", "dr. eggbot"])
    def test_template_hint_refuses(self, name: str) -> None:
        assert not TEMPLATE_NAME_RE.fullmatch(name)

    def test_template_hint_states_the_length_bound(self) -> None:
        assert "1-63" in TEMPLATE_NAME_HINT

    @pytest.mark.parametrize(
        "model", ["auto", "test-model-1", "gpt-6-astra[high]", "ollama/qwen3:8b", "a/b/c"]
    )
    def test_model_hint_admits(self, model: str) -> None:
        assert _MODEL_NAME_RE.match(model)

    @pytest.mark.parametrize("model", ["-rf", "a b", "../model", "/abs", "a[x][y]"])
    def test_model_hint_refuses(self, model: str) -> None:
        assert not _MODEL_NAME_RE.match(model)

    def test_model_hint_names_auto_and_the_qualified_form(self) -> None:
        assert "'auto'" in MODEL_NAME_HINT
        assert "provider/model" in MODEL_NAME_HINT
