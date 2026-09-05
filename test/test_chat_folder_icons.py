"""Auto-generated and manual emoji icons on chat folders (issue #6586).

The generator (``generate_emoji_for_name``) is shared with the artifact
library and already covered elsewhere; these tests pin the CHAT-FOLDER wiring
that regressed in the MeshClaw port:

* create spawns a background generation task (and skips it when the caller
  supplied an explicit icon),
* PATCH accepts ``icon`` (set / clear) and ``regenerate_icon`` (reset to
  auto), rejecting the two together,
* the write-back goes through ``mutate_folders`` and re-finds the folder by
  id, so a folder deleted mid-generation is never resurrected,
* app ownership gates icon writes exactly like every other folder field.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.chat_folders as chat_folders
from kiro_crew.dashboard.chat_folders import (
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_update,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

PERSON = "fldr00000001"
FOREIGN = "fldr00000002"


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": PERSON, "name": "Work", "parent_id": ""},
        {"id": FOREIGN, "name": "Radar output", "parent_id": "", "owner_app": "issue-radar"},
    ]


def _state(*slots: _ChatSlot, folders: list[dict[str, Any]] | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = _folders() if folders is None else folders
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any) -> Any:
        _changed, value = fn(state._folders)
        return value

    state.mutate_folders = AsyncMock(side_effect=_mutate)
    return state


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    return app


def _by_id(state: DashboardState, fid: str) -> dict[str, Any] | None:
    return next((f for f in state._folders if f["id"] == fid), None)


async def _drain_icon_tasks() -> None:
    """Wait for every in-flight icon write-back before asserting on state."""
    tasks = list(chat_folders._CHAT_FOLDER_ICON_TASKS)
    if tasks:
        await asyncio.gather(*tasks)


HEADERS = {"X-Session-Key": "dashboard:chat-1-100"}


class TestCreateGeneratesAnIcon:
    @pytest.mark.asyncio
    async def test_create_spawns_generation_and_writes_the_icon(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="🚀")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                )
                body = await resp.json()
                assert resp.status == 201
                # The create response itself carries no icon — it arrives async.
                assert "icon" not in body
                await _drain_icon_tasks()
        gen.assert_awaited_once_with(state, "Rocketry")
        created = _by_id(state, body["id"])
        assert created is not None and created["icon"] == "🚀"
        # The write-back pushed a slots update so the UI learns about the icon.
        assert state.push_slots_update.call_count >= 2

    @pytest.mark.asyncio
    async def test_an_explicit_icon_is_stored_and_generation_is_skipped(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="🚀")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Rocketry", "icon": "🧪"},
                    headers=HEADERS,
                )
                body = await resp.json()
                assert resp.status == 201
                assert body["icon"] == "🧪"
                await _drain_icon_tasks()
        gen.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_invalid_explicit_icon_is_a_400(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Rocketry", "icon": "not-an-emoji"},
                headers=HEADERS,
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "icon_invalid"

    @pytest.mark.asyncio
    async def test_a_failed_generation_leaves_the_folder_unchanged(self) -> None:
        """The generator's contract is '' on any failure — no icon key lands."""
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(return_value="")):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                )
                body = await resp.json()
                assert resp.status == 201
                await _drain_icon_tasks()
        created = _by_id(state, body["id"])
        assert created is not None and "icon" not in created

    @pytest.mark.asyncio
    async def test_a_folder_deleted_mid_generation_is_not_resurrected(self) -> None:
        """The write-back re-finds the folder under the store lock; a folder
        that vanished while the LLM ran gets no write and no slots push."""
        state = _state(_ChatSlot("chat-1-100"))

        async def _gen(_state: Any, _name: str) -> str:
            # Delete the folder while generation is "running". Keyed on the
            # name, not a captured id: the task can start before the test has
            # parsed the create response.
            state._folders = [f for f in state._folders if f["name"] != "Rocketry"]
            return "🚀"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gen)):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                )
                body = await resp.json()
                assert resp.status == 201
                push_count_before_task = state.push_slots_update.call_count
                await _drain_icon_tasks()
        assert _by_id(state, body["id"]) is None
        # No extra push for a write that never happened.
        assert state.push_slots_update.call_count == push_count_before_task

    @pytest.mark.asyncio
    async def test_a_manual_icon_set_mid_generation_is_not_clobbered(self) -> None:
        """The write-back only lands while the folder's icon epoch is unchanged
        — a manual icon set (real PATCH) while the LLM runs bumps the epoch, so
        the stale generated result is dropped."""
        state = _state(_ChatSlot("chat-1-100"))
        release = asyncio.Event()

        async def _gen(_state: Any, _name: str) -> str:
            await release.wait()  # hold generation until the PATCH lands
            return "🚀"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gen)):
            async with TestClient(TestServer(_make_app(state))) as client:
                try:
                    resp = await client.post(
                        "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                    )
                    body = await resp.json()
                    assert resp.status == 201
                    patched = await client.patch(
                        f"/api/chat/folders/{body['id']}", json={"icon": "🧪"}, headers=HEADERS
                    )
                    assert patched.status == 200
                finally:
                    # A failure above must not leak the gated task: it would
                    # stay pending on release.wait(), bound to this test's
                    # soon-closed loop, and break every later drain.
                    release.set()
                    await _drain_icon_tasks()
        created = _by_id(state, body["id"])
        assert created is not None and created["icon"] == "🧪"

    @pytest.mark.asyncio
    async def test_an_icon_clear_mid_generation_is_not_overwritten(self) -> None:
        """An explicit clear (PATCH icon: "") while generation is in flight
        must win: the epoch bump invalidates the pending result, so the folder
        stays icon-less. Under the previous value-pin the clear left the icon
        equal to its at-schedule value (absent -> absent), so the stale emoji
        landed anyway."""
        state = _state(_ChatSlot("chat-1-100"))
        release = asyncio.Event()

        async def _gen(_state: Any, _name: str) -> str:
            await release.wait()
            return "🚀"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gen)):
            async with TestClient(TestServer(_make_app(state))) as client:
                try:
                    resp = await client.post(
                        "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                    )
                    body = await resp.json()
                    assert resp.status == 201
                    patched = await client.patch(
                        f"/api/chat/folders/{body['id']}", json={"icon": ""}, headers=HEADERS
                    )
                    assert patched.status == 200
                finally:
                    # A failure above must not leak the gated task (see the
                    # manual-set test above).
                    release.set()
                    await _drain_icon_tasks()
        created = _by_id(state, body["id"])
        assert created is not None and "icon" not in created

    @pytest.mark.asyncio
    async def test_a_rename_mid_generation_drops_the_stale_icon(self) -> None:
        """A rename while generation is in flight invalidates the result — the
        pending emoji was derived from the old name and must not land on the
        renamed folder (and a rename never re-arms generation by design)."""
        state = _state(_ChatSlot("chat-1-100"))
        release = asyncio.Event()

        async def _gen(_state: Any, _name: str) -> str:
            await release.wait()
            return "🚀"

        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gen)
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                try:
                    resp = await client.post(
                        "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                    )
                    body = await resp.json()
                    assert resp.status == 201
                    patched = await client.patch(
                        f"/api/chat/folders/{body['id']}",
                        json={"name": "Chemistry"},
                        headers=HEADERS,
                    )
                    assert patched.status == 200
                finally:
                    # A failure above must not leak the gated task (see the
                    # manual-set test above).
                    release.set()
                    await _drain_icon_tasks()
        created = _by_id(state, body["id"])
        assert created is not None and created["name"] == "Chemistry"
        assert "icon" not in created
        # Only the create-time generation ran; the rename armed nothing new.
        gen.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_explicit_empty_icon_on_create_skips_generation(self) -> None:
        """icon: "" on create is an opt-out, not an omission — the folder gets
        no icon and the generator never runs."""
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="🚀")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Rocketry", "icon": ""},
                    headers=HEADERS,
                )
                body = await resp.json()
                assert resp.status == 201
                assert "icon" not in body
                await _drain_icon_tasks()
        gen.assert_not_awaited()
        created = _by_id(state, body["id"])
        assert created is not None and "icon" not in created


class TestPatchIcon:
    @pytest.mark.asyncio
    async def test_icon_and_regenerate_together_conflict(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"icon": "🧪", "regenerate_icon": True},
                headers=HEADERS,
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "icon_conflict"

    @pytest.mark.asyncio
    async def test_a_non_boolean_regenerate_icon_is_a_400(self) -> None:
        """The string "false" is truthy — a sloppy caller must get a 400, not
        a surprise regeneration (or a phantom icon_conflict)."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"regenerate_icon": "false"},
                headers=HEADERS,
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "regenerate_icon_invalid"

    @pytest.mark.asyncio
    async def test_manual_icon_set_and_clear(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"icon": "🧪"}, headers=HEADERS
            )
            assert resp.status == 200
            folder = _by_id(state, PERSON)
            assert folder is not None and folder["icon"] == "🧪"
            # None clears back to the default glyph — the key is dropped, so
            # "absent means the default" stays the one on-disk representation.
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"icon": None}, headers=HEADERS
            )
            assert resp.status == 200
        folder = _by_id(state, PERSON)
        assert folder is not None and "icon" not in folder

    @pytest.mark.asyncio
    async def test_an_invalid_manual_icon_is_a_400(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            for bad in ("abc", "🧪🚀", "x🧪"):
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}", json={"icon": bad}, headers=HEADERS
                )
                body = await resp.json()
                assert resp.status == 400, bad
                assert body["code"] == "icon_invalid"
        folder = _by_id(state, PERSON)
        assert folder is not None and "icon" not in folder

    @pytest.mark.asyncio
    async def test_regenerate_spawns_the_generator_with_the_folder_name(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="📈")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                await _drain_icon_tasks()
        gen.assert_awaited_once_with(state, "Work")
        folder = _by_id(state, PERSON)
        assert folder is not None and folder["icon"] == "📈"

    @pytest.mark.asyncio
    async def test_regenerate_uses_the_renamed_name_when_combined(self) -> None:
        """rename + regenerate in one PATCH regenerates from the NEW name —
        the apply lands before the task is spawned."""
        state = _state(_ChatSlot("chat-1-100"))
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="📈")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"name": "Finances", "regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                await _drain_icon_tasks()
        gen.assert_awaited_once_with(state, "Finances")


class TestAppOwnership:
    @pytest.mark.asyncio
    async def test_an_app_cannot_set_the_icon_of_a_folder_it_does_not_own(self) -> None:
        slot = _ChatSlot("chat-1-100")
        slot._app = "spec-builder"
        state = _state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{FOREIGN}", json={"icon": "🧪"}, headers=HEADERS
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        folder = _by_id(state, FOREIGN)
        assert folder is not None and "icon" not in folder

    @pytest.mark.asyncio
    async def test_an_app_cannot_regenerate_a_foreign_folders_icon(self) -> None:
        slot = _ChatSlot("chat-1-100")
        slot._app = "spec-builder"
        state = _state(slot)
        with patch.object(
            chat_folders, "generate_emoji_for_name", AsyncMock(return_value="📈")
        ) as gen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{FOREIGN}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 403
                await _drain_icon_tasks()
        gen.assert_not_awaited()


class TestDeleteEpochLifecycle:
    @pytest.mark.asyncio
    async def test_a_failed_delete_commit_keeps_the_epoch_guard(self) -> None:
        """The epoch entry is popped only AFTER the removal is confirmed
        persisted. Popping inside the mutation callback is a module-level side
        effect that survives a failed store write: the folder would still
        exist while its epoch read 0 again, so a stale in-flight generation
        could land over a manual icon."""
        state = _state(_ChatSlot("chat-1-100"))
        chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                patched = await client.patch(
                    f"/api/chat/folders/{PERSON}", json={"icon": "🧪"}, headers=HEADERS
                )
                assert patched.status == 200
                assert chat_folders._CHAT_FOLDER_ICON_EPOCHS[PERSON] == 1

                async def _commit_fails(fn: Any) -> Any:
                    # The callback runs (as it does before the repository
                    # persists), then the commit itself fails.
                    fn(state._folders)
                    raise OSError("simulated store write failure")

                state.mutate_folders.side_effect = _commit_fails
                resp = await client.delete(f"/api/chat/folders/{PERSON}", headers=HEADERS)
                assert resp.status == 500
            # The guard survived the failed commit.
            assert chat_folders._CHAT_FOLDER_ICON_EPOCHS.get(PERSON) == 1
        finally:
            chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)

    @pytest.mark.asyncio
    async def test_a_successful_delete_pops_the_epoch_entry(self) -> None:
        """A confirmed delete releases the entry so the registry does not grow
        with every deleted-folder id over the process lifetime."""
        state = _state(_ChatSlot("chat-1-100"))
        chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)
        async with TestClient(TestServer(_make_app(state))) as client:
            patched = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"icon": "🧪"}, headers=HEADERS
            )
            assert patched.status == 200
            assert chat_folders._CHAT_FOLDER_ICON_EPOCHS[PERSON] == 1
            resp = await client.delete(f"/api/chat/folders/{PERSON}", headers=HEADERS)
            assert resp.status == 200
        assert chat_folders._CHAT_FOLDER_ICON_EPOCHS.get(PERSON) is None
        assert _by_id(state, PERSON) is None
