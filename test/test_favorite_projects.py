"""The favourite-projects store and its three endpoints.

Two layers, deliberately separate. The store helpers are tested directly because
their load-bearing property is invisible from the HTTP surface: the write paths read
the file WITHOUT probing the filesystem, so a favourite whose directory is currently
unreachable survives somebody else's add or remove instead of being deleted as a side
effect. The endpoints are tested through a real ``TestClient`` because what they add
is the validation and the owner gate, and a gate asserted by calling the handler
directly is a gate that was never routed.

Everything here is in-process: ``config_dir`` is redirected at ``tmp_path`` so no test
touches the real crew home, and the one gate anchored on the real ``$HOME``
(``is_sensitive_path``) is injected rather than satisfied with a credential directory
on the machine running the suite -- the same two accommodations ``TestRecentProjects``
makes in ``test_dashboard_chat_handlers_coverage.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers as ch

MOD = "kiro_crew.dashboard.chat_handlers"


@pytest.fixture
def _sel():
    """Neutralize the security event log (it otherwise opens the real store)."""
    fake = MagicMock()
    with patch(f"{MOD}.sel", return_value=fake):
        yield fake


@pytest.fixture
def _home(tmp_path: Path):
    """Point the favourites file at tmp_path."""
    with patch(f"{MOD}.config_dir", return_value=tmp_path):
        yield tmp_path


def _make_app(*, app_claim: str | None = "", user: str = "owner") -> web.Application:
    """App carrying the three routes and one request's auth claims.

    ``app_claim`` mirrors ``token_auth_middleware``'s ``request["app"]``: ``""`` for a
    dashboard user, a name for an app caller, ``None`` to leave the key absent (the
    auth middleware never ran). ``state.owner_id`` has to match ``user`` for the write
    gate to pass -- a bare MagicMock attribute would 403 every request for the wrong
    reason.
    """

    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    state.owner_id = "owner"
    app["state"] = state
    app.router.add_get("/api/favorite-projects", ch.api_favorite_projects)
    app.router.add_post("/api/favorite-projects", ch.api_favorite_project_add)
    app.router.add_delete("/api/favorite-projects", ch.api_favorite_project_remove)
    return app


class TestFavoriteProjectsStore:
    def test_path_is_derived_from_config_dir(self, _home):
        assert ch._favorite_projects_path() == _home / "favorite_projects.json"

    def test_an_absent_store_reads_empty(self, _home):
        assert ch._read_favorite_projects() == []
        assert ch._load_favorite_projects() == []

    def test_add_appends_and_reports_that_it_fit(self, _home):
        proj = _home / "a"
        proj.mkdir()
        assert ch._add_favorite_project(str(proj)) == ([str(proj)], True)
        assert json.loads(ch._favorite_projects_path().read_text(encoding="utf-8")) == [str(proj)]

    def test_add_is_idempotent(self, _home):
        proj = _home / "a"
        proj.mkdir()
        ch._add_favorite_project(str(proj))
        # Already favourited is a success, not an error: the toggle's "on" state can be
        # requested twice (two tabs, a double click) and the list already says so.
        assert ch._add_favorite_project(str(proj)) == ([str(proj)], True)
        assert ch._load_favorite_projects() == [str(proj)]

    def test_add_keeps_the_order_the_user_built(self, _home):
        first, second = _home / "a", _home / "b"
        first.mkdir()
        second.mkdir()
        ch._add_favorite_project(str(second))
        ch._add_favorite_project(str(first))
        # Insertion order, NOT recency and not the alphabet: the list is curated by hand.
        assert ch._load_favorite_projects() == [str(second), str(first)]

    def test_a_full_list_refuses_rather_than_dropping_the_oldest(self, _home):
        stored = [f"/nowhere/x{i}" for i in range(ch._MAX_FAVORITE_PROJECTS)]
        ch._write_favorite_projects(stored)
        proj = _home / "a"
        proj.mkdir()
        _, fit = ch._add_favorite_project(str(proj))
        assert fit is False
        # Nothing written: a recents window legitimately evicts its oldest entry, a
        # favourite the user chose must not vanish to make room.
        assert ch._load_favorite_projects() == stored

    def test_the_display_read_hides_a_directory_that_is_gone(self, _home):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj), str(_home / "gone")])
        assert ch._read_favorite_projects() == [str(proj)]

    def test_the_display_read_hides_a_sensitive_path(self, _home):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        with patch(f"{MOD}.is_sensitive_path", return_value=True):
            assert ch._read_favorite_projects() == []

    def test_an_unreachable_favourite_survives_someone_elses_write(self, _home):
        """The property the two reads exist to separate.

        A favourite on a detached volume is hidden from the list but must still be in
        the file afterwards -- an add of an unrelated path must not delete it.
        """
        proj, gone = _home / "a", _home / "gone"
        proj.mkdir()
        ch._write_favorite_projects([str(gone)])
        dirs, _ = ch._add_favorite_project(str(proj))
        assert dirs == [str(proj)]  # hidden from display
        assert ch._load_favorite_projects() == [str(gone), str(proj)]  # kept in the file

    def test_load_dedupes_and_drops_what_is_not_a_path(self, _home):
        ch._favorite_projects_path().write_text(
            json.dumps(["/a", "/a", 7, None, "", "/b"]), encoding="utf-8"
        )
        assert ch._load_favorite_projects() == ["/a", "/b"]

    @pytest.mark.parametrize("body", ["{not json", json.dumps({"dirs": []}), ""])
    def test_an_unreadable_store_reads_empty(self, _home, body):
        ch._favorite_projects_path().write_text(body, encoding="utf-8")
        assert ch._load_favorite_projects() == []
        assert ch._read_favorite_projects() == []

    def test_a_write_leaves_no_temp_file_behind(self, _home):
        ch._write_favorite_projects(["/a"])
        assert list(_home.glob("*.tmp")) == []

    def test_a_failed_write_cleans_up_its_temp_file(self, _home):
        with patch(f"{MOD}.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                ch._write_favorite_projects(["/a"])
        assert list(_home.glob("*.tmp")) == []

    def test_remove_drops_the_path_as_given(self, _home):
        ch._write_favorite_projects(["/a", "/b"])
        assert ch._remove_favorite_project("/b") == []  # neither exists on disk
        assert ch._load_favorite_projects() == ["/a"]

    def test_remove_drops_a_stored_resolved_spelling(self, _home):
        """The add path stores a RESOLVED path; a remove naming the symlink must match it."""
        real = _home / "real"
        real.mkdir()
        link = _home / "link"
        link.symlink_to(real)
        ch._add_favorite_project(str(os.path.realpath(link)))
        assert ch._load_favorite_projects() == [str(real)]
        ch._remove_favorite_project(str(link))
        assert ch._load_favorite_projects() == []

    def test_remove_of_something_absent_changes_nothing(self, _home):
        ch._write_favorite_projects(["/a"])
        ch._remove_favorite_project("/not-favourited")
        assert ch._load_favorite_projects() == ["/a"]


class TestFavoriteProjectsEndpoints:
    @pytest.mark.asyncio
    async def test_get_lists_the_stored_favourites(self, _home, _sel):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/favorite-projects")
            assert resp.status == 200
            assert (await resp.json())["dirs"] == [str(proj)]

    @pytest.mark.asyncio
    async def test_get_tolerates_an_unreadable_store(self, _home, _sel):
        ch._favorite_projects_path().write_text("[[[", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/favorite-projects")
            assert resp.status == 200
            assert (await resp.json())["dirs"] == []

    @pytest.mark.asyncio
    async def test_an_app_caller_may_read_the_list(self, _home, _sel):
        """Ungated, matching the recents read: an installed app may show the list."""
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        async with TestClient(TestServer(_make_app(app_claim="some-app"))) as client:
            resp = await client.get("/api/favorite-projects")
            assert resp.status == 200
            assert (await resp.json())["dirs"] == [str(proj)]

    @pytest.mark.asyncio
    async def test_post_favourites_a_directory(self, _home, _sel):
        proj = _home / "a"
        proj.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/favorite-projects", json={"path": str(proj)})
            assert resp.status == 200
            assert (await resp.json())["dirs"] == [str(proj)]

    @pytest.mark.asyncio
    async def test_post_stores_the_resolved_path(self, _home, _sel):
        """What lets the picker's star compare a row against this list by string."""
        real = _home / "real"
        real.mkdir()
        link = _home / "link"
        link.symlink_to(real)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/favorite-projects", json={"path": str(link)})
            assert (await resp.json())["dirs"] == [str(real)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"path": 7}, "path_not_a_string"),
            ({"path": "   "}, "path_required"),
            ({}, "path_required"),
        ],
    )
    async def test_post_refuses_a_path_it_cannot_use(self, _home, _sel, body, code):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/favorite-projects", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == code

    @pytest.mark.asyncio
    async def test_post_refuses_a_body_that_is_not_a_json_object(self, _home, _sel):
        """``read_bounded_json``'s own refusal, before any field is read."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/favorite-projects",
                data="not json at all",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        assert ch._load_favorite_projects() == []

    @pytest.mark.asyncio
    async def test_post_refuses_something_that_is_not_a_directory(self, _home, _sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/favorite-projects", json={"path": str(_home / "missing")}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "not_a_directory"

    @pytest.mark.asyncio
    async def test_post_refuses_a_sensitive_path_and_records_the_denial(self, _home, _sel):
        proj = _home / "a"
        proj.mkdir()
        with patch(f"{MOD}.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/favorite-projects", json={"path": str(proj)})
                assert resp.status == 403
                assert (await resp.json())["code"] == "denied"
        assert ch._load_favorite_projects() == []
        outcomes = [c.kwargs.get("outcome") for c in _sel.log_api_access.call_args_list]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_post_answers_409_when_the_list_is_full(self, _home, _sel):
        stored = [f"/nowhere/x{i}" for i in range(ch._MAX_FAVORITE_PROJECTS)]
        ch._write_favorite_projects(stored)
        proj = _home / "a"
        proj.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/favorite-projects", json={"path": str(proj)})
            assert resp.status == 409
            assert (await resp.json())["code"] == "favorites_full"
        assert ch._load_favorite_projects() == stored

    @pytest.mark.asyncio
    async def test_delete_un_favourites_a_directory(self, _home, _sel):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete("/api/favorite-projects", params={"path": str(proj)})
            assert resp.status == 200
            assert (await resp.json())["dirs"] == []
        assert ch._load_favorite_projects() == []

    @pytest.mark.asyncio
    async def test_delete_requires_a_path(self, _home, _sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete("/api/favorite-projects")
            assert resp.status == 400
            assert (await resp.json())["code"] == "path_required"

    @pytest.mark.asyncio
    async def test_delete_clears_a_favourite_whose_directory_is_gone(self, _home, _sel):
        """The row a user most needs to remove is the one the add path would now refuse."""
        gone = str(_home / "gone")
        ch._write_favorite_projects([gone])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete("/api/favorite-projects", params={"path": gone})
            assert resp.status == 200
        assert ch._load_favorite_projects() == []

    @pytest.mark.asyncio
    async def test_delete_clears_a_favourite_that_became_sensitive(self, _home, _sel):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        with patch(f"{MOD}.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.delete("/api/favorite-projects", params={"path": str(proj)})
                assert resp.status == 200
        assert ch._load_favorite_projects() == []


class TestFavoriteProjectsWriteGate:
    """The writes are the owner's own; only the read is shared with an app caller."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_an_app_caller_cannot_write(self, _home, _sel, method):
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        async with TestClient(TestServer(_make_app(app_claim="some-app"))) as client:
            resp = await getattr(client, method)(
                "/api/favorite-projects", json={"path": str(proj)}, params={"path": str(proj)}
            )
            assert resp.status == 403
        assert ch._load_favorite_projects() == [str(proj)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_another_dashboard_subject_cannot_write(self, _home, _sel, method):
        """A dashboard token minted for a different subject carries ``app == ""`` too."""
        proj = _home / "a"
        proj.mkdir()
        ch._write_favorite_projects([str(proj)])
        app = _make_app(app_claim="", user="somebody-else")
        async with TestClient(TestServer(app)) as client:
            resp = await getattr(client, method)(
                "/api/favorite-projects", json={"path": str(proj)}, params={"path": str(proj)}
            )
            assert resp.status == 403
        assert ch._load_favorite_projects() == [str(proj)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_an_absent_auth_claim_is_refused(self, _home, _sel, method):
        """Deny-by-default: no ``app`` key means the auth middleware never ran."""
        proj = _home / "a"
        proj.mkdir()
        app = _make_app(app_claim=None)
        async with TestClient(TestServer(app)) as client:
            resp = await getattr(client, method)(
                "/api/favorite-projects", json={"path": str(proj)}, params={"path": str(proj)}
            )
            assert resp.status == 403
