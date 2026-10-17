"""The dashboard's view of a code channel: the ``code_channel`` slot field.

A bound session's slot row carries ``code_channel`` in a person's views of the
slot list; an unbound one, every one while the gate is shut, and every row of
the bare list (SSE, app tokens) carry no such key. Within a person's views the
field rides the row, so the member-principal filter that decides who sees a
session decides who sees its channel.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.slack.code_channels import dashboard_view
from kiro_crew.slack.code_channels.dashboard_view import (
    binding_status,
    code_channel_view,
    code_channel_views,
    slack_channel_url,
    with_code_channels,
)


def _binding(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "channel_id": "C0CODE1",
        "team_id": "T0TEAM1",
        "session_key": "chat-1-1",
        "anchor_ts": "200.000001",
        "channel_name": "fix-login",
        "archived_at": 0.0,
        "wrapped_up_at": 0.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"on": True, "reads": 0}

    def _open() -> bool:
        state["reads"] += 1
        return state["on"]

    monkeypatch.setattr(dashboard_view, "_gate_open", _open)
    return state


class TestUrls:
    def test_well_formed_ids_give_slack_s_redirect_link(self) -> None:
        url = slack_channel_url("T0TEAM1", "C0CODE1")
        assert url == "https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1"

    def test_enterprise_and_private_ids_are_accepted(self) -> None:
        assert slack_channel_url("E0ORG1", "G0PRIV1").endswith("channel=G0PRIV1&team=E0ORG1")

    @pytest.mark.parametrize(
        "team,channel",
        [
            ("", "C0CODE1"),
            ("T0TEAM1", ""),
            ("T0TEAM1", "C0CODE1/../x"),
            ("javascript:alert(1)", "C0CODE1"),
            ("T0TEAM1", 'C0" onmouseover="x'),
            ("t0team1", "c0code1"),
            (None, None),
        ],
    )
    def test_malformed_ids_give_no_link(self, team: Any, channel: Any) -> None:
        assert slack_channel_url(team, channel) == ""


class TestStatus:
    def test_active_by_default(self) -> None:
        assert binding_status(_binding()) == "active"

    def test_wrapped_up(self) -> None:
        assert binding_status(_binding(wrapped_up_at=1.0)) == "wrapped_up"

    def test_archived_outranks_wrapped_up(self) -> None:
        assert binding_status(_binding(wrapped_up_at=1.0, archived_at=2.0)) == "archived"

    def test_a_record_without_the_fields_reads_active(self) -> None:
        bare = SimpleNamespace(channel_id="C0CODE1", team_id="T0TEAM1", session_key="chat-1-1")
        assert binding_status(bare) == "active"

    @pytest.mark.parametrize("value", [True, "1", None, -5, 0])
    def test_only_a_positive_number_is_a_stamp(self, value: Any) -> None:
        assert binding_status(_binding(archived_at=value)) == "active"


class TestView:
    def test_shape(self) -> None:
        assert code_channel_view(_binding()) == {
            "channel_name": "fix-login",
            "status": "active",
            "web_url": "https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1",
        }

    def test_missing_channel_name_reads_empty(self) -> None:
        bare = SimpleNamespace(channel_id="C0CODE1", team_id="T0TEAM1", session_key="k")
        assert code_channel_view(bare)["channel_name"] == ""
        assert code_channel_view(_binding(channel_name=None))["channel_name"] == ""

    def test_name_drops_a_leading_hash_and_is_capped(self) -> None:
        assert code_channel_view(_binding(channel_name=" #fix "))["channel_name"] == "fix"
        assert len(code_channel_view(_binding(channel_name="x" * 300))["channel_name"]) == 80


class TestViews:
    def test_keyed_by_session(self, gate: dict[str, Any]) -> None:
        views = code_channel_views([_binding(), _binding(channel_id="C0CODE2", session_key="k2")])
        assert set(views) == {"chat-1-1", "k2"}

    def test_gate_shut_gives_nothing(self, gate: dict[str, Any]) -> None:
        gate["on"] = False
        assert code_channel_views([_binding()]) == {}

    def test_gate_shut_never_reads_the_store(
        self, gate: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.code_channels import store as store_mod

        gate["on"] = False
        monkeypatch.setattr(store_mod, "store", lambda: pytest.fail("store read"))
        assert code_channel_views() == {}

    def test_an_unread_store_yields_nothing_and_touches_no_disk(
        self, gate: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

        cold = CodeChannelStore(tmp_path / "slack-code-channels.json")
        monkeypatch.setattr(cold, "load", lambda: pytest.fail("loaded on the caller"))
        set_store(cold)
        try:
            assert code_channel_views() == {}
            assert not cold.loaded
        finally:
            set_store(None)

    @pytest.mark.asyncio
    async def test_an_unread_store_is_warmed_off_the_loop_then_re_pushed(
        self, gate: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store
        from kiro_crew.slack.handler_runtime import access

        warm = CodeChannelStore(tmp_path / "slack-code-channels.json")
        warm.put_binding(_store_binding("chat-1-1"))
        cold = CodeChannelStore(tmp_path / "slack-code-channels.json")
        pushes: list[int] = []
        state = SimpleNamespace(push_slots_update=lambda: pushes.append(1))
        monkeypatch.setattr(access, "get_dashboard_state", lambda: state)
        set_store(cold)
        try:
            assert code_channel_views() == {}
            # Await the warm-up itself rather than polling for its effect.
            pending = list(dashboard_view._background)
            assert len(pending) == 1
            await asyncio.wait_for(asyncio.gather(*pending), timeout=30)
            assert pushes == [1] and cold.loaded
            assert set(code_channel_views()) == {"chat-1-1"}
        finally:
            set_store(None)

    def test_a_failure_costs_the_badge_not_the_caller(
        self, gate: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_b: Any) -> Any:
            raise RuntimeError("bad record")

        monkeypatch.setattr(dashboard_view, "code_channel_view", boom)
        assert code_channel_views([_binding()]) == {}


class TestWithCodeChannels:
    def test_only_bound_rows_gain_the_key_and_no_input_row_is_mutated(self) -> None:
        rows = [{"key": "chat-1-1"}, {"key": "chat-2-1"}]
        out = with_code_channels(rows, {"chat-1-1": {"status": "active"}})
        assert out[0]["code_channel"] == {"status": "active"}
        assert "code_channel" not in out[1] and out[1] is rows[1]
        assert rows == [{"key": "chat-1-1"}, {"key": "chat-2-1"}]

    def test_each_row_gets_its_own_copy(self) -> None:
        view = {"status": "active"}
        out = with_code_channels([{"key": "a"}], {"a": view})
        out[0]["code_channel"]["status"] = "mutated"
        assert view["status"] == "active"


# ── Through the real slot list ────────────────────────────────────────────


@pytest.fixture
def bound_state(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, gate: dict[str, Any]
) -> tuple[Any, Any, Any]:
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    bound = state.get_or_create_slot(None)
    plain = state.get_or_create_slot(None)
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    set_store(store)
    store.put_binding(_store_binding(bound.key))
    store.load()
    yield state, bound, plain
    set_store(None)


def _store_binding(session_key: str) -> Any:
    from kiro_crew.slack.code_channels.store import Binding

    return Binding(
        channel_id="C0CODE1",
        team_id="T0TEAM1",
        session_key=session_key,
        anchor_ts="200.000001",
        channel_name="fix-login",
    )


class TestStoredRecord:
    """The real Binding's fields, as the store and the lifecycle events set them."""

    def test_archive_stamp_reads_archived_on_the_row(
        self, bound_state: tuple[Any, Any, Any]
    ) -> None:
        from kiro_crew.slack.code_channels.store import store

        state, bound, _plain = bound_state
        store().update_binding("C0CODE1", archived_at=1.0, wrapped_up_at=1.0)
        row = _row(state.serialize_slots(dashboard_user=True), bound.key)
        assert row["code_channel"]["status"] == "archived"
        assert row["code_channel"]["channel_name"] == "fix-login"

    def test_wrap_up_stamp_reads_wrapped_up(self, bound_state: tuple[Any, Any, Any]) -> None:
        from kiro_crew.slack.code_channels.store import store

        state, bound, _plain = bound_state
        store().update_binding("C0CODE1", wrapped_up_at=5.0)
        row = _row(state.serialize_slots(dashboard_user=True), bound.key)
        assert row["code_channel"]["status"] == "wrapped_up"


def _row(rows: list[dict[str, Any]] | None, key: str) -> dict[str, Any]:
    return next(r for r in rows or () if r["key"] == key)


class TestSerializeSlots:
    @pytest.mark.parametrize(
        "kwargs", [{"dashboard_user": True}, {"include_check_status": True}], ids=["user", "owner"]
    )
    def test_a_person_s_list_carries_it_and_plain_rows_are_unchanged(
        self, bound_state: tuple[Any, Any, Any], kwargs: dict[str, bool]
    ) -> None:
        state, bound, plain = bound_state
        rows = state.serialize_slots(**kwargs)
        assert _row(rows, bound.key)["code_channel"]["channel_name"] == "fix-login"
        assert _row(rows, bound.key)["code_channel"]["web_url"].startswith(
            "https://slack.com/app_redirect?"
        )
        assert "code_channel" not in _row(rows, plain.key)

    def test_the_bare_list_never_carries_it(self, bound_state: tuple[Any, Any, Any]) -> None:
        state, _bound, _plain = bound_state
        assert all("code_channel" not in r for r in state.serialize_slots())

    def test_gate_shut_no_row_carries_it(
        self, bound_state: tuple[Any, Any, Any], gate: dict[str, Any]
    ) -> None:
        state, _bound, _plain = bound_state
        gate["on"] = False
        assert all("code_channel" not in r for r in state.serialize_slots(dashboard_user=True))

    def test_broadcast_views_person_frames_only_and_bare_rows_untouched(
        self, bound_state: tuple[Any, Any, Any]
    ) -> None:
        state, bound, _plain = bound_state
        bare, user, owner = state.serialize_slot_views(owner=True)
        assert "code_channel" in _row(user, bound.key)
        assert "code_channel" in _row(owner, bound.key)
        # SSE and app tokens read the bare list: the person views must not
        # have written into the row dicts they share with it.
        assert "code_channel" not in _row(bare, bound.key)


class TestViewerScoping:
    """Only a person who may see a session's row ever sees its channel."""

    @pytest.mark.asyncio
    async def test_member_principal_sees_no_other_session_s_channel(
        self, bound_state: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chat_test_helpers import _make_app

        from kiro_crew.dashboard import session_control
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        state, bound, plain = bound_state
        # The member owns the PLAIN session only.
        monkeypatch.setattr(
            session_control,
            "member_owns_slot",
            lambda _state, slot, _caller: slot is not None and slot.key == plain.key,
        )

        @web.middleware
        async def as_member(request: web.Request, handler: Any) -> Any:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = "member:someone"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, as_member)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots", headers={"X-Session-Key": "chat-x"})
            assert resp.status == 200
            rows = await resp.json()
        assert [r["key"] for r in rows] == [plain.key]
        assert "C0CODE1" not in str(rows)

    @pytest.mark.asyncio
    async def test_owner_sees_the_bound_session_s_channel(
        self, bound_state: tuple[Any, Any, Any]
    ) -> None:
        from chat_test_helpers import _make_app

        state, bound, _plain = bound_state
        async with TestClient(TestServer(_make_app(state))) as client:
            rows = await (await client.get("/api/chat/slots")).json()
        assert _row(rows, bound.key)["code_channel"]["status"] == "active"

    @pytest.mark.asyncio
    async def test_an_app_caller_never_sees_a_code_channel(
        self, bound_state: tuple[Any, Any, Any]
    ) -> None:
        from chat_test_helpers import _make_app

        state, _bound, _plain = bound_state

        @web.middleware
        async def as_app(request: web.Request, handler: Any) -> Any:
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            body = await resp.text()
        assert "code_channel" not in body and "C0CODE1" not in body

    def test_app_ws_rows_drop_it_even_on_a_row_the_app_may_see(
        self, bound_state: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import ws_event_scope

        state, bound, plain = bound_state
        monkeypatch.setattr(ws_event_scope, "_slot_visible", lambda *_a: True)
        monkeypatch.setattr(ws_event_scope, "_audit_allow", lambda *_a, **_k: None)
        monkeypatch.setattr(ws_event_scope, "_audit_deny", lambda *_a, **_k: None)
        # The WS app frame is filtered from the dashboard-user list.
        source = state.serialize_slots(dashboard_user=True)
        out = ws_event_scope.filter_slots_for_app(source, "some-app", frozenset(), state)
        assert {r["key"] for r in out} == {bound.key, plain.key}
        assert all("code_channel" not in r for r in out)
        assert "code_channel" in _row(source, bound.key)
