"""Removing a saved workflow: a tombstone that hides it and keeps its lineage.

A removed definition must leave every user-facing read (list,
search, slug or id lookup, saved runs, authoring examples) while staying a
valid ``derived_from`` parent, so drafts and runs adapted from it still save.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

SOURCE_V1 = (
    'META = {"name": "debug-project", "description": "Investigate a project failure"}\n'
    "async def workflow(ctx):\n"
    "    ctx.log('hi')\n"
    "    return 1\n"
)
SOURCE_V2 = SOURCE_V1.replace("ctx.log('hi')", "ctx.log('revision two')")


class _NoSessions:
    """The service under test never opens a session on these paths."""


def _service(library: WorkflowDefinitionLibrary) -> WorkflowService:
    return WorkflowService(sessions=_NoSessions(), persist=False, definition_library=library)


def _adapted_from(parent_id: str, revision: int = 1) -> str:
    return SOURCE_V1.replace(
        '"description": "Investigate a project failure"',
        f'"description": "Adapted", "adapted_from": "{parent_id}@{revision}"',
    )


# ── library ──


async def test_remove_writes_a_tombstone_that_keeps_every_revision(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    library.update(created["id"], source=SOURCE_V2, expected_revision=1)

    removed = library.remove("debug-project")

    assert removed is not None
    path = next(library.library_dir.glob("*.json"))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["id"] == created["id"]
    assert on_disk["removed_at"]
    assert "removed_by" not in on_disk
    assert [item["revision"] for item in on_disk["revisions"]] == [1, 2]
    assert on_disk["source"] == SOURCE_V2


async def test_removed_definition_is_absent_from_every_user_facing_read(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    kept = library.create(source=SOURCE_V1, name="Keep Project")
    gone = library.create(source=SOURCE_V1, name="Debug Project")

    library.remove(gone["id"])

    assert library.get(gone["id"]) is None
    assert library.get("debug-project") is None
    assert [item["id"] for item in library.list()] == [kept["id"]]
    assert [item["id"] for item in library.search("debug project", limit=10)] == [kept["id"]]
    assert {item["id"] for item in library.lineage_candidates()} == {kept["id"], gone["id"]}


async def test_remove_of_an_unknown_or_removed_workflow_returns_none(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")

    assert library.remove("wfd_missing") is None
    assert library.remove(created["id"]) is not None
    assert library.remove(created["id"]) is None


async def test_a_record_saved_under_older_redaction_rules_is_still_removable(tmp_path) -> None:
    """The tombstone adds ``removed_at`` only; it never re-gates stored revisions.

    A record whose old source today's redactors would change (saved before a
    rule landed, or placed by hand) is exactly the stale workflow a user wants
    gone, so removal must not refuse it.
    """
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    path = next(library.library_dir.glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    leaked = SOURCE_V1 + "TOKEN = 'ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'\n"
    record["source"] = leaked
    record["revisions"][-1]["source"] = leaked
    path.write_text(json.dumps(record), encoding="utf-8")

    removed = library.remove(created["id"])

    assert removed is not None
    assert library.get(created["id"]) is None
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["removed_at"]
    assert on_disk["revisions"] == record["revisions"]


async def test_a_record_stored_under_another_file_name_is_really_removed(tmp_path) -> None:
    """The tombstone lands on the file the record was read from.

    A hand-placed or copied record need not sit at the library's own file
    name. Writing the tombstone beside it would leave the original live and
    runnable while the removal reported success.
    """
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    own_path = next(library.library_dir.glob("*.json"))
    placed = library.library_dir / "other-name.json"
    own_path.rename(placed)
    svc = _service(library)

    assert svc.remove_definition("debug-project")["ok"] is True

    assert sorted(path.name for path in library.library_dir.glob("*.json")) == ["other-name.json"]
    assert json.loads(placed.read_text(encoding="utf-8"))["removed_at"]
    assert library.get(created["id"]) is None
    assert library.list() == []
    assert svc.get_definition("debug-project") is None
    started = await svc.start_definition("debug-project")
    assert started.get("not_found") is True


async def test_every_live_copy_of_one_id_is_tombstoned(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    own_path = next(library.library_dir.glob("*.json"))
    copy_path = library.library_dir / "copy-of-debug.json"
    copy_path.write_text(own_path.read_text(encoding="utf-8"), encoding="utf-8")

    assert library.remove(created["id"]) is not None

    for path in (own_path, copy_path):
        assert json.loads(path.read_text(encoding="utf-8"))["removed_at"], path.name
    assert library.get(created["id"]) is None
    assert library.remove(created["id"]) is None


async def test_a_removed_workflow_keeps_its_slug_reserved(tmp_path) -> None:
    """A ``/workflow <slug>`` saved elsewhere must never reach a different workflow."""
    library = WorkflowDefinitionLibrary(tmp_path)
    first = library.create(source=SOURCE_V1, name="Debug Project")
    library.remove(first["id"])

    second = library.create(source=SOURCE_V1, name="Debug Project")
    renamed = library.create(source=SOURCE_V1, name="Other")
    revised = library.update(
        renamed["id"], source=SOURCE_V2, expected_revision=1, slug="debug-project"
    )

    assert second["slug"] == "debug-project-2"
    assert revised is not None and revised["slug"] != "debug-project"
    assert library.get("debug-project") is None


async def test_a_removed_workflow_cannot_take_a_new_revision(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    library.remove(created["id"])

    assert library.update(created["id"], source=SOURCE_V2, expected_revision=1) is None
    record = library.lineage_candidates()[0]
    assert record["removed_at"]
    assert record["revision"] == 1


async def test_a_removal_waits_for_an_in_flight_revision_and_keeps_it(tmp_path) -> None:
    """Two processes: an update's read-check-write and a removal never interleave.

    Without the shared lock the removal reads revision 1, the update then writes
    a live revision 2 over the tombstone, and the removed workflow comes back.
    The removal's own lock attempt is observed directly: a non-blocking probe of
    the same lock file at that moment must find it held by the update.
    """
    writer = WorkflowDefinitionLibrary(tmp_path)
    remover = WorkflowDefinitionLibrary(tmp_path)
    created = writer.create(source=SOURCE_V1, name="Debug Project")
    in_write = threading.Event()
    release = threading.Event()
    attempting = threading.Event()
    contended: list[bool] = []
    errors: list[BaseException] = []
    original_write = writer._write
    original_lock = remover._revisions_lock

    def held_write(definition):
        in_write.set()
        assert release.wait(5)
        original_write(definition)

    @contextmanager
    def observed_lock() -> Iterator[None]:
        probe = os.open(remover.library_dir / ".revisions.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.file_lock(probe, exclusive=True, wait=False):
                contended.append(False)
        except BlockingIOError:
            contended.append(True)
        finally:
            os.close(probe)
        attempting.set()
        with original_lock():
            yield

    writer._write = held_write  # type: ignore[method-assign]
    remover._revisions_lock = observed_lock  # type: ignore[method-assign]

    def update() -> None:
        try:
            writer.update(created["id"], source=SOURCE_V2, expected_revision=1)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    def remove() -> None:
        try:
            remover.remove(created["id"])
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    updating = threading.Thread(target=update)
    removing = threading.Thread(target=remove)
    updating.start()
    try:
        assert in_write.wait(5), "the update never reached its locked write"
        removing.start()
        assert attempting.wait(5), "the removal never asked for the library lock"
        assert contended == [True], "the removal's lock attempt found the lock free"
    finally:
        release.set()
        updating.join(5)
        if removing.ident is not None:
            removing.join(5)

    assert not errors
    assert writer.get(created["id"]) is None
    record = writer.lineage_candidates()[0]
    assert record["removed_at"]
    assert record["revision"] == 2


# ── service: lineage, runs and authoring ──


async def test_a_draft_adapted_from_a_removed_workflow_still_saves(tmp_path) -> None:
    """The issue's failure: moving the file broke 'workflow lineage does not resolve'."""
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=SOURCE_V1, name="Debug Project")
    svc = _service(library)
    assert svc.remove_definition(parent["slug"])["ok"] is True

    saved = svc.save_definition(
        SOURCE_V1,
        name="Adapted",
        derived_from={"workflow_id": parent["id"], "revision": 1},
    )

    assert saved["ok"] is True, saved
    assert saved["definition"]["derived_from"] == {"workflow_id": parent["id"], "revision": 1}


async def test_a_run_adapted_from_a_removed_workflow_still_promotes(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=SOURCE_V1, name="Debug Project")
    svc = _service(library)
    started = await svc.start(_adapted_from(parent["id"]), name="Adapted")
    handle = svc.registry.get(started["run_id"])
    assert handle is not None
    if handle.task is not None:
        await handle.task
    svc.remove_definition(parent["id"])

    promoted = await svc.promote_run_definition(started["run_id"], name="Saved adaptation")

    assert promoted["ok"] is True, promoted
    assert promoted["definition"]["derived_from"] == {"workflow_id": parent["id"], "revision": 1}


async def test_removed_workflow_no_longer_lists_runs_or_seeds_authoring(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=SOURCE_V1, name="Debug Project")
    svc = _service(library)

    svc.remove_definition(parent["id"])

    assert svc.list_definitions() == []
    assert svc.list_definitions("debug project") == []
    assert svc.get_definition("debug-project") is None
    assert svc._search_definitions("debug project") == []
    started = await svc.start_definition("debug-project")
    assert started.get("not_found") is True


async def test_remove_definition_reports_not_found_for_an_unknown_ref(tmp_path) -> None:
    svc = _service(WorkflowDefinitionLibrary(tmp_path))

    out = svc.remove_definition("wfd_missing")

    assert out == {"ok": False, "error": "no such saved workflow", "not_found": True}
