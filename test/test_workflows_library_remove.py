"""Removing a saved workflow: a tombstone that hides it and keeps its lineage.

A removed definition must leave every user-facing read (list,
search, slug or id lookup, saved runs, authoring examples) while staying a
valid ``derived_from`` parent, so drafts and runs adapted from it still save.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core, platform_compat
from kiro_crew.dashboard.chat_runner import _handle_workflow_command
from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_definition_get,
    api_workflow_definition_remove,
    api_workflow_definition_run,
    api_workflow_definition_update,
    api_workflow_definitions,
)
from kiro_crew.testing.wait import LoopHop
from kiro_crew.workflows.library import WorkflowDefinitionLibrary, command_ref
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


def _hand_written(library: WorkflowDefinitionLibrary, workflow_id: str, slug: str) -> None:
    """Place a live record in the folder by hand.

    Hand placement (or restoring a copy of a removed record) is the only way a
    live record carries a ref a removed one holds: allocation never hands one out.
    """
    library.library_dir.mkdir(parents=True, exist_ok=True)
    stamp = "2026-10-20T00:00:00Z"
    record = {
        "schema_version": 2,
        "id": workflow_id,
        "slug": slug,
        "name": "Hand Written",
        "description": "Investigate a project failure",
        "format": "python",
        "created_at": stamp,
        "updated_at": stamp,
        "revision": 1,
        "source": SOURCE_V1,
        "content_hash": "hand-written",
        "derived_from": None,
        "revisions": [{"revision": 1, "source": SOURCE_V1, "created_at": stamp}],
    }
    path = library.library_dir / f"{workflow_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")


def _removed_slug_claimed_by_hand(tmp_path) -> WorkflowDefinitionLibrary:
    """``debug-project`` removed, then claimed by a hand-written ``wfd_hand``."""
    library = WorkflowDefinitionLibrary(tmp_path)
    gone = library.create(source=SOURCE_V1, name="Debug Project")
    assert library.remove(gone["id"]) is not None
    _hand_written(library, "wfd_hand", "debug-project")
    return library


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


@pytest.mark.parametrize("schema_version", [1, 2])
async def test_a_tombstone_is_the_stored_record_plus_removed_at_and_nothing_else(
    tmp_path, schema_version: int
) -> None:
    """Removal skips the save-time source gate and redaction, which is safe only
    while the tombstone rewrites the record as stored: it adds ``removed_at``, and
    to a version-1 record the default ``format`` every reader already assumes, and
    changes, drops or adds nothing else. Source today's redactors would change and
    a field the library does not know both survive byte for byte."""
    library = WorkflowDefinitionLibrary(tmp_path)
    library.library_dir.mkdir(parents=True)
    leaked = SOURCE_V1 + "TOKEN = 'ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'\n"
    stored = {
        "schema_version": schema_version,
        "id": "wfd_stored",
        "slug": "debug-project",
        "name": "Debug Project",
        "description": "Investigate a project failure",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-02T00:00:00+00:00",
        "revision": 2,
        "source": leaked,
        "content_hash": "stored-hash",
        "derived_from": {"workflow_id": "wfd_parent", "revision": 1},
        "revisions": [
            {"revision": 1, "source": SOURCE_V1},
            {"revision": 2, "source": leaked},
        ],
        "kept_as_stored": {"unknown": ["field"]},
    }
    if schema_version == 2:
        stored["format"] = "python"
    path = library.library_dir / "stored.json"
    path.write_text(json.dumps(stored), encoding="utf-8")

    removed = library.remove("wfd_stored")

    assert removed is not None
    assert removed["removed_at"]
    expected = {**stored, "removed_at": removed["removed_at"]}
    if schema_version == 1:
        expected["format"] = "python"
    assert json.loads(path.read_text(encoding="utf-8")) == expected


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


async def test_a_copy_left_live_by_a_failed_removal_answers_only_to_its_own_free_slug(
    tmp_path, monkeypatch
) -> None:
    """The first tombstone reserves the id and its copy's slug. A copy whose
    write failed keeps a slug of its own, so it answers to that slug alone and
    a retry by the id answers not found."""
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    own_path = next(library.library_dir.glob("*.json"))
    copy = json.loads(own_path.read_text(encoding="utf-8"))
    copy["slug"] = "deploy-two"
    copy_path = library.library_dir / "zz-copy-of-debug.json"
    copy_path.write_text(json.dumps(copy), encoding="utf-8")
    original_replace = library._replace
    written: list[Path] = []

    def replace_once(definition, path=None):
        if written:
            raise OSError("disk full")
        written.append(path)
        original_replace(definition, path)

    monkeypatch.setattr(library, "_replace", replace_once)
    with pytest.raises(OSError):
        library.remove(created["id"])
    monkeypatch.setattr(library, "_replace", original_replace)

    assert len(written) == 1
    tombstoned = {
        path.name
        for path in library.library_dir.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("removed_at")
    }
    assert tombstoned == {written[0].name}
    left = "deploy-two" if written[0] == own_path else "debug-project"
    gone = "debug-project" if left == "deploy-two" else "deploy-two"
    assert library.get(created["id"]) is None
    assert library.remove(created["id"]) is None
    assert library.get(gone) is None
    reached = library.get(left)
    assert reached is not None and reached["id"] == created["id"]
    assert reached["id_reserved"] is True and "slug_reserved" not in reached
    assert [command_ref(item) for item in library.list()] == [left]


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


async def test_a_removed_slug_stays_dead_when_a_hand_written_record_claims_it(tmp_path) -> None:
    """The slug is permanent, not only the id: a record placed by hand with
    the removed slug and another id answers only to its id, and the list
    offers it under its id."""
    library = _removed_slug_claimed_by_hand(tmp_path)

    assert library.get("debug-project") is None
    assert library.remove("debug-project") is None
    by_id = library.get("wfd_hand")
    assert by_id is not None and by_id["slug"] == "debug-project"
    assert by_id["slug_reserved"] is True
    listed = library.list()
    assert [(item["id"], item.get("slug_reserved")) for item in listed] == [("wfd_hand", True)]
    assert command_ref(listed[0]) == "wfd_hand"
    searched = library.search("debug project", limit=10)
    assert [(item["id"], item.get("slug_reserved")) for item in searched] == [("wfd_hand", True)]

    fresh = library.create(source=SOURCE_V1, name="Debug Project")

    assert fresh["slug"] == "debug-project-2"
    resolved = library.get("debug-project-2")
    assert resolved is not None and resolved["id"] == fresh["id"]
    assert "slug_reserved" not in resolved
    assert command_ref(resolved) == "debug-project-2"
    assert library.get("debug-project") is None


async def test_a_revision_on_a_removed_slug_never_stores_the_read_time_flag(tmp_path) -> None:
    """``slug_reserved`` is computed on read. An update builds on the stored
    record, so the flag never reaches disk, and a revision that sends the slug
    again, as the dashboard editor does, gets the first free numbered slug."""
    library = _removed_slug_claimed_by_hand(tmp_path)
    path = library.library_dir / "wfd_hand.json"

    kept = library.update("wfd_hand", source=SOURCE_V2, expected_revision=1)

    assert kept is not None and kept["slug"] == "debug-project" and kept["slug_reserved"] is True
    assert "slug_reserved" not in json.loads(path.read_text(encoding="utf-8"))

    renamed = library.update(
        "wfd_hand", source=SOURCE_V2, expected_revision=2, slug="debug-project"
    )

    assert renamed is not None and renamed["slug"] == "debug-project-2"
    assert "slug_reserved" not in renamed
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["revision"] == 3 and "slug_reserved" not in on_disk
    resolved = library.get("debug-project-2")
    assert resolved is not None and resolved["id"] == "wfd_hand"
    assert library.get("debug-project") is None


async def test_read_time_flags_stored_in_a_file_are_ignored(tmp_path) -> None:
    """The flags are computed on every read. A hand-edited file that stores
    them while its id and slug are free still lists, and runs, under its slug."""
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "wfd_hand", "debug-project")
    path = library.library_dir / "wfd_hand.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    stored = {**record, "slug_reserved": True, "id_reserved": True}
    path.write_text(json.dumps(stored), encoding="utf-8")

    resolved = library.get("debug-project")

    assert resolved is not None and resolved["id"] == "wfd_hand"
    assert "slug_reserved" not in resolved and "id_reserved" not in resolved
    listed = library.list()
    assert [(item.get("slug_reserved"), item.get("id_reserved")) for item in listed] == [
        (None, None)
    ]
    assert command_ref(listed[0]) == "debug-project"


async def test_removing_one_of_two_records_by_their_shared_slug_retires_it_for_both(
    tmp_path,
) -> None:
    """Two records placed by hand under one slug: removing by that slug
    removes one, and from then on the slug answers not found for the other
    too. The other stays live and is reached, and removed, by its id."""
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "wfd_one", "shared")
    _hand_written(library, "wfd_two", "shared")

    removed = library.remove("shared")

    assert removed is not None
    other = "wfd_two" if removed["id"] == "wfd_one" else "wfd_one"
    assert library.get("shared") is None
    assert library.remove("shared") is None
    survivor = library.get(other)
    assert survivor is not None and survivor["slug_reserved"] is True
    assert [item["id"] for item in library.list()] == [other]
    assert library.remove(other) is not None
    assert library.list() == []


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

    def held_write(definition, path=None):
        in_write.set()
        assert release.wait(5)
        original_write(definition, path)

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


async def test_lineage_resolves_when_a_later_copy_of_the_parent_holds_the_revision(
    tmp_path, monkeypatch
) -> None:
    """Two files can hold one id, a copied or restored record, and the library
    reads them in no fixed order, so lineage must not stop at the first copy."""
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=SOURCE_V1, name="Debug Project")
    newer = library.update(parent["id"], source=SOURCE_V2, expected_revision=1)
    assert newer is not None and newer["revision"] == 2
    older = {**newer, "revision": 1, "source": SOURCE_V1, "revisions": newer["revisions"][:1]}
    (library.library_dir / "restored-copy.json").write_text(json.dumps(older), encoding="utf-8")
    # The order the folder happens to list the files in is not fixed; pin the
    # one where the copy without revision 2 is read first.
    monkeypatch.setattr(library, "lineage_candidates", lambda: [older, newer])
    svc = _service(library)

    saved = svc.save_definition(
        SOURCE_V1,
        name="Adapted",
        derived_from={"workflow_id": parent["id"], "revision": 2},
    )

    assert saved["ok"] is True, saved
    assert saved["definition"]["derived_from"] == {"workflow_id": parent["id"], "revision": 2}


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


# ── every slug reader, on a removed slug a hand-written record claims ──
#
# The readers by ref: get by id or slug, ``/workflow <slug>``,
# ``POST /api/workflows/definitions/{ref}/run`` and ``workflow_run`` with
# ``workflow``. Each answers not found by the removed slug and still reaches
# the hand-written record by its id. Runs are recorded, not executed: what is
# under test is which definition a ref resolves to.


def _recording_service(library: WorkflowDefinitionLibrary) -> tuple[WorkflowService, list[dict]]:
    svc = _service(library)
    starts: list[dict] = []

    async def start(**kwargs):
        starts.append(kwargs)
        return {"run_id": "wf_started", "name": kwargs.get("name", "")}

    svc.start = start  # type: ignore[method-assign]
    return svc, starts


def _service_on_claimed_slug(tmp_path) -> tuple[WorkflowService, list[dict]]:
    return _recording_service(_removed_slug_claimed_by_hand(tmp_path))


def _owner_routes(svc: WorkflowService) -> web.Application:
    @web.middleware
    async def owner(request, handler):
        request["user"] = "test-user"
        request["app"] = ""
        request["is_dashboard_user"] = True
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(workflow_service=svc, owner_id="test-user")
    ref = "/api/workflows/definitions/{workflow_ref}"
    app.router.add_get("/api/workflows/definitions", api_workflow_definitions)
    app.router.add_get(ref, api_workflow_definition_get)
    app.router.add_patch(ref, api_workflow_definition_update)
    app.router.add_delete(ref, api_workflow_definition_remove)
    app.router.add_post(ref + "/run", api_workflow_definition_run)
    return app


def _mcp_through(monkeypatch, client: TestClient) -> None:
    """Send the MCP tools' loopback calls to ``client``'s real routes.

    The tools call ``mcp_core`` from a worker thread, so each call hops onto
    the test's loop and waits for the route's answer under the lost-run bound.
    """
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:main")
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:main")
    hop = LoopHop(asyncio.get_running_loop())

    def through(method: str):
        def call(path, body=None, **_kwargs):
            async def request():
                response = await client.request(method, path, json=body)
                return await response.json()

            return hop.run(request(), f"{method} {path}")

        return call

    monkeypatch.setattr(mcp_core, "_post", through("POST"))
    monkeypatch.setattr(mcp_core, "_get", through("GET"))


async def _tool(name: str, args: dict) -> str:
    return await asyncio.to_thread(mcp_core._call_tool, name, args)


async def test_get_definition_and_start_definition_on_a_claimed_removed_slug(tmp_path) -> None:
    svc, starts = _service_on_claimed_slug(tmp_path)

    assert svc.get_definition("debug-project") is None
    assert (await svc.start_definition("debug-project")).get("not_found") is True
    by_id = svc.get_definition("wfd_hand")
    started = await svc.start_definition("wfd_hand")

    assert by_id is not None and by_id["id"] == "wfd_hand"
    assert started["run_id"] == "wf_started"
    assert started["workflow_id"] == "wfd_hand" and started["slug_reserved"] is True
    assert [call["workflow_id"] for call in starts] == ["wfd_hand"]


async def test_definition_routes_on_a_claimed_removed_slug(tmp_path) -> None:
    svc, starts = _service_on_claimed_slug(tmp_path)

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        get_by_slug = await client.get("/api/workflows/definitions/debug-project")
        get_by_slug_body = await get_by_slug.json()
        run_by_slug = await client.post("/api/workflows/definitions/debug-project/run", json={})
        run_by_slug_body = await run_by_slug.json()
        get_by_id = await client.get("/api/workflows/definitions/wfd_hand")
        get_by_id_body = await get_by_id.json()
        run_by_id = await client.post("/api/workflows/definitions/wfd_hand/run", json={})
        run_by_id_body = await run_by_id.json()
        listed = await (await client.get("/api/workflows/definitions")).json()
        revision = {"source": SOURCE_V2, "expected_revision": 1}
        patch_by_slug = await client.patch(
            "/api/workflows/definitions/debug-project", json=revision
        )
        delete_by_slug = await client.delete("/api/workflows/definitions/debug-project")
        patch_by_id = await client.patch("/api/workflows/definitions/wfd_hand", json=revision)
        patch_by_id_body = await patch_by_id.json()
        delete_by_id = await client.delete("/api/workflows/definitions/wfd_hand")

    assert (get_by_slug.status, get_by_slug_body["code"]) == (404, "workflow_definition_not_found")
    assert (run_by_slug.status, run_by_slug_body["code"]) == (404, "workflow_definition_not_found")
    assert get_by_id.status == 200
    assert get_by_id_body["definition"]["id"] == "wfd_hand"
    assert get_by_id_body["definition"]["slug_reserved"] is True
    assert run_by_id.status == 200 and run_by_id_body["workflow_id"] == "wfd_hand"
    assert [call["workflow_id"] for call in starts] == ["wfd_hand"]
    assert [(item["id"], item.get("slug_reserved")) for item in listed["definitions"]] == [
        ("wfd_hand", True)
    ]
    assert (patch_by_slug.status, delete_by_slug.status) == (404, 404)
    assert patch_by_id.status == 200 and patch_by_id_body["definition"]["revision"] == 2
    assert delete_by_id.status == 200


class _Slot:
    key = "main"
    agent = "kirocrew"

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.credential_evidence = None

    def append(self, role, content, cls, *, meta=None):
        self.messages.append(content)


class _Sel:
    def log_tool_invocation(self, **kwargs):
        return None


async def test_the_workflow_command_on_a_claimed_removed_slug(tmp_path, monkeypatch) -> None:
    """``/workflow`` lists the record under its id, ``/workflow <slug>`` answers
    not found, and ``/workflow <id>`` starts it and names the id, not the slug."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    svc, starts = _service_on_claimed_slug(tmp_path)
    state = SimpleNamespace(workflow_service=svc, push_slots_update=lambda: None)
    slot = _Slot()

    await _handle_workflow_command(state, slot, "/workflow", "dashboard:main")
    await _handle_workflow_command(state, slot, "/workflow debug-project go", "dashboard:main")
    await _handle_workflow_command(state, slot, "/workflow wfd_hand go", "dashboard:main")

    listing, by_slug, by_id = slot.messages
    assert "`/workflow wfd_hand`" in listing and "/workflow debug-project" not in listing
    assert "no such saved workflow: debug-project" in by_slug
    assert "Started `/workflow wfd_hand` as `wf_started`" in by_id
    assert [call["workflow_id"] for call in starts] == ["wfd_hand"]


async def test_workflow_tools_on_a_claimed_removed_slug(tmp_path, monkeypatch) -> None:
    """``workflow_run`` with ``workflow`` and ``workflow_library_list``, through
    the real definition routes: the slug is refused, the id runs, and the list
    offers the id."""
    svc, starts = _service_on_claimed_slug(tmp_path)

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        _mcp_through(monkeypatch, client)
        by_slug = await _tool("workflow_run", {"workflow": "debug-project"})
        by_id = await _tool("workflow_run", {"workflow": "wfd_hand"})
        listing = await _tool("workflow_library_list", {})

    assert by_slug.startswith("workflow_run failed: no such saved workflow")
    assert "Started saved workflow `/workflow wfd_hand` as `wf_started`" in by_id
    assert [call["workflow_id"] for call in starts] == ["wfd_hand"]
    assert "- `/workflow wfd_hand`" in listing and "/workflow debug-project" not in listing


# ── a hand-written id equal to a removed slug ──
#
# The refusal is on the ref, not on how it matches: ``/workflow debug-project``
# written down before the removal must not reach a record placed later with
# ``id: debug-project`` either. That record answers to its own slug while that
# one is free, and to nothing once both are held.


def _id_claims_removed_slug(tmp_path) -> WorkflowDefinitionLibrary:
    """``debug-project`` removed, then ``id: debug-project`` placed by hand
    with the free slug ``something-else``."""
    library = WorkflowDefinitionLibrary(tmp_path)
    gone = library.create(source=SOURCE_V1, name="Debug Project")
    assert gone["slug"] == "debug-project"
    assert library.remove(gone["id"]) is not None
    _hand_written(library, "debug-project", "something-else")
    return library


async def test_a_hand_written_id_equal_to_a_removed_slug_answers_not_found_by_it(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    library = _id_claims_removed_slug(tmp_path)
    svc, starts = _recording_service(library)
    state = SimpleNamespace(workflow_service=svc, push_slots_update=lambda: None)
    slot = _Slot()

    assert library.get("debug-project") is None
    assert library.remove("debug-project") is None
    assert (await svc.start_definition("debug-project")).get("not_found") is True
    await _handle_workflow_command(state, slot, "/workflow debug-project go", "dashboard:main")
    by_slug = library.get("something-else")
    started = await svc.start_definition("something-else")
    await _handle_workflow_command(state, slot, "/workflow something-else go", "dashboard:main")
    await _handle_workflow_command(state, slot, "/workflow", "dashboard:main")

    refused, ran, listing = slot.messages
    assert "no such saved workflow: debug-project" in refused
    assert by_slug is not None and by_slug["id"] == "debug-project"
    assert by_slug["id_reserved"] is True and "slug_reserved" not in by_slug
    assert started["workflow_id"] == "debug-project"
    assert "Started `/workflow something-else`" in ran
    assert [call["workflow_id"] for call in starts] == ["debug-project", "debug-project"]
    assert "`/workflow something-else`" in listing and "/workflow debug-project" not in listing
    assert [command_ref(item) for item in library.list()] == ["something-else"]


async def test_routes_and_tools_refuse_a_hand_written_id_equal_to_a_removed_slug(
    tmp_path, monkeypatch
) -> None:
    svc, starts = _recording_service(_id_claims_removed_slug(tmp_path))

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        _mcp_through(monkeypatch, client)
        get_by_id = await client.get("/api/workflows/definitions/debug-project")
        run_by_id = await client.post("/api/workflows/definitions/debug-project/run", json={})
        tool_by_id = await _tool("workflow_run", {"workflow": "debug-project"})
        run_by_slug = await client.post("/api/workflows/definitions/something-else/run", json={})
        tool_by_slug = await _tool("workflow_run", {"workflow": "something-else"})
        listing = await _tool("workflow_library_list", {})

    assert (get_by_id.status, run_by_id.status) == (404, 404)
    assert tool_by_id.startswith("workflow_run failed: no such saved workflow")
    assert run_by_slug.status == 200
    assert "Started saved workflow `/workflow something-else`" in tool_by_slug
    assert [call["workflow_id"] for call in starts] == ["debug-project", "debug-project"]
    assert "- `/workflow something-else`" in listing and "/workflow debug-project" not in listing
    # Its id answers not found, so the agent list does not print it either.
    assert "id `debug-project`" not in listing


async def test_a_record_whose_id_is_reserved_is_changed_and_removed_by_its_slug(
    tmp_path,
) -> None:
    """The dashboard sends a write to the id, or to the slug when a removed
    workflow's slug holds the id: the reserved id answers 404 on every route."""
    svc, _ = _recording_service(_id_claims_removed_slug(tmp_path))
    revision = {"source": SOURCE_V2, "expected_revision": 1}

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        patch_by_id = await client.patch("/api/workflows/definitions/debug-project", json=revision)
        delete_by_id = await client.delete("/api/workflows/definitions/debug-project")
        get_by_slug = await client.get("/api/workflows/definitions/something-else")
        get_by_slug_body = await get_by_slug.json()
        patch_by_slug = await client.patch(
            "/api/workflows/definitions/something-else", json=revision
        )
        patch_by_slug_body = await patch_by_slug.json()
        delete_by_slug = await client.delete("/api/workflows/definitions/something-else")
        delete_by_slug_body = await delete_by_slug.json()

    assert (patch_by_id.status, delete_by_id.status) == (404, 404)
    assert get_by_slug.status == 200
    assert get_by_slug_body["definition"]["id"] == "debug-project"
    assert patch_by_slug.status == 200
    assert patch_by_slug_body["definition"]["revision"] == 2
    assert patch_by_slug_body["definition"]["slug"] == "something-else"
    assert patch_by_slug_body["definition"]["id_reserved"] is True
    assert delete_by_slug.status == 200 and delete_by_slug_body["id"] == "debug-project"
    assert svc.list_definitions() == []


async def test_a_record_whose_id_and_slug_are_both_reserved_is_listed_but_unreachable(
    tmp_path, monkeypatch
) -> None:
    """Nothing in the product reaches it. The library list keeps it, flagged
    on both refs, for the dashboard to mark; the chat ``/workflow`` list and
    ``workflow_library_list`` leave it out, since every command would answer
    not found. Its file must give it a new id and slug, or go."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    library = WorkflowDefinitionLibrary(tmp_path)
    for name in ("Debug Project", "Triage Tickets"):
        gone = library.create(source=SOURCE_V1, name=name)
        assert library.remove(gone["id"]) is not None
    _hand_written(library, "debug-project", "triage-tickets")
    svc, starts = _recording_service(library)
    state = SimpleNamespace(workflow_service=svc, push_slots_update=lambda: None)
    slot = _Slot()

    listed = library.list()
    await _handle_workflow_command(state, slot, "/workflow", "dashboard:main")
    async with TestClient(TestServer(_owner_routes(svc))) as client:
        _mcp_through(monkeypatch, client)
        statuses = [
            (await client.get(f"/api/workflows/definitions/{ref}")).status
            for ref in ("debug-project", "triage-tickets")
        ]
        tool_listing = await _tool("workflow_library_list", {})

    assert [
        (item["id"], item.get("slug_reserved"), item.get("id_reserved")) for item in listed
    ] == [("debug-project", True, True)]
    assert command_ref(listed[0]) == ""
    for ref in ("debug-project", "triage-tickets"):
        assert library.get(ref) is None
        assert library.remove(ref) is None
        assert (await svc.start_definition(ref)).get("not_found") is True
        assert svc.update_definition(ref, source=SOURCE_V2, expected_revision=1)["not_found"]
    assert statuses == [404, 404]
    assert starts == []
    (listing,) = slot.messages
    assert "No saved workflows yet" in listing
    assert tool_listing == "No saved workflows yet."
    assert library.create(source=SOURCE_V1, name="Debug Project")["slug"] == "debug-project-2"


async def test_lineage_still_resolves_parents_whose_id_is_a_reserved_slug(tmp_path) -> None:
    """Lineage names a parent by id and revision and never goes through the
    ref refusal: a live parent whose id is reserved, and a removed parent whose
    own id equals the slug it reserved, both still prove a draft's origin."""
    library = _id_claims_removed_slug(tmp_path)
    _hand_written(library, "triage-tickets", "triage-tickets")
    assert library.remove("triage-tickets") is not None
    assert library.get("triage-tickets") is None
    svc = _service(library)

    for parent in ("debug-project", "triage-tickets"):
        saved = svc.save_definition(
            SOURCE_V1, name="Adapted", derived_from={"workflow_id": parent, "revision": 1}
        )
        assert saved["ok"] is True, saved
        assert saved["definition"]["derived_from"] == {"workflow_id": parent, "revision": 1}
    unknown = svc.save_definition(
        SOURCE_V1, name="Adapted", derived_from={"workflow_id": "wfd_nope", "revision": 1}
    )
    assert unknown["ok"] is False


# ── a removed record's id and slug stay reserved, matched as an id or a slug ──
#
# The reserved set is the id and the slug of every removed record, and slug
# allocation counts every id and slug on disk, live or removed, as taken.


async def test_a_restored_copy_of_a_removed_record_is_reached_by_no_ref(tmp_path) -> None:
    """Both of its refs named the removed record, so both answer not found.
    The library list keeps it, flagged on both refs."""
    library = WorkflowDefinitionLibrary(tmp_path)
    gone = library.create(source=SOURCE_V1, name="Debug Project")
    assert library.remove(gone["id"]) is not None
    tomb = next(library.library_dir.glob("*.json"))
    restored = json.loads(tomb.read_text(encoding="utf-8"))
    restored.pop("removed_at")
    (library.library_dir / "restored-copy.json").write_text(json.dumps(restored), encoding="utf-8")

    assert library.get(gone["id"]) is None
    assert library.get("debug-project") is None
    assert library.remove(gone["id"]) is None
    listed = library.list()
    assert [
        (item["id"], item.get("slug_reserved"), item.get("id_reserved")) for item in listed
    ] == [(gone["id"], True, True)]
    assert command_ref(listed[0]) == ""


async def test_a_patch_through_a_restored_copys_free_slug_leaves_its_tombstone_intact(
    tmp_path,
) -> None:
    """A tombstone occupies ``<id>.json`` while a copy restored with the same id
    and a free slug sits in another file. A PATCH through that slug revises the
    copy's own file, so the tombstone keeps the retired id and slug, the retired
    slug is still refused, and a create asking for the retired name gets the
    next number instead of taking it over."""
    library = WorkflowDefinitionLibrary(tmp_path)
    gone = library.create(source=SOURCE_V1, name="Debug Project")
    assert library.remove(gone["id"]) is not None
    tomb = library._path_for(gone["id"])
    restored = json.loads(tomb.read_text(encoding="utf-8"))
    restored.pop("removed_at")
    restored["slug"] = "deploy-two"
    copy_path = library.library_dir / "restored-copy.json"
    copy_path.write_text(json.dumps(restored), encoding="utf-8")
    svc = _service(library)
    revision = {"source": SOURCE_V2, "expected_revision": 1}

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        patched = await client.patch("/api/workflows/definitions/deploy-two", json=revision)
        patched_body = await patched.json()
        retired_get = await client.get("/api/workflows/definitions/debug-project")
        retired_patch = await client.patch(
            "/api/workflows/definitions/debug-project", json=revision
        )

    assert patched.status == 200, patched_body
    assert patched_body["definition"]["revision"] == 2
    kept = json.loads(tomb.read_text(encoding="utf-8"))
    assert kept["removed_at"] and kept["revision"] == 1 and kept["slug"] == "debug-project"
    assert json.loads(copy_path.read_text(encoding="utf-8"))["revision"] == 2
    assert (retired_get.status, retired_patch.status) == (404, 404)
    assert library.get(gone["id"]) is None
    created = svc.save_definition(SOURCE_V1, name="Debug Project")
    assert created["ok"] is True and created["definition"]["slug"] == "debug-project-2"
    assert (await svc.start_definition("debug-project")).get("not_found") is True


async def test_a_record_under_another_file_name_takes_its_revision_in_place(tmp_path) -> None:
    """A record placed by hand under any ``*.json`` name is revised in that
    file; no second file for its id appears."""
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "wfd_hand", "hand-slug")
    placed = library.library_dir / "my-own-name.json"
    library._path_for("wfd_hand").rename(placed)

    updated = library.update("wfd_hand", source=SOURCE_V2, expected_revision=1)

    assert updated is not None and updated["revision"] == 2
    assert sorted(path.name for path in library.library_dir.glob("*.json")) == ["my-own-name.json"]
    assert json.loads(placed.read_text(encoding="utf-8"))["revision"] == 2
    assert library.get("hand-slug")["revision"] == 2


async def test_a_new_slug_never_equals_an_id_on_disk(tmp_path) -> None:
    """A create or rename never takes a string a live record answers to as its
    id, so that id cannot be handed to a second workflow and, once its record
    is removed, ``debug-project`` answers not found."""
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "debug-project", "hand-slug")

    fresh = library.create(source=SOURCE_V1, name="Debug Project")
    other = library.create(source=SOURCE_V1, name="Other")
    renamed = library.update(
        other["id"], source=SOURCE_V2, expected_revision=1, slug="debug-project"
    )

    assert fresh["slug"] == "debug-project-2"
    assert renamed is not None and renamed["slug"] == "debug-project-3"
    hand = library.get("debug-project")
    assert hand is not None and hand["id"] == "debug-project"
    assert library.remove("hand-slug") is not None
    assert library.get("debug-project") is None
    assert library.get("hand-slug") is None


async def test_a_hand_written_slug_equal_to_a_removed_id_answers_not_found_by_it(
    tmp_path, monkeypatch
) -> None:
    """A removed record's id is reserved as a slug too. A new save never takes
    it, and a hand-written record whose slug is that id is reached only by its
    own free id, so a written-down ``/workflow debug-tool`` never runs it."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "debug-tool", "hand-tool")
    assert library.remove("debug-tool") is not None
    fresh = library.create(source=SOURCE_V1, name="Debug Tool")
    _hand_written(library, "wfd_hand", "debug-tool")
    svc, starts = _recording_service(library)
    state = SimpleNamespace(workflow_service=svc, push_slots_update=lambda: None)
    slot = _Slot()

    assert fresh["slug"] == "debug-tool-2"
    assert library.get("debug-tool") is None
    assert library.remove("debug-tool") is None
    assert (await svc.start_definition("debug-tool")).get("not_found") is True
    await _handle_workflow_command(state, slot, "/workflow debug-tool go", "dashboard:main")
    by_id = library.get("wfd_hand")
    await _handle_workflow_command(state, slot, "/workflow wfd_hand go", "dashboard:main")

    refused, ran = slot.messages
    assert "no such saved workflow: debug-tool" in refused
    assert by_id is not None and by_id["slug_reserved"] is True and "id_reserved" not in by_id
    assert command_ref(by_id) == "wfd_hand"
    assert "Started `/workflow wfd_hand`" in ran
    assert [call["workflow_id"] for call in starts] == ["wfd_hand"]


@pytest.mark.parametrize("ids_first", [True, False])
async def test_a_free_id_reaches_its_own_record_whatever_the_folder_order(
    tmp_path, ids_first: bool
) -> None:
    """One live record's id is another live record's slug, by hand. The id
    wins, in either order the folder lists the two files."""
    library = WorkflowDefinitionLibrary(tmp_path)
    _hand_written(library, "shared-ref", "alpha")
    _hand_written(library, "wfd_other", "shared-ref")
    records = library._load_all()
    by_id = next(item for item in records if item["id"] == "shared-ref")
    by_slug = next(item for item in records if item["id"] == "wfd_other")
    ordered = [by_id, by_slug] if ids_first else [by_slug, by_id]
    library._load_all = lambda: [dict(item) for item in ordered]  # type: ignore[method-assign]

    resolved = library.get("shared-ref")

    assert resolved is not None and resolved["id"] == "shared-ref"


async def test_an_unreadable_library_file_is_warned_about_once_until_it_changes(
    tmp_path, caplog
) -> None:
    """A removed record's refs stay reserved only while its tombstone reads,
    so a file no read can use is a warning, once per file and version."""
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=SOURCE_V1, name="Debug Project")
    broken = library.library_dir / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    no_id = library.library_dir / "no-id.json"
    no_id.write_text(json.dumps({"slug": "orphan"}), encoding="utf-8")

    def warnings_for(path) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING" and str(path) in record.getMessage()
        ]

    with caplog.at_level("WARNING", logger="kiro_crew.workflows.library"):
        for _ in range(3):
            assert [item["slug"] for item in library.list()] == ["debug-project"]
        assert len(warnings_for(broken)) == 1 and len(warnings_for(no_id)) == 1
        assert "id and slash-command name are free again" in warnings_for(broken)[0]

        broken.write_text("{still not json", encoding="utf-8")
        library.get("debug-project")
        library.get("debug-project")
        second = broken.stat()
        assert len(warnings_for(broken)) == 2

        # Fixed, then put back byte for byte with the same timestamp: it read
        # in between, so the break is news again.
        _hand_written(library, "broken", "fixed-flow")
        library.get("debug-project")
        broken.write_text("{still not json", encoding="utf-8")
        os.utime(broken, ns=(second.st_atime_ns, second.st_mtime_ns))
        library.get("debug-project")

    assert len(warnings_for(broken)) == 3 and len(warnings_for(no_id)) == 1


async def test_a_run_of_a_removed_saved_workflow_says_why_it_cannot_be_saved(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=SOURCE_V1, name="Debug Project")
    svc = _service(library)
    started = await svc.start_definition(saved["slug"])
    handle = svc.registry.get(started["run_id"])
    assert handle is not None
    if handle.task is not None:
        await handle.task

    while_saved = await svc.promote_run_definition(started["run_id"], name="Copy")
    assert svc.remove_definition(saved["id"])["ok"] is True
    after_removal = await svc.promote_run_definition(started["run_id"], name="Copy")

    assert while_saved["already_saved"] is True
    assert while_saved["error"] == "saved workflow invocations are already reusable"
    assert after_removal["already_saved"] is True
    assert after_removal["error"] == (
        "the saved workflow this run started from is no longer in the library"
    )


# ── a request by a slug that moved: the dashboard sends the id it showed ──


async def _rename_and_take_the_slug(client, library: WorkflowDefinitionLibrary) -> dict:
    """Another tab renames the id-reserved record off ``something-else`` (a
    rename frees the old slug at once) and saves a new workflow, which takes
    that slug. Returns the new workflow."""
    renamed = await client.patch(
        "/api/workflows/definitions/something-else",
        json={"source": SOURCE_V2, "expected_revision": 1, "slug": "something-third"},
    )
    assert renamed.status == 200
    newer = library.create(source=SOURCE_V1, name="Something Else")
    assert newer["slug"] == "something-else"
    return newer


async def test_a_remove_by_a_slug_that_moved_is_refused_by_its_expected_id(tmp_path) -> None:
    """The dashboard removes a record whose id is reserved by its free slug,
    sending the id it shows. Once the slug names another workflow the DELETE
    answers 409 and nothing is removed; by its new slug, with the same id, the
    record is removed."""
    library = _id_claims_removed_slug(tmp_path)
    svc, _ = _recording_service(library)

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        newer = await _rename_and_take_the_slug(client, library)
        refused = await client.delete(
            "/api/workflows/definitions/something-else?expected_id=debug-project"
        )
        refused_body = await refused.json()
        removed = await client.delete(
            "/api/workflows/definitions/something-third?expected_id=debug-project"
        )
        removed_body = await removed.json()

    assert (refused.status, refused_body["code"]) == (409, "workflow_definition_conflict")
    still = library.get(newer["id"])
    assert still is not None and still["slug"] == "something-else"
    assert removed.status == 200 and removed_body["id"] == "debug-project"
    assert library.get("something-third") is None


async def test_a_save_by_a_slug_that_moved_is_refused_by_its_expected_id(tmp_path) -> None:
    """A stale editor's revision would match the new workflow's revision 1:
    with the id it shows, the PATCH answers 409 and the new workflow keeps its
    source. A malformed ``expected_id`` is a 400 on both routes."""
    library = _id_claims_removed_slug(tmp_path)
    svc, _ = _recording_service(library)

    async with TestClient(TestServer(_owner_routes(svc))) as client:
        newer = await _rename_and_take_the_slug(client, library)
        stale = await client.patch(
            "/api/workflows/definitions/something-else",
            json={"source": SOURCE_V2, "expected_revision": 1, "expected_id": "debug-project"},
        )
        stale_body = await stale.json()
        bad_patch = await client.patch(
            "/api/workflows/definitions/something-third",
            json={"source": SOURCE_V2, "expected_revision": 2, "expected_id": 7},
        )
        bad_delete = await client.delete("/api/workflows/definitions/something-third?expected_id=")

    assert (stale.status, stale_body["code"]) == (409, "workflow_definition_conflict")
    untouched = library.get(newer["id"])
    assert untouched is not None
    assert (untouched["revision"], untouched["source"]) == (1, SOURCE_V1)
    assert (bad_patch.status, bad_delete.status) == (400, 400)
    assert library.get("something-third")["revision"] == 2


async def test_a_live_copy_is_tombstoned_in_its_own_file_beside_the_first_tombstone(
    tmp_path,
) -> None:
    """A copy with a slug of its own, left live beside the record's tombstone,
    is removed by that slug in its own file: the first tombstone stays, every
    ref stays reserved, and a create asking for the old name is numbered."""
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Debug Project")
    own_path = library._path_for(created["id"])
    copy = json.loads(own_path.read_text(encoding="utf-8"))
    copy["slug"] = "deploy-two"
    (library.library_dir / "zz-copy-of-debug.json").write_text(json.dumps(copy), encoding="utf-8")
    tomb = {
        **json.loads(own_path.read_text(encoding="utf-8")),
        "removed_at": "2026-10-24T00:00:00Z",
    }
    own_path.write_text(json.dumps(tomb), encoding="utf-8")

    assert library.remove("deploy-two") is not None

    assert json.loads(own_path.read_text(encoding="utf-8"))["removed_at"] == tomb["removed_at"]
    assert library.list() == []
    for ref in (created["id"], "debug-project", "deploy-two"):
        assert library.get(ref) is None
    assert library.create(source=SOURCE_V1, name="Debug Project")["slug"] == "debug-project-2"
