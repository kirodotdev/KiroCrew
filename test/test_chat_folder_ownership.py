"""Ownership on the chat-folder tree-shaping endpoints.

A folder created by an app carries it in ``owner_app``; an absent key reads as
the person's, which is what makes this a field addition rather than a migration.
An app may create at the top level or inside a folder it owns, and may rename,
reparent or delete only what it owns. The person is never confined.

The scope is derived from the authenticated calling session, never the body: the
managed MCP set authenticates with the internal secret, which carries no app
claim, so an app agent's tool call arrives with ``request["app"]`` empty.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import pinned_fs
from kiro_crew.dashboard import chat_folders
from kiro_crew.dashboard.chat_folders import (
    _inherited_steering_dirs,
    _resolve_folder_project_dir,
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_update,
    api_chat_slot_folder,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

# fldr…01 belongs to the person, …02 to issue-radar, …03 to another app, and
# …04 predates the field entirely (no key at all) — the legacy row.
PERSON = "fldr00000001"
RADAR = "fldr00000002"
OTHER = "fldr00000003"
LEGACY = "fldr00000004"


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": PERSON, "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": RADAR, "name": "Radar output", "parent_id": "", "owner_app": "issue-radar"},
        {"id": OTHER, "name": "Specs", "parent_id": "", "owner_app": "spec-builder"},
        {"id": LEGACY, "name": "Old", "parent_id": ""},
    ]


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


def _state(*slots: _ChatSlot, folders: list[dict[str, Any]] | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = _folders() if folders is None else folders
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    # No archive by default: _folder_history_counts returns {} early on a falsy
    # conversation_log, which is what an app's delete consults for emptiness. A
    # bare MagicMock here would be iterated instead and raise.
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        # The real store runs the callback under a lock and hands back its
        # second element; the ownership decisions live inside that callback, so a
        # mock that never calls it would prove nothing.
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    state.mutate_folders = AsyncMock(side_effect=_mutate)

    async def _read(fn: Any) -> Any:
        # The committed reader the filing fence takes; on this mock the live
        # list IS the committed list. The provisional-window test below wires
        # the real repository instead, where the two differ.
        return fn(state._folders)

    state.read_folders = AsyncMock(side_effect=_read)

    async def _hold(section: Any) -> Any:
        # The lock-held section the filing chokepoint writes inside; on this mock
        # the snapshot is the live list.
        return await section([dict(f) for f in state._folders])

    state.hold_folders = AsyncMock(side_effect=_hold)
    return state


def _make_app(
    state: DashboardState, *, member_principal: str = "", dashboard_user: bool = False
) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        # Stands in for the token middleware's stamps, which are what the routes
        # read WHO from (``chat_folders._is_the_person``). ``request["app"]`` is
        # empty on the internal-secret (MCP) transport -- the path every agent
        # session's tool call takes; an app is derived from its slot, a member
        # is the principal the chat-route gate stamps on the VERIFIED scope
        # (``handlers/_shared.py``). ``dashboard_user=True`` adds the positive
        # ``is_dashboard_user`` stamp the middleware sets only when the PERSON's
        # own credential validated (the sidebar's cookie or session token);
        # nothing else carries it, so its absence is any other caller.
        request["app"] = ""
        if dashboard_user:
            request["is_dashboard_user"] = True
        if member_principal:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = member_principal
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    return app


def _by_id(state: DashboardState, fid: str) -> dict[str, Any] | None:
    return next((f for f in state._folders if f["id"] == fid), None)


class TestOrderIsStoredVerbatim:
    """The endpoint stores whatever int the body carries, sign included.

    ``chat_folder_move``'s free-slot placement puts a folder ahead of the first
    sibling by writing ``first.order - 1``, which is NEGATIVE once the sidebar has
    renumbered a set from 0 — the ordinary case. Nothing in the tool layer can make
    that work if the endpoint clamps or rejects it, and the tool writes it as the
    single request that keeps a reposition from landing half-applied.
    """

    @pytest.mark.asyncio
    async def test_a_negative_order_is_accepted(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"order": -1},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, PERSON)["order"] == -1

    @pytest.mark.asyncio
    async def test_a_gap_midpoint_is_accepted(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"order": 5},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, PERSON)["order"] == 5

    @pytest.mark.asyncio
    async def test_a_duplicate_order_is_not_refused(self) -> None:
        """Two siblings may share a number; the name tie-break resolves them.

        The free-slot check treats equal neighbours as no room precisely because
        the store allows this, so the allowance has to be pinned.
        """
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"order": 7},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            second = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"order": 7},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert (first.status, second.status) == (200, 200)
        assert _by_id(state, PERSON)["order"] == 7
        assert _by_id(state, RADAR)["order"] == 7


class TestTheToolPreCheckMatchesTheEndpointRule:
    """The tool's renumber pre-check and this endpoint must agree on ownership.

    ``chat_folder_move`` refuses an app a placement that would renumber a row it
    does not own, and it decides that in the TOOL layer, before its first write —
    because the endpoint judges one row at a time, so a refusal arriving halfway
    leaves the sidebar in an order nobody chose. That means the same rule is
    expressed twice: ``owner_app``-vs-caller in ``mcp_dashboard`` and
    ``_folder_owner_app`` inside this endpoint's ``_apply``.

    These drive the real endpoint rather than a patched ``_patch``, so a change to
    either side's rule — a tightened check, a different absent-key default — turns
    one of them red instead of letting the pre-check quietly permit a write the
    endpoint then refuses (or refuse one it would have allowed).
    """

    @pytest.mark.asyncio
    async def test_the_endpoint_refuses_the_order_write_the_pre_check_refuses(self) -> None:
        """An app writing order on a foreign row: refused, exactly as pre-checked."""
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"order": 3},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
        assert resp.status == 403
        assert "order" not in (_by_id(state, PERSON) or {})

    @pytest.mark.asyncio
    async def test_the_endpoint_allows_the_order_write_the_pre_check_allows(self) -> None:
        """The same app on its OWN row: allowed, so the pre-check is not over-broad."""
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"order": 3},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["order"] == 3

    @pytest.mark.asyncio
    async def test_a_legacy_row_reads_as_the_persons_on_both_sides(self) -> None:
        """The absent-key default is the drift the pre-check is most exposed to.

        ``LEGACY`` carries no ``owner_app`` at all. The pre-check reads a missing
        key as the person's via ``.get("owner_app")``; if the endpoint ever read it
        as unowned instead, an app renumber would sail past the pre-check and land.
        """
        state = _state(_app_slot("chat-1-200", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{LEGACY}",
                json={"order": 9},
                headers={"X-Session-Key": "dashboard:chat-1-200"},
            )
        assert resp.status == 403
        assert "order" not in (_by_id(state, LEGACY) or {})


class TestCreateStampsTheOwner:
    @pytest.mark.asyncio
    async def test_an_apps_folder_is_stamped_with_that_app(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert body["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_the_persons_folder_carries_no_owner_key(self) -> None:
        """Absent, not empty-string: "absent means the person" stays the one
        representation, and the person's rows keep the shape they have on disk."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Q3"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert "owner_app" not in body

    @pytest.mark.asyncio
    async def test_the_owner_is_never_taken_from_the_body(self) -> None:
        """A caller that could name its own owner could name someone else's."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "owner_app": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert body["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_an_app_may_nest_under_its_own_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": RADAR},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 201

    @pytest.mark.asyncio
    async def test_an_app_may_not_nest_under_the_persons_folder(self) -> None:
        """Nesting writes to THAT folder's child list."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        before = len(state._folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": PERSON},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert len(state._folders) == before

    @pytest.mark.asyncio
    async def test_a_legacy_row_without_the_key_is_the_persons(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": LEGACY},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403


class TestRenameAndReparentAreBounded:
    @pytest.mark.asyncio
    async def test_an_app_can_rename_its_own_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Renamed"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_an_app_cannot_rename_the_persons_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, PERSON)["name"] == "Work"

    @pytest.mark.asyncio
    async def test_an_app_cannot_rename_another_apps_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{OTHER}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403
        assert _by_id(state, OTHER)["name"] == "Specs"

    @pytest.mark.asyncio
    async def test_the_person_is_not_confined_by_an_apps_ownership(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Tidied up"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Tidied up"

    @pytest.mark.asyncio
    async def test_an_app_cannot_reparent_its_folder_into_the_persons(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"parent_id": PERSON},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, RADAR)["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_an_app_can_reparent_to_the_top_level(self) -> None:
        """The top level is not a folder row, so it has no owner to violate —
        that is where an app's own tree starts."""
        folders = _folders()
        nested = {
            "id": "fldr00000005",
            "name": "Runs",
            "parent_id": RADAR,
            "owner_app": "issue-radar",
        }
        folders.append(nested)
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/folders/fldr00000005",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, "fldr00000005")["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_ownership_cannot_be_reassigned_by_a_patch(self) -> None:
        """Stamped once at create; not a field a request can hand over or clear."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"owner_app": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_moving_own_folder_that_holds_a_foreign_one_is_refused(self) -> None:
        """A move takes the subtree with it, so the person's nested folder would
        be relocated by an app's write."""
        folders = _folders()
        folders.append({"id": "fldr00000007", "name": "Theirs", "parent_id": RADAR})
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, RADAR)["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_renaming_a_folder_that_holds_a_foreign_one_is_still_allowed(self) -> None:
        """Only the MOVE is gated on the subtree -- a rename relocates nothing."""
        folders = _folders()
        folders.append({"id": "fldr00000007", "name": "Theirs", "parent_id": RADAR})
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Renamed"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_the_person_can_still_move_a_folder_holding_an_apps(self) -> None:
        """Containment cuts both ways, but the person is never confined."""
        folders = _folders()
        folders.append(
            {
                "id": "fldr00000007",
                "name": "Radar sub",
                "parent_id": PERSON,
                "owner_app": "issue-radar",
            }
        )
        state = _state(_ChatSlot("chat-1-100"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": OTHER},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, PERSON)["parent_id"] == OTHER


def _slot_with_project(key: str, project: str, app: str = "") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.project = project
    if app:
        slot._app = app
    return slot


class TestAnAgentCannotBindAFolder:
    """A folder's project directory is the PERSON's to bind, from the sidebar:
    every AGENT -- an ordinary session's tool call, a crew member, an app, a
    cron, a subagent, a channel session -- is refused a non-empty
    ``project_dir`` at create and a set or clear on the PATCH, whole and before
    the path is looked at (``_agent_binding_refusal``), with one code and one
    text and the audit naming its principal or its session key. WHO is the person
    is ONE POSITIVE bit (``_is_the_person``): the token middleware's
    ``is_dashboard_user`` stamp, which only the person's own cookie or session token
    earns; every caller without it -- the internal-secret transport never sets it
    -- is refused. No caller is sorted by its key. The agent bind path
    (an admitted way for an agent to bind a folder) is a follow-up, not this
    change; what a binding confers once it exists is unchanged from main: every
    chat filed in the folder inherits it.
    """

    REFUSAL = (
        "an agent cannot set or clear a folder's project directory - the person binds a "
        "folder from the sidebar's Folder settings"
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_key",
        [
            "dashboard:chat-1-100",  # an ordinary session, its slot running in a project
            "cron:job-000001",
            "subagent:abcdef012345",
            "slack:1785370133.000001",
            "channel:chan-000001:helper",
            "",  # no session key at all
        ],
    )
    async def test_every_agent_is_refused_a_bound_create(self, tmp_path, session_key) -> None:
        state = _state(_slot_with_project("chat-1-100", str(tmp_path)))
        headers = {"X-Session-Key": session_key} if session_key else {}
        with patch("kiro_crew.dashboard.chat_folders._validate_project_dir") as validator:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Runs", "project_dir": str(tmp_path)},
                    headers=headers,
                )
                body = await resp.json()
        assert resp.status == 403, body
        assert body == {"error": self.REFUSAL, "code": "folder_project_dir_forbidden"}
        assert not any(f["name"] == "Runs" for f in state._folders)
        validator.assert_not_called()
        state.mutate_folders.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_crew_member_and_an_app_are_refused_the_same_way(self, tmp_path) -> None:
        """The two named agent principals meet the same refusal as an ordinary
        session, on their own folders as on the person's; their unbound
        creates land, stamped as their own."""
        state = _state(_slot_with_project("chat-1-100", str(tmp_path), app="issue-radar"))
        headers = {"X-Session-Key": "dashboard:chat-1-100"}
        async with TestClient(TestServer(_make_app(state))) as client:
            app_bound = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "project_dir": str(tmp_path)},
                headers=headers,
            )
            app_own = await client.patch(
                f"/api/chat/folders/{RADAR}", json={"project_dir": str(tmp_path)}, headers=headers
            )
            app_unbound = await client.post(
                "/api/chat/folders", json={"name": "Runs"}, headers=headers
            )
            app_row = await app_unbound.json()
        member_state = _state(_slot_with_project("chat-1-100", str(tmp_path)))
        member_app = _make_app(member_state, member_principal="member:reviewer-store")
        async with TestClient(TestServer(member_app)) as client:
            member_bound = await client.post(
                "/api/chat/folders",
                json={"name": "Reviews", "project_dir": str(tmp_path)},
                headers=headers,
            )
            member_bound_body = await member_bound.json()
            member_unbound = await client.post(
                "/api/chat/folders", json={"name": "Reviews"}, headers=headers
            )
            member_row = await member_unbound.json()
        assert (app_bound.status, app_own.status, member_bound.status) == (403, 403, 403)
        assert member_bound_body["error"] == self.REFUSAL
        assert "project_dir" not in _by_id(state, RADAR)
        assert (app_unbound.status, member_unbound.status) == (201, 201)
        assert (app_row["owner_app"], app_row["project_dir"]) == ("issue-radar", "")
        assert (member_row["owner_app"], member_row["project_dir"]) == ("member:reviewer-store", "")

    @pytest.mark.asyncio
    async def test_an_agent_cannot_set_or_clear_an_existing_binding(self, tmp_path) -> None:
        bound = {
            "id": "fldr0000000b",
            "name": "Bound",
            "parent_id": "",
            "project_dir": str(tmp_path),
        }
        state = _state(
            _slot_with_project("chat-1-100", str(tmp_path)), folders=[*_folders(), bound]
        )
        headers = {"X-Session-Key": "dashboard:chat-1-100"}
        with patch("kiro_crew.dashboard.chat_folders._validate_project_dir") as validator:
            async with TestClient(TestServer(_make_app(state))) as client:
                setting = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"project_dir": str(tmp_path)},
                    headers=headers,
                )
                clearing = await client.patch(
                    "/api/chat/folders/fldr0000000b", json={"project_dir": ""}, headers=headers
                )
                clearing_body = await clearing.json()
        assert (setting.status, clearing.status) == (403, 403)
        assert clearing_body["code"] == "folder_project_dir_forbidden"
        assert "project_dir" not in _by_id(state, PERSON)
        assert _by_id(state, "fldr0000000b")["project_dir"] == str(tmp_path)
        validator.assert_not_called()
        state.mutate_folders.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited_against_the_calling_session(self, tmp_path) -> None:
        state = _state(_slot_with_project("chat-1-100", str(tmp_path)))
        with patch("kiro_crew.dashboard.chat_folders.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Runs", "project_dir": str(tmp_path)},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "dashboard:chat-1-100"
        assert kwargs["operation"] == "chat.folder_create"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "app_isolation"
        assert kwargs["error"] == "agent cannot set or clear a folder's project directory"

    @pytest.mark.asyncio
    async def test_the_person_binds_anywhere_from_the_sidebar(self, tmp_path) -> None:
        """The person's browser call carries no transport stamp and is outside
        the rule: binds at create, re-points and clears, as the sidebar always
        could; the row carries no owner and the binding reaches every chat."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        state = _state(_ChatSlot("chat-1-100"))
        headers = {"X-Session-Key": "dashboard:chat-1-100"}
        async with TestClient(TestServer(_make_app(state, dashboard_user=True))) as client:
            created = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "project_dir": str(elsewhere)},
                headers=headers,
            )
            made = await created.json()
            updated = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"project_dir": str(tmp_path)}, headers=headers
            )
            cleared = await client.patch(
                f"/api/chat/folders/{made['id']}", json={"project_dir": ""}, headers=headers
            )
        assert (created.status, updated.status, cleared.status) == (201, 200, 200)
        assert "owner_app" not in made
        assert _by_id(state, PERSON)["project_dir"] == str(tmp_path.resolve())
        assert _by_id(state, made["id"])["project_dir"] == ""
        assert _resolve_folder_project_dir(state._folders, PERSON) == (
            str(tmp_path.resolve()),
            None,
        )


class TestAnAgentIsHeldToTheMoveAndSteeringRules:
    """The same WHO bit keys the two rules a binding's reach implies: an agent
    may not move a folder to where its subtree would inherit a different
    binding or different steering (``binding_crossed`` / ``steering_crossed``,
    decided under the store lock), and may not declare ``steering_dirs`` (a
    gateway host-file read that lands in the person's chats). An ordinary
    session is that agent as much as an app or a member; the person -- the
    sidebar's own credential -- keeps all four writes.
    """

    BOUND = "fldr0000000b"
    LOOSE = "fldr0000000c"

    def _tree(self, tmp_path) -> list[dict[str, Any]]:
        return [
            *_folders(),
            {"id": self.BOUND, "name": "Bound", "parent_id": "", "project_dir": str(tmp_path)},
            {"id": self.LOOSE, "name": "Loose", "parent_id": ""},
        ]

    @pytest.mark.asyncio
    async def test_an_ordinary_session_is_held_to_the_move_rule_on_both_axes(
        self, tmp_path
    ) -> None:
        folders = self._tree(tmp_path)
        folders.append(
            {
                "id": "fldr0000000d",
                "name": "Steered",
                "parent_id": "",
                "steering_dirs": [str(tmp_path / "notes")],
            }
        )
        state = _state(_slot_with_project("chat-1-100", str(tmp_path)), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            across_binding = await client.patch(
                f"/api/chat/folders/{self.LOOSE}",
                json={"parent_id": self.BOUND},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            binding_body = await across_binding.json()
            across_steering = await client.patch(
                f"/api/chat/folders/{self.LOOSE}",
                json={"parent_id": "fldr0000000d"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            steering_body = await across_steering.json()
        assert across_binding.status == 403, binding_body
        assert binding_body["code"] == "folder_project_dir_forbidden"
        assert binding_body["error"].startswith("an agent cannot move a folder")
        assert across_steering.status == 403, steering_body
        assert steering_body["code"] == "steering_dirs_forbidden"
        assert _by_id(state, self.LOOSE)["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_an_ordinary_session_cannot_declare_steering_at_create_or_update(
        self, tmp_path
    ) -> None:
        state = _state(
            _slot_with_project("chat-1-100", str(tmp_path)), folders=self._tree(tmp_path)
        )
        with patch("kiro_crew.dashboard.chat_folders.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                created = await client.post(
                    "/api/chat/folders",
                    json={"name": "Notes", "steering_dirs": [str(tmp_path)]},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                created_body = await created.json()
                updated = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"steering_dirs": [str(tmp_path)]},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert created.status == 403, created_body
        assert created_body["code"] == "steering_dirs_forbidden"
        assert updated.status == 403
        assert not any(f["name"] == "Notes" for f in state._folders)
        assert "steering_dirs" not in _by_id(state, PERSON)
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "dashboard:chat-1-100"
        assert kwargs["source"] == "app_isolation"

    @pytest.mark.asyncio
    async def test_a_channel_session_without_a_slot_is_the_same_agent(self, tmp_path) -> None:
        """No key-shape arm: a Channels agent's key is just a session that
        names no slot, refused a declaration and a cross-binding move by the
        same two rules, audited against its key under the same source."""
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(tmp_path))
        headers = {"X-Session-Key": "channel:chan-000001:helper"}
        with patch("kiro_crew.dashboard.chat_folders.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                moved = await client.patch(
                    f"/api/chat/folders/{self.LOOSE}",
                    json={"parent_id": self.BOUND},
                    headers=headers,
                )
                declared = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"steering_dirs": [str(tmp_path)]},
                    headers=headers,
                )
        assert (moved.status, declared.status) == (403, 403)
        assert _by_id(state, self.LOOSE)["parent_id"] == ""
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "channel:chan-000001:helper"
        assert kwargs["source"] == "app_isolation"

    @pytest.mark.asyncio
    async def test_the_person_keeps_all_four(self, tmp_path) -> None:
        """The sidebar's own credential: sets and clears, moves across a binding
        and across steering, and its steering declaration REACHES the validator
        -- the fence's 403 is never the person's answer. Whether the validator
        then admits the directory is the platform's question (native Windows
        refuses every non-empty ``steering_dirs`` with its own 400), so both
        arms are asserted below, each where it can be observed; the sibling test
        pins the unsupported arm on every platform."""
        folders = self._tree(tmp_path)
        folders.append(
            {
                "id": "fldr0000000d",
                "name": "Steered",
                "parent_id": "",
                "steering_dirs": [str(tmp_path / "notes")],
            }
        )
        state = _state(_ChatSlot("chat-1-100"), folders=folders)
        headers = {"X-Session-Key": "dashboard:chat-1-100"}
        async with TestClient(TestServer(_make_app(state, dashboard_user=True))) as client:
            setting = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"project_dir": str(tmp_path)}, headers=headers
            )
            clearing = await client.patch(
                f"/api/chat/folders/{self.BOUND}", json={"project_dir": ""}, headers=headers
            )
            across_binding = await client.patch(
                f"/api/chat/folders/{self.LOOSE}", json={"parent_id": PERSON}, headers=headers
            )
            across_steering = await client.patch(
                f"/api/chat/folders/{self.LOOSE}",
                json={"parent_id": "fldr0000000d"},
                headers=headers,
            )
            declaring = await client.patch(
                f"/api/chat/folders/{self.LOOSE}",
                json={"steering_dirs": [str(tmp_path)]},
                headers=headers,
            )
            declaring_body = await declaring.json()
        assert (
            setting.status,
            clearing.status,
            across_binding.status,
            across_steering.status,
        ) == (200, 200, 200, 200)
        assert _by_id(state, PERSON)["project_dir"] == str(tmp_path.resolve())
        assert _by_id(state, self.BOUND)["project_dir"] == ""
        assert _by_id(state, self.LOOSE)["parent_id"] == "fldr0000000d"
        # The declaration REACHED the validator on every platform -- the fence's
        # 403 is never the person's answer. What the validator then says is the
        # platform's: where a directory can be opened relative to a descriptor
        # it is admitted and stored; on native Windows ``_validate_steering_dirs``
        # refuses every non-empty list with its own 400, before any filesystem
        # call, so that explicit refusal is asserted there instead of skipped.
        if pinned_fs.supports_pinned_tree_walk():
            assert declaring.status == 200, declaring_body
            assert _by_id(state, self.LOOSE)["steering_dirs"] == [str(tmp_path.resolve())]
        else:
            assert declaring.status == 400, declaring_body
            assert declaring_body["code"] == "steering_dirs_invalid"
            assert "not supported on this platform" in declaring_body["error"]
            assert "steering_dirs" not in _by_id(state, self.LOOSE)

    @pytest.mark.asyncio
    async def test_the_persons_declaration_meets_the_validator_not_the_fence_where_walks_are_unpinned(
        self, tmp_path, monkeypatch
    ) -> None:
        """The native-Windows shape, pinned so every platform asserts it: with
        the pinned-walk seam answering False the person's declaration is refused
        by the VALIDATOR (400, its platform text, nothing stored) -- not by the
        fence, whose 403 ``steering_dirs_forbidden`` would mean the person had
        been read as an agent. Red on the r11 shape of the sibling test, which
        expected 200 here: ``assert (200, 200, 200, 200, 400) == (200, 200,
        200, 200, 200)`` was the Windows shard's failure."""
        monkeypatch.setattr(chat_folders.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(tmp_path))
        async with TestClient(TestServer(_make_app(state, dashboard_user=True))) as client:
            declaring = await client.patch(
                f"/api/chat/folders/{self.LOOSE}",
                json={"steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await declaring.json()
        assert declaring.status == 400, body
        assert body["code"] == "steering_dirs_invalid"
        assert "not supported on this platform" in body["error"]
        assert "steering_dirs" not in _by_id(state, self.LOOSE)


class TestAMoveCannotChangeWhatASubtreeInherits:
    """A binding a folder holds -- however it came to hold one: the person's
    Folder settings on a member's folder, a row written before the fences --
    reaches every chat filed in its subtree on their next agent switch, so a
    reparent of an UNBOUND folder under a bound one, or out from under one,
    rebinds those chats exactly as a refused PATCH would have. The rule: a
    non-person caller's reparent may not change the binding the moved subtree
    resolves, compared as the stored binding each place confers
    (``_inherited_project_dir``). A folder carrying its own binding is exempt
    (nearest wins: its subtree resolves it wherever it sits); an unbound one
    moves only between places that confer the same binding; every non-person
    caller -- a crew member, an app, an ordinary session -- is held to it; the
    person is never confined.
    """

    MEMBER = "member:reviewer-store"
    REVIEWS = "fldr00000011"
    BOUND = "fldr00000009"
    BOUND_CHILD = "fldr00000010"

    @staticmethod
    def _tree(bound_dir: str, reviews_parent: str = "") -> list[dict[str, Any]]:
        cls = TestAMoveCannotChangeWhatASubtreeInherits
        folders = _folders()
        folders.append(
            {
                "id": cls.REVIEWS,
                "name": "Reviews",
                "parent_id": reviews_parent,
                "owner_app": cls.MEMBER,
            }
        )
        folders.append(
            {
                "id": cls.BOUND,
                "name": "Bound at create",
                "parent_id": "",
                "owner_app": cls.MEMBER,
                "project_dir": bound_dir,
            }
        )
        folders.append(
            {
                "id": cls.BOUND_CHILD,
                "name": "Under bound",
                "parent_id": cls.BOUND,
                "owner_app": cls.MEMBER,
            }
        )
        return folders

    def _member_app(self, state: DashboardState) -> web.Application:
        return _make_app(state, member_principal=self.MEMBER)

    @pytest.mark.asyncio
    async def test_a_member_cannot_move_its_folder_under_one_it_bound_at_create(
        self, tmp_path
    ) -> None:
        """The exact composition: create C with project_dir, then reparent the
        member's existing folder -- holding one of the person's chats and the
        member's own -- under C. Before: the move lands and the member's own
        chats filed there resolve C's directory on their next agent switch (the
        person's never resolve a member's binding). After: refused with the
        binding-fence code, nothing inherited by anyone."""
        theirs = _ChatSlot("chat-2-200")
        theirs.folder_id = self.REVIEWS
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path)))
        async with TestClient(TestServer(self._member_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": self.BOUND},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_project_dir_forbidden"
        assert _by_id(state, self.REVIEWS)["parent_id"] == ""
        # What the person's chat resolves on its next agent switch: still nothing.
        assert _resolve_folder_project_dir(state._folders, self.REVIEWS) == ("", None)

    @pytest.mark.asyncio
    async def test_moving_out_from_under_a_binding_is_refused_the_same_way(self, tmp_path) -> None:
        """The clear direction: the subtree would stop inheriting."""
        state = _state(
            _ChatSlot("chat-1-100"),
            folders=self._tree(str(tmp_path), reviews_parent=self.BOUND),
        )
        async with TestClient(TestServer(self._member_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_project_dir_forbidden"
        assert _by_id(state, self.REVIEWS)["parent_id"] == self.BOUND

    @pytest.mark.asyncio
    async def test_a_move_that_keeps_the_inherited_binding_still_lands(self, tmp_path) -> None:
        """Between two places under the same bound ancestor nothing changes for
        the subtree, so the member's own tree stays organisable."""
        state = _state(
            _ChatSlot("chat-1-100"),
            folders=self._tree(str(tmp_path), reviews_parent=self.BOUND),
        )
        async with TestClient(TestServer(self._member_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": self.BOUND_CHILD},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, self.REVIEWS)["parent_id"] == self.BOUND_CHILD

    @pytest.mark.asyncio
    async def test_a_member_bound_folder_moves_where_nobody_elses_binding_changes(
        self, tmp_path
    ) -> None:
        """The member's own binding stops its own chats wherever the folder
        sits, and neither place confers anything on anyone else (both are
        member-bound or unbound), so the move changes nothing for any chat and
        lands."""
        folders = self._tree(str(tmp_path))
        next(f for f in folders if f["id"] == self.REVIEWS)["project_dir"] = str(tmp_path / "own")
        state = _state(_ChatSlot("chat-1-100"), folders=folders)
        async with TestClient(TestServer(self._member_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": self.BOUND},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, self.REVIEWS)["parent_id"] == self.BOUND

    @pytest.mark.asyncio
    async def test_a_bound_folder_moves_freely_because_nearest_wins(self, tmp_path) -> None:
        """A folder carrying its own binding resolves it wherever it sits, so
        moving it out from under the person's bound folder changes nothing for
        the chats filed in it -- exempt, whoever moves it."""
        folders = self._tree(str(tmp_path), reviews_parent=PERSON)
        next(f for f in folders if f["id"] == PERSON)["project_dir"] = str(tmp_path / "mine")
        (tmp_path / "mine").mkdir()
        (tmp_path / "own").mkdir()
        next(f for f in folders if f["id"] == self.REVIEWS)["project_dir"] = str(tmp_path / "own")
        theirs = _ChatSlot("chat-2-200")
        theirs.folder_id = self.REVIEWS
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=folders)
        own = str((tmp_path / "own").resolve())
        assert _resolve_folder_project_dir(state._folders, self.REVIEWS) == (own, None)
        async with TestClient(TestServer(self._member_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200, await resp.text()
        assert _by_id(state, self.REVIEWS)["parent_id"] == ""
        assert _resolve_folder_project_dir(state._folders, self.REVIEWS) == (own, None)

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited(self, tmp_path) -> None:
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(str(tmp_path)))
        with patch("kiro_crew.dashboard.chat_folders.sel") as sel_fn:
            async with TestClient(TestServer(self._member_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{self.REVIEWS}",
                    json={"parent_id": self.BOUND},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == self.MEMBER
        assert kwargs["operation"] == "chat.folder_update"
        assert kwargs["outcome"] == "denied"
        assert kwargs["resources"] == self.REVIEWS
        assert "inherit" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_an_app_is_held_to_the_rule_like_every_non_person_caller(self, tmp_path) -> None:
        """An app's unbound folder, holding one of the person's chats, sits
        under the person's BOUND folder (the person nested it there). Moving it
        to the top level would clear that chat's project on its next agent
        switch, so the app's move is refused like a member's or an ordinary
        session's; nothing is inherited differently."""
        folders = _folders()
        next(f for f in folders if f["id"] == PERSON)["project_dir"] = str(tmp_path)
        next(f for f in folders if f["id"] == RADAR)["parent_id"] = PERSON
        theirs = _ChatSlot("chat-2-200")
        theirs.folder_id = RADAR
        state = _state(_app_slot("chat-1-100", "issue-radar"), theirs, folders=folders)
        resolved = str(tmp_path.resolve())
        assert _resolve_folder_project_dir(state._folders, RADAR) == (resolved, None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403, body
        assert body["code"] == "folder_project_dir_forbidden"
        assert _by_id(state, RADAR)["parent_id"] == PERSON
        assert _resolve_folder_project_dir(state._folders, RADAR) == (resolved, None)

    @pytest.mark.asyncio
    async def test_the_person_moves_across_bindings_freely(self, tmp_path) -> None:
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(str(tmp_path)))
        async with TestClient(TestServer(_make_app(state, dashboard_user=True))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.REVIEWS}",
                json={"parent_id": self.BOUND},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, self.REVIEWS)["parent_id"] == self.BOUND


class TestAMoveCannotChangeWhatASubtreeInheritsForSteeringEither:
    """The second thing a folder's ancestry decides for every chat filed beneath
    it: the steering directories it inherits, ACCUMULATIVELY up ``parent_id``
    (``_resolve_folder_steering_dirs``), read into each chat's model context at
    session start. The binding branch of the move rule compared only the
    inherited ``project_dir``, so an agent -- refused a ``steering_dirs``
    declaration of its own -- could reparent the person's folder under one that
    declares steering whenever both places inherit the same binding, and the
    person's chats filed inside picked those documents up at their next start:
    the declaration fence, reached through the tree. So the same pre-commit
    branch compares what the moved subtree would inherit for steering too --
    the stored declarations of every ancestor, root-first, with each declaring
    folder's owner, exactly the data the resolver consumes -- and refuses a
    move that changes it, in either direction, with the steering fence's own
    code. A move between two places that inherit the same steering lands; the
    person is not confined. The mover below is a Channels agent's session: one
    agent among others (its key names no slot), refused by the same bit as an
    ordinary session or an app, audited against its key under the one source.
    """

    CHANNEL = "channel:chan-000001:helper"
    STEERED = "fldr00000031"
    STEERED_CHILD = "fldr00000032"
    RADAR_STEERED = "fldr00000033"
    RADAR_LEAF = "fldr00000034"

    @staticmethod
    def _tree(person_parent: str = "") -> list[dict[str, Any]]:
        folders = _folders()
        next(f for f in folders if f["id"] == PERSON)["parent_id"] = person_parent
        cls = TestAMoveCannotChangeWhatASubtreeInheritsForSteeringEither
        folders.extend(
            [
                # The PERSON's folder declaring steering: stored strings, never
                # validated here -- the branch runs under the store lock.
                {
                    "id": cls.STEERED,
                    "name": "Standards",
                    "parent_id": "",
                    "steering_dirs": ["/srv/standards"],
                },
                {"id": cls.STEERED_CHILD, "name": "Under standards", "parent_id": cls.STEERED},
                # An app's folder on which the PERSON declared steering (allowed:
                # the delivery gate routes it to the app's own chats), holding an
                # unbound folder of the app's.
                {
                    "id": cls.RADAR_STEERED,
                    "name": "Radar standards",
                    "parent_id": "",
                    "owner_app": "issue-radar",
                    "steering_dirs": ["/srv/radar-standards"],
                },
                {
                    "id": cls.RADAR_LEAF,
                    "name": "Radar runs",
                    "parent_id": cls.RADAR_STEERED,
                    "owner_app": "issue-radar",
                },
            ]
        )
        return folders

    @pytest.mark.asyncio
    async def test_an_agent_cannot_move_the_persons_folder_under_declared_steering(
        self,
    ) -> None:
        """Both places inherit the same (empty) binding, so the binding branch is
        silent. Before: the move lands and the person's chat filed inside
        inherits ``/srv/standards`` at its next start. After: refused with the
        steering fence's code, nothing inherited."""
        theirs = _ChatSlot("chat-2-200")
        theirs.folder_id = PERSON
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": self.STEERED},
                headers={"X-Session-Key": self.CHANNEL},
            )
            body = await resp.json()
        assert resp.status == 403, body
        assert body["code"] == "steering_dirs_forbidden"
        assert _by_id(state, PERSON)["parent_id"] == ""
        assert _inherited_steering_dirs(state._folders, PERSON) == ()

    @pytest.mark.asyncio
    async def test_moving_out_from_under_declared_steering_is_refused_too(self) -> None:
        """The clear direction: the person's chats inside would stop receiving
        the standards the person put above them."""
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(person_parent=self.STEERED))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": ""},
                headers={"X-Session-Key": self.CHANNEL},
            )
            body = await resp.json()
        assert resp.status == 403, body
        assert body["code"] == "steering_dirs_forbidden"
        assert _by_id(state, PERSON)["parent_id"] == self.STEERED

    @pytest.mark.asyncio
    async def test_an_app_is_held_to_the_same_rule_on_its_own_folders(self) -> None:
        """Its own unbound folder, out from under its own folder on which the
        person declared steering: the app's chats filed in it would stop
        receiving what the person declared for them. Refused, as every agent
        principal is."""
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=self._tree())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{self.RADAR_LEAF}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403, body
        assert body["code"] == "steering_dirs_forbidden"
        assert _by_id(state, self.RADAR_LEAF)["parent_id"] == self.RADAR_STEERED

    @pytest.mark.asyncio
    async def test_a_move_that_keeps_the_inherited_steering_still_lands(self) -> None:
        """Scope pin: between two places under the same declaring ancestor the
        accumulated set is identical, so the move lands as it always did."""
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree(person_parent=self.STEERED))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": self.STEERED_CHILD},
                headers={"X-Session-Key": self.CHANNEL},
            )
        assert resp.status == 200, await resp.text()
        assert _by_id(state, PERSON)["parent_id"] == self.STEERED_CHILD

    @pytest.mark.asyncio
    async def test_the_person_is_not_confined(self) -> None:
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree())
        async with TestClient(TestServer(_make_app(state, dashboard_user=True))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": self.STEERED},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200, await resp.text()
        assert _by_id(state, PERSON)["parent_id"] == self.STEERED

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited_against_the_calling_key(self) -> None:
        state = _state(_ChatSlot("chat-1-100"), folders=self._tree())
        with patch("kiro_crew.dashboard.chat_folders.sel") as sel_fn:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    f"/api/chat/folders/{PERSON}",
                    json={"parent_id": self.STEERED},
                    headers={"X-Session-Key": self.CHANNEL},
                )
        assert resp.status == 403
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == self.CHANNEL
        assert kwargs["operation"] == "chat.folder_update"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "app_isolation"
        assert kwargs["resources"] == PERSON
        assert "steering" in kwargs["error"]


class TestAnAppCannotDeleteFolders:
    """A delete relocates everything the folder contains, and those contents live
    in a DIFFERENT store from the folder -- the slot table and the session
    archive, neither sharing a lock with it. So emptiness cannot be established
    atomically with the removal, and every narrower rule leaked through another
    seam. The person keeps the delete they always had.

    Nothing shipped loses a capability: no MCP tool exposes folder deletion, and
    the only client of the route is the dashboard UI.
    """

    @pytest.mark.asyncio
    async def test_an_app_cannot_delete_even_an_empty_folder_it_owns(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_delete_forbidden"
        assert _by_id(state, RADAR) is not None

    @pytest.mark.asyncio
    async def test_no_session_is_touched_by_the_refusal(self) -> None:
        """Refused before the unfile loop, so nothing is written and there is
        nothing to roll back."""
        mine = _app_slot("chat-9-900", "issue-radar")
        mine.folder_id = RADAR
        state = _state(_app_slot("chat-1-100", "issue-radar"), mine)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        assert mine.folder_id == RADAR

    @pytest.mark.asyncio
    async def test_the_person_can_still_delete_a_full_folder(self) -> None:
        """The person is not confined: clearing out a folder full of
        conversations and subfolders is the delete they already had."""
        theirs = _app_slot("chat-9-900", "issue-radar")
        theirs.folder_id = RADAR
        folders = _folders()
        folders.append({"id": "fldr00000006", "name": "Sub", "parent_id": RADAR})
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=folders)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 200
        assert _by_id(state, RADAR) is None
        assert theirs.folder_id == ""
        assert _by_id(state, "fldr00000006")["parent_id"] == ""


class TestACallerWhoseSlotIsGoneIsRefused:
    """An empty scope reads as the person, which is right for a caller that never
    had a slot (Slack, a channel session, the person's cron) and wrong for a
    `dashboard:` key, which NAMES one. A tab closing while its tool call is in
    flight pops the slot without draining, so an app-owned session would arrive
    unattributable and be handed the person's authority over the person's folders.
    """

    @pytest.mark.asyncio
    async def test_create_is_refused(self) -> None:
        state = _state()  # the named slot is absent from the registry
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Sneaky"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "caller_unattributable"

    @pytest.mark.asyncio
    async def test_rename_of_the_persons_folder_is_refused(self) -> None:
        state = _state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403
        assert _by_id(state, PERSON)["name"] == "Work"

    @pytest.mark.asyncio
    async def test_delete_is_refused_before_any_slot_is_unfiled(self) -> None:
        filed = _ChatSlot("chat-9-900")
        filed.folder_id = PERSON
        state = _state(filed)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{PERSON}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        assert _by_id(state, PERSON) is not None
        assert filed.folder_id == PERSON

    @pytest.mark.asyncio
    async def test_a_caller_that_never_had_a_slot_is_still_the_person(self) -> None:
        """The refusal must not swallow Slack, channel or cron callers -- they
        never had a slot to be confined to, which is a different fact."""
        state = _state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Tidied"},
                headers={"X-Session-Key": "slack:T1/C1"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Tidied"


class TestFilingASessionCannotChangeWhatItInheritsEither:
    """The third route to the same harm. A folder's binding and steering reach a
    chat through the FOLDER IT IS FILED IN, so re-filing the person's chat under
    a bound folder (or out from under one) rebinds it on its next agent switch
    exactly as a refused reparent would have -- and ``PATCH
    /api/chat/slots/{slot}/folder`` (behind ``chat_folder_move_session`` and
    ``chat_folder_file_self``) wrote ``slot.folder_id`` into any existing folder
    behind transcript-ownership checks alone. The rule is the move rule's, one
    more site: a non-person filing may not change the binding
    (``_inherited_project_dir``) or the steering chain
    (``_inherited_steering_dirs``) the session inherits, compared as the stored
    values the two folders confer, before the write; the person is never
    confined. Red-first on the head before this class: every refused filing
    here landed with 200 and the slot carried the bound folder.
    """

    BOUND = "fldr00000021"
    BOUND_CHILD = "fldr00000022"
    STEERED = "fldr00000023"
    PLAIN = "fldr00000024"

    @classmethod
    def _tree(cls, bound_dir: str, steering: str) -> list[dict[str, Any]]:
        folders = _folders()
        folders.append(
            {"id": cls.BOUND, "name": "Bound", "parent_id": "", "project_dir": bound_dir}
        )
        folders.append({"id": cls.BOUND_CHILD, "name": "Under bound", "parent_id": cls.BOUND})
        folders.append(
            {"id": cls.STEERED, "name": "Steered", "parent_id": "", "steering_dirs": [steering]}
        )
        folders.append({"id": cls.PLAIN, "name": "Plain", "parent_id": ""})
        return folders

    @staticmethod
    async def _file(
        state: DashboardState, app: web.Application, slot_key: str, folder_id: str
    ) -> tuple[int, dict[str, Any]]:
        with (
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"),
            patch("kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/{slot_key}/folder",
                    json={"folder_id": folder_id},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_an_ordinary_session_cannot_file_the_persons_chat_under_a_bound_folder(
        self, tmp_path
    ) -> None:
        """The exact composition the description names: the person's unfiled
        chat, re-filed by an ordinary session (no app, not a member, no person
        stamp -- the transport every agent tool call takes) into a folder whose
        chain confers a binding. Refused with the binding fence's code; the
        slot stays where it was."""
        theirs = _ChatSlot("chat-2-200")
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        status, body = await self._file(state, _make_app(state), "chat-2-200", self.BOUND_CHILD)
        assert status == 403, body
        assert body["code"] == "folder_project_dir_forbidden"
        assert theirs.folder_id == ""

    @pytest.mark.asyncio
    async def test_unfiling_out_from_under_a_binding_is_the_same_change(self, tmp_path) -> None:
        theirs = _ChatSlot("chat-2-200")
        theirs.folder_id = self.BOUND_CHILD
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        status, body = await self._file(state, _make_app(state), "chat-2-200", "")
        assert status == 403, body
        assert body["code"] == "folder_project_dir_forbidden"
        assert theirs.folder_id == self.BOUND_CHILD

    @pytest.mark.asyncio
    async def test_a_filing_that_changes_the_inherited_steering_is_refused_too(
        self, tmp_path
    ) -> None:
        """The steering axis: the destination confers no binding, but declares
        steering the session does not inherit today -- the steering fence's
        code, so a client branches on the rule that refused it."""
        theirs = _ChatSlot("chat-2-200")
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        status, body = await self._file(state, _make_app(state), "chat-2-200", self.STEERED)
        assert status == 403, body
        assert body["code"] == "steering_dirs_forbidden"
        assert theirs.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_filing_between_places_that_confer_the_same_inheritance_lands(
        self, tmp_path
    ) -> None:
        """Scope pin: filing into a folder whose chain confers neither a binding
        nor steering the session lacks is the ordinary sidebar move and still
        lands for an ordinary session; so does a move between two folders under
        the same binding."""
        theirs = _ChatSlot("chat-2-200")
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        status, _body = await self._file(state, _make_app(state), "chat-2-200", self.PLAIN)
        assert status == 200
        assert theirs.folder_id == self.PLAIN
        under = _ChatSlot("chat-3-300")
        under.folder_id = self.BOUND
        state = _state(_ChatSlot("chat-1-100"), under, folders=self._tree(str(tmp_path), "x"))
        status, _body = await self._file(state, _make_app(state), "chat-3-300", self.BOUND_CHILD)
        assert status == 200
        assert under.folder_id == self.BOUND_CHILD

    @pytest.mark.asyncio
    async def test_a_member_is_held_to_it_for_its_own_session(self, tmp_path) -> None:
        """A member files only its own sessions (``member_slot_write_refused``);
        this rule holds it there too: its own chat under the person's bound
        folder would resolve the person's directory on its next switch."""
        mine = _ChatSlot("chat-1-100")
        state = _state(mine, folders=self._tree(str(tmp_path), "x"))
        app = _make_app(state, member_principal="member:reviewer-store")
        status, body = await self._file(state, app, "chat-1-100", self.BOUND)
        assert status == 403, body
        assert body["code"] == "folder_project_dir_forbidden"
        assert mine.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_person_files_anywhere(self, tmp_path) -> None:
        theirs = _ChatSlot("chat-2-200")
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        app = _make_app(state, dashboard_user=True)
        status, _body = await self._file(state, app, "chat-2-200", self.BOUND_CHILD)
        assert status == 200
        assert theirs.folder_id == self.BOUND_CHILD
        status, _body = await self._file(state, app, "chat-2-200", self.STEERED)
        assert status == 200
        assert theirs.folder_id == self.STEERED
        status, _body = await self._file(state, app, "chat-2-200", "")
        assert status == 200
        assert theirs.folder_id == ""

    @pytest.mark.asyncio
    async def test_the_fence_judges_committed_folders_not_a_clear_awaiting_persistence(
        self, tmp_path
    ) -> None:
        """The repository's ``mutate`` applies a change to the
        LIVE list, awaits the off-loop write, and restores the list only if that
        write fails -- so a person's binding CLEAR that is about to roll back
        reads as "unbound" on the live list. The fence read that list unlocked,
        under the slot lock alone: an agent filing during the window landed, the
        write failed, the binding came back, and the agent's chat sat under the
        bound folder with nothing to undo the filing. Red on the head before
        this test: 200 and the slot filed. The fence now reads through
        ``read_folders`` -- the store lock excludes the mutate in flight, so what
        it sees has landed -- and the slot is revalidated after that await. The
        real repository drives the window here; the mock's ``mutate_folders``
        cannot show it."""
        from kiro_crew.dashboard.folder_repository import FolderRepository

        theirs = _ChatSlot("chat-2-200")
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=self._tree(str(tmp_path), "x"))
        repo = FolderRepository(lambda: MagicMock())
        lock = asyncio.Lock()
        write_reached = asyncio.Event()
        release_write = threading.Event()
        loop = asyncio.get_running_loop()

        def _failing_write(_path: Any, _snapshot: Any) -> None:
            loop.call_soon_threadsafe(write_reached.set)
            assert release_write.wait(timeout=10)
            raise OSError("folder store did not persist as intended")

        async def _mutate(fn: Any, on_committed: Any = None) -> Any:
            return await repo.mutate(
                lambda: state._folders, lock, fn, lambda: tmp_path / "folders.json", _failing_write
            )

        async def _read(fn: Any) -> Any:
            return await repo.read(lambda: state._folders, lock, fn)

        async def _hold(section: Any) -> Any:
            return await repo.hold(lambda: state._folders, lock, section)

        state.mutate_folders = AsyncMock(side_effect=_mutate)
        state.read_folders = AsyncMock(side_effect=_read)
        state.hold_folders = AsyncMock(side_effect=_hold)

        def _clear_binding(folders: list[dict[str, Any]]) -> tuple[bool, None]:
            for folder in folders:
                if folder["id"] == self.BOUND:
                    folder["project_dir"] = ""
            return True, None

        clearing = asyncio.create_task(state.mutate_folders(_clear_binding))
        await asyncio.wait_for(write_reached.wait(), timeout=10)
        # The window: the clear is live, its write not yet failed. The agent
        # files the person's chat under the (provisionally unbound) folder.
        assert _by_id(state, self.BOUND)["project_dir"] == ""
        filing = asyncio.create_task(
            self._file(state, _make_app(state), "chat-2-200", self.BOUND_CHILD)
        )
        await asyncio.sleep(0.2)
        release_write.set()
        with pytest.raises(OSError):
            await clearing
        status, body = await asyncio.wait_for(filing, timeout=10)
        assert _by_id(state, self.BOUND)["project_dir"] == str(tmp_path), "the clear rolled back"
        assert status == 403, body
        assert body["code"] == "folder_project_dir_forbidden"
        assert theirs.folder_id == ""


class TestEveryWriterOfASessionsFolderOrProjectIsAccountedFor:
    """The structural pin behind the one filing decision.

    Filing is how a session acquires a folder's binding and steering, so every
    place that writes ``<slot>.folder_id`` or ``<slot>.project`` is one of two
    things: a REQUEST-DRIVEN writer, which must go through the one filing
    decision (``refuse_filing_across_inheritance`` on a route,
    ``filing_crosses_inheritance`` behind the agent tools' own gate) or the
    project fences the routes already carry; or a writer that places a session
    from the gateway's own configuration, a restore, a copy or an admitted
    filing's consequence -- enumerated here with the reason, so a new writer
    lands on this test before it lands on the fences. Found by AST over the
    dashboard package, attributed to the top-level function; the inventory
    below is the whole population on the head this class was written against.
    """

    ROOT = Path(chat_folders.__file__).resolve().parent
    CHOKEPOINT = (
        "refuse_filing_across_inheritance",
        "file_slot_across_inheritance",
        "filing_crosses_inheritance",
        "_refuse_child_filing_across_inheritance",
        "_file_child_or_retract",
    )
    #: Request-driven writers: each must reference a chokepoint name in its source.
    THROUGH_THE_DECISION = {
        ("chat_folders.py", "api_chat_slot_folder"),
        ("chat_folders.py", "file_slot_across_inheritance"),
        ("chat_handlers.py", "api_chat_slot_create"),
        ("session_control.py", "create_session"),
        ("session_control.py", "_file_child_or_retract"),
        ("session_control.py", "fork_session"),
    }
    #: Writers that name no folder from a request, with why each is not the decision's.
    NOT_A_REQUEST_FILING = {
        ("channel_slots.py", "surface_channel_session"): (
            "a channel conversation's arrival filing from the channel's configured folder, "
            "and the restore of a persisted placement"
        ),
        ("channel_slots.py", "backfill_channel_folder"): (
            "backfills the configured channel folder onto conversations already surfaced"
        ),
        ("chat_folders.py", "api_chat_folder_delete"): (
            "unfiles the sessions of a folder being deleted; the request names no destination"
        ),
        ("chat_fork.py", "fork_slot"): (
            "copies the source's own placement onto the fork; a request-named folder is decided "
            "by fork_session before this runs"
        ),
        ("chat_handlers.py", "api_chat_slot_agent"): (
            "re-resolves the binding of an already-admitted filing on an agent switch; the "
            "project comes from the folder chain, never from the request"
        ),
        ("chat_handlers.py", "api_chat_slot_workspace"): (
            "a workspace switch sets that workspace's default project; no folder is named"
        ),
        ("chat_handlers.py", "api_chat_slot_project"): (
            "the session's OWN project from the request -- the person's arm by name, every "
            "other principal through the fenced resolve; no folder is named"
        ),
        ("chat_handlers.py", "_hydrate_slot_from_history"): "a restore from persisted metadata",
        (
            "chat_persistence.py",
            "_rehydrate_slot_from_history",
        ): "a restore from persisted metadata",
        ("chat_persistence.py", "_apply_recent_session"): "a restore from persisted metadata",
        ("cron_inject.py", "_commit_cron_tab_placement"): (
            "the cron run's tab placed into its job's folder by the cron machinery; no request "
            "names a chat folder here"
        ),
        ("handlers/members.py", "api_member_thread"): (
            "the member thread's project from the member's own configured workspace"
        ),
        ("session_directive_apply.py", "_set_project"): (
            "the agent's OWN session project through the fenced resolve; no folder is named"
        ),
        ("session_transfer.py", "_install_arrived_bundle"): (
            "a transferred session's arrival folder from this gateway's configuration"
        ),
    }

    @classmethod
    def _writers(cls) -> dict[tuple[str, str], str]:
        import ast

        found: dict[tuple[str, str], str] = {}
        for path in sorted(cls.ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            rel = path.relative_to(cls.ROOT).as_posix()
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                writes = any(
                    isinstance(sub, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr in ("folder_id", "project")
                        for t in sub.targets
                    )
                    for sub in ast.walk(node)
                )
                if writes:
                    found[(rel, node.name)] = ast.get_source_segment(source, node) or ""
        return found

    def test_the_population_is_the_two_lists_and_nothing_else(self) -> None:
        writers = self._writers()
        expected = set(self.THROUGH_THE_DECISION) | set(self.NOT_A_REQUEST_FILING)
        unaccounted = sorted(set(writers) - expected)
        assert not unaccounted, (
            "a function now writes a session's folder or project without a place in this "
            "test: route it through refuse_filing_across_inheritance (a request naming a "
            f"folder) or add it to NOT_A_REQUEST_FILING with the reason -- {unaccounted}"
        )
        gone = sorted(expected - set(writers))
        assert not gone, f"listed writers no longer write those fields; prune them: {gone}"

    def test_every_request_driven_writer_takes_the_one_decision(self) -> None:
        writers = self._writers()
        for key in sorted(self.THROUGH_THE_DECISION):
            source = writers[key]
            assert any(name in source for name in self.CHOKEPOINT), (
                f"{key} writes a session's folder from a request without the one filing " "decision"
            )
        for key, reason in self.NOT_A_REQUEST_FILING.items():
            assert reason.strip(), f"{key} needs its reason"
