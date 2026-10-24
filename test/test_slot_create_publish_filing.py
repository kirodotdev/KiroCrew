"""App callers and folder filing around a pending ``POST /api/chat/slots``.

An app cannot tell a busy name from one it may not use, a create files into
its folder only after it published, and a refused or failed filing leaves
the folder and the slot as they were.
The shared setup is in ``slot_create_publish_helpers.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
import slot_create_publish_helpers
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from slot_create_publish_helpers import (
    WAIT,
    _config,
    _drain,
    _HeldRecord,
    _pending,
    _post,
    _slots_app,
    _use_config,
)

from kiro_crew.dashboard import chat_folders, chat_handlers
from kiro_crew.dashboard import slot_create_transaction as txn
from kiro_crew.execution_context import read_session_execution

#: The shared fixture, bound here so pytest finds it for this module's tests.
state = slot_create_publish_helpers.state


# -- an app on /v1/chat/completions meets a key under construction -------------


@pytest.mark.asyncio
async def test_an_openai_app_call_on_a_key_under_construction_answers_as_for_no_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app never waits on ``/v1/chat/completions``, and a key it does not own
    that a create is building answers exactly as a key with no slot does: the
    unscoped 403, never ``member_slot_reserved``. Its own key under construction
    gets the retryable 409 ``slot_under_construction``.

    Mutation pin: answer the "still being built" refusal with the generic 409
    and the first two keys read 409 ``member_slot_reserved``.
    """
    from kiro_crew.dashboard import openai_compat

    async def verified(_request: Any) -> None:
        return None

    monkeypatch.setattr(openai_compat, "reject_if_kiro_unverified", verified)
    _use_config(monkeypatch, _config())
    dashboard_create = _pending(state, "building")
    other_app_create = _pending(state, "theirs", app="app-b")
    own_create = _pending(state, "mine", app="app-a")
    app = web.Application()
    app["state"] = state
    app.router.add_post("/v1/chat/completions", openai_compat.api_completions)
    answers: dict[str, tuple[int, Any]] = {}
    try:
        async with TestClient(TestServer(as_owner(app))) as client:
            for key in ("building", "theirs", "nobody", "mine"):
                resp = await asyncio.wait_for(
                    client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "default",
                            "id": key,
                            "messages": [{"role": "user", "content": "hi"}],
                        },
                        headers={"X-Test-App": "app-a"},
                    ),
                    WAIT,
                )
                answers[key] = (resp.status, await resp.json())
    finally:
        for create in (dashboard_create, other_app_create, own_create):
            await create.settle()
    absent = answers["nobody"]
    assert absent[0] == 403, absent
    assert answers["building"] == absent and answers["theirs"] == absent, answers
    status, body = answers["mine"]
    assert status == 409 and body["code"] == "slot_under_construction", body
    assert body["error"]["code"] == "slot_under_construction"
    assert not ({"building", "theirs", "nobody", "mine"} & set(state._slots))


# -- an app learns nothing from the name wait ------------------------------------


async def _app_post(client: TestClient, name: str) -> tuple[int, dict[str, Any]]:
    resp = await asyncio.wait_for(
        client.post("/api/chat/slots", json={"name": name}, headers={"X-Test-App": "app-a"}),
        WAIT,
    )
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_app_cannot_tell_a_busy_name_from_one_it_may_not_use(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is judged before the name wait, and a wait that runs out is the same 404.

    ``theirs`` is another app's slot and ``free`` is nobody's. Another create
    holds both names. The app's create of ``theirs`` is refused at once, without
    waiting for the name; its create of ``free`` is refused at once too, with the
    same answer, because another principal's create holds it.

    Mutation pins: take the name wait before the ownership check, or drop the
    other-principal check, and a request outlasts WAIT.
    """
    _use_config(monkeypatch, _config())
    state.get_or_create_slot("theirs", app="app-b")
    releases = [
        await txn.acquire_slot_create_name(state, "theirs"),
        await txn.acquire_slot_create_name(state, "free"),
    ]
    try:
        async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
            # The real bound is far past WAIT: only an answer that never waits fits.
            monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 3 * WAIT)
            not_yours = await _app_post(client, "theirs")
            busy = await _app_post(client, "free")
    finally:
        for release in releases:
            assert release is not None
            release()
    assert not_yours == busy == (404, {"error": "not found", "code": "slot_not_found"})
    assert state._slots["theirs"]._app == "app-b"
    assert "free" not in state._slots


# -- a tag deleted while the create waits is not published ------------------------


@pytest.mark.asyncio
async def test_a_folder_tag_deleted_while_the_create_waits_is_not_published(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inherited tags are filtered again under ``tags_write_lock`` at the commit.

    The tag-delete sweep cannot see a slot nobody registered, so a tag deleted
    while the create waits in its selection write would otherwise be published
    on the new slot. The re-filter runs with the session-switch lock held, the
    order every holder of both takes (switch lock, then tags lock).

    Mutation pin: publish without the re-filter and the deleted id ships.
    """
    from kiro_crew.dashboard import chat_tags
    from kiro_crew.llm_helpers import slot_switch_session_lock

    _use_config(monkeypatch, _config(worker={}))
    state._tags.extend([{"id": "keep", "name": "Keep"}, {"id": "gone", "name": "Gone"}])
    state._tags_authoritative = True
    state._folders.append({"id": "f1", "name": "F", "tags": ["keep", "gone"]})
    switch_held_at_tags: list[bool] = []
    real_lock = chat_tags.tags_write_lock

    class _Watched:
        def __init__(self, lock: Any) -> None:
            self._lock = lock

        async def acquire(self) -> bool:
            switch_held_at_tags.append(slot_switch_session_lock("dashboard:demo").locked())
            return await self._lock.acquire()

        def release(self) -> None:
            self._lock.release()

        async def __aenter__(self) -> None:
            await self._lock.acquire()

        async def __aexit__(self, *exc: Any) -> None:
            self._lock.release()

    monkeypatch.setattr(chat_handlers, "tags_write_lock", lambda st: _Watched(real_lock(st)))
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            async with real_lock(state):
                state._tags[:] = [t for t in state._tags if t["id"] != "gone"]
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200, await resp.text()
        finally:
            held.release.set()
            await _drain(post)
    assert state._slots["demo"].tags == ["keep"]
    # The inheritance takes the tags lock alone; the commit takes it inside the switch lock.
    assert switch_held_at_tags == [False, True]


def test_no_holder_of_the_tags_lock_takes_the_session_switch_lock() -> None:
    """Lock order: switch lock, then ``tags_write_lock``, never the reverse.

    Every ``async with tags_write_lock(...)`` body in the package is scanned
    for a session-switch lock; the create's commit takes the tags lock inside
    the switch lock, so one such body would be a lock-order inversion.
    """
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    switch_names = {"slot_switch_session_lock", "_slot_switch_session_lock"}
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "tags_write_lock" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.AsyncWith):
                continue
            if not any("tags_write_lock" in ast.unparse(item.context_expr) for item in node.items):
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if any(name in body for name in switch_names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


# -- a create publishes unfiled, then files through the folder-move path ---------


def _agent_folder(state: Any, *, hidden: bool = False) -> dict[str, Any]:
    """An empty folder the agent session ``agent`` created, as the store holds it."""
    folder: dict[str, Any] = {
        "id": "f1",
        "name": "F",
        chat_folders.CREATED_BY_SESSION: "dashboard:agent",
    }
    if hidden:
        folder["hidden"] = True
    state._folders.append(folder)
    return folder


def _folder_is_untouched(folder: dict[str, Any]) -> bool:
    """Still the agent's hidden folder: no un-hide, no claim."""
    return (
        folder.get(chat_folders.CREATED_BY_SESSION) == "dashboard:agent"
        and folder.get("hidden") is True
    )


async def _agent_delete_if_empty(state: Any) -> int:
    """The ``chat_folder_delete`` call the agent session ``agent`` makes; its status."""
    from chat_test_helpers import _make_folder_app

    state.get_or_create_slot("agent")
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await asyncio.wait_for(
            client.delete(
                "/api/chat/folders/f1?if_empty=true",
                headers={
                    "X-Internal-Secret": "s3cret",
                    "X-Internal-Caller": "kirocrew-dashboard",
                    "X-Session-Key": "dashboard:agent",
                },
            ),
            WAIT,
        )
        return resp.status


@pytest.mark.asyncio
async def test_a_refused_create_leaves_the_folder_untouched(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that never publishes writes nothing to the folder, live or on disk.

    Mutation pin: un-hide or claim the folder before the awaits (``_unhide_folder``
    with ``claim_for_person`` at assignment) and the mark is gone.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()
    before = [dict(f) for f in state._folders]
    generation = state.folders_generation()
    _HeldRecord(monkeypatch, refuse=True).release.set()
    status, _body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 503
    assert "demo" not in state._slots
    assert state._folders == before
    assert state.folders_generation() == generation
    assert _folders_file(state).read_bytes() == on_disk
    # Still the agent's, so its empty-only delete goes through.
    assert await _agent_delete_if_empty(state) == 200


@pytest.mark.asyncio
async def test_a_person_claim_during_a_refused_create_still_stops_the_agent_delete(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person renames the folder while the create awaits; the create then refuses.

    The rename removed the agent's mark, and the refusal has nothing to put
    back, so the agent's empty-only delete is still refused.

    Mutation pin: claim at assignment and restore the mark on refusal (the
    rollback this replaced) and the agent deletes the person's folder.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state)
    held = _HeldRecord(monkeypatch, refuse=True)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            # The person's rename: an edit that claims the folder.
            await state.mutate_folders(
                lambda folders: (
                    bool(folders[0].pop(chat_folders.CREATED_BY_SESSION, None))
                    | bool(folders[0].update(name="Mine")),
                    None,
                )
            )
            held.release.set()
            assert (await asyncio.wait_for(post, WAIT)).status == 503
        finally:
            held.release.set()
            await _drain(post)
    folder = next(f for f in state._folders if f["id"] == "f1")
    assert chat_folders.CREATED_BY_SESSION not in folder and folder["name"] == "Mine"
    assert await _agent_delete_if_empty(state) == 403
    assert any(f["id"] == "f1" for f in state._folders)


@pytest.mark.asyncio
async def test_a_published_create_claims_and_unhides_its_folder_at_publish(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim and the un-hide land in the filing step after publication, not before.

    Mutation pin: drop the filing step (``file_once_published``) and the folder
    keeps the agent's mark.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            pending_folder = next(f for f in state._folders if f["id"] == "f1")
            assert pending_folder.get(chat_folders.CREATED_BY_SESSION) == "dashboard:agent"
            assert pending_folder.get("hidden") is True
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == "f1"
        finally:
            held.release.set()
            await _drain(post)
    folder = next(f for f in state._folders if f["id"] == "f1")
    assert chat_folders.CREATED_BY_SESSION not in folder
    assert not folder.get("hidden")
    assert state._slots["demo"].folder_id == "f1"


@pytest.mark.asyncio
async def test_a_folder_deleted_before_publish_leaves_the_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder deleted while the create builds: the move path refuses the filing.

    The create still answers success with its published slot, unfiled: the
    response's ``folder_id`` is empty.

    Mutation pin: return the move path's refusal instead of the slot and the
    create answers 400 for a slot it published.
    """
    _use_config(monkeypatch, _config(worker={}))
    state._folders.append({"id": "f1", "name": "F"})
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await state.mutate_folders(lambda folders: (bool(folders.clear()) or True, None))
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            body = await resp.json()
        finally:
            held.release.set()
            await _drain(post)
    assert body["key"] == "demo" and body["folder_id"] == ""
    assert body["folder_id"] == "" and "filing_error" not in body
    slot = state._slots["demo"]
    assert slot.folder_id == "" and "demo" not in state._slots_under_construction


@pytest.mark.asyncio
async def test_a_create_files_only_after_it_published_through_the_move_path(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filing is the ordinary folder-move path, entered once the slot is published.

    Mutation pin: file before ``create.publish()`` and the move path finds no
    slot under the key (``seen`` records False).
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_move = chat_folders.file_slot_into_folder
    seen: list[tuple[bool, str | None]] = []

    async def watched(
        request: Any, st: Any, name: str, target: str | None = None, **kwargs: Any
    ) -> Any:
        published = name in st._slots and name not in st._slots_under_construction
        seen.append((published, target))
        return await real_move(request, st, name, target, **kwargs)

    monkeypatch.setattr(chat_folders, "file_slot_into_folder", watched)
    status, body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 200 and body["folder_id"] == "f1"
    assert seen == [(True, "f1")]
    assert state._slots["demo"].folder_id == "f1"


@pytest.mark.asyncio
async def test_a_filing_the_move_path_refuses_leaves_the_published_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move path's own authorization refuses the filing: success, empty folder_id.

    The folder is not written, and the slot the create published stays.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    monkeypatch.setattr(chat_folders, "member_slot_write_refused", refused)
    status, body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert state._slots["demo"].folder_id == ""
    assert "demo" not in state._slots_under_construction
    assert _folders_file(state).read_bytes() == on_disk
    assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


def _replace_published(state: Any, key: str = "demo") -> Any:
    """Delete *key*'s slot and open the same key again: a different slot object.

    The replacement writes its own metadata line, as a send to it does, so the
    shared transcript has a line a stale save could overwrite.
    """
    original = state._slots.pop(key)
    replacement = state.get_or_create_slot(key)
    assert replacement is not original and state._slots[key] is replacement
    state.conversation_log.update_metadata(f"dashboard:{key}", {"folder_id": ""})
    return replacement


async def _create_filed_demo(state: Any) -> tuple[int, dict[str, Any]]:
    return await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})


@pytest.mark.asyncio
async def test_a_slot_replaced_before_the_filing_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete plus a same-key open during the birth save: the replacement stays unfiled.

    The create answers 200 for the slot it published, with an empty
    ``folder_id``. The replacement keeps its own (empty) folder, and the birth
    save does not write the original onto its transcript.

    Mutation pin: drop the identity check before the dispatch AND the
    ``expected_slot`` the move path re-checks, and the move path resolves the
    replacement by key and files it into ``f1``.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_save = chat_handlers.save_slot_off_loop
    seen: dict[str, Any] = {}

    async def save_then_replaced(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        if slot.key == "demo" and "replacement" not in seen:
            seen["replacement"] = _replace_published(st)
        seen["saved"] = await real_save(st, slot, *args, **kwargs)
        return seen["saved"]

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_then_replaced)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    replacement = seen["replacement"]
    assert state._slots["demo"] is replacement
    assert replacement.folder_id == ""
    # The birth save's object check refused the write onto the replacement.
    assert seen["saved"] is False
    assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


@pytest.mark.asyncio
async def test_a_slot_replaced_during_the_move_un_hide_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is reused while the move path awaits its folder un-hide.

    The move path re-checks the slot object after that await and refuses with
    its own ``session_gone`` before it persists anything; the create answers 200
    with an empty ``folder_id``.

    Mutation pin: drop the re-check after ``_unhide_folder`` and the move path
    reaches its save (``saves`` is not empty).
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_unhide = chat_folders._unhide_folder
    real_save = chat_folders.save_slot_off_loop
    seen: dict[str, Any] = {}
    saves: list[str] = []

    async def unhide_then_replaced(st: Any, *args: Any, **kwargs: Any) -> bool:
        ok = await real_unhide(st, *args, **kwargs)
        seen["original"] = st._slots["demo"]
        seen["replacement"] = _replace_published(st)
        return ok

    async def counted_save(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        saves.append(slot.key)
        return await real_save(st, slot, *args, **kwargs)

    monkeypatch.setattr(chat_folders, "_unhide_folder", unhide_then_replaced)
    monkeypatch.setattr(chat_folders, "save_slot_off_loop", counted_save)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert saves == []
    assert state._slots["demo"] is seen["replacement"]
    assert seen["replacement"].folder_id == ""
    assert seen["original"].folder_id == ""


@pytest.mark.asyncio
async def test_a_slot_replaced_during_the_move_save_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is reused while the move path's save awaits the history lock.

    The save re-checks the slot object at its locked commit and writes nothing,
    so the move path answers ``session_gone`` and the replacement stays unfiled.

    Mutation pin: drop ``expected_slot_name`` from the move path's save and it
    commits the original's folder onto the shared transcript and answers 200
    with ``folder_id == "f1"``.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_save = chat_folders.save_slot_off_loop
    seen: dict[str, Any] = {}

    async def replaced_then_save(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        seen["replacement"] = _replace_published(st)
        seen["saved"] = await real_save(st, slot, *args, **kwargs)
        return seen["saved"]

    monkeypatch.setattr(chat_folders, "save_slot_off_loop", replaced_then_save)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert seen["saved"] is False
    assert state._slots["demo"] is seen["replacement"]
    assert seen["replacement"].folder_id == ""
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id") == ""


def _fail_filing_step(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    """Make one step of the folder-move path raise, as a failed store write does."""

    async def fails(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("folder store write failed")

    monkeypatch.setattr(chat_folders, step, fails)


def _folder_seen_by_frames(state: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ``demo`` slot's folder as each slots frame carries it."""
    seen: list[str] = []
    original = state._do_slots_broadcast

    def broadcast(*args: Any, **kwargs: Any) -> Any:
        slot = state._slots.get("demo")
        if slot is not None:
            seen.append(slot.folder_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_do_slots_broadcast", broadcast)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["_unhide_folder", "save_slot_off_loop"])
async def test_a_folder_store_failure_during_the_filing_leaves_the_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """A step of the move path raises mid-filing: the published slot is unfiled everywhere.

    The create answers 200 with an empty ``folder_id``. The live slot
    shows no folder, so the coalesced frame does not show it filed; nothing on
    disk names the folder; and the slot is marked dirty so a flush that ran
    meanwhile reconverges.

    Mutation pin: drop the rollback around the move path's un-hide and save,
    and the live slot (and the frame) keep ``folder_id == "f1"`` with nothing
    durable behind it.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    _fail_filing_step(monkeypatch, step)
    frames = _folder_seen_by_frames(state, monkeypatch)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == ""
    assert body["folder_id"] == "" and "filing_error" not in body
    slot = state._slots["demo"]
    assert slot.folder_id == "" and slot._dirty is True
    assert frames and set(frames) == {""}
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id", "") == ""
    if step == "_unhide_folder":
        # The un-hide wrote nothing; a save failure follows an un-hide that the
        # move route has always kept (the folder is shown, the slot is not in it).
        assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["_unhide_folder", "save_slot_off_loop"])
async def test_a_folder_store_failure_on_the_move_route_leaves_the_slot_where_it_was(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """The PATCH move route shares the rollback: a raise leaves the slot unfiled, live and on disk.

    Mutation pin: drop the rollback and the route's 500 leaves the live slot
    filed into ``f1`` while nothing durable holds it.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    status, body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 200, body
    slot = state._slots["demo"]
    slot._dirty = False
    _fail_filing_step(monkeypatch, step)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        resp = await asyncio.wait_for(
            client.patch("/api/chat/slots/demo/folder", json={"folder_id": "f1"}), WAIT
        )
    assert resp.status == 500
    assert slot.folder_id == "" and slot._folder_changed is False and slot._dirty is True
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id", "") == ""


def test_every_python_caller_of_the_create_route_reads_the_folder_id() -> None:
    """A Python module that POSTs ``/api/chat/slots`` reads the answered ``folder_id``.

    A create whose filing is refused still answers 200, with an empty
    ``folder_id``, so a caller that asked for a folder learns of the refusal
    only by comparing it. Today that is ``ScriptContext.open_session``
    (``cron_script_context.py``) alone: the MCP ``session_create`` tool goes
    through ``/api/session-control/create``, which files inline and has no
    deferred filing. A new caller that never reads the field fails here.
    """
    import re
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    # ``self._post(...)`` / ``client.post(...)``, never the router's ``add_post``.
    poster = re.compile(r"""(?<!\w)_?post\(\s*["']/api/chat/slots["']""")
    callers = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if poster.search(path.read_text(encoding="utf-8"))
    }
    assert sorted(callers) == ["cron_script_context.py"]
    assert [name for name, text in callers.items() if '.get("folder_id")' not in text] == []


# -- a cancelled create writes nothing to the folder ------------------------------


class _CancellableCreate:
    """Serves the create route and keeps the task running each request's handler."""

    def __init__(self, state: Any) -> None:
        self.task: asyncio.Task[Any] | None = None
        self.started = asyncio.Event()

        async def create(request: web.Request) -> web.StreamResponse:
            self.task = asyncio.current_task()
            self.started.set()
            return await chat_handlers.api_chat_slot_create(request)

        self.app = web.Application()
        self.app["state"] = state
        self.app.router.add_post("/api/chat/slots", create)

    async def cancel_and_join(self, then: Callable[[], Any] = lambda: None) -> None:
        """Cancel the handler, run *then*, and wait for the handler to finish."""
        assert self.task is not None
        self.task.cancel()
        then()
        await asyncio.wait_for(asyncio.wait((self.task,)), WAIT)


def _folders_file(state: Any) -> Any:
    from kiro_crew.dashboard import state as state_module

    return state_module.config_dir() / state._FOLDERS_FILE


@pytest.mark.asyncio
async def test_a_create_cancelled_before_its_folder_write_leaves_the_folder_untouched(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create cancelled before it publishes writes nothing and gives the key back."""
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()
    before = [dict(f) for f in state._folders]
    generation = state.folders_generation()
    held = _HeldRecord(monkeypatch, refuse=False)
    route = _CancellableCreate(state)
    async with TestClient(TestServer(as_owner(route.app))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await route.cancel_and_join(then=held.release.set)
        finally:
            held.release.set()
            await _drain(post)
    assert "demo" not in state._slots and "demo" not in state._slots_under_construction
    assert "demo" not in txn._PENDING_CREATES.get(state, {})
    assert state._folders == before and state.folders_generation() == generation
    assert _folders_file(state).read_bytes() == on_disk
    assert await _agent_delete_if_empty(state) == 200


# -- the create's own lock waits are bounded --------------------------------------


@pytest.mark.asyncio
async def test_a_create_whose_switch_lock_never_frees_is_refused_in_bound(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held session-switch lock ends the create with the retryable 409, publishing nothing.

    Mutation pin: take the lock without the deadline and the request outlasts WAIT.
    """
    from kiro_crew.llm_helpers import slot_switch_session_lock

    _use_config(monkeypatch, _config(worker={}))
    # The create's lock budget only: the name wait keeps its own bound.
    monkeypatch.setattr(txn.CreateDeadline, "remaining", lambda _self: 0.05)
    lock = slot_switch_session_lock("dashboard:demo")
    await lock.acquire()
    try:
        status, body = await _post(state, {"name": "demo", "agent": "worker"})
    finally:
        lock.release()
    assert status == 409 and body["code"] == "slot_under_construction"
    assert "demo" not in state._slots and not state._slots_under_construction
    assert read_session_execution("dashboard:demo") is None


@pytest.mark.asyncio
async def test_an_opener_given_a_raw_name_waits_for_the_create_of_its_folded_key(
    state: Any,
) -> None:
    """``wait_for_pending_create`` folds the key, as the create holding it did.

    A create of ``demo:1`` holds ``demo_1``. An opener given the raw name, here
    ``acquire_worker_slot``, waits for that create instead of meeting its mark.

    Mutation pin: drop the fold in ``wait_for_pending_create`` and both the
    bare wait and the acquire return while the create is still pending.
    """
    from kiro_crew.apps.worker_slots import acquire_worker_slot

    create = _pending(state, "demo:1")
    assert create.slot.key == "demo_1"
    waiter = asyncio.create_task(txn.wait_for_pending_create(state, "demo:1"))
    acquire = asyncio.create_task(
        acquire_worker_slot(state, "demo-app", "demo:1", project="/work/repo")
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiter.done() and not acquire.done(), (
            acquire.exception() if acquire.done() else None
        )
        await create.settle()
        await asyncio.wait_for(waiter, WAIT)
        lease = await asyncio.wait_for(acquire, WAIT)
        assert lease.key == "demo_1"
        await lease.release()
    finally:
        await create.settle()
        await _drain(waiter, acquire)


@pytest.mark.asyncio
async def test_an_app_never_enters_the_wait_for_a_name_another_principal_holds(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard holds ``free``; an app's create of it is answered without waiting.

    The name wait stays longer than this file's own bound, so a regression that
    joins the gate hangs the test instead of passing, and the answer is the 404 a
    name the app may not use gets. Deterministic: the app never calls the name wait.

    Mutation pin: drop the ``name_held_by_another`` check and the app joins the
    gate and waits out the bound.
    """
    _use_config(monkeypatch, _config())
    state.get_or_create_slot("theirs", app="app-b")
    entered: list[str] = []
    real_acquire = chat_handlers.acquire_slot_create_name

    async def watched(st: Any, key: str, principal: str = "") -> Any:
        entered.append(principal)
        return await real_acquire(st, key, principal)

    monkeypatch.setattr(chat_handlers, "acquire_slot_create_name", watched)
    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 3 * WAIT)
    release = await txn.acquire_slot_create_name(state, "free")
    assert release is not None
    try:
        async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
            busy = await _app_post(client, "free")
            not_yours = await _app_post(client, "theirs")
    finally:
        release()
    assert busy == not_yours == (404, {"error": "not found", "code": "slot_not_found"})
    assert entered == [], "the app entered the name wait"
    assert "free" not in state._slots
    assert txn._NAME_GATES.get(state, {}).get("free") is None


@pytest.mark.asyncio
async def test_an_app_still_waits_behind_its_own_create_of_a_name(state: Any) -> None:
    """The check is per principal: the app's own earlier create is not another's."""
    release = await txn.acquire_slot_create_name(state, "mine", "app-a")
    assert release is not None
    try:
        assert not txn.name_held_by_another(state, "mine", "app-a")
        assert txn.name_held_by_another(state, "mine", "app-b")
        assert txn.name_held_by_another(state, "mine", "")
    finally:
        release()
    assert not txn.name_held_by_another(state, "mine", "app-b")
    assert txn._NAME_GATES.get(state, {}).get("mine") is None


# -- the cron run-start bind has a fallback past the wait -------------------------


@pytest.mark.asyncio
async def test_a_cron_run_start_past_the_wait_bound_falls_back_without_raising(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_cron_slot`` meets a create of its key that outlasts the bounded wait.

    It starts the run without a tab, as the result injection keeps the run in
    the transcript, instead of raising "still being built" into the run.

    Mutation pin: call ``_bind_cron_slot`` with no ValueError fallback and this raises.
    """
    from kiro_crew.dashboard import cron_inject

    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 0.01)
    job = SimpleNamespace(
        id="j1",
        name="J",
        persistent_session=True,
        hide_in_chat=False,
        member_id="",
        agent_id="default",
        memory_store="",
        message="",
        chat_folder_id="",
    )
    pending = SimpleNamespace(slot=SimpleNamespace(key="cron-j1", linked_session_key=""))
    create = txn.PendingSlotCreate(state, pending)
    try:
        await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    finally:
        await create.settle()
    assert "cron-j1" not in state._slots
