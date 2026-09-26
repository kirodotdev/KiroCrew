"""``project_dir`` on the folder routes and tools, driven through the REAL routes.

``test_mcp_dashboard_folders.py`` patches the HTTP helpers and pins the tools'
call shapes. This module observes the other half -- what the routes and the
read side actually do -- by wiring ``mcp_dashboard``'s ``_get`` / ``_post`` /
``_patch`` to a live aiohttp test server running ``chat_folders``' own handlers
over a real ``DashboardState``, then reading the store the routes wrote. The
slot-create route is included so the inheritance a binding exists for is
observed end to end: a folder the person bound is one a session created inside
it inherits from, and a binding reaches every chat filed beneath it, as it
always did.

The tool is synchronous (it is a stdio MCP server), so it runs on a worker
thread while the bridge hands each request back to the test's event loop with
``run_coroutine_threadsafe`` — the same request/response contract
``mcp_core._send`` presents (a 2xx body verbatim; a 4xx collapsed to
``{"error", "code"}``).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_folder_app, _make_state

from kiro_crew.dashboard.chat_folders import (
    _resolve_folder_project_dir,
    create_folder_record,
)
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY
from kiro_crew.mcp_dashboard import _call_tool_inner
from kiro_crew.validation import ValidationError

CALLER = "chat-1-100"


class _Bridge:
    """``mcp_core`` request helpers re-targeted at an aiohttp ``TestClient``.

    ``default_session_key`` mirrors the real helpers' default: ``_get`` /
    ``_post`` / ``_patch`` resolve the caller's own key when none is passed, so a
    read the tool makes without one (the slot roster its tree-shaping gate scopes
    the caller by) still reaches the routes AS the caller. ``_call`` sets it to
    the key it hands the strict resolver.
    """

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
            # ``_http_error_body``'s flattening: the structured error, plus code.
            return {"error": str(payload.get("error")), "code": str(payload.get("code") or "")}
        return payload

    def _run(self, method: str, path: str, body: dict | None, session_key: str | None) -> Any:
        if session_key is None:
            session_key = self.default_session_key
        return asyncio.run_coroutine_threadsafe(
            self._request(method, path, body, session_key), self._loop
        ).result(timeout=30)

    # Signatures mirror ``mcp_core._get`` / ``_post`` / ``_patch``.
    def get(self, path: str, session_key: str | None = None, *, timeout: float = 10) -> Any:
        return self._run("GET", path, None, session_key)

    def post(
        self,
        path: str,
        body: dict | None = None,
        *,
        timeout: float = 30,
        session_key: str | None = None,
    ) -> dict:
        return self._run("POST", path, body or {}, session_key)

    def patch(self, path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
        return self._run("PATCH", path, body or {}, session_key)


async def _call(
    bridge: _Bridge, name: str, args: dict[str, Any], *, caller_key: str = f"dashboard:{CALLER}"
) -> str:
    """Run one tool call on a worker thread against the live routes.

    ``caller_key`` is the verified key the strict resolver hands the tool -- the
    dashboard slot by default, or another namespace (a ``channel:`` agent) to
    drive the routes as that principal.
    """
    bridge.default_session_key = caller_key
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=bridge.get),
        patch("kiro_crew.mcp_dashboard._post", side_effect=bridge.post),
        patch("kiro_crew.mcp_dashboard._patch", side_effect=bridge.patch),
        patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value=caller_key,
        ),
    ):
        return await asyncio.to_thread(_call_tool_inner, name, args)


def _folder(state: Any, fid: str) -> dict[str, Any]:
    return next(f for f in state._folders if f["id"] == fid)


def _created_id(out: str) -> str:
    assert "(id=" in out, out
    return out.split("(id=", 1)[1].split(")", 1)[0]


MEMBER = "member:reviewer-store"


def _member_folder_app(state: Any, principal: str = MEMBER) -> web.Application:
    """The folder routes as an admitted crew MEMBER reaches them: the chat-route
    gate (``handlers/_shared.py``) stamps the verified ``member:<store>``
    principal on the request, and ``folder_principal`` reads that key. The
    member's own slot carries no app, so the tool's tree-shaping gate scopes it
    like the person; the routes are what tell the two apart. Like every tool
    call it rides the internal-secret transport (``_make_folder_app``'s default
    stamp), so it meets the one binding rule as any agent does."""
    app = _make_folder_app(state)

    @web.middleware
    async def _stamp_member(request: web.Request, handler: Any) -> Any:
        request[MEMBER_CHAT_PRINCIPAL_KEY] = principal
        return await handler(request)

    app.middlewares.append(_stamp_member)
    return app


async def _person_binds(state: Any, name: str, project_dir: str, parent_id: str = "") -> str:
    """A folder the PERSON bound from the sidebar, as the store holds it: the
    store function the sidebar's create ends in, with no principal, so the row
    carries no owner and its binding reaches every chat filed beneath it."""
    folder = await create_folder_record(
        state,
        name=name,
        parent_id=parent_id,
        project_dir=project_dir,
        request_app="",
    )
    return str(folder["id"])


def _pin_slot_create_defaults(monkeypatch: Any, tmp_path: Any) -> str:
    """The pins the route's own inheritance test uses (test_dashboard_chat): no
    configured default project, no eager spawn, a string default agent. Returns
    the workspace default a chat falls back to when its folder confers none."""
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
    """Where a folder's project directory comes from and what it confers,
    observed end to end. No folder tool carries ``project_dir`` (the agent
    bind path is a follow-up): a tool call naming one is refused by the schema
    before any request, and the routes refuse an agent's binding whole. The
    person binds from the sidebar and a chat opened in the folder inherits it;
    the move rule holds every agent through ``chat_folder_move``."""

    @pytest.mark.asyncio
    async def test_a_tool_call_naming_project_dir_is_refused_before_any_request(
        self, state: Any, tmp_path: Any
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            bridge = _Bridge(client, asyncio.get_running_loop())
            with pytest.raises(ValidationError):
                await _call(
                    bridge, "chat_folder_create", {"name": "Proj", "project_dir": str(proj)}
                )
        assert state._folders == []

    @pytest.mark.asyncio
    async def test_the_routes_refuse_an_agents_binding_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Straight at the routes on the internal transport (the tools cannot
        carry the field): an ordinary session's bound create and its set or
        clear on the person's folder are refused with the one text, and
        nothing is written."""
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
        """The point of the feature for the person, observed: a folder the
        person bound (from the sidebar -- the store row that create ends in),
        then a chat opened in it the way the dashboard does starts with the
        folder's project."""
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Proj", str(proj))
        assert _folder(state, fid)["project_dir"] == os.path.realpath(str(proj))

        _pin_slot_create_defaults(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
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
        """Through ``chat_folder_move``: the person binds one folder and files a
        chat in another, unbound one; an ordinary session and a Channels
        session each try to move the unbound folder under the bound one.
        Refused with the move rule's text, parent unchanged, nothing inherited;
        a crew member moving its own folder under a member-owned bound folder
        (a row from another route) is refused the same way."""
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


#: The three UNC spellings Windows honours: two backslashes, two forward slashes,
#: and the extended-length ``\\?\UNC\`` form. Every one names a HOST.
_UNC_SPELLINGS = (r"\\evil\share\proj", "//evil/share/proj", r"\\?\UNC\evil\share\proj")
_UNC_REFUSAL = "Project directory must not be a network (UNC) path"


class TestAUncProjectDirIsRefusedBeforeAnyFilesystemCall:
    """``project_dir`` from a caller other than the person is path text that
    reaches the gateway's filesystem. On a Windows gateway ``realpath``/``isdir``
    on ``\\\\host\\share`` opens an SMB connection to that host -- an outbound
    credential probe the text's author controls, with no recovery -- so a
    UNC-shaped value from a NON-PERSON principal is refused lexically, before
    the first filesystem call, through the repo's one UNC helper
    (``is_unc_shape`` / ``unc_probe_allowed``, exactly as the steering
    validator in the same module and the attachment readers do). The shape is
    refused on EVERY host: path text is untrusted everywhere, and the platform is
    never consulted. Same 400 shape as the validator's other refusals; audited
    like the sensitive-path refusal.

    The rule is scoped to the principal the threat names. The PERSON choosing a
    path for the person's own gateway is the operator, not a caller steering
    the gateway onto a host of the caller's choosing, so the person's request
    keeps main's behaviour exactly: ``realpath`` + ``isdir`` by name, a share by
    its UNC spelling included -- a person could bind a project on a share before
    this rule and still can (``_admit_project_dir``,
    ``_validate_project_dir``). Red-first both ways on the head before this
    class: the person's UNC bind answered the UNC refusal at both routes and at
    the slot project endpoint, and ``_admit_project_dir`` took no principal.
    The read paths never run the rule either (every stored binding is the
    person's); see ``TestTheReadPathResolvesAStoredValueAsBefore``.
    """

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
        monkeypatch.setattr(cf, "link_screen", touched)
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
        """The person's arm is main's code: ``realpath`` then ``isdir`` on the
        spelling, no UNC refusal, no screen, no pinned open -- the filesystem is
        faked so no host is contacted on this runner. Whatever the platform's
        ``isabs`` says about a backslash spelling is the ordinary validation's
        answer; it is never the UNC refusal and never the screen's."""
        import kiro_crew.dashboard.chat_folders as cf
        from kiro_crew import pinned_fs

        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(cf, "link_screen", untouched)
        monkeypatch.setattr(pinned_fs, "real_dir_path_pinned", untouched)
        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)
        seen: list[str] = []
        monkeypatch.setattr(os.path, "realpath", lambda p, **kw: (seen.append(p), p)[1])
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        monkeypatch.setattr(cf, "is_sensitive_path", lambda p: False)
        resolved, err = cf._admit_project_dir(unc)
        assert err != _UNC_REFUSAL
        assert err not in (cf.PROJECT_DIR_LINK_REFUSAL, cf.PROJECT_DIR_UNSCREENABLE_LINK_REFUSAL)
        if os.path.isabs(unc):
            assert (resolved, err) == (unc, None)
            assert seen == [unc]
        untouched.assert_not_called()

    def test_the_helper_decides_not_a_second_rule(self, monkeypatch: Any) -> None:
        """When ``unc_probe_allowed`` vouches for the share (the gateway's own data
        home on a roaming profile), the non-person UNC refusal does not fire and
        the fenced checks run -- the screen answers the spelling unchanged and
        the pinned open finds nothing, so the missing-directory text is what a
        non-person caller reads; no host is ever contacted."""
        import kiro_crew.dashboard.chat_folders as cf
        from kiro_crew import pinned_fs

        monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: True)
        monkeypatch.setattr(cf, "link_screen", lambda target: (target, "ok"))
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
        """Straight at the routes, as the person: the UNC spelling is admitted
        by main's own resolution (faked here, so no host is contacted) and stored
        as ``realpath`` returned it; create stores it, the PATCH re-points to it,
        the ``""`` clear still lands. Red on the head before this test: both
        routes answered 400 with the UNC refusal."""
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
        """The same endpoint, two principals: a caller without the person's stamp
        (the internal transport, an app) meets the lexical refusal before any
        filesystem call; the person's request resolves by name as on main and
        the fenced helpers never run for it. Red on the head before this test:
        the person's request answered ``project_unc_path``."""
        import kiro_crew.dashboard.chat_folders as cf
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard.chat import api_chat_slot_project

        unc = "//evil/share/proj"
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(cf, "link_screen", untouched)
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

        def _app(person: bool) -> web.Application:
            app = web.Application()
            app["state"] = state

            @web.middleware
            async def _stamp(request: web.Request, handler: Any) -> Any:
                request["app"] = ""
                if person:
                    request["is_dashboard_user"] = True
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


class _HooksOs:
    """``hooks.os`` stand-in: every attribute is the real ``os`` module's, except
    ``readlink``, which answers a planted link's target or refuses an unreadable
    one. Substituting the SCREEN's own ``os`` (the module already does this in
    its Windows-gate tests) leaves ``os.path.realpath`` -- which the read-path
    fallback runs -- on the real functions."""

    def __init__(self, planted: dict[str, str], unreadable: set[str]) -> None:
        self._planted = planted
        self._unreadable = unreadable

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def readlink(self, path: str, *args: Any, **kwargs: Any) -> str:
        norm = os.path.normcase(os.path.normpath(str(path)))
        if norm in self._unreadable:
            raise PermissionError(13, "the link's target may not be read", str(path))
        if norm in self._planted:
            return self._planted[norm]
        return os.readlink(path, *args, **kwargs)


def _plant_link(monkeypatch: Any, target: str, link: Any, *, fake: bool = False) -> None:
    """Make *link* a link to *target* for the screen, on EVERY platform and
    without a skip: a real symlink where the OS grants one (POSIX; Windows with
    the privilege), a junction where the target exists and a symlink is refused
    (``platform_compat.symlink_or_junction``, no privilege needed), and
    otherwise -- or when *fake* asks for it -- a PLANTED link: a plain
    directory the screen's own predicates read as a link
    (``platform_compat.is_link_or_junction``) whose target the screen's ``os``
    answers (``_HooksOs``). The assertions are the same whichever branch ran;
    the fake branch is exercised on every host by the ``planted`` parameter, so
    a runner that cannot make links loses no assertion."""
    from kiro_crew import hooks, platform_compat

    link = str(link)
    if not fake:
        try:
            os.symlink(target, link, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            if os.path.isdir(target):
                try:
                    platform_compat.symlink_or_junction(target, link)
                    return
                except OSError:
                    pass
    os.makedirs(link, exist_ok=True)
    key = os.path.normcase(os.path.normpath(link))
    fake_os = getattr(hooks.os, "_planted", None)
    if fake_os is None:
        planted: dict[str, str] = {}
        monkeypatch.setattr(hooks, "os", _HooksOs(planted, set()))
    else:
        planted = fake_os
    planted[key] = target
    real_is_link = platform_compat.is_link_or_junction

    def _is_link(path: Any) -> bool:
        return os.path.normcase(os.path.normpath(str(path))) in planted or real_is_link(path)

    monkeypatch.setattr(platform_compat, "is_link_or_junction", _is_link)


def _real_dir_link(target: Any, link: Any) -> None:
    """A REAL directory link at *link* (a symlink, else a junction), for the tests
    whose subject is the kernel's own resolution rule and which a planted link
    therefore cannot serve. Skips only where the OS grants neither."""
    from kiro_crew import platform_compat

    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        try:
            platform_compat.symlink_or_junction(str(target), str(link))
            return
        except OSError as exc:
            pytest.skip(f"this host grants no directory link: {exc}")


def _make_unreadable(monkeypatch: Any, link: Any) -> None:
    """Make the screen's ``readlink`` of *link* fail with a permission error --
    the one link cause that is not a probe and that a stored binding may meet
    (a denied ACL); ``realpath`` keeps the real ``readlink``."""
    from kiro_crew import hooks

    current = hooks.os
    planted = getattr(current, "_planted", {})
    unreadable = set(getattr(current, "_unreadable", set()))
    unreadable.add(os.path.normcase(os.path.normpath(str(link))))
    monkeypatch.setattr(hooks, "os", _HooksOs(dict(planted), unreadable))


@pytest.fixture(params=["real", "fake"])
def planted(request: Any) -> bool:
    """Run a link test twice: once with the OS's own links where it grants
    them, once with planted ones -- so both fixture branches are proved on
    every host, and no host skips the assertion."""
    return request.param == "fake"


class TestALocalLinkToAShareIsRefusedBeforeAnythingFollowsIt:
    """The lexical UNC rule screens the TEXT; a resolve then follows every link
    in the path, and on a Windows gateway a local link whose target is
    ``\\\\host\\share`` makes that resolve open the share -- the probe the rule
    exists to prevent, one link away. So for a NON-PERSON principal (the
    ``set_project`` directive, a non-person request at the slot endpoint or the
    scan root; the person's own request resolves by name as on main, see the
    class above) every link on the way is read without
    being followed by the repo's one link-target screen
    (``hooks.link_screen``, the one ``validate_file_path`` runs for file
    reads), a share-shaped target is refused before anything resolves, and what
    is resolved next is the SCREENED spelling the walk handed back -- never the
    original -- through the repo's pinned open
    (``pinned_fs.real_dir_path_pinned``: an ``openat`` chain under
    ``O_NOFOLLOW`` on POSIX, a root-first chain of handles opened with
    ``FILE_FLAG_OPEN_REPARSE_POINT`` and held on Windows), so a component
    swapped for a link between the screen and the open is refused at the open
    instead of followed, on every host, and no site holds a by-name
    ``realpath`` of its own. Red-first on the head before this class: the
    helper pair did not exist (``AttributeError``), the swap was refused only
    where ``O_NOFOLLOW`` existed and the Windows arm handed the screened
    spelling to ``realpath``.

    Fixtures never skip: a link is real where the OS grants one, a junction
    where it does not and the target exists, and PLANTED otherwise -- and the
    ``planted`` parameter runs every share case both ways on every host.
    """

    def test_a_link_to_a_share_is_refused_by_the_helper(
        self, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf

        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link", fake=planted)
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "share-link")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "share-link")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "share-link")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )

    def test_a_chain_ending_at_a_share_and_a_linked_ancestor_are_refused(
        self, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf

        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link", fake=planted)
        _plant_link(monkeypatch, str(tmp_path / "share-link"), tmp_path / "chain", fake=planted)
        _plant_link(monkeypatch, r"\\evil\share", tmp_path / "share-dir", fake=planted)
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "chain")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )
        # The link is an ANCESTOR of the named path: read root-first, refused
        # before anything below it is touched.
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "share-dir" / "proj")) == (
            "",
            cf.PROJECT_DIR_LINK_REFUSAL,
        )

    def test_benign_links_are_re_spelled_and_resolve_as_they_did(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Scope pin: a link to a local directory -- absolute or relative, the
        leaf or an ancestor -- is not the rule's concern; the screen hands back
        the spelling through the link, and the validator resolves to what it
        names, exactly as before."""
        from kiro_crew import hooks
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "sub").mkdir(parents=True)
        _plant_link(monkeypatch, str(tmp_path / "real"), tmp_path / "dir-link")
        _plant_link(monkeypatch, "./real", tmp_path / "rel-link")
        through = str(tmp_path / "real" / "sub")
        assert cf.link_screen(cf._anchored_spelling(str(tmp_path / "dir-link" / "sub"))) == (
            through,
            hooks.LINK_SCREEN_OK,
        )
        assert cf.link_screen(cf._anchored_spelling(str(tmp_path / "rel-link" / "sub"))) == (
            through,
            hooks.LINK_SCREEN_OK,
        )
        assert cf.link_screen(through) == (through, hooks.LINK_SCREEN_OK)
        resolved = str((tmp_path / "real" / "sub").resolve())
        assert cf._validate_project_dir(str(tmp_path / "dir-link" / "sub")) == (resolved, None)
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "dir-link" / "sub")) == (
            resolved,
            None,
        )

    def test_a_dotdot_after_a_link_resolves_where_the_kernel_resolves_it(
        self, tmp_path: Any
    ) -> None:
        """A walk handed ``os.path.abspath`` of the spelling
        on every host, and ``abspath`` folds ``..`` lexically -- so
        ``work/link/../sibling`` with ``link -> projects/team/subdir`` reached
        ``work/sibling``, where the kernel (and main's ``realpath``) reach
        ``projects/team/sibling``: a different directory for the person's
        chats, admitted silently. The spelling is now anchored only
        (``_anchored_spelling``) and the screen resolves each ``..`` in
        component order, against the directory the link reaches; the pinned
        open then opens ``..`` relative to the directory it holds. On Windows
        the Win32 parser folds ``..`` before any filesystem sees a name, so
        ``abspath`` there is the platform's own rule and the same equality
        holds. Red on the head before this test (POSIX): both sites answered
        ``work/sibling``. Real links only -- a planted link cannot carry the
        kernel's rule -- so a host that grants none skips."""
        from kiro_crew.dashboard import chat_folders as cf

        work = tmp_path / "work"
        (work / "sibling").mkdir(parents=True)
        (work / "other").mkdir()
        team = tmp_path / "projects" / "team"
        (team / "subdir" / "other").mkdir(parents=True)
        (team / "sibling").mkdir()
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "d").mkdir(parents=True)
        (elsewhere / "other").mkdir()
        _real_dir_link(team / "subdir", work / "link")
        _real_dir_link(elsewhere / "d", team / "subdir" / "deep")

        # ``..`` right after the link, and ``..`` after a SECOND link below it.
        for spelling, posix_answer in (
            (os.path.join(str(work), "link", "..", "sibling"), team / "sibling"),
            (os.path.join(str(work), "link", "deep", "..", "other"), elsewhere / "other"),
        ):
            expected = os.path.realpath(spelling)  # main's answer, this host's own rule
            assert cf._validate_project_dir(spelling) == (expected, None), spelling
            assert cf.screen_and_resolve_project_dir(spelling) == (expected, None), spelling
            if os.name != "nt":
                assert expected == str(posix_answer.resolve())
        if os.name == "nt":
            # The platform's lexical rule, stated so the equality above is not
            # vacuous there: ``link\..`` is ``work`` before the junction is seen.
            lexical = os.path.realpath(os.path.join(str(work), "link", "..", "sibling"))
            assert os.path.normcase(lexical) == os.path.normcase(str((work / "sibling").resolve()))

    @pytest.mark.asyncio
    async def test_the_slot_project_endpoint_codes_an_unscreenable_link_apart_from_the_unc_class(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Folding every link cause into the UNC branch --
        ``project_unc_path`` and an SEL line reading ``UNC path`` -- so an
        outbound-credential attempt and an ordinary unscreenable symlink were
        indistinguishable in the audit. Now a share-shaped target is audited as
        the link it is (``UNC link target``, still the UNC code) and a link the
        screen could not read answers its own code with its own SEL line."""
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_project

        (tmp_path / "real").mkdir()
        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link")
        _plant_link(monkeypatch, str(tmp_path / "real"), tmp_path / "dir-link")
        _make_unreadable(monkeypatch, tmp_path / "dir-link")
        sel_fn = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", sel_fn)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        async with TestClient(TestServer(app)) as client:
            share = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "share-link")},
            )
            assert share.status == 400
            assert (await share.json()) == {
                "error": cf.PROJECT_DIR_LINK_REFUSAL,
                "code": "project_unc_path",
            }
            assert sel_fn.return_value.log_api_access.call_args.kwargs["error"] == "UNC link target"
            unreadable = await client.post(
                f"/api/chat/slots/{CALLER}/project",
                json={"project": str(tmp_path / "dir-link")},
            )
            assert unreadable.status == 400
            assert (await unreadable.json()) == {
                "error": cf.PROJECT_DIR_UNSCREENABLE_LINK_REFUSAL,
                "code": "project_dir_link_unscreenable",
            }
            kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
            assert kwargs["operation"] == "chat_slot_project"
            assert kwargs["error"] == "unscreenable link"
        assert state._slots[CALLER].project == ""
        # The folder routes' validator draws the same line, in its own words.
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "dir-link")) == (
            "",
            cf.PROJECT_DIR_UNSCREENABLE_LINK_REFUSAL,
        )

    def test_a_component_swapped_between_the_screen_and_the_open_is_refused_not_followed(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The check-to-use window, simulated on every host: the screen sees a
        plain directory, then -- before the resolve -- that directory becomes a
        link to another. The pinned open meets the link (``O_NOFOLLOW`` on
        POSIX, the reparse point opened as itself on Windows) and refuses;
        nothing traversed it, the other directory is never named, and
        ``realpath`` never sees the original spelling."""
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "plain").mkdir()
        (tmp_path / "other").mkdir()
        real_screen = cf.link_screen

        def _screen_then_swap(target: str) -> tuple[str | None, str]:
            outcome = real_screen(target)
            (tmp_path / "plain").rmdir()
            _plant_link(monkeypatch, str(tmp_path / "other"), tmp_path / "plain")
            return outcome

        monkeypatch.setattr(cf, "link_screen", _screen_then_swap)
        seen: list[str] = []
        real_realpath = os.path.realpath
        monkeypatch.setattr(
            cf.os.path,
            "realpath",
            lambda p, *a, **k: (seen.append(str(p)), real_realpath(p, *a, **k))[1],
        )
        assert cf.screen_and_resolve_project_dir(str(tmp_path / "plain")) == (
            "",
            cf.PROJECT_DIR_UNVERIFIABLE_REFUSAL,
        )
        assert str(tmp_path / "plain") not in seen

    def test_a_host_that_pins_neither_way_refuses_rather_than_resolving_by_name(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """No realpath fallback is left in the admission path: where neither the
        descriptor walk nor the handle chain exists, the resolve fails closed
        and the original spelling reaches no ``realpath``."""
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
        """main's precedence, kept: ``~/.ssh`` that does not exist on this host
        answers the sensitive-path text, not the missing-directory one (the
        Backend Tests (3.12, 2) and (3.12, 8) reds on the previous head)."""
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
        self, state: Any, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        """Through the real routes, as the person (the one caller admitted to
        bind): the link half is a non-person rule, so the person's spelling
        never meets the screen or the pinned open -- a link to a local directory
        lands as ``realpath`` follows it (a link to a share would answer whatever
        main's ``realpath``/``isdir`` answer, never the link refusal -- the UNC
        class above proves the person's share bind with the filesystem faked, so
        no runner contacts a host). Red on the head before this test: the
        person's bind through a link met the screen at both routes."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real").mkdir()
        _plant_link(monkeypatch, str(tmp_path / "real"), tmp_path / "dir-link", fake=planted)
        proj = tmp_path / "proj"
        proj.mkdir()
        fid = await _person_binds(state, "Proj", str(proj))
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran for the person"))
        monkeypatch.setattr(cf, "link_screen", untouched)
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
    async def test_the_slot_project_endpoint_screens_and_resolves_off_the_loop(
        self, state: Any, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        """The endpoint answers 400 ``project_unc_path`` with the link text for a
        share link, resolves a plain directory to its real path, and does both
        through ONE worker-thread call (``asyncio.to_thread``) -- the screen's
        ``lstat``/``readlink`` and the pinned opens never run on the event loop,
        and the endpoint holds no ``realpath`` of its own."""
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard.chat import api_chat_slot_project

        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link", fake=planted)
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
            assert body == {"error": cf.PROJECT_DIR_LINK_REFUSAL, "code": "project_unc_path"}
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
        self, state: Any
    ) -> None:
        """Both arms: a spelling no filesystem can carry is the missing directory,
        not a server error -- the fenced arm's pinned open raised ``ValueError``
        uncaught (GPT), and main's own ``realpath`` raises it too on this
        interpreter, so the person's arm guards it as well."""
        from kiro_crew.dashboard.chat import api_chat_slot_project

        for person in (False, True):
            app = web.Application()
            app["state"] = state

            def _stamp_for(person: bool) -> Any:
                @web.middleware
                async def _stamp(request: web.Request, handler: Any) -> Any:
                    request["app"] = ""
                    if person:
                        request["is_dashboard_user"] = True
                    return await handler(request)

                return _stamp

            app.middlewares.append(_stamp_for(person))
            app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    f"/api/chat/slots/{CALLER}/project", json={"project": "/tmp/a\u0000b"}
                )
                assert resp.status == 400, (person, await resp.text())
                assert (await resp.json()) == {
                    "error": "Not a directory",
                    "code": "project_not_a_directory",
                }
            assert state._slots[CALLER].project == ""

    @pytest.mark.asyncio
    async def test_the_sensitive_verdict_is_taken_on_the_pinned_path_never_by_name(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The check-then-use window one step later: after the pinned resolve, the
        sensitive-path check resolved its argument AGAIN by name
        (``is_sensitive_path`` / ``sensitive_path_refusal`` walk the spelling
        before matching), so a component swapped for a link after the pin -- on
        a Windows gateway, a junction to a share -- was followed by the very
        check meant to fence it. Now the verdict is taken on the real path the
        pin returned, as it stands (``project_dir_sensitive_refusal`` over
        ``is_sensitive_resolved_path``), on the worker thread. Simulated here:
        the pin returns the real path, then the directory at that path becomes a
        link to the home's ``.ssh``. A by-name check followed the swap (answered
        "sensitive", and ``realpath`` saw the pinned path); the no-resolve check
        answers on the pinned path -- admitted, and ``realpath`` never sees it.
        Both fenced sites: the slot project endpoint's non-person arm and the
        ``set_project`` directive. Real links only, so a host without them skips."""
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

    @pytest.mark.asyncio
    async def test_the_set_project_directive_refuses_it_as_denied(
        self, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf
        from kiro_crew.dashboard import session_directive_apply as sda

        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link", fake=planted)
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
    """``_validate_project_dir`` is the STORED-value reader
    (``_resolve_folder_project_dir``, on slot create and agent switch), and it is
    main's: ``realpath``, the sensitive check, ``isdir``, by name. Only the
    person binds a folder, so every stored value is a path the operator chose
    for the operator's own gateway and is honoured exactly as it always was -- a
    link the screen could not read, a parent the pinned open may not open, a
    link whose target is a share, a chain past the screen's horizon. None of
    those meets the screen or the pinned open on the read path; the fenced
    answers are the fenced resolve's (``screen_and_resolve_project_dir``), and
    the pairs below say which caller reads which. Red-first on the head before
    this class: the read path refused the stored share link and the stored
    horizon chain, and the unopenable-ancestor request answered the "retry"
    text.
    """

    @staticmethod
    def _stored(project_dir: str) -> list[dict[str, Any]]:
        return [
            {"id": "fldr00000051", "name": "Bound", "parent_id": "", "project_dir": project_dir}
        ]

    def test_a_stored_binding_through_an_unreadable_link_resolves_as_before(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "sub").mkdir(parents=True)
        _plant_link(monkeypatch, str(tmp_path / "real"), tmp_path / "dir-link")
        _make_unreadable(monkeypatch, tmp_path / "dir-link")
        stored = str(tmp_path / "dir-link" / "sub")
        # A non-person request is refused: the agent re-spells it.
        assert cf.screen_and_resolve_project_dir(stored) == (
            "",
            cf.PROJECT_DIR_UNSCREENABLE_LINK_REFUSAL,
        )
        # The stored value is honoured: main's own resolution.
        expected = os.path.realpath(str(tmp_path / "real" / "sub"))
        assert cf._validate_project_dir(stored) == (expected, None)
        assert _resolve_folder_project_dir(self._stored(stored), "fldr00000051") == (
            expected,
            None,
        )

    def test_an_ancestor_this_process_may_not_open_is_not_a_swap(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The pinned open cannot verify what it may not open (a denied ACL): a
        non-person request answers the unopenable text -- not a spelling problem
        and not a swap, so neither "re-spell" nor "retry" -- and the stored value
        resolves by name as it always did."""
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

    @pytest.mark.skipif(
        (not getattr(os, "O_PATH", 0) or os.geteuid() == 0) if hasattr(os, "geteuid") else True,
        reason="a search-only ancestor needs POSIX modes, O_PATH and a non-root process",
    )
    def test_a_directory_under_a_search_only_ancestor_still_pins(self, tmp_path: Any) -> None:
        """Opus: ``pin_parent`` opened every ancestor read-only, which needs READ
        permission on each, while main's ``realpath`` needed only SEARCH -- so an
        existing directory under a ``--x`` ancestor was refused on the fenced
        path for good. The chain now opens with ``O_PATH`` where the platform
        has it: search permission alone, a link or a file at a component still
        ``ENOTDIR``. The fenced answer equals main's by-name answer."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        gate = tmp_path / "gate"
        proj = gate / "proj"
        proj.mkdir(parents=True)
        (tmp_path / "other").mkdir()
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
        self, tmp_path: Any, monkeypatch: Any, planted: bool
    ) -> None:
        """The person bound it (no one else can), so the read path resolves it
        as main did -- by name, the screen never consulted. The filesystem is
        faked at the link so no runner contacts a host; the non-person admission
        of the same spelling is the refusal."""
        from kiro_crew import pinned_fs
        from kiro_crew.dashboard import chat_folders as cf

        _plant_link(monkeypatch, "//evil/share/proj", tmp_path / "share-link", fake=planted)
        stored = str(tmp_path / "share-link")
        assert cf.screen_and_resolve_project_dir(stored) == ("", cf.PROJECT_DIR_LINK_REFUSAL)
        untouched = MagicMock(side_effect=AssertionError("the fenced path ran on the read path"))
        monkeypatch.setattr(cf, "link_screen", untouched)
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

    def test_a_stored_chain_past_the_screens_horizon_is_honoured_too(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The horizon is a probe cause for a NON-PERSON request (the unread tail
        of a chain the screen could not finish is what a by-name resolve would
        follow); a stored binding is the person's and resolves as main did."""
        from kiro_crew import hooks
        from kiro_crew.dashboard import chat_folders as cf

        (tmp_path / "real" / "sub").mkdir(parents=True)
        _plant_link(monkeypatch, str(tmp_path / "real"), tmp_path / "hop1")
        _plant_link(monkeypatch, str(tmp_path / "hop1"), tmp_path / "hop2")
        monkeypatch.setattr(hooks, "_WINDOWS_LINK_CHAIN_MAX", 1)
        stored = str(tmp_path / "hop2" / "sub")
        assert hooks.link_screen(stored) == (None, hooks.LINK_SCREEN_CHAIN_TOO_LONG)
        assert cf.screen_and_resolve_project_dir(stored) == ("", cf.PROJECT_DIR_LINK_REFUSAL)
        expected = os.path.realpath(str(tmp_path / "real" / "sub"))
        assert cf._validate_project_dir(stored) == (expected, None)

    def test_an_unrepresentable_spelling_is_the_missing_directory_not_a_server_error(
        self, tmp_path: Any
    ) -> None:
        """GPT: a spelling no filesystem can carry (an embedded NUL) passed the
        screen unchanged (``islink`` swallows the ``ValueError``) and reached the
        pinned open, whose ``os.open`` raised it uncaught -- an HTTP 500 where
        main's ``realpath``/``isdir`` swallowed it and answered the
        missing-directory refusal. Both arms answer that refusal now."""
        from kiro_crew.dashboard import chat_folders as cf

        spelling = os.path.join(str(tmp_path), "a\x00b")
        assert cf.screen_and_resolve_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)
        assert cf.screen_and_resolve_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)
        assert cf._validate_project_dir(spelling) == ("", cf.PROJECT_DIR_MISSING_REFUSAL)


def _fake_fs_for(monkeypatch: Any, path: str) -> None:
    """Make *path* look like an existing, resolvable directory to the validator
    WITHOUT the process touching it: ``realpath`` is the identity and ``isdir``
    true for that one string, and the real functions otherwise. On a Windows
    shard the real ``realpath`` on a UNC string would contact the host."""
    import kiro_crew.dashboard.chat_folders as cf

    real_realpath, real_isdir = os.path.realpath, os.path.isdir
    monkeypatch.setattr(
        os.path, "realpath", lambda p, **kw: p if p == path else real_realpath(p, **kw)
    )
    monkeypatch.setattr(os.path, "isdir", lambda p: True if p == path else real_isdir(p))
    monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)


class TestTheReadPathResolvesAStoredValueAsBefore:
    """``_validate_project_dir`` is also the STORED-value reader: the slot-create
    and agent-switch paths run it through ``_resolve_folder_project_dir`` over
    what ``folders.json`` holds. A folder the person bound to a network share
    before the UNC rule existed is therefore not a request to admit but a
    binding to honour: refusing it at read time would fail every ``POST
    /api/chat/slots`` in that folder with 400 and drop the agent to the
    workspace default -- a retroactive refusal with no migration. So the UNC
    refusal is admission-only, and the read path resolves a stored value
    exactly as it did before the rule; a NEW one is still refused where it is
    named (the class above).
    """

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
        """Whatever the ordinary validation says about a stored spelling on this
        host (a backslash form is not "absolute" on POSIX), it is never the
        lexical UNC refusal -- the filesystem is faked, so no host is contacted."""
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
        """The route the retroactive refusal would have broken: a folder whose
        STORED binding is UNC-shaped (written before the rule; admission refuses
        the shape today, so it is placed in the store directly), and a chat
        opened in it the way the dashboard does."""
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
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "legacy-chat", "folder_id": "fldr00000041"}
            )
            data = await resp.json()
        assert resp.status == 200, data
        assert data["project"] == self.STORED
        assert state._slots["legacy-chat"].project == self.STORED
