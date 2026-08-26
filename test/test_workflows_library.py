"""Durable reusable workflow definitions: revisions, lineage, and matching."""

from __future__ import annotations

import json

import pytest

from kiro_crew import security
from kiro_crew.workflows import store as workflow_store
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.store import WORKFLOW_LIBRARY_DIR_NAME

SOURCE_V1 = (
    'META = {"name": "debug-project", "description": "Investigate a project failure"}\n'
    "async def workflow(ctx):\n"
    "    return await ctx.agent('debug the project')\n"
)

SOURCE_V2 = SOURCE_V1.replace("debug the project", "debug and explain the project")


def test_default_library_uses_the_protected_data_home_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workflow_store, "config_dir", lambda: tmp_path)

    library = WorkflowDefinitionLibrary()

    assert library.library_dir == tmp_path / "workflow_library"


class TestWorkflowLibraryProtection:
    def test_library_directory_is_a_keystone_leaf(self) -> None:
        assert WORKFLOW_LIBRARY_DIR_NAME in security._CREW_SECRET_LEAVES

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_definition_files_are_sensitive_under_every_home_prefix(self, prefix: str) -> None:
        path = f"~/{prefix}/workflow_library/wfd_example.json"

        assert security.is_sensitive_path(path) is True
        assert security.is_sensitive_write_path(path) is True

    def test_shell_reads_writes_and_extracts_are_blocked(self) -> None:
        commands = (
            "cat ~/.kiro/crew/workflow_library/wfd_example.json",
            "echo '{}' > ~/.kiro/crew/workflow_library/wfd_example.json",
            "tar -xf planted.tar -C ~/.kiro/crew/workflow_library",
        )

        assert all(security.is_sensitive_bash_command(command) is not None for command in commands)


def test_create_round_trips_a_global_definition_with_lineage(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)

    created = library.create(
        source=SOURCE_V1,
        name="Debug Project",
        description="Investigate a project failure",
        derived_from={"workflow_id": "wfd_parent", "revision": 3},
    )

    restored = WorkflowDefinitionLibrary(tmp_path).get(created["id"])
    assert restored is not None
    assert restored["slug"] == "debug-project"
    assert restored["revision"] == 1
    assert restored["source"] == SOURCE_V1
    assert restored["derived_from"] == {"workflow_id": "wfd_parent", "revision": 3}


def test_create_records_python_as_the_default_source_format(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)

    created = library.create(source=SOURCE_V1, name="Debug Project")

    assert created["schema_version"] == 2
    assert created["format"] == "python"


def test_version_one_definition_without_format_loads_as_python(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    library.library_dir.mkdir(parents=True)
    path = library.library_dir / "wfd_legacy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "wfd_legacy",
                "slug": "legacy",
                "name": "Legacy",
                "revision": 1,
                "source": SOURCE_V1,
                "revisions": [{"revision": 1, "source": SOURCE_V1}],
            }
        ),
        encoding="utf-8",
    )

    restored = library.get("wfd_legacy")

    assert restored is not None
    assert restored["format"] == "python"


def test_task_plan_format_survives_create_and_revision_update(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    source_v1 = "agents:\n  inspect:\n    prompt: inspect the repository\n"
    source_v2 = source_v1.replace("inspect the repository", "inspect the failing tests")

    created = library.create(source=source_v1, name="Inspect", source_format="task-plan")
    updated = library.update(created["id"], source=source_v2, expected_revision=1)

    assert created["format"] == "task-plan"
    assert updated is not None
    assert updated["format"] == "task-plan"


def test_search_can_be_limited_to_one_source_format(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    python_definition = library.create(source=SOURCE_V1, name="Debug Python")
    task_definition = library.create(
        source="agents:\n  debug:\n    prompt: debug the project\n",
        name="Debug Task Plan",
        source_format="task-plan",
    )

    python_matches = library.search("debug project", source_format="python")
    task_matches = library.search("debug project", source_format="task-plan")

    assert [item["id"] for item in python_matches] == [python_definition["id"]]
    assert [item["id"] for item in task_matches] == [task_definition["id"]]


def test_identical_source_creates_a_separate_definition_and_lineage(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)

    first = library.create(source=SOURCE_V1, name="Debug Project")
    second = library.create(
        source=SOURCE_V1,
        name="Another Name",
        derived_from={"workflow_id": first["id"], "revision": first["revision"]},
    )

    assert second["id"] != first["id"]
    assert second["content_hash"] == first["content_hash"]
    assert second["derived_from"] == {"workflow_id": first["id"], "revision": 1}
    assert len(library.list()) == 2


def test_update_keeps_immutable_revision_history(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")

    updated = library.update(created["id"], source=SOURCE_V2, expected_revision=1)

    assert updated is not None
    assert updated["revision"] == 2
    assert updated["source"] == SOURCE_V2
    assert updated["revisions"] == [
        {"revision": 1, "source": SOURCE_V1, "created_at": created["created_at"]},
        {"revision": 2, "source": SOURCE_V2, "created_at": updated["updated_at"]},
    ]


def test_update_rejects_a_stale_expected_revision(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    library.update(created["id"], source=SOURCE_V2, expected_revision=1)

    assert library.update(created["id"], source=SOURCE_V1, expected_revision=1) is None


def test_update_keeps_current_slug_when_submitted_slug_is_blank(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")

    updated = library.update(
        created["id"],
        source=SOURCE_V2,
        expected_revision=1,
        slug="   ",
    )

    assert updated is not None
    assert updated["slug"] == created["slug"]


def test_search_prefers_a_debugging_definition_for_a_debugging_intent(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    debug = library.create(
        source=SOURCE_V1,
        name="Debug Project",
        description="Investigate failures and explain the root cause",
    )
    library.create(
        source=SOURCE_V1.replace("debug-project", "release-notes"),
        name="Release Notes",
        description="Summarize user-visible changes for a release",
    )

    matches = library.search("debugging a failing login flow")

    assert matches
    assert matches[0]["id"] == debug["id"]


def test_get_accepts_the_unique_slug(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")

    assert library.get("debug-project")["id"] == created["id"]


def test_create_rejects_source_that_persistence_would_redact(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    sensitive_source = SOURCE_V1.replace("debug the project", "debug using AKIAIOSFODNN7EXAMPLE")

    with pytest.raises(ValueError, match="sensitive data"):
        library.create(source=sensitive_source, name="Sensitive")

    assert library.list() == []


def test_create_redacts_metadata_before_deriving_the_slug(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    credential = "AKIAIOSFODNN7EXAMPLE"

    created = library.create(
        source=SOURCE_V1,
        name=credential,
        description=f"inspect {credential}",
        slug=credential,
    )

    persisted = (library.library_dir / f"{created['id']}.json").read_text(encoding="utf-8")
    assert credential.lower() not in persisted.lower()
    assert created["slug"] == "redacted-credential"


def test_collision_suffix_stays_inside_the_slug_limit(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    long_name = "a" * 64

    first = library.create(source=SOURCE_V1, name=long_name)
    second = library.create(source=SOURCE_V2, name=long_name)

    assert len(first["slug"]) == 64
    assert len(second["slug"]) == 64
    assert second["slug"].endswith("-2")
