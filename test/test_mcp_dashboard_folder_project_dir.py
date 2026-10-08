"""``project_dir`` on the folder routes and tools, driven through the REAL routes."""

from __future__ import annotations

import asyncio
import errno
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import (
    _make_app_with_agent_routes,
    _make_folder_app,
    _make_state,
    pin_the_owners_store_step,
    stamp_the_person,
)
from dashboard_owner_helpers import as_owner

from conftest import requires_symlinks
from kiro_crew.dashboard.chat_folders import (
    _resolve_folder_project_dir,
    create_folder_record,
)
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import Caller, ToolContext

CALLER = "chat-1-100"


class _Bridge:
    """Main's ``DashboardClient`` port over an aiohttp ``TestClient``: an error status becomes ``DashboardError``, as in the gateway."""

    def __init__(self, client: TestClient, loop: asyncio.AbstractEventLoop) -> None:
        self._client = client
        self._loop = loop
        self.default_session_key: str | None = None

    async def _request(
        self, method: str, path: str, body: dict | None, session_key: str | None
    ) -> Any:
        headers = {"X-Session-Key": session_key} if session_key else {}
        resp = await self._client.request(method, path, json=body, headers=headers)
        payload = await resp.json()
        if resp.status >= 400:
            return {"error": str(payload.get("error")), "code": str(payload.get("code") or "")}
        return payload

    def _run(self, method: str, path: str, body: dict | None, session_key: str | None) -> Any:
        if session_key is None:
            session_key = self.default_session_key
        reply = asyncio.run_coroutine_threadsafe(
            self._request(method, path, body, session_key), self._loop
        ).result(timeout=30)
        if isinstance(reply, dict) and reply.get("error"):
            raise DashboardError(reply)
        return reply

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any:
        return self._run("GET", path, None, session_key)

    def post(
        self,
        path: str,
        body: dict | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self._run("POST", path, body or {}, session_key)

    def patch(self, path: str, body: dict | None = None, *, session_key: str | None = None) -> Any:
        return self._run("PATCH", path, body or {}, session_key)

    def put(self, path: str, body: dict | None = None, *, session_key: str | None = None) -> Any:
        return self._run("PUT", path, body or {}, session_key)

    def delete(self, path: str, body: dict | None = None, *, session_key: str | None = None) -> Any:
        return self._run("DELETE", path, body or {}, session_key)


async def _call(
    bridge: _Bridge, name: str, args: dict[str, Any], *, caller_key: str = f"dashboard:{CALLER}"
) -> str:
    """Run one tool call on a worker thread against the live routes, as a verified caller."""
    bridge.default_session_key = caller_key
    ctx = ToolContext(client=bridge, caller=Caller.strict(caller_key))
    return await asyncio.to_thread(TABLE.call, name, args, ctx)


def _folder(state: Any, fid: str) -> dict[str, Any]:
    return next(f for f in state._folders if f["id"] == fid)


def _created_id(out: str) -> str:
    assert "(id=" in out, out
    return out.split("(id=", 1)[1].split(")", 1)[0]


MEMBER = "member:reviewer-store"


def _member_folder_app(state: Any, principal: str = MEMBER) -> web.Application:
    """The folder routes as an admitted crew MEMBER reaches them."""
    app = _make_folder_app(state)

    @web.middleware
    async def _stamp_member(request: web.Request, handler: Any) -> Any:
        request[MEMBER_CHAT_PRINCIPAL_KEY] = principal
        return await handler(request)

    app.middlewares.append(_stamp_member)
    return app


async def _person_binds(state: Any, name: str, project_dir: str, parent_id: str = "") -> str:
    """A folder the PERSON bound from the sidebar, as the store holds it."""
    folder = await create_folder_record(
        state,
        name=name,
        parent_id=parent_id,
        project_dir=project_dir,
        request_app="",
    )
    return str(folder["id"])


def _pin_slot_create_defaults(monkeypatch: Any, tmp_path: Any) -> str:
    """The pins the route's own inheritance test uses (test_dashboard_chat)."""
    mock_cfg = MagicMock()
    mock_cfg.dashboard.default_project = ""
    mock_cfg.default_agent = ""
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg)
    fallback = str(tmp_path / "workspace-default")
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.default_project_dir", lambda _workspace: fallback
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", lambda *_args, **_kwargs: None
    )
    pin_the_owners_store_step(monkeypatch)
    return fallback


@pytest.fixture
def state(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    # The caller's own live slot: the tree-shaping gate scopes the caller by
    # finding this row, and the routes refuse a ``dashboard:`` key naming a slot
    # that is gone. Created with no app, so the caller is an ordinary session.
    st.get_or_create_slot(CALLER)
    return st


class TestProjectDirThroughTheRealRoutes:
    """Where a folder's project directory comes from and what it confers, observed end to end."""

    @pytest.mark.asyncio
    async def test_a_tool_call_naming_project_dir_is_refused_before_any_request(
        self, state: Any, tmp_path: Any
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            bridge = _Bridge(client, asyncio.get_running_loop())
            # The table validates before any request and never raises a refusal:
            # the schema's answer is the frame's whole reply.
            out = await _call(
                bridge, "chat_folder_create", {"name": "Proj", "project_dir": str(proj)}
            )
        assert out.startswith("Error:") and "project_dir" in out, out
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_the_routes_refuse_an_agents_binding_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Straight at the routes on the internal transport (the tools cannot carry the field)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Theirs", str(proj))
        headers = {"X-Session-Key": f"dashboard:{CALLER}"}
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            created = await client.post(
                "/api/chat/folders",
                json={"name": "Bound", "project_dir": str(proj)},
                headers=headers,
            )
            created_body = await created.json()
            cleared = await client.patch(
                f"/api/chat/folders/{fid}", json={"project_dir": ""}, headers=headers
            )
        assert created.status == 403, created_body
        assert created_body["code"] == "folder_project_dir_forbidden"
        assert "the person binds a folder from the sidebar" in created_body["error"]
        assert cleared.status == 403
        assert not any(f["name"] == "Bound" for f in state._folders)
        assert _folder(state, fid)["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_a_session_created_in_a_folder_the_person_bound_inherits_the_binding(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The point of the feature for the person, observed."""
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Proj", str(proj))
        assert _folder(state, fid)["project_dir"] == os.path.realpath(str(proj))

        _pin_slot_create_defaults(monkeypatch, tmp_path)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "in-proj", "folder_id": fid})
            data = await resp.json()
        assert resp.status == 200, data
        assert data["folder_id"] == fid
        assert data["project"] == os.path.realpath(str(proj))
        assert state._slots["in-proj"].project == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_no_agent_moves_a_folder_across_a_binding(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Through ``chat_folder_move``: the person binds one folder and files a chat in."""
        proj = tmp_path / "proj"
        proj.mkdir()
        bound = await _person_binds(state, "Bound", str(proj))
        member_bound = await create_folder_record(
            state, name="Member bound", project_dir=str(proj), request_app=MEMBER
        )
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            bridge = _Bridge(client, asyncio.get_running_loop())
            work = _created_id(await _call(bridge, "chat_folder_create", {"name": "Work"}))
            theirs = state.get_or_create_slot("chat-2-200")
            theirs.folder_id = work
            for caller_key in (f"dashboard:{CALLER}", "channel:chan-000001:helper"):
                out = await _call(
                    bridge,
                    "chat_folder_move",
                    {"folder": "Work", "new_parent": "Bound"},
                    caller_key=caller_key,
                )
                assert out.startswith(
                    "Error: an agent cannot move a folder where its sessions would inherit a "
                    "different project directory"
                ), out
        async with TestClient(TestServer(_member_folder_app(state))) as client:
            bridge = _Bridge(client, asyncio.get_running_loop())
            radar = _created_id(await _call(bridge, "chat_folder_create", {"name": "Radar output"}))
            filed = state.get_or_create_slot("chat-3-300")
            filed.folder_id = radar
            out = await _call(
                bridge, "chat_folder_move", {"folder": "Radar output", "new_parent": "Member bound"}
            )
        assert out.startswith("Error: an agent cannot move a folder"), out
        assert _folder(state, work)["parent_id"] == ""
        assert _folder(state, radar)["parent_id"] == ""
        assert _resolve_folder_project_dir(state._folders, work) == ("", None)
        assert _folder(state, bound)["project_dir"] == os.path.realpath(str(proj))
        assert _folder(state, str(member_bound["id"]))["project_dir"] == os.path.realpath(str(proj))


class TestADashboardUserTokenAloneIsNotThePerson:
    """The fences ask WHO holds the dashboard-user token, not only that one validated."""

    LINK_HOLDER = {"X-Test-User": "U0ALLOWED"}

    def test_the_predicate_asks_who_holds_the_token(self, state: Any) -> None:
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard import chat_folders as cf

        app = web.Application()
        app["state"] = state

        def _request(**claims: Any) -> web.Request:
            request = make_mocked_request("PATCH", "/api/chat/folders/f", app=app)
            for key, value in claims.items():
                request[key] = value
            return request

        # No owner configured: the machine-local bootstrap subject IS the owner;
        # another subject with the very same stamp is not; the stamp is still
        # required (the internal transport carries the claims and no stamp).
        assert cf._is_the_person(_request(is_dashboard_user=True, app="", user="local-app"))
        assert cf._is_the_person(_request(is_dashboard_user=True, app="", user="local-startup"))
        assert not cf._is_the_person(_request(is_dashboard_user=True, app="", user="U0ALLOWED"))
        assert not cf._is_the_person(_request(app="", user="local-app"))
        assert not cf._is_the_person(
            _request(is_dashboard_user=False, app="design-critique", user="local-app")
        )
        # An owner configured: exactly that subject; the bootstrap subjects are
        # another caller there (a session signed in before the owner was
        # configured keeps its bootstrap subject for life -- its refusals are
        # relabelled to the sign-in-again answer, never admitted; see the
        # stale-session test below).
        state.owner_id = "U0OWNER"
        assert cf._is_the_person(_request(is_dashboard_user=True, app="", user="U0OWNER"))
        assert not cf._is_the_person(_request(is_dashboard_user=True, app="", user="local-app"))
        assert not cf._is_the_person(_request(is_dashboard_user=True, app="", user="local-startup"))
        assert not cf._is_the_person(_request(is_dashboard_user=True, app="", user="U0ALLOWED"))

    @pytest.mark.asyncio
    async def test_an_allow_listed_users_dashboard_link_binds_nothing(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Through the folder routes with the person's harness."""
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(_make_folder_app(state, dashboard_user=True))) as client:
            bound = await client.post(
                "/api/chat/folders",
                json={"name": "Theirs", "project_dir": str(proj)},
                headers=self.LINK_HOLDER,
            )
            assert bound.status == 403, await bound.text()
            assert (await bound.json())["code"] == "folder_project_dir_forbidden"
            plain = await client.post(
                "/api/chat/folders", json={"name": "Theirs"}, headers=self.LINK_HOLDER
            )
            assert plain.status == 201, await plain.text()
            fid = (await plain.json())["id"]
            patched = await client.patch(
                f"/api/chat/folders/{fid}",
                json={"project_dir": str(proj)},
                headers=self.LINK_HOLDER,
            )
            assert patched.status == 403, await patched.text()
            assert (await patched.json())["code"] == "folder_project_dir_forbidden"
            assert _folder(state, fid)["project_dir"] == ""
            owner = await client.patch(f"/api/chat/folders/{fid}", json={"project_dir": str(proj)})
            assert owner.status == 200, await owner.text()
        assert [f["id"] for f in state._folders if f["name"] == "Theirs"] == [fid]
        assert _folder(state, fid)["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_an_allow_listed_users_dashboard_link_files_no_session_under_a_binding(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The slot-create filing (the one decision every filing route takes)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Proj", str(proj))
        _pin_slot_create_defaults(monkeypatch, tmp_path)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            theirs = await client.post(
                "/api/chat/slots",
                json={"name": "theirs", "folder_id": fid},
                headers=self.LINK_HOLDER,
            )
            assert theirs.status == 403, await theirs.text()
            assert (await theirs.json())["code"] == "folder_project_dir_forbidden"
            owners = await client.post("/api/chat/slots", json={"name": "owners", "folder_id": fid})
            assert owners.status == 200, await owners.text()
        assert "theirs" not in state._slots
        assert state._slots["owners"].project == os.path.realpath(str(proj))

    STALE_OPERATOR = {"X-Test-User": "local-startup"}

    @pytest.mark.asyncio
    async def test_a_session_signed_in_before_the_owner_was_configured_is_told_to_sign_in_again(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """A token's subject is fixed at mint (``owner_id or <bootstrap subject>``) and every."""
        from kiro_crew.dashboard.handlers.source_providers import STALE_OWNER_SESSION_CODE

        state.owner_id = "U0OWNER"
        proj = tmp_path / "proj"
        proj.mkdir()

        async def _reauth(resp: Any) -> None:
            assert resp.status == 401, await resp.text()
            assert (await resp.json())["code"] == STALE_OWNER_SESSION_CODE

        async with TestClient(TestServer(_make_folder_app(state, dashboard_user=True))) as client:
            await _reauth(
                await client.post(
                    "/api/chat/folders",
                    json={"name": "Stale", "project_dir": str(proj)},
                    headers=self.STALE_OPERATOR,
                )
            )
            plain = await client.post(
                "/api/chat/folders", json={"name": "Stale"}, headers=self.STALE_OPERATOR
            )
            assert plain.status == 201, await plain.text()
            fid = (await plain.json())["id"]
            await _reauth(
                await client.patch(
                    f"/api/chat/folders/{fid}",
                    json={"project_dir": str(proj)},
                    headers=self.STALE_OPERATOR,
                )
            )
            # The same request from the other two non-person holders of a
            # dashboard-shaped credential keeps the agent's 403.
            for holder in (self.LINK_HOLDER, {"X-Test-App": "design-critique"}):
                other = await client.patch(
                    f"/api/chat/folders/{fid}", json={"project_dir": str(proj)}, headers=holder
                )
                assert other.status == 403, await other.text()
                assert (await other.json())["code"] == "folder_project_dir_forbidden"
        assert _folder(state, fid)["project_dir"] == ""

        bound = await _person_binds(state, "Proj", str(proj))
        _pin_slot_create_defaults(monkeypatch, tmp_path)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            await _reauth(
                await client.post(
                    "/api/chat/slots",
                    json={"name": "stale", "folder_id": bound},
                    headers=self.STALE_OPERATOR,
                )
            )
        assert "stale" not in state._slots

    @pytest.mark.asyncio
    async def test_a_refusal_is_audited_against_attested_identity_never_a_bare_header(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """``_agent_audit_caller`` names the refused caller from attested facts only."""
        import kiro_crew.dashboard.chat_folders as cf

        state.owner_id = "U0OWNER"
        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        proj = tmp_path / "proj"
        proj.mkdir()
        forged = {"X-Session-Key": "U0OWNER"}
        # An unverified internal request forging the owner's subject in the header.
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Forged", "project_dir": str(proj)},
                headers=forged,
            )
            assert resp.status == 403, await resp.text()
        assert sel_fn.return_value.log_api_access.call_args.kwargs["caller"] == "unattributable"
        # The same header on a request whose peer the middleware attested.
        sel_fn.reset_mock()
        app = _make_folder_app(state)

        @web.middleware
        async def _attested(request: web.Request, handler: Any) -> Any:
            request["peer_verified"] = True
            return await handler(request)

        app.middlewares.insert(0, _attested)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Attested", "project_dir": str(proj)},
                headers=forged,
            )
            assert resp.status == 403, await resp.text()
        assert sel_fn.return_value.log_api_access.call_args.kwargs["caller"] == "U0OWNER"
        # A validated non-owner subject is the row's caller, header or not.
        sel_fn.reset_mock()
        async with TestClient(TestServer(_make_folder_app(state, dashboard_user=True))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Link", "project_dir": str(proj)},
                headers={**self.LINK_HOLDER, "X-Session-Key": "U0OWNER"},
            )
            assert resp.status == 403, await resp.text()
        assert sel_fn.return_value.log_api_access.call_args.kwargs["caller"] == "U0ALLOWED"
        assert [f for f in state._folders if f["name"] in ("Forged", "Attested", "Link")] == []


#: The three UNC spellings Windows honours: two backslashes, two forward slashes,
#: and the extended-length ``\\?\UNC\`` form. Every one names a HOST.
_UNC_SPELLINGS = (r"\\evil\share\proj", "//evil/share/proj", r"\\?\UNC\evil\share\proj")
_UNC_REFUSAL = "Project directory must not be a network (UNC) path"


class TestAUncProjectDirIsRefusedBeforeAnyFilesystemCall:
    """``project_dir`` from a caller other than the person is path text that reaches the."""

    @pytest.mark.parametrize("unc", _UNC_SPELLINGS)
    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_a_non_person_spelling_is_refused_without_touching_the_filesystem(
        self, monkeypatch: Any, unc: str, platform: str
    ) -> None:
        import sys

        import kiro_crew.dashboard.chat_folders as cf

        touched = MagicMock(side_effect=AssertionError("filesystem touched for a UNC project_dir"))
        sel_fn = MagicMock()
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(os.path, "realpath", touched)
        monkeypatch.setattr(os.path, "isdir", touched)
        monkeypatch.setattr(cf, "is_sensitive_path", touched)
        monkeypatch.setattr(cf.pinned_fs, "real_dir_path_pinned", touched)
        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)
        monkeypatch.setattr(cf, "sel", sel_fn)
        assert cf.project_dir_unc_refusal(unc) == _UNC_REFUSAL
        touched.assert_not_called()
        # The lexical gate itself audits nothing; the sites that run it (the
        # directive, the endpoint's non-person arm) audit their own refusals.
        sel_fn.return_value.log_api_access.assert_not_called()

    @pytest.mark.parametrize("unc", _UNC_SPELLINGS)
    def test_the_persons_spelling_resolves_by_name_as_it_always_did(
        self, monkeypatch: Any, unc: str
    ) -> None:
        """The person's arm is main's code: ``realpath`` then ``isdir`` on the spelling."""
        import kiro_crew.dashboard.chat_folders as cf
        from kiro_crew import pinned_fs

        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)
        seen: list[str] = []
        monkeypatch.setattr(os.path, "realpath", lambda p, **kw: (seen.append(p), p)[1])
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        monkeypatch.setattr(cf, "is_sensitive_path", lambda p: False)
        resolved, err = cf._validate_project_dir(unc)
        assert err != _UNC_REFUSAL
        assert err != cf.PROJECT_DIR_LINK_REFUSAL
        if os.path.isabs(unc):
            assert (resolved, err) == (unc, None)
            assert seen == [unc]
        untouched.assert_not_called()

    def test_the_helper_decides_not_a_second_rule(self, monkeypatch: Any) -> None:
        """When ``unc_probe_allowed`` vouches for the share (the gateway's own data home on a."""
        import kiro_crew.dashboard.chat_folders as cf
        from kiro_crew import pinned_fs

        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: True)
        monkeypatch.setattr(
            pinned_fs, "real_dir_path_pinned", MagicMock(side_effect=FileNotFoundError())
        )
        monkeypatch.setattr(cf, "is_sensitive_path", lambda p: False)
        assert cf.screen_and_resolve_project_dir("//evil/share/proj") == (
            "",
            "Project directory must be an existing directory",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unc", _UNC_SPELLINGS)
    async def test_the_person_binds_a_share_at_both_routes_as_on_main(
        self, state: Any, tmp_path: Any, monkeypatch: Any, unc: str
    ) -> None:
        """Straight at the routes, as the person."""
        import kiro_crew.dashboard.chat_folders as cf

        real_realpath = os.path.realpath
        real_isdir = os.path.isdir

        def _realpath(p: str, **kw: Any) -> str:
            return p if p == unc else real_realpath(p, **kw)

        monkeypatch.setattr(cf.os.path, "realpath", _realpath)
        monkeypatch.setattr(cf.os.path, "isdir", lambda p: True if p == unc else real_isdir(p))
        headers = {"X-Session-Key": f"dashboard:{CALLER}"}
        async with TestClient(TestServer(_make_folder_app(state, dashboard_user=True))) as client:
            created = await client.post(
                "/api/chat/folders", json={"name": "Share", "project_dir": unc}, headers=headers
            )
            body = await created.json()
            if not os.path.isabs(unc):
                # A backslash spelling is not "absolute" on this platform: the
                # ordinary validation's own answer, exactly as on main -- never
                # the UNC refusal.
                assert created.status == 400
                assert body == {"error": "Project directory must be an absolute path"}
                return
            assert created.status == 201, body
            assert _folder(state, body["id"])["project_dir"] == unc
            plain = await client.post(
                "/api/chat/folders",
                json={"name": "Plain", "project_dir": str(tmp_path)},
                headers=headers,
            )
            assert plain.status == 201, await plain.text()
            fid = (await plain.json())["id"]
            updated = await client.patch(
                f"/api/chat/folders/{fid}", json={"project_dir": unc}, headers=headers
            )
            assert updated.status == 200, await updated.text()
            assert _folder(state, fid)["project_dir"] == unc
            cleared = await client.patch(
                f"/api/chat/folders/{fid}", json={"project_dir": ""}, headers=headers
            )
            assert cleared.status == 200, await cleared.text()
        assert _folder(state, fid)["project_dir"] == ""

    @pytest.mark.asyncio
    async def test_the_slot_project_endpoint_scopes_the_rule_by_principal(
        self, state: Any, monkeypatch: Any
    ) -> None:
        """The same endpoint, two principals."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard.chat import api_chat_slot_project

        unc = "//evil/share/proj"
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.screen_and_resolve_project_dir", untouched
        )
        real_realpath = os.path.realpath
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            os.path, "realpath", lambda p, **kw: p if p == unc else real_realpath(p, **kw)
        )
        monkeypatch.setattr(os.path, "isdir", lambda p: True if p == unc else real_isdir(p))
        # The person's binding pins the resolved directory and records what a
        # share reports (no inode: the UNAVAILABLE state) -- modelled, since the
        # share is a string here and a real pin would contact the host.
        from kiro_crew.sandbox import IDENTITY_UNAVAILABLE

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.directory_identity_pinned",
            lambda p: IDENTITY_UNAVAILABLE,
        )

        def _app(person: bool) -> web.Application:
            app = web.Application()
            app["state"] = state

            @web.middleware
            async def _stamp(request: web.Request, handler: Any) -> Any:
                request["app"] = ""
                if person:
                    stamp_the_person(request)
                return await handler(request)

            app.middlewares.append(_stamp)
            app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
            return app

        async with TestClient(TestServer(_app(person=False))) as client:
            resp = await client.post(f"/api/chat/slots/{CALLER}/project", json={"project": unc})
            assert resp.status == 400
            assert (await resp.json()) == {"error": _UNC_REFUSAL, "code": "project_unc_path"}
        assert state._slots[CALLER].project == ""
        async with TestClient(TestServer(_app(person=True))) as client:
            resp = await client.post(f"/api/chat/slots/{CALLER}/project", json={"project": unc})
            assert resp.status == 200, await resp.text()
        assert state._slots[CALLER].project == unc
        untouched.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_non_person_arm_records_the_held_directorys_identity_for_the_spawn(
        self, state: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """The binding records what the spawn re-verifies, on the SLOT."""
        from kiro_crew.dashboard.chat import api_chat_slot_project

        project = tmp_path / "proj"
        project.mkdir()
        real = os.path.realpath(str(project))
        info = os.stat(real)

        def _app(person: bool) -> web.Application:
            app = web.Application()
            app["state"] = state

            @web.middleware
            async def _stamp(request: web.Request, handler: Any) -> Any:
                request["app"] = ""
                if person:
                    stamp_the_person(request)
                return await handler(request)

            app.middlewares.append(_stamp)
            app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
            return app

        async with TestClient(TestServer(_app(person=False))) as client:
            resp = await client.post(f"/api/chat/slots/{CALLER}/project", json={"project": real})
            assert resp.status == 200, await resp.text()
        slot = state._slots[CALLER]
        assert slot.project == real
        assert slot.project_identity == (real, info.st_dev, info.st_ino)
        # The person re-binds a REPLACED directory (a sibling made while the bound
        # one existed, renamed over the name -- another inode by construction):
        # the record follows what the pinned open of the name proves now.
        other = tmp_path / ".proj.other"
        other.mkdir()
        project.rmdir()
        other.rename(project)
        recreated = os.stat(real)
        async with TestClient(TestServer(_app(person=True))) as client:
            resp = await client.post(f"/api/chat/slots/{CALLER}/project", json={"project": real})
            assert resp.status == 200, await resp.text()
        assert slot.project == real
        assert slot.project_identity == (real, recreated.st_dev, recreated.st_ino)
        assert slot.project_identity != (real, info.st_dev, info.st_ino)


class TestTheFencedResolveHandsOverTheHeldDirectorysIdentity:
    """``screen_and_resolve_project_dir(..., identity_out=...)`` appends the ``(st_dev."""

    def test_success_appends_the_held_identity_and_a_refusal_appends_nothing(
        self, tmp_path: Any
    ) -> None:
        import kiro_crew.dashboard.chat_folders as cf

        target = tmp_path / "proj"
        target.mkdir()
        info = os.stat(target)
        seen: list[tuple[int, int]] = []
        real, refusal = cf.screen_and_resolve_project_dir(str(target), identity_out=seen)
        assert refusal is None
        assert os.path.normcase(real) == os.path.normcase(os.path.realpath(str(target)))
        assert seen == [(info.st_dev, info.st_ino)]
        gone: list[tuple[int, int]] = []
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "gone"), identity_out=gone) == (
            "",
            cf.PROJECT_DIR_MISSING_REFUSAL,
        )
        assert gone == []
        assert cf.screen_and_resolve_project_dir(str(target)) == (real, None)


def _real_dir_link(target: Any, link: Any) -> None:
    """A REAL directory link at *link* (a symlink, else a junction)."""
    from kiro_crew import platform_compat

    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        try:
            platform_compat.symlink_or_junction(str(target), str(link))
            return
        except OSError as exc:  # pragma: no cover - no CI host lacks both
            # Not a skip: a host that grants neither a symlink nor a junction
            # cannot carry the kernel's own traversal these tests assert, and a
            # silent skip there would loosen the ratchet. It fails, named.
            raise AssertionError(f"this host grants no directory link: {exc}") from exc


class TestALocalLinkToAShareIsRefusedBeforeAnythingFollowsIt:
    """For a NON-PERSON principal (the ``set_project`` directive."""

    @requires_symlinks
    def test_a_link_to_a_share_is_refused_without_reading_its_target(self, tmp_path: Any) -> None:
        """A dangling symlink to a UNC spelling: refused at the open as a link."""
        from kiro_crew.dashboard import chat_folders as cf

        os.symlink("//evil/share/proj", str(tmp_path / "share-link"), target_is_directory=True)
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "share-link")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )

    def test_a_link_at_the_leaf_a_chain_and_a_linked_ancestor_are_refused(
        self, tmp_path: Any
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "proj").mkdir(parents=True)
        _real_dir_link(tmp_path / "real", tmp_path / "leaf-link")
        _real_dir_link(tmp_path / "leaf-link", tmp_path / "chain")
        for spelling in (
            tmp_path / "leaf-link",
            tmp_path / "chain",
            tmp_path / "leaf-link" / "proj",  # a linked ANCESTOR of a real directory
        ):
            assert cf.screen_and_resolve_project_dir(str(spelling)) == (
                "",
                cf.PROJECT_DIR_LINK_REFUSAL,
            ), spelling

    def test_a_benign_link_is_refused_for_the_agent_and_resolved_for_the_person(
        self, tmp_path: Any
    ) -> None:
        """The rule applied consistently: a link to a LOCAL directory -- absolute or relative."""
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "sub").mkdir(parents=True)
        _real_dir_link(tmp_path / "real", tmp_path / "dir-link")
        _real_dir_link("./real", tmp_path / "rel-link")
        resolved = str((tmp_path / "real" / "sub").resolve())
        for linked in (tmp_path / "dir-link" / "sub", tmp_path / "rel-link" / "sub"):
            assert cf._validate_project_dir(str(linked)) == (resolved, None), linked
            assert cf.screen_and_resolve_project_dir(str(linked)) == (
                "",
                cf.PROJECT_DIR_LINK_REFUSAL,
            ), linked
        through = str(tmp_path / "real" / "sub")
        assert cf.screen_and_resolve_project_dir(through) == (resolved, None)

    def test_dotdot_is_opened_relative_to_the_directory_the_pin_holds(self, tmp_path: Any) -> None:
        """``..`` in an agent-named spelling is opened by the pinned walk relative to the."""
        from kiro_crew.dashboard import chat_folders as cf

        work = tmp_path / "work"
        (work / "sibling").mkdir(parents=True)
        (work / "other").mkdir()
        team = tmp_path / "projects" / "team"
        (team / "subdir").mkdir(parents=True)
        (team / "sibling").mkdir()
        plain = os.path.join(str(work), "other", "..", "sibling")
        expected = os.path.realpath(plain)
        assert cf.screen_and_resolve_project_dir(plain) == (expected, None)
        assert os.path.normcase(expected) == os.path.normcase(str((work / "sibling").resolve()))
        _real_dir_link(team / "subdir", work / "link")
        through_link = os.path.join(str(work), "link", "..", "sibling")
        assert cf._validate_project_dir(through_link) == (os.path.realpath(through_link), None)
        if os.name == "nt":
            # The Win32 parser folds ``..`` before any filesystem sees a name, so
            # the link is never reached: ``abspath`` applies the platform's own
            # rule and the walk opens ``work\\sibling``, the directory every
            # by-name open on this host would reach too.
            assert cf.screen_and_resolve_project_dir(through_link) == (
                os.path.realpath(os.path.join(str(work), "sibling")),
                None,
            )
        else:
            assert cf.screen_and_resolve_project_dir(through_link) == (
                "",
                cf.PROJECT_DIR_LINK_REFUSAL,
            )

    def test_a_trailing_backslash_is_a_separator_only_on_windows(self, tmp_path: Any) -> None:
        """On POSIX a backslash is an ordinary name character."""
        from kiro_crew.dashboard import chat_folders as cf

        if os.altsep:
            pytest.skip("a backslash is a separator on this platform")
        target = tmp_path / "evil\\"
        (target / "sub").mkdir(parents=True)
        spelling = os.path.join(str(target), "sub")
        expected = os.path.realpath(spelling)
        assert cf.screen_and_resolve_project_dir(spelling) == (expected, None)
        assert expected == str((target / "sub").resolve())

    @pytest.mark.asyncio
    async def test_the_slot_project_endpoint_codes_a_link_on_the_way_apart_from_the_unc_class(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """A link anywhere on the way answers its own code (``project_dir_link``) with its own."""
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_project

        (tmp_path / "real").mkdir()
        _real_dir_link(tmp_path / "real", tmp_path / "dir-link")
        sel_fn = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", sel_fn)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            linked = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "dir-link")},
            )
            assert linked.status == 400
            assert (await linked.json()) == {
                "error": cf.PROJECT_DIR_LINK_REFUSAL,
                "code": "project_dir_link",
            }
            assert sel_fn.return_value.log_api_access.call_args.kwargs["error"] == "link on the way"
            text = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": "\\\\evil\\share\\proj"},
            )
            assert text.status == 400
            assert (await text.json())["code"] == "project_unc_path"
            assert sel_fn.return_value.log_api_access.call_args.kwargs["error"] == "UNC path"
        assert state._slots[CALLER].project == ""

    def test_no_by_name_probe_precedes_the_pin_and_a_link_is_refused_not_followed(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Red-first on the head before this round."""
        from kiro_crew import platform_compat
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "other").mkdir()
        _real_dir_link(tmp_path / "other", tmp_path / "plain")
        monkeypatch.setattr(
            platform_compat,
            "first_linked_ancestor",
            MagicMock(side_effect=AssertionError("a by-name ancestor screen ran before the pin")),
        )
        seen: list[str] = []
        real_realpath = os.path.realpath
        monkeypatch.setattr(
            cf.os.path,
            "realpath",
            lambda p, *a, **k: (seen.append(str(p)), real_realpath(p, *a, **k))[1],
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "plain")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )
        # The sensitive check resolves its ANCHORS (the home directory, the
        # override roots) by name; the agent's own spelling never is.
        under_tmp = [p for p in seen if p.startswith(str(tmp_path))]
        assert under_tmp == [], "the agent's spelling was handed to a by-name resolve"
        # A link-free spelling still resolves (the real path read back off the
        # HELD descriptor is the kernel's, not a by-name walk of the spelling).
        real = str((tmp_path / "other").resolve())
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "other")) == (real, None)

    def test_a_host_that_pins_neither_way_refuses_rather_than_resolving_by_name(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """No realpath fallback is left in the admission path."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real").mkdir()
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(pinned_fs, "_windows_handle_pin_available", lambda: False)
        seen: list[str] = []
        real_realpath = os.path.realpath
        monkeypatch.setattr(
            cf.os.path,
            "realpath",
            lambda p, *a, **k: (seen.append(str(p)), real_realpath(p, *a, **k))[1],
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "real")) == (
            "",
            cf.PROJECT_DIR_UNVERIFIABLE_REFUSAL,
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "real")) == (
            "",
            cf.PROJECT_DIR_UNVERIFIABLE_REFUSAL,
        )
        # The anchors (the home directory) may be resolved for the sensitive check;
        # the ORIGINAL spelling never is.
        assert str(tmp_path / "real") not in seen

    def test_a_missing_sensitive_path_is_still_refused_as_sensitive(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """main's precedence, kept."""
        from kiro_crew.dashboard import chat_folders as cf

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        assert not os.path.exists(os.path.expanduser("~/.ssh"))
        assert cf._validate_project_dir("~/.ssh") == ("", "project_dir refers to a sensitive path")
        assert cf._validate_project_dir(str(tmp_path / "home" / "nope")) == (
            "",
            cf.PROJECT_DIR_MISSING_REFUSAL,
        )

    @pytest.mark.asyncio
    async def test_the_persons_bind_through_a_link_resolves_by_name_as_on_main(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Through the real routes, as the person (the one caller admitted to bind)."""
        from kiro_crew import pinned_fs

        (tmp_path / "real").mkdir()
        _real_dir_link(tmp_path / "real", tmp_path / "dir-link")
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Proj", str(proj))
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        headers = {"X-Session-Key": f"dashboard:{CALLER}"}
        async with TestClient(TestServer(_make_folder_app(state, dashboard_user=True))) as client:
            through = await client.post(
                "/api/chat/folders",
                json={"name": "Linked", "project_dir": str(tmp_path / "dir-link")},
                headers=headers,
            )
            assert through.status == 201, await through.text()
            linked = (await through.json())["id"]
            assert _folder(state, linked)["project_dir"] == os.path.realpath(
                str(tmp_path / "dir-link")
            )
            updated = await client.patch(
                f"/api/chat/folders/{fid}",
                json={"project_dir": str(tmp_path / "dir-link")},
                headers=headers,
            )
            assert updated.status == 200, await updated.text()
        assert _folder(state, fid)["project_dir"] == os.path.realpath(str(tmp_path / "dir-link"))
        untouched.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_slot_project_endpoint_resolves_off_the_loop(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The endpoint answers 400 ``project_unc_path`` with the link text for a share link."""
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard.chat import api_chat_slot_project

        (tmp_path / "real").mkdir()
        _real_dir_link(tmp_path / "real", tmp_path / "share-link")
        (tmp_path / "proj").mkdir()
        offloaded: list[Any] = []
        real_to_thread = asyncio.to_thread

        async def _spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
            offloaded.append(fn)
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.asyncio.to_thread", _spy)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "share-link")},
            )
            body = await resp.json()
            assert resp.status == 400, body
            assert body == {"error": cf.PROJECT_DIR_LINK_REFUSAL, "code": "project_dir_link"}
            assert state._slots[CALLER].project == ""
            missing = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "nope")},
            )
            assert missing.status == 400
            assert (await missing.json()) == {
                "error": "Not a directory",
                "code": "project_not_a_directory",
            }
            plain = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "proj")},
            )
            assert plain.status == 200, await plain.text()
        assert state._slots[CALLER].project == str((tmp_path / "proj").resolve())
        assert cf.screen_and_resolve_project_dir in offloaded

    @pytest.mark.asyncio
    async def test_the_slot_project_endpoint_answers_a_nul_spelling_with_not_a_directory(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Both arms: a spelling no filesystem can carry is the missing directory.

        The spelling hangs off a link-free root (pytest resolves ``tmp_path``):
        on macOS ``/tmp`` is itself a link to ``/private/tmp``, and the agent's arm
        refuses a link at any component before the leaf is judged."""
        from kiro_crew.dashboard.chat import api_chat_slot_project

        nul_spelling = str(tmp_path) + "/a\u0000b"
        for person in (False, True):
            app = web.Application()
            app["state"] = state

            def _stamp_for(person: bool) -> Any:
                @web.middleware
                async def _stamp(request: web.Request, handler: Any) -> Any:
                    request["app"] = ""
                    if person:
                        stamp_the_person(request)
                    return await handler(request)

                return _stamp

            app.middlewares.append(_stamp_for(person))
            app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    f"/api/chat/slots/{CALLER}/project", json={"project": nul_spelling}
                )
                assert resp.status == 400, (person, await resp.text())
                assert (await resp.json()) == {
                    "error": "Not a directory",
                    "code": "project_not_a_directory",
                }
            assert state._slots[CALLER].project == ""

    @pytest.mark.asyncio
    async def test_a_binding_whose_directory_cannot_be_pinned_is_refused_not_stored(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """A failed pin at bind is a FAILED BIND, surfaced."""
        from kiro_crew import sandbox as sandbox_module
        from kiro_crew.dashboard.chat import api_chat_slot_project

        proj = tmp_path / "proj"
        proj.mkdir()
        slot = state._slots[CALLER]
        before = (slot.project, slot.project_identity)
        # The flip: the directory validated by name is a link by the time the
        # binding pins it -- handed to THE identity read every path shares
        # (``open_pinned_directory``), on every host.
        monkeypatch.setattr(
            sandbox_module,
            "open_pinned_directory",
            lambda path: (_ for _ in ()).throw(
                NotADirectoryError(errno.ENOTDIR, "not a real directory", path)
            ),
        )
        app = web.Application()
        app["state"] = state

        @web.middleware
        async def _stamp(request: web.Request, handler: Any) -> Any:
            request["app"] = ""
            stamp_the_person(request)  # the person's arm: the by-name resolve, then the pin
            return await handler(request)

        app.middlewares.append(_stamp)
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{CALLER}/project", json={"project": str(proj)}
            )
            assert resp.status == 400, await resp.text()
            body = await resp.json()
            assert body["code"] == "project_dir_not_pinned"
            assert "could not be pinned" in body["error"]
        assert (slot.project, slot.project_identity) == before

    @pytest.mark.asyncio
    async def test_the_sensitive_verdict_is_taken_on_the_pinned_path_never_by_name(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The check-then-use window one step later: after the pinned resolve."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import session_directive_apply as sda
        from kiro_crew.dashboard.chat import api_chat_slot_project

        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        proj = tmp_path / "proj"
        proj.mkdir()
        real = os.path.realpath(str(proj))
        real_pin = pinned_fs.real_dir_path_pinned

        def _pin_then_swap(path: str, **kw: Any) -> str:
            out = real_pin(path, **kw)
            if os.path.isdir(str(proj)) and not os.path.islink(str(proj)):
                proj.rmdir()
                _real_dir_link(home / ".ssh", proj)
            return out

        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", _pin_then_swap)
        seen: list[str] = []
        real_realpath = os.path.realpath
        monkeypatch.setattr(
            os.path, "realpath", lambda p, **kw: (seen.append(str(p)), real_realpath(p, **kw))[1]
        )

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{CALLER}/project", json={"project": str(proj)}
            )
            assert resp.status == 200, await resp.text()
        assert state._slots[CALLER].project == real
        assert real not in seen and str(proj) not in seen, seen

        # Reset the fixture for the directive: a plain directory again.
        os.unlink(str(proj))
        proj.mkdir()
        seen.clear()
        slot = MagicMock()
        slot.project = ""
        answer = await sda._set_project(MagicMock(), slot, {"project": str(proj)})
        assert answer.startswith("Project set to"), answer
        assert slot.project == real
        assert real not in seen and str(proj) not in seen, seen

        # The macOS arm, on every host: the voice-runtime pre-flight that both
        # fenced sites run after the verdict is darwin-gated, and it re-walked
        # the admitted path by name (``realpath``) -- the very window the pin
        # closed, caught by the macOS shard. Forced darwin with the runtime
        # paths faked, so the pre-flight runs here and the spy proves it takes
        # the pinned path as it stands.
        from kiro_crew import sandbox as sandbox_module

        os.unlink(str(proj))
        proj.mkdir()
        seen.clear()
        runtime = tmp_path / "voice-runtime"
        runtime.mkdir()
        monkeypatch.setattr(sandbox_module, "_voice_runtime_sandbox_paths", lambda: (str(runtime),))
        monkeypatch.setattr(sandbox_module.sys, "platform", "darwin")
        slot = MagicMock()
        slot.project = ""
        answer = await sda._set_project(MagicMock(), slot, {"project": str(proj)})
        assert answer.startswith("Project set to"), answer
        assert slot.project == real
        assert real not in seen and str(proj) not in seen, seen
        # ... and the pre-flight still answers on that path: the runtime
        # directory itself is refused as the overlap it is.
        overlap = sandbox_module.voice_runtime_workspace_conflict(str(runtime), pre_resolved=True)
        assert overlap is not None and "voice runtime" in overlap

    @pytest.mark.asyncio
    async def test_the_set_project_directive_refuses_it_as_denied(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard import session_directive_apply as sda

        (tmp_path / "real").mkdir()
        _real_dir_link(tmp_path / "real", tmp_path / "share-link")
        slot = MagicMock()
        slot.project = ""
        with pytest.raises(sda._DirectiveDenied) as excinfo:
            await sda._set_project(MagicMock(), slot, {"project": str(tmp_path / "share-link")})
        assert cf.PROJECT_DIR_LINK_REFUSAL in str(excinfo.value)
        assert slot.project == ""
        # A missing directory keeps the directive's own answer, not a denial.
        answer = await sda._set_project(MagicMock(), slot, {"project": str(tmp_path / "nope")})
        assert answer.startswith("Error: not a directory:")
        assert slot.project == ""


class TestTheReadPathIsMainsByNameResolutionForEveryStoredValue:
    """``_validate_project_dir`` is the STORED-value reader (``_resolve_folder_project_dir``."""

    @staticmethod
    def _stored(project_dir: str) -> list[dict[str, Any]]:
        return [
            {"id": "fldr00000051", "name": "Bound", "parent_id": "", "project_dir": project_dir}
        ]

    def test_a_stored_binding_through_a_link_resolves_as_before(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The person bound it (no one else can)."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "sub").mkdir(parents=True)
        _real_dir_link(tmp_path / "real", tmp_path / "dir-link")
        stored = str(tmp_path / "dir-link" / "sub")
        # A non-person request is refused: the agent names the directory itself.
        assert cf.screen_and_resolve_project_dir(stored) == ("", cf.PROJECT_DIR_LINK_REFUSAL)
        # The stored value is honoured: main's own resolution, the pin untouched.
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran on the read path"))
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        expected = os.path.realpath(str(tmp_path / "real" / "sub"))
        assert cf._validate_project_dir(stored) == (expected, None)
        assert _resolve_folder_project_dir(self._stored(stored), "fldr00000051") == (
            expected,
            None,
        )
        untouched.assert_not_called()

    def test_an_ancestor_this_process_may_not_open_is_not_a_swap(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The pinned open cannot verify what it may not open (a denied ACL)."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        target = tmp_path / "real" / "sub"
        target.mkdir(parents=True)

        def _denied(path: str, **_kw: Any) -> str:
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", _denied)
        assert cf.screen_and_resolve_project_dir(str(target)) == (
            "",
            cf.PROJECT_DIR_UNOPENABLE_REFUSAL,
        )
        assert cf.screen_and_resolve_project_dir(str(target)) == (
            "",
            cf.PROJECT_DIR_UNOPENABLE_REFUSAL,
        )
        assert "retry" not in cf.PROJECT_DIR_UNOPENABLE_REFUSAL
        assert cf._validate_project_dir(str(target)) == (os.path.realpath(str(target)), None)

    def test_a_directory_under_a_search_only_ancestor_still_pins(self, tmp_path: Any) -> None:
        """Opus: ``pin_parent`` opened every ancestor read-only."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        gate = tmp_path / "gate"
        proj = gate / "proj"
        proj.mkdir(parents=True)
        (tmp_path / "other").mkdir()
        if not getattr(os, "O_PATH", 0) or not hasattr(os, "geteuid") or os.geteuid() == 0:
            # No POSIX search-only mode to express here (Windows), or a root
            # process that no mode restricts: the chain's traverse flags are the
            # pin -- search-only where the platform has it -- and the nested
            # directory still resolves through the pinned walk. Not a skip: the
            # same call runs and the same equality holds.
            if getattr(os, "O_PATH", 0):
                assert pinned_fs.traverse_flags() & os.O_PATH
            expected = os.path.realpath(str(proj))
            assert pinned_fs.real_dir_path_pinned(str(proj), what="project directory") == expected
            assert cf.screen_and_resolve_project_dir(str(proj)) == (expected, None)
            return
        os.chmod(gate, 0o111)
        try:
            expected = os.path.realpath(str(proj))
            assert pinned_fs.real_dir_path_pinned(str(proj), what="project directory") == expected
            assert cf.screen_and_resolve_project_dir(str(proj)) == (expected, None)
            assert cf._validate_project_dir(str(proj)) == (expected, None)
            # Still a pinned walk: a link planted at the name is refused, not
            # followed, whatever its ancestor's mode.
            os.chmod(gate, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
            os.symlink(str(tmp_path / "other"), str(gate / "link"), target_is_directory=True)
            os.chmod(gate, 0o111)
            with pytest.raises(pinned_fs.PinnedPathRefusal):
                pinned_fs.real_dir_path_pinned(str(gate / "link"), what="project directory")
        finally:
            os.chmod(gate, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip

    def test_a_stored_binding_through_a_link_to_a_share_is_honoured(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The person bound it (no one else can)."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real").mkdir()
        _real_dir_link(tmp_path / "real", tmp_path / "share-link")
        stored = str(tmp_path / "share-link")
        assert cf.screen_and_resolve_project_dir(stored) == ("", cf.PROJECT_DIR_LINK_REFUSAL)
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran on the read path"))
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        real_realpath = os.path.realpath
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            cf.os.path,
            "realpath",
            lambda p, **kw: "//evil/share/proj" if p == stored else real_realpath(p, **kw),
        )
        monkeypatch.setattr(
            cf.os.path, "isdir", lambda p: True if p == "//evil/share/proj" else real_isdir(p)
        )
        assert cf._validate_project_dir(stored) == ("//evil/share/proj", None)
        assert _resolve_folder_project_dir(self._stored(stored), "fldr00000051") == (
            "//evil/share/proj",
            None,
        )
        untouched.assert_not_called()

    def test_an_unrepresentable_spelling_is_the_missing_directory_not_a_server_error(
        self, tmp_path: Any
    ) -> None:
        """GPT."""
        from kiro_crew.dashboard import chat_folders as cf

        spelling = os.path.join(str(tmp_path), "a\x00b")
        assert cf.screen_and_resolve_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)
        assert cf.screen_and_resolve_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)
        assert cf._validate_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)


def _fake_fs_for(monkeypatch: Any, path: str) -> None:
    """Make *path* look like an existing."""
    import kiro_crew.dashboard.chat_folders as cf
    import kiro_crew.dashboard.chat_handlers as ch
    from kiro_crew.sandbox import IDENTITY_UNAVAILABLE

    real_realpath, real_isdir = os.path.realpath, os.path.isdir
    real_pin = ch.directory_identity_pinned
    monkeypatch.setattr(
        os.path, "realpath", lambda p, **kw: p if p == path else real_realpath(p, **kw)
    )
    monkeypatch.setattr(os.path, "isdir", lambda p: True if p == path else real_isdir(p))
    monkeypatch.setattr(
        ch,
        "directory_identity_pinned",
        lambda p: IDENTITY_UNAVAILABLE if os.fspath(p) == path else real_pin(p),
    )
    monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)


class TestTheReadPathResolvesAStoredValueAsBefore:
    """``_validate_project_dir`` is also the STORED-value reader."""

    STORED = "//legacy/share/proj"

    def test_a_stored_unc_value_still_resolves(self, monkeypatch: Any) -> None:
        import kiro_crew.dashboard.chat_folders as cf

        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        _fake_fs_for(monkeypatch, self.STORED)
        folders = [
            {"id": "fldr00000041", "name": "Legacy", "parent_id": "", "project_dir": self.STORED}
        ]
        assert _resolve_folder_project_dir(folders, "fldr00000041") == (self.STORED, None)
        # No refusal was audited: the read path never ran the admission rule.
        sel_fn.return_value.log_api_access.assert_not_called()

    @pytest.mark.parametrize("unc", _UNC_SPELLINGS)
    def test_no_stored_spelling_meets_the_admission_refusal(
        self, monkeypatch: Any, unc: str
    ) -> None:
        """Whatever the ordinary validation says about a stored spelling on this host (a."""
        import kiro_crew.dashboard.chat_folders as cf

        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)
        monkeypatch.setattr(os.path, "realpath", lambda p, **kw: p)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        folders = [{"id": "fldr00000041", "name": "Legacy", "parent_id": "", "project_dir": unc}]
        _resolved, err = _resolve_folder_project_dir(folders, "fldr00000041")
        assert err != _UNC_REFUSAL

    @pytest.mark.asyncio
    async def test_a_chat_still_opens_in_a_folder_bound_before_the_rule(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The route the retroactive refusal would have broken."""
        _fake_fs_for(monkeypatch, self.STORED)
        state._folders.append(
            {"id": "fldr00000041", "name": "Legacy", "parent_id": "", "project_dir": self.STORED}
        )
        mock_cfg = MagicMock()
        mock_cfg.dashboard.default_project = ""
        mock_cfg.default_agent = ""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda _workspace: str(tmp_path / "workspace-default"),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            lambda *_args, **_kwargs: None,
        )
        pin_the_owners_store_step(monkeypatch)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "legacy-chat", "folder_id": "fldr00000041"}
            )
            data = await resp.json()
        assert resp.status == 200, data
        assert data["project"] == self.STORED
        assert state._slots["legacy-chat"].project == self.STORED
