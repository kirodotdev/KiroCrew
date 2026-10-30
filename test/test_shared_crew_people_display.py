"""The People display route: any admitted person sets anyone's name and picture.

Covers the release gate (404 while off), equal rights (a teammate edits the
owner's row and a person who was never invited), the avatar checks reused from
invites, the ``person_display`` audit record (who changed whom, old and new
name), the ``shared_crew.people`` frame, and that a display record never
touches access.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.shared_crew import audit, routes_invites, routes_people
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Provenance, Transport
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BOB = tailnet_principal("bob@example.com", "bob-laptop", Provenance.TAILSCALE_LISTENER)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setattr(shared, "_is_restricted_session", lambda *_a: False)
    tailnet_live_allowlist.reset_for_tests()
    yield
    tailnet_live_allowlist.reset_for_tests()


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []
    monkeypatch.setattr(audit, "_default_writer", events.append)
    return events


class _State(SimpleNamespace):
    def broadcast_ws(self, kind: str, frame: dict[str, Any]) -> None:
        self.frames.append((kind, frame))


def _app(principal: Any, *, app_token: str = "") -> tuple[web.Application, _State]:
    @web.middleware
    async def as_viewer(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_token
        request["user"] = "u"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)
        return await handler(request)

    state = _State(owner_id="", _ws_clients=[], frames=[])
    app = web.Application(middlewares=[as_viewer])
    app["state"] = state
    routes_people.register(app)
    return app, state


def _path(user_id: str) -> str:
    return routes_people.DISPLAY_PATH.format(user_id=user_id)


@pytest.mark.asyncio
async def test_gate_off_answers_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    app, _ = _app(BOB)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4102"), json={"alias": "Riley"})
        assert resp.status == 404
    assert store.display_overrides() == {}


@pytest.mark.asyncio
async def test_any_admitted_person_edits_anyone_and_it_is_audited(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    # Riley was never invited: only the daemon's whois named this user id.
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    app, state = _app(BOB)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            _path("4102"),
            json={"alias": "Riley C.", "avatar_data_base64": base64.b64encode(PNG).decode()},
        )
        body = await resp.json()
        assert resp.status == 200, body
        assert body["alias"] == "Riley C." and store.valid_avatar_id(body["avatar_id"])
        assert body["audited"] is True
        resp = await client.put(_path("4102"), json={"alias": "Riley"})
        assert resp.status == 200
        # The picture stays unless the request clears it.
        assert (await resp.json())["avatar_id"] == body["avatar_id"]
    shown = store.display_overrides()["4102"]
    assert shown["alias"] == "Riley" and shown["login"] == "riley@example.com"
    assert shown["updated_by"] == BOB.id
    assert [e.event_type for e in written] == ["shared_crew_action", "shared_crew_action"]
    last = written[-1]
    assert last.operation == "shared_crew.person_display"
    assert last.caller_identity == BOB.id
    change = last.metadata["display_change"]
    assert change == {
        "user_id": "4102",
        "login": "riley@example.com",
        "old_name": "Riley C.",
        "new_name": "Riley",
        "avatar_changed": False,
    }
    assert written[0].metadata["display_change"]["old_name"] == ""
    assert [k for k, _ in state.frames] == ["shared_crew.people", "shared_crew.people"]


@pytest.mark.asyncio
async def test_a_teammate_renames_an_invited_member_and_the_old_name_is_the_invite_alias(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")

    def seed(s: dict[str, Any]) -> None:
        s["members"]["4107"] = {"user_id": "4107", "login": "sam@example.com", "alias": "Sam", "avatar_id": ""}

    store.update_state(seed)
    before = store.load_state()["members"]
    app, _ = _app(BOB)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4107"), json={"alias": "Samuel"})
        assert resp.status == 200
    assert written[-1].metadata["display_change"]["old_name"] == "Sam"
    # Display only: the member record that admission reads is untouched.
    assert store.load_state()["members"] == before


@pytest.mark.asyncio
async def test_bad_avatar_unknown_person_and_app_tokens_are_refused(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    app, _ = _app(local_operator_principal("local-app"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4102"), json={"avatar_data_base64": base64.b64encode(b"<svg/>").decode()})
        assert resp.status == 400 and (await resp.json())["code"] == "avatar_type"
        big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (store.MAX_AVATAR_BYTES + 1)).decode()
        resp = await client.put(_path("4102"), json={"avatar_data_base64": big})
        assert resp.status in (400, 413)
        resp = await client.put(_path("4102"), json={"avatar_data_base64": "not base64!"})
        assert resp.status == 400
        resp = await client.put(_path("9999"), json={"alias": "Nobody"})
        assert resp.status == 404 and (await resp.json())["code"] == "unknown_person"
        resp = await client.put(_path("bad id!"), json={"alias": "x"})
        assert resp.status in (400, 404)
    app_tok, _ = _app(BOB, app_token="some-app")
    async with TestClient(TestServer(app_tok)) as client:
        resp = await client.put(_path("4102"), json={"alias": "x"})
        assert resp.status == 403
    assert store.display_overrides() == {}
    assert written == []


def test_people_rows_carry_the_user_id_and_the_display_record() -> None:
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    store.set_display("4102", login="riley@example.com", alias="Riley", avatar_id="", actor="x")
    riley = tailnet_principal("riley@example.com", "riley-laptop", Provenance.TAILSCALE_LISTENER)
    body = {
        "people": [
            {"principal": riley.to_public_dict(), "provenance": Provenance.TAILSCALE_LISTENER.value},
            {"principal": local_operator_principal("local-app").to_public_dict(), "provenance": "local_operator"},
        ]
    }
    out = routes_invites.decorate_people(body)
    first, second = out["people"]
    assert first["tailscale_user_id"] == "4102"
    assert first["member"]["alias"] == "Riley"
    assert "tailscale_user_id" not in second and "member" not in second


def test_people_rows_say_who_is_here_and_where_by_the_tab_slot_key() -> None:
    from kiro_crew.shared_crew.collab import Activity
    from kiro_crew.shared_crew.presence import presence_for
    from kiro_crew.shared_crew.routes_identity import _attach_live

    state = SimpleNamespace()
    presence_for(state).report("c1", BOB, "Release_plan", Activity.TYPING)
    request = SimpleNamespace(app={"state": state})
    body = {"people": [{"principal": BOB.to_public_dict()}, {"principal": {"id": "p_gone"}}]}
    # The catalog lists the session under its file-stem key.
    rows = [{"key": "dashboard_Release_plan", "title": "Release plan"}]
    _attach_live(request, body, rows)  # type: ignore[arg-type]
    here, gone = body["people"]
    assert here["live"] == {
        "activity": "typing", "session_key": "Release_plan", "session_title": "Release plan"
    }
    assert "live" not in gone
