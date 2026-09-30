"""Agents never fork a duplicate sidebar folder and never leave an empty one.

A person reported a second top-level ``Ops`` folder, an empty
``mockforge-service-builder`` under it, and a stray top-level session, all
made by a conductor that sat in the person's own ``Ops``. Three code paths
produced that shape, and each has a case here:

* a crew member reads only the folders it owns, so its mkdir -p walk did not
  see the person's ``Ops`` and created a second one beside it;
* two walks racing on the same path each read a tree without the segment and
  both created it;
* ``session_create`` created its folder path first, so a create the gateway
  then refused left the new folders empty.

The MCP half is driven through ``_call_tool_inner`` with the HTTP helpers
patched; the gateway half through the real folder endpoint and the real
``create_session``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_folders, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_folders import api_chat_folder_create, create_folder_record
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.mcp_dashboard import _call_tool_inner

_CALLER = "dashboard:chat-1-100"
_OPS = {"id": "0000000000a1", "name": "Ops", "parent_id": ""}
_CALLER_ROW = {"key": "chat-1-100", "title": "Conductor", "folder_id": _OPS["id"]}


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_CALLER):
        yield


@pytest.fixture(autouse=True)
def _custom_sort() -> Any:
    with patch("kiro_crew.mcp_dashboard._read_folder_sort_setting", return_value="custom"):
        yield


class _Gateway:
    """A folder store and a create route, as the MCP server sees them over HTTP.

    ``visible`` filters what GET returns, which is how a crew member's view
    differs from the person's. ``create_error`` makes the session create refuse,
    the dry run and the real call alike, as the gateway's own gate would.
    """

    def __init__(
        self,
        folders: list[dict],
        *,
        visible: Any = None,
        create_error: str = "",
        on_folder_post: Any = None,
    ) -> None:
        self.folders = [dict(f) for f in folders]
        self.visible = visible or (lambda f: True)
        self.create_error = create_error
        self.on_folder_post = on_folder_post
        self.folder_posts: list[dict] = []
        self.create_posts: list[dict] = []
        self._n = 0

    def get(self, path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in self.folders if self.visible(f)]
        if path == "/api/chat/slots":
            return [dict(_CALLER_ROW)]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict, **_kw: Any) -> dict:
        if path == "/api/chat/folders":
            self.folder_posts.append(dict(body))
            if self.on_folder_post is not None:
                self.on_folder_post(self, body)
            # The endpoint's under-lock rule for agent callers, whoever owns the
            # sibling it collides with.
            name = body["name"].strip().casefold()
            if any(
                f["parent_id"] == body["parent_id"] and f["name"].strip().casefold() == name
                for f in self.folders
            ):
                return {"error": "exists", "code": "folder_name_exists"}
            self._n += 1
            row = {
                "id": f"00000000f{self._n:03d}",
                "name": body["name"],
                "parent_id": body["parent_id"],
            }
            self.folders.append(row)
            return dict(row)
        if path == "/api/session-control/create":
            self.create_posts.append(dict(body))
            if self.create_error:
                return {"error": self.create_error, "code": "agent_not_found"}
            if body.get("dry_run"):
                return {"dry_run": True}
            return {
                "target": "chat-9-900",
                "title": body.get("title"),
                "folder_id": body.get("folder_id", ""),
            }
        raise AssertionError(f"unexpected POST {path}")

    def run(self, tool: str, args: dict) -> str:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self.get),
            patch("kiro_crew.mcp_dashboard._post", side_effect=self.post),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}),
        ):
            return _call_tool_inner(tool, args)

    def named(self, name: str) -> list[dict]:
        return [f for f in self.folders if f["name"] == name]


# ── MCP: session_create ──────────────────────────────────────────────────────


def test_a_refused_create_leaves_no_empty_folder_behind() -> None:
    """The create is checked BEFORE the path is made.

    Mutation guard: drop the preflight and the walk creates
    ``mockforge-service-builder`` under Ops before the refusal lands.
    """
    gw = _Gateway([_OPS], create_error="unknown agent")
    out = gw.run(
        "session_create",
        {"title": "worker", "agent": "nope", "folder": "Ops/mockforge-service-builder"},
    )
    assert out.startswith("Error:")
    assert "no folder was created" in out
    assert gw.folder_posts == [], "a refused create must not create any folder"
    assert len(gw.create_posts) == 1 and gw.create_posts[0]["dry_run"] is True


def test_the_dry_run_names_the_deepest_existing_folder() -> None:
    """The probe runs the create against the folder the new path hangs from."""
    gw = _Gateway([_OPS])
    gw.run("session_create", {"title": "worker", "folder": "Ops/kirocrew-worker"})
    probe, real = gw.create_posts
    assert probe == {"title": "worker", "agent": "", "dry_run": True, "folder_id": _OPS["id"]}
    assert "dry_run" not in real
    assert real["folder_id"] == gw.named("kirocrew-worker")[0]["id"]


def test_an_existing_path_needs_no_preflight() -> None:
    """Nothing would be created, so the one create call is the only call."""
    worker = {"id": "0000000000b2", "name": "kirocrew-worker", "parent_id": _OPS["id"]}
    gw = _Gateway([_OPS, worker])
    gw.run("session_create", {"title": "worker", "folder": "Ops/kirocrew-worker"})
    assert gw.folder_posts == []
    assert [("dry_run" in b) for b in gw.create_posts] == [False]
    assert gw.create_posts[0]["folder_id"] == worker["id"]


def test_a_conductor_in_the_persons_folder_nests_its_workers_there() -> None:
    """The reported structure: workers go UNDER the person's Ops, not a fork."""
    gw = _Gateway([_OPS])
    out = gw.run("session_create", {"title": "worker", "folder": "Ops/kirocrew-worker"})
    assert not out.startswith("Error:"), out
    assert len(gw.named("Ops")) == 1, "a second Ops must never be created"
    (leaf,) = gw.named("kirocrew-worker")
    assert leaf["parent_id"] == _OPS["id"]


def test_a_lost_race_reuses_the_winners_folder() -> None:
    """Two parallel creates on one path: the loser files into the winner's folder.

    The concurrent walk's append lands between this walk's read and its POST,
    so the endpoint refuses the duplicate and the walk re-reads the tree.
    """

    def _winner_first(gw: _Gateway, body: dict) -> None:
        if body["name"] == "kirocrew-worker" and not gw.named("kirocrew-worker"):
            gw.folders.append(
                {"id": "0000000000c3", "name": "kirocrew-worker", "parent_id": _OPS["id"]}
            )

    gw = _Gateway([_OPS], on_folder_post=_winner_first)
    out = gw.run("session_create", {"title": "worker", "folder": "Ops/kirocrew-worker"})
    assert not out.startswith("Error:"), out
    assert len(gw.named("kirocrew-worker")) == 1
    assert gw.create_posts[-1]["folder_id"] == "0000000000c3"
    assert "created folder path" not in out, "this call created nothing"


def test_a_same_name_folder_the_caller_cannot_see_is_refused_not_forked() -> None:
    """A crew member's view omits the person's Ops; it must not mint a second one.

    Mutation guard: treat ``folder_name_exists`` like any other create error
    and nothing changes here; drop the endpoint rule and a second Ops appears.
    """
    gw = _Gateway([_OPS], visible=lambda f: f["id"] != _OPS["id"])
    out = gw.run("session_create", {"title": "worker", "folder": "Ops/mockforge-service-builder"})
    assert out.startswith("Error:")
    assert "no duplicate was created" in out
    assert len(gw.named("Ops")) == 1
    assert gw.named("mockforge-service-builder") == []
    assert all(b.get("dry_run") for b in gw.create_posts), "no session may be created"


def test_file_self_into_an_invisible_same_name_folder_is_refused() -> None:
    """``chat_folder_file_self`` shares the walk, so it shares the refusal."""
    gw = _Gateway([_OPS], visible=lambda f: f["id"] != _OPS["id"])
    out = gw.run("chat_folder_file_self", {"folder": "Ops"})
    assert out.startswith("Error:")
    assert "no duplicate was created" in out
    assert len(gw.named("Ops")) == 1


# ── Gateway: the folder endpoint ─────────────────────────────────────────────


def _folder_state(folders: list[dict[str, Any]]) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    state.mutate_folders = _mutate
    return state


async def _post_folder(state: DashboardState, body: dict, *, internal: bool) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    headers = {"X-Session-Key": _CALLER}
    if internal:
        headers |= {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/folders", json=body, headers=headers)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_agent_cannot_create_a_same_name_sibling() -> None:
    """A twin owned by another principal is refused, never forked."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS, owner_app="member:someone-else")]
    status, body = await _post_folder(
        _folder_state(folders), {"name": " ops ", "parent_id": ""}, internal=True
    )
    assert status == 409
    assert body["code"] == "folder_name_exists"
    assert len(folders) == 1


@pytest.mark.asyncio
async def test_an_agent_reuses_its_own_principals_twin() -> None:
    """A person-level agent naming the person's own folder gets that folder back."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS)]
    status, body = await _post_folder(
        _folder_state(folders), {"name": " ops ", "parent_id": ""}, internal=True
    )
    assert status == 200 and body["id"] == _OPS["id"] and body["reused"] is True
    assert len(folders) == 1


@pytest.mark.asyncio
async def test_the_person_may_still_name_two_folders_alike() -> None:
    folders = [dict(_OPS)]
    status, _body = await _post_folder(
        _folder_state(folders), {"name": "Ops", "parent_id": ""}, internal=False
    )
    assert status == 201
    assert len(folders) == 2


@pytest.mark.asyncio
async def test_the_name_rule_is_per_parent() -> None:
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS)]
    status, _body = await _post_folder(
        _folder_state(folders), {"name": "Ops", "parent_id": _OPS["id"]}, internal=True
    )
    assert status == 201


@pytest.mark.asyncio
async def test_the_sibling_test_runs_under_the_lock() -> None:
    """A sibling appended after admission, before the lock, is still seen.

    Mutation guard: hoist the check above ``mutate_folders`` and both land.
    """
    state = _folder_state([])
    real = state.mutate_folders

    async def _racing(fn: Any, on_committed: Any = None) -> Any:
        state._folders.append(
            {"id": "0000000000d4", "name": "Ops", "parent_id": "", "owner_app": "some-app"}
        )
        return await real(fn, on_committed)

    state.mutate_folders = _racing
    # Resolved by name so an unfixed tree fails HERE, naming the missing rule,
    # rather than at collection.
    name_exists = getattr(chat_folders, "FolderNameExistsError", None)
    assert name_exists is not None, "the folder store has no same-name refusal"
    with pytest.raises(name_exists):
        await create_folder_record(state, name="Ops", refuse_duplicate_name=True)
    assert len(state._folders) == 1


# ── Gateway: create_session(dry_run=True) ────────────────────────────────────


@pytest.fixture
def _session_control_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def test_a_dry_run_mints_nothing_and_spends_no_budget(tmp_path, _session_control_on) -> None:
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    before = state.live_slot_count()
    for _ in range(create_rate_limit.MAX_SESSION_CREATES_PER_WINDOW + 2):
        result = asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), dry_run=True)
        )
        assert result == {"dry_run": True}
    assert state.live_slot_count() == before
    # The budget is untouched, so the real create the dry run previewed lands.
    real = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
    assert state.get_slot(real["target"]) is not None


def test_a_dry_run_carries_the_real_refusal(tmp_path, _session_control_on) -> None:
    state = _make_state(tmp_path)
    ghost = state.get_or_create_slot("chat-ghost")
    ghost.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(ghost), dry_run=True)
        )
    assert exc.value.code == "ephemeral_caller"


def test_the_route_refuses_a_non_boolean_dry_run(tmp_path, _session_control_on) -> None:
    """``"false"`` is truthy; a real create must never become a silent preview."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/create"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json() -> dict:
        return {"title": "w", "dry_run": "false"}

    request.json = _json
    before = state.live_slot_count()
    resp = asyncio.run(handlers_sc.api_session_control_create(request))
    assert resp.status == 400
    assert state.live_slot_count() == before


# ── Gateway: an agent nests under the folder its own session is filed in ────

_MEMBER = "member:crew-conductor"
_OTHER = {"id": "0000000000e5", "name": "Canaries", "parent_id": ""}
_SUB = {"id": "0000000000f6", "name": "Sub", "parent_id": _OPS["id"]}


def _home_state(folders: list[dict[str, Any]], *, filed_in: str = _OPS["id"]) -> DashboardState:
    state = _folder_state(folders)
    state._slots["chat-1-100"].folder_id = filed_in
    return state


def _member_app(state: DashboardState, *, member: str = _MEMBER) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _as_member(request: web.Request, handler: Any) -> Any:
        # What the chat gate stamps on an admitted member's verified request.
        request["app"] = ""
        request[MEMBER_CHAT_PRINCIPAL_KEY] = member
        return await handler(request)

    app.middlewares.append(_as_member)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_get("/api/chat/folders", chat_folders.api_chat_folders)
    return app


_INTERNAL = {
    "X-Session-Key": _CALLER,
    "X-Internal-Secret": "s3cret",
    "X-Internal-Caller": "kirocrew-dashboard",
}


async def _member_post(state: DashboardState, body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_member_app(state))) as client:
        resp = await client.post("/api/chat/folders", json=body, headers=_INTERNAL)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_member_in_the_persons_folder_creates_its_subfolder_once() -> None:
    """Conductor in the person's Ops: ``Ops/<agent>`` is made once, then reused.

    Mutation guard: drop the home-folder allowance and the first create is 403.
    """
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS)]
    state = _home_state(folders)
    status, made = await _member_post(state, {"name": "kirocrew-worker", "parent_id": _OPS["id"]})
    assert status == 201, made
    assert made["owner_app"] == _MEMBER, "the new subfolder belongs to the agent"
    status, again = await _member_post(state, {"name": "kirocrew-worker", "parent_id": _OPS["id"]})
    assert status == 200, again
    assert again["id"] == made["id"] and again["reused"] is True
    assert [f["name"] for f in folders].count("kirocrew-worker") == 1
    assert all("reused" not in f for f in folders), "the flag is never persisted"


@pytest.mark.asyncio
async def test_parallel_member_creates_land_one_folder() -> None:
    """Two creates of ``Ops/<agent>`` at once: one folder, both get its id."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS)]
    state = _home_state(folders)
    (s1, a), (s2, b) = await asyncio.gather(
        _member_post(state, {"name": "kirocrew-worker", "parent_id": _OPS["id"]}),
        _member_post(state, {"name": "kirocrew-worker", "parent_id": _OPS["id"]}),
    )
    assert sorted([s1, s2]) == [200, 201]
    assert a["id"] == b["id"]
    assert [f["name"] for f in folders].count("kirocrew-worker") == 1


@pytest.mark.asyncio
async def test_other_person_folders_are_still_refused() -> None:
    """Only the caller's own folder opens: not a sibling, not a deeper child."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS), dict(_OTHER), dict(_SUB)]
    state = _home_state(folders)
    for parent in (_OTHER["id"], _SUB["id"]):
        status, body = await _member_post(state, {"name": "x", "parent_id": parent})
        assert status == 403, (parent, body)
    assert len(folders) == 3


@pytest.mark.asyncio
async def test_a_person_owned_twin_under_the_home_folder_is_not_reused() -> None:
    """Reuse is for the agent's own folder only; the person's is refused."""
    create_rate_limit.reset_for_tests()
    twin = {"id": "0000000000a7", "name": "kirocrew-worker", "parent_id": _OPS["id"]}
    folders = [dict(_OPS), twin]
    status, body = await _member_post(
        _home_state(folders), {"name": "kirocrew-worker", "parent_id": _OPS["id"]}
    )
    assert status == 409 and body["code"] == "folder_name_exists"


@pytest.mark.asyncio
async def test_an_app_cannot_borrow_another_sessions_folder() -> None:
    """The home slot must be the caller's own: an app naming a person's session
    in ``X-Session-Key`` gets no allowance from that session's folder."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS)]
    state = _home_state(folders)
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _as_app(request: web.Request, handler: Any) -> Any:
        request["app"] = "some-app"
        return await handler(request)

    app.middlewares.append(_as_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "w", "parent_id": _OPS["id"]}, headers=_INTERNAL
        )
        assert resp.status == 403
    state._slots["chat-1-100"]._app = "some-app"
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "w", "parent_id": _OPS["id"]}, headers=_INTERNAL
        )
        assert resp.status == 201, "the app's OWN session does open its folder"


@pytest.mark.asyncio
async def test_a_member_sees_its_home_folder_chain_and_nothing_else() -> None:
    """The list adds the folder the member's session sits in and its ancestors."""
    folders = [dict(_OPS), dict(_OTHER), dict(_SUB)]
    state = _home_state(folders, filed_in=_SUB["id"])
    with patch.object(chat_folders, "_folders_with_history_counts", lambda _s: list(folders)):
        async with TestClient(TestServer(_member_app(state))) as client:
            resp = await client.get("/api/chat/folders", headers=_INTERNAL)
            seen = {f["id"] for f in await resp.json()}
    assert seen == {_OPS["id"], _SUB["id"]}


def test_the_walk_takes_a_reused_folder_as_found_not_created() -> None:
    """A 200 reuse from the endpoint is a match, never a "created" segment."""
    winner = {"id": "0000000000b8", "name": "kirocrew-worker", "parent_id": _OPS["id"]}

    def _post(path: str, body: dict, **_kw: Any) -> dict:
        if path == "/api/chat/folders":
            return {**winner, "reused": True}
        if body.get("dry_run"):
            return {"dry_run": True}
        return {"target": "chat-9-900", "title": "w", "folder_id": body.get("folder_id")}

    gw = _Gateway([_OPS])
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=gw.get),
        patch("kiro_crew.mcp_dashboard._post", side_effect=_post) as post,
    ):
        out = _call_tool_inner("session_create", {"title": "w", "folder": "Ops/kirocrew-worker"})
    assert "created folder path" not in out
    assert post.call_args_list[-1].args[1]["folder_id"] == winner["id"]


# ── The preview misses nothing the real create refuses ───────────────────────


def test_a_long_later_segment_creates_nothing() -> None:
    """``New/<too long>``: the length refusal comes before ``New`` is made."""
    gw = _Gateway([_OPS])
    out = gw.run("session_create", {"title": "w", "folder": "New/" + "x" * 101})
    assert out.startswith("Error:") and "too long" in out
    assert gw.folder_posts == [] and gw.create_posts == []


def test_a_spent_create_budget_refuses_the_dry_run(tmp_path, _session_control_on) -> None:
    """A caller out of create budget is refused by the preview, so no folder
    is made for a create the rate guard would then refuse."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    for _ in range(create_rate_limit.MAX_SESSION_CREATES_PER_WINDOW):
        assert create_rate_limit.allow_create(create_rate_limit.SESSION_CREATE, caller.key)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=slot_history_key(caller), dry_run=True)
        )
    assert exc.value.code == "create_rate_limited"


@pytest.mark.asyncio
async def test_a_refused_duplicate_is_audited() -> None:
    """The 409 leaves a denied SEL line, like the ownership refusal beside it."""
    create_rate_limit.reset_for_tests()
    folders = [dict(_OPS, owner_app="member:someone-else")]
    fake_sel = MagicMock()
    with patch.object(chat_folders, "sel", return_value=fake_sel):
        status, _body = await _post_folder(
            _folder_state(folders), {"name": "Ops", "parent_id": ""}, internal=True
        )
    assert status == 409
    outcomes = [c.kwargs.get("outcome") for c in fake_sel.log_api_access.call_args_list]
    assert "denied" in outcomes
