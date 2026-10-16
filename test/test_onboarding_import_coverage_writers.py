"""Branch coverage for the :mod:`kiro_crew.onboarding_import` writers.

Every writer's conflict / rollback branch (skills, MCP servers, workspace mappings,
instructions, memory, schedules), the JSON loader and replace-preserving helpers
they share, and the ``apply_import`` failure paths.

Everything runs against ``tmp_path`` with no network, no subprocesses and no
real foreign-agent install.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from onboarding_import_coverage_helpers import _POSIX_FS_ONLY

from kiro_crew import onboarding_apply, onboarding_scan

# Keep apply_import's MCP sidecar lock off the real ~/.kiro/settings (see the
# shared fixture in test/conftest.py).
pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")


def _api() -> ModuleType:
    return importlib.import_module("kiro_crew.onboarding_import")


class _StubVectorStore:
    """Minimal stand-in for the parts of VectorMemoryStore the writers touch."""

    def __init__(
        self,
        *,
        lessons: list[dict[str, Any]] | None = None,
        semantic: dict[str, Any] | None = None,
        absent_result: str = "imported",
        episodic_present: bool = False,
        episodic_written: bool = True,
    ) -> None:
        self._lessons = lessons or []
        self._semantic = semantic or {}
        self._absent_result = absent_result
        self._episodic_present = episodic_present
        self._episodic_written = episodic_written
        self.written: list[str] = []

    def get_lessons(self) -> list[dict[str, Any]]:
        return self._lessons

    def set_semantic_if_absent(self, key: str, value: Any, *_rest: Any) -> str:
        if self._absent_result == "imported":
            self._semantic[key] = {"value_json": json.dumps(value)}
        return self._absent_result

    def get_semantic(self, key: str) -> dict[str, Any] | None:
        return self._semantic.get(key)

    def has_episodic_text(self, _text: str) -> bool:
        return self._episodic_present

    def write_episodic(self, text: str, **_kwargs: Any) -> bool:
        self.written.append(text)
        return self._episodic_written


class _StubLessonStore:
    def __init__(self, rules: list[str] | None = None, *, loadable: bool = True) -> None:
        self._rules = [SimpleNamespace(rule=rule) for rule in (rules or [])]
        self.saved: list[Any] = []
        if not loadable:
            self.load_all = None  # type: ignore[assignment]

    def load_all(self) -> list[Any]:
        return list(self._rules)

    def save(self, lesson: Any) -> None:
        self.saved.append(lesson)


class TestLoadJsonDict:
    def test_a_missing_file_is_an_empty_mapping(self, tmp_path: Path) -> None:
        assert onboarding_apply._load_json_dict(tmp_path / "absent.json") == {}

    def test_invalid_json_is_tolerated_when_open(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{oops", encoding="utf-8")

        assert onboarding_apply._load_json_dict(path) == {}

    def test_invalid_json_raises_when_fail_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{oops", encoding="utf-8")

        with pytest.raises(ValueError):
            onboarding_apply._load_json_dict(path, fail_closed=True)

    def test_a_non_object_document_raises_when_fail_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2]", encoding="utf-8")

        assert onboarding_apply._load_json_dict(path) == {}
        with pytest.raises(ValueError):
            onboarding_apply._load_json_dict(path, fail_closed=True)


class TestPreserveReplaced:
    def test_a_colliding_restore_tree_is_suffixed(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_text("one", encoding="utf-8")
        destination = tmp_path / "restore" / "pkg"
        destination.mkdir(parents=True)

        target = onboarding_apply._preserve_replaced_tree(source, destination)

        assert Path(target).name == "pkg-1"
        assert (Path(target) / "a.txt").read_text(encoding="utf-8") == "one"

    def test_a_colliding_restore_json_is_suffixed(self, tmp_path: Path) -> None:
        destination = tmp_path / "restore" / "srv.json"
        destination.parent.mkdir(parents=True)
        destination.write_text("{}", encoding="utf-8")

        target = onboarding_apply._preserve_replaced_json({"a": 1}, destination)

        assert Path(target).name == "srv-1.json"
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"a": 1}

    def test_the_restore_dir_is_run_and_category_scoped(self, tmp_path: Path) -> None:

        path = onboarding_apply._restore_dir(tmp_path, "20260101T000000Z", "skills")

        assert (
            path
            == tmp_path / onboarding_apply._REPLACED_RELATIVE_DIR / "20260101T000000Z" / "skills"
        )


class TestSkillTreeState:
    def test_an_absent_destination_is_absent(self, tmp_path: Path) -> None:

        state = onboarding_apply._skill_tree_state(
            tmp_path / "skills" / "demo", {"SKILL.md": "x"}, tmp_path
        )

        assert state == "absent"

    def test_an_identical_tree_is_existing(self, tmp_path: Path) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("x", encoding="utf-8")

        assert (
            onboarding_apply._skill_tree_state(destination, {"SKILL.md": "x"}, tmp_path)
            == "existing"
        )

    def test_a_differing_file_is_a_conflict(self, tmp_path: Path) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("other", encoding="utf-8")

        assert (
            onboarding_apply._skill_tree_state(destination, {"SKILL.md": "x"}, tmp_path)
            == "conflict"
        )

    def test_an_extra_installed_file_is_a_conflict(self, tmp_path: Path) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("x", encoding="utf-8")
        (destination / "stale.md").write_text("gone upstream", encoding="utf-8")

        assert (
            onboarding_apply._skill_tree_state(destination, {"SKILL.md": "x"}, tmp_path)
            == "conflict"
        )

    def test_a_partially_present_tree_is_a_conflict(self, tmp_path: Path) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("x", encoding="utf-8")

        state = onboarding_apply._skill_tree_state(
            destination, {"SKILL.md": "x", "ref.md": "y"}, tmp_path
        )

        assert state == "conflict"

    def test_an_occupied_but_unrelated_directory_is_a_conflict(self, tmp_path: Path) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)
        (destination / "unrelated.md").write_text("x", encoding="utf-8")

        assert (
            onboarding_apply._skill_tree_state(destination, {"SKILL.md": "x"}, tmp_path)
            == "conflict"
        )

    def test_a_link_like_component_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "skills").mkdir()
        monkeypatch.setattr(onboarding_scan, "_is_link_like", lambda *_args, **_kwargs: True)

        state = onboarding_apply._skill_tree_state(
            tmp_path / "skills" / "demo", {"SKILL.md": "x"}, tmp_path
        )

        assert state == "rejected"

    def test_a_path_outside_the_data_home_has_a_symlink_component(self, tmp_path: Path) -> None:

        assert (
            onboarding_apply._has_symlink_component(tmp_path.parent / "elsewhere", tmp_path) is True
        )

    @pytest.mark.parametrize(
        "files",
        [
            "not-a-dict",
            {"other.md": "x"},
            {"SKILL.md": 5},
            {5: "x"},
            # POSIX-absolute only: on Windows an absolute path is `C:\...`, so
            # "/abs.md" is a relative name there and is legitimately accepted.
            # Marked per-param so the other five cases still run on Windows.
            pytest.param({"SKILL.md": "x", "/abs.md": "y"}, marks=_POSIX_FS_ONLY),
            {"SKILL.md": "x", "../escape.md": "y"},
        ],
    )
    def test_invalid_skill_file_maps_are_rejected(self, files: Any) -> None:
        assert onboarding_apply._skill_files_are_valid(files) is False

    def test_a_valid_skill_file_map_is_accepted(self) -> None:
        assert onboarding_apply._skill_files_are_valid({"SKILL.md": "x", "ref/a.md": "y"}) is True


class TestSkillWriter:
    def _item(self, api: ModuleType, files: Any = None) -> Any:
        return api._Item(
            "codex",
            "skills",
            "demo",
            {"name": "demo", "files": files or {"SKILL.md": "new body"}},
        )

    def test_an_invalid_payload_is_rejected(self, tmp_path: Path) -> None:
        api = _api()

        assert (
            onboarding_apply._write_skill(self._item(api, {"no-manifest": "x"}), tmp_path).status
            == "rejected"
        )

    def test_a_fresh_install_lands_the_tree(self, tmp_path: Path) -> None:
        api = _api()

        outcome = onboarding_apply._write_skill(self._item(api), tmp_path)

        installed = tmp_path / "skills" / "imported" / "codex" / "demo" / "SKILL.md"
        assert outcome.status == "imported"
        assert installed.read_text(encoding="utf-8") == "new body"
        assert outcome.destination_key == "skills:codex/demo"

    def test_a_reinstall_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        onboarding_apply._write_skill(self._item(api), tmp_path)

        assert onboarding_apply._write_skill(self._item(api), tmp_path).status == "existing"

    def test_a_colliding_package_is_a_conflict_under_skip(self, tmp_path: Path) -> None:
        api = _api()
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        assert onboarding_apply._write_skill(self._item(api), tmp_path).status == "conflict"

    def test_rename_installs_alongside_the_incumbent(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        outcome = onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_RENAME)

        assert outcome.status == "imported"
        assert outcome.renamed_to == "demo-codex"
        assert (destination.parent / "demo-codex" / "SKILL.md").exists()
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "theirs"

    def test_rename_reports_existing_when_the_renamed_copy_matches(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        root = tmp_path / "skills" / "imported" / "codex"
        (root / "demo").mkdir(parents=True)
        (root / "demo" / "SKILL.md").write_text("theirs", encoding="utf-8")
        (root / "demo-codex").mkdir()
        (root / "demo-codex" / "SKILL.md").write_text("new body", encoding="utf-8")

        outcome = onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_RENAME)

        assert (outcome.status, outcome.renamed_to) == ("existing", "demo-codex")

    def test_rename_gives_up_when_every_candidate_is_occupied(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        root = tmp_path / "skills" / "imported" / "codex"
        for name in ("demo", "demo-codex", f"demo-{item.fingerprint[:8]}"):
            (root / name).mkdir(parents=True)
            (root / name / "SKILL.md").write_text("theirs", encoding="utf-8")

        assert (
            onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_RENAME).status
            == "conflict"
        )

    def test_overwrite_keeps_a_restore_copy_and_replaces_the_tree(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        outcome = onboarding_apply._write_skill(
            item,
            tmp_path,
            strategy=api.STRATEGY_OVERWRITE,
            run_stamp="20260101T000000Z",
        )

        assert outcome.status == "imported"
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "new body"
        assert Path(outcome.restored_to, "SKILL.md").read_text(encoding="utf-8") == "theirs"

    def test_overwrite_refuses_when_the_restore_copy_cannot_be_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        def boom(*_args: Any, **_kwargs: Any) -> str:
            raise OSError("no space")

        monkeypatch.setattr(onboarding_apply, "_preserve_replaced_tree", boom)

        outcome = onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_OVERWRITE)

        assert outcome.status == "conflict"
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "theirs"

    def test_overwrite_restores_the_original_when_the_install_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")
        monkeypatch.setattr(onboarding_apply, "_install_skill_tree", lambda *_a, **_k: "rejected")

        outcome = onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_OVERWRITE)

        assert outcome.status == "rejected"
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "theirs"

    def test_overwrite_restores_the_original_when_the_install_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        def boom(*_args: Any, **_kwargs: Any) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(onboarding_apply, "_install_skill_tree", boom)

        with pytest.raises(KeyboardInterrupt):
            onboarding_apply._write_skill(item, tmp_path, strategy=api.STRATEGY_OVERWRITE)

        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "theirs"

    def test_overwrite_refuses_when_the_move_aside_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        item = self._item(api)
        destination = tmp_path / "skills" / "imported" / "codex" / "demo"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("theirs", encoding="utf-8")

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("locked")

        monkeypatch.setattr(api.os, "replace", boom)

        assert onboarding_apply._write_skill(
            item, tmp_path, strategy=api.STRATEGY_OVERWRITE
        ).status == ("conflict")

    def test_install_reports_a_conflict_when_the_name_appears_mid_flight(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "skills" / "demo"
        destination.mkdir(parents=True)

        assert (
            onboarding_apply._install_skill_tree(destination, {"SKILL.md": "x"}, tmp_path)
            == "conflict"
        )

    def test_install_refuses_a_link_like_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(onboarding_apply, "_has_symlink_component", lambda *_a, **_k: True)

        assert (
            onboarding_apply._install_skill_tree(tmp_path / "demo", {"SKILL.md": "x"}, tmp_path)
            == "rejected"
        )


class TestMcpWriter:
    def _item(self, api: ModuleType, spec: Any = None) -> Any:
        return api._Item(
            "codex",
            "mcp_servers",
            "srv",
            {"name": "srv", "spec": spec or {"command": "srv-bin", "disabled": True}},
        )

    def test_a_non_mapping_servers_block_is_a_conflict(self, tmp_path: Path) -> None:
        api = _api()
        (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": []}), encoding="utf-8")

        outcome = onboarding_apply._write_mcp(self._item(api), tmp_path, tmp_path / "home")

        assert outcome.status == "conflict"

    def test_an_identical_definition_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": item.payload["spec"]}}), encoding="utf-8"
        )

        outcome = onboarding_apply._write_mcp(item, tmp_path, tmp_path / "home")

        assert (outcome.status, outcome.destination_key) == ("existing", "srv")

    def test_a_different_definition_is_a_conflict_under_skip(self, tmp_path: Path) -> None:
        api = _api()
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "theirs"}}}), encoding="utf-8"
        )

        assert (
            onboarding_apply._write_mcp(self._item(api), tmp_path, tmp_path / "home").status
            == "conflict"
        )

    def test_rename_installs_under_a_derived_name(self, tmp_path: Path) -> None:
        api = _api()
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "theirs"}}}), encoding="utf-8"
        )

        outcome = onboarding_apply._write_mcp(
            self._item(api), tmp_path, tmp_path / "home", strategy=api.STRATEGY_RENAME
        )

        written = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        assert (outcome.status, outcome.renamed_to) == ("imported", "srv-codex")
        assert written["mcpServers"]["srv"] == {"command": "theirs"}
        assert written["mcpServers"]["srv-codex"]["command"] == "srv-bin"

    def test_rename_reports_existing_when_the_derived_name_already_matches(
        self, tmp_path: Path
    ) -> None:
        api = _api()
        item = self._item(api)
        (tmp_path / "mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"srv": {"command": "theirs"}, "srv-codex": item.payload["spec"]}}
            ),
            encoding="utf-8",
        )

        outcome = onboarding_apply._write_mcp(
            item, tmp_path, tmp_path / "home", strategy=api.STRATEGY_RENAME
        )

        assert (outcome.status, outcome.renamed_to) == ("existing", "srv-codex")

    def test_rename_gives_up_when_every_candidate_differs(self, tmp_path: Path) -> None:
        api = _api()
        item = self._item(api)
        (tmp_path / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {"command": "a"},
                        "srv-codex": {"command": "b"},
                        f"srv-{item.fingerprint[:8]}": {"command": "c"},
                    }
                }
            ),
            encoding="utf-8",
        )

        outcome = onboarding_apply._write_mcp(
            item, tmp_path, tmp_path / "home", strategy=api.STRATEGY_RENAME
        )

        assert outcome.status == "conflict"

    def test_overwrite_writes_a_restore_copy_first(self, tmp_path: Path) -> None:
        api = _api()
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "theirs"}}}), encoding="utf-8"
        )

        outcome = onboarding_apply._write_mcp(
            self._item(api),
            tmp_path,
            tmp_path / "home",
            strategy=api.STRATEGY_OVERWRITE,
            run_stamp="20260101T000000Z",
        )

        written = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        restored = json.loads(Path(outcome.restored_to).read_text(encoding="utf-8"))
        assert outcome.status == "imported"
        assert written["mcpServers"]["srv"]["command"] == "srv-bin"
        assert restored == {"srv": {"command": "theirs"}}

    def test_overwrite_cannot_claim_a_name_it_does_not_hold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        item = self._item(api)

        def reserved(**_kwargs: Any) -> set[str]:
            return {"srv"}

        module = importlib.import_module("kiro_crew.mcp_discovery")
        monkeypatch.setattr(module, "configured_mcp_aliases", reserved)

        outcome = onboarding_apply._write_mcp(
            item, tmp_path, tmp_path / "home", strategy=api.STRATEGY_OVERWRITE
        )

        assert outcome.status == "conflict"

    def test_overwrite_refuses_when_the_restore_copy_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api()
        (tmp_path / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "theirs"}}}), encoding="utf-8"
        )

        def boom(*_args: Any, **_kwargs: Any) -> str:
            raise OSError("read-only")

        monkeypatch.setattr(onboarding_apply, "_preserve_replaced_json", boom)

        outcome = onboarding_apply._write_mcp(
            self._item(api), tmp_path, tmp_path / "home", strategy=api.STRATEGY_OVERWRITE
        )

        written = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        assert outcome.status == "conflict"
        assert written["mcpServers"]["srv"] == {"command": "theirs"}


class TestWorkspaceWriter:
    def test_a_vanished_workspace_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        item = api._Item("codex", "workspaces", "w", str(tmp_path / "gone"))

        assert onboarding_apply._write_workspace(item, tmp_path).status == "rejected"

    def test_the_data_home_itself_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        item = api._Item("codex", "workspaces", "w", str(data_home))

        assert onboarding_apply._write_workspace(item, data_home).status == "rejected"

    def test_a_non_mapping_workspaces_block_is_a_conflict(self, tmp_path: Path) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        workspace = tmp_path / "project"
        workspace.mkdir()
        (data_home / "config.json").write_text(json.dumps({"workspaces": []}), encoding="utf-8")
        item = api._Item("codex", "workspaces", "w", str(workspace))

        assert onboarding_apply._write_workspace(item, data_home).status == "conflict"

    def test_a_mistyped_existing_entry_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        workspace = tmp_path / "project"
        workspace.mkdir()
        (data_home / "config.json").write_text(
            json.dumps({"workspaces": {"a": 5, "b": {"dir": 7}, "c": {"other": "x"}}}),
            encoding="utf-8",
        )
        item = api._Item("codex", "workspaces", "w", str(workspace))

        assert onboarding_apply._write_workspace(item, data_home).status == "imported"

    def test_an_already_mapped_directory_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        workspace = tmp_path / "project"
        workspace.mkdir()
        (data_home / "config.json").write_text(
            json.dumps({"workspaces": {"other": str(workspace)}}), encoding="utf-8"
        )
        item = api._Item("codex", "workspaces", "w", str(workspace))

        assert onboarding_apply._write_workspace(item, data_home).status == "existing"

    @_POSIX_FS_ONLY
    def test_an_unnameable_directory_falls_back_to_a_source_scoped_name(
        self, tmp_path: Path
    ) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        workspace = tmp_path / "..."
        workspace.mkdir()
        item = api._Item("codex", "workspaces", "w", str(workspace))

        outcome = onboarding_apply._write_workspace(item, data_home)
        written = json.loads((data_home / "config.json").read_text(encoding="utf-8"))

        assert outcome.status == "imported"
        assert "imported-codex" in written["workspaces"]

    def test_rename_derives_a_free_name(self, tmp_path: Path) -> None:
        api = _api()
        data_home = tmp_path / "dest"
        data_home.mkdir()
        workspace = tmp_path / "project"
        workspace.mkdir()
        (data_home / "config.json").write_text(
            json.dumps({"workspaces": {"project": {"dir": str(tmp_path / "other")}}}),
            encoding="utf-8",
        )
        item = api._Item("codex", "workspaces", "w", str(workspace))

        outcome = onboarding_apply._write_workspace(item, data_home, strategy=api.STRATEGY_RENAME)

        assert (outcome.status, outcome.renamed_to) == ("imported", "project-codex")


class TestInstructionWriter:
    def test_a_blank_rule_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        item = api._Item("codex", "instructions", "i", {"rule": "   "})

        assert api._write_instruction(item, None).status == "rejected"

    def test_no_available_store_is_rejected(self) -> None:
        api = _api()
        item = api._Item("codex", "instructions", "i", {"rule": "Always run the linter."})

        assert api._write_instruction(item, None).status == "rejected"

    def test_an_overlapping_vector_lesson_is_never_replaced(self) -> None:
        api = _api()
        store: Any = _StubVectorStore(
            lessons=[
                {"value_json": "not json"},
                {"value_json": json.dumps({"rule": "Always run the linter before pushing."})},
            ]
        )
        item = api._Item("codex", "instructions", "i", {"rule": "always run the linter"})

        assert api._write_instruction(item, None, store).status == "existing"

    def test_a_new_vector_lesson_is_inserted_under_a_digest_key(self) -> None:
        api = _api()
        store: Any = _StubVectorStore(lessons=[{"value_json": json.dumps("unrelated topic")}])
        item = api._Item("codex", "instructions", "i", {"rule": "Pin every dependency version."})

        assert api._write_instruction(item, None, store).status == "imported"

    def test_a_vector_insert_that_loses_the_race_reports_existing(self) -> None:
        api = _api()
        store: Any = _StubVectorStore(absent_result="existing")
        item = api._Item("codex", "instructions", "i", {"rule": "Pin every dependency version."})

        assert api._write_instruction(item, None, store).status == "existing"

    def test_an_exact_duplicate_jsonl_lesson_reports_existing(self) -> None:
        api = _api()
        store = _StubLessonStore(["Always run the linter."])
        item = api._Item("codex", "instructions", "i", {"rule": "always run the linter."})

        assert api._write_instruction(item, store).status == "existing"
        assert store.saved == []

    def test_a_full_lesson_store_refuses_the_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _api()
        monkeypatch.setattr(api, "_MAX_LESSONS_TOTAL", 1)
        store = _StubLessonStore(["Existing user correction."])
        item = api._Item("codex", "instructions", "i", {"rule": "Pin every dependency."})

        assert api._write_instruction(item, store).status == "rejected"
        assert store.saved == []

    def test_a_store_without_load_all_is_written_straight_through(self) -> None:
        api = _api()
        store = _StubLessonStore(loadable=False)
        item = api._Item("codex", "instructions", "i", {"rule": "Pin every dependency."})

        assert api._write_instruction(item, store).status == "imported"
        assert len(store.saved) == 1


class TestMemoryWriter:
    def test_an_unknown_payload_kind_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        item = api._Item("codex", "memories", "m", {"kind": "other"})

        assert api._write_memory(item, tmp_path, None).status == "rejected"

    @pytest.mark.parametrize("kind", ["semantic", "episodic"])
    def test_a_missing_store_is_rejected(self, tmp_path: Path, kind: str) -> None:
        api = _api()
        payload = {"kind": kind, "key": "pref.a", "value": 1, "confidence": 1.0, "text": "x" * 20}
        item = api._Item("codex", "memories", "m", payload)

        assert api._write_memory(item, tmp_path, None).status == "rejected"

    def test_a_semantic_row_that_vanished_after_the_race_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        store: Any = _StubVectorStore(absent_result="existing")
        payload = {"kind": "semantic", "key": "pref.a", "value": "v", "confidence": 1.0}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, store)

        assert outcome.status == "rejected"

    def test_a_semantic_row_with_an_unreadable_value_is_a_conflict(self, tmp_path: Path) -> None:
        api = _api()
        store: Any = _StubVectorStore(
            absent_result="existing", semantic={"pref.a": {"value_json": "{oops"}}
        )
        payload = {"kind": "semantic", "key": "pref.a", "value": "v", "confidence": 1.0}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, store)

        assert outcome.status == "conflict"

    def test_an_identical_semantic_row_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        store: Any = _StubVectorStore(
            absent_result="existing", semantic={"pref.a": {"value_json": json.dumps("v")}}
        )
        payload = {"kind": "semantic", "key": "pref.a", "value": "v", "confidence": 1.0}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, store)

        assert outcome.status == "existing"

    def test_an_episodic_row_already_present_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        store: Any = _StubVectorStore(episodic_present=True)
        payload = {"kind": "episodic", "text": "a durable note", "importance": 0.5}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, store)

        assert outcome.status == "existing"

    def test_an_episodic_write_that_reports_false_is_rejected(self, tmp_path: Path) -> None:
        api = _api()
        store: Any = _StubVectorStore(episodic_written=False)
        payload = {"kind": "episodic", "text": "a durable note", "importance": 0.5}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, store)

        assert outcome.status == "rejected"

    def test_an_episodic_row_is_written_deferred(self, tmp_path: Path) -> None:
        api = _api()
        store = _StubVectorStore()
        typed: Any = store
        payload = {"kind": "episodic", "text": "a durable note", "importance": 0.5}

        outcome = api._write_memory(api._Item("codex", "memories", "m", payload), tmp_path, typed)

        assert outcome.status == "imported"
        assert store.written == ["a durable note"]


class TestScheduleWriter:
    def test_a_cron_service_without_the_atomic_helper_falls_back_to_scanning(self) -> None:
        api = _api()
        payload = {"name": "n", "message": "m", "every_secs": 120}
        added: list[dict[str, Any]] = []

        class Service:
            def list_jobs(self, include_disabled: bool = False) -> list[Any]:
                return []

            def add_job(self, **kwargs: Any) -> None:
                added.append(kwargs)

        service: Any = Service()
        outcome = api._write_schedule(api._Item("codex", "schedules", "s", payload), service)

        assert outcome.status == "imported"
        assert added[0]["created_by"] == "import:codex"
        assert added[0]["enabled"] is False

    def test_an_equivalent_existing_job_reports_existing(self) -> None:
        api = _api()
        payload = {"name": "n", "message": "m", "every_secs": 120}
        job = SimpleNamespace(
            name="n", message="m", timezone="", schedule=SimpleNamespace(every_secs=120)
        )

        class Service:
            def list_jobs(self, include_disabled: bool = False) -> list[Any]:
                return [job]

            def add_job(self, **_kwargs: Any) -> None:
                raise AssertionError("must not insert a duplicate")

        service: Any = Service()

        assert api._write_schedule(
            api._Item("codex", "schedules", "s", payload), service
        ).status == ("existing")

    def test_the_atomic_helper_reports_existing_when_it_returns_none(self) -> None:
        api = _api()
        payload = {"name": "n", "message": "m", "cron_expr": "0 * * * *"}
        service: Any = SimpleNamespace(add_job_if_absent=lambda *_a, **_k: None)

        assert api._write_schedule(
            api._Item("codex", "schedules", "s", payload), service
        ).status == ("existing")

    @pytest.mark.parametrize(
        "job",
        [
            SimpleNamespace(name="other", message="m", timezone="", schedule=SimpleNamespace()),
            SimpleNamespace(name="n", message="other", timezone="", schedule=SimpleNamespace()),
            SimpleNamespace(name="n", message="m", timezone="UTC", schedule=SimpleNamespace()),
            SimpleNamespace(name="n", message="m", timezone="", schedule=None),
            SimpleNamespace(
                name="n", message="m", timezone="", schedule=SimpleNamespace(cron_expr="1 * * * *")
            ),
        ],
    )
    def test_same_schedule_rejects_every_mismatch(self, job: Any) -> None:
        api = _api()
        payload = {"name": "n", "message": "m", "cron_expr": "0 * * * *"}

        assert api._same_schedule(job, payload) is False

    def test_same_schedule_compares_the_one_shot_timestamp(self) -> None:
        api = _api()
        payload = {"name": "n", "message": "m", "at_ts": 100.0}
        job = SimpleNamespace(
            name="n", message="m", timezone="", schedule=SimpleNamespace(at_ts=100.0)
        )

        assert api._same_schedule(job, payload) is True


class TestApplyImportFailurePaths:
    def _codex_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text('timezone = "Europe/London"\n', encoding="utf-8")
        return home

    def test_a_destination_that_cannot_be_read_is_reported_as_a_write_failure(
        self, tmp_path: Path
    ) -> None:
        api = _api()
        home = self._codex_home(tmp_path)
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "config.json").write_text("{ not json", encoding="utf-8")
        plan = api.preview_import(["codex"], home=home, env={"HOME": str(home)})

        result = api.apply_import(plan, data_home=destination)

        assert result["imported_count"] == 0
        assert {entry["reason"] for entry in result["skipped"]} >= {"write_failed"}
        assert [entry["outcome"] for entry in result["item_outcomes"]] == ["rejected"]

    def test_an_unavailable_source_is_skipped_rather_than_scanned(self, tmp_path: Path) -> None:
        api = _api()
        destination = tmp_path / "dest"
        plan = {
            "sources": [
                {"id": "codex", "root": str(tmp_path / "gone"), "user_home": str(tmp_path)}
            ],
            "selection": [{"source_id": "codex", "category_id": "settings"}],
        }

        result = api.apply_import(plan, data_home=destination)

        assert result["skipped"] == [
            {"source_id": "codex", "category_id": "settings", "reason": "source_unavailable"}
        ]
        assert result["conflict_strategy"] == api.STRATEGY_SKIP

    def test_a_selection_naming_no_known_source_writes_nothing(self, tmp_path: Path) -> None:
        api = _api()

        result = api.apply_import({"selection": []}, data_home=tmp_path / "dest")

        assert result["imported_count"] == 0
        assert result["ledger"] == "imports/foreign-agent-imports.json"

    def test_settings_import_is_idempotent_through_the_ledger(self, tmp_path: Path) -> None:
        api = _api()
        home = self._codex_home(tmp_path)
        destination = tmp_path / "dest"
        plan = api.preview_import(["codex"], home=home, env={"HOME": str(home)})

        first = api.apply_import(plan, data_home=destination)
        second = api.apply_import(plan, data_home=destination)

        config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
        assert first["imported"]["settings"] == 1
        assert second["already_imported"] == 1
        assert config["timezone"] == "Europe/London"

    def test_an_unchanged_settings_write_reports_existing(self, tmp_path: Path) -> None:
        api = _api()
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "config.json").write_text(
            json.dumps({"timezone": "Europe/London"}), encoding="utf-8"
        )
        item = api._Item("codex", "settings", "s", {"timezone": "Europe/London"})

        assert onboarding_apply._write_settings(item, destination).status == "existing"
