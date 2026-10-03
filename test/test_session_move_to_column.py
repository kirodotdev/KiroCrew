"""``session_move_to_column`` and the ``POST /api/chat/slots/{slot}/drop`` route it calls.

Route half: the drop now carries the ownership fences ``PUT /tags`` has (app
slot and transcript ownership, the crew-member ``member_owns_slot`` fence), and
an internal (MCP) caller is held to the ``chat_tag set_state`` policy from the
protected grants store, while the browser drop is unchanged. A successful drop
mirrors the new tags onto every live alias of the transcript.

Tool half: dispatch only, with the HTTP helpers patched: strict identity, the
channel refusal, column-name resolution, the body it posts, and how each route
answer is rendered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.mcp_dashboard import _call_tool_inner, _list_tools
from kiro_crew.validation import ValidationError

_AGENT = {"X-Internal-Secret": "s", "X-Internal-Caller": "kirocrew-dashboard"}


@pytest.fixture(autouse=True)
def _hermetic_signing_secret(monkeypatch):
    """Pin the grants-store provenance key, as test_chat_tags.py does."""
    from kiro_crew.dashboard import token_secret

    monkeypatch.setattr(token_secret, "_get_secret", lambda: b"test-signing-key")


def _app(state: Any, *, declared_app: str = "", member_principal: str = "") -> web.Application:
    from kiro_crew.dashboard import chat_tag_grants
    from kiro_crew.dashboard.chat_tags import (
        api_chat_slot_drop,
        api_chat_tag_column_create,
        api_chat_tag_create,
        api_chat_tag_update,
    )

    if not chat_tag_grants._store_path().exists():
        chat_tag_grants.seed_default_grants([])
        chat_tag_grants.refresh_cache()

    @web.middleware
    async def _claims(request: web.Request, handler):
        # Stands in for the token middleware and the chat-route gate. Setup
        # calls (no X-Internal-Secret) are the owner at the dashboard.
        request["app"] = declared_app if "X-Session-Key" in request.headers else ""
        request["user"] = "local-app"
        if member_principal and "X-Session-Key" in request.headers:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = member_principal
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = state
    app.router.add_post("/api/chat/tags", api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", api_chat_tag_update)
    app.router.add_post("/api/chat/tag-columns", api_chat_tag_column_create)
    app.router.add_post("/api/chat/slots/{slot}/drop", api_chat_slot_drop)
    return app


async def _tag(client: TestClient, name: str, *, status: bool, agent: str) -> str:
    tag = await (await client.post("/api/chat/tags", json={"name": name, "status": status})).json()
    resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"agent": agent})
    assert resp.status == 200, await resp.text()
    return str(tag["id"])


async def _column(client: TestClient, name: str, tag_ids: list[str]) -> str:
    col = await (
        await client.post("/api/chat/tag-columns", json={"name": name, "tag_ids": tag_ids})
    ).json()
    return str(col["id"])


def _slot(state: Any, key: str, tags: list[str], app: str = "") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.tags = list(tags)
    slot._app = app
    state._slots[key] = slot
    return slot


async def _drop(client: TestClient, slot: str, body: dict, headers: dict | None = None) -> Any:
    with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", return_value=True):
        resp = await client.post(f"/api/chat/slots/{slot}/drop", json=body, headers=headers or {})
        return resp.status, await resp.json()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


class TestAgentPolicy:
    @pytest.mark.asyncio
    async def test_an_agent_moves_a_card_between_agent_writable_states(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            label = await _tag(client, "spike", status=False, agent="none")
            col = await _column(client, "Review", [review])
            slot = _slot(state, "s1", [todo, label])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200 and body["ok"] is True
        # The human-only plain label is KEPT, not stripped, so it needs no grant.
        assert slot.tags == [label, review]

    @pytest.mark.asyncio
    async def test_a_human_reserved_target_state_is_refused(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            done = await _tag(client, "Done", status=True, agent="none")
            col = await _column(client, "Done", [done])
            slot = _slot(state, "s1", [todo])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 403
        assert (body["code"], body["error"]) == ("tag_policy_denied", f"tag_policy_denied:{done}")
        assert slot.tags == [todo]

    @pytest.mark.asyncio
    async def test_stripping_a_human_reserved_state_is_refused(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            blocked = await _tag(client, "Blocked", status=True, agent="add-only")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            slot = _slot(state, "s1", [blocked])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 403
        assert (body["code"], body["error"]) == (
            "tag_policy_denied",
            f"tag_policy_denied:{blocked}",
        )
        assert slot.tags == [blocked]

    @pytest.mark.asyncio
    async def test_a_forged_vocabulary_status_does_not_make_a_workflow_state(self, state) -> None:
        """The store's status bit decides, never the agent-writable tags.json field."""
        async with TestClient(TestServer(_app(state))) as client:
            plain = await _tag(client, "Plain", status=False, agent="add-remove")
            col = await _column(client, "Plain", [plain])
            next(t for t in state._tags if t["id"] == plain)["status"] = True
            slot = _slot(state, "s1", [])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 403 and body["code"] == "not_a_status_tag"
        assert slot.tags == []

    @pytest.mark.asyncio
    async def test_a_protected_state_with_a_forged_false_status_is_still_stripped(
        self, state
    ) -> None:
        """Two workflow states must not survive an agent move side by side."""
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            next(t for t in state._tags if t["id"] == todo)["status"] = False
            slot = _slot(state, "s1", [todo])
            status, _ = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200
        assert slot.tags == [review]

    @pytest.mark.asyncio
    async def test_a_protected_state_with_a_forged_false_status_still_needs_its_grant(
        self, state
    ) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            done = await _tag(client, "Done", status=True, agent="none")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            next(t for t in state._tags if t["id"] == done)["status"] = False
            slot = _slot(state, "s1", [done])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 403 and body["error"] == f"tag_policy_denied:{done}"
        assert slot.tags == [done]

    @pytest.mark.asyncio
    async def test_a_protected_label_with_a_forged_true_status_is_kept(self, state) -> None:
        """An agent move must not strip a plain label by forging its ``status`` bit."""
        async with TestClient(TestServer(_app(state))) as client:
            label = await _tag(client, "spike", status=False, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            next(t for t in state._tags if t["id"] == label)["status"] = True
            slot = _slot(state, "s1", [label])
            status, _ = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200
        assert slot.tags == [label, review]

    @pytest.mark.asyncio
    async def test_a_rowless_vocabulary_state_is_refused(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            state._tags.append({"id": "planted01", "name": "Planted", "status": True})
            slot = _slot(state, "s1", ["planted01"])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 403 and body["code"] == "status_identity_unprotected"
        assert slot.tags == ["planted01"]

    @pytest.mark.asyncio
    async def test_a_recreated_slot_is_not_moved(self, state) -> None:
        """``expected_created`` pins the move to the slot generation the tool resolved."""
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            slot = _slot(state, "s1", [])
            status, body = await _drop(
                client, "s1", {"column_id": col, "expected_created": "an-older-generation"}, _AGENT
            )
        assert status == 200 and (body["ok"], body["code"]) == (False, "session_gone")
        assert slot.tags == []

    @pytest.mark.asyncio
    async def test_the_browser_drop_is_not_held_to_the_agent_policy(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="none")
            done = await _tag(client, "Done", status=True, agent="none")
            col = await _column(client, "Done", [done])
            slot = _slot(state, "s1", [todo])
            status, body = await _drop(client, "s1", {"column_id": col})
        assert status == 200 and body["ok"] is True
        assert slot.tags == [done]


class TestColumnReference:
    @pytest.mark.asyncio
    async def test_an_unknown_column_id_is_not_found(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            _slot(state, "s1", [])
            status, body = await _drop(client, "s1", {"column_id": "nowhere"}, _AGENT)
        assert status == 404 and body["code"] == "column_not_found"

    @pytest.mark.asyncio
    async def test_a_column_name_is_not_resolved_by_the_route(self, state) -> None:
        # The tool resolves names before posting; the route takes an id only.
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            await _column(client, "Review", [review])
            slot = _slot(state, "s1", [])
            status, body = await _drop(client, "s1", {"column": "Review"}, _AGENT)
        assert status == 404 and body["code"] == "column_not_found"
        assert slot.tags == []

    @pytest.mark.asyncio
    async def test_a_filter_only_column_reports_why_it_did_nothing(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            label = await _tag(client, "spike", status=False, agent="add-remove")
            col = await _column(client, "Spikes", [label])
            slot = _slot(state, "s1", [])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200
        assert (body["ok"], body["code"]) == (False, "not_status_lane")
        assert slot.tags == []


class TestOwnershipFences:
    @pytest.mark.asyncio
    async def test_a_live_alias_of_the_transcript_gets_the_new_tags(self, state) -> None:
        # Two live tabs on one transcript: without the mirror the alias would
        # flush its old tags over the drop and the card would move back.
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            target = _slot(state, "s1", [todo])
            target.linked_session_key = "taskrunner:t1:chat:shared"
            alias = _slot(state, "s2", [todo])
            alias.linked_session_key = "taskrunner:t1:chat:shared"
            other = _slot(state, "s3", [todo])
            status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200 and body["ok"] is True
        assert target.tags == [review]
        assert alias.tags == [review] and alias._dirty is True
        assert alias.tags_revision == target.tags_revision
        assert other.tags == [todo]

    @pytest.mark.asyncio
    async def test_a_tag_edit_also_reaches_a_live_alias_of_the_transcript(self, state) -> None:
        # PUT /tags (chat_tag_assign and the browser tag editor) shares the
        # same mirror, so the alias cannot flush the old tags back either.
        from kiro_crew.dashboard.chat_tags import api_chat_slot_tags

        app = _app(state)
        app.router.add_put("/api/chat/slots/{slot}/tags", api_chat_slot_tags)
        async with TestClient(TestServer(app)) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            label = await _tag(client, "spike", status=False, agent="add-remove")
            target = _slot(state, "s1", [todo])
            target.linked_session_key = "taskrunner:t1:chat:shared"
            alias = _slot(state, "s2", [todo])
            alias.linked_session_key = "taskrunner:t1:chat:shared"
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", return_value=True):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [todo, label]})
            assert resp.status == 200, await resp.text()
        assert alias.tags == [todo, label] and alias._dirty is True
        assert alias.tags_revision == target.tags_revision

    @pytest.mark.asyncio
    async def test_a_drop_with_a_live_alias_saves_again_after_the_mirror(self, state) -> None:
        # An alias flush queued before the mirror can write the old tags after
        # the first save; the second pinned save puts the drop back on disk.
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            target = _slot(state, "s1", [todo])
            target.linked_session_key = "taskrunner:t1:chat:shared"
            alias = _slot(state, "s2", [todo])
            alias.linked_session_key = "taskrunner:t1:chat:shared"
            with patch(
                "kiro_crew.dashboard.chat_tags.save_slot_off_loop", return_value=True
            ) as save:
                resp = await client.post(
                    "/api/chat/slots/s1/drop", json={"column_id": col}, headers=_AGENT
                )
            assert resp.status == 200 and (await resp.json())["ok"] is True
        assert save.await_count == 2
        confirm = save.await_args_list[1]
        assert confirm.args[1] is target
        assert confirm.kwargs["best_effort"] is False
        assert confirm.kwargs["expected_history_key"]
        assert alias.tags == [review]

    @pytest.mark.asyncio
    async def test_a_drop_with_no_alias_saves_once(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            _slot(state, "s1", [])
            with patch(
                "kiro_crew.dashboard.chat_tags.save_slot_off_loop", return_value=True
            ) as save:
                resp = await client.post(
                    "/api/chat/slots/s1/drop", json={"column_id": col}, headers=_AGENT
                )
            assert resp.status == 200
        assert save.await_count == 1

    @pytest.mark.asyncio
    async def test_a_refused_confirm_save_rejects_the_drop_and_undoes_the_mirror(
        self, state
    ) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            target = _slot(state, "s1", [todo])
            target.linked_session_key = "taskrunner:t1:chat:shared"
            alias = _slot(state, "s2", [todo])
            alias.linked_session_key = "taskrunner:t1:chat:shared"
            alias_revision = alias.tags_revision
            with patch(
                "kiro_crew.dashboard.chat_tags.save_slot_off_loop", side_effect=[True, False]
            ):
                resp = await client.post(
                    "/api/chat/slots/s1/drop", json={"column_id": col}, headers=_AGENT
                )
            body = await resp.json()
        assert resp.status == 200
        assert (body["ok"], body["code"]) == (False, "session_gone")
        assert target.tags == [todo] and target._dirty is True
        assert alias.tags == [todo] and alias.tags_revision == alias_revision
        assert alias._dirty is True

    @pytest.mark.asyncio
    async def test_a_failed_confirm_save_rejects_a_tag_edit_and_undoes_the_mirror(
        self, state
    ) -> None:
        from kiro_crew.dashboard.chat_tags import api_chat_slot_tags

        app = _app(state)
        app.router.add_put("/api/chat/slots/{slot}/tags", api_chat_slot_tags)
        async with TestClient(TestServer(app)) as client:
            todo = await _tag(client, "Todo", status=True, agent="add-remove")
            label = await _tag(client, "spike", status=False, agent="add-remove")
            target = _slot(state, "s1", [todo])
            target.linked_session_key = "taskrunner:t1:chat:shared"
            alias = _slot(state, "s2", [todo])
            alias.linked_session_key = "taskrunner:t1:chat:shared"
            with patch(
                "kiro_crew.dashboard.chat_tags.save_slot_off_loop",
                side_effect=[True, OSError("disk full")],
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [todo, label]})
            body = await resp.json()
        assert resp.status == 500 and body["code"] == "persist_failed"
        assert target.tags == [todo] and body["tags"] == [todo]
        assert alias.tags == [todo] and alias._dirty is True

    @pytest.mark.asyncio
    async def test_a_caller_whose_session_closed_is_refused(self, state) -> None:
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            target = _slot(state, "chat-2-200", [])
            status, body = await _drop(
                client,
                "chat-2-200",
                {"column_id": col},
                {**_AGENT, "X-Session-Key": "dashboard:chat-gone-1"},
            )
        assert status == 403 and body["code"] == "caller_unattributable"
        assert target.tags == []

    @pytest.mark.asyncio
    async def test_an_app_slot_linked_to_a_foreign_transcript_is_refused(self, state) -> None:
        async with TestClient(TestServer(_app(state, declared_app="radar"))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            _slot(state, "chat-1-100", [], app="radar")
            theirs = _slot(state, "chat-3-300", [], app="other")
            theirs.linked_session_key = "taskrunner:t1:chat:abc"
            target = _slot(state, "chat-2-200", [], app="radar")
            target.linked_session_key = "taskrunner:t1:chat:abc"
            status, body = await _drop(
                client,
                "chat-2-200",
                {"column_id": col},
                {**_AGENT, "X-Session-Key": "dashboard:chat-1-100"},
            )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.tags == []

    @pytest.mark.asyncio
    async def test_a_slot_rebound_while_waiting_on_the_lock_is_not_moved(self, state) -> None:
        import contextlib

        from kiro_crew.dashboard import chat_tags

        real_lock = chat_tags._tags_write_lock
        async with TestClient(TestServer(_app(state))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            target = _slot(state, "s1", [])

            @contextlib.asynccontextmanager
            async def _rebinding_lock(st):
                # Another writer rebinds the slot's transcript while this
                # request waits on the lock.
                target.linked_session_key = "taskrunner:t9:chat:other"
                async with real_lock(st):
                    yield

            with patch.object(chat_tags, "_tags_write_lock", _rebinding_lock):
                status, body = await _drop(client, "s1", {"column_id": col}, _AGENT)
        assert status == 200 and (body["ok"], body["code"]) == (False, "session_gone")
        assert target.tags == []

    @pytest.mark.asyncio
    async def test_an_app_cannot_move_the_persons_session(self, state) -> None:
        async with TestClient(TestServer(_app(state, declared_app="radar"))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            _slot(state, "chat-1-100", [], app="radar")
            target = _slot(state, "chat-2-200", [])
            status, body = await _drop(
                client,
                "chat-2-200",
                {"column_id": col},
                {**_AGENT, "X-Session-Key": "dashboard:chat-1-100"},
            )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.tags == []

    @pytest.mark.asyncio
    async def test_an_app_can_move_its_own_session(self, state) -> None:
        async with TestClient(TestServer(_app(state, declared_app="radar"))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            _slot(state, "chat-1-100", [], app="radar")
            target = _slot(state, "chat-2-200", [], app="radar")
            status, _ = await _drop(
                client,
                "chat-2-200",
                {"column_id": col},
                {**_AGENT, "X-Session-Key": "dashboard:chat-1-100"},
            )
        assert status == 200
        assert target.tags == [review]

    @pytest.mark.asyncio
    async def test_a_member_cannot_move_a_session_it_does_not_own(self, state, monkeypatch) -> None:
        monkeypatch.setattr(sc, "member_owns_slot", lambda state, slot, key: False)
        member = "member:member-kirocrew-conductor-deadbeef"
        async with TestClient(TestServer(_app(state, member_principal=member))) as client:
            review = await _tag(client, "Review", status=True, agent="add-remove")
            col = await _column(client, "Review", [review])
            _slot(state, "member-conductor", [])
            target = _slot(state, "chat-2-200", [])
            status, body = await _drop(
                client,
                "chat-2-200",
                {"column_id": col},
                {**_AGENT, "X-Session-Key": "dashboard:member-conductor"},
            )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.tags == []


# ── The MCP tool ──

_SLOTS = [
    {"key": "chat-1-100", "title": "Caller"},
    {"key": "chat-3-300", "title": "Worker"},
]
_TAGS = [{"id": "t-review", "name": "Review"}, {"id": "t-spike", "name": "spike"}]
_COLUMNS = [{"id": "c-review", "name": "Review"}, {"id": "c-spike", "name": "Spikes"}]


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    if path == "/api/chat/tags":
        return [dict(t) for t in _TAGS]
    if path == "/api/chat/tag-columns":
        return [dict(c) for c in _COLUMNS]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture
def verified_caller() -> Any:
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict", return_value="dashboard:chat-1-100"
    ):
        yield


def _move(post_result: dict, target: str = "Worker", column: str = "Review") -> tuple[str, Any]:
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
        patch("kiro_crew.mcp_dashboard._post", return_value=post_result) as mock_post,
    ):
        out = _call_tool_inner("session_move_to_column", {"target": target, "column": column})
    return out, mock_post


def test_the_tool_is_advertised_with_both_arguments_required() -> None:
    tool = next(t for t in _list_tools() if t["name"] == "session_move_to_column")
    assert set(tool["inputSchema"]["required"]) == {"target", "column"}


@pytest.mark.usefixtures("verified_caller")
class TestTool:
    def test_posts_the_column_reference_with_the_verified_caller(self) -> None:
        out, mock_post = _move({"ok": True, "tags": ["t-spike", "t-review"]})
        path, body = mock_post.call_args.args
        assert path == "/api/chat/slots/chat-3-300/drop"
        assert body == {"column_id": "c-review"}
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert (
            out == "Moved session `chat-3-300` into column `Review`. Tags now: `spike`, `Review`."
        )

    def test_the_resolved_slot_generation_rides_on_the_write(self) -> None:
        rows = [
            {"key": "chat-1-100", "title": "Caller"},
            {"key": "chat-3-300", "title": "Worker", "created": "c-1"},
        ]
        with (
            patch(
                "kiro_crew.mcp_dashboard._get",
                side_effect=lambda path: rows if path == "/api/chat/slots" else _rows(path),
            ),
            patch("kiro_crew.mcp_dashboard._post", return_value={"ok": True}) as mock_post,
        ):
            _call_tool_inner("session_move_to_column", {"target": "Worker", "column": "Review"})
        assert mock_post.call_args.args[1] == {"column_id": "c-review", "expected_created": "c-1"}

    @pytest.mark.parametrize(
        "code, needle",
        [("not_status_lane", "exactly one status tag"), ("state_lane", "live-state lane")],
    )
    def test_a_column_that_cannot_take_a_card_is_a_result_not_an_error(
        self, code: str, needle: str
    ) -> None:
        out, _ = _move({"ok": False, "reason": "x", "code": code, "tags": []})
        assert out.startswith("Not moved:") and needle in out
        assert "retrying will not change it" in out

    def test_a_policy_refusal_names_the_code_and_tag(self) -> None:
        out, _ = _move({"error": "tag_policy_denied:t-done", "code": "tag_policy_denied"})
        assert out.startswith("Error: tag_policy_denied:t-done")

    def test_a_column_name_resolves_case_insensitively(self) -> None:
        _, mock_post = _move({"ok": True, "tags": []}, column="spikes")
        assert mock_post.call_args.args[1] == {"column_id": "c-spike"}

    def test_a_shared_column_name_asks_for_the_id_before_any_write(self) -> None:
        cols = [{"id": "c1", "name": "Review"}, {"id": "c2", "name": "review"}]
        with (
            patch(
                "kiro_crew.mcp_dashboard._get",
                side_effect=lambda path: cols if path == "/api/chat/tag-columns" else _rows(path),
            ),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner(
                "session_move_to_column", {"target": "Worker", "column": "Review"}
            )
        assert out.startswith("Error:") and "c1, c2" in out and "column id" in out
        mock_post.assert_not_called()

    def test_an_unknown_column_is_refused_before_any_write(self) -> None:
        out, mock_post = _move({"ok": True}, column="Nowhere")
        assert out.startswith("Error:") and "chat_tag_column_list" in out
        mock_post.assert_not_called()

    def test_a_column_name_longer_than_any_column_reference_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            pytest.raises(ValidationError),
        ):
            _call_tool_inner("session_move_to_column", {"target": "Worker", "column": "x" * 65})
        mock_post.assert_not_called()

    def test_an_unknown_session_is_refused_before_any_write(self) -> None:
        out, mock_post = _move({"ok": True}, target="Nope")
        assert out.startswith("Error:")
        mock_post.assert_not_called()

    def test_a_blank_column_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            pytest.raises(ValidationError),
        ):
            _call_tool_inner("session_move_to_column", {"target": "Worker", "column": "   "})
        mock_post.assert_not_called()


def test_an_unverifiable_caller_is_refused() -> None:
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
        patch("kiro_crew.mcp_dashboard._post") as mock_post,
    ):
        out = _call_tool_inner("session_move_to_column", {"target": "Worker", "column": "Review"})
    assert out.startswith("Error:") and "cannot verify" in out
    mock_post.assert_not_called()


def test_a_channel_agent_is_refused_at_dispatch() -> None:
    sel_obj = MagicMock()
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="channel:slack:C1:1"),
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows) as mock_get,
        patch("kiro_crew.mcp_dashboard._post") as mock_post,
        patch("kiro_crew.mcp_dashboard.sel", return_value=sel_obj),
    ):
        out = _call_tool_inner("session_move_to_column", {"target": "Worker", "column": "Review"})
    assert out.startswith("Error:") and "channel agents" in out
    mock_get.assert_not_called()
    mock_post.assert_not_called()
    assert sel_obj.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_blocked_tool"
