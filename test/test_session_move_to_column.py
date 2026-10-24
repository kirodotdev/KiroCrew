"""``session_move_to_column`` and the caller fences it needs on the board drop.

Two halves. The ROUTE half drives the real ``POST /api/chat/slots/{slot}/drop``
handler with the real grants store: an agent drop is held to the same verdict
``PUT /tags`` gives that caller for the same diff, the browser drag is not, and
the app / crew-member ownership fences and the slot-generation pin hold. The
TOOL half drives the table row against an in-memory dashboard: what it sends,
how it resolves the column, and how it renders each route answer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard import chat_tag_grants
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_INTERNAL = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
_SAVE = "kiro_crew.dashboard.chat_tags.save_slot_off_loop"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, _floor_monkeypatch):
    # The private patch stack (D11): a test calling monkeypatch.undo() cannot
    # lift the config-dir and signing-key isolation.
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard import token_secret

    _floor_monkeypatch.setattr(token_secret, "_get_secret", lambda: b"test-signing-key")


def _tag(tid: str, *, status: bool) -> dict[str, Any]:
    return {"id": tid, "name": tid.title(), "color": "#000000", "order": 0, "status": status}


def _board(tmp_path, *, todo="add-remove", done="add-remove", slot_tags=("todo", "label")):
    """A state with Todo/Done status tags, a plain label, and one column each.

    ``todo`` / ``done`` are the grant policies minted for the two states, or
    ``None`` for no row. The slot ``s1`` starts in Todo with the label.
    """
    state = _make_state(tmp_path)
    state._tags = [
        _tag("todo", status=True),
        _tag("done", status=True),
        _tag("label", status=False),
    ]
    state._tag_boards = [
        {"id": "col-todo", "name": "Todo", "tag_ids": ["todo"], "mode": "any"},
        {"id": "col-done", "name": "Done", "tag_ids": ["done"], "mode": "any"},
        {"id": "col-filter", "name": "Labelled", "tag_ids": ["label"], "mode": "any"},
        {"id": "col-live", "name": "Live", "source": "state", "tag_ids": []},
    ]
    app = _make_tags_app(state)
    for tid, policy in (("todo", todo), ("done", done)):
        if policy is not None:
            chat_tag_grants.mint_grant(tid, policy=policy, status=True)
    chat_tag_grants.refresh_cache()
    slot = _ChatSlot("s1")
    slot.tags = list(slot_tags)
    state._slots["s1"] = slot
    return state, app, slot


def _mark(app: web.Application, markers: dict[str, Any]) -> None:
    @web.middleware
    async def _set(request, handler):
        for k, v in markers.items():
            request[k] = v
        return await handler(request)

    app.middlewares.append(_set)


async def _drop(app, body, headers=None):
    async with TestClient(TestServer(app)) as client:
        with patch(_SAVE, return_value=True) as save:
            resp = await client.post("/api/chat/slots/s1/drop", json=body, headers=headers or {})
            return resp.status, await resp.json(), save.call_count


class TestDropRoute:
    @pytest.mark.asyncio
    async def test_an_agent_moves_between_open_states_and_keeps_labels(self, tmp_path):
        _, app, slot = _board(tmp_path)
        status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 200 and body["ok"] is True
        assert slot.tags == ["label", "done"]
        assert saves == 1

    @pytest.mark.asyncio
    async def test_an_agent_cannot_enter_a_state_the_person_reserved(self, tmp_path):
        _, app, slot = _board(tmp_path, done="none")
        status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 403
        assert body == {"error": "tag_policy_denied:done", "code": "tag_policy_denied"}
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_an_agent_cannot_strip_an_add_only_state(self, tmp_path):
        _, app, slot = _board(tmp_path, todo="add-only")
        status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 403 and body["code"] == "tag_policy_denied"
        assert body["error"] == "tag_policy_denied:todo"
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_an_agent_cannot_enter_a_status_with_no_protected_row(self, tmp_path):
        _, app, slot = _board(tmp_path, done=None)
        status, body, _ = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 403 and body["code"] == "status_identity_unprotected"
        assert slot.tags == ["todo", "label"]

    @pytest.mark.asyncio
    async def test_an_agent_cannot_enter_an_add_only_state(self, tmp_path):
        """The ``chat_tag`` ``set_state`` rule: an agent enters only a state it
        could also leave, so a card is never parked where no agent can move it."""
        _, app, slot = _board(tmp_path, done="add-only")
        status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 403
        assert body == {"error": "tag_policy_denied:done", "code": "tag_policy_denied"}
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_an_agent_cannot_enter_a_row_the_store_says_is_not_a_status(self, tmp_path):
        """Status comes from the protected row, not the agent-writable vocabulary:
        a tag the vocabulary calls a status but whose row says label is refused."""
        _, app, slot = _board(tmp_path, done=None)
        chat_tag_grants.mint_grant("done", policy="add-remove", status=False)
        chat_tag_grants.refresh_cache()
        status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 403
        assert body == {"error": "not_a_status_tag:done", "code": "not_a_status_tag"}
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_the_drop_refuses_whatever_put_tags_refuses_for_the_same_diff(self, tmp_path):
        """A drop is the replace an agent could send to PUT /tags, so it may never
        admit a diff PUT /tags refuses. It is stricter on one point only: the
        ``set_state`` rule on the target, which refuses an ``add-only`` state."""
        for done in ("add-remove", "add-only", "none"):
            _, app, _slot = _board(tmp_path, done=done)
            d_status, d_body, _ = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
            _, app, _slot = _board(tmp_path, done=done)
            async with TestClient(TestServer(app)) as client:
                with patch(_SAVE, return_value=True):
                    resp = await client.put(
                        "/api/chat/slots/s1/tags",
                        json={"tags": ["label", "done"]},
                        headers=_INTERNAL,
                    )
                    p_status, p_body = resp.status, await resp.json()
            if p_status != 200:
                assert (d_status, d_body.get("code")) == (p_status, p_body.get("code")), done
            expected = 403 if done == "add-only" else p_status
            assert d_status == expected, done

    @pytest.mark.asyncio
    async def test_the_browser_drag_is_not_held_to_the_agent_policy(self, tmp_path):
        _, app, slot = _board(tmp_path, todo="none", done="none")
        status, body, _ = await _drop(app, {"column_id": "col-done"})
        assert status == 200 and body["ok"] is True
        assert slot.tags == ["label", "done"]

    @pytest.mark.asyncio
    async def test_non_person_signal_enumeration_each_leg_gates_the_drop(self, tmp_path):
        """The drop twin of PUT /tags' pin test: each non-person signal alone
        gates, on a slot the caller legitimately owns, and the person does not."""
        # App-claim leg, browser transport, the app's own slot.
        _, app, slot = _board(tmp_path, done="none")
        slot._app = "an-app"
        _mark(app, {"app": "an-app"})
        status, body, _ = await _drop(app, {"column_id": "col-done"})
        assert status == 403 and body["code"] == "tag_policy_denied"
        # Member leg, on a slot the member owns (its own session key).
        _, app, slot = _board(tmp_path, done="none")
        slot._created_by = ""
        _mark(app, {MEMBER_CHAT_PRINCIPAL_KEY: "member:store-x"})
        status, body, _ = await _drop(
            app, {"column_id": "col-done"}, {"X-Session-Key": effective_session_key(slot)}
        )
        assert status == 403 and body["code"] == "tag_policy_denied"

    @pytest.mark.asyncio
    async def test_an_app_cannot_move_the_persons_session(self, tmp_path):
        _, app, slot = _board(tmp_path)
        _mark(app, {"app": "an-app"})
        status, _, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 404 and saves == 0
        assert slot.tags == ["todo", "label"]

    @pytest.mark.asyncio
    async def test_an_app_moves_its_own_session(self, tmp_path):
        _, app, slot = _board(tmp_path)
        slot._app = "an-app"
        _mark(app, {"app": "an-app"})
        status, body, _ = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 200 and body["ok"] is True
        assert slot.tags == ["label", "done"]

    @pytest.mark.asyncio
    async def test_an_app_cannot_move_a_session_whose_transcript_another_app_owns(self, tmp_path):
        """The transcript fence, not the slot fence: the app owns the slot object,
        but the slot writes into a session another app's slot also writes."""
        state, app, slot = _board(tmp_path)
        slot._app = "an-app"
        other = _ChatSlot("s2")
        other._app = "other-app"
        state._slots["s2"] = other
        slot.linked_session_key = "dashboard:s2"
        _mark(app, {"app": "an-app"})
        status, _, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert status == 404 and saves == 0
        assert slot.tags == ["todo", "label"]

    @pytest.mark.asyncio
    async def test_transcript_ownership_is_asked_again_under_the_lock(self, tmp_path):
        """The second ``app_owns_transcript`` call: ownership that holds at the
        door but is gone once the lock is held writes nothing."""
        _, app, slot = _board(tmp_path)
        slot._app = "an-app"
        _mark(app, {"app": "an-app"})
        with patch(
            "kiro_crew.dashboard.chat_tags.app_owns_transcript", side_effect=[True, False]
        ) as owns:
            status, body, saves = await _drop(app, {"column_id": "col-done"}, _INTERNAL)
        assert owns.call_count == 2
        assert status == 200 and body["ok"] is False and body["code"] == "session_gone"
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_a_caller_whose_own_slot_is_gone_is_refused_like_put_tags(self, tmp_path):
        """A ``dashboard:`` key naming a popped slot cannot be attributed; read as
        the person it would get the person's authority. Same answer as PUT /tags."""
        _, app, slot = _board(tmp_path)
        gone = {"X-Session-Key": "dashboard:chat-gone-1"}
        status, body, saves = await _drop(app, {"column_id": "col-done"}, gone)
        async with TestClient(TestServer(app)) as client:
            with patch(_SAVE, return_value=True):
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": ["label", "done"]}, headers=gone
                )
                p_status, p_body = resp.status, await resp.json()
        assert (status, body["code"]) == (403, "caller_unattributable")
        assert (p_status, p_body["code"]) == (status, body["code"])
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_a_stale_tags_revision_writes_nothing(self, tmp_path):
        _, app, slot = _board(tmp_path)
        seen = slot.tags_revision
        slot.tags = ["todo", "label", "extra"]
        slot.tags_revision = "after-the-read"
        status, body, saves = await _drop(
            app, {"column_id": "col-done", "base_tags_revision": seen}, _INTERNAL
        )
        assert status == 409 and body["code"] == "stale_base"
        assert body["tags_revision"] == "after-the-read"
        assert slot.tags == ["todo", "label", "extra"] and saves == 0

    @pytest.mark.asyncio
    async def test_the_current_tags_revision_moves_the_card(self, tmp_path):
        _, app, slot = _board(tmp_path)
        status, body, _ = await _drop(
            app, {"column_id": "col-done", "base_tags_revision": slot.tags_revision}, _INTERNAL
        )
        assert status == 200 and body["ok"] is True
        assert slot.tags == ["label", "done"]

    @pytest.mark.asyncio
    async def test_a_non_string_tags_revision_is_refused(self, tmp_path):
        _, app, slot = _board(tmp_path)
        status, body, saves = await _drop(
            app, {"column_id": "col-done", "base_tags_revision": 7}, _INTERNAL
        )
        assert status == 400 and body["code"] == "base_not_string"
        assert saves == 0

    @pytest.mark.asyncio
    async def test_a_member_cannot_move_a_session_it_does_not_own(self, tmp_path):
        _, app, slot = _board(tmp_path)
        _mark(app, {MEMBER_CHAT_PRINCIPAL_KEY: "member:store-x"})
        status, _, saves = await _drop(
            app, {"column_id": "col-done"}, {"X-Session-Key": "dashboard:someone-else"}
        )
        # ``member_slot_write_refused`` answers 403 or 404 depending on why.
        assert status in (403, 404) and saves == 0
        assert slot.tags == ["todo", "label"]

    @pytest.mark.asyncio
    async def test_a_stale_slot_generation_writes_nothing(self, tmp_path):
        _, app, slot = _board(tmp_path)
        slot.created_at = "2026-10-12T00:00:00Z"
        status, body, saves = await _drop(
            app, {"column_id": "col-done", "expected_created": "2026-01-01T00:00:00Z"}, _INTERNAL
        )
        assert status == 200 and body["ok"] is False and body["code"] == "session_gone"
        assert slot.tags == ["todo", "label"] and saves == 0

    @pytest.mark.asyncio
    async def test_the_matching_slot_generation_moves_the_card(self, tmp_path):
        _, app, slot = _board(tmp_path)
        slot.created_at = "2026-10-12T00:00:00Z"
        status, body, _ = await _drop(
            app, {"column_id": "col-done", "expected_created": "2026-10-12T00:00:00Z"}, _INTERNAL
        )
        assert status == 200 and body["ok"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "column,code", [("col-filter", "not_status_lane"), ("col-live", "state_lane")]
    )
    async def test_a_board_rejection_carries_a_code(self, tmp_path, column, code):
        _, app, slot = _board(tmp_path)
        status, body, saves = await _drop(app, {"column_id": column}, _INTERNAL)
        assert status == 200 and body["ok"] is False and body["code"] == code
        assert slot.tags == ["todo", "label"] and saves == 0


# ── The tool ──

_SLOTS = [
    {"key": "chat-1-100", "title": "Caller", "created": "c1"},
    {"key": "chat-3-300", "title": "Worker", "created": "c3", "tags_revision": "r3"},
]
_COLUMNS = [
    {"id": "col-todo", "name": "Todo", "tag_ids": ["todo"]},
    {"id": "col-done", "name": "Done", "tag_ids": ["done"]},
]
_TAGS = [{"id": "done", "name": "Done"}, {"id": "label", "name": "spike"}]
_CALLER = Caller.strict("dashboard:chat-1-100")
_DROP = "POST /api/chat/slots/{slot}/drop"


def _routes(columns: Any = None, reply: Any = None) -> dict[str, Any]:
    return {
        "GET /api/chat/tag-columns": _COLUMNS if columns is None else columns,
        "GET /api/chat/tags": _TAGS,
        "GET /api/chat/slots": _SLOTS,
        _DROP: {"ok": True, "tags": ["label", "done"]} if reply is None else reply,
    }


def _move(
    args: dict[str, Any], routes: dict[str, Any] | None = None, caller: Caller = _CALLER
) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient(_routes() if routes is None else routes)
    return TABLE.call("session_move_to_column", args, ToolContext(dash, caller)), dash


def _drops(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return dash.sent(_DROP)


def test_the_tool_is_advertised_with_both_arguments_required() -> None:
    tool = next(t for t in _list_tools() if t["name"] == "session_move_to_column")
    assert set(tool["inputSchema"]["required"]) == {"session", "column"}


class TestTool:
    def test_posts_the_column_id_and_generation_with_the_verified_caller(self) -> None:
        out, dash = _move({"session": "Worker", "column": "Done"})
        (drop,) = _drops(dash)
        assert drop.path == "/api/chat/slots/chat-3-300/drop"
        assert drop.body == {
            "column_id": "col-done",
            "expected_created": "c3",
            "base_tags_revision": "r3",
        }
        assert drop.session_key == "dashboard:chat-1-100"
        assert out == "Moved session `chat-3-300` into column `Done`. Tags now: `spike`, `Done`."

    def test_a_stale_tags_revision_reads_as_call_again(self) -> None:
        reply = {"error": "tags changed since the session was read", "code": "stale_base"}
        out, _ = _move({"session": "chat-3-300", "column": "Done"}, _routes(reply=reply))
        assert out.startswith("Error: the tags on `chat-3-300` changed")
        assert "call session_move_to_column again" in out

    def test_a_column_name_resolves_case_insensitively(self) -> None:
        _, dash = _move({"session": "chat-3-300", "column": "done"})
        assert _drops(dash)[0].body["column_id"] == "col-done"

    def test_a_shared_column_name_is_refused_before_any_write(self) -> None:
        twins = _COLUMNS + [{"id": "col-done2", "name": "Done", "tag_ids": ["done"]}]
        out, dash = _move({"session": "chat-3-300", "column": "Done"}, _routes(columns=twins))
        assert out.startswith("Error: 2 columns share the name")
        assert _drops(dash) == []

    @pytest.mark.parametrize(
        "session, column, error_prefix",
        [
            pytest.param(
                "chat-3-300",
                "Nope",
                "Error: no board column matches",
                id="an_unknown_column_is_refused_before_any_write",
            ),
            pytest.param(
                "ghost", "Done", "Error:", id="an_unknown_session_is_refused_before_any_write"
            ),
        ],
    )
    def test_an_unknown_target_is_refused_before_any_write(
        self, session, column, error_prefix
    ) -> None:
        out, dash = _move({"session": session, "column": column})
        assert out.startswith(error_prefix)
        assert _drops(dash) == []

    def test_an_over_long_column_is_refused_by_the_schema(self) -> None:
        out, dash = _move({"session": "chat-3-300", "column": "x" * 65})
        assert out.startswith("Error")
        assert dash.requests == []

    @pytest.mark.parametrize("code", ["not_status_lane", "state_lane"])
    def test_a_board_rejection_reads_as_not_moved(self, code: str) -> None:
        reply = {"ok": False, "reason": "r", "code": code, "tags": ["todo"]}
        out, _ = _move({"session": "chat-3-300", "column": "Done"}, _routes(reply=reply))
        assert out.startswith("Not moved: session `chat-3-300` stays put")

    def test_a_replaced_session_reads_as_an_error(self) -> None:
        reply = {"ok": False, "reason": "gone", "code": "session_gone", "tags": []}
        out, _ = _move({"session": "chat-3-300", "column": "Done"}, _routes(reply=reply))
        assert out.startswith("Error: session `chat-3-300` closed or was replaced")

    def test_a_policy_refusal_is_surfaced(self) -> None:
        reply = {"error": "tag_policy_denied:done", "code": "tag_policy_denied"}
        out, _ = _move({"session": "chat-3-300", "column": "Done"}, _routes(reply=reply))
        assert out == "Error: tag_policy_denied:done"

    def test_an_unverifiable_caller_is_refused_before_any_read(self) -> None:
        out, dash = _move({"session": "chat-3-300", "column": "Done"}, caller=Caller.unverified())
        assert out.startswith("Error: this session cannot be identified")
        assert dash.requests == []

    def test_a_channel_caller_is_refused_before_any_read(self) -> None:
        out, dash = _move(
            {"session": "chat-3-300", "column": "Done"}, caller=Caller.strict("channel:slack:T1")
        )
        assert "not available to channel agents" in out
        assert dash.requests == []
