"""WorkflowService: append a finished run's source to a saved workflow.

``update_run_definition`` appends a run's exact source as a saved definition's next
revision, and ``preview_run_definition_update`` names the definition an update
would revise. These live apart from ``test_workflows_service.py``, which is at its
line cap, and reuse its fakes and wait helpers.
"""

from __future__ import annotations

import pytest
from test_workflows_service import GOOD_SCRIPT, FakeSessions, _wait_terminal

from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.update_from_run import source_sha256

pytestmark = pytest.mark.asyncio

EDITED_SCRIPT = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('edited')")
EDITED_SHA = source_sha256(EDITED_SCRIPT)


async def _finished_run(svc: WorkflowService, source: str, name: str = "Ad hoc") -> str:
    started = await svc.start(source, name=name)
    await _wait_terminal(svc, started["run_id"])
    return started["run_id"]


async def test_update_run_definition_appends_the_runs_raw_source(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=GOOD_SCRIPT, name="Demo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    run_id = await _finished_run(svc, EDITED_SCRIPT)

    updated = await svc.update_run_definition(
        run_id, saved["slug"], expected_revision=1, expected_source_sha256=EDITED_SHA
    )

    assert updated["ok"] is True
    stored = library.get(saved["id"])
    assert stored["revision"] == 2
    assert stored["source"] == EDITED_SCRIPT
    assert [r["source"] for r in stored["revisions"]] == [GOOD_SCRIPT, EDITED_SCRIPT]


async def test_update_run_definition_refuses_without_writing(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=GOOD_SCRIPT, name="Demo")
    plan = library.create(
        source="agents:\n  a:\n    prompt: p\n", name="Plan", source_format="task-plan"
    )
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    edited = await _finished_run(svc, EDITED_SCRIPT)
    same = await _finished_run(svc, GOOD_SCRIPT)

    sha = {edited: EDITED_SHA, same: source_sha256(GOOD_SCRIPT), "wf_missing": EDITED_SHA}

    async def update(run_id: str, ref: str, revision: int) -> dict:
        return await svc.update_run_definition(
            run_id, ref, expected_revision=revision, expected_source_sha256=sha[run_id]
        )

    stale = await update(edited, saved["id"], 0)
    unchanged = await update(same, saved["id"], 1)
    mismatch = await update(edited, plan["id"], 1)
    no_definition = await update(edited, "missing", 1)
    no_run = await update("wf_missing", saved["id"], 1)

    assert stale["conflict"] is True
    assert unchanged["unchanged"] is True
    assert mismatch["format_mismatch"] is True
    assert no_definition["definition_not_found"] is True
    assert no_run["not_found"] is True
    assert library.get(saved["id"])["revision"] == 1
    assert library.get(plan["id"])["revision"] == 1


async def test_update_run_definition_refuses_a_saved_invocation(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=GOOD_SCRIPT, name="Demo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    started = await svc.start_definition(saved["slug"])
    await _wait_terminal(svc, started["run_id"])

    refused = await svc.update_run_definition(
        started["run_id"], saved["id"], expected_revision=1, expected_source_sha256=EDITED_SHA
    )
    previewed = await svc.preview_run_definition_update(started["run_id"])

    assert refused["already_saved"] is True
    assert previewed["already_saved"] is True
    assert library.get(saved["id"])["revision"] == 1


async def test_preview_suggests_the_workflow_named_by_the_script(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    by_meta = library.create(source=GOOD_SCRIPT, name="Demo")  # GOOD_SCRIPT's META name
    parent = library.create(source=GOOD_SCRIPT, name="Parent")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    plain = await _finished_run(svc, EDITED_SCRIPT, name="Demo - test run")
    adapted = await _finished_run(
        svc,
        EDITED_SCRIPT.replace(
            '"description": "d"', f'"description": "d", "adapted_from": "{parent["id"]}@1"'
        ),
    )
    unnamed = await _finished_run(svc, EDITED_SCRIPT.replace('"demo"', '"unsaved"'), name="x")

    by_name = await svc.preview_run_definition_update(plain)
    by_lineage = await svc.preview_run_definition_update(adapted)
    explicit = await svc.preview_run_definition_update(plain, parent["slug"])
    none = await svc.preview_run_definition_update(unnamed)

    assert by_name["target"] == {
        key: by_meta[key] for key in ("id", "slug", "name", "revision", "format")
    }
    assert by_name["target_source"] == GOOD_SCRIPT
    assert by_name["run_source"] == EDITED_SCRIPT
    assert by_lineage["target"]["id"] == parent["id"]
    assert explicit["target"]["id"] == parent["id"]
    assert none["ok"] is True and none["target"] is None


async def test_preview_skips_a_lineage_whose_revision_is_not_an_ascii_number(tmp_path) -> None:
    # "²".isdigit() is True but int("²") raises, so the reference must be ignored.
    library = WorkflowDefinitionLibrary(tmp_path)
    by_meta = library.create(source=GOOD_SCRIPT, name="Demo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    run_id = await _finished_run(
        svc,
        EDITED_SCRIPT.replace(
            '"description": "d"', '"description": "d", "adapted_from": "wfd_parent@\u00b2"'
        ),
    )

    previewed = await svc.preview_run_definition_update(run_id)

    assert previewed["ok"] is True
    assert previewed["target"]["id"] == by_meta["id"]


async def test_preview_suggests_the_workflow_an_edited_rerun_came_from(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    # A decoy whose slug the edited script's META name ("demo") would select.
    decoy = library.create(source=GOOD_SCRIPT, name="Demo")
    saved = library.create(source=GOOD_SCRIPT, name="Saved", slug="kept-elsewhere")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    started = await svc.start_definition(saved["slug"])
    await _wait_terminal(svc, started["run_id"])
    edited = await svc.rerun_subtree(started["run_id"], source=EDITED_SCRIPT)
    await _wait_terminal(svc, edited["run_id"])
    again = await svc.rerun_subtree(edited["run_id"])
    await _wait_terminal(svc, again["run_id"])

    previewed = await svc.preview_run_definition_update(edited["run_id"])
    carried = await svc.preview_run_definition_update(again["run_id"])

    assert svc.status(edited["run_id"])["derived_from"] == {
        "workflow_id": saved["id"],
        "revision": 1,
    }
    assert previewed["target"]["id"] == saved["id"] != decoy["id"]
    assert carried["target"]["id"] == saved["id"]


async def test_preview_suggests_the_plan_an_adapted_run_came_from(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    plan = library.create(
        source="agents:\n  a:\n    prompt: p\n",
        name="Plan",
        slug="kept-elsewhere",
        source_format="task-plan",
    )
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    run_id = await svc.begin_host_run(
        name="Unrelated name",
        source="agents:\n  a:\n    prompt: replanned\n",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
        derived_from_workflow_id=plan["id"],
        derived_from_revision=1,
    )
    await svc.pause(run_id)

    previewed = await svc.preview_run_definition_update(run_id)
    updated = await svc.update_run_definition(
        run_id,
        plan["id"],
        expected_revision=1,
        expected_source_sha256=previewed["run_source_sha256"],
    )

    assert previewed["target"]["id"] == plan["id"]
    assert updated["ok"] is True
    assert library.get(plan["id"])["source"] == "agents:\n  a:\n    prompt: replanned\n"


async def test_update_run_definition_refuses_a_source_changed_since_the_preview(
    tmp_path,
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=GOOD_SCRIPT, name="Demo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    run_id = await _finished_run(svc, EDITED_SCRIPT)
    previewed = await svc.preview_run_definition_update(run_id, saved["id"])
    # A paused TaskRunner plan is replanned this way while the preview is open.
    await svc.set_source(run_id, EDITED_SCRIPT.replace("edited", "replanned"))

    refused = await svc.update_run_definition(
        run_id,
        saved["id"],
        expected_revision=1,
        expected_source_sha256=previewed["run_source_sha256"],
    )

    assert refused["source_changed"] is True
    assert library.get(saved["id"])["revision"] == 1
