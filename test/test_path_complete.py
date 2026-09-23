"""Tests for ``GET /api/path-complete`` — the composer's ``./`` path completion.

The load-bearing behaviour here is containment: the endpoint takes a caller
relative directory and joins it onto an allow-listed project root, so ``../``
runs and symlinks are the whole risk surface and each has its own test.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_path_complete


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/path-complete", api_path_complete)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture()
def project(tmp_path):
    """A project root with one subdirectory, one file, and one dot file."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("x")
    (root / "src" / "appdata").mkdir()
    (root / "readme.md").write_text("hi")
    (root / ".env").write_text("SECRET=1")
    return root


async def _get(known: str, **params) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(known))) as client:
        resp = await client.get("/api/path-complete", params=params)
        return resp.status, await resp.json()


def _names(payload: dict) -> list[str]:
    return [r["name"] for r in payload["results"]]


class TestPathComplete:
    @pytest.mark.asyncio
    async def test_missing_path_is_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/path-complete?dir=./")
            assert resp.status == 400
            assert (await resp.json())["code"] == "path_required"

    @pytest.mark.asyncio
    async def test_unknown_project_is_403(self, tmp_path, project, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        status, data = await _get(str(project), path=str(other), dir="./")
        assert status == 403
        assert data["code"] == "unknown_project_dir"

    @pytest.mark.asyncio
    async def test_lists_the_project_root_for_a_bare_dot_slash(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        # Directories first, then alphabetical.
        assert _names(data) == ["src", "readme.md"]
        assert data["results"][0]["kind"] == "dir"
        assert data["root"] == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_lists_a_subdirectory(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./src/")
        assert status == 200
        assert _names(data) == ["appdata", "app.ts"]

    @pytest.mark.asyncio
    async def test_prefix_narrows_case_insensitively(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./", q="READ")
        assert status == 200
        assert _names(data) == ["readme.md"]

    @pytest.mark.asyncio
    async def test_dot_entries_are_hidden_until_the_dot_is_typed(self, project, mock_sel):
        _, listed = await _get(str(project), path=str(project), dir="./")
        assert ".env" not in _names(listed)
        _, asked = await _get(str(project), path=str(project), dir="./", q=".")
        assert ".env" in _names(asked)

    @pytest.mark.asyncio
    async def test_a_parent_run_that_escapes_the_project_returns_no_results(
        self, tmp_path, project, mock_sel
    ):
        """`../` may not leave the root: the sibling directory exists and is not
        sensitive, so an unchecked join would have listed it."""
        sibling = tmp_path / "outside"
        sibling.mkdir()
        (sibling / "secrets.txt").write_text("x")
        status, data = await _get(str(project), path=str(project), dir="../outside/")
        assert status == 200
        assert data["results"] == []
        # The one fact the picker cannot work out for itself, and its only consumer.
        assert data["outside"] is True

    @pytest.mark.asyncio
    async def test_a_parent_run_inside_the_project_still_resolves(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./src/../")
        assert status == 200
        assert _names(data) == ["src", "readme.md"]

    @pytest.mark.asyncio
    async def test_a_leading_parent_run_that_comes_back_inside_lists_entries(
        self, project, mock_sel
    ):
        """`../` is refused as a destination, never as a shape.

        Going up and back down into the same project is what a shell does and what
        the containment rule allows, so a token whose LEADING `../` run re-enters
        the project lists that directory.
        """
        status, data = await _get(str(project), path=str(project), dir=f"../{project.name}/src/")
        assert status == 200
        assert _names(data) == ["appdata", "app.ts"]

    @pytest.mark.asyncio
    async def test_an_absolute_dir_is_not_honoured(self, tmp_path, project, mock_sel):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secrets.txt").write_text("x")
        status, data = await _get(str(project), path=str(project), dir=str(outside))
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_no_caller_influenced_path_is_ever_resolved(self, project, mock_sel, monkeypatch):
        """The invariant that ends the whole finding class.

        On Windows ``realpath`` opens the final path, so resolving anything a
        caller steered is itself an outbound SMB authentication when a link in it
        aims at a share -- and a screen placed BEFORE the resolve only narrows the
        window a same-UID writer has to swap one in. So nothing below the project
        dir is resolved at all, and the resolver is replaced with a tripwire that
        fails if it is ever handed such a path.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod.os.path.realpath
        base = real(str(project))

        def _tripwire(path, *args, **kwargs):
            resolved = real(path, *args, **kwargs)
            text = str(path)
            if "attacker" in text or text.startswith(base + os.sep):
                raise AssertionError(f"a caller-influenced path was resolved: {path!r}")
            return resolved

        (project / "share").symlink_to("//attacker/share/secrets")
        monkeypatch.setattr(files_mod.os.path, "realpath", _tripwire)
        for token in ("./", "./src/", "../", "//attacker/share/", "./share/"):
            status, data = await _get(str(project), path=str(project), dir=token)
            assert status == 200
        # And the listing still worked while resolving nothing.
        status, data = await _get(str(project), path=str(project), dir="./src/")
        assert _names(data) == ["appdata", "app.ts"]

    def test_the_lexical_resolver_decides_containment_without_the_filesystem(self):
        """`_completion_segments` IS the containment decision, and it touches nothing."""
        from kiro_crew.dashboard.handlers import files as files_mod

        root = "/work/proj"
        assert files_mod._completion_segments(root, "./") == []
        assert files_mod._completion_segments(root, "./src/") == ["src"]
        assert files_mod._completion_segments(root, "./src/../") == []
        assert files_mod._completion_segments(root, "./a/b/") == ["a", "b"]
        # Out of the project: nothing under the root can be named this way.
        assert files_mod._completion_segments(root, "../") is None
        assert files_mod._completion_segments(root, "../../src/") is None
        assert files_mod._completion_segments(root, "../sibling/") is None
        assert files_mod._completion_segments(root, "/etc/") is None
        assert files_mod._completion_segments(root, "//attacker/share/") is None
        assert files_mod._completion_segments(root, "\\\\attacker\\share\\") is None
        # A backslash IS a separator on Windows, so a token carrying one must be
        # SPLIT here rather than appended as one literal name that the OS then
        # re-interprets at the open -- which is how this escaped a checked root.
        assert files_mod._completion_segments(root, "..\\..\\etc\\") is None
        assert files_mod._completion_segments(root, ".\\src\\") == ["src"]
        assert files_mod._completion_segments(root, "./a\\..\\..\\..\\etc/") is None
        # A `..` run that comes back in is inside again, as a shell would have it.
        assert files_mod._completion_segments(root, "../proj/src/") == ["src"]
        assert files_mod._completion_segments(root, "../../work/proj/src/") == ["src"]
        # And one that cannot pop any further is out, not an exception.
        assert files_mod._completion_segments("/", "../") is None

    def test_a_windows_padded_parent_segment_is_refused(self, monkeypatch):
        """`".. "` is not `".."` to this function but IS to Win32.

        Win32 strips trailing dots and spaces from a component, so a segment the
        check treats as an ordinary name becomes a parent reference at the open --
        the check and the OS disagreeing about one string, which is the same defect
        class as splitting on one separator. The platform flag is flipped at the
        module seam rather than through `os.name`, which would make pathlib build
        WindowsPath objects on Linux.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        root = "/work/proj"
        monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", True)
        # The bypass: a padded parent must not reach the parent directory.
        assert files_mod._completion_segments(root, ".. /") is None
        assert files_mod._completion_segments(root, "./.. /") is None
        # Only TRAILING padding is stripped by Win32, so a leading-dot name is fine.
        assert files_mod._completion_segments(root, "./...ok/") == ["...ok"]
        assert files_mod._completion_segments(root, "./src /") is None
        assert files_mod._completion_segments(root, "./src./") is None
        # And a BARE `..` is a parent reference, not a padded name: the rule must
        # not fire for it, or `../` completion stops working on Windows entirely.
        assert files_mod._completion_segments(root, "../proj/src/") == ["src"]
        assert files_mod._completion_segments(root, "./src/../") == []
        assert files_mod._completion_segments(root, "./a/b/../") == ["a"]
        monkeypatch.undo()
        # POSIX keeps padded names: there they are ordinary, distinct filenames.
        monkeypatch.setattr(files_mod.platform_compat, "IS_WINDOWS", False)
        assert files_mod._completion_segments(root, "./src /") == ["src "]
        assert files_mod._completion_segments(root, "./.. /") == [".. "]
        monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_a_link_is_never_offered_because_it_can_never_be_entered(
        self, tmp_path, project, mock_sel
    ):
        """One rule replaces every question about where a link points.

        The walk refuses to follow a link, so a completion into one would fail on
        the next keystroke; offering it would be offering a dead end. That covers
        a link at a share, a link out of the project, and a link that stays inside
        it, without resolving any of them.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (project / "share").symlink_to("//attacker/share/secrets")
        (project / "escape").symlink_to(outside, target_is_directory=True)
        (project / "inside").symlink_to(project / "src", target_is_directory=True)
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert _names(data) == ["src", "readme.md"]

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_project_is_refused(self, tmp_path, project, mock_sel):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secrets.txt").write_text("x")
        (project / "link").symlink_to(outside, target_is_directory=True)
        status, data = await _get(str(project), path=str(project), dir="./link/")
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_directory_swapped_for_a_link_mid_walk_is_refused(
        self, tmp_path, project, mock_sel, monkeypatch
    ):
        """A link planted in the window is REFUSED, not followed.

        A same-UID writer -- an agent working in this very project -- can rename a
        component and plant a link at its name at any moment. The walk cannot
        prevent that and does not try: it refuses to follow a link at any
        component, so whichever moment the swap lands in, the answer is a refusal
        rather than a listing of what the link pointed at. The swap is driven
        deterministically here at the moment the root is opened, which is before
        the segment below it is reached.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("x")
        real_pin = files_mod.platform_compat.pin_directory
        src = str(project / "src")

        def racing_pin(path):
            fd = real_pin(path)
            if str(path) == str(project) and os.path.isdir(src):
                os.rename(src, str(project / "moved"))
                os.symlink(str(outside), src, target_is_directory=True)
            return fd

        monkeypatch.setattr(files_mod.platform_compat, "pin_directory", racing_pin)
        status, data = await _get(str(project), path=str(project), dir="./src/")
        assert status == 200
        assert data["results"] == []
        assert any(
            call.kwargs.get("outcome") == "denied"
            and "not a real directory" in (call.kwargs.get("error") or "")
            for call in mock_sel.log_api_access.call_args_list
        )
        # A refusal is a different audit fact from "nothing is there", so it is
        # recorded as one rather than answered silently.
        assert any(
            call.kwargs.get("outcome") == "denied"
            and "not a real directory" in (call.kwargs.get("error") or "")
            for call in mock_sel.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_missing_directory_is_audited_as_an_empty_answer(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./nope/")
        assert status == 200
        assert any(
            call.kwargs.get("outcome") == "allowed"
            and (call.kwargs.get("error") or "") == "no such directory"
            for call in mock_sel.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_missing_directory_is_an_empty_listing(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./nope/")
        assert status == 200
        assert data["results"] == []
        # An absent directory is NOT the project boundary, and does not claim to be.
        assert "outside" not in data

    @pytest.mark.asyncio
    async def test_a_nul_byte_in_the_token_is_an_empty_listing_not_a_500(self, project, mock_sel):
        """A NUL never occurs in a real path, and the resolver raises ValueError
        (not OSError) for one, so it is screened at the boundary."""
        for params in ({"dir": "./\x00"}, {"dir": "./", "q": "a\x00"}):
            status, data = await _get(str(project), path=str(project), **params)
            assert status == 200
            assert data["results"] == []

    @pytest.mark.asyncio
    async def test_the_sensitive_fence_never_resolves_what_it_is_handed(
        self, project, mock_sel, monkeypatch
    ):
        """The fence must not be the network call the rest of the design removes.

        ``is_sensitive_path`` canonicalises its argument, which on Windows follows a
        junction aimed at a share -- so the fence would authenticate over SMB before
        the no-follow open could refuse anything. The non-resolving entry point is
        what this endpoint uses, asserted by leaving the resolving one armed as a
        tripwire.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        def _tripwire(*args, **kwargs):
            raise AssertionError("the resolving sensitive-path fence was used")

        monkeypatch.setattr(files_mod, "is_sensitive_path", _tripwire)
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert _names(data) == ["src", "readme.md"]

    @pytest.mark.asyncio
    async def test_the_fence_judges_the_descriptor_not_the_typed_name(
        self, project, mock_sel, monkeypatch
    ):
        """A second NAME for the same directory must not get a second verdict.

        On Windows an 8.3 alias (`SSH~1`) is another name the filesystem keeps for
        `.ssh`, so no lexical fence can see they are the same directory. The witness
        is the kernel's answer for the descriptor already held open, which is the one
        name that cannot be spelled around -- simulated here by having the witness
        report the aliased directory's real name.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        alias = project / "SSH~1"
        alias.mkdir()
        (alias / "id_rsa").write_text("x")
        real_name = str(project / ".ssh")

        monkeypatch.setattr(files_mod.pinned_fs, "fd_real_path", lambda fd: real_name)
        monkeypatch.setattr(files_mod, "is_sensitive_resolved_path", lambda p: p == real_name)
        status, data = await _get(str(project), path=str(project), dir="./SSH~1/")
        assert status == 403
        assert data["code"] == "access_denied"

    @pytest.mark.asyncio
    async def test_an_unreadable_descriptor_name_fails_closed(self, project, mock_sel, monkeypatch):
        """No witness means nothing to validate, so the request is refused."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod.pinned_fs, "fd_real_path", lambda fd: None)
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert data["results"] == []
        assert any(
            call.kwargs.get("outcome") == "denied"
            and "not a real directory" in (call.kwargs.get("error") or "")
            for call in mock_sel.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_witness_outside_the_root_is_refused(
        self, tmp_path, project, mock_sel, monkeypatch
    ):
        """The canonical name gets its own containment check.

        The lexical pass judged the string the caller typed; only this judges the
        directory it turned out to name.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("x")
        monkeypatch.setattr(files_mod.pinned_fs, "fd_real_path", lambda fd: str(outside))
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert data["results"] == []
        assert data["outside"] is True

    @pytest.mark.asyncio
    async def test_a_sensitive_project_root_is_403(self, project, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        with patch.object(files_mod, "is_sensitive_resolved_path", lambda p: True):
            status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 403
        assert data["code"] == "access_denied"

    @pytest.mark.asyncio
    async def test_a_sensitive_entry_is_not_offered(self, project, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        secret = os.path.realpath(str(project / "readme.md"))
        with patch.object(files_mod, "is_sensitive_resolved_path", lambda p: p == secret):
            status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert _names(data) == ["src"]

    @pytest.mark.asyncio
    async def test_the_result_set_is_capped(self, tmp_path, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        root = tmp_path / "many"
        root.mkdir()
        for i in range(files_mod._PATH_COMPLETE_MAX_ENTRIES + 10):
            (root / f"f{i:03d}.txt").write_text("x")
        status, data = await _get(str(root), path=str(root), dir="./")
        assert status == 200
        assert len(data["results"]) == files_mod._PATH_COMPLETE_MAX_ENTRIES
