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
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.shared_crew import audit, routes_invites, routes_people
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Provenance, Transport
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BOB = tailnet_principal("bob@example.com", "bob-laptop", Provenance.TAILSCALE_LISTENER)


@pytest.fixture(autouse=True)
def _clean(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    import kiro_crew.dashboard.handlers._shared as shared

    _floor_monkeypatch.setattr(shared, "_is_restricted_session", lambda *_a: False)
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


def _app(
    principal: Any, *, app_token: str = "", direct: bool = True
) -> tuple[web.Application, _State]:
    @web.middleware
    async def as_viewer(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_token
        request["user"] = "u"
        request["is_dashboard_user"] = True
        # The direct tailnet listener admitted this person after checking the access set.
        request["auth_tailnet_direct"] = direct
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    state = _State(owner_id="", _ws_clients=[], frames=[])
    app = web.Application(middlewares=[as_viewer])
    app["state"] = state
    routes_people.register(app)
    return app, state


def _as_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local operator principal is the owner's own session: the owner gate passes."""
    from unittest.mock import AsyncMock

    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setattr(shared, "require_owner_for_dashboard_caller", AsyncMock(return_value=None))


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
        s["members"]["4107"] = {
            "user_id": "4107",
            "login": "sam@example.com",
            "alias": "Sam",
            "avatar_id": "",
        }

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
    _as_owner(monkeypatch)
    app, _ = _app(local_operator_principal("local-app"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            _path("4102"), json={"avatar_data_base64": base64.b64encode(b"<svg/>").decode()}
        )
        assert resp.status == 400 and (await resp.json())["code"] == "avatar_type"
        big = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (store.MAX_AVATAR_BYTES + 1)
        ).decode()
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


@pytest.mark.asyncio
async def test_a_name_only_save_keeps_a_picture_another_save_just_wrote(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    picture_b = store.save_avatar(PNG)
    real_set = store.set_display

    def racing_set(user_id: str, **kw: Any) -> Any:
        # Another dialog commits picture B after this request was parsed.
        real_set(user_id, login=kw["login"], alias="Riley", avatar_id=picture_b, actor="other")
        return real_set(user_id, **kw)

    monkeypatch.setattr(store, "set_display", racing_set)
    app, _ = _app(BOB)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4102"), json={"alias": "Riley C."})
        assert resp.status == 200
        assert (await resp.json())["avatar_id"] == picture_b
    assert store.display_overrides()["4102"]["avatar_id"] == picture_b


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_link_session_cannot_edit_anyone(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    from unittest.mock import AsyncMock

    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    gate = AsyncMock(
        return_value=web.json_response({"error": "owner only", "code": "owner_only"}, status=403)
    )
    monkeypatch.setattr(shared, "require_owner_for_dashboard_caller", gate)
    # A Telegram/Teams/Webex link session: a dashboard user, not admitted by the direct listener.
    app, _ = _app(local_operator_principal("link"), direct=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4102"), json={"alias": "Mallory"})
        assert resp.status == 403
    assert gate.await_args.args[1] == "shared_crew.person_display"
    assert store.display_overrides() == {}
    assert written == []


@pytest.mark.asyncio
async def test_the_owner_passes_the_owner_gate(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    _as_owner(monkeypatch)
    app, _ = _app(local_operator_principal("owner"), direct=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("4102"), json={"alias": "Riley"})
        assert resp.status == 200
    assert store.display_overrides()["4102"]["alias"] == "Riley"


def test_people_rows_carry_the_user_id_and_the_display_record() -> None:
    tailnet_live_allowlist.note_login("4102", "riley@example.com")
    store.set_display("4102", login="riley@example.com", alias="Riley", avatar_id="", actor="x")
    riley = tailnet_principal("riley@example.com", "riley-laptop", Provenance.TAILSCALE_LISTENER)
    body = {
        "people": [
            {
                "principal": riley.to_public_dict(),
                "provenance": Provenance.TAILSCALE_LISTENER.value,
            },
            {
                "principal": local_operator_principal("local-app").to_public_dict(),
                "provenance": "local_operator",
            },
        ]
    }
    out = routes_invites.decorate_people(body)
    first, second = out["people"]
    assert first["tailscale_user_id"] == "4102"
    assert first["member"]["alias"] == "Riley"
    assert "tailscale_user_id" not in second and "member" not in second


def test_a_full_avatar_folder_drops_only_old_unused_pictures_then_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import time

    monkeypatch.setattr(store, "MAX_AVATAR_FILES", 3)
    folder = store.root() / store.AVATAR_DIR
    shown = store.save_avatar(PNG + b"\x01")
    old_unused = store.save_avatar(PNG + b"\x02")
    draft = store.save_avatar(PNG + b"\x03")
    store.set_display("4102", login="riley@example.com", alias="Riley", avatar_id=shown, actor="x")
    # Replacing a picture never deletes it at once: a draft may still use it.
    store.set_display("4102", login="riley@example.com", alias="Riley", avatar_id=shown, actor="x")
    long_ago = time.time() - store.AVATAR_PRUNE_GRACE_SECS - 60
    for name in (shown, old_unused):
        os.utime(folder / name, (long_ago, long_ago))

    fourth = store.save_avatar(PNG + b"\x04")
    assert not (folder / old_unused).exists()  # old and shown by no record
    assert (folder / shown).exists()  # shown by a record
    assert (folder / draft).exists()  # unused but inside the grace period
    assert (folder / fourth).exists()

    with pytest.raises(ValueError, match="avatar_limit"):
        store.save_avatar(PNG + b"\x05")


def test_a_full_avatar_folder_deletes_nothing_when_the_state_file_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import time

    monkeypatch.setattr(store, "MAX_AVATAR_FILES", 2)
    folder = store.root() / store.AVATAR_DIR
    shown = store.save_avatar(PNG + b"\x01")
    other = store.save_avatar(PNG + b"\x02")
    store.set_display("4102", login="riley@example.com", alias="Riley", avatar_id=shown, actor="x")
    long_ago = time.time() - store.AVATAR_PRUNE_GRACE_SECS - 60
    for name in (shown, other):
        os.utime(folder / name, (long_ago, long_ago))
    (store.root() / store.STATE_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="avatar_limit"):
        store.save_avatar(PNG + b"\x03")
    assert (folder / shown).exists()
    assert (folder / other).exists()


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
        "activity": "typing",
        "session_key": "Release_plan",
        "session_title": "Release plan",
    }
    assert "live" not in gone


def test_people_owner_row_uses_presence_from_every_local_owner_id() -> None:
    from kiro_crew.shared_crew.collab import Activity
    from kiro_crew.shared_crew.presence import presence_for
    from kiro_crew.shared_crew.routes_identity import _attach_live

    state = SimpleNamespace()
    startup = local_operator_principal("local-startup")
    presence_for(state).report("c1", startup, "Owner_session", Activity.TYPING)
    request = SimpleNamespace(app={"state": state})
    body = {
        "people": [
            {"principal": local_operator_principal("local-app").to_public_dict(), "is_owner": True},
            {"principal": {"id": "p_unrelated"}, "is_owner": False},
        ]
    }

    _attach_live(request, body, [])  # type: ignore[arg-type]

    owner, unrelated = body["people"]
    assert owner["live"]["activity"] == "typing"
    assert "live" not in unrelated


@pytest.mark.asyncio
async def test_the_person_who_runs_this_crew_can_be_named_like_anyone(
    monkeypatch: pytest.MonkeyPatch, written: list[Any]
) -> None:
    from kiro_crew.shared_crew import access_set

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(access_set, "owner_user_id", lambda: "11")
    monkeypatch.setattr(access_set, "owner_login", lambda: "owner@example.com")
    _as_owner(monkeypatch)
    app, _ = _app(local_operator_principal("local-app"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(_path("11"), json={"alias": "Bolin"})
        assert resp.status == 200
    assert store.display_overrides()["11"]["login"] == "owner@example.com"
    body = {
        "people": [
            {
                "principal": local_operator_principal("local-app").to_public_dict(),
                "provenance": Provenance.LOCAL_OPERATOR.value,
            }
        ]
    }
    row = routes_invites.decorate_people(body)["people"][0]
    assert row["tailscale_user_id"] == "11"
    assert row["member"]["alias"] == "Bolin"
