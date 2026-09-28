"""The atomic multi-folder reorder endpoint.

``POST /api/chat/folders/reorder`` applies a whole ``[{id, order}, ...]`` list in
ONE ``mutate_folders`` pass under the folder-store lock, all-or-none -- the API
shape a caller needs to renumber several siblings without issuing one PATCH per
row, which has no transaction between the writes and leaves a mix of old and new
order numbers on a partial failure.

The load-bearing test is :class:`TestAMidSequenceWriteFailureLeavesTheOrderUntouched`:
it drives the REAL ``FolderRepository.mutate`` with a persist that fails partway
through writing, and asserts the stored order is exactly what it was -- never
half-applied. Everything above it (shape validation, per-row ownership) guards the
endpoint's edges; that class guards the property the issue was filed for.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import api_chat_folder_reorder
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.loop_lock import LoopBoundLock

# ...01 belongs to the person, ...02 to issue-radar, ...03 predates the owner field.
PERSON = "fldr00000001"
RADAR = "fldr00000002"
LEGACY = "fldr00000003"


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": PERSON, "name": "Work", "parent_id": "", "order": 0, "owner_app": ""},
        {"id": RADAR, "name": "Radar", "parent_id": "", "order": 1, "owner_app": "issue-radar"},
        {"id": LEGACY, "name": "Old", "parent_id": "", "order": 2},
    ]


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


def _state(
    *slots: _ChatSlot,
    folders: list[dict[str, Any]] | None = None,
    write_confirmed: Any = None,
) -> DashboardState:
    """A state whose ``mutate_folders`` runs the REAL repository transaction.

    ``write_confirmed`` is the off-loop persist. The default is a no-op success
    (the store landed); the failure test injects one that raises partway, which
    is the only difference between "the batch committed" and "the batch was
    refused mid-write" -- exactly the seam the atomicity claim rests on.
    """
    state = MagicMock(spec=DashboardState)
    state._folders = _folders() if folders is None else folders
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()

    repo = FolderRepository(lambda: MagicMock())
    lock = LoopBoundLock()
    writer = write_confirmed if write_confirmed is not None else (lambda _path, _snapshot: None)

    async def _mutate(fn: Any) -> Any:
        return await repo.mutate(
            lambda: state._folders,
            lock,
            fn,
            lambda: MagicMock(name="folders.json"),
            writer,
        )

    state.mutate_folders = _mutate
    return state


def _make_app(state: DashboardState, *, app_scope: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        # Empty for the internal-secret (MCP) transport an app agent's tool call
        # takes; set to name an app when the test drives an app-scoped caller.
        request["app"] = app_scope
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders/reorder", api_chat_folder_reorder)
    return app


def _by_id(state: DashboardState, fid: str) -> dict[str, Any] | None:
    return next((f for f in state._folders if f["id"] == fid), None)


class TestAMidSequenceWriteFailureLeavesTheOrderUntouched:
    """The whole reason the endpoint exists: a partial write cannot half-apply.

    The persist writes the new order and then reads it back to prove the WHOLE
    value landed (``FolderRepository.write_confirmed``). A writer that raises
    after mutating some rows is exactly a mid-sequence failure, and the
    transaction restores the pre-write list before releasing the lock -- so the
    caller sees the order it started with, not a mix.
    """

    @pytest.mark.asyncio
    async def test_a_failed_persist_restores_every_row(self) -> None:
        # A full reversal: 0,1,2 -> 2,1,0. Every row's order changes, so a
        # half-applied write would be plainly visible.
        recorded: dict[str, Any] = {}

        def _failing_writer(_path: Any, snapshot: list[dict[str, Any]]) -> None:
            # The store lock is held and the live list has ALREADY been mutated
            # in place by the callback at this point; capture what a half-applied
            # store would have looked like, then fail the write.
            recorded["snapshot_orders"] = {f["id"]: f["order"] for f in snapshot}
            raise OSError("disk full partway through the write")

        state = _state(_ChatSlot("chat-1-100"), write_confirmed=_failing_writer)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={
                    "orders": [
                        {"id": PERSON, "order": 2},
                        {"id": RADAR, "order": 1},
                        {"id": LEGACY, "order": 0},
                    ]
                },
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        # The endpoint surfaces the persist failure rather than reporting success.
        assert resp.status >= 500
        # The writer really did see the new order (proving the callback applied
        # it before the persist) -- so the restore below is undoing a real write,
        # not a no-op.
        assert recorded["snapshot_orders"] == {PERSON: 2, RADAR: 1, LEGACY: 0}
        # The stored order is EXACTLY the pre-request order, not the half.
        assert _by_id(state, PERSON)["order"] == 0
        assert _by_id(state, RADAR)["order"] == 1
        assert _by_id(state, LEGACY)["order"] == 2


class TestASuccessfulReorderAppliesTheWholeList:
    @pytest.mark.asyncio
    async def test_every_named_row_lands(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={
                    "orders": [
                        {"id": PERSON, "order": 2},
                        {"id": RADAR, "order": 0},
                        {"id": LEGACY, "order": 1},
                    ]
                },
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True}
        assert _by_id(state, PERSON)["order"] == 2
        assert _by_id(state, RADAR)["order"] == 0
        assert _by_id(state, LEGACY)["order"] == 1

    @pytest.mark.asyncio
    async def test_an_empty_reorder_is_a_no_op_success(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": []},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        # Untouched.
        assert [f["order"] for f in state._folders] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_a_negative_order_is_stored_verbatim(self) -> None:
        """A drag that puts a folder ahead of the first sibling writes
        ``first.order - 1``, which is negative once a set is renumbered from 0."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": RADAR, "order": -1}]},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["order"] == -1


class TestOwnershipIsReDecidedPerRowUnderTheLock:
    """Ownership lives here, enforced for EVERY row inside the transaction the
    way the single-row PATCH does -- so an app naming a folder it does not own is
    refused whole, and the store is untouched."""

    @pytest.mark.asyncio
    async def test_a_batch_naming_a_foreign_row_is_refused_whole(self) -> None:
        # issue-radar owns RADAR but not PERSON; naming both must refuse ALL.
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state, app_scope="issue-radar"))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={
                    "orders": [
                        {"id": RADAR, "order": 5},
                        {"id": PERSON, "order": 6},
                    ]
                },
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        # Neither row moved -- the OWNED one did not land either.
        assert _by_id(state, RADAR)["order"] == 1
        assert _by_id(state, PERSON)["order"] == 0

    @pytest.mark.asyncio
    async def test_a_row_whose_subtree_holds_a_foreign_folder_is_refused_whole(self) -> None:
        """Row ownership is not the whole rule: repositioning takes the subtree.

        The app owns the row it names, so the per-row owner check passes. But the
        person's folder is nested inside it, and moving the parent's ``order``
        relocates the child -- the same violation the reparent PATCH refuses one
        level down. The reorder that composes the writes is the one place under
        the lock that sees the subtree, so the guard lives here, and the whole
        batch is refused with the store untouched.
        """
        # RADAR is issue-radar's; PERSON is nested inside RADAR and is the
        # person's. Repositioning RADAR would relocate PERSON.
        nested = [
            {"id": RADAR, "name": "Radar", "parent_id": "", "order": 0, "owner_app": "issue-radar"},
            {"id": PERSON, "name": "Work", "parent_id": RADAR, "order": 0, "owner_app": ""},
            {"id": LEGACY, "name": "Old", "parent_id": "", "order": 1, "owner_app": "issue-radar"},
        ]
        state = _state(_app_slot("chat-1-200", "issue-radar"), folders=nested)
        async with TestClient(TestServer(_make_app(state, app_scope="issue-radar"))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": RADAR, "order": 5}]},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        # RADAR did not move: the guard fired before any write landed.
        assert _by_id(state, RADAR)["order"] == 0

    @pytest.mark.asyncio
    async def test_a_legacy_row_reads_as_the_persons(self) -> None:
        """A row with no ``owner_app`` key is the person's, so an app is refused
        it -- the absent-key default the single-row PATCH also applies."""
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state, app_scope="issue-radar"))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": LEGACY, "order": 0}]},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
        assert resp.status == 403
        assert _by_id(state, LEGACY)["order"] == 2

    @pytest.mark.asyncio
    async def test_an_app_may_reorder_only_its_own(self) -> None:
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state, app_scope="issue-radar"))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": RADAR, "order": 9}]},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["order"] == 9

    @pytest.mark.asyncio
    async def test_the_person_is_not_confined(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={
                    "orders": [
                        {"id": RADAR, "order": 0},
                        {"id": PERSON, "order": 1},
                    ]
                },
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["order"] == 0
        assert _by_id(state, PERSON)["order"] == 1


class TestAMissingRowRefusesTheWholeBatch:
    @pytest.mark.asyncio
    async def test_a_reorder_naming_a_deleted_folder_lands_nothing(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={
                    "orders": [
                        {"id": RADAR, "order": 0},
                        {"id": "fldrdoesnotexist", "order": 1},
                    ]
                },
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 404
        assert body["code"] == "folder_not_found"
        # The row that DID exist did not move -- the batch is all-or-none.
        assert _by_id(state, RADAR)["order"] == 1


class TestTheRequestShapeIsValidatedBeforeTheStore:
    @pytest.mark.asyncio
    async def test_orders_must_be_an_array(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": {"id": RADAR, "order": 0}},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "orders_not_array"

    @pytest.mark.asyncio
    async def test_an_entry_must_be_an_object(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [RADAR]},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "order_entry_invalid"

    @pytest.mark.asyncio
    async def test_an_entry_needs_an_id(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"order": 0}]},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "order_id_missing"

    @pytest.mark.asyncio
    async def test_a_non_integer_order_is_refused_not_silently_skipped(self) -> None:
        """The single-row PATCH skips a bad ``order`` field; here the field IS the
        request, so a bad value refuses rather than leaving the row unmoved."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/reorder",
                json={"orders": [{"id": RADAR, "order": "first"}]},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "order_not_int"

    @pytest.mark.asyncio
    async def test_a_boolean_or_float_order_is_refused_not_coerced(self) -> None:
        """A JSON boolean or float is not an integer, so the endpoint 400s it.

        ``int(True)`` is 1 and ``int(1.5)`` truncates to 1, so a bare ``int(...)``
        would silently accept both and move the row to a position the caller never
        named. The integer-only contract rejects them instead. ``True`` is the
        important case: a Python ``bool`` is an ``int`` subclass, so only an exact
        ``type(...) is int`` check excludes it.
        """
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            for bad in (True, 1.5):
                resp = await client.post(
                    "/api/chat/folders/reorder",
                    json={"orders": [{"id": RADAR, "order": bad}]},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                body = await resp.json()
                assert resp.status == 400, bad
                assert body["code"] == "order_not_int", bad
