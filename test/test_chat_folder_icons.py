"""Auto-generated and manual emoji icons on chat folders.

The generator (``generate_emoji_for_name``) is shared with the artifact
library and already covered elsewhere; these tests pin the CHAT-FOLDER wiring:

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
import json
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

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            # The real store runs the hook under the lock only after the
            # write is proven; this fake always "persists", so a changed
            # transaction is a committed one.
            on_committed()
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
    @pytest.mark.parametrize("payload", ([], "a string", 5, True, None))
    async def test_a_non_object_body_on_update_is_a_400_not_a_500(self, payload: Any) -> None:
        """``[]`` etc. are valid JSON, so ``request.json()`` parses them and the
        handler's ``body.get("regenerate_icon", ...)`` would raise
        AttributeError outside the parse ``try`` — a 500 for malformed client
        input. Sent as raw text because the client's
        ``json=None`` means "no body", which exercises the parse error, not
        the guard."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                data=json.dumps(payload),
                headers={**HEADERS, "Content-Type": "application/json"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ([], "a string", 5, True, None))
    async def test_a_non_object_body_on_create_is_a_400_not_a_500(self, payload: Any) -> None:
        """Same guard on the create handler, whose first body read is the icon."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                data=json.dumps(payload),
                headers={**HEADERS, "Content-Type": "application/json"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "invalid_json"

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

    @pytest.mark.asyncio
    async def test_regenerate_uses_the_committed_name_over_a_stale_snapshot(self) -> None:
        """A rename that commits while a bare regenerate PATCH waits for the
        store lock wins even when it replaces the stored folder object: the
        generator derives from the name the store holds when the PATCH's own
        transaction commits, not from the handler's pre-lock read, which now
        points at a detached object."""
        state = _state(_ChatSlot("chat-1-100"))
        orig_mutate = state.mutate_folders

        async def mutate_with_concurrent_rename(fn, on_committed=None):  # type: ignore[no-untyped-def]
            if fn.__name__ == "_apply":

                def _rename(folders: list) -> tuple[bool, str]:
                    idx = next(i for i, f in enumerate(folders) if f["id"] == PERSON)
                    folders[idx] = {**folders[idx], "name": "Ledger"}
                    return True, ""

                await orig_mutate(_rename)
            return await orig_mutate(fn, on_committed=on_committed)

        with (
            patch.object(state, "mutate_folders", mutate_with_concurrent_rename),
            patch.object(
                chat_folders, "generate_emoji_for_name", AsyncMock(return_value="📒")
            ) as gen,
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                await _drain_icon_tasks()
        gen.assert_awaited_once_with(state, "Ledger")

    @pytest.mark.asyncio
    async def test_a_rename_committing_after_the_regenerate_invalidates_its_write_back(
        self,
    ) -> None:
        """The expected epoch is captured at commit time, inside the PATCH's
        own post-commit hook. A rename that commits right after — before the
        handler resumes — bumps past it, so the generation derived from the
        PATCH's committed name is rejected by the write-back instead of
        landing over the newer name."""
        state = _state(_ChatSlot("chat-1-100"))
        orig_mutate = state.mutate_folders

        async def mutate_then_concurrent_rename(fn, on_committed=None):  # type: ignore[no-untyped-def]
            result = await orig_mutate(fn, on_committed=on_committed)
            if fn.__name__ == "_apply":

                def _rename(folders: list) -> tuple[bool, str]:
                    target = next(f for f in folders if f["id"] == PERSON)
                    target["name"] = "Ledger"
                    return True, ""

                await orig_mutate(
                    _rename,
                    on_committed=lambda: chat_folders._bump_icon_epoch(PERSON),
                )
            return result

        with (
            patch.object(state, "mutate_folders", mutate_then_concurrent_rename),
            patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(return_value="📒")),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                await _drain_icon_tasks()
        folder = _by_id(state, PERSON)
        assert folder is not None and folder.get("icon") != "📒"


class TestIconTaskCoalescing:
    """At most one live generation task — and one model call — per folder.

    A burst of ``regenerate_icon`` PATCHes must supersede, not accumulate:
    tasks queue behind a serialized one-call-at-a-time generator, so without
    per-folder coalescing an authenticated caller looping the PATCH grows the
    pending-task set and the paid-call backlog without bound.
    """

    @pytest.mark.asyncio
    async def test_a_regenerate_burst_holds_one_live_task_and_cleans_up(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        release = asyncio.Event()

        async def _gated(_state: Any, _name: str) -> str:
            await release.wait()
            return "📈"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gated)):
            async with TestClient(TestServer(_make_app(state))) as client:
                for _ in range(3):
                    resp = await client.patch(
                        f"/api/chat/folders/{PERSON}",
                        json={"regenerate_icon": True},
                        headers=HEADERS,
                    )
                    assert resp.status == 200
                # Let the two superseded tasks finish cancelling.
                for _ in range(3):
                    await asyncio.sleep(0)
                live = [t for t in chat_folders._CHAT_FOLDER_ICON_TASKS if not t.done()]
                assert len(live) == 1
                # The pending slot maps the folder to the surviving task — the
                # superseded tasks' cleanup callbacks must not have evicted it.
                assert chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS[PERSON] is live[0]
                release.set()
                await asyncio.gather(*chat_folders._CHAT_FOLDER_ICON_TASKS, return_exceptions=True)
        folder = _by_id(state, PERSON)
        assert folder is not None and folder["icon"] == "📈"
        assert not chat_folders._CHAT_FOLDER_ICON_TASKS
        assert not chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS

    @pytest.mark.asyncio
    async def test_delete_cancels_the_folder_pending_icon_generation(self) -> None:
        """Deleting a folder cancels its in-flight icon generation and drops
        the registry entry. Without the cancel, an owner looping
        create->delete accumulates one queued task per deleted folder behind
        the serialized generator — the task set would grow without bound."""
        state = _state(_ChatSlot("chat-1-100"))
        release = asyncio.Event()

        async def _gated(_state: Any, _name: str) -> str:
            # Never released: only the delete-path cancel can end this task.
            await release.wait()
            return "🚀"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gated)):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders", json={"name": "Rocketry"}, headers=HEADERS
                )
                assert resp.status == 201
                fid = (await resp.json())["id"]
                assert fid in chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS
                resp = await client.delete(f"/api/chat/folders/{fid}", headers=HEADERS)
                assert resp.status == 200
                # The registry entry is gone and the task has been cancelled —
                # the generator's gate never opens, so nothing else ends it.
                assert fid not in chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS
                await asyncio.gather(*chat_folders._CHAT_FOLDER_ICON_TASKS, return_exceptions=True)
        assert not chat_folders._CHAT_FOLDER_ICON_TASKS
        assert not chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS
        assert _by_id(state, fid) is None

    @pytest.mark.asyncio
    async def test_cancel_mid_write_lets_the_started_transaction_finish(self) -> None:
        """A superseding regenerate must not tear an in-flight store write.

        Cancelling the pending task while its ``mutate_folders`` transaction
        is running must let that transaction finish before the task unwinds:
        an aborted-midway write would release the store lock while the
        executor thread keeps writing its stale whole-list snapshot, silently
        reverting interim committed folder mutations on the next reload.
        """
        state = _state(_ChatSlot("chat-1-100"))
        write_entered = asyncio.Event()
        write_gate = asyncio.Event()
        gen_gate = asyncio.Event()
        gen_calls = 0

        async def _mutate(fn: Any, on_committed: Any = None) -> Any:
            # Park only the icon write-back (the `_write` closure), simulating
            # the in-lock ``asyncio.to_thread`` write; the PATCH handler's own
            # `_apply` transaction must stay instant or the request deadlocks.
            if fn.__name__ == "_write":
                write_entered.set()
                await write_gate.wait()
            changed, value = fn(state._folders)
            if changed and on_committed is not None:
                on_committed()
            return value

        state.mutate_folders = AsyncMock(side_effect=_mutate)

        async def _gen(_state: Any, _name: str) -> str:
            nonlocal gen_calls
            gen_calls += 1
            if gen_calls == 1:
                return "1️⃣"
            await gen_gate.wait()
            return "2️⃣"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gen)):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                await write_entered.wait()
                first = chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS[PERSON]
                # Supersede while the first task's write is in flight.
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"regenerate_icon": True},
                    headers=HEADERS,
                )
                assert resp.status == 200
                for _ in range(5):
                    await asyncio.sleep(0)
                # The cancel landed, but the started transaction is still
                # running — the shield must keep the task alive until the
                # write completes instead of unwinding mid-write.
                assert not first.done()
                write_gate.set()
                await asyncio.gather(first, return_exceptions=True)
                assert first.cancelled()
                folder = _by_id(state, PERSON)
                # The started write ran to completion despite the cancel.
                assert folder is not None and folder["icon"] == "1️⃣"
                gen_gate.set()
                await asyncio.gather(*chat_folders._CHAT_FOLDER_ICON_TASKS, return_exceptions=True)
        folder = _by_id(state, PERSON)
        # The superseding task wrote last, so its icon wins.
        assert folder is not None and folder["icon"] == "2️⃣"
        assert not chat_folders._CHAT_FOLDER_ICON_TASKS
        assert not chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS

    @pytest.mark.asyncio
    async def test_different_folders_do_not_coalesce_each_other(self) -> None:
        folders = [
            {"id": PERSON, "name": "Work", "parent_id": ""},
            {"id": "fldr00000003", "name": "Music", "parent_id": ""},
        ]
        state = _state(_ChatSlot("chat-1-100"), folders=folders)
        release = asyncio.Event()

        async def _gated(_state: Any, name: str) -> str:
            await release.wait()
            return "💼" if name == "Work" else "🎵"

        with patch.object(chat_folders, "generate_emoji_for_name", AsyncMock(side_effect=_gated)):
            async with TestClient(TestServer(_make_app(state))) as client:
                for fid in (PERSON, "fldr00000003"):
                    resp = await client.patch(
                        f"/api/chat/folders/{fid}",
                        json={"regenerate_icon": True},
                        headers=HEADERS,
                    )
                    assert resp.status == 200
                for _ in range(3):
                    await asyncio.sleep(0)
                live = [t for t in chat_folders._CHAT_FOLDER_ICON_TASKS if not t.done()]
                assert len(live) == 2
                assert len(chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS) == 2
                release.set()
                await asyncio.gather(*chat_folders._CHAT_FOLDER_ICON_TASKS, return_exceptions=True)
        work = _by_id(state, PERSON)
        music = _by_id(state, "fldr00000003")
        assert work is not None and work["icon"] == "💼"
        assert music is not None and music["icon"] == "🎵"
        assert not chat_folders._CHAT_FOLDER_PENDING_ICON_TASKS


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


class TestPatchEpochLifecycle:
    @pytest.mark.asyncio
    async def test_a_failed_patch_commit_leaves_the_epoch_untouched(self) -> None:
        """The epoch bump rides the store's post-commit hook. A rolled-back
        write leaves the folder list restored AND the epoch as it was: an
        in-flight generation derived from the still-current name remains
        valid, so a bump that outlives the rollback would spuriously discard
        its result."""
        state = _state(_ChatSlot("chat-1-100"))
        chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:

                async def _commit_fails(fn: Any, on_committed: Any = None) -> Any:
                    # The mutation callback runs (as it does before the
                    # repository persists), then the commit itself fails —
                    # the post-commit hook must not run.
                    fn(state._folders)
                    raise OSError("simulated store write failure")

                state.mutate_folders.side_effect = _commit_fails
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}", json={"icon": "🧪"}, headers=HEADERS
                )
                assert resp.status == 500
            assert chat_folders._CHAT_FOLDER_ICON_EPOCHS.get(PERSON) is None
        finally:
            chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)

    @pytest.mark.asyncio
    async def test_a_committed_patch_bumps_the_epoch(self) -> None:
        """A persisted icon or name change invalidates an in-flight
        generation: the bump runs in the post-commit hook, under the store
        lock, so it stays ordered against the write-back's epoch check."""
        state = _state(_ChatSlot("chat-1-100"))
        chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}", json={"icon": "🧪"}, headers=HEADERS
                )
                assert resp.status == 200
                assert chat_folders._CHAT_FOLDER_ICON_EPOCHS[PERSON] == 1
                renamed = await client.patch(
                    f"/api/chat/folders/{PERSON}", json={"name": "Renamed"}, headers=HEADERS
                )
                assert renamed.status == 200
                assert chat_folders._CHAT_FOLDER_ICON_EPOCHS[PERSON] == 2
        finally:
            chat_folders._CHAT_FOLDER_ICON_EPOCHS.pop(PERSON, None)


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
