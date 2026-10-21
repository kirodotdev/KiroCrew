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
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
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

    def test_add_appends_and_returns_the_display_list(self, _home):
        proj = _home / "a"
        proj.mkdir()
        assert ch._add_favorite_project(str(proj)) == [str(proj)]
        assert json.loads(ch._favorite_projects_path().read_text(encoding="utf-8")) == [str(proj)]

    def test_add_is_idempotent(self, _home):
        proj = _home / "a"
        proj.mkdir()
        ch._add_favorite_project(str(proj))
        # Already favourited is a success, not an error: the toggle's "on" state can be
        # requested twice (two tabs, a double click) and the list already says so.
        assert ch._add_favorite_project(str(proj)) == [str(proj)]
        assert ch._load_favorite_projects() == [str(proj)]

    def test_add_keeps_the_order_the_user_built(self, _home):
        first, second = _home / "a", _home / "b"
        first.mkdir()
        second.mkdir()
        ch._add_favorite_project(str(second))
        ch._add_favorite_project(str(first))
        # Insertion order, NOT recency and not the alphabet: the list is curated by hand.
        assert ch._load_favorite_projects() == [str(second), str(first)]

    def test_overlapping_writes_lose_neither_change(self, _home):
        # Two dashboard tabs adding different projects at once. The add transaction
        # loads the snapshot, appends, and writes it back under _favorite_projects_lock;
        # without that lock both adds load the same empty snapshot and the second write
        # drops the first project. This proves the lock closes that window.
        #
        # The interleaving is driven by signals, not a sleep or a timeout. The first add
        # to load signals ``first_loaded`` and parks on ``release``; the main thread then
        # asserts _favorite_projects_lock is HELD -- which it is only because the lock
        # serializes the read-modify-write, so this assertion is itself the regression
        # detector: drop the lock and a reader reaches this point with nothing held. The
        # main thread releases immediately, so the passing run finishes in milliseconds;
        # the bounded join is the only lost-run ceiling. With the lock the second add is
        # still blocked acquiring it, loads after the first has written, and both land;
        # without it both wrote from the empty snapshot and the second clobbers the first,
        # which the final assertion catches on every host.
        first, second = _home / "a", _home / "b"
        first.mkdir()
        second.mkdir()

        real_load = ch._load_favorite_projects
        first_loaded = threading.Event()
        release = threading.Event()
        claimed = threading.Lock()
        is_claimed = [False]

        def handshake_load(**kwargs):
            snapshot = real_load(**kwargs)
            with claimed:
                park = not is_claimed[0]
                is_claimed[0] = True
            if park:
                first_loaded.set()
                assert release.wait(timeout=10), "the main thread never released the first load"
            return snapshot

        with patch(f"{MOD}._load_favorite_projects", side_effect=handshake_load):
            threads = [
                threading.Thread(target=ch._add_favorite_project, args=(str(p),))
                for p in (first, second)
            ]
            for t in threads:
                t.start()
            try:
                assert first_loaded.wait(timeout=10), "no add ever loaded its snapshot"
                # The lock is held across the first add's load-modify-write only because
                # it serializes the transaction; a dropped lock lets a reader park here
                # with nothing held, so this is the regression detector, not a timing bet.
                assert ch._favorite_projects_lock.locked(), "the transaction did not hold the lock"
            finally:
                release.set()  # always free the parked load, even if an assertion fired
            for t in threads:
                t.join(timeout=30)
            assert not any(t.is_alive() for t in threads), "an add thread never returned"
        assert sorted(ch._load_favorite_projects()) == sorted([str(first), str(second)])

    def test_a_display_read_in_flight_never_fails_a_write(self, _home):
        # Windows refuses to rename over a file another handle has open (WinError 5/32);
        # POSIX allows it, which is why this only ever failed on the Windows shard. The
        # display read -- every GET, and the tail of every add and remove -- opened the
        # file with no lock, so an add whose rename landed while it was open raised
        # PermissionError and its project was lost (main CI on 4f16fd2ffc, in the
        # overlapping-writes test above). The display read now loads under the same lock
        # as the write transaction, so no read in this process is open during a rename.
        #
        # ``windows_replace`` is the Windows rule: it refuses while a read is open. The
        # interleaving is driven by signals, not sleeps. The display read parks with the
        # file "open"; the add then either finds the lock held and waits (the reader
        # holds it) or reaches the rename while the read is open and fails. The lock
        # stand-in reports the first case and the add's own exit the second, so the
        # reader is released only once the add has done one or the other.
        proj = _home / "a"
        proj.mkdir()
        fp = os.fspath(ch._favorite_projects_path())
        real_load, real_replace = ch._load_favorite_projects, os.replace
        open_reads = [0]
        reader_open, release, add_progressed = (threading.Event() for _ in range(3))
        add_errors: list[BaseException] = []

        def parked_display_load(**kwargs):
            if kwargs.get("strict") or reader_open.is_set():
                return real_load(**kwargs)  # the add's transaction and its tail read
            open_reads[0] += 1
            try:
                reader_open.set()
                assert release.wait(timeout=10), "the main thread never released the read"
                return real_load(**kwargs)
            finally:
                open_reads[0] -= 1

        def windows_replace(src, dst, *args, **kwargs):
            if os.fspath(dst) == fp and open_reads[0]:
                raise PermissionError(13, "[WinError 5] Access is denied", fp)
            return real_replace(src, dst, *args, **kwargs)

        class ContentionReportingLock:
            """The transaction lock, signalling when a caller finds it already held."""

            def __init__(self) -> None:
                self._lock = threading.Lock()

            def __enter__(self):
                if not self._lock.acquire(blocking=False):
                    add_progressed.set()
                    self._lock.acquire()
                return self

            def __exit__(self, *exc) -> None:
                self._lock.release()

            def locked(self) -> bool:
                return self._lock.locked()

        def add() -> None:
            try:
                ch._add_favorite_project(str(proj))
            except BaseException as exc:  # recorded for the assertion below
                add_errors.append(exc)
            finally:
                add_progressed.set()

        with (
            patch(f"{MOD}._load_favorite_projects", side_effect=parked_display_load),
            patch(f"{MOD}._favorite_projects_lock", ContentionReportingLock()),
            patch("os.replace", windows_replace),
        ):
            reader = threading.Thread(target=ch._read_favorite_projects)
            adder = threading.Thread(target=add)
            reader.start()
            try:
                assert reader_open.wait(timeout=10), "the display read never loaded"
                adder.start()
                assert add_progressed.wait(timeout=10), "the add neither waited nor wrote"
            finally:
                release.set()  # always free the parked read, even if an assertion fired
            for t in (reader, adder):
                t.join(timeout=30)
            assert not reader.is_alive() and not adder.is_alive(), "a thread never returned"
        assert add_errors == []
        assert ch._load_favorite_projects() == [str(proj)]

    def test_a_write_survives_a_windows_sharing_violation_at_the_rename(self, _home, monkeypatch):
        # A handle from outside this process -- an indexer, an AV scanner on the fresh
        # temp file -- fails a Windows rename the same way, and no lock here can stop
        # it. A bare ``os.replace`` turned that moment into a failed add; the write goes
        # through the shared ``atomic_write``, whose rename retries that window.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
        proj = _home / "a"
        proj.mkdir()
        with replace_sharing_violation(match="favorite_projects.json", times=2) as sim:
            ch._add_favorite_project(str(proj))
        assert sim["n"] == 3
        assert ch._load_favorite_projects() == [str(proj)]
        assert list(_home.glob("*.tmp")) == []

    def test_an_add_past_a_hundred_still_lands(self, _home):
        # No cap: the file is owner-written only, so there is nothing a bound defends.
        stored = [str(_home / f"gone{i}") for i in range(100)]
        ch._write_favorite_projects(stored)
        proj = _home / "a"
        proj.mkdir()
        assert ch._add_favorite_project(str(proj)) == [str(proj)]
        assert ch._load_favorite_projects() == [*stored, str(proj)]

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
        dirs = ch._add_favorite_project(str(proj))
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

    @pytest.mark.parametrize("body", ["{not json", json.dumps({"dirs": []}), ""])
    def test_a_strict_load_refuses_an_unreadable_store(self, _home, body):
        ch._favorite_projects_path().write_text(body, encoding="utf-8")
        with pytest.raises(ch.FavoritesUnreadable):
            ch._load_favorite_projects(strict=True)

    def test_a_strict_load_of_an_absent_store_is_empty(self, _home):
        assert ch._load_favorite_projects(strict=True) == []

    @pytest.mark.parametrize("write", ["add", "remove"])
    def test_a_write_never_replaces_an_unreadable_store(self, _home, write):
        # A hand edit with a trailing comma must not cost the user their whole list:
        # writing back the empty display snapshot would replace it with one entry.
        body = '["/home/dev/keep",]'
        ch._favorite_projects_path().write_text(body, encoding="utf-8")
        proj = _home / "a"
        proj.mkdir()
        with pytest.raises(ch.FavoritesUnreadable):
            if write == "add":
                ch._add_favorite_project(str(proj))
            else:
                ch._remove_favorite_project("/home/dev/keep")
        assert ch._favorite_projects_path().read_text(encoding="utf-8") == body

    def test_a_write_leaves_no_temp_file_behind(self, _home):
        ch._write_favorite_projects(["/a"])
        assert list(_home.glob("*.tmp")) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_write_publishes_the_file_owner_only(self, _home):
        # The hand-rolled write published mkstemp's 0o600; the shared writer's default
        # outside the data home is the umask's, so the mode is pinned explicitly.
        ch._write_favorite_projects(["/a"])
        assert ch._favorite_projects_path().stat().st_mode & 0o777 == 0o600

    def test_a_failed_write_cleans_up_its_temp_file(self, _home):
        with patch(f"{MOD}.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                ch._write_favorite_projects(["/a"])
        assert list(_home.glob("*.tmp")) == []

    def test_remove_drops_the_path_as_given(self, _home):
        # Absent under tmp_path on every OS. A bare "/a" is NOT absent everywhere: on a
        # GitHub Windows runner it resolves to D:\a, the runner's own workspace root.
        gone_a, gone_b = str(_home / "gone-a"), str(_home / "gone-b")
        ch._write_favorite_projects([gone_a, gone_b])
        assert ch._remove_favorite_project(gone_b) == []  # neither exists on disk
        assert ch._load_favorite_projects() == [gone_a]

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
    async def test_writes_refuse_an_unreadable_store_and_leave_it_untouched(self, _home, _sel):
        body = '["/home/dev/keep",]'
        ch._favorite_projects_path().write_text(body, encoding="utf-8")
        proj = _home / "a"
        proj.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            add = await client.post("/api/favorite-projects", json={"path": str(proj)})
            assert add.status == 409
            payload = await add.json()
            assert payload["code"] == "favorites_unreadable"
            assert "favorite_projects.json" in payload["error"]
            rm = await client.delete("/api/favorite-projects", params={"path": "/home/dev/keep"})
            assert rm.status == 409
            assert (await rm.json())["code"] == "favorites_unreadable"
        assert ch._favorite_projects_path().read_text(encoding="utf-8") == body
        outcomes = [c.kwargs.get("outcome") for c in _sel.log_api_access.call_args_list]
        assert outcomes.count("denied") == 2

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
    async def test_post_resolves_the_path_off_the_event_loop(self, _home, _sel):
        # realpath walks the symlink chain; on a stalled network mount it would freeze
        # every request and the heartbeat if it ran on the loop thread.
        proj = _home / "a"
        proj.mkdir()
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_realpath = os.path.realpath

        def recording_realpath(p, *a, **kw):
            seen.append(threading.get_ident())
            return real_realpath(p, *a, **kw)

        with patch(f"{MOD}.os.path.realpath", side_effect=recording_realpath):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.post("/api/favorite-projects", json={"path": str(proj)})
                assert resp.status == 200
        assert seen, "the add path must resolve the path it stores"
        assert loop_thread not in seen

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
